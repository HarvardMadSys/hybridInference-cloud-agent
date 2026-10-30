"""Multi-step deployment workflows: each step is intents, and a step ends only on evidence.

A workflow advances when an agent reports, when an intent result arrives, and
on a background tick. Advancing is idempotent: every intent a step creates
has the idempotency key ``<workflow>:<step>:<agent>``, so two control-plane
processes advancing the same workflow at once create each intent once, and
the workflow row moves only from the version that was read.

Kinds register an *advancer* in :data:`ADVANCERS`. This module holds the
release rollout; the key rotations and database workflows register theirs
from their own modules.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from cloud_agent.application import deployments

if TYPE_CHECKING:
    from collections.abc import Callable

Advancer = "Callable[[Any, dict[str, Any]], Awaitable[None]]"
ADVANCERS: dict[str, Any] = {}


class WorkflowRefused(ValueError):
    """A workflow cannot start; the message says why."""


async def save(
    storage: Any,
    workflow: dict[str, Any],
    *,
    state: str | None = None,
    step: str | None = None,
    detail: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Move a workflow on from the version it was read at; ``None`` if another process did."""
    updated = await storage.deployments.update_workflow(
        workflow["workflow_id"],
        expected_version=workflow["version"],
        state=state or workflow["state"],
        step=step or workflow["step"],
        detail=workflow["detail"] if detail is None else detail,
        data=workflow["data"] if data is None else data,
    )
    if updated is not None:
        workflow.update(updated)
    return updated


