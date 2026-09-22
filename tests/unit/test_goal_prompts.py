"""The two prompts a goal adds: the per-turn preamble, and an automatic round.

Both reach the model through the ordinary task prompt, so what they say is the
entire contract the model has to work from. The report block is the part that
matters: it is named in every one of them, it is the same for a person's turn
and an automatic one, and it never points at a tool the sandbox may not have.
"""

from __future__ import annotations

from cloud_agent.goals.models import MAX_REPORT_REPAIRS
from cloud_agent.goals.prompts import (
    REPORT_INSTRUCTIONS,
    prompt_with_goal,
    render_repair_prompt,
    render_round_prompt,
)

#: A goal the owner never bounded, which is what the product creates.
ACTIVE = {
    "objective": "make `pytest` pass",
    "state": "active",
    "rounds_used": 2,
    "max_rounds": None,
}
#: The same goal, for the owner who stated a number themselves.
BOUNDED = {**ACTIVE, "max_rounds": 10}


def test_no_goal_returns_the_prompt_unchanged():
    assert prompt_with_goal("fix it", None) == "fix it"


def test_a_goal_without_an_objective_is_not_a_goal():
    assert prompt_with_goal("fix it", {"state": "active"}) == "fix it"


def test_a_goal_that_is_not_active_adds_nothing():
    """A preamble promising a judgement the platform will not make is worse than none."""
    for state in ("paused", "complete"):
        assert prompt_with_goal("fix it", {**ACTIVE, "state": state}) == "fix it"


def test_the_preamble_goes_first_and_carries_the_report_block():
    prompt = prompt_with_goal("fix it", ACTIVE)
    assert prompt.startswith("<conversation_goal>\n")
    assert prompt.endswith("\n\nfix it")
    assert REPORT_INSTRUCTIONS in prompt
    assert '"make `pytest` pass"' in prompt
    # Rounds run is a fact; a total is not, because there is not one.
    assert "Automatic rounds run so far: 2" in prompt
    assert "2/10" not in prompt
    assert "the user allows" not in prompt


def test_the_report_instructions_name_the_fence_and_its_schema():
    assert "```cloud-agent-goal-report" in REPORT_INSTRUCTIONS
    assert '"outcome":"complete"' in REPORT_INSTRUCTIONS
    assert '"version":1' in REPORT_INSTRUCTIONS
    assert "needs_input" in REPORT_INSTRUCTIONS


def test_the_report_instructions_never_name_a_tool_or_the_old_convention():
    """The report is the model's own final message; no runtime needs a tool for it."""
    for forbidden in ("GOAL_STATUS", "report_goal", "get_goal", "tool call"):
        assert forbidden not in REPORT_INSTRUCTIONS


def test_an_objective_with_quotes_stays_one_line():
    prompt = prompt_with_goal("x", {**ACTIVE, "objective": 'say "hi"\nthen stop'})
    objective_line = prompt.splitlines()[1]
    assert objective_line.startswith("Objective: ")
    assert "\n" not in objective_line.removeprefix("Objective: ")


def test_a_counter_that_is_missing_reads_as_zero_rather_than_raising():
    """The goal arrives as the store's dict; a prompt that cannot be built is a turn that cannot run."""
    prompt = prompt_with_goal("x", {"objective": "ship it", "state": "active"})
    assert "Automatic rounds run so far: 0" in prompt


def test_a_stated_bound_is_named_and_an_absent_one_is_not():
    """The model is told a limit exactly when the owner set one.

    "of 999999" would be a limit nobody chose, and a model that believes it is
    on a budget plans differently from one that was told to finish the job.
    """
    assert "of at most 10 the user allows" in prompt_with_goal("x", BOUNDED)
    assert "the user allows" not in prompt_with_goal("x", ACTIVE)


def test_a_nonsense_bound_is_treated_as_no_bound():
    """A prompt that cannot be built is a turn that cannot run; a guess is worse."""
    for bad in (0, -1, "ten", True):
        prompt = prompt_with_goal("x", {**ACTIVE, "max_rounds": bad})
        assert "the user allows" not in prompt


def test_a_work_round_names_its_number_and_what_the_last_turn_reported():
    prompt = render_round_prompt(
        objective="make `pytest` pass",
        round_number=3,
        max_rounds=10,
        previous="two tests still fail",
    )
    assert '<goal_round kind="work" round="3" of="10">' in prompt
    assert "What the previous turn reported: two tests still fail" in prompt
    assert "as authoritative" in prompt
    assert REPORT_INSTRUCTIONS in prompt
    assert prompt.endswith("</goal_round>")


def test_a_round_of_an_unbounded_goal_names_no_total():
    """Round 90 of nothing is still a round: no `of=` attribute at all."""
    prompt = render_round_prompt(objective="ship it", round_number=90, max_rounds=None)
    assert '<goal_round kind="work" round="90">' in prompt
    assert 'of="' not in prompt


def test_a_work_round_with_nothing_reported_says_nothing_about_it():
    prompt = render_round_prompt(objective="ship it", round_number=2, max_rounds=None)
    assert "What the previous turn reported" not in prompt
    assert "was paused since the last turn" not in prompt


def test_a_work_round_after_a_pause_reminds_the_model_it_was_stopped():
    prompt = render_round_prompt(
        objective="ship it",
        round_number=2,
        max_rounds=5,
        previous="no network access",
        previous_paused=True,
    )
    assert (
        "The goal was paused since the last turn and the user asked for it to continue. "
        "What was recorded then: no network access" in prompt
    )


def test_a_repair_round_forbids_more_work():
    prompt = render_repair_prompt(objective="make `pytest` pass", detail="malformed_json")
    assert '<goal_round kind="repair">' in prompt
    assert "Do not run more tools and do not do more work." in prompt
    assert "What was wrong with it: malformed_json" in prompt
    assert '"make `pytest` pass"' in prompt
    assert REPORT_INSTRUCTIONS in prompt
    assert f"You get at most {MAX_REPORT_REPAIRS} attempt at this." in prompt
    assert prompt.endswith("</goal_round>")


def test_a_repair_round_without_a_detail_still_asks_for_the_block():
    prompt = render_repair_prompt(objective="ship it")
    assert "What was wrong with it:" not in prompt
    assert "end this message with the block again" in prompt
