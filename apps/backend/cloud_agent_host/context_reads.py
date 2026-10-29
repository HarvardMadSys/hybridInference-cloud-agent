"""Asking a settled CLI what its context holds, over its own protocol.

The drivers call these between turns only (``HarnessTurn.after_settle_events``),
after a turn's own stream reached its boundary, so a question can never take
a line the turn needed. Each read is bounded:

- **Claude Code** (``get_context_usage`` control request): the answer is read
  by its ``request_id``; other lines read meanwhile are put back, in order,
  for the next reader; an answer that misses the deadline is remembered and
  dropped whenever it arrives (:func:`is_late_answer`).
- **pi** (``get_session_stats``, then ``get_messages``): answered synchronously
  by pi's RPC loop. ``get_messages`` returns the whole context on one line,
  base64 images included, and a line over the host's 8 MB protocol limit stops
  the process, so it is asked for only under :data:`PI_CONTEXT_MESSAGES_TOKENS`.
- **OpenCode and Kilo** (the bridge's ``context`` command, helper version 5):
  the bridge summarizes inside the sandbox, so its answer is small. An older
  bridge refuses the command, which is recorded as unsupported.
"""

from __future__ import annotations

import json
import queue
import time
import uuid
from typing import TYPE_CHECKING, Any

from cloud_agent_host.runtimes.context_snapshot import (
    bridge_snapshot,
    claude_snapshot,
    failed_snapshot,
    pi_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: How long a settled turn waits for Claude's context meter. It counts with up
#: to about twenty token-counting calls to the inference endpoint.
CLAUDE_CONTEXT_USAGE_S = 20.0
#: pi's own context estimate under which its messages are read whole. About
#: 400k characters of text; images ride along as base64 on the same line.
PI_CONTEXT_MESSAGES_TOKENS = 100_000

CLAUDE_SOURCE = "claude_get_context_usage"


def _object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def is_late_answer(line: str, stale: set[str]) -> bool:
    """Whether ``line`` answers a context request that already gave up; forgets it."""
    record = _object(line)
    if record is None or record.get("type") != "control_response":
        return False
    response = record.get("response")
    request_id = response.get("request_id") if isinstance(response, dict) else None
    if isinstance(request_id, str) and request_id in stale:
        stale.discard(request_id)
        return True
    return False


def _claude_answer(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("subtype") != "success":
        return failed_snapshot(CLAUDE_SOURCE, "refused", str(response.get("error") or ""))
    answer = response.get("response")
    if not isinstance(answer, dict):
        return failed_snapshot(CLAUDE_SOURCE, "malformed")
    return claude_snapshot(answer)


def read_claude_context(
    *,
    write_line: Callable[[str], None],
    next_line: Callable[[float], str | None],
    put_back: Callable[[list[str]], None],
    stale: set[str],
    timeout: float,
) -> dict[str, Any]:
    """Send ``get_context_usage`` and read its answer (pinned 2.1.220).

    The CLI makes no model request for it; it counts with the provider's
    token-counting endpoint and estimates locally when that is refused.
    """
    request_id = f"context-{uuid.uuid4().hex}"
    write_line(
        json.dumps(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "get_context_usage"},
            },
            separators=(",", ":"),
        )
    )
    deadline = time.monotonic() + timeout
    others: list[str] = []
    try:
        while True:
            try:
                line = next_line(max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                stale.add(request_id)
                return failed_snapshot(CLAUDE_SOURCE, "timeout")
            if line is None:
                return failed_snapshot(CLAUDE_SOURCE, "ended")
            record = _object(line) or {}
            response = record.get("response") if record.get("type") == "control_response" else None
            if isinstance(response, dict) and response.get("request_id") == request_id:
                return _claude_answer(response)
            others.append(line)
    finally:
        put_back(others)


def read_pi_context(request: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Pi's own context estimate and, when small enough, its context messages."""
    stats = request("get_session_stats")
    data = stats.get("data") if stats.get("success") is True else None
    usage = data.get("contextUsage") if isinstance(data, dict) else None
    tokens = usage.get("tokens") if isinstance(usage, dict) else None
    if not isinstance(tokens, int) or tokens > PI_CONTEXT_MESSAGES_TOKENS:
        return pi_snapshot(None, data)
    messages = request("get_messages")
    listed = messages.get("data") if messages.get("success") is True else None
    return pi_snapshot(listed.get("messages") if isinstance(listed, dict) else None, data)


def read_bridge_context(answer: dict[str, Any], *, runtime: str) -> dict[str, Any]:
    """The bridge's answer to ``context``: its summary, or why there is none."""
    if answer.get("bridge") == "context_result":
        return bridge_snapshot(answer.get("context"), runtime=runtime)
    return failed_snapshot(
        f"{runtime}_session_messages", "unsupported", str(answer.get("detail") or "")
    )


def should_ask(*, enabled: bool, settled: bool, failed: bool, cancelled: bool, alive: bool) -> bool:
    """Whether a turn's end is one to ask about: settled cleanly, process alive."""
    return enabled and settled and not failed and not cancelled and alive


def snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The lifecycle payload a snapshot is stored as."""
    return {"phase": "context_snapshot", "snapshot": snapshot}


def error_snapshot(source: str, exc: BaseException) -> dict[str, Any]:
    """A read that broke: its source and the error's type, never its text."""
    return failed_snapshot(source, "error", type(exc).__name__)


__all__ = [
    "CLAUDE_CONTEXT_USAGE_S",
    "CLAUDE_SOURCE",
    "PI_CONTEXT_MESSAGES_TOKENS",
    "error_snapshot",
    "is_late_answer",
    "read_bridge_context",
    "read_claude_context",
    "read_pi_context",
    "should_ask",
    "snapshot_payload",
]
