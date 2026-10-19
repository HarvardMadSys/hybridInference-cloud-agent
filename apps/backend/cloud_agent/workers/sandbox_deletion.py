"""Archive retained containers in S3 before permitting their exact deletion."""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypedDict
from urllib.parse import urlparse

import httpx

from cloud_agent.capacity_protocol import FRESH_AFTER_SECONDS
from cloud_agent.domain.compute import RECLAIMED_QUIET_SECONDS
from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_archive import (
    S3SandboxArchiveStore,
    SandboxArchiveError,
    SandboxArchiveUnavailable,
    SandboxArchiveVerificationError,
)
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import (
    sandbox_manager_configuration_error,
    sandbox_manager_for_host,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.ports import HostPoolRepository, SandboxRepository
    from cloud_agent.ports.sandbox import DeletionFailureDecision

logger = get_logger(__name__)
CLAIM_TTL_SECONDS = 3900
POLL_SECONDS = 5.0
ATTEMPT_TELEMETRY_TIMEOUT_SECONDS = 0.25
#: How often to look for sandboxes whose every chat was deleted, and how many
#: to act on per pass. Neither is urgent: the chats are already hidden.
DELETED_SESSIONS_SWEEP_SECONDS = 60.0
DELETED_SESSIONS_BATCH = 20
#: Who stops and deletes those sandboxes, in their trajectory and audit rows.
DELETED_SESSIONS_ACTOR = "user-deletion-sweeper"


class _DeletionStorage(Protocol):
    """Keep destructive orchestration on the narrow host and sandbox ports."""

    host_pool: HostPoolRepository
    sandbox: SandboxRepository


class _DeletionFence(TypedDict):
    sandbox_id: str
    request_id: str
    claim_token: str
    generation: int
    container_id: str


def _validate_receipt(receipt: dict[str, Any], expected: dict[str, Any]) -> None:
    """Refuse a host answer belonging to any other request or container."""
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("Sandbox archive receipt did not match the reviewed container.")


def _host_away_reason(host: dict[str, Any] | None) -> str:
    """Name the absent host a discard waits for, and what ends the wait.

    Only that host can remove the container it may still hold. Empty while the
    host reports as usual, when an unreachable sandbox manager is a connection
    problem instead.
    """
    if host is None:
        return ""
    if not host.get("enrolled", True) and host.get("status") == "disabled":
        state, ends = "was removed from the pool", "if that host is enrolled again"
    else:
        beat = host.get("last_heartbeat_at")
        if not isinstance(beat, datetime):
            return ""
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=UTC)
        if (datetime.now(UTC) - beat).total_seconds() <= FRESH_AFTER_SECONDS:
            return ""
        state, ends = "is offline", "when the host reconnects"
    if host.get("compute_node_id") is not None:
        ends += ", or without it once its provider confirms the machine is gone"
    return (
        f"The owning host {state}, and only it can remove the container: deletion finishes {ends}."
    )


def _discard_failure_reason(cause: BaseException, host: dict[str, Any] | None = None) -> str:
    """Classify sandbox manager failures without persisting their text or request URLs.

    A host the request cannot reach because it is offline or was removed is
    named as what the deletion waits for.
    """
    transport = cause.__cause__ if isinstance(cause, SandboxManagerError) else cause
    if isinstance(transport, httpx.TransportError) and (away := _host_away_reason(host)):
        return away
    if isinstance(transport, httpx.TimeoutException):
        return "The owning host's sandbox manager timed out before confirming removal."
    if isinstance(transport, httpx.TransportError):
        return "The owning host's sandbox manager could not be reached. Check its connectivity."
    if not isinstance(cause, SandboxManagerError):
        return ""
    if cause.status_code in {401, 403}:
        return (
            "The owning host rejected the sandbox manager credential. "
            "Check that the control plane and host use the same credential."
        )
    if cause.status_code == 404:
        return (
            "The owning host could not find the sandbox or deletion endpoint. "
            "Check its container inventory and sandbox manager version."
        )
    if cause.status_code == 409:
        return (
            "The owning host refused deletion of the reviewed container. "
            "Confirm that the exact container is stopped and idle."
        )
    if cause.status_code == 410:
        return (
            "The owning host could not recover the retained sandbox. "
            "Check the reviewed container on that host."
        )
    if cause.status_code == 422:
        return (
            "The owning host rejected the deletion request. "
            "Check that its sandbox manager is up to date."
        )
    if cause.status_code == 429:
        return "The owning host's sandbox manager is busy."
    if cause.status_code >= 500:
        return (
            "The owning host's sandbox manager failed while processing deletion. "
            "Check its sandbox manager logs."
        )
    return "The owning host rejected the deletion request."


