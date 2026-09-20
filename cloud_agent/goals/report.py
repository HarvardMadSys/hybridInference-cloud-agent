"""Reading a turn's self-reported goal outcome out of its final message.

The report is a fenced JSON block at the very end of the turn's final assistant
message. It replaces the two-line text convention: a block has a version, a
closed set of fields and a parser that can *refuse*, which is what makes
``invalid`` — and therefore "ask the model once more, then stop" — expressible
at all. A convention that guesses never fails, and never terminates either.

The parse is deliberately strict. Unknown fields, duplicate keys, a wrong
version, an over-long reason, more than eight evidence items, a second block, a
block that is not last, or any trailing prose are all *invalid*: a model that
produces one of those gets exactly one bounded chance to restate the report,
and then the goal pauses with a reason that names the problem. Nothing is
salvaged by fuzzy matching, and nothing is truncated into acceptance.

``evidence`` is what the model *says* it ran. The platform never executes a
command because it appeared in a report; a report is a declaration, and the
field is recorded and displayed as one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

#: The fence that opens and closes the block. A model that emits one of these
#: has made a report attempt, whether or not the JSON inside is valid — which is
#: exactly the distinction between ``invalid`` (the platform saw an attempt)
#: and ``absent`` (it saw none).
GOAL_REPORT_FENCE = "cloud-agent-goal-report"
_FENCE = f"```{GOAL_REPORT_FENCE}"

#: The only report protocol this server reads. A block carrying another version
#: is invalid rather than "close enough": the fields may mean something else.
REPORT_VERSION = 1
MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 1000
MAX_REPORT_BYTES = 8 * 1024
MAX_REASON_CHARS = 2000

GoalReportOutcome = Literal["complete", "needs_input"]
#: ``absent`` and ``valid`` are the two answers the platform acts on directly.
#: ``invalid`` earns one repair round; ``unavailable`` pauses, because a report
#: that could not be read is not the same fact as a report that was not made.
GoalReportParse = Literal["absent", "valid", "invalid", "unavailable"]


@dataclass(frozen=True)
class GoalReport:
    """What one turn said about the goal, and why."""

    outcome: GoalReportOutcome
    reason: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedGoalReport:
    """The outcome of reading one final message, with a reason when it failed."""

    result: GoalReportParse
    report: GoalReport | None = None
    #: Machine-readable, one of :data:`REPORT_INVALID_REASONS`. Only set when
    #: ``result`` is ``invalid``.
    detail: str | None = None


def source_digest(text: str | None) -> str | None:
    """A short, stable name for the message a report was read out of.

    The report's *provenance*, not its content: the digest is what the audit
    trail points at when it says which final message a completion came from.
    """
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class GoalReportEnvelope:
    """One turn's answer about its goal, as the control plane stores it.

    The same four facts the runtime adapter produces, plus the provenance of the
    message they were read out of. It crosses one boundary — the finish request —
    and everything on the far side reads it as *what the model said*, never as
    something the platform verified. The identity fields (job, attempt, lease,
    goal, revision) are deliberately absent: they are bound by the authenticated
    execution context, and a model's own claim about which goal it was working
    on is not evidence about anything.
    """

    parse_result: GoalReportParse
    outcome: GoalReportOutcome | None = None
    reason: str | None = None
    #: A list, not a tuple: this is the value that goes into a JSONB column, and
    #: the driver will not encode a tuple into one.
    evidence: list[str] | None = None
    detail: str | None = None
    #: Which final message this was read from. The audit trail points at it; the
    #: report's content is not republished here.
    source_digest: str | None = None

    @classmethod
    def from_parsed(
        cls, parsed: ParsedGoalReport, *, source_digest: str | None
    ) -> GoalReportEnvelope:
        """The envelope for one parsed final message."""
        report = parsed.report
        return cls(
            parse_result=parsed.result,
            outcome=report.outcome if report is not None else None,
            reason=report.reason if report is not None else None,
            evidence=list(report.evidence) if report is not None else None,
            detail=parsed.detail,
            source_digest=source_digest,
        )


#: Every way a report can be refused. Machine-readable because the pause detail
#: shown to an owner is built from it, and because a test asserting "this was
#: rejected for the right reason" should not match on prose.
REPORT_INVALID_REASONS = (
    "duplicate_block",
    "unclosed_block",
    "trailing_content",
    "too_large",
    "malformed_json",
    "duplicate_key",
    "unknown_field",
    "missing_field",
    "bad_version",
    "bad_outcome",
    "bad_reason",
    "bad_evidence",
)


def unavailable(detail: str | None = None) -> ParsedGoalReport:
    """A final message that could not be read, so no report can be attributed."""
    return ParsedGoalReport(result="unavailable", detail=detail)


def absent() -> ParsedGoalReport:
    """A complete final message that simply made no report."""
    return ParsedGoalReport(result="absent")


def invalid(detail: str) -> ParsedGoalReport:
    """A report attempt the platform refuses, and will ask once to restate."""
    return ParsedGoalReport(result="invalid", detail=detail)


def parse_goal_report(text: str | None) -> ParsedGoalReport:
    """Read the report block at the end of a turn's final message.

    ``text`` is the *final assistant message* of one turn, and nothing else.
    ``None`` means the turn produced no assistant text at all, which is
    ``absent``: a turn that ran and said nothing made no report. A caller that
    cannot prove it has the complete final message must call
    :func:`unavailable` instead of guessing from a prefix.
    """
    if text is None:
        return absent()
    # The *fence* is what makes a block, not the protocol's name: a message that
    # merely mentions the name in prose has made no attempt at a report, and
    # searching for the name alone found nothing to open and raised.
    start = text.find(_FENCE)
    if start == -1:
        return absent()
    if text.find(_FENCE, start + len(_FENCE)) != -1:
        return invalid("duplicate_block")
    open_end = text.find("\n", start)
    if open_end == -1:
        return invalid("unclosed_block")
    open_line = text[start:open_end].rstrip()
    # A fence carrying an info string is a different fence, and the protocol
    # spells the block as a bare fence: an HTML-style ``json`` suffix is not
    # accepted rather than guessed at, so a model that invents one is told.
    if open_line != _FENCE:
        return invalid("unclosed_block")
    # The closing fence has to be a fence, not the first three backticks
    # anywhere: the body is JSON, and a run of three inside a string would end
    # the block inside the value that contains it.
    close = _closing_fence(text, open_end + 1)
    if close == -1:
        return invalid("unclosed_block")
    if text[close + 3 :].strip():
        # Something follows the block. The protocol puts the report last so
        # that a reader never has to decide which of two statements was final.
        return invalid("trailing_content")
    payload = text[open_end + 1 : close]
    if len(payload.encode("utf-8")) > MAX_REPORT_BYTES:
        return invalid("too_large")
    return _decode(payload)


def _closing_fence(text: str, search_from: int) -> int:
    """Where the block's closing fence starts, or ``-1`` when it never does."""
    index = text.find("```", search_from)
    while index != -1:
        rest = text[index + 3 :]
        line_end = rest.find("\n")
        line = rest if line_end == -1 else rest[:line_end]
        if not line.strip():
            return index
        index = text.find("```", index + 3)
    return -1


