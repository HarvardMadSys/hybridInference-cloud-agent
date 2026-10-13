"""Shared-workspace snapshot capture, on a clock of its own.

Deliberately not part of the reaper's loop: capturing a due snapshot can wait
on a host, and a slow host must not stretch the interval at which leases are
reaped. Same process, same lifespan, same order of startup and shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting
from cloud_agent.workers.background_wake import SNAPSHOT
from cloud_agent_shared.host_settings import HOST_SETTINGS_BY_KEY

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository, SnapshotRepository
    from cloud_agent.ports.environments import EnvironmentRepository
    from cloud_agent.workers.background_wake import BackgroundWake

#: The moved loop keeps logging under the module it came from: the deployment's
#: log selection is on ``cloud_agent.app``, and a relocation must not move these
# records to a logger nobody watches.
logger = get_logger("cloud_agent.app")


class _SnapshotCapture(Protocol):
    """Capturing a due shared-workspace snapshot, through its host's sandbox manager."""

    host_pool: HostPoolRepository
    snapshot: SnapshotRepository
    environments: EnvironmentRepository


SNAPSHOT_CAPTURE_INTERVAL_S = 5.0
#: The largest patch a snapshot receipt may carry. The broker captures it from
#: Git output it stops at its command-output limit, and no host may set that
#: above this setting's maximum, so an honest receipt never exceeds it. A
#: larger one is a host misbehaving, and is refused before it reaches the
#: database rather than stored for the publisher to reject later.
MAX_SNAPSHOT_PATCH_BYTES = (
    HOST_SETTINGS_BY_KEY["workspace_command_output_max_mib"].maximum * 1024 * 1024
)
#: The longest an idle coordinator that is told about new work sleeps. It wakes
#: sooner for a notification or the earliest capture retry; this is for what
#: announces nothing, such as a capture the reaper found stalled.
SNAPSHOT_IDLE_S = 30.0


async def _capture_due_shared_snapshot(store: _SnapshotCapture) -> bool:
    """Capture at most one quiescent shared workspace without blocking turns.

    Reports whether one was due, captured or deferred.
    """
    snapshot = await store.snapshot.claim_due_sandbox_snapshot()
    if snapshot is None:
        return False

    from cloud_agent.sandbox_manager_client import SandboxManagerError
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    try:
        sandbox_manager = await sandbox_manager_for_job(store.host_pool, snapshot["anchor_job_id"])
        if sandbox_manager is None:
            await store.snapshot.defer_sandbox_snapshot_capture(
                snapshot_id=int(snapshot["snapshot_id"]),
                sandbox_id=str(snapshot["sandbox_id"]),
                sandbox_generation=int(snapshot["generation"]),
                container_id=str(snapshot["container_id"]),
                detail="the owning sandbox manager is unavailable",
            )
            return True
        environment = (
            await store.environments.get_job_environment(snapshot["anchor_job_id"])
            if getattr(store, "environments", None)
            else None
        )
        if environment is not None and environment.required:
            snapshot = {**snapshot, "environment": environment.model_dump(mode="json")}
        receipt = await sandbox_manager.capture_shared_snapshot(snapshot)
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
        if len(patch.encode("utf-8")) > MAX_SNAPSHOT_PATCH_BYTES:
            raise ValueError("shared snapshot host receipt exceeds the patch size limit")
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
    except SandboxManagerError as exc:
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
    return True


async def _next_look_s(
    store: _SnapshotCapture, wake: BackgroundWake, interval_seconds: float
) -> float:
    """How long the coordinator may sleep before it looks for a capture again."""
    if not wake.listening:
        return interval_seconds
    idle_s = number_setting("snapshot_idle_s", SNAPSHOT_IDLE_S)
    with contextlib.suppress(Exception):
        due = await store.snapshot.next_snapshot_retry_due()
        if due is not None:
            idle_s = min(idle_s, (due - datetime.now(UTC)).total_seconds())
    return max(idle_s, 0.0)


async def coordinate_shared_snapshots(
    store: _SnapshotCapture,
    *,
    interval_seconds: float | None = None,
    wake: BackgroundWake | None = None,
) -> None:
    """Capture shared-workspace publication snapshots on an independent clock.

    With ``wake`` a snapshot that becomes due is captured at once, and an idle
    coordinator sleeps until it is woken or the earliest deferred capture may be
    retried. Each pass captures every workspace that is due: a deferred capture
    is not claimed again before its retry time, so it neither loops on a busy
    host nor holds up the workspaces behind it.
    """
    if interval_seconds is not None and interval_seconds <= 0:
        raise ValueError("snapshot capture interval must be positive")
    while True:
        interval = (
            interval_seconds
            if interval_seconds is not None
            else number_setting("snapshot_capture_interval_s", SNAPSHOT_CAPTURE_INTERVAL_S)
        )
        if wake is None:
            await asyncio.sleep(interval)
        else:
            await wake.wait(SNAPSHOT, await _next_look_s(store, wake, interval))
        try:
            while await _capture_due_shared_snapshot(store):
                pass
        except Exception:
            logger.warning("Shared workspace snapshot pass failed", exc_info=True)
