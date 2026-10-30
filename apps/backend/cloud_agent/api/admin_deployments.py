"""Administrators' deployment management: agents, intents, rollouts and workflows.

Every route needs administrator access, and creating anything needs
deployment authority on top (:func:`require_deployment_authority`). Nothing
here returns a credential except the enrollment token, once, to the
administrator who issued it; sealed values are accepted and relayed, never
read.
"""

from __future__ import annotations

import contextlib
from typing import Any

from asyncpg.exceptions import ReadOnlySQLTransactionError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent import maintenance
from cloud_agent.api.deps import (
    get_client_ip,
    get_storage,
    log_admin_action,
    require_store,
    verify_admin_access,
)
from cloud_agent.application import deployments, workflows
from cloud_agent.application.key_rotation import register_advancers
from cloud_agent_shared.deployment_contract import CONFIG_KEYS_BY_ROLE, OPERATIONS, SERVICES


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


register_advancers()

router = APIRouter(
    prefix="/admin/agent/deployments",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


def require_deployment_authority(request: Request, admin_id: str) -> None:
    """Deployment changes need ``ADMIN_TOKEN``, a Gateway administrator, or a scoped credential.

    ``ADMIN_TOKEN`` and a Gateway administrator session are full administrators
    today and keep that meaning. A daily administrator credential acts here only
    if it was created with the ``deployment`` scope.
    """
    if admin_id == "admin-token" or admin_id.startswith("admin:"):
        return
    scopes: frozenset[str] = getattr(request.state, "admin_credential_scopes", frozenset())
    if admin_id.startswith("admin-credential:") and "deployment" in scopes:
        return
    raise HTTPException(status_code=403, detail="This credential has no deployment authority.")


class EnrollTokenRequest(BaseModel):
    """Which kind of machine the token enrolls."""

    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern=r"^(control-plane|host)$")


class IntentRequest(BaseModel):
    """One operation for one agent."""

    model_config = ConfigDict(extra="forbid")
    operation: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    sealed: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)
    confirm: str | None = Field(default=None, max_length=64)


class RolloutRequest(BaseModel):
    """Move every machine to one verified release."""

    model_config = ConfigDict(extra="forbid")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm: str = Field(max_length=64)


class ConfirmRequest(BaseModel):
    """A repeated name confirming an irreversible action."""

    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(max_length=128)


#: Workflow data never shown: sealed values, secrets kept encrypted at rest, probes.
_UNSHOWN_PROGRESS = frozenset(
    {"sealed_by_agent", "next_token", "next_secret", "next_key", "probe_token", "tunnel_keys"}
)


def _workflow_view(workflow: dict[str, Any]) -> dict[str, Any]:
    """A workflow for the page: its counters and any question, never a sealed value."""
    return {
        **{k: v for k, v in workflow.items() if k != "data"},
        "progress": {k: v for k, v in workflow["data"].items() if k not in _UNSHOWN_PROGRESS},
    }


def _intent_view(intent: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in intent.items() if k != "sealed"}


@router.get("")
async def overview(store: Any = Depends(get_storage)) -> dict[str, Any]:
    """Every agent with its evidence, recent intents and workflows, and the contract."""
    storage = require_store(store)
    agents = await storage.deployments.list_agents()
    return {
        "maintenance": {**maintenance.current(), "progress": maintenance.progress()},
        "agents": [deployments.agent_view(agent) for agent in agents],
        "intents": [_intent_view(i) for i in await storage.deployments.list_intents(limit=100)],
        "workflows": [
            _workflow_view(workflow)
            for workflow in await storage.deployments.list_workflows(limit=30)
        ],
        "contract": {
            "operations": {
                name: {
                    "roles": list(op.roles),
                    "confirm": op.confirm,
                    "sealed": op.sealed,
                    "params": list(op.params),
                    "description": op.description,
                }
                for name, op in OPERATIONS.items()
            },
            "config_keys": {
                role: {
                    name: {
                        "kind": key.kind,
                        "item": key.item,
                        "services": list(key.services),
                        "choices": list(key.choices),
                        "boundary": key.boundary,
                    }
                    for name, key in keys.items()
                }
                for role, keys in CONFIG_KEYS_BY_ROLE.items()
            },
            "services": {role: list(names) for role, names in SERVICES.items()},
        },
    }


@router.post("/enroll-token")
async def enroll_token(
    body: EnrollTokenRequest,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """A one-time token that enrolls one agent within the hour; shown once."""
    require_deployment_authority(request, admin_id)
    storage = require_store(store)
    token = await deployments.issue_enroll_token(
        storage.deployments, role=body.role, admin_id=admin_id
    )
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.deployment.enroll_token",
        details={"role": body.role},
        actor_id=admin_id,
    )
    return {"token": token, "expires_in_seconds": int(deployments.ENROLL_TOKEN_TTL.total_seconds())}


