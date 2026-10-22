"""Archive barriers, residency replacement, and turn-loss supervisor behavior.

The core module retains the shared fakes; this file keeps the intact lifecycle
cases independently schedulable without extracting their fixture machinery.
"""

from __future__ import annotations

from tests.unit.test_shared_session_supervisor import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
    GuestObservation,
    SandboxError,
    SessionBusy,
    SessionLeaseLost,
    SharedChatSessionSupervisor,
    SharedMemberManifest,
    SimpleNamespace,
    _gated_inspect,
    _in_thread,
    _launch,
    _Manager,
    _members,
    _spec,
    _Transport,
    _TransportFactory,
    pytest,
    replace,
    threading,
    time,
)

from cloud_agent_host.guest_observer import GUEST_OBSERVER_SOURCE
from cloud_agent_host.resident_transport import ResidentTransport
from cloud_agent_host.sandbox import KATA_RUNTIME
from cloud_agent_host.shared_session_supervisor import archive_failure_http_detail
from cloud_agent_host.tool_windows import ToolMark


class _HeldStartManager(_Manager):
    """Hold every start once armed, including a running group's bridge check."""

    def __init__(self) -> None:
        super().__init__()
        self.hold_start = False
        self.start_entered = threading.Event()
        self.start_release = threading.Event()

    def start(self, spec, **kwargs):
        if self.hold_start:
            self.start_entered.set()
            assert self.start_release.wait(timeout=2)
        return super().start(spec, **kwargs)


def test_initial_archive_waits_out_a_start_that_begins_under_its_barrier(monkeypatch) -> None:
    """A request admitted before the barrier may begin its bridge check under it."""
    manager = _HeldStartManager()
    supervisor = SharedChatSessionSupervisor(manager)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    running, _ = supervisor.operation_opened("athr_peer")
    group = supervisor._members["athr_source"]
    # A second read passes its first admission, then is held before its check.
    entered, release = _gated_inspect(manager, monkeypatch, _spec().container_name)
    manager.hold_start = True
    opening, _ = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    assert entered.wait(timeout=1)
    captured = threading.Event()

    def capture(*_args):
        captured.set()
        return {"enabled": True, "stored": True}

    capturing, outcome = _in_thread(
        lambda: supervisor.capture_initial_archive("athr_source", lease.token, capture=capture)
    )
    try:
        deadline = time.monotonic() + 1
        while not group.snapshotting and time.monotonic() < deadline:
            time.sleep(0.01)
        assert group.snapshotting
        release.set()
        assert manager.start_entered.wait(timeout=1)
        assert group.starting
        supervisor.operation_closed("athr_peer", running)
        capturing.join(timeout=0.6)
        assert capturing.is_alive(), f"the archive did not wait for the start: {outcome}"
        assert not captured.is_set()
    finally:
        release.set()
        manager.start_release.set()
        opening.join(timeout=2)
        capturing.join(timeout=2)

    assert not capturing.is_alive()
    assert "error" not in outcome
    assert captured.is_set()
    assert group.snapshotting is False


def test_initial_archive_reports_busy_once_a_request_outlasts_its_wait() -> None:
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.initial_archive_wait_s = 0.3
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=_members(),
    )
    token, _ = supervisor.operation_opened("athr_peer")
    captured: list[bool] = []

    with pytest.raises(SessionBusy, match="active mutations"):
        supervisor.capture_initial_archive(
            "athr_source", lease.token, capture=lambda *_args: captured.append(True) or {}
        )

    group = supervisor._members["athr_source"]
    assert captured == []
    assert group.snapshotting is False
    # The barrier is gone: the request finishes, and the next attempt captures.
    supervisor.operation_closed("athr_peer", token)
    supervisor.capture_initial_archive(
        "athr_source", lease.token, capture=lambda *_args: captured.append(True) or {}
    )
    assert captured == [True]


