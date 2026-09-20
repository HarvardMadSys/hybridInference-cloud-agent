"""Cross-task aggregation: one job means the same thing here as everywhere else."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone


def timezone_offset(hours: int) -> timezone:
    """An explicit offset, so equivalent local boundaries select the same jobs."""
    return timezone(timedelta(hours=hours))


from tests.unit.test_ledger_snapshots import END, ledger_row, report_payload

from cloud_agent.admin_usage import (
    BREAKDOWN_TOP_GROUPS,
    MAX_USAGE_WINDOW_DAYS,
    aggregate_jobs,
    breakdown,
    day_buckets,
    day_key,
    days_breakdown,
    resolve_usage_window,
)
from cloud_agent.ledger import summarize_job_ledger

TERMINAL = ("succeeded", "failed", "cancelled")
DAY = datetime(2026, 9, 9, tzinfo=UTC)


def job(
    *,
    job_id: str = "ajob_1",
    user_id: str = "owner-one",
    model: str = "model-a",
    created_at: datetime = DAY,
    state: str = "succeeded",
    ledger: list[dict] | None = None,
    runtime: str = "claude-code",
    repo: str = "example/repository",
) -> dict:
    """One selected job row as the bounded admin usage read returns it."""
    rows = [ledger_row()] if ledger is None else ledger
    for row in rows:
        row["job_id"] = job_id
    return {
        "id": job_id,
        "user_id": user_id,
        "model": model,
        "runtime": runtime,
        "repo": repo,
        "state": state,
        "created_at": created_at,
        "ledger": rows,
    }


def spend(amount: str, *, calls: int = 1) -> dict:
    """A report with a known amount, so ranking and totals are explicit."""
    payload = report_payload()
    payload["totals"] = {**payload["totals"], "spent_usd": amount, "calls": calls}
    return payload


def test_the_same_job_is_summarized_once_with_the_owner_accounting_rules():
    summary = aggregate_jobs([job()], terminal_states=TERMINAL)
    reference = summarize_job_ledger(job()["ledger"], terminal=True)
    assert summary.run_count == 1
    assert summary.attempt_count == len(reference.attempts)
    assert summary.runs_with_snapshot == 1
    assert summary.missing_attempts == reference.missing_attempts
    assert summary.totals is not None and reference.totals is not None
    assert summary.totals.model_dump() == reference.totals.model_dump()
    assert summary.unknown_rows == reference.unknown_rows
    assert summary.status_counts["available"] == 1
    assert sum(summary.status_counts.values()) == summary.run_count


def test_a_job_without_a_snapshot_contributes_coverage_and_no_zero_usage():
    pending = job(job_id="ajob_pending", state="running", ledger=[])
    empty = job(
        job_id="ajob_empty",
        ledger=[
            ledger_row(
                job_id="ajob_empty",
                snapshot=report_payload(
                    totals={
                        "calls": 0,
                        "tokens_in": None,
                        "tokens_out": None,
                        "cache_read": None,
                        "cache_write": None,
                        "reasoning": None,
                        "spent_usd": None,
                    },
                    unknown_rows=dict.fromkeys(report_payload()["unknown_rows"], 0),
                ),
            )
        ],
    )
    summary = aggregate_jobs([pending, empty], terminal_states=TERMINAL)
    assert summary.run_count == 2
    assert summary.runs_with_snapshot == 1
    assert summary.totals is not None
    # A zero-call snapshot is a real zero, and it does not invent a cost.
    assert summary.totals.calls == 0
    assert summary.totals.spent_usd is None
    assert summary.totals.tokens_in is None
    assert summary.status_counts["pending"] == 1
    assert summary.status_counts["available"] == 1
    assert summary.active_runs == 1


def test_partly_known_metrics_sum_what_is_known_and_count_the_rest():
    known = job(job_id="ajob_known")
    unknown = job(
        job_id="ajob_unknown",
        ledger=[
            ledger_row(
                job_id="ajob_unknown",
                snapshot=report_payload(
                    totals={
                        "calls": 1,
                        "tokens_in": 120,
                        "tokens_out": 20,
                        "cache_read": 100,
                        "cache_write": 0,
                        "reasoning": None,
                        "spent_usd": None,
                    },
                    unknown_rows={**report_payload()["unknown_rows"], "spent_usd": 1},
                ),
            )
        ],
    )
    summary = aggregate_jobs([known, unknown], terminal_states=TERMINAL)
    assert summary.totals is not None and summary.unknown_rows is not None
    assert summary.totals.tokens_in == 240
    assert summary.totals.spent_usd == "0.00012345"
    assert summary.unknown_rows.spent_usd == 1
    assert summary.runs_with_snapshot == 2


def test_estimated_calls_and_missing_attempts_are_carried_through():
    job_with_gap = job(
        ledger=[
            ledger_row(
                job_id="ajob_1", id=None, attempt_id=9, attempt_no=1, snapshot=None, queried_at=None
            ),
            ledger_row(job_id="ajob_1", attempt_no=2, snapshot=report_payload(estimated_calls=1)),
        ]
    )
    summary = aggregate_jobs([job_with_gap], terminal_states=TERMINAL)
    assert summary.estimated_calls == 1
    assert summary.missing_attempts == 1
    assert summary.runs_with_snapshot == 1
    assert summary.oldest_queried_at == END


def test_oldest_queried_snapshot_is_the_minimum_known_read_time():
    earlier = END - timedelta(minutes=5)
    summary = aggregate_jobs(
        [
            job(job_id="ajob_1", ledger=[ledger_row(job_id="ajob_1", queried_at=END)]),
            job(
                job_id="ajob_2",
                ledger=[ledger_row(job_id="ajob_2", queried_at=earlier)],
            ),
        ],
        terminal_states=TERMINAL,
    )
    assert summary.oldest_queried_at == earlier


def test_day_buckets_cover_every_intersecting_utc_day_without_overlap():
    since = datetime(2026, 9, 9, 23, 59, tzinfo=UTC)
    until = datetime(2026, 9, 11, 0, 30, tzinfo=UTC)
    buckets = day_buckets(since=since, until=until)
    assert [day_key(start) for start, _ in buckets] == ["2026-09-09", "2026-09-10", "2026-09-11"]
    # The first and last buckets return the intersection, never a day outside
    # the selection the administrator asked for.
    assert buckets[0] == (since, datetime(2026, 9, 10, tzinfo=UTC))
    assert buckets[-1] == (datetime(2026, 9, 11, tzinfo=UTC), until)
    assert buckets[1] == (
        datetime(2026, 9, 10, tzinfo=UTC),
        datetime(2026, 9, 11, tzinfo=UTC),
    )


def test_a_run_keeps_its_whole_usage_on_its_creation_day():
    created_late = datetime(2026, 9, 10, 23, 59, tzinfo=UTC)
    rows = [
        job(job_id="ajob_late", created_at=created_late),
        job(job_id="ajob_next", created_at=datetime(2026, 9, 11, 0, 1, tzinfo=UTC)),
    ]
    days = days_breakdown(
        rows,
        buckets=day_buckets(
            since=datetime(2026, 9, 10, tzinfo=UTC), until=datetime(2026, 9, 12, tzinfo=UTC)
        ),
        terminal_states=TERMINAL,
    )
    # The selection ends at 2026-09-12T00:00Z, so its last creation day is
    # the 11th and the whole run created at 23:59 on the 10th counts there.
    assert [day.date for day in days] == ["2026-09-10", "2026-09-11"]
    assert [day.usage.run_count for day in days] == [1, 1]
    assert days[0].usage.totals is not None
    assert days[0].usage.totals.calls == 1


def test_an_equivalent_offset_selects_the_same_jobs_and_days():
    created = datetime(2026, 9, 10, 8, tzinfo=UTC)
    rows = [job(created_at=created)]
    utc_days = days_breakdown(
        rows,
        buckets=day_buckets(
            since=datetime(2026, 9, 10, tzinfo=UTC), until=datetime(2026, 9, 11, tzinfo=UTC)
        ),
        terminal_states=TERMINAL,
    )
    offset_days = days_breakdown(
        rows,
        buckets=day_buckets(
            since=datetime(2026, 9, 10, 9, tzinfo=timezone_offset(9)),
            until=datetime(2026, 9, 11, 9, tzinfo=timezone_offset(9)),
        ),
        terminal_states=TERMINAL,
    )
    assert [day.date for day in utc_days] == [day.date for day in offset_days]
    assert offset_days[0].usage.run_count == 1


def test_days_and_breakdowns_add_up_to_the_summary():
    rows = [
        job(job_id="ajob_1", created_at=datetime(2026, 9, 9, 1, tzinfo=UTC)),
        job(
            job_id="ajob_2",
            user_id="owner-two",
            model="model-b",
            created_at=datetime(2026, 9, 10, 1, tzinfo=UTC),
        ),
        job(
            job_id="ajob_3",
            user_id="owner-two",
            model="",
            created_at=datetime(2026, 9, 10, 2, tzinfo=UTC),
            state="running",
            ledger=[],
        ),
    ]
    summary = aggregate_jobs(rows, terminal_states=TERMINAL)
    days = days_breakdown(
        rows,
        buckets=day_buckets(
            since=datetime(2026, 9, 9, tzinfo=UTC), until=datetime(2026, 9, 11, tzinfo=UTC)
        ),
        terminal_states=TERMINAL,
    )
    users = breakdown(rows, dimension="user_id", terminal_states=TERMINAL)
    models = breakdown(rows, dimension="model", terminal_states=TERMINAL)
    assert sum(day.usage.run_count for day in days) == summary.run_count
    assert sum(day.usage.attempt_count for day in days) == summary.attempt_count
    assert (
        sum(group.usage.run_count for group in users.groups)
        + (users.other.usage.run_count if users.other else 0)
        == summary.run_count
    )
    assert (
        sum(group.usage.run_count for group in models.groups)
        + (models.other.usage.run_count if models.other else 0)
        == summary.run_count
    )
    # The two breakdowns partition the summary; adding them together would
    # double count every run, so only each of them is compared to it.
    assert (
        sum(group.usage.totals.calls for group in users.groups if group.usage.totals)
        + (users.other.usage.totals.calls if users.other and users.other.usage.totals else 0)
        == summary.totals.calls
    )
    assert [group.key for group in users.groups] == ["owner-one", "owner-two"]
    # An empty configured model is one explicit unknown group, never a guess.
    assert [group.key for group in models.groups] == ["model-a", "model-b", None]


def test_groups_rank_by_recorded_cost_and_unknown_cost_ranks_last():
    rows = [
        job(job_id="ajob_1", user_id="cheap", ledger=[ledger_row(snapshot=spend("0.01"))]),
        job(job_id="ajob_2", user_id="dear", ledger=[ledger_row(snapshot=spend("2.00"))]),
        job(job_id="ajob_3", user_id="unknown-cost", state="running", ledger=[]),
    ]
    groups = breakdown(rows, dimension="user_id", terminal_states=TERMINAL)
    assert [group.key for group in groups.groups] == ["dear", "cheap", "unknown-cost"]
    assert groups.groups[-1].usage.totals is None
    assert groups.other is None


def test_equal_costs_tie_break_on_the_whole_key_in_utf8_order():
    rows = [
        job(job_id="ajob_1", user_id="zeta", ledger=[ledger_row(snapshot=spend("0.50"))]),
        job(job_id="ajob_2", user_id="alpha", ledger=[ledger_row(snapshot=spend("0.50"))]),
        job(job_id="ajob_3", user_id="Ünicode", ledger=[ledger_row(snapshot=spend("0.50"))]),
    ]
    groups = breakdown(rows, dimension="user_id", terminal_states=TERMINAL)
    # UTF-8 byte order puts ASCII before multi-byte keys, whatever the database
    # collation would have done with the same strings.
    assert [group.key for group in groups.groups] == ["alpha", "zeta", "Ünicode"]


def test_more_than_twenty_groups_are_ranked_and_the_remainder_is_aggregated():
    rows = [
        job(
            job_id=f"ajob_{index:03d}",
            user_id=f"owner-{index:03d}",
            ledger=[ledger_row(snapshot=spend(f"{index}.00"))],
        )
        for index in range(1, BREAKDOWN_TOP_GROUPS + 6)
    ]
    groups = breakdown(rows, dimension="user_id", terminal_states=TERMINAL)
    assert groups.group_count == BREAKDOWN_TOP_GROUPS + 5
    assert len(groups.groups) == BREAKDOWN_TOP_GROUPS
    assert groups.groups[0].key == f"owner-{BREAKDOWN_TOP_GROUPS + 5:03d}"
    assert groups.other is not None
    assert groups.other.key is None
    assert groups.other.usage.run_count == 5
    assert groups.other.usage.totals is not None
    assert groups.other.usage.totals.spent_usd == "15.00"
    # The remainder is a real aggregation, so its coverage is complete too.
    assert groups.other.usage.runs_with_snapshot == 5


def test_a_full_key_is_never_truncated_into_another_group():
    long_key = "owner-" + "x" * 300
    shared_prefix = long_key[:260]
    rows = [
        job(job_id="ajob_1", user_id=long_key, ledger=[ledger_row(snapshot=spend("1.00"))]),
        job(job_id="ajob_2", user_id=shared_prefix, ledger=[ledger_row(snapshot=spend("1.00"))]),
    ]
    groups = breakdown(rows, dimension="user_id", terminal_states=TERMINAL)
    assert groups.group_count == 2
    assert {group.key for group in groups.groups} == {long_key, shared_prefix}


def test_resolve_usage_window_defaults_bounds_and_rejects_bad_windows():
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    since, until = resolve_usage_window(since=None, until=None, now=now)
    assert until == now
    assert since == now - timedelta(days=7)
    since, until = resolve_usage_window(since=None, until=now, now=None)
    assert since == now - timedelta(days=7)

    offset = timezone_offset(9)
    since, until = resolve_usage_window(
        since=datetime(2026, 9, 10, 9, tzinfo=offset), until=datetime(2026, 9, 11, 9, tzinfo=offset)
    )
    assert since == datetime(2026, 9, 10, tzinfo=UTC)
    assert until == datetime(2026, 9, 11, tzinfo=UTC)

    for bad in (
        (datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC)),
        (datetime(2026, 9, 11, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC)),
        (
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=MAX_USAGE_WINDOW_DAYS + 1),
        ),
    ):
        try:
            resolve_usage_window(since=bad[0], until=bad[1])
        except ValueError:
            continue
        raise AssertionError(f"accepted an unusable window: {bad}")


def test_empty_selection_reports_zero_counts_and_no_totals():
    summary = aggregate_jobs([], terminal_states=TERMINAL)
    assert summary.run_count == 0
    assert summary.attempt_count == 0
    assert summary.totals is None
    assert summary.unknown_rows is None
    assert summary.oldest_queried_at is None
    assert sum(summary.status_counts.values()) == 0
    groups = breakdown([], dimension="user_id", terminal_states=TERMINAL)
    assert groups.group_count == 0
    assert groups.groups == []
    assert groups.other is None
