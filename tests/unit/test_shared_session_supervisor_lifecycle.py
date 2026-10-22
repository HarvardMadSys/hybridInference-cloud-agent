"""Lifecycle I/O fencing, admission rechecks, and journal rotation."""

from __future__ import annotations

import json

from tests.unit.test_shared_session_supervisor import (
    _HELD_S,
    _UNBLOCKED_S,
    MEMBER_UID_BASE,
    Any,
    ChatSandboxSpec,
    ChatSandboxStatus,
    GuestObserverCursor,
    ProcessBackend,
    ResidentLaunch,
    RetainedChatLost,
    SandboxError,
    SessionBusy,
    SessionLeaseLost,
    SharedChatSessionSupervisor,
    _gated_inspect,
    _in_thread,
    _launch,
    _Manager,
    _members,
    _running_group,
    _spec,
    _Transport,
    _TransportFactory,
    _Turn,
    create_app,
    logging,
    pytest,
    replace,
    threading,
)

from cloud_agent_host.member_resources import CheckpointSchedule
from cloud_agent_host.sandbox import KATA_RUNTIME
from cloud_agent_host.session_lifecycle import StaleStartFence


def test_http_observations_and_runner_heartbeats_do_not_wait_for_final_archive(monkeypatch):
    """The real endpoint/client stay responsive throughout a held upload."""
    from concurrent.futures import ThreadPoolExecutor

    from anyio.from_thread import start_blocking_portal
    from fastapi.testclient import TestClient

    from cloud_agent_host import runner
    from cloud_agent_host.session_supervisor_client import SessionSupervisorClient

    manager = _Manager()
    entered, release = threading.Event(), threading.Event()
    archived = []
    orphan = replace(_spec(), thread_id="athr_orphan", sandbox_id="asbx_orphan")

    def archive(spec, inspection, members):
        archived.append((spec, inspection, members))
        entered.set()
        assert release.wait(timeout=_HELD_S)

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)
    supervisor.register_manifest(_spec(), _members())
    manager.items[orphan.container_name] = manager._inspection(orphan, running=True)
    manager.read_running_final_archive_marker = lambda *_args, **_kwargs: b"pending"
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="test-secret",
        shared_session_supervisor=supervisor,
    )

    # A deployed app has one event loop, including its endpoint lifecycle
    # lock. Keep one portal across all concurrent synchronous HTTP clients.
    portal_context = start_blocking_portal()
    portal = portal_context.__enter__()

    def client_from_env(**kwargs):
        client = SessionSupervisorClient("http://testserver", "test-secret", **kwargs)
        client._client.close()
        client._client = TestClient(app, headers=client._headers)
        client._client.portal = portal
        return client

    monkeypatch.setattr(runner, "_session_supervisor_from_env", client_from_env)
    calls = ThreadPoolExecutor(max_workers=8)
    http = TestClient(app, headers={"X-Agent-Workspace-Token": "test-secret"})
    http.portal = portal
    try:
        # This first observation must discover AND enqueue the orphan without
        # making the HTTP request own the archive upload's lifetime.
        rows = calls.submit(runner._retained_sandbox_heartbeat_rows).result(timeout=2)
        assert rows is not None
        assert {row["sandbox_id"] for row in rows} == {"asbx_shared", "asbx_orphan"}
        assert entered.wait(timeout=2)
        beats = [calls.submit(runner._retained_sandbox_heartbeat_rows) for _ in range(16)]
        for beat in beats:
            rows = beat.result(timeout=2)
            assert rows is not None
            observed = next(row for row in rows if row["sandbox_id"] == "asbx_orphan")
            assert observed["running"] is False
            assert observed["activity_active"] is True
            assert observed["generation"] == orphan.generation
            assert observed["container_id"] == "container-shared"
        response = calls.submit(http.get, "/sandboxes").result(timeout=2)
        assert response.status_code == 200
        pending = next(
            row for row in response.json()["sandboxes"] if row["sandbox_id"] == "asbx_orphan"
        )
        assert pending["lifecycle_pending"] is True
        assert pending["group_busy"] is True
        assert pending["checkpoint"] is None
        assert calls.submit(http.get, "/health").result(timeout=2).status_code == 200
        assert len(archived) == 1
        assert manager.stop_count == 1
        assert manager.items[orphan.container_name].networks == ("agent-net",)
        with pytest.raises(SessionBusy):
            supervisor.operation_opened(orphan.thread_id)
    finally:
        release.set()
        calls.shutdown(wait=True)
        supervisor.quiesce()
        http.close()
        portal_context.__exit__(None, None, None)
    assert len(archived) == 1
    assert manager.stop_count == 2
    assert manager.items[orphan.container_name].networks == ()
    assert supervisor.status(orphan.thread_id).lifecycle_pending is False


@pytest.mark.parametrize("replace_queued_container", [False, True])
def test_background_recovery_drains_queued_groups_before_quiesce_returns(replace_queued_container):
    from concurrent.futures import ThreadPoolExecutor

    manager = _Manager()
    first, second = _spec(), replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    for spec in (first, second):
        manager.items[spec.container_name] = manager._inspection(spec, running=True)
    manager.read_running_final_archive_marker = lambda *_args, **_kwargs: b"pending"
    entered, release = threading.Event(), threading.Event()
    archived = []

    def archive(spec, *_args):
        archived.append(spec.sandbox_id)
        if spec == first:
            entered.set()
            assert release.wait(timeout=_HELD_S)

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)
    calls = ThreadPoolExecutor(max_workers=1)
    try:
        rows = supervisor.list(background_recovery=True)
        assert entered.wait(timeout=2)
        assert all(row.lifecycle_pending for row in rows)
        for _ in range(10):
            assert all(row.group_busy for row in supervisor.list(background_recovery=True))
        with pytest.raises(SessionBusy, match="lifecycle check"):
            supervisor.operation_opened(second.thread_id)
        assert archived == [first.sandbox_id]
        if replace_queued_container:
            manager.items[second.container_name] = replace(
                manager.items[second.container_name], container_id="replacement"
            )
        drained = calls.submit(supervisor.quiesce)
        with pytest.raises(TimeoutError):
            drained.result(timeout=0.05)
        assert manager.items[first.container_name].networks == ("agent-net",)
        release.set()
        drained.result(timeout=2)
        if replace_queued_container:
            assert archived == [first.sandbox_id]
            assert manager.items[second.container_name].running is True
            assert manager.items[second.container_name].networks == ("agent-net",)
            assert supervisor.status(first.thread_id).lifecycle_pending is False
        else:
            assert archived == [first.sandbox_id, second.sandbox_id]
            assert all(not row.lifecycle_pending for row in supervisor.list())
            assert all(item.networks == () for item in manager.items.values())
    finally:
        release.set()
        calls.shutdown(wait=True)
        supervisor.quiesce()