def test_failed_marked_initial_archive_stops_and_completes_the_final_archive() -> None:
    manager = _Manager()
    prepared: list[bool] = []
    archived: list[tuple[bool, tuple[str, ...]]] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda _spec, inspection: prepared.append(inspection.running),
        stopped_archive_hook=lambda _spec, inspection, _members: archived.append(
            (inspection.running, inspection.networks)
        ),
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    initial_failure = RuntimeError("initial upload failed")

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        raise initial_failure

    with pytest.raises(RuntimeError, match="initial upload failed") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=capture,
        )

    status = supervisor.status("athr_source")
    assert raised.value is initial_failure
    assert archive_failure_http_detail(raised.value) == (
        "archive failure (phase=initial, type=RuntimeError)"
    )
    assert prepared == [True]
    assert archived == [(False, ("agent-net",))]
    assert manager.stop_count == 2
    assert manager.items[_spec().container_name].networks == ()
    assert status.running is False
    assert status.snapshotting is False
    assert all(event[0] != "thaw" for event in manager.events)


def test_failed_marked_initial_archive_waits_for_concurrent_guard_stop() -> None:
    class BlockingStopManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.stop_entered = threading.Event()
            self.stop_release = threading.Event()

        def stop(self, spec, **kwargs):
            self.stop_entered.set()
            assert self.stop_release.wait(timeout=5)
            return super().stop(spec, **kwargs)

    manager = BlockingStopManager()
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=lambda *_args: None,
    )
    fresh_members = [
        SharedMemberManifest(
            thread_id="athr_source",
            context_status="new",
            resident_slot_id=0,
        )
    ]
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=fresh_members,
    )
    capture_entered = threading.Event()
    preservation_collided = threading.Event()
    capture_finished = threading.Event()
    capture_errors: list[BaseException] = []
    guard_errors: list[BaseException] = []
    stop_if_idle = supervisor.stop_if_idle

    def observed_stop_if_idle(*args, **kwargs):
        try:
            return stop_if_idle(*args, **kwargs)
        except SessionBusy:
            if kwargs.get("reason") == "orphaned_attempt":
                preservation_collided.set()
            raise

    supervisor.stop_if_idle = observed_stop_if_idle  # type: ignore[method-assign]

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        capture_entered.set()
        assert manager.stop_entered.wait(timeout=5)
        raise RuntimeError("initial upload failed")

    def run_capture() -> None:
        try:
            supervisor.capture_initial_archive(
                "athr_source",
                lease.token,
                capture=capture,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            capture_errors.append(exc)
        finally:
            capture_finished.set()

    def run_guard_stop() -> None:
        try:
            supervisor.stop_if_idle(
                "athr_source",
                generation=1,
                reason="unguarded",
                defer_archive=True,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            guard_errors.append(exc)

    capture_thread = threading.Thread(target=run_capture)
    guard_thread = threading.Thread(target=run_guard_stop)
    capture_thread.start()
    assert capture_entered.wait(timeout=2)
    guard_thread.start()
    assert manager.stop_entered.wait(timeout=2)
    assert preservation_collided.wait(timeout=2)
    # The preservation cleanup must wait for the already-owned stop instead of
    # sampling RUNNING and permanently retaining the snapshot barrier.
    assert capture_finished.wait(timeout=0.2) is False
    manager.stop_release.set()
    capture_thread.join(timeout=2)
    guard_thread.join(timeout=2)

    assert not capture_thread.is_alive()
    assert not guard_thread.is_alive()
    assert guard_errors == []
    assert len(capture_errors) == 1
    assert isinstance(capture_errors[0], SessionBusy)
    status = supervisor.status("athr_source")
    assert status.running is False
    assert status.snapshotting is False
    assert supervisor.has_pending_archive_work() is True
    assert all(event[0] != "thaw" for event in manager.events)


def test_failed_final_upload_keeps_the_stopped_bridge_and_never_thaws(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _Manager()
    prepared: list[bool] = []

    class ProviderError(RuntimeError):
        def __init__(self, *, code: str, status: int, unsafe: str) -> None:
            super().__init__(unsafe)
            self.response = {
                "Error": {"Code": code, "Message": unsafe},
                "ResponseMetadata": {
                    "HTTPStatusCode": status,
                    "HTTPHeaders": {"authorization": unsafe},
                },
                "Body": unsafe,
            }

    initial_unsafe = "https://initial.invalid/private-key?credential=initial-secret"
    final_unsafe = "https://final.invalid/object-key?credential=final-secret"
    final_failure = RuntimeError("final upload failed")
    final_failure.__cause__ = ProviderError(
        code="SlowDown",
        status=503,
        unsafe=final_unsafe,
    )

    def archive(*_args) -> None:
        raise final_failure

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda _spec, inspection: prepared.append(inspection.running),
        stopped_archive_hook=archive,
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    initial_failure = RuntimeError("initial upload failed")
    initial_failure.__cause__ = ProviderError(
        code="AccessDenied",
        status=403,
        unsafe=initial_unsafe,
    )

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        raise initial_failure

    with pytest.raises(RuntimeError, match="final upload failed") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=capture,
        )

    status = supervisor.status("athr_source")
    assert raised.value is final_failure
    assert raised.value.__cause__ is initial_failure
    safe_detail = archive_failure_http_detail(raised.value)
    assert safe_detail == (
        "archive failure (phase=initial, type=ProviderError, code=AccessDenied, status=403); "
        "archive failure (phase=fail_stop_final, type=ProviderError, code=SlowDown, status=503)"
    )
    assert initial_unsafe not in safe_detail
    assert final_unsafe not in safe_detail
    assert initial_unsafe not in caplog.text
    assert final_unsafe not in caplog.text
    assert initial_unsafe not in " ".join(getattr(raised.value, "__notes__", ()))
    assert final_unsafe not in " ".join(getattr(raised.value, "__notes__", ()))
    assert any(
        getattr(record, "detail", None)
        == "archive failure (phase=fail_stop_final, type=ProviderError, "
        "code=SlowDown, status=503)"
        for record in caplog.records
    )
    assert prepared == [True]
    assert manager.stop_count == 1
    assert manager.items[_spec().container_name].networks == ("agent-net",)
    assert status.running is False
    assert status.snapshotting is False
    assert supervisor.has_pending_archive_work() is True
    assert all(event[0] != "thaw" for event in manager.events)
    with pytest.raises(SessionBusy, match="final archive retry"):
        supervisor.operation_opened("athr_source")


def test_failed_initial_preservation_stop_releases_barrier_after_exact_retry() -> None:
    class StopFailureManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_stop_once = True

        def stop(self, spec, **kwargs):
            if self.fail_stop_once:
                self.fail_stop_once = False
                self.stop_count += 1
                raise SandboxError("docker stop failed")
            return super().stop(spec, **kwargs)

    manager = StopFailureManager()
    archive_calls: list[object] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=lambda *args: archive_calls.append(args),
    )
    fresh_members = [
        SharedMemberManifest(
            thread_id="athr_source",
            context_status="new",
            resident_slot_id=0,
        )
    ]
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=False,
        manifest=fresh_members,
    )
    initial_failure = RuntimeError("initial upload failed")

    def capture(*_args):
        manager.initial_archive_marker = b"prepared-initial-marker"
        raise initial_failure

    with pytest.raises(SandboxError, match="docker stop failed") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=capture,
        )

    status = supervisor.status("athr_source")
    assert raised.value.__cause__ is initial_failure
    assert manager.stop_count == 1
    assert archive_calls == []
    assert status.running is True
    assert status.snapshotting is True
    assert supervisor.has_pending_archive_work() is True
    assert all(event[0] != "thaw" for event in manager.events)
    with pytest.raises(SessionBusy, match="snapshot capture"):
        supervisor.operation_opened("athr_source")

    stopped = supervisor.stop_if_idle(
        "athr_source",
        generation=1,
        reason="orphaned_attempt",
    )

    assert stopped.running is False
    assert stopped.snapshotting is False
    assert manager.stop_count == 3
    assert len(archive_calls) == 1
    assert supervisor.has_pending_archive_work() is False
    assert all(event[0] != "thaw" for event in manager.events)

    replacement = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=False,
        manifest=fresh_members,
        expected_container_id="container-shared",
    )

    assert replacement.token
    assert supervisor.status("athr_source").running is True
    assert supervisor.status("athr_source").snapshotting is False
    supervisor.abandon("athr_source", replacement.token, lost=False)