def _deletion_failure_details(stage: str, cause: BaseException) -> tuple[str, int | None, str]:
    """Map a failure to bounded diagnostics without persisting provider text."""
    status = cause.status_code if isinstance(cause, SandboxManagerError) else None
    if isinstance(cause, SandboxManagerError):
        if status in {408, 425, 429} or (status is not None and status >= 500):
            return "sandbox_manager_unavailable", status, "transient"
        if status in {401, 403, 404, 409, 410}:
            return "sandbox_manager_precondition", status, "operator_action"
        if status in {400, 422}:
            return "sandbox_manager_rejected", status, "permanent"
        return "sandbox_manager_rejected", status, "operator_action"
    if isinstance(cause, SandboxArchiveUnavailable):
        return "archive_not_configured", status, "operator_action"
    if isinstance(cause, SandboxArchiveVerificationError):
        return "archive_verification_failed", status, "operator_action"
    if isinstance(cause, SandboxArchiveError):
        return "archive_service_failed", status, "transient"
    if isinstance(cause, (asyncio.TimeoutError, httpx.TimeoutException)):
        return f"{stage}_timeout", status, "transient"
    if isinstance(cause, ValueError):
        return f"{stage}_rejected", status, "operator_action"
    return f"{stage}_failed", status, "transient"


def _prearchive_failure_is_retryable(cause: BaseException) -> bool:
    """Keep transient failures queued when no host archive barrier exists."""
    if isinstance(cause, SandboxManagerError):
        status = cause.status_code
        return status <= 0 or status in {408, 425, 429} or status >= 500
    if isinstance(cause, SandboxArchiveUnavailable):
        return False
    if isinstance(cause, (SandboxArchiveError, asyncio.TimeoutError, httpx.TimeoutException)):
        return True
    return not isinstance(cause, ValueError)


def _verify_stopped_status(status: dict[str, Any], expected: dict[str, Any]) -> None:
    """Require live host proof that this exact container is stopped or dead, and idle.

    Dead is accepted as the host's archive export and the force-delete review
    accept it: Docker's dead state is terminal and not running. Refusing it
    here would leave a force-deleted dead container undeletable, since the
    refusal needs an operator and resuming it meets the same refusal.
    """
    identity = {
        "thread_id": expected["thread_id"],
        "origin_thread_id": expected["thread_id"],
        "sandbox_id": expected["sandbox_id"],
        "generation": expected["generation"],
        "container_id": expected["container_id"],
    }
    if any(status.get(key) != value for key, value in identity.items()):
        raise ValueError("The owning host reported a different sandbox identity.")
    if status.get("running") is not False or status.get("docker_status") not in {
        "stopped",
        "dead",
    }:
        raise ValueError("The owning host did not prove that the sandbox is stopped.")
    terminal_count = status.get("terminal_count")
    operation_count = status.get("operation_count")
    counts_valid = (
        isinstance(terminal_count, int)
        and not isinstance(terminal_count, bool)
        and isinstance(operation_count, int)
        and not isinstance(operation_count, bool)
    )
    if (
        not counts_valid
        or terminal_count != 0
        or operation_count != 0
        or status.get("busy") is not False
        or status.get("group_busy") is not False
        or status.get("snapshotting") is not False
    ):
        raise ValueError("The owning host reports active sandbox work.")


async def _record_attempt_stage(
    store: _DeletionStorage,
    *,
    correlation_id: str | None,
    stage: str,
    sandbox_manager_status: int | None = None,
    cleanup_outcome: str | None = None,
) -> None:
    """Write optional telemetry without gating or fencing deletion work."""
    if correlation_id is None:
        return
    try:
        await asyncio.wait_for(
            store.sandbox.update_admin_sandbox_deletion_attempt_stage(
                correlation_id=correlation_id,
                stage=stage,
                sandbox_manager_status=sandbox_manager_status,
                cleanup_outcome=cleanup_outcome,
            ),
            timeout=ATTEMPT_TELEMETRY_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "Sandbox deletion attempt telemetry update failed",
            extra={"correlation_id": correlation_id},
        )


