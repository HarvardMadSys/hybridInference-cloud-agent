"""Deployment agents from the control plane's side: enrollment, intents, rollouts.

The control plane decides and records; it executes nothing on a machine. An
administrator's request becomes an *intent* for one agent, validated here
against the shared contract and the policy that agent last reported, and the
agent validates it again before it runs. Multi-step operations (a release
rollout, a key rotation, a database switch) are *workflows*: each step is one
or more intents, and :func:`advance_workflow` moves a workflow on only when
the evidence for its current step is in.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.deployment_contract import (
    OPERATIONS,
    ContractError,
    check_operation,
    decode_b64,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloud_agent.ports import DeploymentAgentRepository

ENROLL_TOKEN_TTL = timedelta(hours=1)
INTENT_TTL = timedelta(hours=1)
#: An agent that has not reported for this long is shown offline and gets no new intents.
STALE_AGENT_AFTER = timedelta(minutes=2)
_TOKEN_PREFIX = "cde_"
_CREDENTIAL_PREFIX = "cad_"


class DeploymentRefused(ValueError):
    """An administrator's request this control plane will not turn into an intent."""


def digest(secret: str) -> str:
    """The SHA-256 stored in place of a token or credential."""
    return hashlib.sha256(secret.encode()).hexdigest()


def new_enroll_token() -> str:
    """A one-time agent enrollment token."""
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_agent_credential(agent_id: str) -> str:
    """An agent credential; the id prefix lets an operator tell agents apart in a log."""
    return f"{_CREDENTIAL_PREFIX}{agent_id}_{secrets.token_urlsafe(32)}"


def is_agent_credential(value: str) -> bool:
    """Whether a bearer value has the agent credential shape (never an admin credential)."""
    return value.startswith(_CREDENTIAL_PREFIX)


def online(agent: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether an agent reported recently and is not revoked."""
    seen = agent.get("last_report_at")
    if agent.get("revoked_at") is not None or seen is None:
        return False
    return (now or datetime.now(UTC)) - seen <= STALE_AGENT_AFTER


async def issue_enroll_token(repo: DeploymentAgentRepository, *, role: str, admin_id: str) -> str:
    """Create a token that enrolls one agent of ``role`` within the hour."""
    if role not in {"control-plane", "host"}:
        raise DeploymentRefused("an agent is a control-plane or a host agent")
    token = new_enroll_token()
    await repo.create_enroll_token(
        token_hash=digest(token),
        role=role,
        created_by=admin_id,
        expires_at=datetime.now(UTC) + ENROLL_TOKEN_TTL,
    )
    return token


async def register_agent(
    repo: DeploymentAgentRepository,
    *,
    token: str,
    name: str,
    role: str,
    machine: str,
    public_key: str,
) -> tuple[dict[str, Any], str]:
    """Spend an enrollment token; returns the agent and its credential (shown once)."""
    try:
        point = decode_b64(public_key)
    except ValueError as exc:
        raise DeploymentRefused("the public key is not base64") from exc
    if len(point) != 65 or point[0] != 4:
        raise DeploymentRefused("the public key is not an uncompressed P-256 point")
    agent_id = uuid.uuid4().hex
    credential = new_agent_credential(agent_id)
    try:
        agent = await repo.register_agent(
            token_hash=digest(token),
            agent_id=agent_id,
            name=name,
            role=role,
            machine=machine,
            credential_hash=digest(credential),
            public_key=public_key,
        )
    except ValueError as exc:
        raise DeploymentRefused(str(exc)) from exc
    if agent is None:
        raise DeploymentRefused(
            "the enrollment token is unknown, spent, expired or for another role"
        )
    return agent, credential


def _check_against_report(agent: dict[str, Any], operation: str, params: dict[str, Any]) -> None:
    """What the agent last reported must make the operation possible at all."""
    report = agent.get("report") or {}
    if operation.startswith("release_") and params["manifest_sha256"] not in (
        report.get("releases") or {}
    ):
        raise DeploymentRefused("this machine has not imported and verified that release")
    if operation == "rollback_config" and params["to_rev"] not in (
        report.get("config_versions") or []
    ):
        raise DeploymentRefused("this machine does not hold that configuration version")
    if operation == "kata_install" and not (report.get("policy") or {}).get("has_kata_helper"):
        raise DeploymentRefused("this machine's policy names no Kata helper")


async def create_intent(
    repo: DeploymentAgentRepository,
    *,
    agent_id: str,
    operation: str,
    params: dict[str, Any],
    sealed: dict[str, Any],
    idempotency_key: str,
    confirm: str | None,
    admin_id: str,
    workflow_id: str | None = None,
    host_status: Callable[[str], Awaitable[str | None]] | None = None,
) -> dict[str, Any]:
    """Validate an administrator's request and store it as an intent for one agent."""
    agent = await repo.get_agent(agent_id)
    if agent is None or agent.get("revoked_at") is not None:
        raise DeploymentRefused("no such deployment agent")
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise DeploymentRefused("unknown operation")
    if spec.confirm and confirm != operation:
        raise DeploymentRefused(f"confirm this operation by repeating its name: {operation}")
    policy = (agent.get("report") or {}).get("policy")
    try:
        check_operation(operation, agent["role"], params, sealed, policy=policy)
    except ContractError as exc:
        raise DeploymentRefused(str(exc)) from exc
    _check_against_report(agent, operation, params)
    if operation == "kata_install":
        runner_host = str((agent.get("report") or {}).get("runner_host") or "")
        status = await host_status(runner_host) if host_status and runner_host else None
        if status != "draining":
            raise DeploymentRefused("drain this server on the Servers page before installing Kata")
    return await repo.create_intent(
        agent_id=agent_id,
        intent_id=uuid.uuid4().hex,
        operation=operation,
        params=params,
        sealed=sealed,
        idempotency_key=idempotency_key,
        created_by=admin_id,
        expires_at=datetime.now(UTC) + INTENT_TTL,
        workflow_id=workflow_id,
    )


def agent_view(agent: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """An agent as the Admin page shows it: identity, evidence, no credential."""
    report = agent.get("report") or {}
    return {
        "agent_id": agent["agent_id"],
        "name": agent["name"],
        "role": agent["role"],
        "machine": agent["machine"],
        "public_key": agent["public_key"],
        "enrolled_at": agent.get("enrolled_at"),
        "last_report_at": agent.get("last_report_at"),
        "revoked": agent.get("revoked_at") is not None,
        "online": online(agent, now),
        "observed": {k: v for k, v in report.items() if k != "operations"},
        "local_operations": [
            op for op in report.get("operations") or [] if op.get("origin") == "local"
        ][-20:],
    }


__all__ = [
    "STALE_AGENT_AFTER",
    "DeploymentRefused",
    "agent_view",
    "create_intent",
    "digest",
    "is_agent_credential",
    "issue_enroll_token",
    "online",
    "register_agent",
]
