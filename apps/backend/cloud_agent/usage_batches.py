"""Sum one owner's model usage over any range, in bounded batches.

The admin Usage aggregation refuses a selection over its run, row and snapshot
budgets, which protected the database by making long ranges impossible. An
owner's ``all`` range must cover everything they have, so it is read in
batches instead (:meth:`AdminUsageQueries.batched_selection`): each batch is
held to the same budgets, all of them come from one snapshot, and each is
reduced and merged before the next is read, so memory stays bounded by one
batch. The merge is exact (:func:`cloud_agent.admin_usage.merge_aggregates`):
the result is the aggregate a single unbounded selection would have returned.

Two more bounds protect the service: a read that needs more than one batch
waits for one of :data:`MULTI_BATCH_SLOTS`, and every read stops at
:data:`OWNER_USAGE_DEADLINE_S` with an error rather than a partial sum.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent.admin_usage import (
    MAX_USAGE_LEDGER_ROWS,
    MAX_USAGE_RUNS,
    MAX_USAGE_SNAPSHOT_BYTES,
    UNKNOWN_MODEL_GROUP,
    AdminUsageAggregate,
    AdminUsageBreakdown,
    AdminUsageDay,
    grouped_aggregates,
    merge_aggregates,
    prepare_jobs,
    ranked_breakdown,
)
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.storage.admin_usage_queries import UsageQueryUnavailable
from cloud_agent.usage_range import BucketSize, bucket_key, buckets

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from cloud_agent.storage.admin_usage_queries import AdminUsageQueries

#: The longest one owner's usage read may take, all batches included.
OWNER_USAGE_DEADLINE_S = 60.0
#: Multi-batch reads running at once in this process. A read that fits one
#: batch never waits for a slot.
MULTI_BATCH_SLOTS = 2
_SLOTS = asyncio.Semaphore(MULTI_BATCH_SLOTS)


@dataclass
class BatchedUsage:
    """The owner's usage over a range, every run counted exactly once."""

    summary: AdminUsageAggregate
    buckets: list[AdminUsageDay]
    by_model: AdminUsageBreakdown
    by_thread: dict[str, AdminUsageAggregate] = field(default_factory=dict)
    batches: int = 0


class _Merger:
    """Running aggregates per key, merged after every batch."""

    def __init__(self) -> None:
        self.values: dict[Any, AdminUsageAggregate] = {}

    def add(self, parts: dict[Any, AdminUsageAggregate]) -> None:
        for key, part in parts.items():
            known = self.values.get(key)
            self.values[key] = part if known is None else merge_aggregates([known, part])


def _reduce(
    cohort: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    size: BucketSize,
    by_thread: bool,
) -> tuple[dict[Any, AdminUsageAggregate], ...]:
    """One batch's summary, bucket, model and thread aggregates (CPU-bound)."""
    ledger_by_job: dict[str, list[dict[str, Any]]] = {row["id"]: [] for row in cohort}
    for row in rows:
        ledger_by_job[row["job_id"]].append(row)
    jobs = [{**row, "ledger": ledger_by_job[row["id"]]} for row in cohort]
    prepared = prepare_jobs(jobs, terminal_states=TERMINAL_STATES)
    summary = grouped_aggregates(prepared, lambda _row: None)
    per_bucket = grouped_aggregates(prepared, lambda row: bucket_key(row["created_at"], size))
    per_model = grouped_aggregates(prepared, lambda row: row.get("model") or UNKNOWN_MODEL_GROUP)
    per_thread = grouped_aggregates(prepared, lambda row: row["thread_id"]) if by_thread else {}
    return summary, per_bucket, per_model, per_thread


def _empty() -> AdminUsageAggregate:
    return AdminUsageAggregate(run_count=0, attempt_count=0)


@contextlib.asynccontextmanager
async def _slot(deadline: float) -> AsyncIterator[None]:
    remaining = deadline - time.monotonic()
    try:
        await asyncio.wait_for(_SLOTS.acquire(), timeout=max(0.0, remaining))
    except TimeoutError:
        raise UsageQueryUnavailable from None
    try:
        yield
    finally:
        _SLOTS.release()


async def owner_usage(
    queries: AdminUsageQueries,
    *,
    user_id: str,
    since: datetime,
    until: datetime,
    size: BucketSize,
    thread_ids: list[str] | None = None,
    max_runs: int = MAX_USAGE_RUNS,
    deadline_s: float = OWNER_USAGE_DEADLINE_S,
) -> BatchedUsage:
    """The owner's usage of runs created in ``[since, until)``, bucketed by ``size``.

    Pass ``thread_ids`` to sum only those conversations, with a per-thread
    aggregate for each.

    Raises:
        UsageQueryTooLarge: If one minimal batch exceeds a budget.
        UsageQueryUnavailable: If a statement fails or the deadline passes.
    """
    deadline = time.monotonic() + deadline_s
    summary, per_bucket, per_model, per_thread = _Merger(), _Merger(), _Merger(), _Merger()
    batches = 0
    slot: contextlib.AsyncExitStack = contextlib.AsyncExitStack()
    async with slot:
        async for cohort, rows in queries.batched_selection(
            since=since,
            until=until,
            user_id=user_id,
            thread_ids=thread_ids,
            max_runs=max_runs,
            max_rows=MAX_USAGE_LEDGER_ROWS,
            max_snapshot_bytes=MAX_USAGE_SNAPSHOT_BYTES,
            deadline=deadline,
        ):
            if batches == 1:
                # A second batch makes this a long read: it now shares a slot.
                await slot.enter_async_context(_slot(deadline))
            batches += 1
            parts = await asyncio.to_thread(
                _reduce, cohort, rows, size=size, by_thread=thread_ids is not None
            )
            for merger, part in zip(
                (summary, per_bucket, per_model, per_thread), parts, strict=True
            ):
                merger.add(part)
    return BatchedUsage(
        summary=summary.values.get(None) or _empty(),
        buckets=[
            AdminUsageDay(
                date=bucket_key(start, size),
                since=start,
                until=end,
                usage=per_bucket.values.get(bucket_key(start, size)) or _empty(),
            )
            for start, end in buckets(since, until, size)
        ],
        by_model=ranked_breakdown(per_model.values),
        by_thread=per_thread.values,
        batches=batches,
    )
