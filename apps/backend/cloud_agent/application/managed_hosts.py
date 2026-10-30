"""Rotation participants: which servers a workflow reaches, and how, one at a time.

A server is reached through its deployment agent, or, for a managed compute
server, through the existing SSH executor. Nothing here adds an agent to a
managed server. A server with neither cannot take part, and a workflow that
needs it refuses to start and names it.

SSH work can last as long as the server's longest running turn, because a
restart drains first, so it runs in a task beside the workflow rather than
inside the advance that started it: the step is claimed on the workflow row,
and the result is written back to it. The remote scripts are idempotent, so a
claim abandoned by a process that died is simply run again.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.application.workflows import WorkflowRefused, save
from cloud_agent.compute_secrets import ComputeSecretError
from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from cloud_agent.ports.external.compute_provider import NodeObservation
    from cloud_agent.ports.external.host_bootstrap import BootstrapIntent

#: After this long with no result, a claim on a step belongs to a dead process.
CLAIM_EXPIRES = timedelta(hours=24, minutes=10)
_PROFILE_DOMAIN = "deployment_profile"
_LIVE_STATES = {"online", "draining"}


@dataclass(frozen=True)
class ManagedHost:
    """One running Host on a compute node, as the SSH executor addresses it."""

    host: str
    node: NodeObservation
    profile: DeploymentProfile
    intent: BootstrapIntent
    #: Whether the saved deployment inputs live in the database (encrypted),
    #: where a rotation can replace them. A file on the control plane cannot be.
    stored: bool
    #: The automatic setup that provisioned its tunnel accounts, if one did.
    automatic: dict[str, Any] | None


class ManagedHosts:
    """The SSH executor as a rotation sees it."""

    def __init__(
        self,
        *,
        repository: Any,
        credential_rotation: Any,
        bootstrap: Any,
        secrets: Any,
        tunnels: Any,
        setup: Any,
    ) -> None:
        """Bind the compute adapters built at startup."""
        self.repository = repository
        self.credential_rotation = credential_rotation
        self.bootstrap = bootstrap
        self.secrets = secrets
        self.tunnels = tunnels
        self.setup = setup

    async def hosts(self, operation_id: str) -> dict[str, ManagedHost]:
        """Every live managed Host by name."""
        found: dict[str, ManagedHost] = {}
        for allocation in await self.repository.list_allocations():
            snapshot = allocation.get("deployment_profile")
            if not snapshot or allocation.get("observed_state") in {"terminated", "absent"}:
                continue
            profile = DeploymentProfile.model_validate(snapshot)
            nodes = {node["id"]: node for node in allocation["nodes"]}
            for row in allocation["hosts"]:
                node = nodes.get(row.get("compute_node_id"))
                if node is None or row.get("status") not in _LIVE_STATES:
                    continue
                intent = ComputeReconciler._bootstrap_intent(
                    {"id": operation_id}, {**allocation, "slot_host": row["host"]}, node
                )
                found[row["host"]] = ManagedHost(
                    host=row["host"],
                    node=ComputeReconciler._node(node),
                    profile=profile,
                    intent=intent,
                    stored=profile.environment_ref.startswith("stored-profile:"),
                    automatic=await self._automatic(profile),
                )
        return found

    async def _automatic(self, profile: DeploymentProfile) -> dict[str, Any] | None:
        row = await self.repository.get_deployment_profile(profile.id)
        metadata = ((row or {}).get("configuration") or {}).get("automatic_setup")
        return metadata if isinstance(metadata, dict) and metadata.get("ready") else None

    async def apply(self, host: ManagedHost, **change: Any) -> None:
        """Change credentials on one Host through the fixed remote script."""
        await self.bootstrap.apply_environment(host.node, host.profile, host.intent, **change)

    def _payload(self, profile_id: str, row: dict[str, Any]) -> dict[str, Any]:
        return json.loads(
            self.secrets.decrypt("profile:" + profile_id, _PROFILE_DOMAIN, row["secret_ciphertext"])
        )

    async def profile_inputs(self, profile_id: str) -> dict[str, Any]:
        """The decrypted inputs of one stored profile (trusted callers only)."""
        row = await self.repository.get_deployment_profile(profile_id)
        if row is None:
            raise ComputeSecretError("deployment profile no longer exists")
        return self._payload(profile_id, row)

    async def update_profile(
        self, profile_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        """Rewrite one stored profile's inputs, so a later redeploy cannot bring old ones back."""
        for _ in range(5):
            row = await self.repository.get_deployment_profile(profile_id)
            if row is None:
                raise ComputeSecretError("deployment profile no longer exists")
            updated = change(self._payload(profile_id, row))
            ciphertext = self.secrets.encrypt(
                "profile:" + profile_id, _PROFILE_DOMAIN, json.dumps(updated)
            )
            if await self.credential_rotation.replace_profile_secret(
                profile_id=profile_id,
                expected_ciphertext=row["secret_ciphertext"],
                secret_ciphertext=ciphertext,
            ):
                return
        raise ComputeSecretError("deployment profile kept changing; try again")


