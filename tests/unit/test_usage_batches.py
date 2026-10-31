"""Batched owner usage equals the single unbounded aggregate, exactly."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from tests.unit.test_ledger_snapshots import ledger_row, report_payload

from cloud_agent.admin_usage import aggregate_jobs, breakdown, merge_aggregates
from cloud_agent.storage.admin_usage_queries import UsageQueryUnavailable
from cloud_agent.usage_batches import owner_usage
from cloud_agent.usage_range import (
    bucket_key,
    bucket_size,
    buckets,
    day_range,
    resolve_range,
)

TERMINAL = ("succeeded", "failed", "cancelled")
START = datetime(2025, 1, 1, tzinfo=UTC)


def _jobs(count: int) -> list[dict]:
    jobs = []
    for index in range(count):
        snapshot = report_payload()
        if index % 7 == 0:
            # An unknown cost stays unknown and is counted, never summed as 0.
            snapshot["totals"] = {**snapshot["totals"], "spent_usd": None}
            snapshot["unknown_rows"] = {**snapshot["unknown_rows"], "spent_usd": 1}
        else:
            snapshot["totals"] = {**snapshot["totals"], "spent_usd": f"0.{index:08d}"}
        ledger = (
            [] if index % 11 == 0 else [ledger_row(id=index, attempt_id=index, snapshot=snapshot)]
        )
        job_id = f"ajob_{index:05d}"
        for row in ledger:
            row["job_id"] = job_id
        jobs.append(
            {
                "id": job_id,
                "user_id": "owner",
                "thread_id": f"athr_{index % 5}",
                "model": f"model-{index % 3}",
                "runtime": "codex",
                "repo": "r",
                "state": "succeeded",
                "created_at": START + timedelta(hours=7 * index),
                "ledger": ledger,
            }
        )
    return jobs


class _Queries:
    """Partitions the jobs into batches the way the keyset read does."""

    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = jobs
        self.batch_sizes: list[int] = []

    async def batched_selection(self, *, since, until, thread_ids, max_runs, deadline, **_):
        selected = [
            job
            for job in self.jobs
            if since <= job["created_at"] < until
            and (thread_ids is None or job["thread_id"] in thread_ids)
        ]
        for offset in range(0, len(selected), max_runs):
            if time.monotonic() > deadline:
                raise UsageQueryUnavailable
            batch = selected[offset : offset + max_runs]
            self.batch_sizes.append(len(batch))
            cohort = [{k: v for k, v in job.items() if k != "ledger"} for job in batch]
            rows = [row for job in batch for row in job["ledger"]]
            yield cohort, rows


async def test_many_small_batches_sum_to_the_single_aggregate():
    jobs = _jobs(230)
    until = START + timedelta(days=200)
    queries = _Queries(jobs)
    usage = await owner_usage(
        queries, user_id="owner", since=START, until=until, size="week", max_runs=17
    )
    assert usage.batches == len(queries.batch_sizes) > 10
    single = aggregate_jobs(jobs, terminal_states=TERMINAL)
    assert usage.summary.model_dump() == single.model_dump()
    assert usage.summary.totals is not None
    assert usage.summary.unknown_rows is not None and usage.summary.unknown_rows.spent_usd > 0
    # Buckets partition the runs; empty buckets are present, not skipped.
    assert sum(bucket.usage.run_count for bucket in usage.buckets) == 230
    assert [b.date for b in usage.buckets] == [
        bucket_key(start, "week") for start, _ in buckets(START, until, "week")
    ]
    reference = breakdown(jobs, dimension="model", terminal_states=TERMINAL)
    assert usage.by_model.model_dump() == reference.model_dump()


async def test_thread_selection_sums_each_conversation():
    jobs = _jobs(60)
    usage = await owner_usage(
        _Queries(jobs),
        user_id="owner",
        since=START,
        until=START + timedelta(days=60),
        size="day",
        thread_ids=["athr_1", "athr_2"],
        max_runs=9,
    )
    assert set(usage.by_thread) == {"athr_1", "athr_2"}
    for thread, aggregate in usage.by_thread.items():
        expected = aggregate_jobs(
            [j for j in jobs if j["thread_id"] == thread], terminal_states=TERMINAL
        )
        assert aggregate.model_dump() == expected.model_dump()


async def test_the_deadline_refuses_rather_than_returning_part():
    with pytest.raises(UsageQueryUnavailable):
        await owner_usage(
            _Queries(_jobs(50)),
            user_id="owner",
            since=START,
            until=START + timedelta(days=60),
            size="day",
            max_runs=5,
            deadline_s=-1,
        )


def test_merging_nothing_is_an_empty_aggregate_with_unknown_totals():
    merged = merge_aggregates([])
    assert merged.run_count == 0 and merged.totals is None


def test_presets_and_buckets():
    now = datetime(2026, 10, 29, 15, 4, tzinfo=UTC)
    today = resolve_range("today", first_record_at=None, now=now)
    assert today.since == datetime(2026, 10, 29, tzinfo=UTC) and today.until == now
    week = resolve_range("7d", first_record_at=None, now=now)
    assert week.since == datetime(2026, 10, 23, tzinfo=UTC)
    month = resolve_range("30d", first_record_at=None, now=now)
    assert len(buckets(month.since, month.until, month.bucket)) == 30
    long_ago = datetime(2019, 2, 3, 4, 5, tzinfo=UTC)
    every = resolve_range("all", first_record_at=long_ago, now=now)
    assert every.since == datetime(2019, 2, 3, tzinfo=UTC) and every.bucket == "month"
    spans = buckets(every.since, every.until, every.bucket)
    assert spans[0][0] == every.since and spans[-1][1] == now
    assert all(a[1] == b[0] for a, b in pairwise(spans))
    assert bucket_size(now - timedelta(days=91), now) == "day"
    assert bucket_size(now - timedelta(days=200), now) == "week"
    # A future first record (clock skew) never starts the range after today.
    assert (
        resolve_range("all", first_record_at=now + timedelta(days=2), now=now).since == today.since
    )
    assert day_range(7, max_days=90, now=now).since == week.since
    with pytest.raises(ValueError):
        day_range(91, max_days=90, now=now)