def test_background_final_archive_failure_preserves_fence_and_retries():
    manager = _Manager()
    spec = _spec()
    manager.items[spec.container_name] = manager._inspection(spec, running=True)
    manager.read_running_final_archive_marker = lambda *_args, **_kwargs: b"pending"
    manager.read_stopped_final_archive_marker_optional = lambda *_args, **_kwargs: b"pending"
    attempts = []

    def archive(*_args):
        attempts.append("archive")
        if len(attempts) == 1:
            raise SandboxError("upload failed")

    supervisor = SharedChatSessionSupervisor(manager, stopped_archive_hook=archive)
    try:
        supervisor.list(background_recovery=True)
        with supervisor._condition:
            group = supervisor._members[spec.thread_id]
            assert supervisor._condition.wait_for(lambda: group.lifecycle_check is None, timeout=2)
        assert attempts == ["archive"]
        assert supervisor.status(spec.thread_id).lifecycle_pending is True
        assert manager.items[spec.container_name].networks == ("agent-net",)
        with pytest.raises(SessionBusy):
            supervisor.operation_opened(spec.thread_id)
        supervisor.list(background_recovery=True)
        with supervisor._condition:
            assert supervisor._condition.wait_for(lambda: group.lifecycle_check is None, timeout=2)
        assert attempts == ["archive", "archive"]
        assert supervisor.status(spec.thread_id).lifecycle_pending is False
        assert manager.items[spec.container_name].networks == ()
    finally:
        supervisor.quiesce()


@pytest.mark.parametrize(
    "operation", ["idle_stop", "begin_archive", "commit_archive", "reconcile_marker"]
)
def test_lifecycle_io_leaves_other_groups_available(monkeypatch, operation):
    """A slow lifecycle check must fence its group without stalling siblings."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    other = replace(spec, thread_id="athr_other", sandbox_id="asbx_other")
    supervisor.register_manifest(other, [replace(_members()[0], thread_id=other.thread_id)])
    entered = threading.Event()
    release = threading.Event()

    def gate():
        entered.set()
        assert release.wait(timeout=_HELD_S)

    if operation == "idle_stop":
        inspect = manager.inspect

        def inspect_slow(name):
            observed = inspect(name)
            if name == spec.container_name and not entered.is_set():
                gate()
            return observed

        monkeypatch.setattr(manager, "inspect", inspect_slow)

        def call():
            return supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    elif operation in {"begin_archive", "commit_archive"}:
        manager.items[spec.container_name] = replace(
            manager.items[spec.container_name], running=False, status=ChatSandboxStatus.STOPPED
        )

        def require_stopped(owned_spec, *, container_id):
            current = manager.items[owned_spec.container_name]
            assert current.container_id == container_id
            assert not current.running
            if operation == "begin_archive":
                gate()
            return current

        monkeypatch.setattr(manager, "require_stopped_archive", require_stopped, raising=False)
        kwargs = {
            "request_id": "delete-1",
            "generation": spec.generation,
            "container_id": "container-shared",
        }
        if operation == "begin_archive":

            def call():
                return supervisor.begin_archive(spec.thread_id, **kwargs)
        else:
            supervisor.begin_archive(spec.thread_id, **kwargs)

            def remove(owned_spec, *, container_id):
                assert manager.items[owned_spec.container_name].container_id == container_id
                gate()
                manager.items.pop(owned_spec.container_name)

            monkeypatch.setattr(manager, "remove_stopped_archived", remove, raising=False)

            def call():
                return supervisor.commit_archive(
                    spec, request_id="delete-1", container_id="container-shared"
                )
    else:
        orphan = replace(spec, thread_id="athr_orphan", sandbox_id="asbx_orphan")
        manager.items[orphan.container_name] = manager._inspection(orphan)

        def read_marker(owned_spec, **_kwargs):
            if owned_spec == orphan:
                gate()
            return None

        monkeypatch.setattr(manager, "read_running_final_archive_marker", read_marker)
        call = supervisor.reconcile

    worker, outcome = _in_thread(call)
    other_worker = None
    try:
        assert entered.wait(timeout=2)
        other_worker, other_outcome = _in_thread(lambda: supervisor.status(other.thread_id))
        other_worker.join(timeout=_UNBLOCKED_S)
        assert not other_worker.is_alive(), "lifecycle I/O held the supervisor-wide lock"
        assert "error" not in other_outcome
        if operation != "reconcile_marker":
            with pytest.raises(SessionBusy):
                supervisor.operation_opened(spec.thread_id)
    finally:
        release.set()
        worker.join(timeout=5)
        if other_worker is not None:
            other_worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in outcome


@pytest.mark.parametrize("operation", ["idle_stop", "begin_archive", "reconcile_marker"])
@pytest.mark.parametrize("fence", ["index", "lifecycle", "spec", "container"])
def test_lifecycle_check_rejects_superseded_observations(monkeypatch, caplog, operation, fence):
    """An observation cannot commit to a group whose authority changed.

    Reconciliation rejects it for that group alone: it logs the refusal and
    goes on with the host's other groups rather than failing their listing.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    observed = replace(group.inspection, running=False, status=ChatSandboxStatus.STOPPED)

    def changed(*_args, **_kwargs):
        with supervisor._lock:
            if fence == "index":
                supervisor._groups.pop(spec.sandbox_id)
            elif fence == "lifecycle":
                group.lifecycle_generation += 1
            elif fence == "spec":
                group.spec = replace(spec, generation=spec.generation + 1)
            else:
                group.inspection = replace(group.inspection, container_id="replacement")
        return observed

    if operation == "idle_stop":
        monkeypatch.setattr(manager, "inspect", changed)

        def call():
            return supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    elif operation == "reconcile_marker":
        group.orphan_marker_unreadable = True
        monkeypatch.setattr(manager, "read_running_final_archive_marker", changed)
        call = supervisor.reconcile
    else:
        monkeypatch.setattr(manager, "require_stopped_archive", changed, raising=False)

        def call():
            return supervisor.begin_archive(
                spec.thread_id,
                request_id="delete-1",
                generation=spec.generation,
                container_id=observed.container_id,
            )

    if operation == "reconcile_marker":
        with caplog.at_level(logging.WARNING):
            call()
        refusals = [
            str(getattr(record, "detail", ""))
            for record in caplog.records
            if getattr(record, "event", None) == "shared_sandbox_reconcile_group_failed"
        ]
        assert len(refusals) == 1
        assert "lifecycle changed" in refusals[0]
    else:
        with pytest.raises(SessionLeaseLost, match="lifecycle changed"):
            call()
    assert manager.stop_count == 0
    assert group.inspection.running is True
    assert group.archive_request_id is None