async def step_intents(
    storage: Any,
    workflow: dict[str, Any],
    step: str,
    agent_ids: list[str],
    operation: str,
    params: dict[str, Any],
    *,
    sealed_for: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Create (once) one intent per agent for this step and return all of them."""
    created = []
    for agent_id in agent_ids:
        created.append(
            await deployments.create_intent(
                storage.deployments,
                agent_id=agent_id,
                operation=operation,
                params=params,
                sealed=sealed_for(agent_id) if sealed_for else {},
                idempotency_key=f"{workflow['workflow_id']}:{step}:{agent_id}",
                confirm=operation,
                admin_id=workflow["created_by"],
                workflow_id=workflow["workflow_id"],
            )
        )
    return created


def step_state(intents: list[dict[str, Any]]) -> str:
    """``succeeded`` when every intent did, ``failed`` when any ended otherwise, else ``waiting``."""
    states = {intent["state"] for intent in intents}
    if states and states <= {"succeeded"}:
        return "succeeded"
    if states & {"failed", "rejected", "expired", "cancelled"}:
        return "failed"
    return "waiting"


async def current_intents(
    storage: Any, workflow: dict[str, Any], step: str
) -> list[dict[str, Any]]:
    """This step's intents, read fresh."""
    return [
        intent
        for intent in await storage.deployments.list_intents(
            workflow_id=workflow["workflow_id"], limit=500
        )
        if intent["idempotency_key"].split(":")[1] == step
    ]


def failure_detail(step: str, intents: list[dict[str, Any]], agents: dict[str, str]) -> str:
    """Which agent failed which step, and its own words."""
    for intent in intents:
        if intent["state"] in {"failed", "rejected", "expired", "cancelled"}:
            name = agents.get(intent["agent_id"], intent["agent_id"][:8])
            return f"{step} {intent['state']} on {name}: {intent['detail']}"[:2000]
    return f"{step} failed"


async def advance_workflow(storage: Any, workflow: dict[str, Any]) -> None:
    """Advance one workflow by as many steps as its evidence allows."""
    advancer = ADVANCERS.get(workflow["kind"])
    if advancer is None or workflow["state"] not in {"running", "waiting"}:
        return
    await advancer(storage, workflow)


async def advance_after_agent_update(storage: Any, agent_id: str) -> None:
    """Advance every unfinished workflow; called when an agent reports or finishes an intent."""
    deployments_repo = getattr(storage, "deployments", None)
    if deployments_repo is None:
        return
    for workflow in await deployments_repo.list_workflows(active_only=True):
        try:
            await advance_workflow(storage, workflow)
        except deployments.DeploymentRefused as exc:
            await save(storage, workflow, state="failed", detail=str(exc))


async def run_workflows_forever(storage: Any, interval_s: float = 5.0) -> None:
    """Background tick: advance workflows whose step waits on time or on the control plane."""
    import asyncio

    from cloud_agent.logging_utils import get_logger

    logger = get_logger(__name__)
    while True:
        try:
            await advance_after_agent_update(storage, "")
        except Exception as exc:
            logger.warning("workflow tick failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval_s)


# -- release rollout ---------------------------------------------------------

ROLLOUT_STEPS = ("prepare", "prepare_database", "activate_hosts", "activate_control_plane")


async def start_rollout(storage: Any, *, manifest_sha256: str, admin_id: str) -> dict[str, Any]:
    """Begin moving every enrolled machine to one verified release, hosts before the control plane.

    Every unrevoked agent must be online and must have imported and verified
    the release; one that is not blocks the rollout rather than being skipped,
    because the control plane's readiness fence waits for every host anyway.
    """
    agents = [a for a in await storage.deployments.list_agents() if a.get("revoked_at") is None]
    control = [a for a in agents if a["role"] == "control-plane"]
    if not control:
        raise WorkflowRefused("no control-plane machine has a deployment agent")
    blocked = []
    for agent in agents:
        if not deployments.online(agent):
            blocked.append(f"{agent['name']} is offline")
        elif manifest_sha256 not in ((agent.get("report") or {}).get("releases") or {}):
            blocked.append(f"{agent['name']} has not imported this release")
    if blocked:
        raise WorkflowRefused("; ".join(blocked))
    workflow = await storage.deployments.create_workflow(
        workflow_id=uuid.uuid4().hex,
        kind="rollout",
        step="prepare",
        params={
            "manifest_sha256": manifest_sha256,
            "control_plane": [a["agent_id"] for a in control],
            "hosts": [a["agent_id"] for a in agents if a["role"] == "host"],
            "names": {a["agent_id"]: a["name"] for a in agents},
        },
        created_by=admin_id,
    )
    if workflow is None:
        raise WorkflowRefused("another rollout is still running")
    await advance_workflow(storage, workflow)
    return workflow


async def _advance_rollout(storage: Any, workflow: dict[str, Any]) -> None:
    params = workflow["params"]
    targets = {
        "prepare": (params["control_plane"] + params["hosts"], "release_prepare"),
        "prepare_database": (params["control_plane"][:1], "release_prepare_database"),
        "activate_hosts": (params["hosts"], "release_activate"),
        "activate_control_plane": (params["control_plane"], "release_activate"),
    }
    while workflow["state"] in {"running", "waiting"}:
        step = workflow["step"]
        agent_ids, operation = targets[step]
        if not agent_ids:
            outcome = "succeeded"
        else:
            await step_intents(
                storage,
                workflow,
                step,
                agent_ids,
                operation,
                {"manifest_sha256": params["manifest_sha256"]},
            )
            intents = await current_intents(storage, workflow, step)
            outcome = step_state(intents)
            if outcome == "failed":
                await save(
                    storage,
                    workflow,
                    state="failed",
                    detail=failure_detail(step, intents, params["names"])
                    + (
                        "; the control plane was not switched. Run release_abandon on the "
                        "control-plane machine if this was a cold deployment."
                        if step != "activate_control_plane"
                        else ""
                    ),
                )
                return
        if outcome == "waiting":
            if workflow["state"] != "waiting":
                await save(storage, workflow, state="waiting", detail=f"waiting for {step}")
            return
        index = ROLLOUT_STEPS.index(step)
        if index + 1 == len(ROLLOUT_STEPS):
            await save(
                storage, workflow, state="succeeded", detail="every machine runs the release"
            )
            return
        if (
            await save(storage, workflow, state="running", step=ROLLOUT_STEPS[index + 1], detail="")
            is None
        ):
            return


ADVANCERS["rollout"] = _advance_rollout

__all__ = [
    "ADVANCERS",
    "ROLLOUT_STEPS",
    "WorkflowRefused",
    "advance_after_agent_update",
    "advance_workflow",
    "current_intents",
    "run_workflows_forever",
    "save",
    "start_rollout",
    "step_intents",
    "step_state",
]
