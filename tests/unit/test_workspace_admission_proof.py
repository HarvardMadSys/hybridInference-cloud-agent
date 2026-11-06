"""A workspace operation's admission stands on a recent network proof, and only within limits.

Admission of a file listing, preview or other workspace operation proves the
group network. A second admission within :data:`TURN_NETWORK_PROOF_S` of a
proof stands on it instead, as a turn's commands do, but only for exactly the
running container that proof saw, in the same lifecycle generation, with no
proof of it failed since, and with the host rules still required.
"""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.test_shared_session_supervisor import _Manager, _members, _spec

from cloud_agent_host.container.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.container.chat_sandbox_exec import network_proof, standing_network_proof
from cloud_agent_host.container.exact_container import ExactContainer
from cloud_agent_host.container.sandbox import SandboxError
from cloud_agent_host.supervisor import shared_admission_proof
from cloud_agent_host.supervisor.shared_session_supervisor import SharedChatSessionSupervisor

STARTED_AT = "2026-11-03T10:00:00.000000000Z"


def _running(spec: ChatSandboxSpec, **changes: Any) -> ChatSandboxInspection:
    inspection = ChatSandboxInspection(
        container_id="container-shared",
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING,
        running=True,
        image="sha256:image",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("agent-net",),
        network_ids=(("agent-net", "network-1"),),
        started_at=STARTED_AT,
    )
    return replace(inspection, **changes)


class _ProofManager:
    """The parts of a manager a standing proof reads, with the real proof records."""

    def __init__(self) -> None:
        self.exact = ExactContainer(SimpleNamespace(), state_roots=("/workspace", "/agent-state"))
        self.host_rule_checks = 0
        self.host_rules_fail = False
        self.network = SimpleNamespace(
            network_for=lambda _spec: "agent-net", require_host_rules=self._require_host_rules
        )

    def _require_host_rules(self) -> None:
        self.host_rule_checks += 1
        if self.host_rules_fail:
            raise SandboxError("the sandbox network rules cannot be shown")

    def prove(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> float:
        proved_from = time.monotonic()
        self.exact._proven.record(spec, inspection)
        self.exact._proven.note_proof(spec, proved_from)
        return proved_from


def _standing(manager: _ProofManager, spec: ChatSandboxSpec, current, **kwargs) -> float | None:
    return standing_network_proof(
        manager,  # type: ignore[arg-type]
        spec,
        current,
        max_age_s=kwargs.get("max_age_s", 60.0),
    )


def test_a_proof_stands_for_the_exact_running_container_it_saw() -> None:
    manager = _ProofManager()
    spec = _spec()
    proved_from = manager.prove(spec, _running(spec))

    assert _standing(manager, spec, _running(spec)) == proved_from
    # The host rules are still required, which the guard answers from its own
    # verification only while that is fresh.
    assert manager.host_rule_checks == 1


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"container_id": "container-other"}, id="replaced"),
        pytest.param({"started_at": "2026-11-03T10:00:30.000000000Z"}, id="restarted"),
        pytest.param({"running": False, "status": ChatSandboxStatus.STOPPED}, id="stopped"),
        pytest.param({"status": ChatSandboxStatus.PAUSED}, id="paused"),
        pytest.param({"network_ids": (("agent-net", "network-2"),)}, id="other-bridge-object"),
        pytest.param(
            {
                "networks": ("agent-net", "bridge"),
                "network_ids": (("agent-net", "network-1"), ("bridge", "default")),
            },
            id="second-network",
        ),
    ],
)
def test_a_proof_does_not_stand_for_a_changed_container(changes: dict[str, Any]) -> None:
    manager = _ProofManager()
    spec = _spec()
    manager.prove(spec, _running(spec))

    assert _standing(manager, spec, _running(spec, **changes)) is None
    assert manager.host_rule_checks == 0


def test_a_proof_does_not_stand_without_a_docker_start_time() -> None:
    manager = _ProofManager()
    spec = _spec()
    manager.prove(spec, _running(spec, started_at=""))

    assert _standing(manager, spec, _running(spec, started_at="")) is None


def test_a_proof_does_not_stand_past_its_age_or_for_another_generation() -> None:
    manager = _ProofManager()
    spec = _spec()
    manager.prove(spec, _running(spec))

    assert _standing(manager, spec, _running(spec), max_age_s=0.0) is None
    successor = replace(spec, generation=spec.generation + 1)
    assert _standing(manager, successor, _running(successor)) is None


def test_a_proof_does_not_stand_for_an_unproven_guest() -> None:
    manager = _ProofManager()
    spec = _spec()
    manager.prove(spec, _running(spec))
    manager.exact.note_unproven_start("container-shared")

    assert _standing(manager, spec, _running(spec)) is None


def test_a_proof_does_not_stand_once_the_host_rules_cannot_be_shown() -> None:
    manager = _ProofManager()
    spec = _spec()
    manager.prove(spec, _running(spec))
    manager.host_rules_fail = True

    with pytest.raises(SandboxError, match="rules cannot be shown"):
        _standing(manager, spec, _running(spec))


def test_a_failed_proof_withdraws_the_one_before_it_and_any_begun_earlier() -> None:
    manager = _ProofManager()
    spec = _spec()
    manager.prove(spec, _running(spec))
    begun_before_failure = time.monotonic()

    manager.exact._proven.withdraw_proof(spec)
    assert _standing(manager, spec, _running(spec)) is None

    # A proof that began before the failure and passed after it proves
    # nothing about the state the failure saw.
    manager.exact._proven.note_proof(spec, begun_before_failure)
    assert _standing(manager, spec, _running(spec)) is None

    proved_from = manager.prove(spec, _running(spec))
    assert _standing(manager, spec, _running(spec)) == proved_from


