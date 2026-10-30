"""Rotate the sandbox manager credential on every server, one server at a time.

``stage``       each server accepts the next credential beside the current
                one: through its deployment agent, or for a managed compute
                server through the SSH executor.
``verify_next`` the control plane authenticates to each server's sandbox
                manager with the next credential; this is the evidence, for
                both kinds of server.
``switch``      the saved setting becomes the next credential.
``adopt``       every control-plane process has adopted it.
``promote``     each server makes it the only credential; a managed server's
                stored deployment inputs change too, so a redeploy or a node
                replacement cannot bring the old one back.
``verify``      each server answers the new credential.

Restarts drain first and go one server at a time, so the pool keeps serving
turns throughout. A server neither executor can reach refuses the start.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cloud_agent.application import managed_hosts
from cloud_agent.application.key_rotation import (
    VERIFY_TIMEOUT,
    _host_agents,
    _label,
    _open_at_rest,
    _require_sealed,
    _run_agent_step,
    _seal_at_rest,
    _sealed_for,
    _verify,
)
from cloud_agent.application.workflows import WorkflowRefused, save
from cloud_agent_shared.deployment_contract import key_identifier

_NEXT = "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"
_TOKEN = "AGENT_SANDBOX_MANAGER_TOKEN"
_RESTART = ("sandbox-manager", "runner")


async def prepare(storage, agents, names, params, sealed_by_agent, secret):  # type: ignore[no-untyped-def]
    """Validate the participants and keep the new credential encrypted for the workflow."""
    if not secret or len(secret.encode()) < 32:
        raise WorkflowRefused("the new credential must be at least 32 bytes")
    workflow_id = str(params.get("operation_id") or "0" * 32)
    members = await managed_hosts.participants(
        storage, _host_agents(agents), operation_id=workflow_id
    )
    with_agents = [m["agent_id"] for m in members if m["kind"] == "agent"]
    _require_sealed([a for a in agents if a["agent_id"] in with_agents], sealed_by_agent, _NEXT)
    prepared = {
        "new_token_id": key_identifier(secret),
        "participants": members,
        "agents": with_agents,
        "names": names,
    }
    return prepared, {"next_token": _seal_at_rest(secret)}, "stage"


async def _reachable_with(storage: Any, host: str, token: str) -> bool:
    """Whether this server's sandbox manager accepts ``token`` on its authenticated health route."""
    from cloud_agent.sandbox_manager_client import SandboxManagerClient, SandboxManagerError
    from cloud_agent.sandbox_manager_routing import (
        SandboxManagerUrlRefused,
        validate_sandbox_manager_url,
    )

    row = (await managed_hosts.live_hosts(storage)).get(host)
    if row is None:
        return False
    try:
        url = validate_sandbox_manager_url(row.get("sandbox_manager_url"))
    except SandboxManagerUrlRefused:
        return False
    if not url:
        return False
    try:
        await SandboxManagerClient(url, token).health()
    except SandboxManagerError:
        return False
    return True


async def _evidence(
    storage: Any, workflow: dict[str, Any], token: str, next_step: str | None, waiting: str
) -> bool:
    """Wait until every participant answers ``token``; a time limit fails the workflow."""
    members = workflow["params"]["participants"]
    laggards = [m["host"] for m in members if not await _reachable_with(storage, m["host"], token)]
    if not laggards:
        data = {**workflow["data"], "verify_started_at": None}
        if next_step is None:
            data.pop("next_token", None)
            await save(storage, workflow, state="succeeded", detail=waiting, data=data)
            return False
        return (
            await save(storage, workflow, state="running", step=next_step, detail="", data=data)
            is not None
        )
    data = dict(workflow["data"])
    started = data.get("verify_started_at") or datetime.now(UTC).isoformat()
    data["verify_started_at"] = started
    if datetime.now(UTC) - datetime.fromisoformat(started) > VERIFY_TIMEOUT:
        await save(
            storage,
            workflow,
            state="failed",
            detail="these servers do not accept the credential: " + ", ".join(laggards[:10]),
            data=data,
        )
        return False
    await save(
        storage,
        workflow,
        state="waiting",
        detail="waiting for these servers to accept the credential: " + ", ".join(laggards[:10]),
        data=data,
    )
    return False


async def _on_each_server(storage: Any, workflow: dict[str, Any], step: str, one: Any) -> bool:
    """Run ``one`` on the servers in order; each waits for the one before it."""
    for member in workflow["params"]["participants"]:
        outcome = await one(member)
        if outcome == "failed":
            if workflow["state"] != "failed":
                await save(
                    storage,
                    workflow,
                    state="failed",
                    detail=f"{step} on {member['host']}: "
                    + managed_hosts.step_detail(workflow, f"{step}:{member['host']}"),
                )
            return False
        if outcome != "succeeded":
            detail = f"{step}: working on {member['host']}"
            if workflow["detail"] != detail:
                await save(storage, workflow, state="waiting", detail=detail)
            return False
    return True


