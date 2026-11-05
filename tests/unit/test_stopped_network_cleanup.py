"""A stopped group's network release must clear its own record, and only that.

Docker keeps two views of a stopped container's network. Measured on Docker 29
in a disposable daemon (``tests/external/test_stopped_network_release.py``
repeats it in CI):

* ``stop`` empties the network's ``Containers``; the container keeps
  ``Networks[name]`` with the old ``NetworkID`` and an empty ``EndpointID``;
* ``network rm`` then succeeds and the container record stays behind;
* with the network gone, ``network disconnect --force <name>`` exits 0 and
  clears the record, while ``<old id>`` exits 1 and leaves it;
* with a same-name replacement, disconnect by name also clears the record.
  The product still refuses that case rather than resolve the replacement.

The retained record is what the supervisor reads as "final archive not yet
finished", so a release that leaves it re-runs recovery on every listing and
refuses every discard. ``_StoppedDocker`` adds those measured behaviours to
the shared model. The real-Docker normal-stop regression also confirms that
disconnect by an existing network's ID clears the stopped record. One fault
injection test covers a daemon retaining it despite answering successfully.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.unit.test_chat_group_network import (
    _NAMESPACE,
    FakeDocker,
    FakeGuard,
    _config,
    _group_bridge,
    _owned_labels,
    _spec as _network_spec,
)
from tests.unit.test_shared_session_supervisor import _Manager, _spec as _shared_spec

from cloud_agent_host.chat_sandbox import ChatSandboxManager, RetainedChatLost
from cloud_agent_host.chat_sandbox_models import CHAT_GENERATION_LABEL
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.session_lifecycle import SessionBusy
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor

SANDBOX = "sandbox-container"


class _StoppedDocker(FakeDocker):
    """The shared Docker model plus the measured stopped-container behaviour."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        #: Fault injection: success from disconnect without the expected effect.
        self.id_disconnect_keeps_record = False
        #: A daemon that answers 0 without clearing anything.
        self.keep_stopped_records = False

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["network", "disconnect"]:
            self.calls.append(list(argv))
            return self._disconnect(argv[-2], argv[-1])
        if argv[1:3] == ["container", "rm"]:
            self.calls.append(list(argv))
            container = self.container(argv[-1])
            if container is None or container["running"]:
                return self._fail("cannot remove container")
            del self.containers[container["Id"]]
            return self._ok("")
        result = super().__call__(argv, **kwargs)
        if argv[1] == "stop":
            container = self.container(argv[-1])
            assert container is not None
            for name, record in container["networks"].items():
                network = self.network(name)
                if network is not None:
                    network["endpoints"].pop(container["Id"], None)
                record["EndpointID"] = ""
        return result

    def _disconnect(self, ref: str, container_ref: str) -> subprocess.CompletedProcess[str]:
        container = self.container(container_ref)
        if container is None:
            return self._fail("no such container")
        network = self.network(ref)
        if container["running"]:
            if network is None:
                return self._fail(f"network {ref} not found")
            network["endpoints"].pop(container["Id"], None)
            container["networks"].pop(network["Name"], None)
            return self._ok("")
        if self.keep_stopped_records:
            return self._ok("")
        key = ref if ref in container["networks"] else None
        if key is None and network is not None and not self.id_disconnect_keeps_record:
            key = network["Name"]
        if key is None:
            return self._fail(f"network {ref} not found") if network is None else self._ok("")
        container["networks"].pop(key)
        return self._ok("")


def _world(monkeypatch, spec):
    """A stopped group whose bridge holds only its relay, as after ``docker stop``."""
    config = _config()
    backend = ContainerBackend(image="img", sandbox_network=config, sandbox_namespace=_NAMESPACE)
    backend._network_guard = FakeGuard()  # type: ignore[assignment]
    manager = ChatSandboxManager(backend)
    docker = _StoppedDocker(config)
    monkeypatch.setattr(manager, "_run", docker)
    docker.add_sandbox(spec, running=True)
    name = manager._network_lifecycle._ensure_group_network(spec)
    network = docker.network(name)
    assert network is not None
    docker.connect(network["Id"], SANDBOX, [])
    docker(["docker", "stop", SANDBOX])
    assert docker.names_on(name) == {"relay-mcp"}
    assert docker.containers[SANDBOX]["networks"][name]["NetworkID"] == network["Id"]
    docker.calls.clear()
    return manager, docker, name, network["Id"]


