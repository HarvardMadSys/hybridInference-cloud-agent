"""The two prompts a goal adds: the per-turn preamble, and an automatic round."""

from __future__ import annotations

from cloud_agent.goals.prompts import (
    REPORT_INSTRUCTIONS,
    REPORT_TOOL_INSTRUCTIONS,
    prompt_with_goal,
    render_round_prompt,
    with_mcp_availability,
)

ACTIVE = {
    "objective": "make `pytest` pass",
    "state": "active",
    "rounds_used": 2,
    "max_rounds": 10,
}


def test_no_goal_returns_the_prompt_unchanged():
    assert prompt_with_goal("fix it", None) == "fix it"


def test_a_goal_without_an_objective_is_not_a_goal():
    assert prompt_with_goal("fix it", {"state": "active"}) == "fix it"


def test_a_goal_that_is_not_active_adds_nothing():
    for state in ("paused", "blocked", "complete"):
        assert prompt_with_goal("fix it", {**ACTIVE, "state": state}) == "fix it"


def test_the_preamble_names_the_tool_when_the_session_can_reach_it():
    """A harness the relay serves is told about the tool, and nothing else.

    Both ways of reporting work, and both are accepted at once — but a preamble
    that described both would leave the model choosing, and the text convention
    exists for the case where there is no tool to choose.
    """
    prompt = prompt_with_goal("fix it", with_mcp_availability(ACTIVE, True) or {})
    assert REPORT_TOOL_INSTRUCTIONS in prompt
    assert "GOAL_STATUS" not in prompt


def test_the_preamble_falls_back_to_the_text_convention_without_a_tool():
    prompt = prompt_with_goal("fix it", with_mcp_availability(ACTIVE, False) or {})
    assert REPORT_INSTRUCTIONS in prompt
    assert "report_goal" not in prompt


def test_availability_is_composed_onto_the_goal_not_stored_in_it():
    """The same conversation tells two harnesses different things."""
    goal = dict(ACTIVE)
    assert with_mcp_availability(goal, True)["mcp_available"] is True
    assert "mcp_available" not in goal
    assert with_mcp_availability(None, True) is None


def test_the_preamble_goes_first_and_carries_the_report_convention():
    prompt = prompt_with_goal("fix it", ACTIVE)
    assert prompt.startswith("<conversation_goal>\n")
    assert prompt.endswith("\n\nfix it")
    assert REPORT_INSTRUCTIONS in prompt
    assert '"make `pytest` pass"' in prompt
    assert "Round: 2/10 used" in prompt


def test_an_objective_with_quotes_stays_one_line():
    prompt = prompt_with_goal("x", {**ACTIVE, "objective": 'say "hi"\nthen stop'})
    objective_line = prompt.splitlines()[1]
    assert objective_line.startswith("Objective: ")
    assert "\n" not in objective_line.removeprefix("Objective: ")


def test_a_work_round_names_its_number_and_the_prior_reason():
    prompt = render_round_prompt(
        objective="make `pytest` pass",
        kind="work",
        round_number=3,
        max_rounds=10,
        previous="two tests still fail",
    )
    assert '<goal_round kind="work" round="3" of="10">' in prompt
    assert "The previous turn reported: two tests still fail" in prompt
    assert "as authoritative" in prompt
    assert prompt.endswith("</goal_round>")


def test_a_work_round_after_an_unaccepted_blocked_says_so():
    prompt = render_round_prompt(
        objective="ship it",
        kind="work",
        round_number=2,
        max_rounds=5,
        previous="no network access",
        previous_blocked=True,
    )
    assert "Your previous turn reported blocked: no network access" in prompt
    assert "has not accepted it yet" in prompt


def test_an_unaccepted_blocked_with_no_reason_still_says_so():
    prompt = render_round_prompt(
        objective="ship it",
        kind="work",
        round_number=2,
        max_rounds=5,
        previous=None,
        previous_blocked=True,
    )
    assert "the platform has not accepted it yet" in prompt


def test_a_verify_round_forbids_editing():
    prompt = render_round_prompt(
        objective="make `pytest` pass",
        kind="verify",
        round_number=3,
        max_rounds=10,
    )
    assert '<goal_round kind="verify">' in prompt
    assert "do not modify code" in prompt
    assert "re-run the check the objective names" in prompt
    assert '"make `pytest` pass"' in prompt