async def _stage(storage: Any, workflow: dict[str, Any]) -> bool:
    token = _open_at_rest(workflow["data"]["next_token"])

    async def one(member: dict[str, Any]) -> str:
        if member["kind"] == "agent":
            return await _run_agent_step(
                storage,
                workflow,
                f"stage-{member['agent_id']}",
                [member["agent_id"]],
                "rotation_step",
                {
                    "workflow": "sandbox_manager_token",
                    "operation_id": workflow["workflow_id"],
                    "step": "stage",
                },
                sealed_for=_sealed_for(workflow, _NEXT),
            )

        async def apply() -> None:
            await _apply(workflow, member["host"], changes={_NEXT: token}, services=_RESTART)

        return await managed_hosts.run_step(storage, workflow, f"stage:{member['host']}", apply)

    if not await _on_each_server(storage, workflow, "stage", one):
        return False
    data = {k: v for k, v in workflow["data"].items() if k != "sealed_by_agent"}
    return (
        await save(storage, workflow, state="running", step="verify_next", detail="", data=data)
        is not None
    )


async def _apply(workflow: dict[str, Any], host: str, **change: Any) -> None:
    executor = managed_hosts.executor()
    if executor is None:
        raise ValueError("this process has no SSH executor")
    server = (await executor.hosts(workflow["workflow_id"])).get(host)
    if server is None:
        raise ValueError(f"{host} is no longer a live managed server")
    await executor.apply(server, **change)


async def _switch(storage: Any, workflow: dict[str, Any]) -> bool:
    from cloud_agent import settings

    token = _open_at_rest(workflow["data"]["next_token"])
    revision, _encoded = await storage.settings.read_settings()
    new_revision, _encoded, _previous = await storage.settings.update_settings(
        settings.encode_changes({"sandbox_manager_token": token}), expected_revision=revision
    )
    return (
        await save(
            storage,
            workflow,
            state="running",
            step="adopt",
            detail="",
            data={**workflow["data"], "switch_revision": new_revision},
        )
        is not None
    )


async def _promote(storage: Any, workflow: dict[str, Any]) -> bool:
    token = _open_at_rest(workflow["data"]["next_token"])

    async def one(member: dict[str, Any]) -> str:
        if member["kind"] == "agent":
            return await _run_agent_step(
                storage,
                workflow,
                f"promote-{member['agent_id']}",
                [member["agent_id"]],
                "rotation_step",
                {
                    "workflow": "sandbox_manager_token",
                    "operation_id": workflow["workflow_id"],
                    "step": "promote",
                },
            )

        async def apply() -> None:
            await _apply(
                workflow,
                member["host"],
                changes={_TOKEN: token},
                remove=(_NEXT,),
                services=_RESTART,
            )
            executor = managed_hosts.executor()
            assert executor is not None  # _apply refused otherwise
            server = (await executor.hosts(workflow["workflow_id"]))[member["host"]]
            await executor.update_profile(
                server.profile.id,
                lambda inputs: {
                    **inputs,
                    "environment": managed_hosts.set_template_value(
                        inputs["environment"], _TOKEN, token
                    ),
                },
            )

        return await managed_hosts.run_step(storage, workflow, f"promote:{member['host']}", apply)

    if not await _on_each_server(storage, workflow, "promote", one):
        return False
    return await save(storage, workflow, state="running", step="verify", detail="") is not None


async def advance(storage: Any, workflow: dict[str, Any]) -> None:
    """Advance as many steps as the evidence allows."""
    while workflow["state"] in {"running", "waiting"}:
        step = workflow["step"]
        if step == "stage":
            moved = await _stage(storage, workflow)
        elif step == "verify_next":
            moved = await _evidence(
                storage,
                workflow,
                _open_at_rest(workflow["data"]["next_token"]),
                "switch",
                "every server accepts the next credential",
            )
        elif step == "switch":
            moved = await _switch(storage, workflow)
        elif step == "adopt":
            target = workflow["data"]["switch_revision"]
            moved = await _verify(
                storage,
                workflow,
                lambda instances, target=target: [
                    _label(i) for i in instances if i["adopted_settings_revision"] < target
                ],
                "promote",
                "waiting for every process to use the new credential",
            )
        elif step == "promote":
            moved = await _promote(storage, workflow)
        elif step == "verify":
            moved = await _evidence(
                storage,
                workflow,
                _open_at_rest(workflow["data"]["next_token"]),
                None,
                "every server uses the new credential",
            )
        else:
            return
        if not moved:
            return


__all__ = ["advance", "prepare"]