@pytest.mark.parametrize("operation", ["idle_stop", "begin_archive"])
def test_failed_lifecycle_proof_releases_only_its_temporary_reservation(monkeypatch, operation):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]

    def failed(*_args, **_kwargs):
        raise SandboxError("Docker did not answer")

    with monkeypatch.context() as patch:
        if operation == "idle_stop":
            patch.setattr(manager, "inspect", failed)
            with pytest.raises(SandboxError, match="did not answer"):
                supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
        else:
            patch.setattr(manager, "require_stopped_archive", failed, raising=False)
            with pytest.raises(SandboxError, match="did not answer"):
                supervisor.begin_archive(
                    spec.thread_id,
                    request_id="delete-1",
                    generation=spec.generation,
                    container_id=group.inspection.container_id,
                )
    assert group.lifecycle_check is None
    assert group.archive_request_id is None
    assert not group.stopping
    token, descriptor = supervisor.operation_opened(spec.thread_id)
    assert descriptor.running
    supervisor.operation_closed(spec.thread_id, token)


def test_absence_proof_rechecks_registration_after_slow_docker_reads(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    entered, release = _gated_inspect(manager, monkeypatch, spec.container_name)
    worker, outcome = _in_thread(
        lambda: supervisor.admin_deletion_target_absent(
            spec.thread_id, sandbox_id=spec.sandbox_id, container_id="container-shared"
        )
    )
    try:
        assert entered.wait(timeout=2)
        supervisor.register_manifest(spec, _members())
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome == {"value": False}


def test_absent_cleanup_fences_registration_until_callback_returns():
    creation_started = threading.Event()

    class Manager(_Manager):
        def create(self, spec, *, facts=None, observer=None):
            creation_started.set()
            return super().create(spec, facts=facts, observer=observer)

    manager = Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    cleanup_entered, release_cleanup = threading.Event(), threading.Event()

    def cleanup(container_name):
        assert container_name == spec.container_name
        cleanup_entered.set()
        assert release_cleanup.wait(timeout=_HELD_S)

    deletion, deletion_outcome = _in_thread(
        lambda: supervisor.admin_deletion_target_absent(
            spec.thread_id,
            sandbox_id=spec.sandbox_id,
            container_id="container-shared",
            on_absent=cleanup,
        )
    )
    assert cleanup_entered.wait(timeout=_UNBLOCKED_S)
    admission_started = threading.Event()

    def register():
        admission_started.set()
        return supervisor.register_manifest(spec, _members())

    registration, registration_outcome = _in_thread(register)
    try:
        assert admission_started.wait(timeout=_UNBLOCKED_S)
        assert not creation_started.wait(timeout=0.1)
        assert registration.is_alive()
    finally:
        release_cleanup.set()
        deletion.join(timeout=_UNBLOCKED_S)
        registration.join(timeout=_UNBLOCKED_S)

    assert not deletion.is_alive()
    assert not registration.is_alive()
    assert deletion_outcome == {"value": True}
    assert "error" not in registration_outcome
    assert creation_started.is_set()
    assert supervisor.owns(spec.thread_id)


def test_deletion_retries_upload_receipt_cleanup_after_failure(monkeypatch):
    from cloud_agent_host import shared_session_supervisor as supervisor_module

    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    manager.items[spec.container_name] = replace(
        group.inspection, running=False, status=ChatSandboxStatus.STOPPED
    )
    retired = []

    def require_stopped(owned_spec, *, container_id):
        current = manager.items[owned_spec.container_name]
        assert current.container_id == container_id
        return current

    def remove(owned_spec, *, container_id):
        assert manager.items[owned_spec.container_name].container_id == container_id
        manager.items.pop(owned_spec.container_name)

    def retire(container_name):
        retired.append(container_name)
        if len(retired) == 1:
            raise OSError("receipt directory sync failed")

    monkeypatch.setattr(manager, "require_stopped_archive", require_stopped, raising=False)
    monkeypatch.setattr(manager, "remove_stopped_archived", remove, raising=False)
    monkeypatch.setattr(
        supervisor_module,
        "forget_deleted_container_uploads",
        retire,
    )
    supervisor.begin_archive(
        spec.thread_id,
        request_id="delete-1",
        generation=spec.generation,
        container_id=group.inspection.container_id,
    )

    with pytest.raises(OSError, match="directory sync"):
        supervisor.commit_archive(
            spec, request_id="delete-1", container_id=group.inspection.container_id
        )
    assert supervisor._groups[spec.sandbox_id] is group
    supervisor.commit_archive(
        spec, request_id="delete-1", container_id=group.inspection.container_id
    )
    supervisor.commit_archive(
        spec, request_id="delete-1", container_id=group.inspection.container_id
    )

    assert retired == [spec.container_name] * 3


@pytest.mark.parametrize("replacement", ["stable_name", "renamed_original"])
def test_deletion_proves_both_container_identities_absent_before_forgetting_group(
    monkeypatch, replacement
):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    manager.items[spec.container_name] = replace(
        group.inspection, running=False, status=ChatSandboxStatus.STOPPED
    )

    def require_stopped(owned_spec, *, container_id):
        current = manager.items[owned_spec.container_name]
        assert current.container_id == container_id
        return current

    def remove(owned_spec, *, container_id):
        original = manager.items.pop(owned_spec.container_name)
        if replacement == "stable_name":
            manager.items[owned_spec.container_name] = replace(original, container_id="replacement")
        else:
            manager.items[container_id] = replace(original, name="renamed")

    monkeypatch.setattr(manager, "require_stopped_archive", require_stopped, raising=False)
    monkeypatch.setattr(manager, "remove_stopped_archived", remove, raising=False)
    supervisor.begin_archive(
        spec.thread_id,
        request_id="delete-1",
        generation=spec.generation,
        container_id=group.inspection.container_id,
    )
    with pytest.raises(RetainedChatLost, match="identity changed"):
        supervisor.commit_archive(
            spec, request_id="delete-1", container_id=group.inspection.container_id
        )
    assert supervisor._groups[spec.sandbox_id] is group
    assert supervisor._members[spec.thread_id] is group
    assert group.archive_request_id == "delete-1"
    assert group.stopping
    assert group.lifecycle_check is None


def test_refused_busy_deletion_does_not_invalidate_active_lifecycle(monkeypatch):
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    token, _descriptor = supervisor.operation_opened(spec.thread_id)
    group = supervisor._members[spec.thread_id]
    generation = group.lifecycle_generation

    def inspect(*_args):
        pytest.fail("a refused busy deletion must not perform Docker I/O")

    monkeypatch.setattr(manager, "inspect", inspect)
    with pytest.raises(SessionBusy, match="idle group"):
        supervisor.commit_archive(
            spec, request_id="delete-1", container_id=group.inspection.container_id
        )
    assert group.lifecycle_generation == generation
    assert group.lifecycle_check is None
    assert group.archive_request_id is None
    supervisor.operation_closed(spec.thread_id, token)


class _RecheckingStartManager(_Manager):
    """Hold the bridge recheck a concurrent workspace request performs."""

    def __init__(self) -> None:
        super().__init__()
        self.block_start = False
        self.start_entered = threading.Event()
        self.start_release = threading.Event()

    def start(self, spec, **kwargs):
        if self.block_start:
            self.start_entered.set()
            assert self.start_release.wait(timeout=5)
        expected = kwargs.get("expected_container_id")
        if expected is not None and self.items[spec.container_name].container_id != expected:
            raise RetainedChatLost("chat sandbox container identity changed before start")
        return super().start(spec, **kwargs)


def _rechecking_activation():
    manager = _RecheckingStartManager()
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
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", lease.token, launch)
    manager.block_start = True
    return manager, supervisor, factory, lease, launch


@pytest.mark.parametrize("call", ["resident", "turn", "late_turn"])
def test_prompt_start_waits_for_a_workspace_bridge_recheck(monkeypatch, call):
    """A files/terminal request must not destroy an admitted but unsent turn."""
    manager, supervisor, factory, lease, launch = _rechecking_activation()
    waited = threading.Event()
    wait = supervisor._condition.wait

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    reading, read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    if call == "late_turn":
        # Let the first admission check pass, then race the process census.
        manager.start_release.set()
        reading.join(timeout=2)
        supervisor.operation_closed("athr_peer", read_outcome["value"][0])
        manager.start_entered.clear()
        manager.start_release.clear()
        census = manager.capture_member_processes

        def capture(*args, **kwargs):
            nonlocal reading, read_outcome
            reading, read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
            assert manager.start_entered.wait(timeout=2)
            return census(*args, **kwargs)

        monkeypatch.setattr(manager, "capture_member_processes", capture)
    else:
        assert manager.start_entered.wait(timeout=2)

    def activate():
        if call == "resident":
            return supervisor.ensure_resident("athr_source", lease.token, launch)
        return supervisor.start_turn(
            "athr_source",
            lease.token,
            prompt="read PR state",
            model="glm-5.1",
            reasoning_effort=None,
        )

    activating, outcome = _in_thread(activate)
    try:
        assert waited.wait(timeout=1), outcome
        assert factory.transports[0].turns == []
    finally:
        manager.start_release.set()
        reading.join(timeout=2)
        activating.join(timeout=2)
    assert not reading.is_alive() and not activating.is_alive()
    assert "error" not in read_outcome
    assert "error" not in outcome
    assert len(factory.transports[0].turns) == (0 if call == "resident" else 1)
    supervisor.operation_closed("athr_peer", read_outcome["value"][0])


@pytest.mark.parametrize("invalidate", ["lease", "container"])
def test_prompt_wait_rechecks_the_lease_and_container_before_dispatch(monkeypatch, invalidate):
    manager, supervisor, factory, lease, _launch_spec = _rechecking_activation()
    waited = threading.Event()
    wait = supervisor._condition.wait

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    reading, _read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    assert manager.start_entered.wait(timeout=2)
    activating, outcome = _in_thread(
        lambda: supervisor.start_turn(
            "athr_source",
            lease.token,
            prompt="must not send",
            model="glm-5.1",
            reasoning_effort=None,
        )
    )
    try:
        assert waited.wait(timeout=1), outcome
        if invalidate == "lease":
            supervisor.abandon("athr_source", lease.token, lost=False)
        else:
            current = manager.items[_spec().container_name]
            manager.items[_spec().container_name] = replace(current, container_id="replacement")
    finally:
        manager.start_release.set()
        reading.join(timeout=2)
        activating.join(timeout=2)
    assert not reading.is_alive() and not activating.is_alive()
    assert isinstance(
        outcome.get("error"), SessionLeaseLost if invalidate == "lease" else RetainedChatLost
    )
    assert factory.transports[0].turns == []


def test_prompt_wait_does_not_accept_a_failed_bridge_recheck(monkeypatch):
    manager, supervisor, factory, lease, _launch_spec = _rechecking_activation()
    waited = threading.Event()
    wait = supervisor._condition.wait
    start = manager.start

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    def fail_start(*args, **kwargs):
        start(*args, **kwargs)
        raise SandboxError("bridge recheck failed")

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    monkeypatch.setattr(manager, "start", fail_start)
    reading, read_outcome = _in_thread(lambda: supervisor.operation_opened("athr_peer"))
    assert manager.start_entered.wait(timeout=2)
    activating, outcome = _in_thread(
        lambda: supervisor.start_turn(
            "athr_source",
            lease.token,
            prompt="must not send",
            model="glm-5.1",
            reasoning_effort=None,
        )
    )
    try:
        assert waited.wait(timeout=1), outcome
    finally:
        manager.start_release.set()
        reading.join(timeout=2)
        activating.join(timeout=2)
    assert not reading.is_alive() and not activating.is_alive()
    assert isinstance(read_outcome.get("error"), SandboxError)
    assert isinstance(outcome.get("error"), SandboxError)
    assert factory.transports[0].turns == []
    # A fresh, successful check lifts the failed-proof fence.
    monkeypatch.setattr(manager, "start", start)
    token, _ = supervisor.operation_opened("athr_peer")
    supervisor.operation_closed("athr_peer", token)
    supervisor.start_turn(
        "athr_source", lease.token, prompt="now verified", model="glm-5.1", reasoning_effort=None
    )
    assert len(factory.transports[0].turns) == 1


def test_failed_idle_stop_keeps_its_automatic_retry_deadline(monkeypatch):
    """A temporary inspection reservation must not suppress the stop retry."""
    manager = _Manager()
    now = {"value": 100.0}
    supervisor = SharedChatSessionSupervisor(manager, clock=lambda: now["value"])
    spec = _spec()
    supervisor.register_manifest(spec, _members())
    group = supervisor._members[spec.thread_id]
    group.idle_deadline = now["value"]
    stop = manager.stop

    def fail_stop(*_args, **_kwargs):
        raise SandboxError("Docker stop is temporarily unavailable")

    monkeypatch.setattr(manager, "stop", fail_stop)
    with pytest.raises(SandboxError, match="temporarily unavailable"):
        supervisor.stop_if_idle(spec.thread_id, generation=spec.generation)
    assert group.lifecycle_check is None
    assert group.stopping is False
    assert group.idle_deadline == now["value"] + supervisor.idle_timeout_s
    monkeypatch.setattr(manager, "stop", stop)
    now["value"] += supervisor.idle_timeout_s + 1
    stopped = supervisor.sweep()
    assert len(stopped) == 1
    assert stopped[0].running is False


@pytest.mark.parametrize("entrypoint", ["operation", "terminal"])
@pytest.mark.parametrize("race", ["before", "after", "membership", "container", "failure"])
def test_workspace_admission_waits_for_bridge_rechecks(monkeypatch, entrypoint, race):
    """Files and PTYs wait at both admission boundaries, then recheck identity."""
    manager, supervisor, _factory, _lease, _launch_spec = _rechecking_activation()
    group = supervisor._members["athr_source"]
    opened = getattr(supervisor, f"{entrypoint}_opened")
    closed = getattr(supervisor, f"{entrypoint}_closed")
    waited = threading.Event()
    wait = supervisor._condition.wait
    rechecks = []

    def waiting(*args, **kwargs):
        waited.set()
        return wait(*args, **kwargs)

    def begin_recheck():
        manager.block_start = True
        rechecks.append(_in_thread(lambda: supervisor._start_group(group)))
        assert manager.start_entered.wait(timeout=2)

    monkeypatch.setattr(supervisor._condition, "wait", waiting)
    if race == "before":
        begin_recheck()
    else:
        # Start the competing check after this request's own check, before it
        # can register its operation/PTY token. No sleep-based race timing.
        manager.block_start = False
        ensure_running = supervisor._ensure_running
        start = manager.start

        def ensure_then_recheck(current_group, **kwargs):
            ensure_running(current_group, **kwargs)
            if race == "failure":

                def fail_start(*args, **kwargs):
                    start(*args, **kwargs)
                    raise SandboxError("bridge recheck failed")

                monkeypatch.setattr(manager, "start", fail_start)
            begin_recheck()

        monkeypatch.setattr(supervisor, "_ensure_running", ensure_then_recheck)

    opening, outcome = _in_thread(lambda: opened("athr_source"))
    try:
        assert waited.wait(timeout=1), outcome
        assert group.operation_tokens == {} and group.terminal_tokens == {}
        if race == "membership":
            with supervisor._lock:
                group.members["athr_source"] = replace(group.members["athr_source"])
        elif race == "container":
            current = manager.items[_spec().container_name]
            manager.items[_spec().container_name] = replace(current, container_id="replacement")
    finally:
        manager.start_release.set()
        opening.join(timeout=2)
        for checking, _ in rechecks:
            checking.join(timeout=2)
    assert not opening.is_alive()
    assert all(not checking.is_alive() for checking, _ in rechecks)
    if race in {"before", "after"}:
        assert "error" not in outcome
        assert all("error" not in result for _, result in rechecks)
        token, descriptor = outcome["value"]
        assert descriptor.running is True
        closed("athr_source", token)
    else:
        expected_error = {
            "membership": SessionLeaseLost,
            "container": RetainedChatLost,
            "failure": SandboxError,
        }[race]
        assert isinstance(outcome.get("error"), expected_error), outcome
    assert group.operation_tokens == {} and group.terminal_tokens == {}


def _fenced_supervisor(
    *, sealed_by_checkpoint: int | None = None
) -> tuple[_Manager, SharedChatSessionSupervisor, list[int | None]]:
    """A supervisor whose stops record the label their checkpoint would carry."""
    manager = _Manager()
    labels: list[int | None] = []
    supervisor = SharedChatSessionSupervisor(
        manager,
        stopped_archive_hook=lambda spec, *_args: labels.append(
            supervisor.start_seq_for(spec.sandbox_id)
        ),
    )
    supervisor.checkpoint_seq_for = lambda _spec, _inspection: sealed_by_checkpoint
    supervisor.register_manifest(_spec(), _members())
    return manager, supervisor, labels


def _run_once(supervisor: SharedChatSessionSupervisor, *, start_seq: int | None) -> None:
    token, _ = supervisor.operation_opened("athr_source", start_seq=start_seq)
    supervisor.operation_closed("athr_source", token)


def test_a_run_is_labelled_only_with_requests_it_admitted() -> None:
    """A request registered but not yet here cannot lend its fence to another's run.

    Two workspace requests took fences 1 and 2; only the first reached the
    group before it stopped. Its checkpoint used to name 2, the fence the
    control plane holds, and so covered the group without the second's work.
    """
    manager, supervisor, labels = _fenced_supervisor()

    _run_once(supervisor, start_seq=1)
    assert supervisor.stop_if_idle("athr_source", generation=1)

    assert labels == [1]
    # The second request arrives later and starts its own run.
    _run_once(supervisor, start_seq=2)
    assert supervisor.start_seq_for("asbx_shared") == 2
    assert manager.items[_spec().container_name].running


def test_a_request_older_than_the_last_stop_cannot_start_the_group_again() -> None:
    """Its fence is sealed: the checkpoint of that stop may be what the group moves with."""
    manager, supervisor, labels = _fenced_supervisor()
    _run_once(supervisor, start_seq=2)
    assert supervisor.stop_if_idle("athr_source", generation=1)
    assert labels == [2]

    for late in (1, 2):
        with pytest.raises(StaleStartFence):
            supervisor.operation_opened("athr_source", start_seq=late)
        with pytest.raises(StaleStartFence):
            supervisor.terminal_opened("athr_source", start_seq=late)
    assert not manager.items[_spec().container_name].running

    # The refusal is no failed start: the control plane's next fence starts it.
    _run_once(supervisor, start_seq=3)
    assert supervisor.start_seq_for("asbx_shared") == 3


def test_requests_joining_a_running_group_raise_its_label_or_unlabel_it() -> None:
    _manager, supervisor, labels = _fenced_supervisor()
    first, _ = supervisor.operation_opened("athr_source", start_seq=4)

    _run_once(supervisor, start_seq=2)
    assert supervisor.start_seq_for("asbx_shared") == 4
    _run_once(supervisor, start_seq=6)
    assert supervisor.start_seq_for("asbx_shared") == 6
    # A request without a fence, as from an older control plane.
    _run_once(supervisor, start_seq=None)
    supervisor.operation_closed("athr_source", first)
    assert supervisor.stop_if_idle("athr_source", generation=1)

    # Its checkpoint names no fence, so it covers nothing.
    assert labels == [None]


def test_a_group_found_after_a_restart_seals_its_last_checkpoint() -> None:
    """The fence of the last checkpoint kept on disk survives the restart."""
    manager, supervisor, labels = _fenced_supervisor(sealed_by_checkpoint=5)
    # Found going after the restart, it stops before any request names a fence.
    assert supervisor.stop_if_idle("athr_source", generation=1)
    assert labels == [None]

    with pytest.raises(StaleStartFence):
        supervisor.operation_opened("athr_source", start_seq=5)
    assert not manager.items[_spec().container_name].running

    _run_once(supervisor, start_seq=6)
    assert supervisor.start_seq_for("asbx_shared") == 6


def _journal_sample_line(measurement_id: str, sample_seq: int) -> bytes:
    """One complete window_sample census record, as the guest observer writes it."""
    record = {
        "version": 2,
        "seq": sample_seq,
        "kind": "window_sample",
        "mono_ns": 1_000_000 * (sample_seq + 1),
        "measurement_id": measurement_id,
        "uid": 11001,
        "window": {
            "sample_seq": sample_seq,
            "counters": 0x3F,
            "tasks": 1,
            "address_spaces": 1,
            "pss_bytes": 4096,
            "pss_failed": 0,
            "cpu_ns": 5_000_000_000,
            "read_bytes": 100,
            "write_bytes": 20,
            "task_detail": [
                {
                    "tid": 7,
                    "tgid": 7,
                    "uid": 11001,
                    "starttime": 100,
                    "cpu_ns": 5_000_000_000,
                    "read_bytes": 100,
                    "write_bytes": 20,
                    "counters": 0x3F,
                }
            ],
        },
    }
    return json.dumps(record).encode() + b"\n"


class _RotatingJournalManager(_Manager):
    """A manager with a simulated guest journal that tests can rotate.

    The journal is a byte buffer with a device/inode identity, exactly what
    ``member_window_journal_delta`` fences on: a read past the end of the
    buffer raises the same "behind the read fence" error the real guest reader
    raises when its file was rotated away.
    """

    def __init__(self) -> None:
        super().__init__()
        self.journal = bytearray()
        self.journal_dev = 100
        self.journal_ino = 200
        self.begin_count = 0

    def begin_guest_observation(
        self,
        spec: ChatSandboxSpec,
        *,
        user_uid: int,
        measurement_id: str | None = None,
    ) -> GuestObserverCursor:
        self.events.append(("observe-begin", user_uid))
        self.begin_count += 1
        return GuestObserverCursor(
            uid=user_uid,
            offset=len(self.journal),
            journal_dev=self.journal_dev,
            journal_ino=self.journal_ino,
            guest_boot_id="boot-1",
            guest_mono_ns=1_000_000 * self.begin_count,
            available=True,
            measurement_id=measurement_id,
        )

    def member_window_journal_delta(
        self,
        spec: ChatSandboxSpec,
        cursor: GuestObserverCursor,
        *,
        timeout_s: float = 10.0,
    ) -> tuple[bytes, int]:
        del spec, timeout_s
        if len(self.journal) < cursor.offset:
            raise SandboxError(
                "member window journal read failed: observer journal is behind the read fence"
            )
        payload = bytes(self.journal[cursor.offset :])
        return payload, cursor.offset + len(payload)

    def rotate_journal(self, *, new_size: int = 0) -> None:
        self.journal = bytearray(new_size)
        self.journal_ino += 1

    def append_journal(self, payload: bytes) -> None:
        self.journal.extend(payload)


def test_a_second_window_reads_the_journal_from_its_own_opening_cursor():
    """Census must not leak across windows, nor be lost after a rotation.

    ``window_journal_offset`` used to start at zero and survive across turns:
    the first window read pre-window bytes, and once the journal rotated, a
    later window's stale offset sat behind the new file's read fence, so the
    suppressed read error silently emptied every later checkpoint.
    """
    manager = _RotatingJournalManager()
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
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

    # Pre-window bytes: a census written before this member's window opened.
    pre_window = _journal_sample_line("0" * 32, 1)
    manager.append_journal(pre_window)

    turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work",
        model="glm-5.1",
        reasoning_effort=None,
    )
    first_window = supervisor._groups["asbx_shared"].members["athr_source"].window
    assert first_window is not None
    measurement = first_window.handle.measurement_id

    # One in-window census. It is the only cycle this window may count.
    manager.append_journal(_journal_sample_line(measurement, 2))
    assert supervisor.sample_open_windows() == 1
    checkpoint = supervisor.take_reported_checkpoint("athr_source")
    assert checkpoint is not None
    assert checkpoint["memory"]["valid_samples"] == 1

    supervisor.complete_turn("athr_source", lease.token, turn)

    # The journal rotates between the two windows: a new file replaces the
    # old one, so every offset into the old file is behind the new fence.
    manager.rotate_journal()

    second_turn = supervisor.start_turn(
        "athr_source",
        lease.token,
        prompt="work again",
        model="glm-5.1",
        reasoning_effort=None,
    )
    second_window = supervisor._groups["asbx_shared"].members["athr_source"].window
    assert second_window is not None
    second_measurement = second_window.handle.measurement_id
    assert second_measurement != measurement

    manager.append_journal(_journal_sample_line(second_measurement, 3))
    assert supervisor.sample_open_windows() == 1
    checkpoint = supervisor.take_reported_checkpoint("athr_source")
    assert checkpoint is not None
    # The rotated journal was read from its own beginning: the cycle the
    # second window wrote is in the checkpoint instead of being silently lost.
    assert checkpoint["memory"]["valid_samples"] == 1

    supervisor.complete_turn("athr_source", lease.token, second_turn)