def test_failed_initial_archive_before_marker_thaws_without_stopping() -> None:
    manager = _Manager()
    prepared: list[bool] = []
    archived: list[object] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda _spec, inspection: prepared.append(inspection.running),
        stopped_archive_hook=lambda *args: archived.append(args),
    )
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    initial_failure = RuntimeError("initial capture failed before marker")

    with pytest.raises(RuntimeError, match="before marker") as raised:
        supervisor.capture_initial_archive(
            "athr_source",
            lease.token,
            capture=lambda *_args: (_ for _ in ()).throw(initial_failure),
        )

    status = supervisor.status("athr_source")
    assert raised.value is initial_failure
    assert prepared == []
    assert archived == []
    assert manager.stop_count == 0
    assert status.running is True
    assert status.snapshotting is False
    assert manager.events[0] == ("freeze", (0, 1))
    assert manager.events[1][0] == "thaw"


@pytest.mark.parametrize("entrypoint", ["operation", "terminal"])
def test_failed_start_marker_completion_is_retried_by_same_mutation_entrypoint(
    entrypoint: str,
) -> None:
    manager = _Manager()
    completions = 0

    def complete(_spec, inspection) -> None:
        nonlocal completions
        assert inspection.running is True
        completions += 1
        if completions == 1:
            raise RuntimeError("marker clear failed")

    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=lambda _spec, inspection: (
            None if not inspection.running else pytest.fail("start fence requires stopped state")
        ),
        started_archive_hook=complete,
    )
    supervisor.register_manifest(_spec(), _members())
    manager.stop(_spec(), release_networks=False)
    opened = (
        supervisor.operation_opened if entrypoint == "operation" else supervisor.terminal_opened
    )

    with pytest.raises(RuntimeError, match="marker clear failed"):
        opened("athr_source")

    token, descriptor = opened("athr_source")

    assert token
    assert descriptor.running is True
    assert completions == 2
    assert manager.stop_count == 1
    if entrypoint == "operation":
        supervisor.operation_closed("athr_source", token)
    else:
        supervisor.terminal_closed("athr_source", token)


