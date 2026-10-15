"""Materializing a native Fork: the saga between prepare and finalize.

The handler validates the anchor, asks the store to prepare the operation and
maps what happens here onto HTTP; this flow does the rest — claim the
coordinator lease, establish both residencies, mint their credentials, ask the
retaining host to fork, and record the receipt. It was 186 lines in the middle
of `fork_agent_job`; a flow that can only be reached through an HTTP request is
a flow whose failure branches can only be tested through one.

Nothing about the steps changed. The order, the transaction boundaries the
store owns, the error mapping — every branch still raises the same
``HTTPException`` with the same status, type and message — and the worker-fenced
cleanup a failed mint performs is the same nested helper, moved with the block
that calls it. `release_failed_fork_residencies` stays nested for exactly that
reason: as a module function it would need three closure variables as
parameters, and the call inside the block would stop being the same line.

The reaper reaches `finalize_session_fork` too, and does something different
with it — it collects ids instead of refusing, and releases residencies through
the sandbox manager rather than the store. §5.2 keeps that difference: only steps whose
semantics really match are shared, and these do not.
"""

from __future__ import annotations

import contextlib
import secrets
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException

from cloud_agent.application.residency import _mint_residency_grant, residency_ttl_s
from cloud_agent.sandbox_manager_client import (
    SHARED_PLUGIN_SESSION_FORK_TIMEOUT_S,
    SHARED_SESSION_FORK_TIMEOUT_S,
    SandboxManagerError,
)
from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job
from cloud_agent.workspace_errors import _sandbox_manager_http_error
from cloud_agent_shared.environment import environment_payload

if TYPE_CHECKING:
    from cloud_agent.ports import (
        GrantRepository,
        HostPoolRepository,
        ResidencyRepository,
        SessionForkRepository,
    )
    from cloud_agent.ports.environments import EnvironmentRepository


class _ForkMaterializationStorage(Protocol):
    """What materializing a fork reaches the store for."""

    grants: GrantRepository
    host_pool: HostPoolRepository
    residency: ResidencyRepository
    session_fork: SessionForkRepository
    environments: EnvironmentRepository


# Keep a short recovery margin past the host request's deadline so a second API
# worker cannot take over while the first is recording its receipt. Plugin
# installation needs a longer request and lease; the stalled-saga reconciler
# also respects that live lease.
SESSION_FORK_COORDINATOR_LEASE_S = SHARED_SESSION_FORK_TIMEOUT_S + 30.0


