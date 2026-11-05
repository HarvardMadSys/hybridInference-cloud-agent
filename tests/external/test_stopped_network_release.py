"""Real Docker's stopped endpoints must not keep final-archive recovery alive.

Docker removes stopped endpoints from network inspect, but retains the network
name on container inspect, even after network rm. Exercise the product release
path against that disagreement in a disposable daemon, never a live sandbox.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from tests.external.test_host_release_real_docker import BASE, Node, node, node_image

from cloud_agent_host.chat_sandbox import ChatSandboxManager
from cloud_agent_host.chat_sandbox_models import CHAT_NAMESPACE_LABEL, ChatSandboxSpec
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_shared.egress import network_from_env

pytestmark = pytest.mark.external
__all__ = ["node", "node_image"]


def _stopped_group(node: Node, monkeypatch):
    config = network_from_env(
        {
            "AGENT_SANDBOX_NAMESPACE": "release-regression",
            "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
            "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
            "AGENT_RELAY_NETWORK": "agent-relays",
            "AGENT_GATEWAY_URL": "http://agent-gateway:8080",
            "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
        }
    )
    manager = ChatSandboxManager(
        ContainerBackend(image=BASE, sandbox_namespace="release-regression", sandbox_network=config)
    )

    def run(command, **kwargs):
        return subprocess.run(
            ["docker", "exec", node.name, *command],
            capture_output=True,
            text=kwargs.get("text", True),
            timeout=kwargs.get("timeout", 30),
        )

    monkeypatch.setattr(manager, "_run", run)
    # This regression is Docker metadata and finalization, not firewall policy.
    # No host guard is installed or changed; its separate packet tests own that.
    monkeypatch.setattr(manager.network, "_drop_relay_exceptions", lambda _name: None)
    spec = ChatSandboxSpec(
        thread_id="athr_release", sandbox_id="asbx_release", generation=1, runtime_id="claude-code"
    )
    name = manager.network.network_for(spec)
    labels = manager.network._network_labels(spec)
    network_id = node.docker(
        "network",
        "create",
        *[arg for k, v in labels.items() for arg in ("--label", f"{k}={v}")],
        name,
    ).strip()
    labels = {**spec.docker_labels(), CHAT_NAMESPACE_LABEL: "release-regression"}
    container_id = node.docker(
        "run",
        "-d",
        "--network",
        name,
        "--name",
        spec.container_name,
        *[arg for k, v in labels.items() for arg in ("--label", f"{k}={v}")],
        BASE,
        "sleep",
        "1d",
    ).strip()
    node.docker("stop", "-t", "1", container_id)
    assert json.loads(node.docker("network", "inspect", name))[0]["Containers"] == {}
    raw = json.loads(node.docker("inspect", container_id))[0]
    assert raw["NetworkSettings"]["Networks"][name]["EndpointID"] == ""
    assert raw["NetworkSettings"]["Networks"][name]["NetworkID"] == network_id
    assert name in manager._require_exact(spec, container_id).networks
    return manager, spec, name, network_id, container_id


@pytest.mark.parametrize("bridge_removed", [False, True])
def test_final_release_clears_stopped_network_intent(node: Node, monkeypatch, bridge_removed):
    manager, spec, name, network_id, container_id = _stopped_group(node, monkeypatch)
    if bridge_removed:
        node.docker("network", "rm", network_id)
    released = manager.stop(spec, expected_container_id=container_id, release_networks=True)
    assert not released.running
    assert name not in released.networks, "a stale bridge name retriggers final-archive recovery"
    # Re-read Docker; changing only the returned inspection cannot pass.
    assert name not in manager._require_exact(spec, container_id).networks
    assert manager.stop(spec, expected_container_id=container_id) == released


def test_release_never_removes_a_same_name_replacement_or_its_peer(node: Node, monkeypatch):
    manager, spec, name, old_id, container_id = _stopped_group(node, monkeypatch)
    node.docker("network", "rm", old_id)
    replacement_id = node.docker("network", "create", name).strip()
    peer = node.docker("run", "-d", "--network", name, BASE, "sleep", "1d").strip()
    before = json.loads(node.docker("network", "inspect", replacement_id))[0]
    with pytest.raises(SandboxError, match="network intent"):
        manager.stop(spec, expected_container_id=container_id, release_networks=True)
    after = json.loads(node.docker("network", "inspect", replacement_id))[0]
    assert before["Id"] == after["Id"] == replacement_id
    assert after["Containers"] == before["Containers"]
    assert peer in after["Containers"]


def test_repeated_listing_finishes_once_and_allows_discard(node: Node, monkeypatch):
    manager, spec, name, network_id, container_id = _stopped_group(node, monkeypatch)
    node.docker("network", "rm", network_id)
    archived = []
    monkeypatch.setattr(
        manager,
        "read_stopped_final_archive_marker_optional",
        lambda *_args, **_kwargs: b"completed",
    )
    monkeypatch.setattr(manager.network, "prune_orphan_networks", lambda *_args: None)
    # The completed-checkpoint hook is a no-op; Docker and recovery are real.
    # A stale name must not keep reacquiring lifecycle reservations on listing.
    supervisor = SharedChatSessionSupervisor(
        manager, stopped_archive_hook=lambda *_args: archived.append(container_id)
    )
    for _ in range(3):
        supervisor.reconcile()
    assert archived == [container_id]
    assert name not in manager._require_exact(spec, container_id).networks
    group = supervisor._groups[spec.sandbox_id]
    assert group.lifecycle_check is None
    assert not group.stop_archive_pending
    supervisor.begin_archive(
        spec.thread_id,
        request_id="discard-1",
        generation=spec.generation,
        container_id=container_id,
    )
    supervisor.commit_archive(spec, request_id="discard-1", container_id=container_id)
    assert manager.inspect(spec.container_name) is None
    assert supervisor.reconcile() == []
