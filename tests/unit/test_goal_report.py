"""Reading the goal report out of a turn's final message.

The parser is the whole reason the protocol can terminate: it either reads a
report the platform acts on, or it names exactly why it will not. These cases
pin both halves — what counts as a report, and which refusal a malformed one
earns — because "invalid" is what buys the model its one bounded repair, and
"absent" is what continues the goal.
"""

from __future__ import annotations

import json

from cloud_agent.goals.report import (
    GOAL_REPORT_FENCE,
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_ITEMS,
    MAX_REASON_CHARS,
    MAX_REPORT_BYTES,
    REPORT_INVALID_REASONS,
    REPORT_VERSION,
    GoalReport,
    GoalReportEnvelope,
    invalid,
    parse_goal_report,
)

_FENCE = f"```{GOAL_REPORT_FENCE}"


def block(document: str, *, tail: str = "") -> str:
    """A final message whose last content is one fenced report block."""
    return f"Worked on it.\n\n{_FENCE}\n{document}\n```{tail}"


def document(**fields: object) -> str:
    """A report document, with the caller's fields overlaid."""
    return json.dumps(
        {
            "version": REPORT_VERSION,
            "outcome": "complete",
            "reason": "pytest: 42 passed",
            **fields,
        }
    )


def report(**fields: object) -> str:
    """A block holding a report document."""
    return block(document(**fields))


def refused(text: str, code: str) -> None:
    """Assert a message is refused for exactly one machine-readable reason."""
    parsed = parse_goal_report(text)
    assert parsed.result == "invalid", parsed
    assert parsed.detail is not None
    assert parsed.detail.split(":", 1)[0] == code, parsed.detail
    assert code in REPORT_INVALID_REASONS


def test_a_message_with_no_block_makes_no_report():
    parsed = parse_goal_report("I am still working on it. Two tests fail.")
    assert parsed.result == "absent"
    assert parsed.report is None
    assert parsed.detail is None


def test_missing_and_empty_text_are_both_absent():
    """``None`` means the turn said nothing, which is not a report attempt."""
    for text in (None, "", "   \n"):
        assert parse_goal_report(text).result == "absent"


def test_a_valid_complete_report_is_read_with_its_evidence():
    parsed = parse_goal_report(
        report(reason="pytest: 42 passed", evidence=["pytest -q, exit 0", "git status clean"])
    )
    assert parsed.result == "valid"
    assert parsed.report == GoalReport(
        outcome="complete",
        reason="pytest: 42 passed",
        evidence=("pytest -q, exit 0", "git status clean"),
    )


def test_a_valid_needs_input_report_is_read():
    parsed = parse_goal_report(
        report(outcome="needs_input", reason="which region should I deploy to?")
    )
    assert parsed.result == "valid"
    assert parsed.report == GoalReport(
        outcome="needs_input", reason="which region should I deploy to?"
    )


def test_evidence_is_optional_and_absent_evidence_is_empty():
    assert parse_goal_report(report()).report == GoalReport(
        outcome="complete", reason="pytest: 42 passed"
    )


def test_the_reason_and_each_evidence_item_are_trimmed():
    parsed = parse_goal_report(
        block(
            json.dumps(
                {
                    "version": 1,
                    "outcome": "complete",
                    "reason": "  green now  ",
                    "evidence": ["  pytest -q  "],
                }
            )
        )
    )
    assert parsed.report == GoalReport(
        outcome="complete", reason="green now", evidence=("pytest -q",)
    )


def test_whitespace_after_the_block_is_not_trailing_content():
    parsed = parse_goal_report(block(document(), tail="\n   \n"))
    assert parsed.result == "valid"


def test_whitespace_after_the_opening_fence_is_tolerated():
    parsed = parse_goal_report(f"Done.\n\n{_FENCE}   \n{document()}\n```\n")
    assert parsed.result == "valid"


def test_a_run_of_backticks_inside_the_payload_does_not_close_the_block():
    """The body is JSON; a fence inside a string is part of the value it sits in."""
    evidence = "grep -n '```' notes.md"
    parsed = parse_goal_report(report(evidence=[evidence]))

    assert parsed.result == "valid"
    assert parsed.report is not None
    assert parsed.report.evidence == (evidence,)


def test_two_blocks_are_refused():
    """The protocol allows exactly one statement of the outcome."""
    refused(f"{report()}\n\n{report()}", "duplicate_block")