class _JournalReadCountingManager(_RotatingJournalManager):
    """Counts every read of the simulated guest journal."""

    def __init__(self) -> None:
        super().__init__()
        self.journal_reads = 0

    def member_window_journal_delta(
        self,
        spec: ChatSandboxSpec,
        cursor: GuestObserverCursor,
        *,
        timeout_s: float = 10.0,
    ) -> tuple[bytes, int]:
        self.journal_reads += 1
        return super().member_window_journal_delta(spec, cursor, timeout_s=timeout_s)


def test_the_census_reads_the_journal_only_for_a_checkpoint():
    """A census writes its cycle; only a checkpoint reads the journal back.

    Folding the journal after every census cost each running member a second
    guest exec every second, although nothing between two checkpoints reads
    the fold. Folding at the checkpoint loses no cycle: the window's cursor
    keeps the journal from rotating while it is open.
    """
    manager = _JournalReadCountingManager()
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
    member = supervisor._groups["asbx_shared"].members["athr_source"]
    assert member.window is not None
    measurement = member.window.handle.measurement_id
    # Sixteen simulated seconds of the production cadence: a census every
    # second, a checkpoint every fifteen.
    now = [0.0]
    member.window.checkpoints = CheckpointSchedule(interval_s=15.0, clock=lambda: now[0])
    checkpoints: list[dict[str, Any]] = []
    for second in range(16):
        member.window.next_census_mono = 0.0
        manager.append_journal(_journal_sample_line(measurement, second + 1))
        assert supervisor.sample_open_windows() == 1
        checkpoint = supervisor.take_reported_checkpoint("athr_source")
        if checkpoint is not None:
            checkpoints.append(checkpoint)
        now[0] += 1.0

    assert manager.events.count(("member-window", "sample")) == 16
    assert manager.journal_reads == 2
    assert [checkpoint["memory"]["valid_samples"] for checkpoint in checkpoints] == [1, 16]

    supervisor.complete_turn("athr_source", lease.token, turn)


