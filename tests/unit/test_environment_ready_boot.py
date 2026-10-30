"""A boot whose preparation is already done is answered without a start or a proof."""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.unit.test_shared_session_supervisor import (
    SharedChatSessionSupervisor,
    _Manager,
    _members,
    _spec,
)

from cloud_agent_host import shared_session_supervisor
from cloud_agent_host.chat_sandbox import ChatSandboxStatus
from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_shared.environment import BoundEnvironment

_CONTAINER = "container-shared"
_ENVIRONMENT = BoundEnvironment(binding_id="bound", setup_script="install", start_script="serve")


def _ready(**overrides) -> dict:
    """This container's receipt, prepared for its current boot."""
    receipt = {
        "binding_id": "bound",
        "container_id": _CONTAINER,
        "steps": [
            {"phase": "setup", "state": "succeeded", "output": "", "exit_code": 0},
            {"phase": "start", "state": "succeeded", "output": "", "exit_code": 0},
        ],
        "setup_done": True,
        "start_done": True,
        "boot_id": "boot-two",
        "ready": True,
    }
    receipt.update(overrides)
    return receipt


def _leased():
    """A leased member of a running group on its second boot."""
    manager = _Manager()
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
    manager.starts.clear()
    manager.network_proofs.clear()
    return manager, supervisor, lease


def _status(receipt: dict):
    return SimpleNamespace(status=lambda _backend, _binding_id: receipt)


def _started_or_proved(manager: _Manager) -> int:
    return len(manager.starts) + len(manager.network_proofs)


def _answers(monkeypatch) -> list:
    """What the shortcut answered, once per preparation: ``None`` takes the full path."""
    answered: list = []
    shortcut = shared_session_supervisor.ready_boot

    def recorded(*args):
        answered.append(shortcut(*args))
        return answered[-1]

    monkeypatch.setattr(shared_session_supervisor, "ready_boot", recorded)
    return answered


def test_a_prepared_boot_is_answered_without_a_start_or_a_proof(tmp_path):
    manager, supervisor, lease = _leased()
    preparer = EnvironmentPreparation(tmp_path)
    preparer.root.mkdir(parents=True, exist_ok=True)
    receipt = _ready()
    del receipt["ready"]
    preparer._receipt(_CONTAINER)[0].write_text(json.dumps(receipt))

    prepared = supervisor.prepare_environment(
        "athr_source", _ENVIRONMENT, preparer, activation_owner=lease.token
    )

    assert prepared["ready"] is True
    assert (prepared["container_id"], prepared["boot_id"]) == (_CONTAINER, "boot-two")
    assert _started_or_proved(manager) == 0
    assert supervisor._members["athr_source"].inspection.started_at == "boot-two"


def _fall_through_cases():
    """Every way the shortcut must hand the request to the start-and-prove path."""

    def other_boot(manager, supervisor, lease):
        return _ready(boot_id="boot-one"), lease.token

    def other_container(manager, supervisor, lease):
        return _ready(container_id="container-old"), lease.token

    def not_ready(manager, supervisor, lease):
        return _ready(ready=False), lease.token

    def lease_lost(manager, supervisor, lease):
        return _ready(), "attempt:superseded"

    def stopped(manager, supervisor, lease):
        name = _spec().container_name
        manager.items[name] = replace(
            manager.items[name], running=False, status=ChatSandboxStatus.STOPPED
        )
        return _ready(), lease.token

    def replaced(manager, supervisor, lease):
        name = _spec().container_name
        manager.items[name] = replace(manager.items[name], container_id="container-new")
        return _ready(container_id="container-new"), lease.token

    def replaced_since_the_receipt(manager, supervisor, lease):
        name = _spec().container_name
        manager.items[name] = replace(manager.items[name], container_id="container-new")
        return _ready(), lease.token

    def failed_start(manager, supervisor, lease):
        supervisor._members["athr_source"].start_failed = True
        return _ready(), lease.token

    def archive_completion(manager, supervisor, lease):
        group = supervisor._members["athr_source"]
        group.start_archive_completion_pending = True
        supervisor._started_archive_hook = lambda *_args: None
        return _ready(), lease.token

    def container_lost(manager, supervisor, lease):
        supervisor._members["athr_source"].container_lost = True
        return _ready(), lease.token

    def capturing(manager, supervisor, lease):
        supervisor._members["athr_source"].snapshotting = True
        return _ready(), lease.token

    def removed(manager, supervisor, lease):
        del manager.items[_spec().container_name]
        return _ready(), lease.token

    return {
        "an-earlier-boot": other_boot,
        "another-container": other_container,
        "not-ready": not_ready,
        "lease-lost": lease_lost,
        "stopped": stopped,
        "replaced-under-the-name": replaced,
        "replaced-since-the-receipt": replaced_since_the_receipt,
        "failed-start": failed_start,
        "archive-completion-pending": archive_completion,
        "container-lost": container_lost,
        "capturing-a-snapshot": capturing,
        "removed": removed,
    }


