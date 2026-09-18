"""The continuation rules, one case at a time.

These are the feature's safety story written as assertions: what continues,
what stops, and what a completion claim is worth.
"""

from __future__ import annotations

from dataclasses import replace

from cloud_agent.goals.decision import GoalContext, decide


def context(**overrides) -> GoalContext:
    """An active goal, mid-flight, whose last turn reported ``continue``."""
    base = GoalContext(
        state="active",
        objective="make `pytest` pass",
        revision=1,
        max_rounds=10,
        rounds_used=1,
        blocked_streak=0,
        turn_state="succeeded",
        turn_source="goal",
        turn_round_kind="work",
        report="continue",
        report_reason="two tests still fail",
        thread_has_queued_turn=False,
    )
    return replace(base, **overrides)


def test_continue_queues_the_next_work_round():
    action = decide(context())
    assert action.kind == "queue_work_round"
    assert action.blocked_streak == 0


def test_missing_report_is_treated_as_continue():
    action = decide(context(report=None, report_reason=None))
    assert action.kind == "queue_work_round"


def test_every_non_active_state_stops():
    for state in ("paused", "blocked", "complete"):
        assert decide(context(state=state)).kind == "none"


def test_failed_turn_pauses_with_its_detail():
    action = decide(context(turn_state="failed", report_reason="exit code 124 after 1h"))
    assert action.kind == "pause"
    assert action.reason_code == "turn-failed"
    assert action.message == "exit code 124 after 1h"


def test_cancelled_turn_pauses():
    action = decide(context(turn_state="cancelled"))
    assert (action.kind, action.reason_code) == ("pause", "cancelled")


def test_blocked_is_not_accepted_until_three_consecutive_turns():
    first = decide(context(report="blocked", report_reason="no network"))
    assert first.kind == "queue_work_round"
    assert first.blocked_streak == 1

    second = decide(
        context(report="blocked", report_reason="no network", blocked_streak=first.blocked_streak)
    )
    assert second.kind == "queue_work_round"
    assert second.blocked_streak == 2

    third = decide(
        context(report="blocked", report_reason="no network", blocked_streak=second.blocked_streak)
    )
    assert third.kind == "block"
    assert third.reason_code == "model-reported"
    assert third.message == "no network"
    assert third.blocked_streak == 3


def test_a_continue_between_blocked_reports_resets_the_streak():
    streak = decide(context(report="blocked")).blocked_streak
    assert streak == 1
    assert decide(context(report="continue", blocked_streak=streak)).blocked_streak == 0
    assert decide(context(report=None, blocked_streak=streak)).blocked_streak == 0


def test_complete_claim_buys_a_verification_round():
    action = decide(context(report="complete", report_reason="pytest: 42 passed"))
    assert action.kind == "queue_verify_round"


def test_only_a_verification_round_can_complete_the_goal():
    action = decide(
        context(report="complete", turn_round_kind="verify", report_reason="pytest: 42 passed")
    )
    assert action.kind == "complete"


def test_verification_round_reporting_anything_else_continues():
    for report in ("continue", "blocked", None):
        action = decide(context(report=report, turn_round_kind="verify", report_reason="still red"))
        assert action.kind == "queue_work_round"


def test_verification_round_reporting_blocked_still_builds_a_streak():
    action = decide(context(report="blocked", turn_round_kind="verify", blocked_streak=2))
    assert action.kind == "block"
    assert action.reason_code == "model-reported"


def test_a_queued_turn_makes_the_goal_yield():
    assert decide(context(thread_has_queued_turn=True)).kind == "none"


def test_yielding_still_records_a_blocked_streak():
    """The person's turn ends like any other; the streak must not restart."""
    action = decide(context(report="blocked", thread_has_queued_turn=True))
    assert action.kind == "none"
    assert action.blocked_streak == 1


def test_a_queued_turn_defers_a_completion_claim_too():
    """The claim is not lost by deferring — it is carried to the next turn.

    ``continuation._report_of`` re-reads an unclaimed ``complete`` from the
    preceding turn, so what the decision does here is decide *this* turn:
    nothing, because a person's message is already waiting.
    """
    action = decide(context(report="complete", thread_has_queued_turn=True))
    assert action.kind == "none"


def test_round_limit_blocks_when_the_budget_is_spent():
    action = decide(context(rounds_used=10, max_rounds=10))
    assert action.kind == "block"
    assert action.reason_code == "round-limit"
    assert "10" in (action.message or "")


def test_completion_claim_on_the_last_round_is_still_verified():
    """Verification is not charged to the budget, so step 6 precedes step 7."""
    action = decide(context(rounds_used=10, max_rounds=10, report="complete"))
    assert action.kind == "queue_verify_round"


def test_a_failed_turn_wins_over_a_completion_claim():
    action = decide(context(turn_state="failed", report="complete"))
    assert action.kind == "pause"


def test_blocked_acceptance_wins_over_the_round_limit():
    action = decide(context(rounds_used=10, max_rounds=10, report="blocked", blocked_streak=2))
    assert action.kind == "block"
    assert action.reason_code == "model-reported"
