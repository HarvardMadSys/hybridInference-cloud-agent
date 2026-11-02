"""Message parts of the OpenCode/Kilo HTTP bridge, turned into turn events.

The part-level pieces of :class:`cloud_agent_host.drivers.HttpAgentLiveSession`:
a tool part's call, an answer's preview, and the Context events the pinned
servers write on their stream anyway (context design record, section 7.2):

- a text part of a *user* message the server marked ``synthetic`` is text it
  injected itself (a summary request, a tool the user ran); reported once;
- a completed tool part re-sent with ``state.time.compacted`` is the server
  pruning that output from context (Kilo 7.4.17 does so before an oversized
  request); reported once, and never mistaken for a new result.

Nothing here asks the server anything.
"""

from __future__ import annotations

from typing import Any, Protocol

from cloud_agent_host.harness_event import LINE, HarnessEvent
from cloud_agent_host.runtimes import LIFECYCLE, RAW, TOOL_USE, ReplyPreview, merged_mcp_server

#: Characters of an injected text an event keeps.
INJECTED_TEXT_CHARS = 4000


class BridgeTurn(Protocol):
    """What these helpers read of a bridge turn."""

    text_parts: dict[str, tuple[str, str]]
    message_roles: dict[str, str]
    preview: ReplyPreview


def tool_call_event(
    part: dict[str, Any], state: dict[str, Any], call_id: str, line: str
) -> HarnessEvent:
    """The call a tool part stands for, with the input its state carries."""
    return HarnessEvent(
        TOOL_USE,
        {
            "name": part.get("tool"),
            "id": call_id,
            "input": state.get("input"),
            # Both servers name a relay tool `session_<alias>__<tool>`.
            **merged_mcp_server(part.get("tool"), separator="_"),
        },
        raw_line=line,
    )


def answer_preview(turn: BridgeTurn, part_id: str, *, ended: bool, line: str) -> HarnessEvent:
    """Preview a text part, if it is already known to be the assistant's.

    A part whose message metadata arrives later is previewed from its next
    update on, with all the text it has by then.
    """
    message_id, text = turn.text_parts[part_id]
    if turn.message_roles.get(message_id) != "assistant":
        return HarnessEvent(LINE, raw_line=line)
    piece = turn.preview.grow_to(part_id, text)
    if piece is None and ended:
        # A finished part may sit before a long tool call; show all of it.
        piece = turn.preview.flush(part_id)
    if piece is None:
        return HarnessEvent(LINE, raw_line=line)
    return HarnessEvent(RAW, piece, raw_line=line)


class ContextRecords:
    """The synthetic text parts and pruned tool outputs one bridge turn saw."""

    def __init__(self) -> None:
        #: Synthetic text parts, and whether each was already reported.
        self._synthetic: dict[str, bool] = {}
        #: Tool calls whose output the server pruned this turn.
        self._pruned: set[str] = set()

    def is_synthetic(self, part_id: str) -> bool:
        """Whether the server marked this text part as its own."""
        return part_id in self._synthetic

    def text_part(
        self, part: dict[str, Any], part_id: str, turn: BridgeTurn
    ) -> HarnessEvent | None:
        """The injection a synthetic text part is, once its owner is the user."""
        if part.get("synthetic") is not True:
            return None
        self._synthetic.setdefault(part_id, False)
        return self._injection(part_id, turn)

    def pending(self, turn: BridgeTurn) -> list[HarnessEvent]:
        """Synthetic parts whose owner was not known when they arrived."""
        return [
            injection
            for part_id in list(self._synthetic)
            if (injection := self._injection(part_id, turn)) is not None
        ]

    def tool_part(
        self, runtime: str, part: dict[str, Any], call_id: str, line: str
    ) -> HarnessEvent | None:
        """A prune, when the server re-sent a completed tool part as compacted."""
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        timing = state.get("time")
        if state.get("status") != "completed" or not (
            isinstance(timing, dict) and timing.get("compacted")
        ):
            return None
        if call_id in self._pruned:
            return HarnessEvent(RAW, {"text": line[:4000]}, raw_line=line)
        self._pruned.add(call_id)
        return HarnessEvent(
            LIFECYCLE,
            {
                "phase": "context_prune",
                "source": f"{runtime}_prune",
                "tool": str(part.get("tool") or "")[:120],
                "call_id": call_id[:256],
                "chars": len(str(state.get("output") or "")),
            },
            raw_line=line,
        )

    def _injection(self, part_id: str, turn: BridgeTurn) -> HarnessEvent | None:
        message_id, text = turn.text_parts.get(part_id, ("", ""))
        if self._synthetic.get(part_id) or turn.message_roles.get(message_id) != "user":
            return None
        if not text.strip():
            return None
        self._synthetic[part_id] = True
        return HarnessEvent(
            LIFECYCLE,
            {
                "phase": "context_injection",
                "source": "server_synthetic_part",
                "label": "synthetic",
                "text": text[:INJECTED_TEXT_CHARS],
                "chars": len(text),
                **({"truncated": True} if len(text) > INJECTED_TEXT_CHARS else {}),
            },
        )