@pytest.mark.parametrize("case", sorted(_fall_through_cases()))
def test_anything_short_of_this_exact_prepared_boot_takes_the_full_path(monkeypatch, case):
    manager, supervisor, lease = _leased()
    receipt, owner = _fall_through_cases()[case](manager, supervisor, lease)
    answered = _answers(monkeypatch)

    # The full path's own outcome is not under test here, only that it ran.
    with contextlib.suppress(Exception):
        supervisor.prepare_environment(
            "athr_source", _ENVIRONMENT, _status(receipt), activation_owner=owner
        )

    assert answered == [None]


def test_a_start_finishing_during_the_reads_gets_its_own_boot_prepared(monkeypatch):
    """A start replaces the group's inspection without a new lifecycle generation."""
    manager, supervisor, lease = _leased()
    name = _spec().container_name
    group = supervisor._members["athr_source"]
    inspect = manager.inspect

    def raced(container_name):
        seen = inspect(container_name)
        # Another request's start finishes just after this read, on a new boot.
        with supervisor._lock:
            group.inspection = manager.items[name] = replace(seen, started_at="boot-three")
        manager.inspect = inspect
        return seen

    manager.inspect = raced
    answered = _answers(monkeypatch)

    with contextlib.suppress(Exception):
        supervisor.prepare_environment(
            "athr_source", _ENVIRONMENT, _status(_ready()), activation_owner=lease.token
        )

    assert answered == [None]
    assert group.inspection.started_at == "boot-three"


def test_a_group_known_to_be_stopped_is_left_to_the_full_path_unread(monkeypatch):
    _manager, supervisor, lease = _leased()
    group = supervisor._members["athr_source"]
    with supervisor._lock:
        group.inspection = replace(
            group.inspection, running=False, status=ChatSandboxStatus.STOPPED
        )
    reads: list[str] = []

    def status(_backend, binding_id):
        reads.append(binding_id)
        return _ready()

    answered = _answers(monkeypatch)
    with contextlib.suppress(Exception):
        supervisor.prepare_environment(
            "athr_source",
            _ENVIRONMENT,
            SimpleNamespace(status=status),
            activation_owner=lease.token,
        )

    assert answered == [None]
    assert reads == ["bound"], "only the full path read the receipt"


def test_a_lifecycle_change_during_the_reads_takes_the_full_path():
    manager, supervisor, lease = _leased()
    reads: list[int] = []

    def status(_backend, _binding_id):
        group = supervisor._members["athr_source"]
        if not reads:
            with supervisor._lock:
                group.lifecycle_generation += 1
        reads.append(group.lifecycle_generation)
        return _ready()

    supervisor.prepare_environment(
        "athr_source",
        _ENVIRONMENT,
        SimpleNamespace(status=status),
        activation_owner=lease.token,
    )

    assert len(reads) == 2
    assert _started_or_proved(manager) >= 1


def test_an_unreadable_receipt_takes_the_full_path_and_its_error():
    manager, supervisor, lease = _leased()

    def status(_backend, _binding_id):
        raise ValueError("Workspace binding does not match preparation")

    with pytest.raises(ValueError, match="binding does not match"):
        supervisor.prepare_environment(
            "athr_source",
            _ENVIRONMENT,
            SimpleNamespace(status=status),
            activation_owner=lease.token,
        )

    assert _started_or_proved(manager) >= 1