class _CensusOverlapManager(_Manager):
    """Records how censuses overlap, within a container and across them."""

    def __init__(self) -> None:
        super().__init__()
        self.backend.runtime = KATA_RUNTIME
        # Each container's first census waits here for another container's.
        # Censuses taken one after another never meet: the wait runs out.
        self.rendezvous = threading.Barrier(2, timeout=_UNBLOCKED_S)
        self.met: list[str] = []
        self.most_in_flight: dict[str, int] = {}
        self._in_flight: dict[str, int] = {}
        self._guard = threading.Lock()

    def member_window_request(self, spec: ChatSandboxSpec, argv: list[str], *, timeout_s: float):
        if argv[1] != "sample":
            return super().member_window_request(spec, argv, timeout_s=timeout_s)
        name = spec.container_name
        with self._guard:
            first = name not in self.most_in_flight
            self._in_flight[name] = self._in_flight.get(name, 0) + 1
            self.most_in_flight[name] = max(self.most_in_flight.get(name, 0), self._in_flight[name])
        try:
            if first:
                self.rendezvous.wait()
                self.met.append(name)
            return super().member_window_request(spec, argv, timeout_s=timeout_s)
        finally:
            with self._guard:
                self._in_flight[name] -= 1


def test_separate_containers_are_censused_at_once_and_one_container_in_order():
    """One slow guest must not stretch every other container's census period.

    Each census is a Docker exec into its own container's guest. Taken one
    after another, every concurrent turn on the host delayed all the others.
    The censuses of one container still reach its single observer one at a
    time.
    """
    manager = _CensusOverlapManager()
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=_TransportFactory(),
        window_sample_interval_s=10.0,
    )
    other = replace(_spec(), thread_id="athr_other", sandbox_id="asbx_other")
    leases = {
        "athr_source": supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
        ),
        "athr_peer": supervisor.acquire(
            _spec(),
            thread_id="athr_peer",
            owner="attempt:2",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
        ),
        "athr_other": supervisor.acquire(
            other,
            thread_id="athr_other",
            owner="attempt:3",
            activation_generation=1,
            has_settled_context=True,
            manifest=[replace(_members()[0], thread_id="athr_other")],
        ),
    }
    launches = {
        "athr_source": _launch("athr_source", native_session_id="native-source", slot=0),
        "athr_peer": _launch("athr_peer", native_session_id="native-peer", slot=1),
        "athr_other": _launch("athr_other", native_session_id="native-source", slot=0),
    }
    supervisor._stop.set()
    if supervisor._sampler_thread is not None:
        supervisor._sampler_thread.join(timeout=2)
    for thread_id, lease in leases.items():
        supervisor.ensure_resident(thread_id, lease.token, launches[thread_id])
        supervisor.start_turn(
            thread_id,
            lease.token,
            prompt="work",
            model="glm-5.1",
            reasoning_effort=None,
        )

    assert supervisor.sample_open_windows() == 3

    assert sorted(manager.met) == sorted([_spec().container_name, other.container_name])
    assert manager.most_in_flight == {_spec().container_name: 1, other.container_name: 1}
    assert manager.events.count(("member-window", "sample")) == 3


