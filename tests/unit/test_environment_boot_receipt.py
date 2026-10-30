"""A boot counts as prepared only by a receipt of exactly that boot."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.unit.test_environment_idle_boot import _booted, _installed
from tests.unit.test_shared_session_supervisor import (
    _HELD_S,
    _UNBLOCKED_S,
    SharedChatSessionSupervisor,
    _in_thread,
    _Manager,
    _members,
    _spec,
)

from cloud_agent_host.chat_sandbox import ChatSandboxStatus
from cloud_agent_host.environment_preparation import prepared_in
from cloud_agent_shared.environment import BoundEnvironment


def _restarted(tmp_path, *, read_while: str):
    """A group whose receipt prepared boot-one, which stopped; its next start is boot-two.

    The environment's status is read once, as the control plane's environment
    view does: while boot-one still ``"running"``, or once it has
    ``"stopped"``. Either read is fresh when the group starts again.
    """
    manager, supervisor, lease, preparer = _booted(tmp_path, _installed())
    name = _spec().container_name
    backend = SimpleNamespace(manager=manager, spec=_spec())
    manager.items[name] = replace(manager.items[name], started_at="boot-one")
    if read_while == "running":
        assert preparer.status(backend, "bound")["ready"] is True
    manager.items[name] = replace(
        manager.items[name], running=False, status=ChatSandboxStatus.STOPPED
    )
    if read_while == "stopped":
        # Docker keeps a stopped container's last StartedAt: still boot-one.
        assert preparer.status(backend, "bound")["boot_id"] == "boot-one"
    start = manager.start

    def start_next_boot(spec, **kwargs):
        current = manager.items[spec.container_name]
        if not current.running:
            manager.items[spec.container_name] = replace(current, started_at="boot-two")
        return start(spec, **kwargs)

    manager.start = start_next_boot
    return manager, supervisor, lease, preparer


@pytest.mark.parametrize("read_while", ["stopped", "running"])
def test_a_status_read_before_a_start_does_not_make_the_new_boot_look_prepared(
    tmp_path, read_while
):
    _manager, supervisor, lease, preparer = _restarted(tmp_path, read_while=read_while)
    environment = BoundEnvironment(binding_id="bound", setup_script="install")

    result = supervisor.prepare_environment(
        "athr_source", environment, preparer, activation_owner=lease.token
    )

    # Prepared for the boot now running, not answered with the last boot's receipt.
    assert result["boot_id"] == "boot-two"
    start = [step for step in result["steps"] if step["phase"] == "start"]
    assert [step["state"] for step in start] == ["succeeded"]


def test_the_new_boot_runs_its_start_script(tmp_path):
    _manager, supervisor, lease, preparer = _restarted(tmp_path, read_while="stopped")
    started: list[str] = []

    def prepare(_backend, environment, *, cancel):
        started.append(environment.start_script)
        return {"steps": []}

    environment = BoundEnvironment(binding_id="bound", setup_script="install", start_script="serve")

    supervisor.prepare_environment(
        "athr_source",
        environment,
        SimpleNamespace(status=preparer.status, prepare=prepare),
        activation_owner=lease.token,
    )

    assert started == ["serve"]


def test_a_receipt_of_the_running_boot_is_still_answered_without_preparing(tmp_path):
    _manager, supervisor, lease, preparer = _booted(tmp_path, _installed(boot_id="boot-two"))
    prepared: list[object] = []

    def prepare(_backend, environment, *, cancel):
        prepared.append(environment)
        return {"steps": []}

    environment = BoundEnvironment(binding_id="bound", setup_script="install", start_script="serve")

    result = supervisor.prepare_environment(
        "athr_source",
        environment,
        SimpleNamespace(status=preparer.status, prepare=prepare),
        activation_owner=lease.token,
    )

    assert (result["ready"], result["boot_id"]) == (True, "boot-two")
    assert prepared == []


@pytest.mark.parametrize(
    ("receipt", "inspection", "expected"),
    [
        pytest.param({}, {}, True, id="this-boot"),
        pytest.param({}, {"running": False}, False, id="stopped"),
        pytest.param({}, {"started_at": "boot-two"}, False, id="a-later-boot"),
        pytest.param({}, {"container_id": "container-new"}, False, id="another-container"),
        pytest.param({"boot_id": ""}, {"started_at": ""}, False, id="no-boot-identity"),
    ],
)
def test_a_receipt_prepares_only_the_running_boot_it_names(receipt, inspection, expected):
    running = {"running": True, "container_id": "container-shared", "started_at": "boot-one"}
    seen = SimpleNamespace(**{**running, **inspection})

    assert prepared_in(_installed(**receipt), seen) is expected


def test_environment_preparation_reads_docker_outside_the_supervisor_lock(monkeypatch):
    """Preparation inspects the container and reads its receipt without the lock.

    Every turn with an environment binding prepares first, and so does every
    terminal that requires its environment. Holding the supervisor-wide lock
    across those reads stalled every other group for as long as Docker took.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    name = _spec().container_name
    manager.items[name] = replace(manager._inspection(_spec()), started_at="boot-one")
    inspect = manager.inspect
    locked_inspections: list[str] = []

    def observed_inspect(name):
        if supervisor._lock._is_owned():
            locked_inspections.append(name)
        return inspect(name)

    monkeypatch.setattr(manager, "inspect", observed_inspect)
    entered = threading.Event()
    release = threading.Event()
    ready = {"ready": True, "container_id": "container-shared", "boot_id": "boot-one"}

    def receipt(backend, binding_id):
        # EnvironmentPreparation.status inspects the container on a cache miss.
        backend.manager.inspect(backend.spec.container_name)
        entered.set()
        assert release.wait(timeout=_HELD_S)
        return {"binding_id": binding_id, **ready}

    preparer = SimpleNamespace(status=receipt)
    environment = SimpleNamespace(binding_id="binding-1")
    preparing, outcome = _in_thread(
        lambda: supervisor.prepare_environment("athr_source", environment, preparer)
    )
    try:
        assert entered.wait(timeout=1)
        status_thread, status_outcome = _in_thread(lambda: supervisor.status(other.thread_id))
        status_thread.join(timeout=_UNBLOCKED_S)
        assert not status_thread.is_alive(), "preparation held the supervisor-wide lock"
    finally:
        release.set()
        preparing.join(timeout=2)

    assert not preparing.is_alive()
    assert outcome == {"value": {"binding_id": "binding-1", **ready}}
    assert "error" not in status_outcome
    assert locked_inspections == []