def test_pending_final_archive_retries_after_external_bridge_removal() -> None:
    manager = _Manager()
    archive_calls = 0

    def archive(_spec, _inspection, _members) -> None:
        nonlocal archive_calls
        archive_calls += 1
        if archive_calls == 1:
            raise RuntimeError("upload response lost")

    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=archive,
    )
    supervisor.register_manifest(_spec(), _members())

    with pytest.raises(RuntimeError, match="upload response lost"):
        supervisor.stop_if_idle("athr_source", generation=1)
    stopped = manager.items[_spec().container_name]
    assert stopped.running is False
    assert stopped.networks == ("agent-net",)
    manager.items[_spec().container_name] = replace(stopped, networks=())
    supervisor.register_manifest(
        _spec(),
        [
            replace(_members()[0], resident_state="restarting"),
            replace(_members()[1], context_status="lost", resident_state="lost"),
        ],
    )
    assert supervisor.status("athr_source").resident_state == "restarting"
    assert supervisor.status("athr_peer").resident_state == "lost"

    descriptor = supervisor.stop_if_idle("athr_source", generation=1)

    assert descriptor.running is False
    assert descriptor.resident_state == "stopped"
    assert supervisor.status("athr_peer").resident_state == "lost"
    assert archive_calls == 2
    assert manager.items[_spec().container_name].networks == ()


def test_direct_orphan_archive_retry_clears_the_admission_fence() -> None:
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    manager.read_running_final_archive_marker = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: b"durable-pending-marker"
    )
    archive_calls = 0

    def archive(_spec, _inspection, _members) -> None:
        nonlocal archive_calls
        archive_calls += 1
        if archive_calls == 1:
            raise RuntimeError("upload failed")

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)

    descriptors = supervisor.reconcile()

    assert [item.thread_id for item in descriptors] == [spec.thread_id]
    assert archive_calls == 1
    with pytest.raises(SessionBusy, match="final archive"):
        supervisor.operation_opened(spec.thread_id)

    stopped = supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    token, _ = supervisor.operation_opened(spec.thread_id)

    assert stopped.running is False
    assert archive_calls == 2
    supervisor.operation_closed(spec.thread_id, token)


