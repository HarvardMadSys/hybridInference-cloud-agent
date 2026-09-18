"""Reading a turn's self-reported goal outcome out of its last message.

The report is a two-line text convention at the end of the turn's final
assistant message. The same two values are what the MCP tool writes, and
everything downstream — the decision function, the storage columns, the UI —
is identical either way.

The convention is deliberately forgiving about the things a model gets wrong
for no reason (case, trailing spaces, a stray blank line) and strict about the
one thing that matters: ``continue``, ``complete`` and ``blocked`` are the only
accepted words, and an unrecognised status is *no report* rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from cloud_agent.goals.models import MAX_REASON_CHARS, GoalReportStatus

GOAL_STATUS_LABEL = "GOAL_STATUS"
GOAL_REASON_LABEL = "GOAL_REASON"

_STATUS = re.compile(
    rf"^\s*{GOAL_STATUS_LABEL}:\s*(continue|complete|blocked)\s*$",
    re.IGNORECASE,
)
_REASON = re.compile(rf"^\s*{GOAL_REASON_LABEL}:\s*(.*?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class GoalReport:
    """What one turn said about the goal, and why."""

    status: GoalReportStatus
    reason: str | None = None


def parse_goal_report(text: str | None) -> GoalReport | None:
    """The last report in a turn's final message, or ``None`` if it made none.

    The *last* one wins: a turn that restated its status while thinking out
    loud ends with what it concluded, and a heading a model quoted from the
    prompt it was given must not be able to masquerade as its answer.
    """
    if not text:
        return None
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        match = _STATUS.match(lines[index])
        if match is None:
            continue
        return GoalReport(
            status=cast("GoalReportStatus", match.group(1).lower()),
            reason=_reason_after(lines, index),
        )
    return None


def _reason_after(lines: list[str], status_index: int) -> str | None:
    """The reason line, which is the very next line — not a later one.

    A model that wrote prose between the two lines did not follow the
    convention, and pairing them anyway would attach the wrong sentence to the
    status as evidence. The reason is optional; an absent one is honest.
    """
    if status_index + 1 >= len(lines):
        return None
    match = _REASON.match(lines[status_index + 1])
    if match is None:
        return None
    reason = match.group(1).strip()
    if not reason:
        return None
    return reason[:MAX_REASON_CHARS]