@router.post("/agents/{agent_id}/intents")
async def create_intent(
    agent_id: str,
    body: IntentRequest,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Validate and queue one operation for one agent."""
    require_deployment_authority(request, admin_id)
    storage = require_store(store)

    async def host_status(host: str) -> str | None:
        hosts = await storage.host_pool.list_runner_hosts()
        return next((h.get("status") for h in hosts if h.get("host") == host), None)

    try:
        intent = await deployments.create_intent(
            storage.deployments,
            agent_id=agent_id,
            operation=body.operation,
            params=body.params,
            sealed=body.sealed,
            idempotency_key=body.idempotency_key,
            confirm=body.confirm,
            admin_id=admin_id,
            host_status=host_status,
        )
    except deployments.DeploymentRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.deployment.intent",
        details={
            "agent_id": agent_id,
            "operation": body.operation,
            "intent_id": intent["intent_id"],
            "changed_keys": sorted((body.params.get("changes") or {}).keys())
            if body.operation == "apply_config"
            else None,
            "sealed_keys": sorted(body.sealed),
        },
        actor_id=admin_id,
    )
    return _intent_view(intent)


@router.post("/intents/{intent_id}/cancel")
async def cancel_intent(
    intent_id: str,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Cancel an intent the agent has not fetched yet."""
    require_deployment_authority(request, admin_id)
    storage = require_store(store)
    cancelled = await storage.deployments.cancel_intent(intent_id)
    if not cancelled:
        raise HTTPException(
            status_code=409, detail="Only an intent the agent has not fetched can be cancelled."
        )
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.deployment.cancel",
        details={"intent_id": intent_id},
        actor_id=admin_id,
    )
    return {"cancelled": True}


@router.post("/agents/{agent_id}/revoke")
async def revoke_agent(
    agent_id: str,
    body: ConfirmRequest,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Revoke an agent's credential; it can no longer report or fetch."""
    require_deployment_authority(request, admin_id)
    storage = require_store(store)
    agent = await storage.deployments.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="No such agent.")
    if body.confirm != agent["name"]:
        raise HTTPException(status_code=422, detail="Confirm by typing the agent's name.")
    await storage.deployments.revoke_agent(agent_id)
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.deployment.revoke",
        details={"agent_id": agent_id},
        actor_id=admin_id,
    )
    return {"revoked": True}


@router.post("/rollouts")
async def start_rollout(
    body: RolloutRequest,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Prepare everywhere, migrate, activate hosts, then the control plane."""
    require_deployment_authority(request, admin_id)
    if body.confirm != "release_activate":
        raise HTTPException(status_code=422, detail="Confirm by repeating: release_activate")
    storage = require_store(store)
    try:
        workflow = await workflows.start_rollout(
            storage, manifest_sha256=body.manifest_sha256, admin_id=admin_id
        )
    except (workflows.WorkflowRefused, deployments.DeploymentRefused) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.deployment.rollout",
        details={"manifest_sha256": body.manifest_sha256, "workflow_id": workflow["workflow_id"]},
        actor_id=admin_id,
    )
    return workflow


@router.get("/workflows/{workflow_id}")
async def read_workflow(workflow_id: str, store: Any = Depends(get_storage)) -> dict[str, Any]:
    """One workflow and its intents."""
    storage = require_store(store)
    workflow = await storage.deployments.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="No such workflow.")
    with contextlib.suppress(ReadOnlySQLTransactionError):
        # A fenced database still shows where the workflow stands.
        await workflows.advance_workflow(storage, workflow)
    return {
        **_workflow_view(workflow),
        "intents": [
            _intent_view(i)
            for i in await storage.deployments.list_intents(workflow_id=workflow_id, limit=500)
        ],
    }


class WorkflowRequest(BaseModel):
    """A key rotation or database workflow."""

    model_config = ConfigDict(extra="forbid")
    kind: str = Field(
        pattern=r"^(admin_token|root_key|sandbox_manager_token|control_signing|credential_key|tunnel_keys|database_connection|database_migration)$"
    )
    params: dict[str, Any] = Field(default_factory=dict)
    #: Each agent's sealed values, by agent id: only that agent can open them.
    sealed_by_agent: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: A new secret the control plane itself must hold (the sandbox manager
    #: credential or the signing key), kept encrypted at rest until used.
    secret: str | None = Field(default=None, max_length=4096)
    confirm: str = Field(max_length=64)


@router.post("/workflows")
async def start_workflow(
    body: WorkflowRequest,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Start a rotation or database workflow (ADMIN_TOKEN or a signed-in administrator only)."""
    from cloud_agent.api.admin_credentials import require_full_authority
    from cloud_agent.application.credential_key_rotation import start_credential_key_rotation
    from cloud_agent.application.database_workflows import start_database_workflow
    from cloud_agent.application.key_rotation import start_rotation
    from cloud_agent.application.tunnel_key_rotation import start_tunnel_rotation

    require_full_authority(admin_id)
    if body.confirm != body.kind:
        raise HTTPException(status_code=422, detail=f"Confirm by repeating: {body.kind}")
    storage = require_store(store)
    try:
        if body.kind.startswith("database_"):
            workflow = await start_database_workflow(
                storage,
                kind=body.kind,
                params=body.params,
                sealed_by_agent=body.sealed_by_agent,
                admin_id=admin_id,
            )
        elif body.kind == "tunnel_keys":
            workflow = await start_tunnel_rotation(
                storage, hosts=body.params.get("hosts"), admin_id=admin_id
            )
        elif body.kind == "credential_key":
            workflow = await start_credential_key_rotation(
                storage, key=str(body.params.get("key")), secret=body.secret, admin_id=admin_id
            )
        else:
            workflow = await start_rotation(
                storage,
                kind=body.kind,
                params=body.params,
                sealed_by_agent=body.sealed_by_agent,
                secret=body.secret,
                admin_id=admin_id,
            )
    except (workflows.WorkflowRefused, deployments.DeploymentRefused) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_admin_action(
        storage,
        get_client_ip(request),
        f"agent.deployment.workflow.{body.kind}",
        details={"workflow_id": workflow["workflow_id"], "agents": sorted(body.sealed_by_agent)},
        actor_id=admin_id,
    )
    return {k: v for k, v in workflow.items() if k != "data"}


class FarEndConfirmation(BaseModel):
    """An administrator changed a tunnel's far end as the workflow asked."""

    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=3, max_length=256)