def test_a_block_that_never_closes_is_refused():
    refused(f"Done.\n\n{_FENCE}\n{json.dumps({'version': 1})}\n", "unclosed_block")


def test_a_fence_with_no_newline_after_it_is_refused():
    refused(f"Done. {_FENCE}", "unclosed_block")


def test_a_fence_with_an_info_string_is_refused():
    """Only the bare fence opens a block; an invented suffix means guessing."""
    for suffix in ("json", " python", "jsonc"):
        refused(f"Done.\n\n{_FENCE}{suffix}\n{document()}\n```", "unclosed_block")


def test_the_fence_quoted_in_prose_is_not_a_report():
    """Echoing the instruction is not making a report, and it still has no block."""
    refused(f"Use the {_FENCE} fence when you finish.", "unclosed_block")


def test_content_after_the_block_is_refused():
    """The block has to be last, or a reader cannot tell which statement was final."""
    refused(block(document(), tail="\nThat is all."), "trailing_content")


def test_a_second_paragraph_after_the_block_is_refused():
    refused(f"{report()}\n\nThanks for your help!", "trailing_content")


def test_an_oversized_payload_is_refused():
    refused(block("x" * (MAX_REPORT_BYTES + 1)), "too_large")


def test_a_payload_that_is_not_json_is_refused():
    refused(block("reason: all done"), "malformed_json")


def test_a_payload_that_is_not_an_object_is_refused():
    for payload in ("[]", '"complete"', "null", "42"):
        refused(block(payload), "malformed_json")


def test_a_duplicate_key_is_refused_and_names_the_key():
    refused(
        block('{"version":1,"outcome":"complete","reason":"done","reason":"done again"}'),
        "duplicate_key",
    )


def test_an_unknown_field_is_refused_and_names_the_field():
    refused(report(blocked=True), "unknown_field")


def test_a_missing_required_field_is_refused_and_names_the_field():
    refused(block(json.dumps({"version": 1, "outcome": "complete"})), "missing_field")


def test_a_wrong_version_is_refused():
    for version in (2, 0, "1", None, True):
        refused(report(version=version), "bad_version")


def test_an_unknown_outcome_is_refused():
    for outcome in ("blocked", "CONTINUE", "done", None, 1):
        refused(report(outcome=outcome), "bad_outcome")


def test_a_reason_that_is_empty_or_not_text_is_refused():
    for reason in ("", "   ", None, 7, ["done"]):
        refused(report(reason=reason), "bad_reason")


def test_a_reason_longer_than_the_bound_is_refused_not_truncated():
    refused(report(reason="x" * (MAX_REASON_CHARS + 1)), "bad_reason")


def test_more_evidence_items_than_the_bound_is_refused():
    refused(report(evidence=["cmd"] * (MAX_EVIDENCE_ITEMS + 1)), "bad_evidence")


def test_an_over_long_evidence_item_is_refused():
    refused(report(evidence=["x" * (MAX_EVIDENCE_CHARS + 1)]), "bad_evidence")


def test_a_non_text_evidence_item_is_refused():
    for item in (7, None, {"cmd": "pytest"}, ["pytest"]):
        refused(report(evidence=[item]), "bad_evidence")


def test_evidence_that_is_not_a_list_is_refused():
    refused(report(evidence="pytest -q"), "bad_evidence")


def test_the_envelope_carries_what_was_read_and_nothing_on_a_refusal():
    """The finish request's shape: the report, or the reason there is none."""
    source = report(reason="pytest: 42 passed", evidence=["pytest -q"])
    envelope = GoalReportEnvelope.from_parsed(parse_goal_report(source), source_digest="abc123")
    assert (envelope.parse_result, envelope.outcome, envelope.reason) == (
        "valid",
        "complete",
        "pytest: 42 passed",
    )
    assert envelope.evidence == ["pytest -q"]
    assert envelope.source_digest == "abc123"

    refused_envelope = GoalReportEnvelope.from_parsed(invalid("malformed_json"), source_digest=None)
    assert (refused_envelope.parse_result, refused_envelope.detail) == (
        "invalid",
        "malformed_json",
    )
    assert refused_envelope.outcome is None and refused_envelope.reason is None


def test_a_bare_fence_in_a_configuration_snippet_is_a_refused_attempt():
    """The fence is distinctive enough that seeing one means the model tried."""
    refused("Here is the block I would emit:\n\n```cloud-agent-goal-report\n", "unclosed_block")