async def _finish_attempt(
    store: _DeletionStorage,
    *,
    correlation_id: str | None,
    outcome: str,
    failure_code: str | None = None,
    sandbox_manager_status: int | None = None,
    retry_class: str | None = None,
    retry_scheduled: bool | None = None,
    cleanup_outcome: str = "not_required",
) -> None:
    """Persist sanitized attempt results after the existing policy has run."""
    if correlation_id is None:
        return
    try:
        await asyncio.wait_for(
            store.sandbox.finish_admin_sandbox_deletion_attempt(
                correlation_id=correlation_id,
                outcome=outcome,
                failure_code=failure_code,
                sandbox_manager_status=sandbox_manager_status,
                retry_class=retry_class,
                retry_scheduled=retry_scheduled,
                cleanup_outcome=cleanup_outcome,
            ),
            timeout=ATTEMPT_TELEMETRY_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "Sandbox deletion attempt result could not be recorded",
            extra={"correlation_id": correlation_id},
        )


async def delete_one_archived_sandbox(
    store: _DeletionStorage,
    *,
    archive_factory: Callable[[], S3SandboxArchiveStore] = S3SandboxArchiveStore.from_settings,
) -> bool:
    """Process one fenced request, retaining archives across interrupted deletion.

    The database receipt commits before the sandbox manager receives permission to
    remove Docker. Once that checkpoint exists, an error keeps admission
    blocked and retries the same deletion; it never reopens a possibly removed
    native session. A verified object is checked again on every retry.
    """
    if await store.sandbox.recover_reclaimed_admin_sandbox_discard(
        reclaimed_quiet_seconds=RECLAIMED_QUIET_SECONDS
    ):
        return True
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=CLAIM_TTL_SECONDS)
    if claim is None:
        return False
    fence: _DeletionFence = {
        "sandbox_id": str(claim["id"]),
        "request_id": str(claim["deletion_request_id"]),
        "claim_token": str(claim["deletion_claim_token"]),
        "generation": int(claim["generation"]),
        "container_id": str(claim["container_id"]),
    }
    correlation_id = claim.get("deletion_attempt_correlation_id")
    correlation_id = str(correlation_id) if correlation_id is not None else None
    attempt_number = claim.get("deletion_attempt_number")
    stage = "sandbox_manager_resolution"
    cleanup_outcome = "not_required"
    sandbox_manager = None
    payload: dict[str, Any] | None = None
    discarding = bool(claim.get("deletion_discard"))
    prepare_started = False
    verified = claim["deletion_state"] == "archived"
    archive_verified = verified
    configuration_error = None
    host: dict[str, Any] | None = None
    request_phase = "resolve_sandbox_manager"
    await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
    try:
        if discarding:
            host = await store.host_pool.get_host(host=str(claim["host"]))
            if host is not None and host.get("compute_node_id") is not None:
                request_phase = "complete_reclaimed_discard"
                if await store.sandbox.complete_admin_sandbox_discard(
                    **fence,
                    attempt_correlation_id=correlation_id,
                    reclaimed_quiet_seconds=RECLAIMED_QUIET_SECONDS,
                ):
                    # Its provider reclaimed the machine, and the container
                    # with it: the host this would wait for never answers.
                    return True
                request_phase = "resolve_sandbox_manager"
            sandbox_manager = sandbox_manager_for_host(host) if host is not None else None
            if sandbox_manager is None:
                configuration_error = sandbox_manager_configuration_error(host)
                raise ValueError("The owning sandbox manager is unavailable.")
            payload = {
                "request_id": fence["request_id"],
                "generation": fence["generation"],
                "expected_container_id": fence["container_id"],
                "sandbox_id": fence["sandbox_id"],
            }
            request_phase = "discard"
            stage = "discard_container_removal"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            deleted = await sandbox_manager.discard_sandbox_deletion(
                str(claim["origin_thread_id"]), payload
            )
            request_phase = "validate_discard_receipt"
            stage = "discard_receipt_validation"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            _validate_receipt(
                deleted,
                {
                    "request_id": fence["request_id"],
                    "container_id": fence["container_id"],
                },
            )
            if deleted.get("deleted") is not True:
                raise ValueError("The host did not confirm container deletion.")
            request_phase = "complete_discard"
            stage = "database_finalization"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            completed = await store.sandbox.complete_admin_sandbox_discard(
                **fence, attempt_correlation_id=correlation_id
            )
            if not completed:
                raise ValueError("Container deletion lost its durable request fence.")
            return True
        host = await store.host_pool.get_host(host=str(claim["host"]))
        sandbox_manager = sandbox_manager_for_host(host) if host is not None else None
        if sandbox_manager is None:
            raise SandboxManagerError(503, "The owning sandbox manager is unavailable.")
        thread_id = str(claim["origin_thread_id"])
        if claim.get("deletion_cleanup_required"):
            payload = {
                "request_id": fence["request_id"],
                "generation": fence["generation"],
                "expected_container_id": fence["container_id"],
                "archive_key": str(claim["deletion_cleanup_archive_key"]),
            }
            # A timed-out abort may still be running on the host. Never start
            # another export for this request until cleanup is acknowledged.
            stage = "cleanup_abort"
            cleanup_outcome = "pending"
            await _record_attempt_stage(
                store,
                correlation_id=correlation_id,
                stage=stage,
                cleanup_outcome=cleanup_outcome,
            )
            raise ValueError("Sandbox archive cleanup is waiting for the host.")
        archives = archive_factory()
        if not archives.available:
            raise ValueError("S3 sandbox archive storage is not configured.")
        archive_location = claim.get("deletion_archive_key") if verified else None
        key = (
            urlparse(str(archive_location)).path.lstrip("/")
            if archive_location
            else archives.key_for(fence["sandbox_id"], fence["request_id"])
        )
        payload = {
            "request_id": fence["request_id"],
            "generation": fence["generation"],
            "expected_container_id": fence["container_id"],
            "archive_key": key,
        }
        expected = {
            "request_id": fence["request_id"],
            "container_id": fence["container_id"],
            "archive_key": key,
        }
        if verified:
            sha256 = str(claim["deletion_sha256"])
            size_bytes = int(claim["deletion_size_bytes"])
        else:
            stage = "stop_verification"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            status = await sandbox_manager.shared_chat_sandbox_status(thread_id, timeout=10)
            _verify_stopped_status(
                status,
                {
                    "thread_id": thread_id,
                    "sandbox_id": fence["sandbox_id"],
                    "generation": fence["generation"],
                    "container_id": fence["container_id"],
                },
            )
            prepare_started = True
            stage = "archive_prepare"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            prepared = await sandbox_manager.prepare_sandbox_deletion(thread_id, payload)
            _validate_receipt(prepared, expected)
            sha256 = str(prepared.get("sha256") or "")
            raw_size = prepared.get("size_bytes")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError("Sandbox archive omitted its SHA-256 checksum.")
            if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size <= 0:
                raise ValueError("Sandbox archive omitted its byte length.")
            size_bytes = raw_size
            stage = "upload_authorization"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            upload = await archives.prepare_upload(
                fence["sandbox_id"], fence["request_id"], sha256, size_bytes
            )
            if upload.key != key:
                raise ValueError("Sandbox archive upload destination changed.")
            stage = "archive_upload"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            uploaded = await sandbox_manager.upload_sandbox_deletion_archive(
                thread_id,
                {
                    **payload,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "upload_url": upload.upload_url,
                    "upload_headers": upload.headers,
                },
            )
            _validate_receipt(uploaded, {**expected, "sha256": sha256, "size_bytes": size_bytes})
            if uploaded.get("uploaded") is not True:
                raise ValueError("The host did not confirm the sandbox archive upload.")
        stage = "archive_verification"
        await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
        receipt = await archives.verify_archive(str(archive_location or key), sha256, size_bytes)
        archive_verified = True
        if not verified:
            stage = "durable_receipt"
            await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
            recorded = await store.sandbox.record_admin_sandbox_archive(
                **fence,
                archive_key=receipt.archive_uri,
                sha256=sha256,
                size_bytes=size_bytes,
                archive_version_id=receipt.version_id,
            )
            if not recorded:
                raise ValueError("The sandbox archive lost its durable request fence.")
            verified = True
        stage = "container_removal"
        await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
        deleted = await sandbox_manager.commit_sandbox_deletion(
            thread_id, {**payload, "sha256": sha256, "size_bytes": size_bytes}
        )
        _validate_receipt(deleted, {**expected, "sha256": sha256, "size_bytes": size_bytes})
        if deleted.get("deleted") is not True:
            raise ValueError("The host did not confirm container deletion.")
        stage = "database_finalization"
        await _record_attempt_stage(store, correlation_id=correlation_id, stage=stage)
        completed = await store.sandbox.complete_admin_sandbox_deletion(
            **fence, attempt_correlation_id=correlation_id
        )
        if not completed:
            raise ValueError("Container deletion lost its durable request fence.")
    except BaseException as cause:
        # Upload URLs are credentials. Never persist arbitrary SDK/HTTP errors
        # containing request URLs in user-visible state or operational logs.
        failure_stage = stage
        failure_code, sandbox_manager_status, retry_class = _deletion_failure_details(
            failure_stage, cause
        )
        await _record_attempt_stage(
            store,
            correlation_id=correlation_id,
            stage=failure_stage,
            sandbox_manager_status=sandbox_manager_status,
        )
        abort_acknowledged = False
        if (
            not discarding
            and not archive_verified
            and sandbox_manager is not None
            and payload is not None
            and (prepare_started or claim.get("deletion_cleanup_required"))
        ):
            cleanup_outcome = "pending"
            await _record_attempt_stage(
                store,
                correlation_id=correlation_id,
                stage=failure_stage,
                sandbox_manager_status=sandbox_manager_status,
                cleanup_outcome=cleanup_outcome,
            )
            try:
                # Renew before touching the host barrier. An expired claimant
                # must not abort a newer worker's operation for this request.
                owned = await store.sandbox.renew_admin_sandbox_deletion_claim(
                    **fence,
                    ttl_seconds=300,
                    for_cleanup=True,
                    cleanup_archive_key=str(payload["archive_key"]),
                )
                if owned:
                    await _record_attempt_stage(
                        store,
                        correlation_id=correlation_id,
                        stage=failure_stage,
                        sandbox_manager_status=sandbox_manager_status,
                        cleanup_outcome=cleanup_outcome,
                    )
                    aborted = await sandbox_manager.abort_sandbox_deletion(
                        str(claim["origin_thread_id"]), payload
                    )
                    _validate_receipt(
                        aborted,
                        {
                            "request_id": fence["request_id"],
                            "container_id": fence["container_id"],
                            "archive_key": payload["archive_key"],
                        },
                    )
                    abort_acknowledged = aborted.get("aborted") is True
                    cleanup_outcome = "acknowledged" if abort_acknowledged else "unconfirmed"
                else:
                    cleanup_outcome = "unconfirmed"
            except Exception:
                cleanup_outcome = "unconfirmed"
            await _record_attempt_stage(
                store,
                correlation_id=correlation_id,
                stage=failure_stage,
                sandbox_manager_status=sandbox_manager_status,
                cleanup_outcome=cleanup_outcome,
            )
        elif not discarding and not prepare_started and not claim.get("deletion_cleanup_required"):
            # Before prepare begins there is no host archive barrier to abort.
            # A permanent precondition can be released for operator review;
            # transient host or archive-service failures remain retryable.
            abort_acknowledged = not _prearchive_failure_is_retryable(cause)
        # A discard may already have removed Docker. Retry that exact request
        # instead of aborting it or starting an archive for a missing container.
        retain_pending = discarding or archive_verified or not abort_acknowledged
        if discarding:
            reason = configuration_error or _discard_failure_reason(cause, host)
            error = (
                "Container removal without an archive could not be confirmed. "
                + (f"{reason} " if reason else "")
                + "Deletion will be retried."
            )
        else:
            error = "S3 archive or sandbox deletion could not complete. " + (
                "The verified archive is retained; deletion will be retried."
                if archive_verified
                else (
                    "The container is retained; the request will retry when the host is available."
                    if retain_pending
                    else "The container has not been deleted. Refresh details before retrying."
                )
            )
        failure_decision: DeletionFailureDecision | None = None
        # A failed database response leaves the business transition uncertain.
        # Keep the attempt open so a later claim can classify an actually
        # expired lease instead of reporting one speculatively.
        with contextlib.suppress(Exception):
            failure_decision = await store.sandbox.fail_admin_sandbox_deletion(
                **fence,
                error=error,
                retain_pending=retain_pending,
                attempt_correlation_id=correlation_id,
                failure_code=failure_code,
                sandbox_manager_status=sandbox_manager_status,
                retry_class=retry_class,
                cleanup_outcome=cleanup_outcome,
            )
        if failure_decision is not None and not failure_decision.recorded:
            await _finish_attempt(
                store,
                correlation_id=correlation_id,
                outcome="lease_expired",
                failure_code=failure_code,
                sandbox_manager_status=sandbox_manager_status,
                retry_class=retry_class,
                retry_scheduled=None,
                cleanup_outcome=cleanup_outcome,
            )
        if isinstance(cause, asyncio.CancelledError):
            raise
        if not isinstance(cause, Exception):
            raise
        logger.warning(
            "Sandbox archive/delete request did not complete",
            extra={
                "sandbox_id": fence["sandbox_id"],
                "correlation_id": correlation_id,
                "attempt_number": attempt_number,
                "failure_type": type(cause).__name__,
                "request_phase": request_phase if discarding else "archive_delete",
                "stage": failure_stage,
                "failure_code": failure_code,
                "retry_class": retry_class,
                "retry_scheduled": (
                    failure_decision.retry_scheduled if failure_decision is not None else None
                ),
                "retry_at": (
                    failure_decision.retry_at.isoformat()
                    if failure_decision is not None and failure_decision.retry_at is not None
                    else None
                ),
                "terminal_reason": (
                    failure_decision.terminal_reason if failure_decision is not None else None
                ),
                "failure_transition_recorded": (
                    failure_decision.recorded if failure_decision is not None else None
                ),
                "cleanup_outcome": cleanup_outcome,
                "sandbox_manager_status": sandbox_manager_status,
            },
        )
    return True


