"""Lifecycle/fencing tests for the host-singleton chat supervisor."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.session_supervisor import (
    DEFAULT_CHAT_IDLE_TIMEOUT_S,
    ChatSessionSupervisor,
    SessionBusy,
    SessionLeaseLost,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Manager:
    def __init__(self) -> None:
        self.items: dict[str, tuple[ChatSandboxSpec, ChatSandboxInspection]] = {}
        self.created = 0
        self.started = 0
        self.stopped = 0
        self.stale_terminal_reaps: list[str] = []

    @staticmethod
    def _inspection(spec: ChatSandboxSpec, *, running: bool) -> ChatSandboxInspection:
        return ChatSandboxInspection(
            container_id=f"container:{spec.sandbox_id}",
            name=spec.container_name,
            status=ChatSandboxStatus.RUNNING if running else ChatSandboxStatus.STOPPED,
            running=running,
            image=spec.sandbox_image_digest or "sha256:image",
            labels=spec.docker_labels(),
            mounts=(),
            networks=("agent-net",),
        )

    def create(
        self,
        spec: ChatSandboxSpec,
        *,
        image: str | None = None,
        network_phase: str = "setup",
    ) -> ChatSandboxInspection:
        assert image in {None, spec.sandbox_image_digest}
        assert network_phase in {"setup", "agent"}
        self.created += 1
        item = self.items.get(spec.container_name)
        if item is None:
            inspection = self._inspection(spec, running=False)
            self.items[spec.container_name] = (spec, inspection)
            return inspection
        return item[1]

    def require_existing(
        self, spec: ChatSandboxSpec, *, container_id: str
    ) -> ChatSandboxInspection:
        item = self.items.get(spec.container_name)
        if item is None:
            raise SandboxError("missing")
        if item[1].container_id != container_id:
            raise SandboxError("wrong durable container")
        return item[1]

    def start(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        item = self.items.get(spec.container_name)
        if item is None:
            raise SandboxError("missing")
        inspection = item[1]
        if not inspection.running:
            self.started += 1
            inspection = replace(
                inspection,
                status=ChatSandboxStatus.RUNNING,
                running=True,
            )
            self.items[spec.container_name] = (spec, inspection)
        return inspection

    def stop(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        item = self.items.get(spec.container_name)
        if item is None:
            raise SandboxError("missing")
        inspection = item[1]
        if inspection.running:
            self.stopped += 1
            inspection = replace(
                inspection,
                status=ChatSandboxStatus.STOPPED,
                running=False,
            )
            self.items[spec.container_name] = (spec, inspection)
        return inspection

    def inspect(self, container: str) -> ChatSandboxInspection | None:
        item = self.items.get(container)
        return None if item is None else item[1]

    def list(self) -> list[ChatSandboxInspection]:
        return [item[1] for item in self.items.values()]

    def require_network_phase(self, spec: ChatSandboxSpec, phase: str) -> None:
        assert phase == "agent"
        assert self.items[spec.container_name][1].networks == ("agent-net",)

    def reap_stale_terminal_processes(self, spec: ChatSandboxSpec) -> None:
        self.stale_terminal_reaps.append(spec.thread_id)


def _spec(thread: str = "athr_1", sandbox: str = "asbx_1") -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id=thread,
        sandbox_id=sandbox,
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image",
    )


def test_the_product_idle_timeout_defaults_to_thirty_minutes():
    assert DEFAULT_CHAT_IDLE_TIMEOUT_S == 1800.0


def test_supervisor_start_reaps_only_reconciled_stale_terminal_execs():
    manager = _Manager()
    spec = _spec()
    manager.create(spec)
    manager.start(spec)
    supervisor = ChatSessionSupervisor(manager, sweep_interval_s=60)

    supervisor.start()
    try:
        assert manager.stale_terminal_reaps == [spec.thread_id]
        assert manager.inspect(spec.container_name).running is True
    finally:
        supervisor.close()


def test_first_acquire_creates_starts_and_reports_new_context():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, clock=_Clock())

    lease = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)

    assert manager.created == 1
    assert manager.started == 1
    assert lease.context_status == "new"
    assert lease.activation_generation == 1
    assert supervisor.status("athr_1").busy is True


def test_existing_chat_never_creates_a_replacement_for_a_missing_container():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, clock=_Clock())

    with pytest.raises(SandboxError, match="missing"):
        supervisor.acquire(
            _spec(),
            owner="attempt:2",
            has_settled_context=True,
            expected_container_id="container:asbx_1",
        )

    assert manager.created == 0


def test_one_chat_cannot_be_admitted_by_two_attempts():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, clock=_Clock())
    first = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)

    same = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    with pytest.raises(SessionBusy):
        supervisor.acquire(_spec(), owner="attempt:2", has_settled_context=False)

    assert same.token == first.token


def test_stale_local_lease_cannot_settle_or_advance_process_generation():
    supervisor = ChatSessionSupervisor(_Manager(), clock=_Clock())
    lease = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)

    with pytest.raises(SessionLeaseLost):
        supervisor.settle(
            "athr_1",
            "wrong-token",
            job_id="ajob_1",
            context_status="new",
        )
    with pytest.raises(SessionLeaseLost):
        supervisor.note_process_started("athr_1", "wrong-token")

    assert supervisor.status("athr_1").busy is True
    assert lease.token


def test_settled_idle_timeout_stops_but_never_removes_the_container():
    manager = _Manager()
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=1800, clock=clock)
    lease = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    descriptor = supervisor.settle(
        "athr_1",
        lease.token,
        job_id="ajob_1",
        context_status="new",
    )

    assert descriptor.idle_deadline_monotonic == 1900.0
    clock.now = 1899.9
    assert supervisor.sweep() == []
    clock.now = 1900.0
    stopped = supervisor.sweep()

    assert len(stopped) == 1
    assert stopped[0].running is False
    assert stopped[0].docker_status == "stopped"
    assert manager.stopped == 1
    assert _spec().container_name in manager.items


def test_follow_up_is_live_in_a_running_chat_and_restored_after_stop():
    manager = _Manager()
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=10, clock=clock)
    first = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", first.token, job_id="ajob_1", context_status="new")

    live = supervisor.acquire(
        _spec(),
        owner="attempt:2",
        has_settled_context=True,
    )
    assert live.context_status == "live"
    supervisor.settle("athr_1", live.token, job_id="ajob_2", context_status="live")

    clock.now += 10
    supervisor.sweep()
    restored = supervisor.acquire(_spec(), owner="attempt:3", has_settled_context=True)

    assert restored.context_status == "restored"
    assert restored.container_id == first.container_id
    assert restored.activation_generation == first.activation_generation + 1


def test_an_active_turn_prevents_idle_stop_even_after_deadline():
    manager = _Manager()
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=10, clock=clock)
    first = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", first.token, job_id="ajob_1", context_status="new")
    supervisor.acquire(_spec(), owner="attempt:2", has_settled_context=True)

    clock.now += 100

    assert supervisor.sweep() == []
    assert manager.stopped == 0


def test_an_open_terminal_prevents_stop_and_closing_it_restarts_the_idle_clock():
    manager = _Manager()
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=10, clock=clock)
    first = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", first.token, job_id="ajob_1", context_status="new")
    terminal_token, descriptor = supervisor.terminal_opened("athr_1")

    assert descriptor.terminal_count == 1
    clock.now += 100
    assert supervisor.sweep() == []

    closed = supervisor.terminal_closed("athr_1", terminal_token)
    assert closed.idle_deadline_monotonic == clock.now + 10
    clock.now += 10
    assert len(supervisor.sweep()) == 1


def test_reaping_an_idle_terminal_does_not_start_a_second_chat_timeout():
    """Terminal idle and chat idle are one inactivity window, not two in series."""
    manager = _Manager()
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=10, clock=clock)
    first = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", first.token, job_id="ajob_1", context_status="new")
    terminal_token, _ = supervisor.terminal_opened("athr_1")
    last_activity = clock.now

    clock.now += 10
    closed = supervisor.terminal_closed(
        "athr_1",
        terminal_token,
        last_activity_monotonic=last_activity,
    )

    assert closed.idle_deadline_monotonic == clock.now
    assert len(supervisor.sweep()) == 1


def test_invalid_terminal_activity_timestamp_does_not_consume_the_token():
    manager = _Manager()
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=10, clock=clock)
    first = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", first.token, job_id="ajob_1", context_status="new")
    terminal_token, _ = supervisor.terminal_opened("athr_1")

    with pytest.raises(ValueError, match="last activity timestamp"):
        supervisor.terminal_closed(
            "athr_1",
            terminal_token,
            last_activity_monotonic=float("nan"),
        )

    assert supervisor.status("athr_1").terminal_count == 1
    assert supervisor.terminal_closed("athr_1", terminal_token).terminal_count == 0


def test_a_bounded_workspace_operation_prevents_stop_then_rearms_idle_timeout():
    manager = _Manager()
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=10, clock=clock)
    first = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", first.token, job_id="ajob_1", context_status="new")

    operation_token, descriptor = supervisor.operation_opened("athr_1")
    assert descriptor.running is True
    clock.now += 100
    assert supervisor.sweep() == []

    closed = supervisor.operation_closed("athr_1", operation_token)
    assert closed.idle_deadline_monotonic == clock.now + 10
    clock.now += 10
    assert len(supervisor.sweep()) == 1


def test_workspace_suspension_blocks_new_operations_and_drains_existing_one():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=10, clock=_Clock())
    lease = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", lease.token, job_id="ajob_1", context_status="new")
    operation_token, _ = supervisor.operation_opened("athr_1")

    supervisor.suspend_workspace("athr_1")
    with pytest.raises(SessionBusy):
        supervisor.operation_opened("athr_1")
    with pytest.raises(SessionBusy):
        supervisor.terminal_opened("athr_1")

    supervisor.operation_closed("athr_1", operation_token)
    drained = supervisor.wait_workspace_operations("athr_1", timeout_s=0.1)
    assert drained.operation_count == 0

    supervisor.resume_workspace("athr_1")
    new_token, _ = supervisor.operation_opened("athr_1")
    supervisor.operation_closed("athr_1", new_token)


def test_supervisor_restart_reconciles_without_stopping_or_removing_containers():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = (spec, manager._inspection(spec, running=True))
    clock = _Clock()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=30, clock=clock)

    found = supervisor.reconcile()

    assert len(found) == 1
    assert found[0].running is True
    assert found[0].idle_deadline_monotonic is None
    assert manager.started == manager.stopped == 0


def test_supervisor_restart_reconstructs_the_pinned_image_identity():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = (spec, manager._inspection(spec, running=False))
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=30, clock=_Clock())
    supervisor.reconcile()

    lease = supervisor.acquire(
        spec,
        owner="attempt:2",
        has_settled_context=True,
        expected_container_id="container:asbx_1",
    )

    assert lease.container_id == "container:asbx_1"
    assert manager.created == 0


def test_supervisor_restart_reconstructs_the_immutable_resource_policy():
    manager = _Manager()
    spec = replace(_spec(), memory_limit="1g", cpu_limit="1", stop_timeout_s=2)
    manager.items[spec.container_name] = (spec, manager._inspection(spec, running=False))
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=30, clock=_Clock())
    supervisor.reconcile()

    lease = supervisor.acquire(
        spec,
        owner="attempt:2",
        has_settled_context=True,
        expected_container_id="container:asbx_1",
    )

    assert lease.container_id == "container:asbx_1"
    assert manager.created == 0


def test_durable_idle_command_can_stop_a_reconciled_container_without_guessing():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = (spec, manager._inspection(spec, running=True))
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=30, clock=_Clock())
    supervisor.reconcile()

    stopped = supervisor.stop_if_idle(
        "athr_1",
        generation=1,
        activation_generation=9,
    )

    assert stopped.running is False
    assert stopped.activation_generation == 9
    assert manager.stopped == 1


def test_durable_lost_command_fences_and_stops_a_stale_local_owner():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=30, clock=_Clock())
    supervisor.acquire(
        _spec(),
        owner="attempt:1",
        has_settled_context=False,
        activation_generation=4,
    )
    supervisor.terminal_opened("athr_1")

    with pytest.raises(SessionBusy):
        supervisor.stop_if_idle(
            "athr_1",
            generation=1,
            activation_generation=4,
        )

    stopped = supervisor.stop_if_idle(
        "athr_1",
        generation=1,
        activation_generation=4,
        reason="lost",
    )

    assert stopped.running is False
    assert stopped.busy is False
    assert stopped.terminal_count == 0
    assert stopped.context_status == "lost"
    assert manager.stopped == 1


def test_abandon_stops_unsettled_activation_instead_of_arming_idle_timeout():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, idle_timeout_s=30, clock=_Clock())
    lease = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)

    abandoned = supervisor.abandon("athr_1", lease.token, lost=False)

    assert abandoned.running is False
    assert abandoned.busy is False
    assert abandoned.idle_deadline_monotonic is None
    assert manager.stopped == 1


def test_process_generation_is_separate_from_container_activation_generation():
    supervisor = ChatSessionSupervisor(_Manager(), clock=_Clock())
    lease = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)

    one = supervisor.note_process_started("athr_1", lease.token)
    two = supervisor.note_process_started("athr_1", lease.token)

    assert one.activation_generation == two.activation_generation == 1
    assert one.process_generation == 1
    assert two.process_generation == 2


def test_close_does_not_stop_retained_chat_state():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, sweep_interval_s=0.01, clock=_Clock())
    supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)

    supervisor.close()

    assert manager.stopped == 0
    assert manager.items[_spec().container_name][1].running is True


def test_durable_idle_stop_cannot_race_a_suspended_workspace():
    manager = _Manager()
    supervisor = ChatSessionSupervisor(manager, clock=_Clock())
    lease = supervisor.acquire(_spec(), owner="attempt:1", has_settled_context=False)
    supervisor.settle("athr_1", lease.token, job_id="ajob_1", context_status="new")
    supervisor.suspend_workspace("athr_1")

    with pytest.raises(SessionBusy, match="workspace is suspended"):
        supervisor.stop_if_idle(
            "athr_1",
            generation=1,
            activation_generation=1,
        )

    assert manager.stopped == 0
