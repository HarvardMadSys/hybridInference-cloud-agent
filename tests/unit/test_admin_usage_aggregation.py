"""Cross-task aggregation: one job means the same thing here as everywhere else."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

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
from cloud_agent.runtime_usage import NO_RUNTIME_REPORT, summarize_runtime_job

TERMINAL = ("succeeded", "failed", "cancelled")
DAY = datetime(2026, 9, 9, tzinfo=UTC)


def timezone_offset(hours: int) -> timezone:
    """An explicit offset, so equivalent local boundaries select the same jobs."""
    return timezone(timedelta(hours=hours))


def attempt(
    *,
    attempt_no: int = 1,
    saw_usage: bool = True,
    input_tokens: int | None = 20,
    output_tokens: int | None = 10,
    cache_read_tokens: int | None = 80,
    cache_write_tokens: int | None = 0,
) -> dict:
    """One attempt's rollup as the bounded usage read returns it."""
    return {
        "attempt_id": 100 + attempt_no,
        "attempt_no": attempt_no,
        "saw_usage": saw_usage,
        "input_tokens": input_tokens if saw_usage else None,
        "output_tokens": output_tokens if saw_usage else None,
        "cache_read_tokens": cache_read_tokens if saw_usage else None,
        "cache_write_tokens": cache_write_tokens if saw_usage else None,
    }


def job(
    *,
    job_id: str = "ajob_1",
    user_id: str = "owner-one",
    model: str = "model-a",
    created_at: datetime = DAY,
    state: str = "succeeded",
    usage: list[dict] | None = None,
    runtime: str = "claude-code",
    repo: str = "example/repository",
) -> dict:
    """One selected job row with its attempts' usage."""
    rows = [attempt()] if usage is None else usage
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
        "usage": rows,
    }


def tokens(output: int) -> list[dict]:
    """A report with a known output, so ranking and totals are explicit."""
    return [attempt(output_tokens=output, input_tokens=0, cache_read_tokens=0)]


def test_the_same_job_is_summarized_once_with_the_per_turn_rules():
    summary = aggregate_jobs([job()], terminal_states=TERMINAL)
    reference = summarize_runtime_job(job(), terminal=True)
    assert summary.run_count == 1
    assert summary.attempt_count == len(reference.attempts)
    assert summary.runs_with_snapshot == 1
    assert summary.missing_attempts == reference.missing_attempts
    assert summary.totals is not None and reference.totals is not None
    assert summary.totals.model_dump() == reference.totals.model_dump()
    assert summary.unknown_rows == reference.unknown_rows
    assert summary.status_counts["available"] == 1
    assert sum(summary.status_counts.values()) == summary.run_count


def test_input_is_cache_inclusive_only_where_the_agent_establishes_it():
    claude = summarize_runtime_job(job(runtime="claude-code"), terminal=True)
    codex = summarize_runtime_job(
        job(runtime="codex", usage=[attempt(input_tokens=100, cache_read_tokens=80)]),
        terminal=True,
    )
    unverified = summarize_runtime_job(job(runtime="opencode"), terminal=True)
    partial = summarize_runtime_job(
        job(runtime="claude-code", usage=[attempt(cache_write_tokens=None)]), terminal=True
    )
    # Claude Code reports uncached input, cache reads and cache writes apart.
    assert claude.totals is not None and claude.totals.tokens_in == 100
    # Codex input already includes its cache hits.
    assert codex.totals is not None and codex.totals.tokens_in == 100
    # An agent whose convention is not verified, or a missing cache counter,
    # leaves input unknown rather than guessed; what it did report stays.
    for summary in (unverified, partial):
        assert summary.totals is not None and summary.totals.tokens_in is None
        assert summary.totals.tokens_out == 10
        assert summary.unknown_rows is not None and summary.unknown_rows.tokens_in == 1


