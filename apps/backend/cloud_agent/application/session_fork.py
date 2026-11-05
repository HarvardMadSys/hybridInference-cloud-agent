"""A native Fork: the anchor's eligibility, prepare, materialize and finalize.

The handler resolves the owner's job and maps what happens here onto HTTP;
`fork_settled_turn` does the rest. It refuses an anchor that is not a
successfully settled turn of a shared-native sandbox, asks the store to
prepare the operation and answers the store's refusals, and drives a PREPARING
operation through `materialize_session_fork` — claim the coordinator lease,
establish both residencies, mint their credentials, ask the retaining host to
fork, and record the receipt — before the store finalizes the child. The
materialization was 186 lines in the middle of `fork_agent_job`, and the
eligibility rules sat above it there; a flow that can only be reached through
an HTTP request is a flow whose failure branches can only be tested through
one.

Nothing about the steps changed. The order, the transaction boundaries the
store owns, the error mapping — every branch still refuses with the same type
and message, now as ``Refused`` or ``Unavailable``, and the retaining host's
own failure comes back as ``ForkHostFailed`` for the handler to map — and the
worker-fenced cleanup a failed mint performs is the same nested helper, moved
with the block that calls it. `release_failed_fork_residencies` stays nested for exactly that
reason: as a module function it would need three closure variables as
parameters, and the call inside the block would stop being the same line.

The reaper reaches `finalize_session_fork` too, and does something different
with it — it collects ids instead of refusing, and releases residencies through
the sandbox manager rather than the store. §5.2 keeps that difference: only steps whose
semantics really match are shared, and these do not.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.application.model_connections import (
    access_for_job,
    connection_residency_credential,
    model_connection_cipher,
    unusable_error,
)
from cloud_agent.application.residency import residency_ttl_s
from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.domain.execution import SUCCEEDED, TERMINAL_STATES
from cloud_agent.domain.model_connections import ModelConnectionUnusable
from cloud_agent.logging_utils import get_logger
from cloud_agent.sandbox_manager.sandbox_manager_client import (
    SHARED_PLUGIN_SESSION_FORK_TIMEOUT_S,
    SHARED_SESSION_FORK_TIMEOUT_S,
    SandboxManagerError,
    identity_from_row,
)
from cloud_agent.sandbox_manager.sandbox_manager_routing import sandbox_manager_for_job
from cloud_agent_shared.environment import environment_payload
from cloud_agent_shared.sandbox_manager_wire import member_manifest

if TYPE_CHECKING:
    from cloud_agent.ports import (
        HostPoolRepository,
        ModelConnectionRepository,
        ResidencyRepository,
        SessionForkRepository,
    )
    from cloud_agent.ports.environments import EnvironmentBindingRepository


#: The fork logs under the aggregator's logger name, as the handler it left did.
logger = get_logger("cloud_agent.api.routes")


class _ForkMaterializationStorage(Protocol):
    """What materializing a fork reaches the store for."""

    host_pool: HostPoolRepository
    model_connections: ModelConnectionRepository
    residency: ResidencyRepository
    session_fork: SessionForkRepository

    @property
    def environments(self) -> EnvironmentBindingRepository:
        """Environment persistence needed by this flow."""
        ...


# Keep a short recovery margin past the host request's deadline so a second API
# worker cannot take over while the first is recording its receipt. Plugin
# installation needs a longer request and lease; the stalled-saga reconciler
# also respects that live lease.
SESSION_FORK_COORDINATOR_LEASE_S = SHARED_SESSION_FORK_TIMEOUT_S + 30.0


def _recorded_group(operation: dict[str, Any]) -> dict[str, Any]:
    """The group as this Fork recorded it, not whatever the group holds now.

    The operation's own ``generation`` counts the Fork's row; the group's is
    ``sandbox_generation``. Its container is required: the host reads a null
    expected container as no expectation, and would skip its check or create
    a container.
    """
    if not operation.get("container_id"):
        raise KeyError("container_id")
    return {**operation, "generation": operation["sandbox_generation"]}


class ForkHostFailed(Exception):
    """The retaining host failed the native fork request; ``failure`` is its answer.

    Its own exception so the handler maps it the way it maps every sandbox
    manager failure, without catching the ones raised before the fork request.
    """

    def __init__(self, failure: SandboxManagerError) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _credential_refused(exc: Exception) -> Refused | Unavailable:
    if isinstance(exc, ModelConnectionUnusable):
        return unusable_error(exc, kind="conflict")
    return Unavailable("model_connections_unconfigured", "Model connection keys cannot be read.")


async def _fork_source_job(
    job_store: _ForkMaterializationStorage, operation: dict[str, Any]
) -> dict[str, Any]:
    """The anchor turn, refused if it runs on a connection that can no longer serve it.

    Asked before any lease, residency or host call: the side chat would start
    two processes on that connection, and a Fork never moves a chat elsewhere.
    """
    source_job: dict[str, Any] = operation.get("source_job") or {}
    try:
        if not source_job.get("model_connection_id"):
            raise ModelConnectionUnusable("no_connection")
        await access_for_job(job_store.model_connections, source_job, model_connection_cipher())
    except (ModelConnectionUnusable, CredentialCipherError) as exc:
        raise _credential_refused(exc) from None
    return source_job


async def _fork_credentials(
    job_store: _ForkMaterializationStorage,
    source_job: dict[str, Any],
    residencies: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Each member's own launch fields: its credential and where it runs.

    Each residency is bound to the connection's current revision and handed
    its key, and each launch names the connection's exact endpoint, protocol
    and revision; on a translated plan each member also gets its own loopback
    token.
    """
    source_access = await connection_residency_credential(
        job_store, source_job, row=residencies["source"]
    )
    target_access = await connection_residency_credential(
        job_store, source_job, row=residencies["target"]
    )
    if source_access.revision != target_access.revision:
        raise ModelConnectionUnusable("changed")
    return (
        {"credential": source_access.api_key, **source_access.launch_fields()},
        {"credential": target_access.api_key, **target_access.launch_fields()},
    )


