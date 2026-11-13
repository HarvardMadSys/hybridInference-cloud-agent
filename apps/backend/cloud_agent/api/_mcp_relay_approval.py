"""Decide one MCP relay call, and hold it for its owner's approval when it needs one.

Split from :mod:`cloud_agent.api.mcp_relay`. Tools, resources and prompts all
pass through :func:`decide_call`, the single decision point on the relay's
surface. A call that needs approval is recorded, kept open while its owner
decides, and dispatched only by the caller that claims the approval, under the
fence the approval was given for.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent.api._mcp_relay_callers import residency_fence, resolve_live_binding
from cloud_agent.api.deps import get_mcp_decision_signal
from cloud_agent.crypto.credential_cipher import CredentialCipherError
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.authorization import decide
from cloud_agent.mcp.host import arguments_digest, encode_arguments
from cloud_agent.mcp.invocations import MAX_HELD_ARGUMENTS_BYTES, deadline_for
from cloud_agent.mcp.jsonrpc import INVALID_PARAMS
from cloud_agent.mcp.models import CapabilityKind
from cloud_agent.mcp.registry import invocation_cipher
from cloud_agent.mcp.relay_surface import error_reply

if TYPE_CHECKING:
    from fastapi import Request

    from cloud_agent.api._mcp_relay_callers import Authorized
    from cloud_agent.api.mcp_relay import McpRelayStorage
    from cloud_agent.workers.mcp_decision_signal import McpDecisionSignal

# The relay's own logger: these events kept their name when the code moved.
logger = get_logger("cloud_agent.api.mcp_relay")


#: A held call waits for its owner's decision, which wakes it, and looks again
#: at least this often anyway: its caller leaving, its attempt being superseded
#: and its connection changing announce nothing.
APPROVAL_IDLE_S = 5.0


#: How often it looks where nothing would wake it: no listener in this process,
#: or the listening connection lost.
APPROVAL_POLL_S = 1.0


async def decide_call(
    authorized: Authorized,
    store: McpRelayStorage,
    *,
    name: str,
    arguments: Any,
    capability: Any,
    attempt_id: int | None,
    lease_generation: int,
    request_id: Any,
    kind: CapabilityKind = CapabilityKind.TOOL,
    request: Request | None = None,
) -> dict[str, Any] | None:
    """Decide one call, or return ``None`` when it may be dispatched.

    The single decision point on this surface: tools, resources and prompts all
    call it, because two copies of an authorization rule is one copy that will
    be forgotten, and the copy that gets forgotten is always the newer one.
    ``kind`` selects the scope: a resource read is checked against the resource
    rules, which is what stops it being a way to reach something the session
    did not enable.
    """
    decision = decide(
        connection=authorized.connection,
        binding=authorized.binding,
        capability=capability,
        kind=kind,
        name=name,
        user_id=authorized.user_id,
    )
    if decision.allowed:
        return None
    if decision.refused:
        logger.warning(
            "agent_mcp_tool_refused",
            extra={
                "event": "agent_mcp_tool_refused",
                "job_id": authorized.job_id,
                "alias": authorized.alias,
                "tool": name,
                "code": decision.code,
            },
        )
        return error_reply(request_id, decision.message or "this tool is not enabled")

    digest = arguments_digest(arguments if isinstance(arguments, dict) else None)
    # What the approval would have to have been given for. Every one of these
    # moves when something the owner agreed to changes: the target, the
    # catalogue, the session's own rules, or whose job this is.
    fence = {
        "config_revision": authorized.connection.config_revision,
        "authorization_epoch": authorized.connection.authorization_epoch,
        "capability_revision": authorized.connection.capability_revision,
        "policy_revision": authorized.binding.policy_revision,
        "attempt_id": attempt_id,
        "lease_generation": lease_generation or None,
        **residency_fence({"residency_generation": authorized.residency_generation}),
    }
    approved = await store.mcp.find_approved_mcp_invocation(
        job_id=authorized.job_id,
        connection_id=authorized.connection_id,
        capability_name=name,
        arguments_digest=digest,
        capability_kind=kind.value,
        **fence,
    )
    if approved is not None:
        claimed = await store.mcp.claim_mcp_invocation(invocation_id=int(approved["id"]), **fence)
        if claimed is not None:
            # Exactly one dispatcher ever wins this UPDATE, which is why a
            # retry storm cannot send a write twice.
            authorized.dispatches.append((int(claimed["id"]), int(claimed["dispatch_generation"])))
            return None
    if not held_arguments_fit(arguments):
        logger.warning(
            "agent_mcp_call_too_large_to_hold",
            extra={
                "event": "agent_mcp_call_too_large_to_hold",
                "job_id": authorized.job_id,
                "alias": authorized.alias,
                "tool": name,
            },
        )
        return error_reply(
            request_id,
            f"This call needs its owner's approval, and its arguments are larger than the "
            f"{MAX_HELD_ARGUMENTS_BYTES} bytes a held call can keep for review. "
            "It was not held or sent.",
            INVALID_PARAMS,
        )
    invocation = await store.mcp.create_mcp_invocation(
        job_id=authorized.job_id,
        attempt_id=attempt_id,
        lease_generation=lease_generation,
        thread_id=authorized.thread_id,
        connection_id=authorized.connection_id,
        capability_name=name,
        capability_kind=kind.value,
        arguments_digest=digest,
        arguments_ciphertext=protect_arguments(arguments),
        config_revision=authorized.connection.config_revision,
        authorization_epoch=authorized.connection.authorization_epoch,
        capability_revision=authorized.connection.capability_revision,
        policy_revision=authorized.binding.policy_revision,
        expires_at=deadline_for(created_at=datetime.now(UTC)).expires_at,
        **residency_fence({"residency_generation": authorized.residency_generation}),
    )
    logger.info(
        "agent_mcp_call_held",
        extra={
            "event": "agent_mcp_call_held",
            "job_id": authorized.job_id,
            "alias": authorized.alias,
            "tool": name,
            "invocation_id": invocation["id"],
        },
    )
    return await wait_for_approval(
        authorized,
        store,
        invocation=invocation,
        fence=fence,
        request_id=request_id,
        request=request,
    )


async def wait_for_approval(  # noqa: PLR0912 complexity baseline, #771
    authorized: Authorized,
    store: McpRelayStorage,
    *,
    invocation: dict[str, Any],
    fence: dict[str, Any],
    request_id: Any,
    request: Request | None,
) -> dict[str, Any] | None:
    """Keep the original CLI call open and dispatch it once its owner approves.

    Waiting owns no transaction. The normal attempt heartbeat continues, and
    each look checks whether the attempt or selected connection changed. A
    disconnected caller leaves no reusable approval behind. The owner's
    decision wakes the wait; otherwise it looks again on a clock.
    """
    invocation_id = int(invocation["id"])
    message = (
        "MCP session authorization changed while waiting for approval; this call was not sent."
    )
    signal = get_mcp_decision_signal(request) if request is not None else None
    subscription = (
        signal.subscribe(str(invocation_id)) if signal is not None else contextlib.nullcontext()
    )
    with subscription as woken:
        try:
            while True:
                if request is not None and await request.is_disconnected():
                    raise asyncio.CancelledError
                if datetime.now(UTC) >= invocation["expires_at"]:
                    message = (
                        "MCP approval expired while waiting for the owner's decision; "
                        "this call was not sent to the server. Ask the owner to review "
                        "Approvals or this conversation's Tools permissions before requesting it again."
                    )
                    break
                attempt_id = fence["attempt_id"]
                if attempt_id is None:
                    break
                identity = await store.credential_queries.resolve_model_credential(
                    job_id=authorized.job_id,
                    attempt_id=attempt_id,
                    lease_generation=int(fence["lease_generation"] or 0),
                    **residency_fence(fence),
                )
                if identity is None:
                    break
                identity = {**identity, **residency_fence(fence)}
                try:
                    current = await resolve_live_binding(
                        identity,
                        authorized.alias,
                        store,
                        attempt_id=attempt_id,
                        lease_generation=int(fence["lease_generation"] or 0),
                    )
                except HTTPException:
                    break
                if (
                    not current.binding.enabled
                    or current.connection_id != authorized.connection_id
                    or current.connection.config_revision != fence["config_revision"]
                    or current.connection.authorization_epoch != fence["authorization_epoch"]
                    or current.connection.capability_revision != fence["capability_revision"]
                    or current.binding.policy_revision != fence["policy_revision"]
                ):
                    break
                row = await store.mcp.get_mcp_invocation(invocation_id=invocation_id)
                if row is None:
                    break
                if row["status"] == "approved":
                    claimed = await store.mcp.claim_mcp_invocation(
                        invocation_id=invocation_id, **fence
                    )
                    if claimed is not None:
                        # OAuth may have refreshed while the user was deciding.
                        # Use the checked connection's current credential for dispatch.
                        authorized.connection = current.connection
                        authorized.secret = current.secret
                        authorized.dispatches.append(
                            (invocation_id, int(claimed["dispatch_generation"]))
                        )
                        return None
                    message = (
                        "MCP approval could not be claimed; another caller may already have sent it. "
                        "Check the external state before retrying a write."
                    )
                    break
                if row["status"] != "pending":
                    if row["status"] == "expired":
                        return error_reply(
                            request_id,
                            "MCP approval expired before dispatch; this call was not sent to the server. "
                            "Ask the owner to review Approvals or this conversation's Tools permissions "
                            "before requesting it again.",
                        )
                    if row["status"] in {"dispatching", "completed", "failed", "outcome_unknown"}:
                        return error_reply(
                            request_id,
                            f"MCP call {row['status']}; its approval was already claimed for dispatch. "
                            "This caller cannot confirm the result. Check the external state "
                            "before retrying a write.",
                        )
                    return error_reply(
                        request_id, f"MCP call {row['status']}; it was not dispatched."
                    )
                await until_decided(signal, woken, expires_at=invocation["expires_at"])
        except asyncio.CancelledError:
            await asyncio.shield(store.mcp.expire_mcp_invocation(invocation_id=invocation_id))
            raise
    await store.mcp.expire_mcp_invocation(invocation_id=invocation_id)
    return error_reply(request_id, message)


async def until_decided(
    signal: McpDecisionSignal | None, woken: asyncio.Event | None, *, expires_at: datetime
) -> None:
    """Wait for the held call's decision, its expiry, or the next look anyway."""
    look = APPROVAL_IDLE_S if signal is not None and signal.listening else APPROVAL_POLL_S
    remaining = (expires_at - datetime.now(UTC)).total_seconds()
    timeout_s = max(0.0, min(look, remaining))
    if signal is None or woken is None:
        await asyncio.sleep(timeout_s)
    else:
        await signal.wait(woken, timeout_s)