def _decode(payload: str) -> ParsedGoalReport:
    """Turn the block's JSON into a report, or name exactly why it is not one."""
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey as exc:
        return invalid(f"duplicate_key:{exc.key}")
    except (ValueError, TypeError, json.JSONDecodeError):
        return invalid("malformed_json")
    if not isinstance(document, dict):
        return invalid("malformed_json")
    unknown = sorted(set(document) - {"version", "outcome", "reason", "evidence"})
    if unknown:
        return invalid(f"unknown_field:{unknown[0]}")
    missing = sorted({"version", "outcome", "reason"} - set(document))
    if missing:
        return invalid(f"missing_field:{missing[0]}")
    version = document["version"]
    if isinstance(version, bool) or version != REPORT_VERSION:
        return invalid("bad_version")
    outcome = document["outcome"]
    if outcome not in ("complete", "needs_input"):
        return invalid("bad_outcome")
    reason = document["reason"]
    if not isinstance(reason, str) or not reason.strip():
        return invalid("bad_reason")
    if len(reason) > MAX_REASON_CHARS:
        return invalid("bad_reason")
    evidence = _evidence(document.get("evidence"))
    if evidence is None:
        return invalid("bad_evidence")
    return ParsedGoalReport(
        result="valid",
        report=GoalReport(
            outcome=outcome,
            reason=reason.strip(),
            evidence=tuple(evidence),
        ),
    )


def _evidence(value: Any) -> list[str] | None:
    """The evidence list, or ``None`` when it is not one.

    Absent is fine — evidence is optional, and a model with nothing to cite is
    more honest for leaving it out than for inventing a command. Present but
    wrong is not: an over-long or non-text item is the same class of protocol
    error as a missing field, and is answered the same way.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_ITEMS:
        return None
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        if len(item) > MAX_EVIDENCE_CHARS:
            return None
        items.append(item.strip())
    return items


class _DuplicateKey(ValueError):
    """Raised by the JSON hook so the duplicate's name reaches the detail."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def _reject_constant(name: str) -> Any:
    """``NaN``/``Infinity`` are not JSON; Python's decoder accepts them anyway."""
    raise ValueError(name)


__all__ = [
    "GOAL_REPORT_FENCE",
    "MAX_EVIDENCE_CHARS",
    "MAX_EVIDENCE_ITEMS",
    "MAX_REPORT_BYTES",
    "REPORT_INVALID_REASONS",
    "REPORT_VERSION",
    "GoalReport",
    "GoalReportEnvelope",
    "GoalReportOutcome",
    "GoalReportParse",
    "ParsedGoalReport",
    "absent",
    "invalid",
    "parse_goal_report",
    "unavailable",
]
