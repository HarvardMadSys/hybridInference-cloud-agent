"""Rotate the SSH keys of the three tunnels a server opens, one server at a time.

For each server: **generate** new keys → **authorize** them beside the old ones
on the far end → **switch** the server to them (drained, restarted, restored if
it does not stay up) → **confirm** the server's heartbeat returned over the new
control-plane tunnel → **record** → **revoke** the old keys.

A managed server's keys are generated here and kept sealed on the workflow
until its stored deployment inputs hold them; its far ends are the accounts
automatic setup provisioned, reached through the existing SSH helper. A
self-managed server's agent generates its own, so the private key never leaves
the machine, and a control-plane agent whose local policy names the tunnel's
``authorized_keys`` edits that file. Any far end nothing here can edit (the
inference gateway is usually one) stops the workflow on an administrator
confirmation that shows the exact restricted entry to install or remove.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cloud_agent.application import managed_hosts
from cloud_agent.application.key_rotation import (
    _cp_agents,
    _host_agents,
    _open_at_rest,
    _run_agent_step,
    _seal_at_rest,
)
from cloud_agent.application.workflows import (
    ADVANCERS,
    WorkflowRefused,
    current_intents,
    save,
)
from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.providers.compute.setup_tunnels import ControlPlaneEndpoint, generate_tunnel_key
from cloud_agent_shared.deployment_contract import TUNNELS

ROLES = TUNNELS
_RESTART = ("gateway-tunnel", "mcp-tunnel", "control-plane-tunnel", "sandbox-manager", "runner")
_HEARTBEAT_WAIT = timedelta(minutes=10)
_PHASES = ("generate", "authorize", "switch", "confirm", "record", "revoke")


def _file(role: str) -> str:
    return f".compute-secrets/{role}.key"


async def start_tunnel_rotation(
    storage: Any, *, hosts: list[str] | None, admin_id: str
) -> dict[str, Any]:
    """Validate who takes part and start rotating their tunnel keys."""
    agents = await storage.deployments.list_agents()
    workflow_id = uuid.uuid4().hex
    members = await managed_hosts.participants(
        storage, _host_agents(agents), operation_id=workflow_id
    )
    if hosts:
        unknown = sorted(set(hosts) - {m["host"] for m in members})
        if unknown:
            raise WorkflowRefused("not an online server: " + ", ".join(unknown[:10]))
        members = [m for m in members if m["host"] in hosts]
    workflow = await storage.deployments.create_workflow(
        workflow_id=workflow_id,
        kind="tunnel_keys",
        step="rolling",
        params={
            "participants": members,
            "control_agents": [
                {
                    "agent_id": a["agent_id"],
                    "tunnels": ((a.get("report") or {}).get("policy") or {}).get(
                        "tunnel_authorized_keys"
                    )
                    or [],
                }
                for a in _cp_agents(agents)
            ],
            "names": {a["agent_id"]: a["name"] for a in agents},
        },
        created_by=admin_id,
    )
    if workflow is None:
        raise WorkflowRefused("a tunnel key rotation is already running")
    await save(storage, workflow, data={"phases": {}, "tunnel_keys": {}, "public_keys": {}})
    await ADVANCERS["tunnel_keys"](storage, workflow)
    return workflow


def _phase(workflow: dict[str, Any], host: str) -> str:
    return str(workflow["data"]["phases"].get(host) or "generate")


async def _next_phase(storage: Any, workflow: dict[str, Any], host: str) -> bool:
    current = _phase(workflow, host)
    following = "done" if current == _PHASES[-1] else _PHASES[_PHASES.index(current) + 1]
    data = {
        **workflow["data"],
        "phases": {**workflow["data"]["phases"], host: following},
        "attention": None,
    }
    return await save(storage, workflow, state="running", detail="", data=data) is not None


async def _fail(storage: Any, workflow: dict[str, Any], host: str, detail: str) -> bool:
    await save(
        storage,
        workflow,
        state="failed",
        detail=f"{host}: {detail}; its previous keys still work",
    )
    return False


async def _wait(storage: Any, workflow: dict[str, Any], detail: str) -> bool:
    if workflow["detail"] != detail or workflow["state"] != "waiting":
        await save(storage, workflow, state="waiting", detail=detail)
    return False


async def _ask(
    storage: Any, workflow: dict[str, Any], host: str, action: str, tunnels: dict[str, str]
) -> bool:
    """Stop for an administrator to change a far end nothing here can edit."""
    confirmed = workflow["data"].get("confirmed") or []
    token = f"{host}:{action}"
    if token in confirmed:
        return True
    attention = {"token": token, "host": host, "action": action, "tunnels": tunnels}
    if workflow["data"].get("attention") != attention:
        await save(storage, workflow, data={**workflow["data"], "attention": attention})
    await _wait(
        storage,
        workflow,
        f"{host}: {action} these keys on the far end, then confirm: " + ", ".join(sorted(tunnels)),
    )
    return False


async def confirm_far_end(storage: Any, workflow_id: str, token: str) -> dict[str, Any]:
    """An administrator says the far end was changed; the next step proves or refutes it."""
    workflow = await storage.deployments.get_workflow(workflow_id)
    attention = (workflow or {}).get("data", {}).get("attention") or {}
    if workflow is None or workflow["kind"] != "tunnel_keys" or attention.get("token") != token:
        raise WorkflowRefused("this rotation is not waiting for that confirmation")
    confirmed = [*(workflow["data"].get("confirmed") or []), token]
    await save(storage, workflow, data={**workflow["data"], "confirmed": confirmed})
    await ADVANCERS["tunnel_keys"](storage, workflow)
    return workflow


# -- a managed server, through the SSH executor ------------------------------


async def _managed_server(workflow: dict[str, Any], host: str) -> Any:
    executor = managed_hosts.executor()
    server = await executor.hosts(workflow["workflow_id"]) if executor else {}
    if host not in server:
        raise ValueError(f"{host} is no longer a live managed server")
    return executor, server[host]


def _public_of(private: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_ssh_private_key(private.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


async def _managed_generate(storage: Any, workflow: dict[str, Any], host: str) -> bool:
    executor, server = await _managed_server(workflow, host)
    inputs = await executor.profile_inputs(server.profile.id)
    old = {role: _public_of(inputs["files"][_file(role)].rstrip() + "\n") for role in ROLES}
    keys = {role: generate_tunnel_key() for role in ROLES}
    data = {
        **workflow["data"],
        "tunnel_keys": {
            **workflow["data"]["tunnel_keys"],
            host: {r: _seal_at_rest(k.private_key.get_secret_value()) for r, k in keys.items()},
        },
        "public_keys": {
            **workflow["data"]["public_keys"],
            host: {"old": old, "new": {r: k.public_key for r, k in keys.items()}},
        },
    }
    return await save(storage, workflow, data=data) is not None and await _next_phase(
        storage, workflow, host
    )


def _entries(public: dict[str, str]) -> dict[str, str]:
    return {
        role: f'restrict,port-forwarding,permitopen="<as today>" {key} cloud-agent:{role}'
        for role, key in public.items()
    }


async def _far_ends(workflow: dict[str, Any], host: str, mode: str) -> None:
    """Run one authorized_keys transition on both far ends a managed setup provisioned."""
    executor, server = await _managed_server(workflow, host)
    automatic = server.automatic or {}
    request = (await executor.repository.provider_defaults("automatic_setup")).get("request") or {}
    from pydantic import ValidationError

    from cloud_agent.schemas_compute import AutomaticDeploymentSetup

    try:
        setup = AutomaticDeploymentSetup.model_validate(request)
        endpoint = ControlPlaneEndpoint.model_validate(automatic["endpoint"])
    except ValidationError:
        raise ValueError("the saved automatic setup cannot be read") from None
    keys = workflow["data"]["public_keys"][host]
    await executor.tunnels.rotate_gateway(
        await executor.setup._connection(setup.gateway),
        old_key=keys["old"]["gateway"],
        new_key=keys["new"]["gateway"],
        port=setup.gateway_port,
        setup_id=server.profile.id,
        mode=mode,
    )
    await executor.tunnels.rotate_control(
        await executor.setup._connection(setup.control),
        old_keys={r: keys["old"][r] for r in ("mcp", "control")},
        new_keys={r: keys["new"][r] for r in ("mcp", "control")},
        endpoint=endpoint,
        reverse_port=int(automatic["reverse_port"]),
        setup_id=server.profile.id,
        mode=mode,
    )


async def _ssh(storage: Any, workflow: dict[str, Any], host: str, phase: str, action: Any) -> str:
    return await managed_hosts.run_step(storage, workflow, f"{phase}:{host}", action)


async def _advance_ssh(
    storage: Any, workflow: dict[str, Any], host: str, phase: str, action: Any
) -> bool:
    outcome = await _ssh(storage, workflow, host, phase, action)
    if outcome == "succeeded":
        return await _next_phase(storage, workflow, host)
    if outcome == "failed":
        return await _fail(
            storage, workflow, host, managed_hosts.step_detail(workflow, f"{phase}:{host}")
        )
    return await _wait(storage, workflow, f"{host}: {phase} in progress")


async def _record_managed(workflow: dict[str, Any], host: str) -> None:
    executor, server = await _managed_server(workflow, host)
    sealed = workflow["data"]["tunnel_keys"][host]
    fresh = {role: _open_at_rest(sealed[role]) for role in ROLES}
    public = workflow["data"]["public_keys"][host]["new"]

    def change(inputs: dict[str, Any]) -> dict[str, Any]:
        files = dict(inputs["files"])
        for role, private in fresh.items():
            files[_file(role)] = private
        return {**inputs, "files": files, "public_keys": dict(public)}

    await executor.update_profile(server.profile.id, change)


async def _managed_step(storage: Any, workflow: dict[str, Any], host: str, phase: str) -> bool:
    executor, server = await _managed_server(workflow, host)
    keys = workflow["data"]["public_keys"].get(host) or {}
    if phase == "authorize":
        if server.automatic:
            return await _advance_ssh(
                storage, workflow, host, phase, lambda: _far_ends(workflow, host, "authorize")
            )
        if not await _ask(storage, workflow, host, "add", _entries(keys["new"])):
            return False
        return await _next_phase(storage, workflow, host)
    if phase == "switch":
        sealed = workflow["data"]["tunnel_keys"][host]

        async def switch() -> None:
            await executor.apply(
                server,
                files={_file(r): _open_at_rest(sealed[r]).rstrip() + "\n" for r in ROLES},
                services=_RESTART,
            )

        return await _advance_ssh(storage, workflow, host, phase, switch)
    if phase == "record":
        return await _advance_ssh(
            storage, workflow, host, phase, lambda: _record_managed(workflow, host)
        )
    if server.automatic:
        return await _advance_ssh(
            storage, workflow, host, phase, lambda: _far_ends(workflow, host, "retire")
        )
    if not await _ask(storage, workflow, host, "remove", _entries(keys["old"])):
        return False
    return await _next_phase(storage, workflow, host)


# -- a self-managed server, through its agents --------------------------------


def _rotation_params(workflow: dict[str, Any], step: str, **more: Any) -> dict[str, Any]:
    return {
        "workflow": "tunnel_keys",
        "operation_id": workflow["workflow_id"],
        "step": step,
        **more,
    }


async def _agent_step(
    storage: Any,
    workflow: dict[str, Any],
    member: dict[str, Any],
    step: str,
    agent_id: str,
    **more: Any,
) -> str:
    return await _run_agent_step(
        storage,
        workflow,
        f"{step}-{agent_id}-{member['host'].replace(':', '-')}",
        [agent_id],
        "rotation_step",
        _rotation_params(workflow, step, **more),
    )


def _by_far_end(
    workflow: dict[str, Any], tunnels: dict[str, str]
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Tunnels a control-plane agent edits, by agent, and those nothing here can edit."""
    agents: dict[str, dict[str, str]] = {}
    remaining = dict(tunnels)
    for agent in workflow["params"]["control_agents"]:
        for tunnel in agent["tunnels"]:
            if tunnel in remaining:
                agents.setdefault(agent["agent_id"], {})[tunnel] = remaining.pop(tunnel)
    return agents, remaining


