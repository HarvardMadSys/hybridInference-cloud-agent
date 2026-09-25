"""Concurrency and capture barriers for shared live chat sandboxes."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.guest_observer import (
    GUEST_OBSERVER_SOURCE,
    GuestObservation,
    GuestObserverCursor,
)
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransport,
    ResidentTransportFactory,
)
from cloud_agent_host.sandbox import KATA_RUNTIME, ProcessBackend, SandboxError
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
)
from cloud_agent_host.tool_windows import ToolMark
from cloud_agent_host.workspace_broker import SharedSandboxIdentityRequest, create_app
from cloud_agent_shared import operation_profiling


class _Manager:
    def __init__(self) -> None:
        self.backend = SimpleNamespace(
            uid=10001,
            gid=10001,
            image="sha256:image",
            runtime="runc",
        )
        self.items: dict[str, ChatSandboxInspection] = {}
        self.events: list[object] = []
        self.starts: list[str] = []
        self.stop_count = 0
        # The host-rule check: how many of the next checks fail, and how many ran.
        self.host_rule_failures = 0
        self.host_rule_checks = 0
        self.pruned_specs: list[ChatSandboxSpec] = []
        self.process_baseline: tuple[tuple[int, int], ...] = ((41, 400),)

    @staticmethod
    def _inspection(spec: ChatSandboxSpec, *, running: bool = True) -> ChatSandboxInspection:
        return ChatSandboxInspection(
            container_id="container-shared",
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
        facts: Any = None,
        observer: Any = None,
    ) -> ChatSandboxInspection:
        created = spec.container_name not in self.items
        # Instrumented like the real manager, so a test can exercise the stage
        # path rather than only the action path.
        with operation_profiling.substage(observer, "container_create"):
            inspection = self.items.setdefault(spec.container_name, self._inspection(spec))
        if created and facts is not None:
            # Mirrors the real manager: only a container this call actually
            # made is reported as created.
            facts.note("create")
        return inspection

    def require_existing(
        self, spec: ChatSandboxSpec, *, container_id: str
    ) -> ChatSandboxInspection:
        current = self.items[spec.container_name]
        assert current.container_id == container_id
        return current

    def inspect(self, container_name: str) -> ChatSandboxInspection | None:
        return self.items.get(container_name)

    def list(self) -> list[ChatSandboxInspection]:
        return list(self.items.values())

    def start(
        self,
        spec: ChatSandboxSpec,
        *,
        facts: Any = None,
        observer: Any = None,
    ) -> ChatSandboxInspection:
        self.starts.append(spec.container_name)
        current = self.items[spec.container_name]
        if not current.running:
            with operation_profiling.substage(observer, "container_start"):
                current = replace(
                    current,
                    running=True,
                    status=ChatSandboxStatus.RUNNING,
                )
                self.items[spec.container_name] = current
            if facts is not None:
                facts.note("start")
        elif facts is not None:
            with operation_profiling.substage(observer, "service_hosts_sync"):
                facts.note("reuse")
        return current

    def stop(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        self.stop_count += 1
        current = replace(
            self.items[spec.container_name],
            running=False,
            status=ChatSandboxStatus.STOPPED,
            networks=(),
        )
        self.items[spec.container_name] = current
        return current

    def prune_orphan_networks(self, specs: list[ChatSandboxSpec]) -> None:
        self.pruned_specs = specs

    def verify_host_rules(self) -> None:
        self.host_rule_checks += 1
        if self.host_rule_failures:
            self.host_rule_failures -= 1
            raise SandboxError("inet cloud_agent_x is missing chain 'from_group'")

    def freeze_shared_writers(
        self, spec: ChatSandboxSpec, *, slot_ids: list[int], token: str
    ) -> str:
        self.events.append(("freeze", tuple(sorted(slot_ids))))
        return f"/run/agent-activation/snapshot-{token}.pids"

    def thaw_shared_writers(
        self, spec: ChatSandboxSpec, *, slot_ids: list[int], manifest: str
    ) -> None:
        self.events.append(("thaw", tuple(sorted(slot_ids)), manifest))

    def capture_member_processes(self, spec: ChatSandboxSpec, **kwargs):
        self.events.append(("census", kwargs["slot_id"]))
        return self.process_baseline

    def reap_member_processes_except(self, spec: ChatSandboxSpec, **kwargs) -> None:
        self.events.append(("reap-except", kwargs["slot_id"], kwargs["baseline"]))

    def normalize_member_files(self, spec: ChatSandboxSpec, **kwargs) -> None:
        self.events.append(("normalize", kwargs["slot_id"]))

    def begin_guest_observation(
        self,
        spec: ChatSandboxSpec,
        *,
        user_uid: int,
        measurement_id: str | None = None,
    ) -> GuestObserverCursor:
        self.events.append(("observe-begin", user_uid))
        return GuestObserverCursor(
            uid=user_uid,
            available=True,
            measurement_id=measurement_id,
        )

    def member_window_request(self, spec: ChatSandboxSpec, argv: list[str], *, timeout_s: float):
        """Accept the fixed window verbs without a real guest helper."""
        self.events.append(("member-window", argv[1]))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    def finish_guest_observation(
        self, spec: ChatSandboxSpec, cursor: GuestObserverCursor | None
    ) -> GuestObservation:
        self.events.append(("observe-finish", None if cursor is None else cursor.uid))
        return GuestObservation(
            source="kata-guest-ebpf-v1",
            complete=True,
            lost_events=0,
            truncated=False,
        )


class _Turn:
    def __init__(self) -> None:
        self.interrupted = False

    def interrupt(self) -> None:
        self.interrupted = True


class _Transport:
    retire_after_turn = False

    def __init__(self, launch: ResidentLaunch) -> None:
        self.launch = launch
        self.native_session_id = launch.native_session_id or "native-child"
        self.alive = True
        self.turns: list[_Turn] = []

    def compatible_with(self, launch: ResidentLaunch) -> bool:
        # Mirror ResidentTransport: the renewable credential and its generation
        # are not ordinary config equality. The supervisor must separately
        # force a restart when the residency fence rotates.
        return (
            replace(
                self.launch,
                credential=launch.credential,
                residency_generation=launch.residency_generation,
            )
            == launch
        )

    def close(self) -> None:
        self.alive = False

    def start_turn(self, **_kwargs) -> _Turn:
        turn = _Turn()
        self.turns.append(turn)
        return turn


class _TransportFactory:
    def __init__(self) -> None:
        self.opened: list[ResidentLaunch] = []
        self.transports: list[_Transport] = []

    def open(self, spec: ChatSandboxSpec, launch: ResidentLaunch) -> _Transport:
        self.opened.append(launch)
        transport = _Transport(launch)
        self.transports.append(transport)
        return transport

    def fork_native(self, *_args, **_kwargs) -> str:
        raise AssertionError("this test did not configure native fork behavior")


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_source",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image",
        shared_live=True,
        resident_slot_capacity=4,
    )


def _launch(thread_id: str, *, native_session_id: str | None, slot: int) -> ResidentLaunch:
    return ResidentLaunch(
        thread_id=thread_id,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        native_session_id=native_session_id,
        model="glm-5.1",
        reasoning_effort=None,
        gateway_base_url="http://gateway",
        credential="grant",
        slot_id=slot,
        residency_generation=slot + 1,
    )


def _members() -> list[SharedMemberManifest]:
    return [
        SharedMemberManifest(
            thread_id="athr_source",
            native_session_id="native-source",
            context_status="live",
            resident_slot_id=0,
            last_settled_job_id="ajob_source",
        ),
        SharedMemberManifest(
            thread_id="athr_peer",
            native_session_id="native-peer",
            context_status="live",
            resident_slot_id=1,
            last_settled_job_id="ajob_peer",
        ),
    ]


def test_broker_reconcile_stops_orphan_processes_before_accepting_requests():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    supervisor = SharedChatSessionSupervisor(manager)

    descriptors = supervisor.reconcile()

    assert manager.stop_count == 1
    assert manager.items[spec.container_name].running is False
    assert manager.pruned_specs == [spec]
    assert descriptors[0].running is False


def test_status_listing_never_stops_a_managed_running_group():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )
    supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )

    descriptors = supervisor.list()

    assert manager.stop_count == 0
    assert manager.items[spec.container_name].running is True
    assert next(item for item in descriptors if item.thread_id == "athr_source").group_busy is True


def test_status_retries_network_cleanup_for_an_externally_stopped_group():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )
    manager.items[spec.container_name] = manager._inspection(spec, running=False)

    descriptors = supervisor.list()

    assert manager.stop_count == 1
    assert manager.items[spec.container_name].networks == ()
    assert all(item.running is False for item in descriptors)


def test_acquire_starts_a_settled_context_on_its_group():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )

    assert manager.starts == [_spec().container_name]


def _running_group() -> tuple[_Manager, SharedChatSessionSupervisor]:
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    return manager, supervisor


def test_a_host_that_cannot_show_its_rules_stops_every_running_group():
    """Fail closed: a group may not go on without the boundary it started behind."""
    manager, supervisor = _running_group()
    manager.host_rule_failures = 2

    stopped = supervisor.guard_network()

    assert manager.host_rule_checks == 2
    assert manager.stop_count == 1
    assert manager.items[_spec().container_name].running is False
    # Stopped, not lost: the native session wakes once the rules are back.
    assert [descriptor.resident_state for descriptor in stopped] == ["stopped"]
    assert supervisor.status("athr_source").context_status != "lost"


def test_one_failed_check_is_retried_before_anything_is_stopped():
    manager, supervisor = _running_group()
    manager.host_rule_failures = 1

    assert supervisor.guard_network() == []
    assert manager.host_rule_checks == 2
    assert manager.stop_count == 0


def test_the_rules_are_not_checked_while_nothing_runs():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    assert supervisor.guard_network() == []
    assert manager.host_rule_checks == 0


def test_stale_activation_does_not_wake_a_stopped_shared_container():
    manager = _Manager()
    spec = _spec()
    manager.create(spec)
    manager.stop(spec)
    members = [
        replace(member, activation_generation=2) if member.thread_id == "athr_source" else member
        for member in _members()
    ]
    supervisor = SharedChatSessionSupervisor(manager)

    with pytest.raises(SessionLeaseLost, match="activation generation is stale"):
        supervisor.acquire(
            spec,
            thread_id="athr_source",
            owner="attempt:stale",
            activation_generation=1,
            has_settled_context=True,
            manifest=members,
            expected_container_id="container-shared",
        )

    assert manager.starts == []
    assert manager.items[spec.container_name].running is False


def test_invalid_terminal_close_keeps_reference_for_a_valid_retry():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(
        manager,
        idle_timeout_s=30,
        clock=lambda: 100.0,
    )
    supervisor.register_manifest(_spec(), _members())
    token, opened = supervisor.terminal_opened("athr_source")
    assert opened.terminal_count == 1

    with pytest.raises(ValueError, match="timestamp is invalid"):
        supervisor.terminal_closed(
            "athr_source",
            token,
            last_activity_monotonic=float("inf"),
        )

    retained = supervisor.status("athr_source")
    assert retained.terminal_count == 1
    assert retained.idle_deadline_monotonic is None

    closed = supervisor.terminal_closed(
        "athr_source",
        token,
        last_activity_monotonic=90.0,
    )
    assert closed.terminal_count == 0
    assert closed.idle_deadline_monotonic == 120.0


def test_native_fork_journal_uses_the_docker_layer_owner_not_capabilityless_root():
    class JournalManager:
        def __init__(self) -> None:
            self.backend = SimpleNamespace(uid=10001, gid=10001)
            self.calls: list[tuple[list[str], dict]] = []

        def exec_capture(self, _spec, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    manager = JournalManager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]

    factory._write_fork_journal(_spec(), "fork-one", {"state": "preparing"})
    assert factory._read_fork_journal(_spec(), "fork-one") is None

    assert [call[1]["user_uid"] for call in manager.calls] == [10001, 10001]
    assert [call[1]["user_gid"] for call in manager.calls] == [10001, 10001]
    assert all(
        "/home/agent/.freeinference-platform/session-forks" in " ".join(call[0])
        for call in manager.calls
    )


@pytest.mark.parametrize(
    ("runtime_id", "collection"),
    [
        ("codex", "codex-members"),
        ("opencode", "opencode-members"),
        ("kilo", "kilo-members"),
    ],
)
def test_private_provider_members_get_deterministic_distinct_homes(
    runtime_id: str, collection: str
):
    source = _launch("athr_source", native_session_id="native-source", slot=0)
    child = replace(source, thread_id="athr_child", native_session_id="native-child", slot_id=1)

    source_home = ResidentTransportFactory._member_state_dir(replace(source, runtime_id=runtime_id))
    child_home = ResidentTransportFactory._member_state_dir(replace(child, runtime_id=runtime_id))

    assert source_home.startswith(f"/agent-state/{collection}/")
    assert child_home.startswith(f"/agent-state/{collection}/")
    assert source_home != child_home


def test_codex_preparing_journal_recovers_one_persisted_child_without_refork(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id="codex", runtime_version="0.145.0")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    mutation_calls: list[str] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **_kwargs: mutation_calls.append("fork") or "duplicate"
        ),
        close=lambda: mutation_calls.append("close"),
    )
    expected = {
        "operation_id": "fork-recover",
        "sandbox_id": spec.sandbox_id,
        "sandbox_generation": spec.generation,
        "runtime_id": "codex",
        "runtime_version": "0.145.0",
        "source_session_id": "native-source",
        "source_native_turn_no": 2,
        "target_thread_id": "athr_child",
        "target_slot_id": 2,
    }
    writes: list[dict[str, object]] = []
    clones: list[tuple[ResidentLaunch, ResidentLaunch]] = []
    monkeypatch.setattr(
        factory,
        "_read_fork_journal",
        lambda *_args: {
            **expected,
            "state": "preparing",
            "source_child_session_ids": ["native-old-child"],
        },
    )
    monkeypatch.setattr(
        factory,
        "_codex_fork_children",
        lambda *_args, **_kwargs: {"native-old-child", "native-recovered-child"},
    )
    monkeypatch.setattr(
        factory,
        "_clone_private_member_state",
        lambda _spec, source_item, target_item: clones.append((source_item, target_item)),
    )
    monkeypatch.setattr(
        factory,
        "_write_fork_journal",
        lambda _spec, _operation_id, value: writes.append(value),
    )

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-recover",
        target_launch,
        2,
    )

    assert child_id == "native-recovered-child"
    assert mutation_calls == ["close"]
    assert clones == [(source_launch, target_launch)]
    assert writes == [{**expected, "state": "ready", "target_session_id": "native-recovered-child"}]


def test_codex_lost_fork_response_recovers_from_the_rollout_inventory(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id="codex", runtime_version="0.145.0")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id="codex",
        runtime_version="0.145.0",
    )
    closed: list[bool] = []

    def lost_response(**_kwargs):
        raise EOFError("response disappeared after persistence")

    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(fork_native=lost_response),
        close=lambda: closed.append(True),
    )
    inventories = iter(
        [
            {"native-old-child"},
            {"native-old-child", "native-recovered-child"},
        ]
    )
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(
        factory,
        "_codex_fork_children",
        lambda *_args, **_kwargs: next(inventories),
    )
    monkeypatch.setattr(factory, "_clone_private_member_state", lambda *_args: None)
    monkeypatch.setattr(
        factory,
        "_write_fork_journal",
        lambda _spec, _operation_id, value: writes.append(value),
    )

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-lost-response",
        target_launch,
        2,
    )

    assert child_id == "native-recovered-child"
    assert closed == [True]
    assert writes[0]["state"] == "preparing"
    assert writes[0]["source_child_session_ids"] == ["native-old-child"]
    assert writes[1]["state"] == "ready"
    assert writes[1]["target_session_id"] == "native-recovered-child"


@pytest.mark.parametrize("runtime_id", ["opencode", "kilo"])
def test_http_native_fork_flushes_and_clones_private_provider_state(monkeypatch, runtime_id: str):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id=runtime_id, runtime_version="test-version")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id=runtime_id,
        runtime_version="test-version",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id=runtime_id,
        runtime_version="test-version",
    )
    lifecycle: list[str] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **_kwargs: lifecycle.append("fork") or "native-child"
        ),
        close=lambda: lifecycle.append("close"),
    )
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(
        factory,
        "_clear_http_member_locks",
        lambda _spec, launch: lifecycle.append(f"clear:{launch.thread_id}"),
    )
    monkeypatch.setattr(
        factory,
        "_clone_private_member_state",
        lambda _spec, source_item, target_item: lifecycle.append(
            f"clone:{source_item.thread_id}:{target_item.thread_id}"
        ),
    )
    monkeypatch.setattr(
        factory,
        "_write_fork_journal",
        lambda _spec, _operation_id, value: writes.append(value),
    )

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-http",
        target_launch,
        2,
    )

    assert child_id == "native-child"
    assert lifecycle == [
        "fork",
        "close",
        "clear:athr_source",
        "clone:athr_source:athr_child",
    ]
    assert manager.events == [("normalize", 0), ("normalize", 2)]
    assert writes[0]["state"] == "preparing"
    assert writes[1]["state"] == "ready"


def test_pi_fork_exposes_the_source_owned_child_session_to_its_target(monkeypatch):
    manager = _Manager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    spec = replace(_spec(), runtime_id="pi", runtime_version="0.73.1")
    source_launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        runtime_id="pi",
        runtime_version="0.73.1",
    )
    target_launch = replace(
        _launch("athr_child", native_session_id=None, slot=2),
        runtime_id="pi",
        runtime_version="0.73.1",
    )
    fork_calls: list[dict] = []
    closed: list[bool] = []
    source = SimpleNamespace(
        native_session_id="native-source",
        launch=source_launch,
        session=SimpleNamespace(
            fork_native=lambda **kwargs: fork_calls.append(kwargs) or "native-child"
        ),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(factory, "_read_fork_journal", lambda *_args: None)
    monkeypatch.setattr(factory, "_write_fork_journal", lambda *_args: None)

    child_id = factory.fork_native(
        spec,
        source,  # type: ignore[arg-type]
        "fork-pi",
        target_launch,
        2,
    )

    assert child_id == "native-child"
    assert fork_calls[0]["source_native_turn_no"] == 2
    assert closed == [True]
    # Pi mutates from the live source process, so the new child file is owned
    # by slot 0 even though slot 2 will subsequently resume it.
    assert manager.events == [("normalize", 0)]


@pytest.mark.parametrize("runtime_id", ["opencode", "kilo"])
def test_http_member_lock_cleanup_runs_as_the_source_slot_owner(runtime_id: str):
    class LockManager:
        def __init__(self) -> None:
            self.backend = SimpleNamespace(gid=10001)
            self.calls: list[tuple[list[str], dict]] = []

        def exec_capture(self, _spec, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    manager = LockManager()
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=3),
        runtime_id=runtime_id,
        runtime_version="test-version",
    )

    factory._clear_http_member_locks(_spec(), launch)

    assert len(manager.calls) == 1
    argv, kwargs = manager.calls[0]
    assert argv[-1] == runtime_id
    assert argv[-2] == factory._member_state_dir(launch)
    assert "locks" in argv[2]
    assert kwargs["user_uid"] == 11004
    assert kwargs["user_gid"] == 10001


def test_snapshot_is_a_short_group_barrier_and_always_thaws_writers():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    def capture(_backend):
        manager.events.append("capture")
        with pytest.raises(SessionBusy, match="snapshot"):
            supervisor.operation_opened("athr_peer")
        return {"patch": "diff --git a/a b/a\n"}

    result = supervisor.capture_snapshot("athr_source", capture=capture)

    assert result == {"patch": "diff --git a/a b/a\n"}
    assert manager.events[0] == ("freeze", (0, 1))
    assert manager.events[1] == "capture"
    assert manager.events[2][0] == "thaw"
    assert supervisor.status("athr_source").snapshotting is False


def test_snapshot_refuses_active_mutations_instead_of_serializing_normal_work():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    token, _ = supervisor.operation_opened("athr_peer")

    with pytest.raises(SessionBusy, match="active mutations"):
        supervisor.capture_snapshot("athr_source", capture=lambda _backend: {"patch": ""})

    supervisor.operation_closed("athr_peer", token)
    assert supervisor.capture_snapshot("athr_source", capture=lambda _backend: {"patch": ""}) == {
        "patch": ""
    }


def test_snapshot_starts_the_exact_idle_stopped_container_and_rearms_idle():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, idle_timeout_s=30)
    supervisor.register_manifest(_spec(), _members())
    before = manager.stop(_spec())

    result = supervisor.capture_snapshot(
        "athr_source",
        capture=lambda _backend: {"patch": "diff --git a/a b/a\n"},
    )

    after = supervisor.status("athr_source")
    assert result == {"patch": "diff --git a/a b/a\n"}
    assert after.container_id == before.container_id
    assert after.running is True
    assert after.snapshotting is False
    assert after.idle_deadline_monotonic is not None
    assert manager.events[0] == ("freeze", (0, 1))
    assert manager.events[-1][0] == "thaw"


def test_snapshot_start_failure_releases_the_admission_barrier():
    class StartFailureManager(_Manager):
        def start(self, spec, **_kwargs):
            raise SandboxError("docker start failed")

    manager = StartFailureManager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    manager.stop(_spec())

    with pytest.raises(SandboxError, match="docker start failed"):
        supervisor.capture_snapshot("athr_source", capture=lambda _backend: {"patch": ""})

    status = supervisor.status("athr_source")
    assert status.running is False
    assert status.snapshotting is False


@pytest.mark.parametrize("reserved_child", [False, True])
def test_native_fork_tracks_activity_but_does_not_lock_a_sibling_operation(reserved_child):
    manager = _Manager()
    factory = _TransportFactory()
    supervisor: SharedChatSessionSupervisor

    def native_forker(spec, source_transport, operation_id, target_launch, source_native_turn_no):
        assert source_transport.native_session_id == "native-source"
        assert operation_id == "fork-one"
        assert source_native_turn_no == 1
        token, peer = supervisor.operation_opened("athr_peer")
        assert peer.operation_count == 1
        assert supervisor.status("athr_source").operation_count == 1
        supervisor.operation_closed("athr_peer", token)
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        native_forker=native_forker,
    )
    supervisor.register_manifest(_spec(), _members())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    source_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, source_launch)
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )

    # The control plane reserves the child membership before asking the host
    # to fork. It has a slot and a residency, but no native session yet.
    pending_manifest = [
        *_members(),
        SharedMemberManifest(
            thread_id="athr_child",
            resident_slot_id=2,
            residency_generation=3,
        ),
    ]
    if reserved_child:
        supervisor.register_manifest(_spec(), pending_manifest)

    result = supervisor.fork_native(
        "athr_source",
        operation_id="fork-one",
        source_native_session_id="native-source",
        source_native_turn_no=1,
        target_thread_id="athr_child",
        target_slot_id=2,
        source_launch=source_launch,
        target_launch=_launch("athr_child", native_session_id=None, slot=2),
    )

    assert result.container_id == "container-shared"
    assert result.target_native_session_id == "native-child"
    assert supervisor.status("athr_child").resident_state == "live"
    assert supervisor.status("athr_source").operation_count == 0
    assert supervisor.status("athr_source").idle_deadline_monotonic is not None

    if reserved_child:
        # A concurrent Files/Git request can still carry the pending database
        # manifest until the fork receipt is finalized by the control plane.
        supervisor.register_manifest(_spec(), pending_manifest)
        child = supervisor.status("athr_child")
        assert child.native_session_id == "native-child"
        assert child.resident_state == "live"
        assert child.residency_generation == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("already_registered", [False, True])
async def test_broker_fork_materializes_reserved_target_and_replays_stale_manifest(
    already_registered,
):
    """A durable child reservation must neither block Fork nor erase its retry."""
    manager = _Manager()
    factory = _TransportFactory()
    fork_calls: list[str] = []

    def native_forker(_spec, _transport, operation_id, _launch, _turn_no):
        fork_calls.append(operation_id)
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager, transport_factory=factory, native_forker=native_forker
    )
    spec = _spec()
    reserved = SharedMemberManifest(thread_id="athr_child", resident_slot_id=2)
    members = [*_members(), reserved]
    supervisor.register_manifest(spec, members if already_registered else _members())
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="broker-secret",
        shared_session_supervisor=supervisor,
    )
    body = {
        "sandbox_id": spec.sandbox_id,
        "generation": spec.generation,
        "origin_thread_id": spec.thread_id,
        "runtime_id": spec.runtime_id,
        "runtime_version": spec.runtime_version,
        "sandbox_image_digest": spec.sandbox_image_digest,
        "expected_container_id": "container-shared",
        "resident_slot_capacity": spec.resident_slot_capacity,
        "memory_limit": spec.memory_limit,
        "cpu_limit": spec.cpu_limit,
        "stop_timeout_s": spec.stop_timeout_s,
        "members": [asdict(member) for member in members],
        "operation_id": "fork-reserved-target",
        "source_native_session_id": "native-source",
        "source_native_turn_no": 1,
        "target_thread_id": "athr_child",
        "target_slot_id": 2,
        "source_launch": asdict(_launch("athr_source", native_session_id="native-source", slot=0)),
        "target_launch": asdict(_launch("athr_child", native_session_id=None, slot=2)),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        first = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=body,
        )
        assert first.status_code == 200, first.text
        # Files/Git hydration can register the still-pending database manifest
        # after the host has created the child but before receipt finalization.
        registered = await client.post(
            "/shared-sandboxes/athr_source/register",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=SharedSandboxIdentityRequest.model_validate(body).model_dump(),
        )
        assert registered.status_code == 200, registered.text
        # The control plane can retry before storing the receipt, so the
        # durable target manifest still contains no native session identity.
        body["source_launch"]["residency_generation"] = 10
        body["target_launch"]["residency_generation"] = 11
        retried = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=body,
        )
        assert retried.status_code == 200, retried.text

        collision = {
            **body,
            "members": [
                *[asdict(member) for member in _members()],
                asdict(replace(reserved, native_session_id="native-child")),
            ],
            "target_thread_id": "athr_collision",
            "target_slot_id": 1,  # The unaffected peer already owns this slot.
            "target_launch": {**body["target_launch"], "slot_id": 1},
        }
        refused = await client.post(
            "/shared-sandboxes/athr_source/fork",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=collision,
        )
        assert refused.status_code == 409, refused.text
        assert "slot is already occupied" in refused.json()["detail"]

    assert fork_calls == ["fork-reserved-target"]
    assert first.json()["target_native_session_id"] == "native-child"
    assert retried.json()["target_native_session_id"] == "native-child"
    assert retried.json()["target_residency_generation"] == 11
    assert supervisor.status("athr_child").resident_state == "live"
    assert supervisor.status("athr_peer").native_session_id == "native-peer"


def test_native_fork_retry_rotates_only_source_and_hidden_child_processes():
    manager = _Manager()
    factory = _TransportFactory()
    fork_calls: list[str] = []

    def native_forker(
        _spec, _source_transport, operation_id, _target_launch, _source_native_turn_no
    ):
        fork_calls.append(operation_id)
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        native_forker=native_forker,
    )
    supervisor.register_manifest(_spec(), _members())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    source_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, source_launch)
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    target_launch = _launch("athr_child", native_session_id=None, slot=2)
    first = supervisor.fork_native(
        "athr_source",
        operation_id="fork-retry",
        source_native_session_id="native-source",
        source_native_turn_no=1,
        target_thread_id="athr_child",
        target_slot_id=2,
        source_launch=source_launch,
        target_launch=target_launch,
    )

    retried = supervisor.fork_native(
        "athr_source",
        operation_id="fork-retry",
        source_native_session_id="native-source",
        source_native_turn_no=1,
        target_thread_id="athr_child",
        target_slot_id=2,
        source_launch=replace(source_launch, credential="grant-rotated", residency_generation=10),
        target_launch=replace(target_launch, credential="grant-rotated", residency_generation=11),
    )

    assert fork_calls == ["fork-retry"]
    assert first.source_process_generation == first.target_process_generation == 1
    assert retried.source_process_generation == retried.target_process_generation == 2
    assert retried.source_residency_generation == 10
    assert retried.target_residency_generation == 11
    assert [launch.residency_generation for launch in factory.opened] == [1, 3, 10, 11]


@pytest.mark.parametrize("observation", ["none", "status", "list", "residency_lost"])
def test_native_fork_keeps_source_transport_ownership_during_cli_restart(observation):
    manager = _Manager()
    factory = _TransportFactory()
    observations = []

    def native_forker(
        _spec, source_transport, _operation_id, _target_launch, _source_native_turn_no
    ):
        source_transport.alive = False

        # HTTP-backed CLIs close the source while persisting/copying the
        # native fork. Poll from another thread in that exact window, before
        # the forker returns and the supervisor resumes the source.
        def observe():
            if observation == "status":
                observations.append(supervisor.status("athr_source"))
            elif observation == "list":
                observations.extend(supervisor.list())
            elif observation == "residency_lost":
                observations.append(supervisor.residency_lost("athr_source", generation=1))

        observer = threading.Thread(target=observe)
        observer.start()
        observer.join(timeout=2)
        assert not observer.is_alive(), "status polling blocked on the native fork"
        return "native-child"

    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory,
        native_forker=native_forker,
    )
    supervisor.register_manifest(_spec(), _members())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        expected_container_id="container-shared",
    )
    source_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, source_launch)
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )

    fork_args = {
        "operation_id": "fork-closes-source",
        "source_native_session_id": "native-source",
        "source_native_turn_no": 1,
        "target_thread_id": "athr_child",
        "target_slot_id": 2,
        "source_launch": source_launch,
        "target_launch": _launch("athr_child", native_session_id=None, slot=2),
    }
    if observation == "residency_lost":
        # A real grant revocation must still detach the source and reject
        # this fork; the observational race fix cannot bypass that fence.
        with pytest.raises(SessionLeaseLost, match="source transport changed after native fork"):
            supervisor.fork_native("athr_source", **fork_args)
        assert factory.opened == [source_launch]
        assert supervisor.status("athr_source").residency_generation == 0
        assert all(item.thread_id != "athr_child" for item in supervisor.list())
        return

    result = supervisor.fork_native("athr_source", **fork_args)

    child_launch = _launch("athr_child", native_session_id="native-child", slot=2)
    assert result.source_process_generation == 2
    assert supervisor.status("athr_source").resident_state == "live"
    assert factory.opened == [source_launch, source_launch, child_launch]
    if observation != "none":
        source_observation = next(item for item in observations if item.thread_id == "athr_source")
        assert source_observation.operation_count == 1
        assert source_observation.resident_state == "live"
        assert source_observation.residency_generation == 1


def test_idle_stop_releases_networks_after_docker_already_stopped_the_container():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=False)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(
        spec,
        _members(),
        expected_container_id="container-shared",
    )

    stopped = supervisor.stop_if_idle("athr_source", generation=spec.generation)

    # The real manager's idempotent stop path releases any phase bridge still
    # attached after an external stop or a partially failed prior cleanup.
    assert manager.stop_count == 1
    assert stopped.running is False
    assert manager.items[spec.container_name].networks == ()


def test_new_activation_waits_for_an_inflight_idle_stop_then_restarts_exact_container():
    class BlockingStopManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.stop_entered = threading.Event()
            self.allow_stop = threading.Event()

        def stop(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
            self.stop_entered.set()
            assert self.allow_stop.wait(timeout=2)
            return super().stop(spec)

    manager = BlockingStopManager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    manifest = _members()
    manager.create(_spec())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=manifest,
        expected_container_id="container-shared",
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )

    stopped: list[object] = []
    errors: list[BaseException] = []

    def stop_group() -> None:
        try:
            stopped.append(supervisor.stop_if_idle("athr_source", generation=1))
        except BaseException as exc:  # pragma: no cover - assertion reports it below
            errors.append(exc)

    stop_thread = threading.Thread(target=stop_group)
    stop_thread.start()
    assert manager.stop_entered.wait(timeout=1)
    with pytest.raises(SessionBusy, match="stop"):
        supervisor.operation_opened("athr_peer")

    acquired: list[object] = []

    def wake_group() -> None:
        try:
            acquired.append(
                supervisor.acquire(
                    _spec(),
                    thread_id="athr_source",
                    owner="attempt:2",
                    activation_generation=2,
                    has_settled_context=True,
                    manifest=manifest,
                    expected_container_id="container-shared",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it below
            errors.append(exc)

    wake_thread = threading.Thread(target=wake_group)
    wake_thread.start()
    wake_thread.join(timeout=0.05)
    assert wake_thread.is_alive(), "new activation bypassed the stop admission barrier"

    manager.allow_stop.set()
    stop_thread.join(timeout=1)
    wake_thread.join(timeout=1)

    assert errors == []
    assert len(stopped) == len(acquired) == 1
    assert acquired[0].container_id == "container-shared"
    assert acquired[0].context_status == "restored"
    assert manager.items[_spec().container_name].running is True


def test_settlement_ack_replays_only_for_the_same_completed_activation():
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    original = supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    assert (
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_source",
            context_status="live",
        )
        == original
    )
    assert len(factory.transports) == 1
    for token, job_id, status in [
        ("wrong-token", "ajob_source", "live"),
        (lease.token, "ajob_other", "live"),
        (lease.token, "ajob_source", "restored"),
    ]:
        with pytest.raises(SessionLeaseLost):
            supervisor.settle("athr_source", token, job_id=job_id, context_status=status)

    successor = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    with pytest.raises(SessionLeaseLost):
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_source",
            context_status="live",
        )
    assert supervisor.status("athr_source").busy
    supervisor.abandon("athr_source", successor.token, lost=False)
    with pytest.raises(SessionLeaseLost):
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_source",
            context_status="live",
        )


def test_status_downgrades_a_dead_resident_process_without_losing_native_context():
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor.settle(
        "athr_source",
        lease.token,
        job_id="ajob_source",
        context_status="live",
    )
    factory.transports[-1].alive = False

    descriptor = supervisor.status("athr_source")

    assert descriptor.resident_state == "restarting"
    assert descriptor.context_status == "live"
    assert descriptor.native_session_id == "native-source"


def test_process_fixed_config_restart_reports_restored_instead_of_hot():
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", first.token, launch)
    supervisor.settle(
        "athr_source",
        first.token,
        job_id="ajob_source",
        context_status="live",
    )

    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    assert second.context_status == "live"

    restarted = supervisor.ensure_resident(
        "athr_source",
        second.token,
        replace(
            launch,
            model="glm-5.2",
            credential="grant-2",
            residency_generation=2,
        ),
    )

    assert restarted.process_generation == 2
    assert restarted.context_status == "restored"


def test_retry_retirement_settles_native_locator_then_restores_same_session():
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=[
            replace(
                _members()[0],
                native_session_id=None,
                last_settled_job_id=None,
                context_status="new",
            ),
            _members()[1],
        ],
    )
    supervisor.ensure_resident(
        "athr_source", first.token, _launch("athr_source", native_session_id=None, slot=0)
    )
    transport = factory.transports[-1]
    transport.retire_after_turn = True
    settled = supervisor.settle(
        "athr_source", first.token, job_id="ajob_first", context_status="new"
    )
    assert not transport.alive
    assert settled.native_session_id == "native-child"
    assert settled.resident_state == "restarting"
    assert settled.last_settled_job_id == "ajob_first"
    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=[
            replace(
                _members()[0],
                native_session_id=settled.native_session_id,
                last_settled_job_id=settled.last_settled_job_id,
                context_status=settled.context_status,
            ),
            _members()[1],
        ],
    )
    assert second.context_status == "restored"
    restored = supervisor.ensure_resident(
        "athr_source",
        second.token,
        replace(
            _launch("athr_source", native_session_id=settled.native_session_id, slot=0),
            residency_generation=2,
        ),
    )
    assert restored.native_session_id == "native-child"
    assert restored.process_generation == 2
    assert restored.context_status == "restored"


def test_unused_mcp_token_does_not_change_live_process_compatibility():
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert transport.compatible_with(
        replace(launch, mcp_base_url="http://relay", mcp_credential="fresh-attempt-token")
    )


def test_missing_launch_locator_restores_the_known_member_instead_of_starting_fresh():
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    restored = supervisor.ensure_resident(
        "athr_source", lease.token, _launch("athr_source", native_session_id=None, slot=0)
    )
    assert factory.opened[-1].native_session_id == "native-source"
    assert restored.native_session_id == "native-source"
    assert restored.context_status == "restored"


@pytest.mark.parametrize("runtime", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_wrong_native_resume_is_rejected_without_replacing_the_saved_locator(runtime):
    class WrongResumeFactory(_TransportFactory):
        def open(self, spec, launch):
            transport = super().open(spec, launch)
            transport.native_session_id = "unexpected-new-session"
            return transport

    factory = WrongResumeFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    with pytest.raises(SandboxError, match="different native session"):
        supervisor.ensure_resident(
            "athr_source",
            lease.token,
            replace(
                _launch("athr_source", native_session_id="native-source", slot=0),
                runtime_id=runtime,
            ),
        )
    assert not factory.transports[-1].alive
    assert not factory.transports[-1].turns
    assert supervisor.status("athr_source").native_session_id == "native-source"


def test_settlement_cannot_overwrite_the_previously_retained_native_session():
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    factory.transports[-1].native_session_id = "unexpected-new-session"
    with pytest.raises(SandboxError, match="different native session"):
        supervisor.settle(
            "athr_source",
            lease.token,
            job_id="ajob_bad",
            context_status="restored",
            native_session_id="unexpected-new-session",
        )
    descriptor = supervisor.status("athr_source")
    assert descriptor.native_session_id == "native-source"
    assert descriptor.last_settled_job_id == "ajob_source"


def test_turn_boundary_preserves_reviewed_resident_tree_and_reaps_new_processes():
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    supervisor.complete_turn("athr_source", lease.token, turn)

    assert manager.events[-3:] == [
        ("census", 0),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]
    assert supervisor.status("athr_source").resident_state == "live"


def test_kata_turn_observation_is_uid_scoped_and_closes_before_platform_cleanup():
    class Manager(_Manager):
        def finish_guest_observation(self, spec, cursor):
            observation = super().finish_guest_observation(spec, cursor)
            return replace(
                observation,
                tool_marks=(
                    ToolMark(10.0, "pre", "toolu_1"),
                    ToolMark(10.1, "post", "toolu_1"),
                ),
            )

        def tool_mark_reader(self, _spec):
            raise AssertionError("Kata must not trust the agent-owned mark file")

    manager = Manager()
    manager.backend.runtime = KATA_RUNTIME
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="run a sub-millisecond command",
        model="glm-5.1",
        reasoning_effort=None,
    )
    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.complete is True
    assert telemetry.tool_marks == (
        ToolMark(10.0, "pre", "toolu_1"),
        ToolMark(10.1, "post", "toolu_1"),
    )
    # The window is fenced by the observation that reads it back: the opening
    # census lands after the fence is taken, the closing census lands before
    # the fence is read, and the platform's own cleanup follows both so it is
    # never billed to the member.
    assert manager.events[-8:] == [
        ("census", 0),
        ("observe-begin", 11001),
        ("member-window", "register"),
        ("member-window", "sample"),
        ("observe-finish", 11001),
        ("member-window", "close"),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]


def test_kata_observer_failure_keeps_the_source_and_reports_a_gap():
    class Manager(_Manager):
        def finish_guest_observation(self, _spec, _cursor):
            return GuestObservation.unavailable("observer stopped")

    manager = Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )

    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.source == GUEST_OBSERVER_SOURCE
    assert telemetry.complete is False


def test_lost_shared_residency_preserves_observation_until_turn_closes():
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )

    supervisor.residency_lost("athr_source", generation=1)

    assert turn.interrupted is True
    # Losing residency interrupts the member but must not close the window
    # early: the turn is still the turn, and its closing census belongs to
    # ``complete_turn`` like every other activation's.
    assert ("member-window", "close") not in manager.events

    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.complete is True
    assert manager.events[-5:] == [
        ("member-window", "sample"),
        ("observe-finish", 11001),
        ("member-window", "close"),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]


def test_supervisor_close_releases_an_active_turn_observation_and_member_lock():
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    member = supervisor._members["athr_source"].members["athr_source"]

    supervisor.close()

    assert manager.events[-1] == ("observe-finish", 11001)
    assert member.turn_lock.locked() is False
    assert member.activation_processes is None


def test_supervisor_close_does_not_steal_an_initialization_lock():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    member.turn_lock.acquire()

    supervisor.close()

    assert member.turn_lock.locked() is True
    member.turn_lock.release()


def test_abandon_releases_observation_when_normal_turn_cleanup_fails(monkeypatch):
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(supervisor, "complete_turn", fail_cleanup)
    supervisor.abandon("athr_source", lease.token, lost=True)

    assert manager.events[-1] == ("observe-finish", 11001)


def test_shared_claude_turn_fences_and_returns_exact_tool_marks():
    """The supervisor must carry the retained hook slice to the broker."""

    class Manager(_Manager):
        def tool_mark_reader(self, _spec):
            self.events.append(("marks-begin",))

            def read():
                self.events.append(("marks-finish",))
                return [ToolMark(10.0, "pre", "toolu_1"), ToolMark(10.1, "post", "toolu_1")]

            return SimpleNamespace(read=read)

    manager = Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.tool_marks == (
        ToolMark(10.0, "pre", "toolu_1"),
        ToolMark(10.1, "post", "toolu_1"),
    )
    assert manager.events[-5:] == [
        ("census", 0),
        ("marks-begin",),
        ("marks-finish",),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]


def test_the_census_has_its_own_clock_and_honours_its_own_interval():
    """The census must not depend on the turn emitting events.

    Driving censuses from the turn's event stream meant a turn that spent its
    time waiting on a model produced almost none, while the summary still
    advertised a one-second period. This pins the independent tick: one
    census, then nothing until the interval has passed.
    """
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    # The sampler thread is started with the supervisor; stop it so this test
    # drives the tick itself and asserts the interval deterministically.
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)

    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    registered = [event for event in manager.events if event[0] == "member-window"]
    assert registered[-1] == ("member-window", "register")

    assert supervisor.sample_open_windows() == 1
    sampled = [event for event in manager.events if event[0] == "member-window"]
    assert sampled[-1] == ("member-window", "sample")

    # Within the interval nothing more is due, so the period is the census's
    # own property rather than however often a caller happens to ask.
    assert supervisor.sample_open_windows() == 0
    assert [event for event in manager.events if event[0] == "member-window"] == sampled


def test_a_member_without_an_open_window_is_never_censused():
    """Nothing is measured for a member that is not running a turn."""
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
    )
    supervisor._stop.set()

    assert supervisor.sample_open_windows() == 0
    assert [event for event in manager.events if event[0] == "member-window"] == []


def test_a_checkpoint_is_produced_without_any_turn_event():
    """Uploads must not wait for the agent to say something.

    The sampler thread owns both the census and the upload cadence, so a turn
    that spends its time inside one silent tool call still leaves a report
    behind. This drives the tick directly and asserts a checkpoint appears with
    no event stream involved.
    """
    reported: list[tuple[str, dict]] = []
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
        window_checkpoint_hook=lambda thread_id, snapshot: reported.append((thread_id, snapshot)),
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)

    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    assert reported == []

    supervisor.sample_open_windows()

    # A checkpoint was produced and is waiting for the turn stream to forward,
    # without a single turn event having been read.
    assert len(reported) == 1
    assert reported[0][0] == "athr_source"
    assert reported[0][1]["state"] == "collecting"
    pending = supervisor.take_reported_checkpoint("athr_source")
    assert pending is not None and pending["state"] == "collecting"
    assert pending["cpu"]["capacity_cores"] == 2.0
    # Taking it is what makes forwarding happen once rather than repeatedly.
    assert supervisor.take_reported_checkpoint("athr_source") is None


def test_checkpoints_are_produced_without_a_hook_because_production_has_none():
    """The delivery gate must not depend on an observer nobody installs.

    The broker builds its supervisor without a checkpoint hook. An earlier
    version returned early when the hook was absent, so no checkpoint was ever
    produced in production while a test that passed its own hook stayed green --
    the same shape of gap as a fixture that does not match the producer.
    """
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
    )
    assert supervisor._window_checkpoint_hook is None

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)

    supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    supervisor.sample_open_windows()

    pending = supervisor.take_reported_checkpoint("athr_source")
    assert pending is not None
    assert pending["state"] == "collecting"


def test_the_final_report_sequence_stays_above_every_checkpoint():
    """A checkpoint produced while the turn closes must not block the final.

    The sampler keeps ticking while the closing observation runs, which takes a
    guest exec. A final report that derived its sequence from the census counter
    could come out below a checkpoint made in that window, and the store
    rejects a lower sequence -- leaving a completed turn stored as collecting.
    """
    manager = _Manager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
    )
    lease = supervisor.acquire(
        replace(_spec(), cpu_limit="0.5"),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
    )
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    # A later configuration must never relabel an already-open measurement.
    group = supervisor._members["athr_source"]
    group.spec = replace(group.spec, cpu_limit="4")
    sequences: list[int] = []
    for _ in range(3):
        supervisor.sample_open_windows()
        checkpoint = supervisor.take_reported_checkpoint("athr_source")
        if checkpoint is not None:
            sequences.append(checkpoint["report_seq"])
            assert checkpoint["cpu"]["capacity_cores"] == 0.5
    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert sequences, "checkpoints should have been produced"
    assert telemetry.turn_resources is not None
    assert telemetry.turn_resources["report_seq"] > max(sequences)

    assert telemetry.turn_resources["cpu"]["capacity_cores"] == 0.5


@pytest.mark.parametrize(
    ("pre_existing", "running", "expected"),
    [
        # No container yet: this activation pays for creating it.
        (False, None, "create"),
        # It exists and is stopped: this activation starts it.
        (True, False, "start"),
        # It exists and is running: this activation only attaches to it.
        (True, True, "reuse"),
    ],
)
def test_acquire_reports_what_it_did_to_the_container(pre_existing, running, expected):
    """The four startup paths differ by the action, and only the call knows it.

    A group can outlive the broker that made it, and a container that was
    stopped and restarted looks exactly like one that never stopped once it is
    running again. So the action is reported by the code that performed it
    rather than inferred later from the container's state.
    """
    manager = _Manager()
    spec = _spec()
    if pre_existing:
        manager.items[spec.container_name] = manager._inspection(spec, running=running)
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        spec,
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    assert lease.container_action == expected


def test_a_container_created_then_started_is_still_reported_as_created():
    """Creating a group also starts it; the stronger fact must survive."""
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

    # The fake manager, like the real one, starts what it just created.
    assert manager.starts == [_spec().container_name]
    assert lease.container_action == "create"


def test_container_action_is_reported_even_when_this_process_is_not_profiling():
    """The action is a fact, not a measurement: it never depends on a switch."""
    assert operation_profiling.active_run() is None
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

    assert lease.container_action == "create"
    assert lease.stages == []


def test_ensure_resident_reports_a_reopened_native_session():
    """Reopening a retained native session is a resume, not a fresh CLI."""
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )

    descriptor = supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id=None, slot=0),
    )

    # The retained member owns the locator, so an omitted field still resumes
    # the exact session rather than starting a fresh chat.
    assert descriptor.resident_action == "resume"


def test_ensure_resident_reports_a_first_process_as_new():
    """A member with no session to resume starts a new CLI."""
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    fresh = [
        SharedMemberManifest(
            thread_id="athr_source",
            native_session_id=None,
            context_status="new",
            resident_slot_id=0,
            last_settled_job_id=None,
        )
    ]
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=fresh,
    )

    descriptor = supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id=None, slot=0),
    )

    assert descriptor.resident_action == "new"


def test_ensure_resident_reports_reuse_for_a_hot_process():
    """The same live process accepting another turn is a reuse, not a start."""
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    assert (
        supervisor.ensure_resident("athr_source", first.token, launch).resident_action == "resume"
    )
    supervisor.settle("athr_source", first.token, job_id="ajob_source", context_status="restored")

    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    descriptor = supervisor.ensure_resident("athr_source", second.token, launch)

    assert descriptor.resident_action == "reuse"
    assert len(factory.opened) == 1


def test_broker_sub_stages_are_reported_when_this_process_is_profiling(monkeypatch, tmp_path):
    """With the broker's switch on, the container work comes back as durations."""
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "broker-stages-1",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                "max_operations": 10,
            }
        ),
    )
    operation_profiling.reset_cache()
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "broker-stages-1",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "call_id": "c7",
            "expires_at": time.time() + 60,
        },
    )

    # The answer says which call it belongs to and that this process collected.
    assert lease.profiling_call_id == "c7"
    assert lease.profiling_recorded is True
    # The fake manager is instrumented like the real one: the container was
    # created here and then found already running.
    names = [item["name"] for item in lease.stages]
    assert names == ["container_create", "service_hosts_sync"]
    assert all(item["duration_us"] >= 0 for item in lease.stages)
    # Only durations travel: the caller's timeline cannot place them.
    assert all("start_offset_us" not in item for item in lease.stages)
    assert lease.container_action == "create"


