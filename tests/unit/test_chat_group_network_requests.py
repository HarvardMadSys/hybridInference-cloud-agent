"""One network proof per workspace request, and a heartbeat listing in four processes.

A hot Kata group behind the real supervisor and sandbox manager endpoints,
against the model of Docker in :mod:`tests.unit.test_chat_group_network`:
each Docker CLI process a request starts is counted, so a second proof of the
group network, or an inspection repeated per container or per bridge, shows.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.test_chat_group_network import _NAMESPACE, FakeDocker, FakeGuard, _config

from cloud_agent_host.chat_sandbox import (
    _GUEST_NETWORK_CHECK,
    CHAT_NAMESPACE_LABEL,
    CHAT_SANDBOX_LABEL,
    ChatSandboxManager,
    ChatSandboxSpec,
)
from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend, ProcessBackend
from cloud_agent_host.sandbox_manager import create_app
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
)


class _KataExecDocker(FakeDocker):
    """The model with its sandbox under Kata, answering every exec it is sent."""

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1] == "exec":
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["container", "ls"]:
            self.calls.append(list(argv))
            rows = [
                {"id": container["Id"], "namespace": labels[CHAT_NAMESPACE_LABEL], "probe": ""}
                for container in self.containers.values()
                if (labels := container["labels"]).get(CHAT_SANDBOX_LABEL) == "true"
            ]
            return subprocess.CompletedProcess(argv, 0, "\n".join(map(json.dumps, rows)), "")
        return super().__call__(argv, **kwargs)

    def _container_json(self, container: dict[str, Any]) -> dict[str, Any]:
        raw = super()._container_json(container)
        if container["Id"] == "sandbox-container":
            raw["HostConfig"]["Runtime"] = KATA_RUNTIME
        return raw


def _hot_kata_workspace(monkeypatch, *, stopped_groups: int = 0):
    """A running Kata group on its bridge, behind the real supervisor and endpoints.

    ``docker.calls`` records every Docker CLI process a request starts: the
    lifecycle's own calls, and the execs that go through subprocess, which
    run a short local process in their place. ``proofs`` records each group
    network proof, the one a start makes of a running container included.
    ``stopped_groups`` more groups are retained, stopped, each with a bridge.
    """
    config = _config()
    backend = ContainerBackend(
        image="img", runtime=KATA_RUNTIME, sandbox_network=config, sandbox_namespace=_NAMESPACE
    )
    backend._network_guard = FakeGuard()  # type: ignore[assignment]
    manager = ChatSandboxManager(backend)
    docker = _KataExecDocker(config)
    monkeypatch.setattr(manager, "_run", docker)
    run, popen = subprocess.run, subprocess.Popen

    def exec_run(argv, **kwargs):
        if argv[:2] != ["docker", "exec"]:
            return run(argv, **kwargs)
        docker.calls.append(list(argv))
        # The guest check passes, and a workspace read finds an empty directory.
        answer = [] if _GUEST_NETWORK_CHECK in argv else {"kind": "directory", "entries": []}
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer).encode(), b"")

    def exec_popen(argv, **kwargs):
        if argv[:2] != ["docker", "exec"]:
            return popen(argv, **kwargs)
        docker.calls.append(list(argv))
        # A Git view reads its own markers back over a clean workspace: each
        # section it prints, in its order and empty, then a status of 0.
        tags = {kind: tag for tag, kind in re.findall(r"(__HYI_\w+?_([A-Z]+)__)", argv[-1])}
        status = tags.pop("STATUS", None)
        output = "".join(f"\n{tag}\n" for tag in tags.values())
        output += f"\n{status}0\n" if status else ""
        return popen(["/bin/sh", "-c", 'sleep 0.05; printf "%s" "$1"', "sh", output], **kwargs)

    monkeypatch.setattr(subprocess, "run", exec_run)
    monkeypatch.setattr(subprocess, "Popen", exec_popen)
    # Ready for the running boot, which this Docker model starts without a StartedAt.
    ready = {"ready": True, "container_id": "sandbox-container", "boot_id": ""}
    monkeypatch.setattr(EnvironmentPreparation, "status", lambda *_args: ready)

    spec = ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=3,
        runtime_id="claude-code",
        sandbox_image_digest="sha256:image-pinned",
        shared_live=True,
        resident_slot_capacity=2,
    )
    docker.add_sandbox(spec, running=True)
    name = manager._network_lifecycle._ensure_group_network(spec)
    network = docker.network(name)
    network["Created"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    docker.connect(network["Id"], "sandbox-container", [])
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(
        spec,
        [
            SharedMemberManifest(
                thread_id=spec.thread_id,
                native_session_id="native-01",
                context_status="live",
                resident_slot_id=0,
                last_settled_job_id="ajob_01",
            )
        ],
        expected_container_id="sandbox-container",
    )
    # Hot: the turn that activated the group already started and proved it.
    manager.start(spec, expected_container_id="sandbox-container")
    for index in range(stopped_groups):
        other = replace(spec, thread_id=f"athr_stopped{index}", sandbox_id=f"asbx_stopped{index}")
        docker.add_container(
            f"stopped-container-{index}",
            name=other.container_name,
            labels={**other.docker_labels(), CHAT_NAMESPACE_LABEL: _NAMESPACE},
            image="sha256:image-pinned",
        )
        manager._network_lifecycle._ensure_group_network(other)
        supervisor.register_manifest(
            other,
            [
                SharedMemberManifest(
                    thread_id=other.thread_id,
                    native_session_id=f"native-stopped{index}",
                    context_status="live",
                    resident_slot_id=0,
                    last_settled_job_id=f"ajob_stopped{index}",
                )
            ],
            expected_container_id=f"stopped-container-{index}",
        )
    proofs: list[ChatSandboxSpec] = []
    prove = manager._network_lifecycle._require_group_network

    def counted_proof(proved: ChatSandboxSpec, *args: Any, **kwargs: Any):
        proofs.append(proved)
        return prove(proved, *args, **kwargs)

    monkeypatch.setattr(manager._network_lifecycle, "_require_group_network", counted_proof)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="secret",
        shared_session_supervisor=supervisor,
    )
    docker.calls.clear()
    return app, docker, proofs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "processes", "proved"),
    [("files", 12, 1), ("git", 13, 1), ("terminal", 13, 1), ("environment", 1, 0)],
)
async def test_a_workspace_request_proves_a_hot_group_network_once(
    monkeypatch, kind, processes, proved
):
    """Admission proves the bridge, and nothing on the way to the work proves it again.

    A request used to prove it two or three times: the start its admission
    makes, the endpoint's own check, and a spawned command's check. On a hot
    Kata group that was 27 Docker CLI processes for a file read, 39 for a Git
    view, 27 for a terminal and 26 for a ready environment. One proof made it
    14, 14, 14 and 13; entering the proven container by id, with no inspection
    before each bounded exec, makes it 12, 13, 13 and 12. A spawn still
    inspects first. A ready environment runs nothing in the guest, so it is
    answered on one inspection of its boot, with no proof at all.
    """
    app, docker, proofs = _hot_kata_workspace(monkeypatch)
    headers = {"X-Agent-Workspace-Token": "secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox-manager"
    ) as client:
        if kind == "files":
            response = await client.get("/workspaces/athr_01/files?path=", headers=headers)
        elif kind == "git":
            response = await client.get("/workspaces/athr_01/git", headers=headers)
        elif kind == "terminal":
            response = await client.post(
                "/workspaces/athr_01/terminals", headers=headers, json={"rows": 24, "cols": 80}
            )
        else:
            response = await client.post(
                "/workspaces/athr_01/environment/prepare",
                headers=headers,
                json={"environment": {"binding_id": "binding"}},
            )
        measured = list(docker.calls)
        if kind == "terminal":
            await client.delete(
                f"/workspaces/athr_01/terminals/{response.json()['id']}", headers=headers
            )

    assert response.status_code == 200, response.text
    assert len(proofs) == proved
    guest_syncs = [argv for argv in measured if argv[1] == "exec" and "--privileged" in argv]
    assert len(guest_syncs) == proved, "the guest's relay names are written once"
    assert len(measured) == processes


@pytest.mark.asyncio
async def test_a_heartbeat_listing_reads_every_group_in_four_processes(monkeypatch):
    """One listing and one batched inspection each, for the containers and their bridges.

    It was a process per container, per bridge, and once more per bridge whose
    group the listing named: 17 for these five groups, and growing with every
    group retained on the host until the heartbeat's timeout dropped them.
    """
    app, docker, _proofs = _hot_kata_workspace(monkeypatch, stopped_groups=4)
    headers = {"X-Agent-Workspace-Token": "secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox-manager"
    ) as client:
        settled = await client.get("/sandboxes", headers=headers)
        docker.calls.clear()
        listed = await client.get("/sandboxes", headers=headers)

    assert settled.status_code == listed.status_code == 200
    assert len(listed.json()["sandboxes"]) == 5
    assert [argv[1:3] for argv in docker.calls] == [
        ["container", "ls"],
        ["container", "inspect"],
        ["network", "ls"],
        ["network", "inspect"],
    ]
