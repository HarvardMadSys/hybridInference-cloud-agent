"""Archive retained containers in S3 before permitting their exact deletion."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Any, Protocol, TypedDict
from urllib.parse import urlparse

import httpx

from cloud_agent.broker_routing import broker_configuration_error, broker_for_host
from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_archive import S3SandboxArchiveStore
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.ports import HostPoolRepository, SandboxRepository

logger = get_logger(__name__)
CLAIM_TTL_SECONDS = 3900
POLL_SECONDS = 5.0


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


def _discard_failure_reason(cause: BaseException) -> str:
    """Classify broker failures without persisting their text or request URLs."""
    transport = cause.__cause__ if isinstance(cause, WorkspaceBrokerError) else cause
    if isinstance(transport, httpx.TimeoutException):
        return "The owning host's workspace broker timed out before confirming removal."
    if isinstance(transport, httpx.TransportError):
        return "The owning host's workspace broker could not be reached. Check its connectivity."
    if not isinstance(cause, WorkspaceBrokerError):
        return ""
    if cause.status_code in {401, 403}:
        return (
            "The owning host rejected the workspace broker credential. "
            "Check that the control plane and host use the same credential."
        )
    if cause.status_code == 404:
        return (
            "The owning host could not find the sandbox or deletion endpoint. "
            "Check its container inventory and workspace broker version."
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
            "Check that its workspace broker is up to date."
        )
    if cause.status_code == 429:
        return "The owning host's workspace broker is busy."
    if cause.status_code >= 500:
        return (
            "The owning host's workspace broker failed while processing deletion. "
            "Check its broker logs."
        )
    return "The owning host rejected the deletion request."


async def delete_one_archived_sandbox(
    store: _DeletionStorage,
    *,
    archive_factory: Callable[[], S3SandboxArchiveStore] = S3SandboxArchiveStore.from_settings,
) -> bool:
    """Process one fenced request, retaining archives across interrupted deletion.

    The database receipt commits before the broker receives permission to
    remove Docker. Once that checkpoint exists, an error keeps admission
    blocked and retries the same deletion; it never reopens a possibly removed
    native session. A verified object is checked again on every retry.
    """
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
    broker = None
    payload: dict[str, Any] | None = None
    discarding = bool(claim.get("deletion_discard"))
    verified = claim["deletion_state"] == "archived"
    archive_verified = verified
    configuration_error = None
    request_phase = "resolve_broker"
    try:
        if discarding:
            host = await store.host_pool.get_host(host=str(claim["host"]))
            broker = broker_for_host(host) if host is not None else None
            if broker is None:
                configuration_error = broker_configuration_error(host)
                raise ValueError("The owning workspace broker is unavailable.")
            payload = {
                "request_id": fence["request_id"],
                "generation": fence["generation"],
                "expected_container_id": fence["container_id"],
            }
            request_phase = "discard"
            deleted = await broker.discard_sandbox_deletion(str(claim["origin_thread_id"]), payload)
            request_phase = "validate_discard_receipt"
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
            completed = await store.sandbox.complete_admin_sandbox_discard(**fence)
            if not completed:
                raise ValueError("Container deletion lost its durable request fence.")
            return True
        host = await store.host_pool.get_host(host=str(claim["host"]))
        broker = broker_for_host(host) if host is not None else None
        if broker is None:
            raise ValueError("The owning workspace broker is unavailable.")
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
            prepared = await broker.prepare_sandbox_deletion(thread_id, payload)
            _validate_receipt(prepared, expected)
            sha256 = str(prepared.get("sha256") or "")
            raw_size = prepared.get("size_bytes")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError("Sandbox archive omitted its SHA-256 checksum.")
            if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size <= 0:
                raise ValueError("Sandbox archive omitted its byte length.")
            size_bytes = raw_size
            upload = await archives.prepare_upload(
                fence["sandbox_id"], fence["request_id"], sha256, size_bytes
            )
            if upload.key != key:
                raise ValueError("Sandbox archive upload destination changed.")
            uploaded = await broker.upload_sandbox_deletion_archive(
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
        receipt = await archives.verify_archive(str(archive_location or key), sha256, size_bytes)
        archive_verified = True
        if not verified:
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
        deleted = await broker.commit_sandbox_deletion(
            thread_id, {**payload, "sha256": sha256, "size_bytes": size_bytes}
        )
        _validate_receipt(deleted, {**expected, "sha256": sha256, "size_bytes": size_bytes})
        if deleted.get("deleted") is not True:
            raise ValueError("The host did not confirm container deletion.")
        completed = await store.sandbox.complete_admin_sandbox_deletion(**fence)
        if not completed:
            raise ValueError("Container deletion lost its durable request fence.")
    except BaseException as cause:
        # Upload URLs are credentials. Never persist arbitrary SDK/HTTP errors
        # containing request URLs in user-visible state or operational logs.
        abort_acknowledged = False
        if not discarding and not archive_verified and broker is not None and payload is not None:
            with contextlib.suppress(Exception):
                # Renew before touching the host barrier. An expired claimant
                # must not abort a newer worker's operation for this request.
                owned = await store.sandbox.renew_admin_sandbox_deletion_claim(
                    **fence,
                    ttl_seconds=300,
                    for_cleanup=True,
                    cleanup_archive_key=str(payload["archive_key"]),
                )
                if owned:
                    aborted = await broker.abort_sandbox_deletion(
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
        # A discard may already have removed Docker. Retry that exact request
        # instead of aborting it or starting an archive for a missing container.
        retain_pending = discarding or archive_verified or not abort_acknowledged
        if discarding:
            reason = configuration_error or _discard_failure_reason(cause)
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
        with contextlib.suppress(Exception):
            await store.sandbox.fail_admin_sandbox_deletion(
                **fence, error=error, retain_pending=retain_pending
            )
        if isinstance(cause, asyncio.CancelledError):
            raise
        if not isinstance(cause, Exception):
            raise
        logger.warning(
            "Sandbox archive/delete request did not complete",
            extra={
                "sandbox_id": fence["sandbox_id"],
                "failure_type": type(cause).__name__,
                "request_phase": request_phase if discarding else "archive_delete",
                "broker_status": cause.status_code
                if isinstance(cause, WorkspaceBrokerError)
                else None,
            },
        )
    return True


async def coordinate_sandbox_deletions(
    store: _DeletionStorage, *, interval_seconds: float = POLL_SECONDS
) -> None:
    """Retry durable deletion requests independently of host heartbeats."""
    if interval_seconds <= 0:
        raise ValueError("Sandbox deletion interval must be positive.")
    while True:
        try:
            while await delete_one_archived_sandbox(store):
                pass
        except Exception:
            logger.warning("Sandbox deletion coordinator pass failed")
        await asyncio.sleep(interval_seconds)