def _remove_bridge(docker: _StoppedDocker, network_id: str) -> None:
    """Remove a bridge as an earlier release did: relays out, then ``network rm``."""
    for relay in sorted(docker.networks[network_id]["endpoints"]):
        assert (
            docker(["docker", "network", "disconnect", "--force", network_id, relay]).returncode
            == 0
        )
    assert docker(["docker", "network", "rm", network_id]).returncode == 0
    docker.calls.clear()


def _record(docker: _StoppedDocker, name: str, container: str = SANDBOX) -> dict | None:
    return docker.containers[container]["networks"].get(name)


def _mutations(docker: _StoppedDocker, container: str = SANDBOX) -> list[list[str]]:
    return [
        call
        for call in docker.calls
        if call[1:3] == ["network", "rm"]
        or (call[1:3] == ["network", "disconnect"] and call[-1] == container)
    ]


def _after_first_missing_read(monkeypatch, manager, docker, name, mutate):
    """Run ``mutate`` once the release has seen its bridge missing, before it acts."""
    fired = False

    def run(argv, **kwargs):
        nonlocal fired
        result = docker(argv, **kwargs)
        reads_bridge = argv[1:3] == ["network", "inspect"] and argv[3:] == [name]
        if not fired and reads_bridge and result.returncode != 0:
            fired = True
            mutate()
        return result

    monkeypatch.setattr(manager, "_run", run)


# ── the manager's release ─────────────────────────────────────────────


@pytest.mark.parametrize("bridge_removed", [False, True], ids=["normal-stop", "bridge-gone"])
def test_release_clears_the_stopped_record_and_converges(monkeypatch, bridge_removed):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    if bridge_removed:
        _remove_bridge(docker, old_id)
        assert _record(docker, name)["NetworkID"] == old_id
        docker.calls.clear()

    released = manager.release_stopped_networks(spec, expected_container_id=SANDBOX)

    assert released.networks == ()
    assert manager._require_exact(spec, SANDBOX).networks == ()
    assert docker.network(name) is None
    if bridge_removed:
        # Only the name addresses the record; the old id would exit 1.
        assert _mutations(docker) == [["docker", "network", "disconnect", "--force", name, SANDBOX]]
    docker.calls.clear()
    assert manager.release_stopped_networks(spec, expected_container_id=SANDBOX) == released
    assert _mutations(docker) == []


def test_a_daemon_keeping_the_record_after_an_id_disconnect_converges_on_retry(monkeypatch):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    docker.id_disconnect_keeps_record = True

    with pytest.raises(SandboxError, match="network intent"):
        manager.release_stopped_networks(spec, expected_container_id=SANDBOX)
    assert docker.network(name) is None
    assert _record(docker, name)["NetworkID"] == old_id

    assert manager.release_stopped_networks(spec, expected_container_id=SANDBOX).networks == ()


@pytest.mark.parametrize("bridge_removed", [False, True], ids=["normal-stop", "bridge-gone"])
def test_a_record_docker_silently_keeps_is_refused_not_reported_released(
    monkeypatch, bridge_removed
):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    if bridge_removed:
        _remove_bridge(docker, old_id)
    docker.keep_stopped_records = True

    with pytest.raises(SandboxError, match="network intent"):
        manager.release_stopped_networks(spec, expected_container_id=SANDBOX)
    assert _record(docker, name)["NetworkID"] == old_id


@pytest.mark.parametrize("failing_read", ["bridge", "recheck"])
def test_an_unreadable_network_refuses_without_touching_the_record(monkeypatch, failing_read):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    if failing_read == "bridge":
        docker.uninspectable.add(old_id)
    else:
        _remove_bridge(docker, old_id)
        reads = 0

        def run(argv, **kwargs):
            nonlocal reads
            if argv[1:3] == ["network", "inspect"] and argv[3:] == [name]:
                reads += 1
                if reads == 2:
                    docker.calls.append(list(argv))
                    return subprocess.CompletedProcess(argv, 1, "", "input/output error")
            return docker(argv, **kwargs)

        monkeypatch.setattr(manager, "_run", run)
    docker.calls.clear()

    with pytest.raises(SandboxError, match="network inspect failed"):
        manager.release_stopped_networks(spec, expected_container_id=SANDBOX)
    assert _record(docker, name)["NetworkID"] == old_id
    assert _mutations(docker) == []