async def retire_deleted_session_sandboxes(
    store: _DeletionStorage,
    *,
    archive_available: Callable[[], bool] = lambda: S3SandboxArchiveStore.from_settings().available,
    limit: int = DELETED_SESSIONS_BATCH,
) -> int:
    """Stop, then queue deletion of, each sandbox whose every chat was deleted.

    A running sandbox is asked to stop first; a later pass finds it stopped.
    Deletion then takes the administrator's path: archived to S3 first when
    this deployment has an archive bucket, discarded otherwise. Database
    records are kept either way. Returns how many requests were accepted.
    """
    accepted = 0
    discard = not archive_available()
    for row in await store.sandbox.sandboxes_of_deleted_sessions(limit=limit):
        if row["state"] == "running":
            result = await store.sandbox.request_deleted_sessions_stop(
                sandbox_id=str(row["sandbox_id"]),
                generation=int(row["generation"]),
                container_id=str(row["container_id"]),
                actor_id=DELETED_SESSIONS_ACTOR,
            )
        else:
            result = await store.sandbox.request_admin_delete(
                sandbox_id=str(row["sandbox_id"]),
                generation=int(row["generation"]),
                container_id=str(row["container_id"]),
                last_activity_at=row["last_activity_at"],
                actor_id=DELETED_SESSIONS_ACTOR,
                admin_ip="internal",
                discard=discard,
                source="system",
            )
        if result == "accepted":
            accepted += 1
    return accepted


async def coordinate_sandbox_deletions(
    store: _DeletionStorage,
    *,
    interval_seconds: float = POLL_SECONDS,
    deleted_sessions_interval_seconds: float = DELETED_SESSIONS_SWEEP_SECONDS,
) -> None:
    """Retry durable deletion requests independently of host heartbeats.

    Less often, also request the stop and deletion of sandboxes whose chats
    were all deleted; those requests then flow through the same retries.
    Requests that keep failing can keep the queue from ever emptying, so the
    sweep does not wait for it to.
    """
    if interval_seconds <= 0:
        raise ValueError("Sandbox deletion interval must be positive.")
    loop = asyncio.get_running_loop()
    next_sweep = loop.time()
    while True:
        if loop.time() >= next_sweep:
            next_sweep = loop.time() + deleted_sessions_interval_seconds
            try:
                await retire_deleted_session_sandboxes(store)
            except Exception:
                logger.warning("Deleted-session sandbox sweep failed")
        try:
            while await delete_one_archived_sandbox(store):
                if loop.time() >= next_sweep:
                    break
        except Exception:
            logger.warning("Sandbox deletion coordinator pass failed")
        await asyncio.sleep(interval_seconds)