async def finish_dispatches(authorized: Authorized, store: McpRelayStorage, *, status: str) -> None:
    """Record an observed outcome without retrying a possible external write."""
    for invocation_id, generation in authorized.dispatches:
        await store.mcp.finish_mcp_invocation(
            invocation_id=invocation_id,
            dispatch_generation=generation,
            status=status,
        )
    authorized.dispatches.clear()


def held_arguments_fit(arguments: Any) -> bool:
    """Whether a held call's arguments fit under :data:`MAX_HELD_ARGUMENTS_BYTES` as stored."""
    if not isinstance(arguments, dict):
        return True
    return len(encode_arguments(arguments)) <= MAX_HELD_ARGUMENTS_BYTES


def protect_arguments(arguments: Any) -> str | None:
    """Store a held call's arguments under the invocation cipher."""
    if not isinstance(arguments, dict):
        return None
    from cloud_agent.mcp.host import encode_arguments

    try:
        return invocation_cipher().encrypt(encode_arguments(arguments))
    except CredentialCipherError:
        # Without a key the platform cannot hold the body. The call is still
        # held -- the owner can approve it by name -- but the arguments will not
        # be shown, which is worse than useless for a write, so this is logged.
        logger.error("agent_mcp_invocation_arguments_unprotectable")
        return None