def _windowed_supervisor(manager: _Manager, factory: _TransportFactory | None = None):
    manager.backend.runtime = KATA_RUNTIME
    supervisor = SharedChatSessionSupervisor(
        manager,
        transport_factory=factory or _TransportFactory(),
        window_sample_interval_s=10.0,
        checkpoint_interval_s=0.0,
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
    return supervisor, lease


def test_a_checkpoint_left_when_its_turn_ends_never_reaches_the_next_turn():
    """A parked checkpoint goes with the window that produced it.

    The store fixes an attempt's measurement at the first snapshot it accepts.
    A checkpoint the sampler parked after the turn stream's last collection,
    handed to the next turn of the same chat, would claim that turn's attempt
    for a window that had already ended, and the new window's own reports would
    then be refused.
    """
    supervisor, lease = _windowed_supervisor(_Manager())
    first = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    # Parked, and never taken: the stream had already made its last collection.
    supervisor.sample_open_windows()
    supervisor.complete_turn("athr_source", lease.token, first)

    second = supervisor.start_turn(
        "athr_source", lease.token, prompt="again", model="glm-5.1", reasoning_effort=None
    )
    assert supervisor.take_reported_checkpoint("athr_source") is None

    supervisor.sample_open_windows()
    pending = supervisor.take_reported_checkpoint("athr_source")
    window = supervisor._groups["asbx_shared"].members["athr_source"].window
    assert pending is not None and window is not None
    assert pending["measurement_id"] == window.handle.measurement_id
    # Each window numbers its own reports: the store orders them per measurement.
    assert pending["report_seq"] == 1
    supervisor.complete_turn("athr_source", lease.token, second)


def test_a_checkpoint_built_across_a_turn_boundary_never_reaches_the_next_turn():
    """A checkpoint still being built when its turn ends never reaches the next turn.

    The sampler builds a checkpoint outside the supervisor lock, so a turn can
    end and the member's next turn open before the checkpoint is parked. It
    used to be parked on the member, which by then held the next window, so the
    next turn forwarded the previous window's snapshot. The store fixes an
    attempt's measurement at the first snapshot it accepts, so that snapshot
    claimed the new attempt for a window that had already ended, and the new
    window's own reports were then refused.
    """
    read_held = threading.Event()
    release_read = threading.Event()

    class HeldJournalManager(_RotatingJournalManager):
        """Holds one journal read, its slice already taken, until the test releases it."""

        reads = 0
        hold_read: int | None = None

        def member_window_journal_delta(self, spec, cursor, *, timeout_s=10.0):
            self.reads += 1
            read = super().member_window_journal_delta(spec, cursor, timeout_s=timeout_s)
            if self.reads == self.hold_read:
                read_held.set()
                assert release_read.wait(timeout=_HELD_S)
            return read

    manager = HeldJournalManager()
    supervisor, lease = _windowed_supervisor(manager)
    member = supervisor._groups["asbx_shared"].members["athr_source"]
    first = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    manager.append_journal(_journal_sample_line(member.observer_cursor.measurement_id, 2))
    # A tick's census only writes its cycle, so the checkpoint it then builds
    # makes the tick's one read: that is the one held.
    manager.hold_read = manager.reads + 1
    tick, outcome = _in_thread(supervisor.sample_open_windows)
    try:
        assert read_held.wait(timeout=_HELD_S)
        supervisor.complete_turn("athr_source", lease.token, first)
        second = supervisor.start_turn(
            "athr_source", lease.token, prompt="again", model="glm-5.1", reasoning_effort=None
        )
    finally:
        release_read.set()
        tick.join(timeout=_HELD_S)
    assert not tick.is_alive()
    assert "error" not in outcome

    assert supervisor.take_reported_checkpoint("athr_source") is None
    supervisor.complete_turn("athr_source", lease.token, second)


def test_a_journal_read_that_outlives_its_turn_does_not_move_the_next_windows_fence():
    """A journal read still running when the next turn opens leaves that turn's fence.

    A checkpoint reads the journal outside the supervisor lock, so a turn can
    end and the member's next turn open while a read is still running. That
    read used to record where it stopped on the member, which by then held the
    next window, so the next window's first read began inside the previous
    turn's bytes.
    """
    read_held = threading.Event()
    release_read = threading.Event()

    class HeldJournalManager(_RotatingJournalManager):
        """Holds one journal read, its slice already taken, until the test releases it."""

        hold_next_read = False

        def __init__(self) -> None:
            super().__init__()
            self.reads: list[tuple[str | None, int]] = []

        def member_window_journal_delta(self, spec, cursor, *, timeout_s=10.0):
            self.reads.append((cursor.measurement_id, cursor.offset))
            read = super().member_window_journal_delta(spec, cursor, timeout_s=timeout_s)
            if self.hold_next_read:
                self.hold_next_read = False
                read_held.set()
                assert release_read.wait(timeout=_HELD_S)
            return read

    manager = HeldJournalManager()
    supervisor, lease = _windowed_supervisor(manager)
    member = supervisor._groups["asbx_shared"].members["athr_source"]
    first = supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )
    first_measurement = member.observer_cursor.measurement_id
    manager.append_journal(_journal_sample_line(first_measurement, 2))
    manager.hold_next_read = True
    census, outcome = _in_thread(supervisor.sample_open_windows)
    try:
        assert read_held.wait(timeout=_HELD_S)
        supervisor.complete_turn("athr_source", lease.token, first)
        # The closing census lands after the held read took its slice.
        manager.append_journal(_journal_sample_line(first_measurement, 3))
        second = supervisor.start_turn(
            "athr_source", lease.token, prompt="again", model="glm-5.1", reasoning_effort=None
        )
        fence = member.observer_cursor
    finally:
        release_read.set()
        census.join(timeout=_HELD_S)
    assert not census.is_alive()
    assert "error" not in outcome

    # Released, the held read lands on the window it was made for. The next
    # tick checkpoints the next window, whose first read starts where it opened.
    supervisor.sample_open_windows()
    reads = [offset for measurement, offset in manager.reads if measurement == fence.measurement_id]
    assert reads[:1] == [fence.offset]
    supervisor.complete_turn("athr_source", lease.token, second)