def test_ambiguous_post_start_failure_remains_pending_until_exact_stop_retry() -> None:
    class UncertainStartManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.fail_start_once = True
            self.fail_stop_once = True

        def start(self, spec, **kwargs):
            if self.fail_start_once:
                self.fail_start_once = False
                current = self.items[spec.container_name]
                callback = kwargs.get("before_stopped_start")
                assert callback is not None and current.running is False
                callback(current)
                self.items[spec.container_name] = replace(
                    current,
                    running=True,
                    status=ChatSandboxStatus.RUNNING,
                )
                raise SandboxError("guest network proof failed")
            return super().start(spec, **kwargs)

        def stop(self, spec, **kwargs):
            if self.fail_stop_once:
                self.fail_stop_once = False
                raise SandboxError("fail-stop response lost")
            return super().stop(spec, **kwargs)

    manager = UncertainStartManager()
    archived: list[tuple] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=lambda *_args: None,
        started_archive_hook=lambda *_args: None,
        stopped_archive_prepare_hook=lambda *_args: None,
        stopped_archive_hook=lambda *args: archived.append(args),
    )
    supervisor.register_manifest(_spec(), _members())
    manager.items[_spec().container_name] = manager._inspection(_spec(), running=False)

    with pytest.raises(SandboxError, match="fail-stop response lost"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )
    with pytest.raises(SessionBusy, match="final archive retry"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:2",
            activation_generation=2,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )

    supervisor.reconcile()

    assert len(archived) == 1
    current = manager.items[_spec().container_name]
    assert current.running is False
    assert current.networks == ()


def test_external_stopped_group_with_missing_final_marker_keeps_its_bridge() -> None:
    manager = _Manager()
    spec = _spec()
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda *_args: (_ for _ in ()).throw(
            SandboxError("final archive marker is missing")
        ),
    )
    supervisor.register_manifest(spec, _members())
    manager.items[spec.container_name] = manager._inspection(spec, running=False)

    descriptors = supervisor.list()

    assert all(item.running is False for item in descriptors)
    assert manager.items[spec.container_name].networks == ("agent-net",)
    assert manager.stop_count == 0
    with pytest.raises(SessionBusy, match="native flush failure"):
        supervisor.operation_opened(spec.thread_id)


@pytest.mark.parametrize("generations", [{"athr_source": 0}, {"athr_source": 1, "athr_peer": 0}])
def test_delayed_admin_stop_cannot_stop_changed_members(generations):
    manager = _Manager()
    spec = _spec()
    manager.create(spec)
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(spec, _members(), expected_container_id="container-shared")
    with pytest.raises(SessionLeaseLost, match="activation changed"):
        supervisor.stop_if_idle(
            "athr_source",
            generation=1,
            member_activation_generations=generations,
        )
    assert manager.stop_count == 0
    stopped = supervisor.stop_if_idle(
        "athr_source",
        generation=1,
        member_activation_generations={"athr_source": 0, "athr_peer": 0},
    )
    assert not stopped.running


def test_new_activation_waits_for_an_inflight_idle_stop_then_restarts_exact_container():
    class BlockingStopManager(_Manager):
        def __init__(self) -> None:
            super().__init__()
            self.stop_entered = threading.Event()
            self.allow_stop = threading.Event()

        def stop(
            self,
            spec: ChatSandboxSpec,
            *,
            expected_container_id: str | None = None,
            release_networks: bool = True,
        ) -> ChatSandboxInspection:
            self.stop_entered.set()
            assert self.allow_stop.wait(timeout=2)
            return super().stop(
                spec,
                expected_container_id=expected_container_id,
                release_networks=release_networks,
            )

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


def test_a_residency_mcp_credential_lets_a_probe_borrow_the_live_process():
    """The process keeps its residency's credential; a probe carries none."""
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        mcp_server_ids=(":platform:memory", "github"),
        mcp_base_url="http://relay",
        mcp_credential="amr.residency-1",
    )
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert transport.compatible_with(replace(launch, mcp_credential=""))
    assert transport.compatible_with(launch)
    # What the CLI fixed at start still has to match.
    assert not transport.compatible_with(replace(launch, mcp_credential="amr.residency-2"))
    assert not transport.compatible_with(
        replace(launch, mcp_credential="", mcp_server_ids=(":platform:memory",))
    )
    assert not transport.compatible_with(
        replace(launch, mcp_credential="", mcp_base_url="http://other-relay")
    )


def test_a_per_attempt_mcp_token_still_needs_a_new_process():
    """A process holding one turn's token cannot serve the next turn's tools."""
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        mcp_server_ids=(":platform:memory",),
        mcp_base_url="http://relay",
        mcp_credential="ajt.turn-1",
    )
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert not transport.compatible_with(replace(launch, mcp_credential=""))
    assert not transport.compatible_with(replace(launch, mcp_credential="ajt.turn-2"))


