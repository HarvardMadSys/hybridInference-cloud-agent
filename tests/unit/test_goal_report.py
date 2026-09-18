"""Reading the two-line status convention out of a turn's final message."""

from __future__ import annotations

from cloud_agent.goals.models import MAX_REASON_CHARS
from cloud_agent.goals.report import GoalReport, parse_goal_report


def test_reads_status_and_reason():
    report = parse_goal_report("Done.\n\nGOAL_STATUS: complete\nGOAL_REASON: pytest: 42 passed")
    assert report == GoalReport(status="complete", reason="pytest: 42 passed")


def test_case_trailing_space_and_a_blank_line_are_tolerated():
    report = parse_goal_report("GOAL_status:   COMPLETE  \n\n")
    assert report == GoalReport(status="complete", reason=None)


def test_the_last_report_wins():
    text = (
        "GOAL_STATUS: continue\nGOAL_REASON: still red\n"
        "...thinking...\n"
        "GOAL_STATUS: complete\nGOAL_REASON: green now"
    )
    assert parse_goal_report(text) == GoalReport(status="complete", reason="green now")


def test_a_status_without_a_reason_is_a_report():
    assert parse_goal_report("GOAL_STATUS: blocked") == GoalReport(status="blocked", reason=None)


def test_prose_between_the_two_lines_breaks_the_pairing():
    """A reason is the next line or it is nothing; it is evidence, not a guess."""
    report = parse_goal_report("GOAL_STATUS: complete\nI ran the tests.\nGOAL_REASON: passed")
    assert report == GoalReport(status="complete", reason=None)


def test_a_reason_label_alone_is_not_a_report():
    assert parse_goal_report("GOAL_REASON: because I said so") is None


def test_an_unknown_status_is_no_report():
    for text in (
        "GOAL_STATUS: finished",
        "GOAL_STATUS: complete-ish",
        # The preamble itself names all three words on one line. Echoing the
        # instruction back is not a report, and the alternation refuses it
        # because no single word is what the line holds.
        "The prompt said GOAL_STATUS: continue | complete | blocked",
    ):
        assert parse_goal_report(text) is None


def test_empty_and_missing_text_are_no_report():
    assert parse_goal_report(None) is None
    assert parse_goal_report("") is None
    assert parse_goal_report("No status here at all.") is None


def test_an_overlong_reason_is_truncated_not_refused():
    report = parse_goal_report(f"GOAL_STATUS: continue\nGOAL_REASON: {'x' * 5000}")
    assert report is not None
    assert len(report.reason or "") == MAX_REASON_CHARS