def test_forgetting_a_container_drops_its_proof() -> None:
    manager = _ProofManager()
    spec = _spec()
    manager.prove(spec, _running(spec))

    manager.exact._proven.forget(spec)

    assert _standing(manager, spec, _running(spec)) is None


def test_a_proving_method_keeps_its_proof_or_withdraws_it_on_failure() -> None:
    class Prover(_ProofManager):
        fail = False

        @network_proof
        def start(self, spec: ChatSandboxSpec, *, current: ChatSandboxInspection):
            if self.fail:
                raise SandboxError("guest network sync failed")
            return self.exact._proven.record(spec, current)

    manager = Prover()
    spec = _spec()
    before = time.monotonic()
    manager.start(spec, current=_running(spec))
    proved_from = _standing(manager, spec, _running(spec))
    assert proved_from is not None and before <= proved_from <= time.monotonic()

    manager.fail = True
    with pytest.raises(SandboxError, match="guest network sync failed"):
        manager.start(spec, current=_running(spec))

    assert _standing(manager, spec, _running(spec)) is None


def test_a_stand_in_manager_without_proof_records_has_none_standing() -> None:
    spec = _spec()

    assert standing_network_proof(object(), spec, _running(spec), max_age_s=60.0) is None  # type: ignore[arg-type]


class _ProvingManager(_Manager):
    """The supervisor tests' manager, keeping the real proof records a start makes."""

    def __init__(self) -> None:
        super().__init__()
        self.exact = ExactContainer(SimpleNamespace(), state_roots=("/workspace", "/agent-state"))
        self.host_rule_requirements = 0
        self.fail_start = False

    @network_proof
    def start(self, spec: ChatSandboxSpec, **kwargs: Any) -> ChatSandboxInspection:
        if self.fail_start:
            raise SandboxError("group network changed during activation")
        return self.exact._proven.record(spec, super().start(spec, **kwargs))

    def require_host_rules(self) -> None:
        self.host_rule_requirements += 1


def _supervised() -> tuple[_ProvingManager, SharedChatSessionSupervisor, ChatSandboxSpec]:
    manager = _ProvingManager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    manager.items[spec.container_name] = _running(spec)
    return manager, supervisor, spec


def _operate(supervisor: SharedChatSessionSupervisor, thread_id: str = "athr_source") -> None:
    token, _ = supervisor.operation_opened(thread_id)
    supervisor.operation_closed(thread_id, token)


def test_a_second_workspace_operation_stands_on_the_first_ones_proof() -> None:
    manager, supervisor, spec = _supervised()
    before = time.monotonic()
    _operate(supervisor)
    starts = len(manager.starts)

    _operate(supervisor)
    _operate(supervisor, "athr_peer")

    assert len(manager.starts) == starts
    assert manager.host_rule_requirements == 2
    # The backend's commands stand on the proof's own age, not the request's.
    backend = supervisor.backend("athr_source", network_proved_at=time.monotonic())
    proved_at = backend._network_proved_at
    assert proved_at is not None and before <= proved_at
    assert proved_at == manager.exact._proven.standing_proof(spec)[1]  # type: ignore[index]


def test_a_terminal_admission_proves_the_network_again() -> None:
    manager, supervisor, _ = _supervised()
    _operate(supervisor)
    starts = len(manager.starts)

    token, _ = supervisor.terminal_opened("athr_source")
    supervisor.terminal_closed("athr_source", token)

    assert len(manager.starts) == starts + 1
    assert manager.host_rule_requirements == 0


def test_a_new_lifecycle_generation_needs_a_new_proof() -> None:
    manager, supervisor, spec = _supervised()
    _operate(supervisor)
    starts = len(manager.starts)
    with supervisor.state.lock:
        supervisor.state.groups[spec.sandbox_id].lifecycle_generation += 1

    _operate(supervisor)

    assert len(manager.starts) == starts + 1


def test_a_restarted_container_is_proved_again() -> None:
    manager, supervisor, spec = _supervised()
    _operate(supervisor)
    starts = len(manager.starts)
    manager.items[spec.container_name] = _running(spec, started_at="2026-11-03T10:05:00.000000000Z")

    _operate(supervisor)

    assert len(manager.starts) == starts + 1


def test_an_aged_proof_is_made_again(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, supervisor, _ = _supervised()
    monkeypatch.setattr(shared_admission_proof, "TURN_NETWORK_PROOF_S", 0.0)
    _operate(supervisor)
    starts = len(manager.starts)

    _operate(supervisor)

    assert len(manager.starts) == starts + 1


def test_a_failed_proof_elsewhere_makes_the_next_operation_prove_again() -> None:
    manager, supervisor, spec = _supervised()
    _operate(supervisor)
    starts = len(manager.starts)
    # A turn's activation of the same group finds the network broken.
    manager.fail_start = True
    with pytest.raises(SandboxError, match="group network changed"):
        manager.start(spec)
    manager.fail_start = False

    _operate(supervisor)

    assert len(manager.starts) == starts + 1
    assert manager.host_rule_requirements == 0


def test_a_stopped_container_is_started_and_proved() -> None:
    manager, supervisor, spec = _supervised()
    _operate(supervisor)
    starts = len(manager.starts)
    manager.items[spec.container_name] = _running(
        spec, running=False, status=ChatSandboxStatus.STOPPED
    )

    _operate(supervisor)

    assert len(manager.starts) == starts + 1
    assert manager.items[spec.container_name].running is True