async def _far_end_step(
    storage: Any,
    workflow: dict[str, Any],
    member: dict[str, Any],
    step: str,
    action: str,
    tunnels: dict[str, str],
) -> bool:
    edited, manual = _by_far_end(workflow, tunnels)
    for agent_id, values in edited.items():
        outcome = await _agent_step(
            storage, workflow, member, step, agent_id, host=member["host"], tunnels=values
        )
        if outcome != "succeeded":
            if outcome == "waiting":
                await _wait(storage, workflow, f"{member['host']}: {step} on the control plane")
            return False
    return not manual or await _ask(storage, workflow, member["host"], action, manual)


async def _agent_phase(
    storage: Any, workflow: dict[str, Any], member: dict[str, Any], phase: str
) -> bool:
    host, agent_id = member["host"], member["agent_id"]
    keys = workflow["data"]["public_keys"].get(host) or {}
    if phase == "generate":
        outcome = await _agent_step(storage, workflow, member, "generate", agent_id)
        if outcome != "succeeded":
            return False
        intents = await current_intents(
            storage, workflow, f"generate-{agent_id}-{host.replace(':', '-')}"
        )
        public = (intents[0].get("evidence") or {}).get("public_keys") or {}
        if not public.get("new"):
            return await _fail(storage, workflow, host, "the agent reported no new keys")
        data = {
            **workflow["data"],
            "public_keys": {**workflow["data"]["public_keys"], host: public},
        }
        return await save(storage, workflow, data=data) is not None and await _next_phase(
            storage, workflow, host
        )
    if phase == "authorize":
        if not await _far_end_step(storage, workflow, member, "authorize", "add", keys["new"]):
            return False
        return await _next_phase(storage, workflow, host)
    if phase == "switch":
        outcome = await _agent_step(storage, workflow, member, "switch", agent_id)
        if outcome == "succeeded":
            return await _next_phase(storage, workflow, host)
        if outcome == "waiting":
            await _wait(storage, workflow, f"{host}: switching to the new keys")
        return False
    if phase == "record":
        return await _next_phase(storage, workflow, host)
    if not await _far_end_step(storage, workflow, member, "revoke", "remove", keys["old"]):
        return False
    outcome = await _agent_step(storage, workflow, member, "finish", agent_id)
    if outcome == "succeeded":
        return await _next_phase(storage, workflow, host)
    return False