def _restart(docker, spec, manager):
    docker.containers[SANDBOX].update(running=True, status="running")


def _replace_container(docker, spec, manager):
    docker.containers[SANDBOX]["Name"] = "/renamed-original"
    docker.add_container(
        "replacement-container",
        name=spec.container_name,
        labels=dict(docker.containers[SANDBOX]["labels"]),
        image=docker.containers[SANDBOX]["image"],
    )


def _relabel_generation(docker, spec, manager):
    docker.containers[SANDBOX]["labels"][CHAT_GENERATION_LABEL] = str(spec.generation + 1)


def _pending_record(docker, spec, manager):
    _record(docker, manager.network.network_for(spec))["NetworkID"] = ""


def _other_record(docker, spec, manager):
    _record(docker, manager.network.network_for(spec))["NetworkID"] = "net-someone-else"


def _recreate_bridge(docker, spec, manager):
    _group_bridge(manager, docker, spec, subnet="10.240.10.0/29")


@pytest.mark.parametrize(
    ("race", "error", "match"),
    [
        (_restart, SandboxError, "restarted"),
        (_replace_container, RetainedChatLost, "identity changed"),
        (_relabel_generation, SandboxError, "outside this chat/generation"),
        (_pending_record, SandboxError, "identity changed during release"),
        (_other_record, SandboxError, "identity changed during release"),
        (_recreate_bridge, SandboxError, "reappeared"),
    ],
    ids=["restart", "container-replaced", "generation", "record-pending", "record-other", "bridge"],
)
def test_a_race_after_the_bridge_is_seen_missing_refuses_before_disconnect(
    monkeypatch, race, error, match
):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    _remove_bridge(docker, old_id)
    before = dict(_record(docker, name))
    _after_first_missing_read(
        monkeypatch, manager, docker, name, lambda: race(docker, spec, manager)
    )
    docker.calls.clear()

    with pytest.raises(error, match=match):
        manager.release_stopped_networks(spec, expected_container_id=SANDBOX)

    assert _mutations(docker) == []
    assert _record(docker, name)["EndpointID"] == before["EndpointID"]
    if race is _recreate_bridge:
        assert docker.names_on(name) == set()


def test_a_known_same_name_replacement_and_its_peer_are_never_touched(monkeypatch):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    _remove_bridge(docker, old_id)
    replacement = docker.add_network(
        name, subnet="10.240.10.0/29", labels=_owned_labels(manager, spec)
    )
    docker.add_container("peer", running=True)
    docker.connect(replacement["Id"], "peer", ["peer"])
    before = {"Id": replacement["Id"], "endpoints": dict(replacement["endpoints"])}
    docker.calls.clear()

    # Docker would clear the record by name here; the product refuses instead.
    with pytest.raises(SandboxError, match="network intent"):
        manager.release_stopped_networks(spec, expected_container_id=SANDBOX)

    assert docker.network(name)["Id"] == before["Id"]
    assert docker.network(name)["endpoints"] == before["endpoints"]
    assert _mutations(docker) == [] and _mutations(docker, "peer") == []
    assert _record(docker, name)["NetworkID"] == old_id


@pytest.mark.parametrize(
    "mutate", [_restart, _replace_container, _relabel_generation, _pending_record, _other_record]
)
def test_a_changed_record_on_an_existing_bridge_refuses_before_disconnect(monkeypatch, mutate):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    fired = False

    def run(argv, **kwargs):
        nonlocal fired
        result = docker(argv, **kwargs)
        if not fired and argv[1:3] == ["network", "inspect"] and argv[3:] == [name]:
            fired = True
            mutate(docker, spec, manager)
        return result

    monkeypatch.setattr(manager, "_run", run)
    with pytest.raises(SandboxError):
        manager.release_stopped_networks(spec, expected_container_id=SANDBOX)
    assert docker.network(name)["Id"] == old_id
    assert not [
        call for call in docker.calls if call[1:3] in (["network", "disconnect"], ["network", "rm"])
    ]


