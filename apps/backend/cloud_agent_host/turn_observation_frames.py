"""Frames produced by observations that remain independent of CLI output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent_host import question_change_signals

if TYPE_CHECKING:
    from collections.abc import Iterator


def _take_reported_checkpoint(
    supervisor: Any, member_thread_id: str, member_turn: Any
) -> dict[str, Any] | None:
    """Collect a checkpoint already produced for this turn, best-effort."""
    take = getattr(supervisor, "take_reported_checkpoint", None)
    if take is None:
        return None
    try:
        return take(member_thread_id, member_turn)
    except Exception:
        return None


def turn_observation_frames(supervisor: Any, thread_id: str, turn: Any) -> Iterator[str]:
    """Yield question hints and turn-fenced checkpoints before native output."""
    yield from question_change_signals.question_change_frames(turn)
    checkpoint = _take_reported_checkpoint(supervisor, thread_id, turn)
    if checkpoint is not None:
        yield (
            json.dumps(
                {"kind": "resource_checkpoint", "turn_resources": checkpoint},
                separators=(",", ":"),
            )
            + "\n"
        )
