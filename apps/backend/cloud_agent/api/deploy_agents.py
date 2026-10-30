"""The deployment agents' own endpoints: enroll, report, pull intents, report results.

Only an agent credential (``cad_...``) authenticates here, and it reaches
nothing else: the admin API refuses it, and this router refuses every other
credential. An agent sees only its own intents.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api.deps import bearer_token, get_storage, require_store
from cloud_agent.application import deployments
from cloud_agent.application.key_rotation import register_advancers
from cloud_agent.application.workflows import advance_after_agent_update

register_advancers()

router = APIRouter(prefix="/v1/agent/deploy", tags=["deployment-agents"])


class RegisterRequest(BaseModel):
    """An agent spending a one-time enrollment token."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=8, max_length=256)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    role: str = Field(pattern=r"^(control-plane|host)$")
    machine: str = Field(min_length=1, max_length=255)
    public_key: str = Field(min_length=32, max_length=256)


class ReportRequest(BaseModel):
    """What the agent observes and the state of its recent operations."""

    model_config = ConfigDict(extra="forbid")

    observed: dict[str, Any] = Field(default_factory=dict)
    operations: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class ResultRequest(BaseModel):
    """One intent's state, from the agent that ran it."""

    model_config = ConfigDict(extra="forbid")

    state: str = Field(pattern=r"^(running|succeeded|failed|rejected)$")
    detail: str = Field(default="", max_length=2000)
    evidence: dict[str, Any] = Field(default_factory=dict)


async def current_agent(
    authorization: str | None = Header(None), store: Any = Depends(get_storage)
) -> dict[str, Any]:
    """The agent this request's credential names."""
    presented = bearer_token(authorization) or ""
    if not deployments.is_agent_credential(presented):
        raise HTTPException(status_code=401, detail="A deployment agent credential is required.")
    agent = await require_store(store).deployments.agent_by_credential(
        deployments.digest(presented)
    )
    if agent is None:
        raise HTTPException(status_code=401, detail="This agent credential is not valid.")
    return agent


@router.post("/register")
async def register(body: RegisterRequest, store: Any = Depends(get_storage)) -> dict[str, Any]:
    """Spend an enrollment token; the credential is returned once and never again."""
    try:
        agent, credential = await deployments.register_agent(
            require_store(store).deployments,
            token=body.token,
            name=body.name,
            role=body.role,
            machine=body.machine,
            public_key=body.public_key,
        )
    except deployments.DeploymentRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    return {"agent_id": agent["agent_id"], "credential": credential}


@router.post("/report")
async def report(
    body: ReportRequest,
    agent: dict[str, Any] = Depends(current_agent),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Store the report and move the agent's intents forward from its journal."""
    if len(str(body.observed)) > 262144:
        raise HTTPException(status_code=413, detail="The report is too large.")
    storage = require_store(store)
    await storage.deployments.record_report(agent["agent_id"], body.observed, body.operations)
    await advance_after_agent_update(storage, agent["agent_id"])
    return {"ok": True}


@router.get("/intents")
async def intents(
    after_seq: int = 0,
    agent: dict[str, Any] = Depends(current_agent),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Pending intents for this agent above ``after_seq``, now marked delivered."""
    delivered = await require_store(store).deployments.deliver_intents(
        agent["agent_id"], max(0, after_seq)
    )
    return {
        "intents": [
            {
                "intent_id": intent["intent_id"],
                "agent_id": intent["agent_id"],
                "seq": intent["seq"],
                "operation": intent["operation"],
                "params": intent["params"],
                "sealed": intent.get("sealed") or {},
                "expires_at": intent["expires_at"].timestamp(),
            }
            for intent in delivered
        ]
    }


@router.post("/intents/{intent_id}/result")
async def result(
    intent_id: str,
    body: ResultRequest,
    agent: dict[str, Any] = Depends(current_agent),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Record one intent's state; only a forward transition is accepted."""
    storage = require_store(store)
    moved = await storage.deployments.update_intent(
        agent_id=agent["agent_id"],
        intent_id=intent_id,
        state=body.state,
        detail=body.detail,
        evidence=body.evidence,
    )
    if moved:
        await advance_after_agent_update(storage, agent["agent_id"])
    return {"accepted": moved}