def set_template_value(template: str, name: str, value: str) -> str:
    """One ``NAME='value'`` line of a profile's environment template, replaced or added."""
    line = f"{name}='{value}'"
    pattern = re.compile(rf"^{re.escape(name)}=.*$", re.MULTILINE)
    if pattern.search(template):
        return pattern.sub(lambda _match: line, template, count=1)
    return template.rstrip("\n") + "\n" + line + "\n"


_executor: ManagedHosts | None = None
_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}


def configure(executor: ManagedHosts | None) -> None:
    """Name this process's SSH executor; only a process with compute has one."""
    global _executor
    _executor = executor


def executor() -> ManagedHosts | None:
    """This process's SSH executor, if it has one."""
    return _executor


async def live_hosts(storage: Any) -> dict[str, dict[str, Any]]:
    """Enrolled servers a rotation must reach, by name."""
    return {
        row["host"]: row
        for row in await storage.host_pool.list_runner_hosts()
        if row.get("status") in _LIVE_STATES
    }


async def participants(
    storage: Any, agents: list[dict[str, Any]], *, operation_id: str, need_stored: bool = True
) -> list[dict[str, Any]]:
    """Every live server and how to reach it; refuses naming any that cannot be reached.

    A server with a deployment agent uses it, even a managed one. Offline
    agents refuse too: skipping one would strand it on the old credential.
    """
    from cloud_agent.application import deployments

    by_host = {str((a.get("report") or {}).get("runner_host") or ""): a for a in agents}
    live = await live_hosts(storage)
    managed = await _executor.hosts(operation_id) if _executor is not None else {}
    result: list[dict[str, Any]] = []
    unreachable: list[str] = []
    for name in sorted(live):
        agent = by_host.get(name)
        if agent is not None:
            if not deployments.online(agent):
                unreachable.append(f"{name} (its deployment agent is offline)")
            else:
                result.append(
                    {
                        "host": name,
                        "kind": "agent",
                        "agent_id": agent["agent_id"],
                        "name": agent["name"],
                    }
                )
        elif name not in managed:
            unreachable.append(f"{name} (no deployment agent)")
        elif need_stored and not managed[name].stored:
            unreachable.append(f"{name} (its deployment inputs are files, not stored)")
        else:
            result.append({"host": name, "kind": "managed"})
    if unreachable:
        raise WorkflowRefused(
            "these servers cannot be reached, so they cannot take part: "
            + ", ".join(unreachable[:10])
        )
    if not result:
        raise WorkflowRefused("no server is online")
    return result


def _steps(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(workflow["data"].get("host_steps") or {})


async def _record(storage: Any, workflow_id: str, key: str, entry: dict[str, Any]) -> None:
    from cloud_agent.application.workflows import advance_workflow

    for _ in range(30):
        workflow = await storage.deployments.get_workflow(workflow_id)
        if workflow is None:
            return
        data = {**workflow["data"], "host_steps": {**_steps(workflow), key: entry}}
        if await save(storage, workflow, data=data) is not None:
            await advance_workflow(storage, workflow)
            return
        await asyncio.sleep(0.2)


async def _perform(
    storage: Any, workflow_id: str, key: str, action: Callable[[], Awaitable[Any]]
) -> None:
    try:
        await action()
        entry: dict[str, Any] = {"state": "succeeded"}
    except (ProviderError, ComputeSecretError, ValueError) as exc:
        entry = {"state": "failed", "detail": str(exc)[:500]}
    finally:
        _tasks.pop((workflow_id, key), None)
    await _record(storage, workflow_id, key, entry)


async def run_step(
    storage: Any,
    workflow: dict[str, Any],
    key: str,
    action: Callable[[], Awaitable[Any]],
) -> str:
    """``succeeded``, ``failed`` or ``waiting`` for one claimed SSH step of a workflow."""
    workflow_id = workflow["workflow_id"]
    entry = _steps(workflow).get(key)
    if entry and entry["state"] in {"succeeded", "failed"}:
        return str(entry["state"])
    if (workflow_id, key) in _tasks:
        return "waiting"
    if entry and datetime.now(UTC) - datetime.fromisoformat(entry["claimed_at"]) < CLAIM_EXPIRES:
        return "waiting"  # another process holds it
    claim = {"state": "running", "claimed_at": datetime.now(UTC).isoformat()}
    data = {**workflow["data"], "host_steps": {**_steps(workflow), key: claim}}
    if await save(storage, workflow, data=data) is None:
        return "waiting"
    _tasks[(workflow_id, key)] = asyncio.create_task(_perform(storage, workflow_id, key, action))
    return "waiting"


def step_detail(workflow: dict[str, Any], key: str) -> str:
    """The safe failure text of one SSH step."""
    return str((_steps(workflow).get(key) or {}).get("detail") or "the step failed")


async def settle() -> None:
    """Wait for this process's running steps (tests and shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks.values()), return_exceptions=True)


__all__ = [
    "ManagedHost",
    "ManagedHosts",
    "configure",
    "executor",
    "live_hosts",
    "participants",
    "run_step",
    "set_template_value",
    "settle",
    "step_detail",
]