def test_github_token_rotation_or_removal_requires_a_new_resident_process():
    launch = replace(
        _launch("athr_source", native_session_id="native-source", slot=0),
        github_token="first-token",
        github_repo="owner/repo",
    )
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    assert transport.compatible_with(replace(launch, credential="renewed-grant"))
    assert not transport.compatible_with(replace(launch, github_token="renewed-token"))
    assert not transport.compatible_with(replace(launch, github_token=None))
    assert not transport.compatible_with(replace(launch, github_repo="owner/other"))


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


def test_successful_turn_boundary_retains_the_member_before_broker_settlement():
    """The member remains resident and settled context survives local cleanup."""
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

    descriptor = supervisor.status("athr_source")

    assert descriptor.resident_state == "live"
    assert descriptor.last_settled_job_id == "ajob_source"
    assert descriptor.context_status == "restored"
    assert descriptor.native_session_id == "native-source"


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


@pytest.mark.parametrize("turn", ["never_started", "aborted"])
def test_a_reaped_activation_ends_with_its_residency_so_its_group_can_idle(turn):
    """A runner that dies mid-activation never settles or abandons its member.

    The control plane reaps the attempt and closes the residency its process
    ran on. The lease the dead activation holds ends with that process, as an
    abandon would end it; otherwise a sibling's group never goes idle and its
    publication snapshot is refused as an active mutation. A turn the dead
    runner had started was already aborted when its stream dropped.
    """
    now = [1000.0]
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(
        manager, transport_factory=_TransportFactory(), idle_timeout_s=60, clock=lambda: now[0]
    )
    dead = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source", dead.token, _launch("athr_source", native_session_id="native-source", slot=0)
    )
    if turn == "aborted":
        started = supervisor.start_turn(
            "athr_source", dead.token, prompt="work", model="glm-5.1", reasoning_effort=None
        )
        supervisor.abort_turn("athr_source", dead.token, started)
    peer = supervisor.acquire(
        _spec(),
        thread_id="athr_peer",
        owner="attempt:7",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_peer", peer.token, _launch("athr_peer", native_session_id="native-peer", slot=1)
    )
    supervisor.settle("athr_peer", peer.token, job_id="ajob_peer", context_status="live")

    ended = supervisor.residency_lost("athr_source", generation=1)

    assert ended.busy is False
    assert ended.group_busy is False
    assert ended.context_status == ended.resident_state == "lost"
    assert ended.residency_generation == 0
    assert ended.idle_deadline_monotonic == 1060.0
    with pytest.raises(SessionLeaseLost):
        supervisor.settle("athr_source", dead.token, job_id="ajob_dead", context_status="live")
    assert supervisor.capture_snapshot("athr_peer", capture=lambda _backend: {"patch": ""}) == {
        "patch": ""
    }
    now[0] += 60
    assert [stopped.running for stopped in supervisor.sweep()] == [False]
    assert manager.items[_spec().container_name].running is False


def test_a_loss_its_activation_rotated_past_meanwhile_leaves_the_new_process_running():
    """A loss names the generation its beat sampled, which can be rotated past meanwhile.

    The loss closes that generation's process and then ends the activation
    that took it on. Before it does, the same activation can rotate to a newer
    residency and start the replacement, as a Fork rotates its source's. Ending
    the activation then tore the replacement down and marked its context lost.
    """
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
    first = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, first)
    old = factory.transports[0]
    rotated = []

    def rotate_as_the_old_process_closes() -> None:
        # The loss closes the old process outside the supervisor's lock, and
        # only then ends the activation; the rotation lands in between.
        type(old).close(old)
        rotated.append(
            supervisor.ensure_resident(
                "athr_source",
                lease.token,
                replace(first, credential="grant-2", residency_generation=2),
            )
        )

    old.close = rotate_as_the_old_process_closes

    lost = supervisor.residency_lost("athr_source", generation=1)

    assert [replacement.residency_generation for replacement in rotated] == [2]
    assert old.alive is False
    assert factory.transports[1].alive is True
    assert lost.residency_generation == 2
    assert lost.context_status != "lost"
    settled = supervisor.settle(
        "athr_source", lease.token, job_id="ajob_next", context_status="restored"
    )
    assert settled.busy is False
    assert settled.context_status == "restored"


