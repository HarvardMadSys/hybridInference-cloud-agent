"""The Usage page's state names, limits, sample intervals and windows."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from cloud_agent.owner_usage import (
    MAX_ALL_TIME_DAYS,
    MAX_SAMPLE_GAP_SECONDS,
    all_time_window,
    container_state,
    cpu_limit_cores,
    days_period,
    history_window,
    measured_interval,
    memory_limit_bytes,
    utc_day_shares,
)

NOW = datetime(2026, 10, 15, 13, 30, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("state", "deletion_state", "expected"),
    [
        ("provisioning", "none", "starting"),
        ("starting", "none", "starting"),
        ("restoring", "none", "starting"),
        ("running", "none", "running"),
        ("stopping", "none", "stopping"),
        ("stopped", "none", "stopped"),
        ("stopped_dirty", "none", "stopped"),
        ("cold", "none", "stored"),
        ("lost", "none", "unavailable"),
        ("running", "pending", "deleting"),
        ("stopped", "archived", "deleting"),
        ("stopped", "failed", "stopped"),
        ("a-state-added-later", "none", "unavailable"),
    ],
)
def test_every_stored_state_reads_as_one_owner_state(state, deletion_state, expected):
    assert container_state(state, deletion_state) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("4g", 4 * 1024**3),
        ("4G", 4 * 1024**3),
        ("4gb", 4 * 1024**3),
        ("512m", 512 * 1024**2),
        ("1.5g", int(1.5 * 1024**3)),
        ("1048576", 1024**2),
        ("2048k", 2 * 1024**2),
        ("0", None),
        ("", None),
        (None, None),
        ("four gigabytes", None),
        ("4x", None),
    ],
)
def test_docker_memory_limits_read_in_binary_units(value, expected):
    assert memory_limit_bytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2", 2.0), ("1.5", 1.5), (" 4 ", 4.0), ("0", None), ("-1", None), ("nan", None), ("", None)],
)
def test_cpu_limits_are_positive_core_counts(value, expected):
    assert cpu_limit_cores(value) == expected


def test_a_sample_stands_for_the_gap_since_the_previous_one():
    """Hosts report every other 15-second sample, so the gap is about 30 seconds."""
    assert (
        measured_interval(
            sampled_at=NOW,
            previous_sampled_at=NOW - timedelta(seconds=30),
            cpu_window_seconds=15.0,
        )
        == 30.0
    )


@pytest.mark.parametrize(
    ("previous", "window", "expected"),
    [
        # A container's first sample after it starts: only its own window.
        (None, 15.0, 15.0),
        (None, None, 0.0),
        # Lost reports: the gap is not assumed to be continuous use.
        (NOW - timedelta(seconds=MAX_SAMPLE_GAP_SECONDS + 1), 15.0, 15.0),
        (NOW - timedelta(hours=2), 300.0, MAX_SAMPLE_GAP_SECONDS),
        # A clock that went backwards proves nothing about the gap either.
        (NOW + timedelta(seconds=5), 15.0, 15.0),
        (NOW, None, 0.0),
    ],
)
def test_a_sample_without_a_usable_gap_stands_for_its_own_window(previous, window, expected):
    assert (
        measured_interval(sampled_at=NOW, previous_sampled_at=previous, cpu_window_seconds=window)
        == expected
    )


@pytest.mark.parametrize(
    ("ended_at", "seconds", "expected"),
    [
        (NOW, 30.0, [(date(2026, 10, 15), 30.0)]),
        # Ten seconds after midnight: the other twenty were used the day before.
        (
            MIDNIGHT + timedelta(seconds=10),
            30.0,
            [(date(2026, 10, 14), 20.0), (date(2026, 10, 15), 10.0)],
        ),
        # The same moment on another clock is split at the same UTC midnight.
        (
            datetime(2026, 10, 15, 8, 0, 10, tzinfo=timezone(timedelta(hours=8))),
            30.0,
            [(date(2026, 10, 14), 20.0), (date(2026, 10, 15), 10.0)],
        ),
        # Taken exactly at midnight, it stands for the end of the day before.
        (MIDNIGHT, 30.0, [(date(2026, 10, 14), 30.0)]),
        # Beginning exactly at midnight, it is all the new day's.
        (
            MIDNIGHT + timedelta(seconds=MAX_SAMPLE_GAP_SECONDS),
            MAX_SAMPLE_GAP_SECONDS,
            [(date(2026, 10, 15), MAX_SAMPLE_GAP_SECONDS)],
        ),
        (NOW, 0.0, []),
    ],
)
def test_the_time_a_sample_stands_for_is_split_at_utc_midnight(ended_at, seconds, expected):
    assert utc_day_shares(ended_at=ended_at, seconds=seconds) == expected


def test_a_history_window_is_whole_utc_days_up_to_now():
    since, until = history_window(7, now=NOW)
    assert since == datetime(2026, 10, 9, tzinfo=UTC)
    assert until == NOW
    assert history_window(1, now=NOW) == (datetime(2026, 10, 15, tzinfo=UTC), NOW)


@pytest.mark.parametrize("days", [0, -1, 91])
def test_a_history_window_is_bounded(days):
    with pytest.raises(ValueError, match="between 1 and 90"):
        history_window(days, now=NOW)


def test_a_days_period_names_its_kind_and_reads_no_first_activity():
    period = days_period(30, now=NOW)
    assert period.kind == "days"
    assert (period.since, period.until) == history_window(30, now=NOW)
    assert period.first_activity_at is None and period.limited_by is None


def test_all_time_without_any_run_is_today():
    period = all_time_window(earliest=None, overflow=None, now=NOW)
    assert period.kind == "all"
    assert (period.since, period.until) == (MIDNIGHT, NOW)
    assert period.first_activity_at is None and period.limited_by is None


def test_all_time_starts_on_the_first_runs_utc_day():
    # 01:30 in UTC+8 is the evening before in UTC: that UTC day is the first.
    first = datetime(2026, 9, 2, 1, 30, tzinfo=timezone(timedelta(hours=8)))
    period = all_time_window(earliest=first, overflow=None, now=NOW)
    assert period.since == datetime(2026, 9, 1, tzinfo=UTC)
    assert period.until == NOW
    assert period.first_activity_at == first and period.limited_by is None


def test_all_time_leaves_out_the_whole_day_the_run_budget_ends_in():
    first = datetime(2026, 8, 7, 9, tzinfo=UTC)
    overflow = datetime(2026, 10, 3, 17, 45, tzinfo=UTC)
    period = all_time_window(earliest=first, overflow=overflow, now=NOW)
    # Not 2026-10-03: only part of that day's runs fit, so none of it is shown.
    assert period.since == datetime(2026, 10, 4, tzinfo=UTC)
    assert period.first_activity_at == first and period.limited_by == "runs"


def test_a_run_budget_that_ends_today_still_reads_today():
    """The read is then refused as too large rather than given no days at all."""
    first = datetime(2026, 10, 1, tzinfo=UTC)
    period = all_time_window(earliest=first, overflow=NOW - timedelta(minutes=5), now=NOW)
    assert (period.since, period.until) == (MIDNIGHT, NOW)
    assert period.limited_by == "runs"


def test_all_time_spans_at_most_its_bound():
    first = MIDNIGHT - timedelta(days=MAX_ALL_TIME_DAYS + 40)
    period = all_time_window(earliest=first, overflow=None, now=NOW)
    assert period.since == MIDNIGHT - timedelta(days=MAX_ALL_TIME_DAYS - 1)
    assert period.limited_by == "days"
    # The first day inside the bound is still the owner's own first day.
    inside = MIDNIGHT - timedelta(days=MAX_ALL_TIME_DAYS - 1)
    assert all_time_window(earliest=inside, overflow=None, now=NOW).limited_by is None


def test_the_later_of_two_limits_names_the_reason():
    first = MIDNIGHT - timedelta(days=MAX_ALL_TIME_DAYS + 40)
    older = MIDNIGHT - timedelta(days=MAX_ALL_TIME_DAYS + 10)
    newer = MIDNIGHT - timedelta(days=20, hours=-3)
    assert all_time_window(earliest=first, overflow=older, now=NOW).limited_by == "days"
    period = all_time_window(earliest=first, overflow=newer, now=NOW)
    assert period.limited_by == "runs"
    assert period.since == MIDNIGHT - timedelta(days=19)
