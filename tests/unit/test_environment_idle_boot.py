"""An environment boot that runs no script is prepared without freezing writers."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_shared_session_supervisor import (
    SharedChatSessionSupervisor,
    _Manager,
    _members,
    _spec,
)

from cloud_agent_host.chat_sandbox import RetainedChatLost
from cloud_agent_host.environment_preparation import EnvironmentPreparation, runs_no_script
from cloud_agent_host.session_lifecycle import SessionLeaseLost
from cloud_agent_shared.environment import BoundEnvironment

if TYPE_CHECKING:
    import threading

_CONTAINER = "container-shared"


class _EnvironmentManager(_Manager):
    """A running group whose container answers execs and carries its boot."""

    def __init__(self) -> None:
        super().__init__()
        self.execs: list[tuple[list[str], int | None]] = []

    def exec_capture(self, _spec, argv, *, user_uid=None, **_kwargs):
        self.events.append(("exec", user_uid))
        self.execs.append((list(argv), user_uid))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    def thaw_shared_writers(self, spec, *, require_exact_census=True, **kwargs) -> None:
        # Preparation lets the start script's services stay new writers.
        assert require_exact_census is False
        super().thaw_shared_writers(spec, **kwargs)


def _installed(**overrides) -> dict:
    """A receipt for this container whose installation finished on an earlier boot."""
    receipt = {
        "binding_id": "bound",
        "container_id": _CONTAINER,
        "steps": [
            {"phase": "setup", "state": "succeeded", "output": "", "exit_code": 0},
            {"phase": "start", "state": "succeeded", "output": "", "exit_code": 0},
        ],
        "setup_done": True,
        "start_done": True,
        "boot_id": "boot-one",
    }
    receipt.update(overrides)
    return receipt


def _booted(tmp_path, receipt: dict):
    """A leased member of a group on its second boot, with ``receipt`` on this host."""
    manager = _EnvironmentManager()
    supervisor = SharedChatSessionSupervisor(manager)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    name = _spec().container_name
    manager.items[name] = replace(manager.items[name], started_at="boot-two")
    preparer = EnvironmentPreparation(tmp_path)
    preparer.root.mkdir(parents=True, exist_ok=True)
    path, _lock = preparer._receipt(_CONTAINER)
    path.write_text(json.dumps(receipt))
    manager.events.clear()
    return manager, supervisor, lease, preparer


def _froze(manager: _EnvironmentManager) -> list[str]:
    return [event[0] for event in manager.events if event[0] in ("freeze", "thaw")]


def test_an_empty_start_script_is_recorded_for_this_boot_without_a_writer_freeze(tmp_path):
    manager, supervisor, lease, preparer = _booted(tmp_path, _installed())
    environment = BoundEnvironment(binding_id="bound", setup_script="install")

    result = supervisor.prepare_environment(
        "athr_source", environment, preparer, activation_owner=lease.token
    )

    assert _froze(manager) == []
    # Only the fixed tool-home script ran, as the installer, before the record.
    assert [uid for _argv, uid in manager.execs] == [30000]
    assert result["boot_id"] == "boot-two"
    start = [step for step in result["steps"] if step["phase"] == "start"]
    assert [(step["state"], step["ran"]) for step in start] == [("succeeded", False)]
    backend = SimpleNamespace(manager=manager, spec=_spec())
    assert preparer.status(backend, "bound")["ready"] is True
    assert supervisor._members["athr_source"].snapshotting is False


def test_a_start_skipped_for_this_turn_is_recorded_without_a_writer_freeze(tmp_path):
    manager, supervisor, lease, preparer = _booted(tmp_path, _installed())
    environment = BoundEnvironment(
        binding_id="bound", setup_script="install", start_script="serve", skip_start=True
    )

    result = supervisor.prepare_environment(
        "athr_source", environment, preparer, activation_owner=lease.token
    )

    assert _froze(manager) == []
    start = [step for step in result["steps"] if step["phase"] == "start"]
    assert [step["state"] for step in start] == ["skipped"]
    # As before, a skipped start leaves the boot unprepared for the next turn.
    backend = SimpleNamespace(manager=manager, spec=_spec())
    assert preparer.status(backend, "bound")["ready"] is False


@pytest.mark.parametrize(
    ("receipt", "start_script"),
    [
        pytest.param(_installed(), "serve", id="a-start-script-runs"),
        pytest.param(_installed(update_pending=True), "", id="an-update-is-pending"),
        pytest.param(_installed(setup_done=False), "", id="installation-not-done"),
        pytest.param(_installed(container_id="container-old"), "", id="another-containers-receipt"),
    ],
)
def test_writers_stay_frozen_around_any_script(tmp_path, receipt, start_script):
    manager, supervisor, lease, _ = _booted(tmp_path, receipt)
    prepared: list[list[str]] = []

    def prepare(_backend, _environment, *, cancel):
        prepared.append(_froze(manager))
        return {"steps": []}

    preparer = SimpleNamespace(status=lambda _backend, _binding_id: receipt, prepare=prepare)
    environment = BoundEnvironment(
        binding_id="bound", setup_script="install", start_script=start_script
    )

    supervisor.prepare_environment(
        "athr_source", environment, preparer, activation_owner=lease.token
    )

    assert prepared == [["freeze"]]
    assert _froze(manager) == ["freeze", "thaw"]


def test_a_lease_lost_during_an_unfrozen_preparation_is_still_refused(tmp_path):
    """The activation's lease and the group's lifecycle are fenced exactly as before."""
    manager, supervisor, lease, _ = _booted(tmp_path, _installed())
    receipt = _installed()

    def prepare(_backend, _environment, *, cancel: threading.Event):
        with supervisor._lock:
            supervisor._members["athr_source"].lifecycle_generation += 1
        assert cancel.wait(5), "the lease monitor did not revoke the preparation"
        return {"steps": []}

    preparer = SimpleNamespace(status=lambda _backend, _binding_id: receipt, prepare=prepare)
    environment = BoundEnvironment(binding_id="bound", setup_script="install")

    with pytest.raises(SessionLeaseLost, match="lease was lost"):
        supervisor.prepare_environment(
            "athr_source", environment, preparer, activation_owner=lease.token
        )

    assert _froze(manager) == []
    assert supervisor._members["athr_source"].snapshotting is False


def test_an_unfrozen_preparation_still_proves_its_exact_container(tmp_path):
    manager, supervisor, lease, _ = _booted(tmp_path, _installed())
    receipt = _installed()
    name = _spec().container_name

    def prepare(_backend, _environment, *, cancel):
        manager.items[name] = replace(manager.items[name], container_id="container-replaced")
        return {"steps": []}

    preparer = SimpleNamespace(status=lambda _backend, _binding_id: receipt, prepare=prepare)
    environment = BoundEnvironment(binding_id="bound", setup_script="install")

    with pytest.raises(RetainedChatLost):
        supervisor.prepare_environment(
            "athr_source", environment, preparer, activation_owner=lease.token
        )


@pytest.mark.parametrize(
    ("receipt", "environment", "expected"),
    [
        (_installed(), BoundEnvironment(binding_id="bound"), True),
        (_installed(), BoundEnvironment(binding_id="bound", start_script="  \n"), True),
        (
            _installed(),
            BoundEnvironment(binding_id="bound", start_script="serve", skip_start=True),
            True,
        ),
        (_installed(), BoundEnvironment(binding_id="bound", start_script="serve"), False),
        (_installed(update_pending=True), BoundEnvironment(binding_id="bound"), False),
        (_installed(setup_done=False), BoundEnvironment(binding_id="bound"), False),
        (_installed(setup_done="yes"), BoundEnvironment(binding_id="bound"), False),
        (_installed(container_id="other"), BoundEnvironment(binding_id="bound"), False),
        ({"ready": False}, BoundEnvironment(binding_id="bound"), False),
    ],
)
def test_runs_no_script_only_for_this_containers_finished_installation(
    receipt, environment, expected
):
    assert runs_no_script(receipt, environment, _CONTAINER) is expected