async def materialize_session_fork(
    operation: dict[str, Any],
    *,
    job_id: str,
    job_store: _ForkMaterializationStorage,
) -> None:
    """Drive a PREPARING fork operation to MATERIALIZED, or refuse it.

    Raises:
        Refused: an incomplete manifest, a fork already being materialized, a
            changed fence, or a connection that cannot serve it.
        Unavailable: an unreachable host, unreadable connection keys, or a
            receipt that does not match.
        ForkHostFailed: the retaining host's own failure of the fork request.
    """
    operation_id = str(operation["id"])

    async def release_failed_fork_residencies(rows: dict[str, Any]) -> None:
        for role in ("source", "target"):
            residency = rows[role]
            await job_store.residency.release_residency(
                thread_id=str(residency["thread_id"]),
                generation=int(residency["generation"]),
                host=str(operation["host"]),
            )

    sandbox_manager = await sandbox_manager_for_job(job_store.host_pool, job_id)
    if sandbox_manager is None:
        raise Unavailable(
            "sandbox_host_unavailable",
            "The host retaining this chat is not reachable for native Fork.",
        )
    # Validate every immutable host input before rotating either residency. A
    # malformed manifest is a control-plane defect, not a reason to start two
    # members in an uncallable saga.
    source_job = await _fork_source_job(job_store, operation)
    source_thread_id = str(operation["source_thread_id"])
    members = member_manifest(operation.get("group_members") or [])
    source_member = next(
        (member for member in members if member["thread_id"] == source_thread_id),
        None,
    )
    if source_member is None or operation.get("resident_slot_id") is None:
        raise Refused(
            "conflict", "native_context_unavailable", "The shared sandbox manifest is incomplete."
        )
    launch_common: dict[str, Any] = {
        "runtime_id": str(operation["runtime_id"]),
        "runtime_version": operation.get("runtime_version"),
        "model": str(source_job.get("model") or ""),
        "reasoning_effort": source_job.get("reasoning_effort"),
        "plugins": source_job.get("plugins") or [],
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
        raise Refused(
            "conflict",
            "fork_in_progress",
            "This native Fork is already being materialized; retry shortly.",
            retry_after_seconds=2,
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
        raise Refused(
            "conflict", "fork_fence_changed", "The source session changed before Fork could start."
        )
    try:
        source_fields, target_fields = await _fork_credentials(job_store, source_job, residencies)
    except (ModelConnectionUnusable, CredentialCipherError) as exc:
        await release_failed_fork_residencies(residencies)
        await job_store.session_fork.release_session_fork_materialization(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
        )
        raise _credential_refused(exc) from None

    source_member["residency_generation"] = int(residencies["source"]["generation"])
    source_member["resident_state"] = "restarting"
    try:
        receipt = await sandbox_manager.fork_shared_session(
            source_thread_id,
            identity=identity_from_row(_recorded_group(operation), members=members),
            operation_id=operation_id,
            source_native_session_id=str(operation["source_native_session_id"]),
            source_native_turn_no=int(operation["source_native_turn_no"]),
            target_thread_id=str(operation["target_thread_id"]),
            target_slot_id=int(operation["target_resident_slot_id"]),
            source_launch={
                **launch_common,
                "native_session_id": str(operation["source_native_session_id"]),
                **source_fields,
                "slot_id": int(operation["resident_slot_id"]),
                "residency_generation": int(residencies["source"]["generation"]),
            },
            target_launch={
                **launch_common,
                "native_session_id": None,
                **target_fields,
                "slot_id": int(operation["target_resident_slot_id"]),
                "residency_generation": int(residencies["target"]["generation"]),
            },
        )
    except SandboxManagerError as exc:
        # PREPARING is intentionally retained: a retry with the same key
        # recovers the in-container journal or safely resumes before the
        # native mutation was reached.
        raise ForkHostFailed(exc) from exc
    materialized = await job_store.session_fork.record_session_fork_materialized(
        operation_id=operation_id,
        coordinator_token=coordinator_token,
        receipt=receipt,
    )
    if materialized is None:
        raise Unavailable(
            "invalid_native_fork_receipt",
            "The sandbox host returned a mismatched native Fork receipt.",
        )


#: The store's refusals of ``prepare_session_fork``, in the owner's words: its
#: code, then the wire's ``error.type`` and message. Every one is a conflict.
_PREPARE_REFUSALS: dict[str, tuple[str, str]] = {
    "session_archived": (
        "session_archived",
        "This conversation is archived. Restore it before creating a side chat.",
    ),
    "idempotency_conflict": (
        "idempotency_conflict",
        "This Idempotency-Key was already used for a different fork source.",
    ),
    "not_settled": (
        "not_settled",
        "This turn is still active; stop the run before forking it.",
    ),
    "anchor_not_settled": (
        "historical_snapshot_unavailable",
        "Native Fork requires a successfully completed assistant turn.",
    ),
    "not_shared_live": (
        "native_fork_unavailable",
        "This chat does not use a shared-native sandbox.",
    ),
    "native_fork_unavailable": (
        "native_fork_unavailable",
        "This retained sandbox version cannot fork from a historical turn.",
    ),
    "sandbox_not_ready": (
        "native_context_unavailable",
        "This chat's retained native context is not safe to fork.",
    ),
    "host_removed": (
        "native_context_unavailable",
        "This chat's sandbox is on a server that has been removed.",
    ),
    "source_busy": (
        "sandbox_busy",
        "This chat is currently running a turn; retry after it settles.",
    ),
    "fork_in_progress": (
        "fork_in_progress",
        "Another native fork is already being prepared from this chat.",
    ),
    "resident_capacity_exhausted": (
        "resident_capacity_exhausted",
        "This shared sandbox has no free resident Agent CLI slot.",
    ),
    "native_context_unavailable": (
        "native_context_unavailable",
        "This chat has no settled native session to fork.",
    ),
    "runtime_mismatch": (
        "native_fork_unavailable",
        "The retained runtime no longer matches this chat.",
    ),
}

_PREPARE_REFUSAL_FALLBACK = (
    "native_fork_unavailable",
    "The retained native session cannot be forked now.",
)


def refuse_ineligible_anchor(job: dict[str, Any]) -> None:
    """Refuse a Fork anchor that is not a settled success in a shared-native sandbox.

    Asked of the owner's job as read, before the store prepares anything; the
    store asks its own version of each question again under its locks.

    Raises:
        Refused: a conflict naming the first rule the anchor fails.
    """
    if job["state"] not in TERMINAL_STATES:
        raise Refused(
            "conflict",
            "not_settled",
            "This turn is still active; wait for it to settle before forking it.",
        )
    if job["state"] != SUCCEEDED:
        raise Refused(
            "conflict",
            "anchor_not_settled",
            "Only a successfully settled turn can be used as a Fork anchor.",
        )
    if job.get("thread_sandbox") is None:
        raise Refused(
            "conflict",
            "native_context_unavailable",
            "This historical chat has no shared native-session sandbox.",
        )
    sandbox = job.get("thread_sandbox") or {}
    if sandbox.get("shared_live") is not True:
        raise Refused(
            "conflict",
            "native_fork_unavailable",
            (
                "This historical retained chat predates shared native-session "
                "forks. Start a new chat to use same-workspace Fork."
            ),
        )


async def _prepared_operation(
    job_store: _ForkMaterializationStorage,
    *,
    job_id: str,
    user_id: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """The store's operation for this Fork, or its refusal in the owner's words."""
    operation = await job_store.session_fork.prepare_session_fork(
        source_job_id=job_id,
        user_id=user_id,
        idempotency_key=idempotency_key or f"generated:{secrets.token_urlsafe(24)}",
    )
    if operation is None:
        raise Refused("not_found", "not_found", f"No such agent job: {job_id}")
    error = operation.get("error")
    if error:
        error_type, message = _PREPARE_REFUSALS.get(str(error), _PREPARE_REFUSAL_FALLBACK)
        raise Refused("conflict", error_type, message)
    if operation["state"] == "failed":
        raise Refused(
            "conflict",
            "native_fork_failed",
            str(operation.get("last_error") or "Native Fork could not complete."),
        )
    return operation


async def fork_settled_turn(
    job_store: _ForkMaterializationStorage,
    job: dict[str, Any],
    *,
    job_id: str,
    user_id: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Fork the owner's settled turn ``job`` (``job_id``) into the same shared workspace.

    Returns the child job as the store finalized it. A retry with the same
    idempotency key returns the same child.

    Raises:
        Refused: an ineligible anchor, a store refusal, a failed operation,
            a refusal while materializing, or a fence that changed before
            finalization.
        Unavailable: as `materialize_session_fork`.
        ForkHostFailed: the retaining host's own failure of the fork request.
    """
    refuse_ineligible_anchor(job)
    operation = await _prepared_operation(
        job_store, job_id=job_id, user_id=user_id, idempotency_key=idempotency_key
    )
    operation_id = str(operation["id"])
    if operation["state"] == "preparing":
        # Resolve the host only for the state that still needs a native
        # mutation.  READY and MATERIALIZED are durable control-plane states:
        # a retry after a lost HTTP response must return/finalize the same
        # child even when the retaining host has since become unreachable.
        await materialize_session_fork(operation, job_id=job_id, job_store=job_store)

    forked = await job_store.session_fork.finalize_session_fork(operation_id=operation_id)
    if forked is None:
        raise Refused(
            "conflict",
            "native_fork_fence_changed",
            "The source chat changed before native Fork finalization.",
        )
    logger.info(
        "agent_job_forked",
        extra={
            "event": "agent_job_forked",
            "job_id": forked["id"],
            "thread_id": forked.get("thread_id"),
            "source_job_id": job_id,
            "sandbox_clone": False,
            "workspace_shared": True,
            "fork_operation_id": operation_id,
        },
    )
    return forked