# -- both -----------------------------------------------------------------------


async def _confirm(storage: Any, workflow: dict[str, Any], host: str) -> bool:
    """The heartbeat after the restart came over the new control-plane tunnel."""
    data = workflow["data"]
    since = data.get("switched_at") or {}
    if host not in since:
        stamped = {**since, host: datetime.now(UTC).isoformat()}
        await save(storage, workflow, data={**data, "switched_at": stamped})
        since = stamped
    started = datetime.fromisoformat(since[host])
    row = (await managed_hosts.live_hosts(storage)).get(host) or {}
    beat = row.get("last_heartbeat_at")
    if isinstance(beat, datetime) and beat > started:
        return await _next_phase(storage, workflow, host)
    if datetime.now(UTC) - started > _HEARTBEAT_WAIT:
        return await _fail(
            storage, workflow, host, "no heartbeat over the new control-plane tunnel"
        )
    return await _wait(storage, workflow, f"{host}: waiting for a heartbeat over the new tunnel")


async def _advance(storage: Any, workflow: dict[str, Any]) -> None:
    while workflow["state"] in {"running", "waiting"}:
        member = next(
            (
                m
                for m in workflow["params"]["participants"]
                if _phase(workflow, m["host"]) != "done"
            ),
            None,
        )
        if member is None:
            data = {k: v for k, v in workflow["data"].items() if k != "tunnel_keys"}
            await save(
                storage,
                workflow,
                state="succeeded",
                detail="every server uses its new tunnel keys; the old ones are revoked",
                data=data,
            )
            return
        host, phase = member["host"], _phase(workflow, member["host"])
        try:
            if phase == "confirm":
                moved = await _confirm(storage, workflow, host)
            elif member["kind"] == "agent":
                moved = await _agent_phase(storage, workflow, member, phase)
            elif phase == "generate":
                moved = await _managed_generate(storage, workflow, host)
            else:
                moved = await _managed_step(storage, workflow, host, phase)
        except (ProviderError, ValueError, KeyError, CredentialCipherError) as exc:
            detail = str(exc) if isinstance(exc, (ProviderError, ValueError)) else "missing input"
            await _fail(storage, workflow, host, detail[:500])
            return
        if not moved:
            return


def register() -> None:
    """Add the tunnel key rotation's advancer."""
    ADVANCERS["tunnel_keys"] = _advance


__all__ = ["confirm_far_end", "register", "start_tunnel_rotation"]
