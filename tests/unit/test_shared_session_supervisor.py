"""Concurrency and capture barriers for shared live chat sandboxes."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

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
from cloud_agent_host.sandbox import KATA_RUNTIME, SandboxError
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
)


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
        self.start_phases: list[str | None] = []
        self.stop_count = 0
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

    def create(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        return self.items.setdefault(spec.container_name, self._inspection(spec))

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
        network_phase: str | None = None,
    ) -> ChatSandboxInspection:
        self.start_phases.append(network_phase)
        current = self.items[spec.container_name]
        if not current.running:
            current = replace(
                current,
                running=True,
                status=ChatSandboxStatus.RUNNING,
            )
            self.items[spec.container_name] = current
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
        self, spec: ChatSandboxSpec, *, user_uid: int
    ) -> GuestObserverCursor:
        self.events.append(("observe-begin", user_uid))
        return GuestObserverCursor(uid=user_uid, available=True)

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


def test_acquire_recreates_agent_network_for_settled_context():
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

    assert manager.start_phases[-1] == "agent"


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

    assert manager.start_phases == []
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


def test_native_fork_tracks_activity_but_does_not_lock_a_sibling_operation():
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
                    (10.0, "pre", "toolu_1"),
                    (10.1, "post", "toolu_1"),
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
        (10.0, "pre", "toolu_1"),
        (10.1, "post", "toolu_1"),
    )
    assert manager.events[-5:] == [
        ("census", 0),
        ("observe-begin", 11001),
        ("observe-finish", 11001),
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
    assert manager.events[-1] == ("observe-begin", 11001)

    telemetry = supervisor.complete_turn("athr_source", lease.token, turn)

    assert telemetry.complete is True
    assert manager.events[-3:] == [
        ("observe-finish", 11001),
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
                return [(10.0, "pre", "toolu_1"), (10.1, "post", "toolu_1")]

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
        (10.0, "pre", "toolu_1"),
        (10.1, "post", "toolu_1"),
    )
    assert manager.events[-5:] == [
        ("census", 0),
        ("marks-begin",),
        ("marks-finish",),
        ("reap-except", 0, ((41, 400),)),
        ("normalize", 0),
    ]