def test_cost_reasoning_and_calls_are_never_invented():
    summary = aggregate_jobs([job(), job(job_id="ajob_2")], terminal_states=TERMINAL)
    assert summary.totals is not None and summary.unknown_rows is not None
    assert summary.totals.spent_usd is None
    assert summary.totals.reasoning is None
    assert summary.totals.calls is None
    # Each reporting attempt is a row that lacked them.
    assert summary.unknown_rows.spent_usd == 2
    assert summary.unknown_rows.reasoning == 2
    assert summary.estimated_calls == 0


def test_a_job_without_a_report_contributes_coverage_and_no_zero_usage():
    pending = job(job_id="ajob_pending", state="running", usage=[attempt(saw_usage=False)])
    silent = job(job_id="ajob_silent", usage=[attempt(saw_usage=False)])
    summary = aggregate_jobs([pending, silent], terminal_states=TERMINAL)
    assert summary.run_count == 2
    assert summary.runs_with_snapshot == 0
    assert summary.totals is None
    assert summary.status_counts["pending"] == 1
    assert summary.status_counts["unavailable"] == 1
    assert summary.missing_attempts == 1
    assert summary.active_runs == 1
    assert summarize_runtime_job(silent, terminal=True).attempts[0].reason == NO_RUNTIME_REPORT


def test_a_reported_zero_is_a_real_zero():
    summary = aggregate_jobs(
        [job(usage=[attempt(input_tokens=0, output_tokens=0, cache_read_tokens=0)])],
        terminal_states=TERMINAL,
    )
    assert summary.totals is not None
    assert (summary.totals.tokens_in, summary.totals.tokens_out) == (0, 0)


def test_retries_add_each_attempts_own_report_and_count_the_silent_one():
    retried = job(usage=[attempt(attempt_no=1, saw_usage=False), attempt(attempt_no=2)])
    summary = aggregate_jobs([retried], terminal_states=TERMINAL)
    assert summary.attempt_count == 2
    assert summary.missing_attempts == 1
    assert summary.status_counts["partial"] == 1
    assert summary.totals is not None and summary.totals.tokens_out == 10
    twice = aggregate_jobs(
        [job(usage=[attempt(attempt_no=1), attempt(attempt_no=2)])], terminal_states=TERMINAL
    )
    assert twice.totals is not None and twice.totals.tokens_out == 20


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
    assert days[0].usage.totals.tokens_out == 10


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
            usage=[],
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
        sum(group.usage.totals.tokens_out for group in users.groups if group.usage.totals)
        + (users.other.usage.totals.tokens_out if users.other and users.other.usage.totals else 0)
        == summary.totals.tokens_out
    )
    assert [group.key for group in users.groups] == ["owner-one", "owner-two"]
    # An empty configured model is one explicit unknown group, never a guess.
    assert [group.key for group in models.groups] == ["model-a", "model-b", None]


def test_groups_rank_by_reported_tokens_and_unreported_ranks_last():
    rows = [
        job(job_id="ajob_1", user_id="light", usage=tokens(1)),
        job(job_id="ajob_2", user_id="heavy", usage=tokens(200)),
        job(job_id="ajob_3", user_id="unreported", state="running", usage=[]),
    ]
    groups = breakdown(rows, dimension="user_id", terminal_states=TERMINAL)
    assert [group.key for group in groups.groups] == ["heavy", "light", "unreported"]
    assert groups.groups[-1].usage.totals is None
    assert groups.other is None


def test_equal_volumes_tie_break_on_the_whole_key_in_utf8_order():
    rows = [
        job(job_id="ajob_1", user_id="zeta", usage=tokens(50)),
        job(job_id="ajob_2", user_id="alpha", usage=tokens(50)),
        job(job_id="ajob_3", user_id="Ünicode", usage=tokens(50)),
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
            usage=tokens(index),
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
    assert groups.other.usage.totals.tokens_out == 15
    # The remainder is a real aggregation, so its coverage is complete too.
    assert groups.other.usage.runs_with_snapshot == 5


def test_a_full_key_is_never_truncated_into_another_group():
    long_key = "owner-" + "x" * 300
    shared_prefix = long_key[:260]
    rows = [
        job(job_id="ajob_1", user_id=long_key, usage=tokens(1)),
        job(job_id="ajob_2", user_id=shared_prefix, usage=tokens(1)),
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