def test_a_turn_refused_at_admission_closes_the_window_it_opened():
    """A turn that loses its lease while starting closes its window with its cursor.

    The turn is interrupted and refused, so no turn is left to close the
    window's registration, which would otherwise keep measuring a member that
    runs nothing.
    """
    holder: dict[str, Any] = {}

    class LeaseLosingTransport(_Transport):
        def start_turn(self, **kwargs) -> _Turn:
            turn = super().start_turn(**kwargs)
            holder["supervisor"].abandon("athr_source", holder["token"], lost=False)
            return turn

    class Factory(_TransportFactory):
        def open(self, spec: ChatSandboxSpec, launch: ResidentLaunch) -> _Transport:
            self.opened.append(launch)
            transport = LeaseLosingTransport(launch)
            self.transports.append(transport)
            return transport

    manager = _Manager()
    supervisor, lease = _windowed_supervisor(manager, Factory())
    holder.update(supervisor=supervisor, token=lease.token)
    manager.events.clear()

    with pytest.raises(SessionLeaseLost, match="during turn admission"):
        supervisor.start_turn(
            "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
        )

    assert [event[1] for event in manager.events if event[0] == "member-window"] == [
        "register",
        "close",
    ]
    assert ("observe-finish", MEMBER_UID_BASE) in manager.events