def test_opportunistic_pruning_never_detaches_a_stopped_sandbox(monkeypatch):
    spec = _network_spec()
    manager, docker, name, old_id = _world(monkeypatch, spec)
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    docker.networks[old_id]["Created"] = stale
    # A stopped container this manager does not retain, on another stale bridge.
    other_spec = _network_spec(thread="athr_02", sandbox="asbx_02")
    other_name, other = _group_bridge(manager, docker, other_spec, age_s=3600)
    docker.add_container("unrelated", running=True)
    docker.connect(other["Id"], "unrelated", [])
    docker(["docker", "stop", "unrelated"])
    docker.calls.clear()

    manager.network.prune_orphan_networks([spec])

    assert docker.network(name)["Id"] == old_id
    assert _record(docker, name)["NetworkID"] == old_id
    assert _record(docker, other_name, "unrelated")["NetworkID"] == other["Id"]
    assert not [
        call
        for call in docker.calls
        if call[1:3] == ["network", "disconnect"] and call[-1] in (SANDBOX, "unrelated")
    ]


# ── the supervisor's recovery and discard ─────────────────────────────


class _DockerBackedManager(_Manager):
    """The supervisor's fake, with containers and networks from ``_StoppedDocker``.

    The shared fake's ``stop`` always empties ``networks``, which hid the
    retained record from every supervisor test.
    """

    def __init__(self, real: ChatSandboxManager) -> None:
        super().__init__()
        self.real = real
        self.marker: bytes | None = b"uploaded-final-marker"

    @property
    def network(self):
        return self.real.network

    def inspect(self, container_name: str):
        return self.real.inspect(container_name)

    def list(self):
        current = self.real.inspect(SANDBOX)
        return [] if current is None else [current]

    def read_stopped_final_archive_marker_optional(self, _spec, *, expected_container_id):
        assert expected_container_id == SANDBOX
        return self.marker

    def stop(self, spec, **kwargs):
        self.stop_count += 1
        return self.real.stop(spec, **kwargs)

    def release_stopped_networks(self, spec, **kwargs):
        return self.real.release_stopped_networks(spec, **kwargs)

    def require_stopped_archive(self, spec, **kwargs):
        return self.real.require_stopped_archive(spec, **kwargs)

    def remove_stopped_archived(self, spec, **kwargs):
        return self.real.remove_stopped_archived(spec, **kwargs)


def _supervised(monkeypatch, *, archive=None):
    spec = _shared_spec()
    real, docker, name, old_id = _world(monkeypatch, spec)
    manager = _DockerBackedManager(real)
    finals: list[str] = []

    def completed_final(_spec, inspection, _members):
        # A completed marker makes the archive runtime return without upload.
        finals.append(inspection.container_id)
        if archive is not None:
            archive()

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=completed_final)
    return supervisor, docker, spec, name, old_id, finals


def _admit_discard(supervisor, spec):
    supervisor.begin_archive(
        spec.thread_id, request_id="discard-1", generation=spec.generation, container_id=SANDBOX
    )
    supervisor.commit_archive(spec, request_id="discard-1", container_id=SANDBOX)


@pytest.mark.parametrize("bridge_removed", [False, True], ids=["normal-stop", "bridge-gone"])
def test_a_finished_stop_stops_requeueing_recovery_and_admits_discard(monkeypatch, bridge_removed):
    supervisor, docker, spec, name, old_id, finals = _supervised(monkeypatch)
    if bridge_removed:
        _remove_bridge(docker, old_id)

    for _ in range(4):
        supervisor.reconcile()
    group = supervisor._groups[spec.sandbox_id]

    assert finals == [SANDBOX], "a retained record re-ran final recovery"
    assert _record(docker, name) is None
    assert group.lifecycle_check is None and not group.stopping
    assert not (group.stop_archive_pending or group.orphan_finalization_pending)
    _admit_discard(supervisor, spec)
    assert docker.container(SANDBOX) is None
    assert spec.sandbox_id not in supervisor._groups


@pytest.mark.parametrize("failure", ["archive", "network"])
def test_a_genuine_archive_or_network_failure_still_blocks_discard(monkeypatch, failure):
    def fail_upload():
        raise SandboxError("final archive upload failed")

    supervisor, docker, spec, name, old_id, _finals = _supervised(
        monkeypatch, archive=fail_upload if failure == "archive" else None
    )
    docker.keep_stopped_records = failure == "network"

    for _ in range(2):
        supervisor.reconcile()

    assert _record(docker, name)["NetworkID"] == old_id
    with pytest.raises(SessionBusy, match="archive must finish before deletion"):
        _admit_discard(supervisor, spec)
    assert docker.container(SANDBOX) is not None
