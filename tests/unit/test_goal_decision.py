"""The continuation rules, one case at a time, in the order they are read.

The order *is* the specification: a person's message beats a completion claim,
a readable completion beats the owner's own round bound, and a runtime that
cannot deliver a report stops the goal instead of looping. These cases pin each
rule and the places where one rule deliberately outranks another.

The default context carries **no round bound at all**, because that is what the
product creates: a goal keeps going until something real stops it. The bound
cases exist for the owner who stated a number, and are deliberately written as
the exception rather than the rule.
"""

from __future__ import annotations

from dataclasses import replace

from cloud_agent.goals.decision import GoalContext, decide
from cloud_agent.goals.models import MAX_REASON_CHARS, MAX_REPORT_REPAIRS
from cloud_agent.goals.report import GoalReport


def context(**overrides) -> GoalContext:
    """An active goal mid-flight whose last automatic round reported nothing."""
    base = GoalContext(
        state="active",
        goal_id="goal-1",
        revision=3,
        # No bound: the ordinary case. A test that needs one states it.
        max_rounds=None,
        rounds_used=1,
        turn_job_id="job-1",
        turn_goal_id="goal-1",
        turn_revision=3,
        turn_state="succeeded",
        turn_round_kind="work",
    )
    return replace(base, **overrides)


def complete_report(reason: str = "pytest: 42 passed") -> GoalContext:
    """The same goal, with a valid completion reported by the last turn."""
    return context(report_parse="valid", report=GoalReport(outcome="complete", reason=reason))


def test_only_an_active_goal_continues_itself():
    """Paused and complete both mean the platform stops, whatever the turn said."""
    for state in ("paused", "complete"):
        assert decide(context(state=state)).kind == "none"
    assert decide(context(state="paused", turn_state="failed")).kind == "none"


def test_a_failed_turn_pauses_the_goal_with_its_detail():
    action = decide(context(turn_state="failed", turn_detail="exit code 124 after 1h"))
    assert (action.kind, action.reason) == ("pause", "execution_failed")
    assert action.message == "exit code 124 after 1h"


def test_a_cancelled_turn_pauses_the_goal():
    action = decide(context(turn_state="cancelled"))
    assert (action.kind, action.reason) == ("pause", "cancelled")


def test_a_failed_turn_outranks_a_completion_claim():
    """A turn that did not end workably never contributes a completion."""
    action = decide(replace(complete_report(), turn_state="failed"))
    assert (action.kind, action.reason) == ("pause", "execution_failed")


def test_a_round_of_a_replaced_goal_decides_nothing():
    """Its report was about the objective the owner has since replaced."""
    assert decide(context(turn_goal_id="goal-2")).kind == "none"


def test_a_runtime_that_cannot_report_stops_the_goal():
    action = decide(context(runtime_supported=False))
    assert (action.kind, action.reason) == ("pause", "runtime_unsupported")


def test_an_unreadable_final_message_pauses_rather_than_continuing():
    action = decide(context(report_parse="unavailable", report_detail="truncated at 64 KiB"))
    assert (action.kind, action.reason) == ("pause", "report_unavailable")
    assert action.message == "truncated at 64 KiB"


def test_unavailable_is_never_treated_as_an_absent_report():
    """No report is a turn that said nothing; unreadable is a turn nobody can read."""
    assert decide(context(report_parse="absent")).kind == "queue_work_round"
    assert decide(context(report_parse="unavailable")).reason == "report_unavailable"


def test_a_malformed_report_earns_one_repair_round_on_a_verified_runtime():
    action = decide(
        context(report_parse="invalid", report_detail="malformed_json", repair_supported=True)
    )
    assert action.kind == "queue_repair_round"


def test_the_repair_is_spent_after_one_round():
    action = decide(
        context(
            report_parse="invalid",
            report_detail="malformed_json",
            repair_supported=True,
            repairs_used=MAX_REPORT_REPAIRS,
        )
    )
    assert (action.kind, action.reason) == ("pause", "report_invalid")
    assert "malformed_json" in (action.message or "")


def test_an_unverified_runtime_gets_no_repair_round():
    """A repair that can also edit the repository is another work round."""
    action = decide(context(report_parse="invalid", report_detail="duplicate_block"))
    assert (action.kind, action.reason) == ("pause", "report_invalid")


def test_a_valid_completion_completes_on_the_models_own_report():
    action = decide(complete_report())
    assert action.kind == "complete"
    assert action.completion_basis == "model_reported"


def test_a_valid_needs_input_report_pauses_with_what_is_missing():
    action = decide(
        context(
            report_parse="valid",
            report=GoalReport(outcome="needs_input", reason="which region should I deploy to?"),
        )
    )
    assert (action.kind, action.reason) == ("pause", "needs_input")
    assert action.message == "which region should I deploy to?"


def test_a_person_who_is_already_waiting_beats_a_completion_claim():
    action = decide(replace(complete_report(), thread_has_queued_turn=True))
    assert action.kind == "none"


def test_input_accepted_since_the_turn_was_created_beats_a_completion_claim():
    """A claim decided before the owner's message cannot finish the goal after it."""
    action = decide(replace(complete_report(), thread_has_newer_input=True))
    assert action.kind == "none"


def test_a_goal_with_no_bound_keeps_going_past_the_old_defaults():
    """The regression this whole change exists for.

    Ten was the product's default and fifty its ceiling. Neither is a reason to
    stop a goal any more: a goal nobody bounded is bounded by nothing, and the
    rounds it has run are a statistic rather than a stop condition.
    """
    for rounds in (9, 10, 11, 49, 50, 51, 5_000):
        assert decide(context(rounds_used=rounds)).kind == "queue_work_round", rounds
        assert decide(context(rounds_used=rounds, report_parse="absent")).kind == (
            "queue_work_round"
        ), rounds


def test_no_bound_is_the_absence_of_a_value_not_a_large_one():
    """``None`` and a huge integer must not be the same decision.

    A sentinel would make "raise the cap" and "remove the cap" indistinguishable
    in the column, in the API and in a bug report.
    """
    assert context(max_rounds=None, rounds_used=10**9).max_rounds is None
    assert decide(context(max_rounds=None, rounds_used=10**9)).kind == "queue_work_round"


def test_a_bound_the_owner_stated_is_honored_exactly():
    action = decide(context(rounds_used=10, max_rounds=10, report_parse="absent"))
    assert (action.kind, action.reason) == ("pause", "round_limit")
    assert "10" in (action.message or "")
    # One round short of it is still work, not a stop.
    assert decide(context(rounds_used=9, max_rounds=10)).kind == "queue_work_round"


def test_a_completion_on_the_last_permitted_round_still_completes():
    """The bound is checked after the report, so the last round is not wasted."""
    action = decide(replace(complete_report(), rounds_used=10, max_rounds=10))
    assert action.kind == "complete"


def test_an_absent_report_queues_the_next_work_round():
    assert decide(context(report_parse="absent")).kind == "queue_work_round"


def test_a_turn_with_no_report_read_at_all_queues_the_next_work_round():
    """An owner-triggered resume has no turn to read; it is an authorization, not a report."""
    assert decide(context(report_parse=None)).kind == "queue_work_round"


def test_a_pause_message_is_clipped_to_what_the_column_accepts():
    action = decide(context(turn_state="failed", turn_detail="x" * (MAX_REASON_CHARS + 500)))
    assert len(action.message or "") == MAX_REASON_CHARS