def test_a_residency_its_activation_is_replacing_leaves_that_activation_running():
    """A host beat can name the generation a live activation is rotating away from.

    The activation found the member's process unfit for its turn (here, a new
    model) before taking it on, and asked the control plane for a new
    residency, so the old generation is no longer current: a beat sampled just
    before reports it lost. That closes the old process only. The activation
    keeps its lease and starts the replacement under its new generation.
    """
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
    old_launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", first.token, old_launch)
    supervisor.settle("athr_source", first.token, job_id="ajob_source", context_status="live")
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    probe = replace(old_launch, model="glm-5.2", credential=None)
    with pytest.raises(SandboxError, match="requires a renewable credential"):
        supervisor.ensure_resident("athr_source", lease.token, probe)

    lost = supervisor.residency_lost("athr_source", generation=1)

    assert lost.busy is True
    assert lost.context_status == "live"
    assert factory.transports[0].alive is False
    replacement = supervisor.ensure_resident(
        "athr_source", lease.token, replace(probe, credential="grant-2", residency_generation=2)
    )
    assert replacement.residency_generation == 2
    assert replacement.resident_action == "resume"
    settled = supervisor.settle(
        "athr_source", lease.token, job_id="ajob_next", context_status="restored"
    )
    assert settled.busy is False
    assert settled.context_status == "restored"


def _hung_turn_with_a_live_sibling(now: list[float]):
    """A member's turn is running for a runner that hangs; its Fork sibling has settled."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(
        manager, transport_factory=_TransportFactory(), idle_timeout_s=60, clock=lambda: now[0]
    )
    hung = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_source", hung.token, _launch("athr_source", native_session_id="native-source", slot=0)
    )
    turn = supervisor.start_turn(
        "athr_source", hung.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    peer = supervisor.acquire(
        _spec(),
        thread_id="athr_peer",
        owner="attempt:7",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident(
        "athr_peer", peer.token, _launch("athr_peer", native_session_id="native-peer", slot=1)
    )
    supervisor.settle("athr_peer", peer.token, job_id="ajob_peer", context_status="live")
    return manager, supervisor, hung, turn


def test_a_loss_during_its_turn_ends_the_activation_once_its_stream_closes_the_turn():
    """A hung runner still holds its turn stream when the reaper closes its residency.

    The loss closes the process and leaves the running turn to that stream,
    which still answers for what the turn did. Nothing else will release the
    lease afterwards: the runner never settles or abandons, and the member's
    residency generation is gone, so no host beat names it again. The
    activation therefore ends with its turn, as an abandon would end it.
    """
    now = [1000.0]
    _manager, supervisor, hung, turn = _hung_turn_with_a_live_sibling(now)

    lost = supervisor.residency_lost("athr_source", generation=1)

    assert lost.busy is True
    assert turn.interrupted is True
    # The stream ends the turn once the closed process has ended it.
    supervisor.complete_turn("athr_source", hung.token, turn)

    ended = supervisor.status("athr_source")
    assert ended.busy is False
    assert ended.group_busy is False
    assert ended.context_status == ended.resident_state == "lost"
    assert ended.idle_deadline_monotonic == 1060.0
    with pytest.raises(SessionLeaseLost):
        supervisor.settle("athr_source", hung.token, job_id="ajob_hung", context_status="live")
    assert supervisor.capture_snapshot("athr_peer", capture=lambda _backend: {"patch": ""}) == {
        "patch": ""
    }


def test_a_lost_turn_whose_stream_never_closes_it_is_closed_by_the_sweep():
    """A stream to a runner that stopped reading never reaches its turn's end.

    Its frames wait on a socket the runner no longer drains, so the turn the
    loss interrupted stays open with its member lock and lease. Once the
    stream has had ample time, the sweep closes that turn's boundary on its
    behalf, which ends the activation; a stream that wakes later finds it gone.
    """
    now = [1000.0]
    manager, supervisor, hung, turn = _hung_turn_with_a_live_sibling(now)
    member = supervisor._members["athr_source"].members["athr_source"]
    supervisor.residency_lost("athr_source", generation=1)

    supervisor.sweep()
    assert supervisor.status("athr_source").busy is True
    now[0] += 60

    assert supervisor.sweep() == []

    ended = supervisor.status("athr_source")
    assert ended.busy is False
    assert ended.group_busy is False
    assert ended.context_status == ended.resident_state == "lost"
    assert member.turn_lock.locked() is False
    assert manager.events[-2:] == [("reap-except", 0, ((41, 400),)), ("normalize", 0)]
    with pytest.raises(SessionLeaseLost):
        supervisor.complete_turn("athr_source", hung.token, turn)
    now[0] += 60
    assert [stopped.running for stopped in supervisor.sweep()] == [False]


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


def test_abandon_releases_the_member_lock_of_the_turn_it_detaches(monkeypatch):
    """With normal cleanup failed, abandon owns the attached turn's release."""
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=_TransportFactory())
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, launch)
    supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    assert member.turn_lock.locked() is True

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(supervisor, "complete_turn", fail_cleanup)
    supervisor.abandon("athr_source", lease.token, lost=False)

    assert member.turn_lock.locked() is False
    monkeypatch.undo()
    successor = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )
    supervisor.ensure_resident("athr_source", successor.token, launch)
    supervisor.start_turn(
        "athr_source", successor.token, prompt="again", model="glm-5.1", reasoning_effort=None
    )