async def materialize_session_fork(
    operation: dict[str, Any],
    *,
    job_id: str,
    job_store: _ForkMaterializationStorage,
    grants: Any,
) -> None:
    """Drive a PREPARING fork operation to MATERIALIZED, or refuse it.

    Raises:
        HTTPException: the same refusals the handler used to raise from here —
            an unreachable host, an incomplete manifest, a fork already being
            materialized, a changed fence, credentials that could not be
            minted, and a receipt that does not match.
    """
    operation_id = str(operation["id"])

    async def release_failed_fork_residencies(rows: dict[str, Any]) -> None:
        for role in ("source", "target"):
            residency = rows[role]
            grant_id = await job_store.residency.release_residency(
                thread_id=str(residency["thread_id"]),
                generation=int(residency["generation"]),
                host=str(operation["host"]),
            )
            if grant_id and grants is not None:
                with contextlib.suppress(Exception):
                    await grants.revoke(grant_id)

    sandbox_manager = await sandbox_manager_for_job(job_store.host_pool, job_id)
    if sandbox_manager is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "sandbox_host_unavailable",
                    "message": ("The host retaining this chat is not reachable for native Fork."),
                }
            },
        )
    # Validate every immutable host input before minting or rotating either
    # residency. A malformed manifest is a control-plane defect, not a
    # reason to leak two renewable grants into an uncallable saga.
    source_job = operation.get("source_job") or {}
    source_thread_id = str(operation["source_thread_id"])
    members: list[dict[str, Any]] = []
    for member in operation.get("group_members") or []:
        manifest = {
            key: member.get(key)
            for key in (
                "thread_id",
                "native_session_id",
                "context_status",
                "activation_generation",
                "process_generation",
                "residency_generation",
                "resident_state",
                "resident_slot_id",
                "last_settled_job_id",
            )
        }
        members.append(manifest)
    source_member = next(
        (member for member in members if member["thread_id"] == source_thread_id),
        None,
    )
    if source_member is None or operation.get("resident_slot_id") is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_context_unavailable",
                    "message": "The shared sandbox manifest is incomplete.",
                }
            },
        )
    launch_common: dict[str, Any] = {
        "runtime_id": str(operation["runtime_id"]),
        "runtime_version": operation.get("runtime_version"),
        "model": str(source_job.get("model") or ""),
        "reasoning_effort": source_job.get("reasoning_effort"),
        "plugins": source_job.get("plugins") or [],
        # Replaced by the sandbox manager's host-local, sandbox-reachable relay.
        "gateway_base_url": "",
        # Fork-only does not need a tool credential. A later turn whose
        # MCP set is non-empty performs a member-only native restart.
        "mcp_server_ids": [],
        "mcp_base_url": "",
        "mcp_credential": "",
    }
    if source_job.get("environment_binding_id"):
        environment = await job_store.environments.get_environment_binding(
            source_job["user_id"], source_job["environment_binding_id"]
        )
        if environment.required:
            # The forked launch needs no snapshot fields; an older broker
            # would refuse them.
            launch_common["environment"] = environment_payload(environment, ())
            await sandbox_manager.prepare_environment(source_thread_id, environment)
    coordinator_token = secrets.token_urlsafe(32)
    claimed = await job_store.session_fork.claim_session_fork_materialization(
        operation_id=operation_id,
        coordinator_token=coordinator_token,
        lease_seconds=(
            SHARED_PLUGIN_SESSION_FORK_TIMEOUT_S + 30.0
            if source_job.get("plugins")
            else SESSION_FORK_COORDINATOR_LEASE_S
        ),
    )
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "fork_in_progress",
                    "message": "This native Fork is already being materialized; retry shortly.",
                }
            },
            headers={"Retry-After": "2"},
        )
    residencies = await job_store.session_fork.establish_session_fork_residencies(
        operation_id=operation_id,
        coordinator_token=coordinator_token,
        ttl_seconds=residency_ttl_s(),
    )
    if residencies is None:
        await job_store.session_fork.release_session_fork_materialization(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "fork_fence_changed",
                    "message": "The source session changed before Fork could start.",
                }
            },
        )
    for role in ("source", "target"):
        previous_grant = residencies[role].get("previous_grant_id")
        if previous_grant and grants is not None:
            with contextlib.suppress(Exception):
                await grants.revoke(previous_grant)
    source_credential = await _mint_residency_grant(
        job_store.grants,
        grants,
        row=residencies["source"],
    )
    target_credential = await _mint_residency_grant(
        job_store.grants,
        grants,
        row=residencies["target"],
    )
    if not source_credential or not target_credential:
        await release_failed_fork_residencies(residencies)
        await job_store.session_fork.release_session_fork_materialization(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "residency_grant_unavailable",
                    "message": "Fork could not obtain two independent resident credentials.",
                }
            },
        )

    source_member["residency_generation"] = int(residencies["source"]["generation"])
    source_member["resident_state"] = "restarting"
    try:
        receipt = await sandbox_manager.fork_shared_session(
            source_thread_id,
            sandbox_id=str(operation["sandbox_id"]),
            generation=int(operation["sandbox_generation"]),
            origin_thread_id=str(operation["origin_thread_id"]),
            runtime_id=str(operation["runtime_id"]),
            runtime_version=operation.get("runtime_version"),
            sandbox_image_digest=operation.get("sandbox_image_digest"),
            expected_container_id=str(operation["container_id"]),
            resident_slot_capacity=int(operation["resident_slot_capacity"]),
            memory_limit=str(operation["memory_limit"]),
            cpu_limit=str(operation["cpu_limit"]),
            disk_limit_mib=int(operation.get("disk_limit_mib", 0)),
            stop_timeout_s=int(operation["stop_timeout_seconds"]),
            members=members,
            operation_id=operation_id,
            source_native_session_id=str(operation["source_native_session_id"]),
            source_native_turn_no=int(operation["source_native_turn_no"]),
            target_thread_id=str(operation["target_thread_id"]),
            target_slot_id=int(operation["target_resident_slot_id"]),
            source_launch={
                **launch_common,
                "native_session_id": str(operation["source_native_session_id"]),
                "credential": source_credential,
                "slot_id": int(operation["resident_slot_id"]),
                "residency_generation": int(residencies["source"]["generation"]),
            },
            target_launch={
                **launch_common,
                "native_session_id": None,
                "credential": target_credential,
                "slot_id": int(operation["target_resident_slot_id"]),
                "residency_generation": int(residencies["target"]["generation"]),
            },
        )
    except SandboxManagerError as exc:
        # PREPARING is intentionally retained: a retry with the same key
        # recovers the in-container journal or safely resumes before the
        # native mutation was reached.
        raise _sandbox_manager_http_error(exc) from exc
    materialized = await job_store.session_fork.record_session_fork_materialized(
        operation_id=operation_id,
        coordinator_token=coordinator_token,
        receipt=receipt,
    )
    if materialized is None:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "invalid_native_fork_receipt",
                    "message": "The sandbox host returned a mismatched native Fork receipt.",
                }
            },
        )