def test_a_group_stop_drops_the_window_of_a_running_turn():
    """Stopping a group drops each running turn's window with its cursor.

    The stop detaches every member's turn and observer cursor. A window it left
    on the member was censused on every later tick, against a container that
    had stopped.
    """
    manager = _Manager()
    supervisor, lease = _windowed_supervisor(manager)
    supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )

    supervisor.stop_if_idle("athr_source", generation=1, reason="orphaned_attempt")
    manager.events.clear()

    assert supervisor.sample_open_windows() == 0
    assert [event for event in manager.events if event[0] == "member-window"] == []


def test_abandon_drops_the_window_when_normal_turn_cleanup_fails(monkeypatch):
    """An abandon that detaches the turn itself drops its window with its cursor.

    ``complete_turn`` drops the window with the turn. When it fails, abandon's
    own teardown detaches the turn and its cursor, and a window it left on the
    member was censused on every later tick for a member running nothing.
    """
    manager = _Manager()
    supervisor, lease = _windowed_supervisor(manager)
    supervisor.start_turn(
        "athr_source", lease.token, prompt="work", model="glm-5.1", reasoning_effort=None
    )

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(supervisor, "complete_turn", fail_cleanup)
    supervisor.abandon("athr_source", lease.token, lost=False)
    manager.events.clear()

    assert supervisor.sample_open_windows() == 0
    assert [event for event in manager.events if event[0] == "member-window"] == []


def test_a_stopping_group_is_kept_behind_renewed_host_rules():
    """It runs on through its archive and Docker stop, though no group counts as running."""
    manager, supervisor = _running_group()
    supervisor._members["athr_source"].stopping = True

    assert supervisor.guard_network() == []

    assert manager.host_rule_checks == 0
    assert manager.host_rule_renewals == 1
    assert manager.stop_count == 0
