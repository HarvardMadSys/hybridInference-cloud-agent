"""Database workflows: change the connection to the same database, or move to another one.

``database_connection`` (WP11): prove with a live probe that the candidate
connection reaches this same database (a clone cannot read the token written
now, nor write a reply this connection then reads); then the leader machine,
if the password changes, sets it over the current connection as a SCRAM
verifier, and every control-plane machine switches; done when every current
process reports the new connection's identifier. Once the password changed a
failure is fixed forward: the old password no longer works.

``database_migration`` (WP12): every control-plane process first enters
maintenance (:mod:`cloud_agent.maintenance`) and confirms it refuses writes;
only then the leader machine's agent fences every writer by making the source
read-only, copies into an empty target, verifies it, marks maintenance over in
the target, and switches; the other machines then switch; done when every
current process reports the target's identifier. The source stays read-only
and is never switched back to once writes resume on the target. The journal is
on the agent, with the target connection sealed to the agent's own key, so the
control plane restarting or losing its database mid-copy loses nothing the
migration needs, and the agent resumes by itself.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from cloud_agent.application.key_rotation import (
    _cp_agents,
    _label,
    _require_online,
    _require_sealed,
    _run_agent_step,
    _sealed_for,
    _verify,
)
from cloud_agent.application.workflows import ADVANCERS, WorkflowRefused, current_intents, save

_URL = "AGENT_DATABASE_URL"


async def start_database_workflow(
    storage: Any,
    *,
    kind: str,
    params: dict[str, Any],
    sealed_by_agent: dict[str, dict[str, Any]],
    admin_id: str,
) -> dict[str, Any]:
    """Validate and start a connection change or a migration."""
    if kind not in {"database_connection", "database_migration"}:
        raise WorkflowRefused("unknown database workflow")
    agents = await storage.deployments.list_agents()
    cp = _cp_agents(agents)
    _require_online(cp, "control-plane")
    _require_sealed(cp, sealed_by_agent, _URL)
    dsn_id = params.get("dsn_id")
    if not isinstance(dsn_id, str) or len(dsn_id) != 16:
        raise WorkflowRefused("dsn_id is the new connection's identifier")
    leader = str(params.get("leader") or cp[0]["agent_id"])
    if leader not in {a["agent_id"] for a in cp}:
        raise WorkflowRefused("the leader must be a control-plane agent")
    if kind == "database_migration" and not (
        next(a for a in cp if a["agent_id"] == leader).get("report") or {}
    ).get("policy", {}).get("has_database_tools"):
        raise WorkflowRefused("the leader machine's policy names no PostgreSQL client programs")
    prepared = {
        "dsn_id": dsn_id,
        "rotate_password": bool(params.get("rotate_password")) and kind == "database_connection",
        "leader": leader,
        "others": [a["agent_id"] for a in cp if a["agent_id"] != leader],
        "agents": [a["agent_id"] for a in cp],
        "names": {a["agent_id"]: a["name"] for a in agents},
    }
    workflow_id = uuid.uuid4().hex
    workflow = await storage.deployments.create_workflow(
        workflow_id=workflow_id,
        kind=kind,
        step="probe" if kind == "database_connection" else "maintenance",
        params=prepared,
        created_by=admin_id,
    )
    if workflow is None:
        raise WorkflowRefused("this database workflow is already running")
    data: dict[str, Any] = {"sealed_by_agent": sealed_by_agent}
    if kind == "database_connection":
        token = secrets.token_hex(32)
        await storage.database_probes.create_probe(workflow_id, token)
        data.update(probe_token=token, identity=await storage.database_probes.identity())
    await save(storage, workflow, data=data)
    await ADVANCERS[kind](storage, workflow)
    return workflow


def _forget(workflow: dict[str, Any], *names: str) -> dict[str, Any]:
    return {k: v for k, v in workflow["data"].items() if k not in {"sealed_by_agent", *names}}


async def _advance_connection(storage: Any, workflow: dict[str, Any]) -> None:
    params = workflow["params"]
    while workflow["state"] in {"running", "waiting"}:
        step = workflow["step"]
        if step == "probe":
            outcome = await _run_agent_step(
                storage,
                workflow,
                "probe",
                params["agents"],
                "database_check",
                {
                    "probe_id": workflow["workflow_id"],
                    "probe_token": workflow["data"]["probe_token"],
                    "rotate_password": params["rotate_password"],
                },
                sealed_for=_sealed_for(workflow, _URL),
            )
            if outcome != "succeeded":
                return
            replies = await storage.database_probes.probe_responses(workflow["workflow_id"])
            mine = workflow["data"]["identity"]
            for intent in await current_intents(storage, workflow, "probe"):
                evidence = intent["evidence"] or {}
                if replies.get(intent["agent_id"]) != evidence.get("reply"):
                    await save(
                        storage,
                        workflow,
                        state="failed",
                        detail=f"{params['names'].get(intent['agent_id'])}: the reply did not reach this database; not the same database",
                    )
                    return
                for field in ("datid", "system_identifier"):
                    if mine.get(field) and evidence.get(field) and mine[field] != evidence[field]:
                        await save(
                            storage,
                            workflow,
                            state="failed",
                            detail=f"the candidate database's {field} differs; not the same database",
                        )
                        return
            await save(storage, workflow, state="running", step="switch_leader", detail="")
        elif step in {"switch_leader", "switch_others"}:
            agents = [params["leader"]] if step == "switch_leader" else params["others"]
            if agents:
                outcome = await _run_agent_step(
                    storage,
                    workflow,
                    step,
                    agents,
                    "database_switch",
                    {
                        "probe_id": workflow["workflow_id"],
                        "rotate_password": params["rotate_password"],
                        "leader": step == "switch_leader",
                    },
                    sealed_for=_sealed_for(workflow, _URL),
                )
                if outcome != "succeeded":
                    return
            await save(
                storage,
                workflow,
                state="running",
                step="switch_others" if step == "switch_leader" else "verify",
                detail="",
            )
        elif step == "verify":
            await save(storage, workflow, data=_forget(workflow, "probe_token"))
            await _verify(
                storage,
                workflow,
                lambda instances: [
                    _label(i)
                    for i in instances
                    if i["details"].get("database_url_id") != params["dsn_id"]
                ],
                None,
                "waiting for every process to use the new connection",
            )
            return


async def _leave_maintenance(storage: Any, workflow: dict[str, Any]) -> None:
    """The move stopped with the source still authoritative: writes may resume."""
    await storage.maintenance.finish(
        workflow_id=workflow["workflow_id"], state="aborted", detail=workflow["detail"][:2000]
    )


async def _enter_maintenance(storage: Any, workflow: dict[str, Any]) -> bool:
    """Every process refuses writes before anything is fenced; ``True`` to go on."""
    workflow_id = workflow["workflow_id"]
    if not await storage.maintenance.begin(
        workflow_id=workflow_id, kind="database_migration", detail="moving to another database"
    ):
        await save(storage, workflow, state="failed", detail="another maintenance is running")
        return False
    moved = await _verify(
        storage,
        workflow,
        lambda instances: [
            _label(i) for i in instances if i["details"].get("maintenance_seen") != workflow_id
        ],
        "migrate",
        "waiting for every process to stop accepting changes",
    )
    if not moved and workflow["state"] == "failed":
        await _leave_maintenance(storage, workflow)
    return moved


async def _advance_migration(storage: Any, workflow: dict[str, Any]) -> None:
    params = workflow["params"]
    while workflow["state"] in {"running", "waiting"}:
        step = workflow["step"]
        if step == "maintenance":
            if not await _enter_maintenance(storage, workflow):
                return
        elif step == "migrate":
            outcome = await _run_agent_step(
                storage,
                workflow,
                "migrate",
                [params["leader"]],
                "database_migrate",
                {"operation_id": workflow["workflow_id"]},
                sealed_for=_sealed_for(workflow, _URL),
            )
            if outcome == "failed":
                await _leave_maintenance(storage, workflow)
            if outcome != "succeeded":
                return
            await save(storage, workflow, state="running", step="switch_others", detail="")
        elif step == "switch_others":
            if params["others"]:
                outcome = await _run_agent_step(
                    storage,
                    workflow,
                    "switch_others",
                    params["others"],
                    "database_switch",
                    {
                        "probe_id": workflow["workflow_id"],
                        "rotate_password": False,
                        "leader": False,
                    },
                    sealed_for=_sealed_for(workflow, _URL),
                )
                if outcome != "succeeded":
                    return
            await save(
                storage, workflow, state="running", step="verify", detail="", data=_forget(workflow)
            )
        elif step == "verify":
            await _verify(
                storage,
                workflow,
                lambda instances: [
                    _label(i)
                    for i in instances
                    if i["details"].get("database_url_id") != params["dsn_id"]
                ],
                None,
                "waiting for every process to use the target database",
            )
            return


def register() -> None:
    """Add both database workflows' advancers."""
    ADVANCERS["database_connection"] = _advance_connection
    ADVANCERS["database_migration"] = _advance_migration


__all__ = ["register", "start_database_workflow"]