def _enable_broker_profiling(monkeypatch) -> None:
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(
            {
                "run_id": "broker-context-1",
                "operations": ["attempt.startup"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
                "max_operations": 10,
            }
        ),
    )
    operation_profiling.reset_cache()


def test_a_lapsed_selection_stops_the_broker_recording(monkeypatch):
    """A run that already expired must not collect container sub-stages.

    Regression: the broker checked only that its cached run existed, so it
    kept measuring after the runner had stopped.
    """
    _enable_broker_profiling(monkeypatch)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "broker-context-1",
            "measurement_id": "msm_gone",
            "operation": "attempt.startup",
            "expires_at": time.time() - 1,
        },
    )

    assert lease.stages == []
    # A refusal is still an answer about collection: it says this process did
    # not record, which is what makes the caller's fragment missing.
    assert lease.profiling_recorded is False
    # The action is a fact, so it survives the lapsed measurement.
    assert lease.container_action == "create"


def test_a_selection_for_another_run_is_refused(monkeypatch):
    """The broker honours only the run configured here."""
    _enable_broker_profiling(monkeypatch)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "some-other-run",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "expires_at": time.time() + 60,
        },
    )

    assert lease.stages == []


def test_a_live_selection_records_container_sub_stages(monkeypatch):
    """With a matching, unexpired selection the broker reports its work."""
    _enable_broker_profiling(monkeypatch)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling={
            "run_id": "broker-context-1",
            "measurement_id": "msm_x",
            "operation": "attempt.startup",
            "call_id": "c9",
            "expires_at": time.time() + 60,
        },
    )

    assert [item["name"] for item in lease.stages] == [
        "container_create",
        "service_hosts_sync",
    ]
    assert all(item["duration_us"] >= 0 for item in lease.stages)
    assert lease.profiling_call_id == "c9"
    assert lease.profiling_recorded is True
    assert lease.profiling_truncated is False