@router.post("/workflows/{workflow_id}/confirm-far-end")
async def confirm_tunnel_far_end(
    workflow_id: str,
    body: FarEndConfirmation,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Tell a tunnel key rotation the requested far-end change is made."""
    from cloud_agent.api.admin_credentials import require_full_authority
    from cloud_agent.application.tunnel_key_rotation import confirm_far_end

    require_full_authority(admin_id)
    storage = require_store(store)
    try:
        workflow = await confirm_far_end(storage, workflow_id, body.token)
    except workflows.WorkflowRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.deployment.workflow.tunnel_keys.confirm",
        details={"workflow_id": workflow_id, "token": body.token},
        actor_id=admin_id,
    )
    return _workflow_view(workflow)


@router.post("/workflows/{workflow_id}/retry")
async def retry_workflow(
    workflow_id: str,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Continue a stopped rotation from the step that stopped."""
    from cloud_agent.api.admin_credentials import require_full_authority

    require_full_authority(admin_id)
    storage = require_store(store)
    try:
        workflow = await workflows.retry_workflow(storage, workflow_id)
    except workflows.WorkflowRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.deployment.workflow.retry",
        details={"workflow_id": workflow_id, "kind": workflow["kind"]},
        actor_id=admin_id,
    )
    return _workflow_view(workflow)


@router.get("/host-credentials")
async def host_credentials(store: Any = Depends(get_storage)) -> dict[str, Any]:
    """Every enrolled server's credential: which signing key minted it, whether a renewal waits."""
    from cloud_agent.application.signing_key_rotation import host_lag
    from cloud_agent.tokens import signing_key_id

    storage = require_store(store)
    key_id = signing_key_id()
    return {
        "signing_key_id": key_id,
        "hosts": [
            {
                "host": row["host"],
                "status": row["status"],
                "current_key": bool(key_id) and row["credential_key_id"] == key_id,
                "key_known": row["credential_key_id"] is not None,
                "renews": bool(row["renews"]),
                "renewal_pending": bool(row["renewal_pending"]),
                "renewal_requested": bool(row["renewal_requested"]),
                "last_heartbeat_at": row["last_heartbeat_at"],
                "lag": None if row["credential_key_id"] == key_id else host_lag(row),
            }
            for row in await storage.host_credentials.credential_status()
        ],
    }


@router.post("/host-credentials/{host}/renew")
async def renew_host_credential(
    host: str,
    body: ConfirmRequest,
    request: Request,
    store: Any = Depends(get_storage),
    admin_id: str = Depends(verify_admin_access),
) -> dict[str, Any]:
    """Give one server a fresh credential at its next heartbeat; the old one lapses after a grace.

    The renewal replaces re-enrollment for a server whose runner can renew:
    no enrollment token, no restart, and the old credential stops working
    shortly after the new one's first use.
    """
    require_deployment_authority(request, admin_id)
    if body.confirm != host:
        raise HTTPException(status_code=422, detail="Confirm by typing the server's name.")
    storage = require_store(store)
    if not await storage.host_credentials.request_renewal(host):
        raise HTTPException(status_code=404, detail="No enrolled server has this name.")
    await log_admin_action(
        storage,
        get_client_ip(request),
        "agent.runner_host.renew_credential",
        details={"host": host},
        actor_id=admin_id,
    )
    return {"requested": True}
