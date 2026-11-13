"""Pure readings of the pinned Codex app-server's (0.145.0) notifications.

Token use arrives in ``thread/tokenUsage/updated``: ``tokenUsage.last`` is the
thread's latest model call and ``tokenUsage.total`` the thread's running sum,
in camelCase. ``turn/completed`` carries no usage at all.
"""

from __future__ import annotations

import json
from typing import Any


def codex_error_text(message: str) -> str:
    """Codex's error message without the request diagnostics it appends.

    0.145.0 appends ``, url: <address>`` and then cf-ray, request id and auth
    details to an HTTP error's message. The address is the model provider's,
    which the owner already knows; the full error stays in the event.
    """
    return message.split(", url: ", 1)[0][:2000]


def codex_tool_output(item: dict[str, Any]) -> str:
    """What a command or an MCP call returned, as the text the owner reads.

    A command's ``aggregatedOutput`` is text already. An MCP call's ``result``
    is an object of the tool's content blocks and structured content, and its
    ``error`` an object naming a message (0.145.0); ``str`` of either is a
    Python rendering. So the result is stored as JSON, as the other runtimes
    store theirs, and the error as its message.
    """
    output = item.get("aggregatedOutput") or item.get("result")
    if not output:
        error = item.get("error")
        output = (error.get("message") or error) if isinstance(error, dict) else error
    if output is None or isinstance(output, str):
        return output or ""
    return json.dumps(output, ensure_ascii=False, default=str)


def _count(value: Any) -> bool:
    """Whether a protocol counter is a token count: a non-negative integer."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _turn_usage(
    params: dict[str, Any],
    *,
    active_turn: str | None,
    spawned_in: dict[str, str],
    subagent: bool,
) -> dict[str, Any] | None:
    """One model call's usage event payload, if this notification is the active turn's.

    ``spawned_in`` maps each subagent thread to the turn its spawn item was
    seen in; ``subagent`` says the notification is from such a thread rather
    than the conversation's own.

    The pinned notification carries ``last``, the thread's latest model
    call, and ``total``, the thread's running sum. A call is counted as its
    ``last``, under an identity made of its thread and the ``total`` it
    brought the thread to: Codex repeats a notification without a new call
    (a rate-limit refresh), and a repeat names the same total, so it is
    counted once. No earlier turn's total is needed, so a resumed or
    resident thread counts only this turn's calls.

    A notification is this turn's when the conversation's thread names this
    turn, or a subagent thread was spawned in it. Anything else -- a late
    notification for a turn that has settled, one replayed at resume, a
    subagent spawned in another turn or before this process -- has no turn
    it can be proven to belong to, and stays a raw row that counts nowhere.

    ``reasoningOutputTokens`` is part of ``outputTokens``, kept beside it,
    never added to it. ``inputTokens`` already includes cached input.
    """
    thread_id = params.get("threadId")
    usage = params.get("tokenUsage")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(usage, dict):
        return None
    if not active_turn:
        return None
    if subagent:
        if spawned_in.get(thread_id) != active_turn:
            return None
    elif params.get("turnId") != active_turn:
        return None
    last, total = usage.get("last"), usage.get("total")
    if not isinstance(last, dict) or not isinstance(total, dict):
        return None
    counters = {
        "input_tokens": last.get("inputTokens"),
        "output_tokens": last.get("outputTokens"),
        "cache_read_tokens": last.get("cachedInputTokens"),
        # The pinned schema defaults an absent cache-write counter to zero.
        "cache_write_tokens": last.get("cacheWriteInputTokens", 0),
        "reasoning_output_tokens": last.get("reasoningOutputTokens"),
    }
    thread_total = total.get("totalTokens")
    if not all(_count(value) for value in (*counters.values(), thread_total)):
        return None
    return {
        **counters,
        "usage_id": f"codex:{thread_id}:{thread_total}",
        "text": f"{counters['input_tokens']} in / {counters['output_tokens']} out tokens",
    }


class CodexTurnUsage:
    """Which turn each subagent thread was spawned in, kept for the process.

    A subagent's model calls are the usage of the turn that spawned it, and
    count only while that turn is the one running.
    """

    def __init__(self) -> None:
        """Start knowing of no subagent."""
        self._spawned_in: dict[str, str] = {}

    def spawned(self, thread_id: str, turn_id: str | None) -> None:
        """Record that ``thread_id`` was spawned in ``turn_id`` (the first time seen)."""
        if turn_id:
            self._spawned_in.setdefault(thread_id, turn_id)

    def counted(
        self, params: dict[str, Any], active_turn: str | None, owner: dict[str, str]
    ) -> dict[str, Any] | None:
        """A usage event's payload, marked as ``owner``'s, or ``None`` if it counts nowhere."""
        usage = _turn_usage(
            params,
            active_turn=active_turn,
            spawned_in=self._spawned_in,
            subagent=bool(owner),
        )
        return {**usage, **owner} if usage else None


__all__ = ["CodexTurnUsage", "codex_error_text", "codex_tool_output"]