def test_a_reuse_with_no_stages_still_reports_that_it_collected(monkeypatch):
    """An empty stage list is a completed collection, not a missing one.

    Regression: the reuse path runs no `cli_spawn`, so it answers with no
    stages, and the caller read that as never having heard from the broker.
    """
    _enable_broker_profiling(monkeypatch)
    manager = _Manager()
    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=factory)
    live = {
        "run_id": "broker-context-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "call_id": "c1",
        "expires_at": time.time() + 60,
    }
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling=live,
    )
    supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
        profiling=live,
    )
    supervisor.settle("athr_source", lease.token, job_id="ajob_source", context_status="live")

    second = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
        profiling={**live, "call_id": "c2"},
    )
    descriptor = supervisor.ensure_resident(
        "athr_source",
        second.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
        profiling={**live, "call_id": "c2"},
    )

    assert descriptor.resident_action == "reuse"
    assert descriptor.stages == []
    assert descriptor.profiling_call_id == "c2"
    # The member's work was skipped on purpose and that is a complete answer.
    assert descriptor.profiling_recorded is True


def test_a_collection_cut_short_by_the_deadline_says_so(monkeypatch):
    """An empty answer is only "nothing to time" when collection finished.

    Regression, at the response the runner actually reads: the broker's local
    deadline lapsed while it waited for the member lock, the CLI start was
    never timed, and the lease still reported a completed collection.
    """
    _enable_broker_profiling(monkeypatch)
    live = {
        "run_id": "broker-context-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "call_id": "c1",
        "expires_at": time.time() + 600,
    }
    real_recorder = operation_profiling.start_stage_recording

    def lapsed(context):
        """A recorder that already missed its own (shorter) deadline."""
        recorder = real_recorder(context)
        if recorder is not None:
            recorder.deadline_mono = time.monotonic() - 1
        return recorder

    monkeypatch.setattr("cloud_agent_host.shared_session_supervisor.start_stage_recording", lapsed)
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager, transport_factory=_TransportFactory())

    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        profiling=live,
    )
    descriptor = supervisor.ensure_resident(
        "athr_source",
        lease.token,
        _launch("athr_source", native_session_id="native-source", slot=0),
        profiling={**live, "call_id": "c2"},
    )

    for answer in (lease, descriptor):
        # The recorder existed and the call answered...
        assert answer.profiling_recorded is True
        # ...but every stage was skipped, so nothing was measured at all.
        assert answer.stages == []
        assert answer.profiling_truncated is True