def test_a_turn_detached_by_complete_turn_releases_its_lock_even_if_it_then_fails():
    """Once ``complete_turn`` detaches the turn, the member lock is its to release."""

    class FlakyTransport(_Transport):
        failing = False

        @property
        def alive(self) -> bool:
            if self.failing:
                raise RuntimeError("process table unavailable")
            return True

        @alive.setter
        def alive(self, _value: bool) -> None:
            return

    class Factory(_TransportFactory):
        def open(self, spec, launch):
            transport = FlakyTransport(launch)
            self.transports.append(transport)
            return transport

    factory = Factory()
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
    turn = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    factory.transports[0].failing = True

    with pytest.raises(RuntimeError, match="process table"):
        supervisor.complete_turn("athr_source", lease.token, turn)

    assert member.current_turn is None
    assert member.turn_lock.locked() is False


def test_abandon_leaves_a_resident_start_its_own_member_lock():
    """A slow resident start keeps its exclusion, and its process, from abandon.

    ``threading.Lock`` has no owner. Abandon released the member lock whenever
    it was held, so a runner that gave up on a slow resident start freed the
    lock that start still held: the start then installed a live process over
    the abandoned member, and its own release raised ``RuntimeError``.
    """
    entered = threading.Event()
    proceed = threading.Event()

    class SlowFactory(_TransportFactory):
        def open(self, spec, launch):
            entered.set()
            assert proceed.wait(5)
            return super().open(spec, launch)

    factory = SlowFactory()
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=factory)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    member = supervisor._members["athr_source"].members["athr_source"]
    errors: list[BaseException] = []

    def start() -> None:
        try:
            supervisor.ensure_resident(
                "athr_source",
                lease.token,
                _launch("athr_source", native_session_id="native-source", slot=0),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    assert entered.wait(5)

    abandoned = supervisor.abandon("athr_source", lease.token, lost=True)

    assert member.turn_lock.locked() is True
    assert abandoned.resident_state == "lost"
    proceed.set()
    starter.join(5)
    assert not starter.is_alive()
    assert [type(error) for error in errors] == [SessionLeaseLost]
    assert member.turn_lock.locked() is False
    assert member.transport is None
    assert (member.context_status, member.resident_state) == ("lost", "lost")
    assert [transport.alive for transport in factory.transports] == [False]


def test_abandon_leaves_a_turn_in_admission_its_own_member_lock():
    """A turn still being admitted releases its own lock and is refused."""
    admitting = threading.Event()
    proceed = threading.Event()

    class SlowCensus(_Manager):
        def capture_member_processes(self, spec, **kwargs):
            admitting.set()
            assert proceed.wait(5)
            return super().capture_member_processes(spec, **kwargs)

    factory = _TransportFactory()
    supervisor = SharedChatSessionSupervisor(SlowCensus(), transport_factory=factory)
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
    member = supervisor._members["athr_source"].members["athr_source"]
    errors: list[BaseException] = []

    def admit() -> None:
        try:
            supervisor.start_turn(
                "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    admission = threading.Thread(target=admit)
    admission.start()
    assert admitting.wait(5)

    supervisor.abandon("athr_source", lease.token, lost=False)

    assert member.turn_lock.locked() is True
    proceed.set()
    admission.join(5)
    assert not admission.is_alive()
    assert [type(error) for error in errors] == [SessionLeaseLost]
    assert member.turn_lock.locked() is False
    assert member.current_turn is None
