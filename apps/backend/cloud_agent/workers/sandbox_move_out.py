"""Move the groups of a Host whose lease is ending to other Hosts, one at a time.

A Host in its move-out window holds every claim (the ``move_out`` admission
hold the compute reconciler sets). This sweep finishes the move: it asks the
Host to stop each idle group, whose final archive then records a checkpoint,
and hands each covered stopped group over as ``cold``. The next turn of any of
its chats is claimed by another restore-capable Host and recreated there from
that checkpoint. A running turn is never interrupted; its group is stopped
once the turn has settled. See
``docs/agents/specs/2026-10-16-host-move-out-design.zh-CN.md``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports import SandboxRepository

logger = get_logger(__name__)

MOVE_OUT_SWEEP_SECONDS = 15.0
MOVE_OUT_BATCH = 50
MOVE_OUT_ACTOR = "host-move-out"


class _MoveOutStorage(Protocol):
    sandbox: SandboxRepository


async def move_out_sandboxes(store: _MoveOutStorage, *, limit: int = MOVE_OUT_BATCH) -> int:
    """Request each idle group's stop and hand each covered one over; count both."""
    moved = await store.sandbox.release_moving_out_groups()
    stopped = 0
    for row in await store.sandbox.sandboxes_moving_out(limit=limit):
        result = await store.sandbox.request_move_out_stop(
            sandbox_id=str(row["sandbox_id"]),
            generation=int(row["generation"]),
            container_id=str(row["container_id"]),
            actor_id=MOVE_OUT_ACTOR,
        )
        if result == "accepted":
            stopped += 1
    if moved or stopped:
        logger.info(
            "host_move_out_progress",
            extra={"handed_over": len(moved), "stops_requested": stopped},
        )
    return len(moved) + stopped


async def coordinate_move_out(
    store: _MoveOutStorage, *, interval_seconds: float = MOVE_OUT_SWEEP_SECONDS
) -> None:
    """Run the move-out sweep until cancelled; a failed pass waits for the next."""
    if interval_seconds <= 0:
        raise ValueError("Move-out interval must be positive.")
    while True:
        try:
            await move_out_sandboxes(store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("host_move_out_sweep_failed", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(interval_seconds)
