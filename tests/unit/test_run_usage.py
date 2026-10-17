"""User-facing usage preserves input definitions and conversation gaps."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.unit.test_ledger_snapshots import END, ledger_row, report_payload

from cloud_agent.ledger import summarize_conversation
from cloud_agent.thread_stats import normalized_runtime_input_tokens


def _event(usage, *, attempt_id=1, seq=1, **payload):
    return {
        "attempt_id": attempt_id,
        "seq": seq,
        "event_type": "usage",
        "payload": {"usage": usage, **payload},
    }


@pytest.mark.parametrize("runtime", ["claude-code", "pi"])
def test_cache_counters_are_added_once_per_response_and_per_attempt(runtime):
    counters = {"input_tokens": 100, "cache_read_tokens": 200, "cache_write_tokens": 30}
    events = [
        _event(counters, usage_id="same", seq=1),
        _event(counters, usage_id="same", seq=2),
        _event(counters, usage_id="same", seq=3, attempt_id=2),
    ]
    assert normalized_runtime_input_tokens(runtime=runtime, events=events) == 660


def test_attempt_total_replaces_incremental_responses_and_input_zero_is_valid():
    events = [
        _event({"input_tokens": 100}, usage_id="response"),
        _event(
            {"input_tokens": 0, "cache_read_tokens": 200, "cache_write_tokens": 30},
            seq=2,
            usage_scope="attempt",
        ),
    ]
    assert normalized_runtime_input_tokens(runtime="claude-code", events=events) == 230


def test_codex_input_already_includes_cached_tokens():
    events = [_event({"input_tokens": 400, "cached_input_tokens": 250})]
    assert normalized_runtime_input_tokens(runtime="codex", events=events) == 400


@pytest.mark.parametrize("runtime", ["claude-code", "pi", "kilo", "opencode", None])
def test_missing_cache_or_unknown_runtime_cannot_establish_inclusive_input(runtime):
    events = [_event({"input_tokens": 100, "cache_read_tokens": 200})]
    assert normalized_runtime_input_tokens(runtime=runtime, events=events) is None


def test_missing_counter_in_one_response_does_not_create_a_partial_input_total():
    events = [
        _event({"input_tokens": 100, "cache_read_tokens": 200, "cache_write_tokens": 30}),
        _event({"output_tokens": 20}, seq=2),
    ]
    assert normalized_runtime_input_tokens(runtime="claude-code", events=events) is None
    assert normalized_runtime_input_tokens(runtime="codex", events=events) is None


def test_conversation_sums_turns_with_equal_attempt_numbers_without_rounding():
    rows = [
        ledger_row(job_id="job1"),
        ledger_row(job_id="job2", attempt_id=11, queried_at=END + timedelta(minutes=1)),
        {"job_id": "queued", "attempt_id": None},
    ]
    summary = summarize_conversation(rows)
    assert summary.turn_count == 3
    assert summary.totals.calls == 2
    assert summary.totals.spent_usd == "0.00024690"
    assert summary.queried_at == END
    assert summary.incomplete is False  # Unreported reasoning alone is not a gap.


@pytest.mark.parametrize("change", ["missing", "stale", "unknown_cost"])
def test_conversation_keeps_known_totals_but_names_incomplete_coverage(change):
    second = ledger_row(job_id="job2", attempt_id=11)
    if change == "missing":
        second.update(id=None, snapshot=None, queried_at=None)
    elif change == "stale":
        second["last_error_code"] = "usage_unavailable"
    else:
        report = report_payload()
        report["totals"]["spent_usd"] = None
        report["unknown_rows"]["spent_usd"] = 1
        second["snapshot"] = report
    summary = summarize_conversation([ledger_row(job_id="job1"), second])
    assert summary.incomplete is True
    assert summary.totals.calls >= 1
    assert summary.queried_at == END


def test_a_turn_that_never_called_a_model_is_no_gap_in_its_conversation():
    """It spent nothing, so the conversation's totals are all there is."""
    quiet = {
        "job_id": "quiet",
        "attempt_id": 12,
        "attempt_no": 1,
        "attempt_status": "finished",
        "model_usage_reported": False,
        "id": None,
        "snapshot": None,
    }
    summary = summarize_conversation([quiet, ledger_row(job_id="job2", attempt_id=11)])
    assert summary.turn_count == 2
    assert summary.totals.calls == 1
    assert summary.incomplete is False


def test_unstarted_conversation_does_not_fabricate_zero_cost():
    summary = summarize_conversation([{"job_id": "queued", "attempt_id": None}])
    assert summary.turn_count == 1
    assert summary.totals is None
    assert summary.queried_at is None


def test_other_turns_scheduled_refresh_keeps_conversation_updates_visible():
    due = END + timedelta(seconds=300)
    summary = summarize_conversation(
        [
            ledger_row(job_id="job1"),
            ledger_row(job_id="job2", attempt_id=11, next_refresh_at=due),
        ]
    )
    assert summary.refreshing is False
    assert summary.next_refresh_at == due


def test_running_sibling_keeps_conversation_polling_until_it_finishes():
    rows = [
        ledger_row(job_id="old", job_state="succeeded"),
        ledger_row(job_id="new", attempt_id=11, job_state="running"),
    ]
    summary = summarize_conversation(rows)
    assert summary.active is True
    assert summary.refreshing is False
    assert summary.next_refresh_at is None
    rows[1]["job_state"] = "succeeded"
    assert summarize_conversation(rows).active is False
