"""Shared-workspace snapshot capture, on a clock of its own.

Deliberately not part of the reaper's loop: capturing a due snapshot can wait
on a host, and a slow host must not stretch the interval at which leases are
reaped. Same process, same lifespan, same order of startup and shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository, SnapshotRepository

#: The moved loop keeps logging under the module it came from: the deployment's
#: log selection is on ``cloud_agent.app``, and a relocation must not move these
# records to a logger nobody watches.
logger = get_logger("cloud_agent.app")


class _SnapshotCapture(Protocol):
    """Capturing a due shared-workspace snapshot, through its host's broker."""

    host_pool: HostPoolRepository
    snapshot: SnapshotRepository


SNAPSHOT_CAPTURE_INTERVAL_S = 5.0


async def _capture_due_shared_snapshot(store: _SnapshotCapture) -> None:
    """Capture at most one quiescent shared workspace without blocking turns."""
    snapshot = await store.snapshot.claim_due_sandbox_snapshot()
    if snapshot is None:
        return

    from cloud_agent.broker_routing import broker_for_job
    from cloud_agent.workspace_broker_client import WorkspaceBrokerError

    try:
        broker = await broker_for_job(store.host_pool, snapshot["anchor_job_id"])
        if broker is None:
            await store.snapshot.defer_sandbox_snapshot_capture(
                snapshot_id=int(snapshot["snapshot_id"]),
                sandbox_id=str(snapshot["sandbox_id"]),
                sandbox_generation=int(snapshot["generation"]),
                container_id=str(snapshot["container_id"]),
                detail="the owning workspace broker is unavailable",
            )
            return
        receipt = await broker.capture_shared_snapshot(snapshot)
        expected = {
            "snapshot_id": int(snapshot["snapshot_id"]),
            "sandbox_id": snapshot["sandbox_id"],
            "sandbox_generation": int(snapshot["generation"]),
            "container_id": snapshot["container_id"],
            "workspace_epoch": int(snapshot["workspace_epoch"]),
            "base_sha": snapshot["base_sha"],
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError("shared snapshot host receipt identity did not match the claim")
        patch = receipt.get("patch")
        digest = receipt.get("content_sha256")
        if not isinstance(patch, str) or not isinstance(digest, str):
            raise ValueError("shared snapshot host receipt omitted its patch or hash")
        recorded = await store.snapshot.record_sandbox_snapshot_capture(
            snapshot_id=int(snapshot["snapshot_id"]),
            sandbox_id=str(snapshot["sandbox_id"]),
            sandbox_generation=int(snapshot["generation"]),
            container_id=str(snapshot["container_id"]),
            workspace_epoch=int(snapshot["workspace_epoch"]),
            base_sha=str(snapshot["base_sha"]),
            patch=patch,
            content_sha256=digest,
        )
        if recorded is None:
            raise ValueError("shared snapshot receipt lost its durable generation fence")
    except WorkspaceBrokerError as exc:
        await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=int(snapshot["snapshot_id"]),
            sandbox_id=str(snapshot["sandbox_id"]),
            sandbox_generation=int(snapshot["generation"]),
            container_id=str(snapshot["container_id"]),
            detail=str(exc),
            dirty=exc.status_code in {410, 422, 502},
        )
    except ValueError as exc:
        await store.snapshot.defer_sandbox_snapshot_capture(
            snapshot_id=int(snapshot["snapshot_id"]),
            sandbox_id=str(snapshot["sandbox_id"]),
            sandbox_generation=int(snapshot["generation"]),
            container_id=str(snapshot["container_id"]),
            detail=str(exc),
            dirty=True,
        )
    except asyncio.CancelledError:
        # A graceful control-plane stop must not leave this claim invisible
        # until the much longer stalled-capture timeout elapses.
        with contextlib.suppress(Exception):
            await store.snapshot.defer_sandbox_snapshot_capture(
                snapshot_id=int(snapshot["snapshot_id"]),
                sandbox_id=str(snapshot["sandbox_id"]),
                sandbox_generation=int(snapshot["generation"]),
                container_id=str(snapshot["container_id"]),
                detail="snapshot coordinator stopped during capture",
            )
        raise
    except Exception as exc:
        # Routing/database surprises are retryable infrastructure failures.
        # Release the durable claim before surfacing it to the coordinator's
        # log-and-continue boundary.
        with contextlib.suppress(Exception):
            await store.snapshot.defer_sandbox_snapshot_capture(
                snapshot_id=int(snapshot["snapshot_id"]),
                sandbox_id=str(snapshot["sandbox_id"]),
                sandbox_generation=int(snapshot["generation"]),
                container_id=str(snapshot["container_id"]),
                detail=str(exc) or type(exc).__name__,
            )
        raise


async def coordinate_shared_snapshots(
    store: _SnapshotCapture,
    *,
    interval_seconds: float = SNAPSHOT_CAPTURE_INTERVAL_S,
) -> None:
    """Capture shared-workspace publication snapshots on an independent clock."""
    if interval_seconds <= 0:
        raise ValueError("snapshot capture interval must be positive")
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _capture_due_shared_snapshot(store)
        except Exception:
            logger.warning("Shared workspace snapshot pass failed", exc_info=True)
