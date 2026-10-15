"""Regression tests for the shared-only sandbox manager."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import tracemalloc
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from cloud_agent.sandbox_manager_client import SandboxManagerClient
from cloud_agent_host import sandbox_manager as sandbox_manager_mod
from cloud_agent_host.chat_sandbox import (
    ACTIVATION_TMPFS,
    ChatSandboxManager,
    RetainedChatBackend,
    _RetainedExecProcess,
)
from cloud_agent_host.drivers import InputOutcome
from cloud_agent_host.guest_observer import GuestObservation, GuestProcess
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.runtimes import RuntimeEffortUnsupportedError, RuntimeMCPUnavailableError
from cloud_agent_host.sandbox import (
    ContainerBackend,
    ProcessBackend,
    SandboxError,
    SandboxSpec,
    TerminalProcess,
)
from cloud_agent_host.sandbox_manager import (
    MAX_TERMINAL_BUFFER_BYTES,
    MAX_TERMINAL_BUFFER_EVENTS,
    MAX_TERMINAL_SESSIONS_PER_WORKSPACE,
    SharedTurnRequest,
    TerminalRequest,
    _run_terminal,
    _terminal_sse_frame,
    _TerminalLimits,
    _TerminalManager,
    _TerminalSession,
    create_app,
)
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
)
from cloud_agent_host.shared_session_supervisor import (
    SharedSessionDescriptor,
    SharedSessionLease,
    SharedTurnTelemetry,
)
from cloud_agent_host.tool_windows import ToolMark
from cloud_agent_shared.host_settings import SANDBOX_MANAGER_SETTING_KEYS, default_values


@pytest.mark.asyncio
async def test_resource_checkpoint_is_forwarded_before_any_native_event():
    """A checkpoint persists during a silent tool invocation, before CLI output."""
    gate = threading.Event()
    turn_started = threading.Event()

    class QuietTurn:
        @staticmethod
        def lines():
            turn_started.set()
            gate.wait(2)
            yield SimpleNamespace(event_type="message", payload={"text": "done"}, raw_line="done")

    supervisor = _SharedSupervisor()
    supervisor.start_turn = lambda *_args, **_kw: QuietTurn()  # type: ignore[attr-defined]
    pending = [{"measurement_id": "sample", "state": "collecting", "report_seq": 1}]
    supervisor.take_reported_checkpoint = (  # type: ignore[attr-defined]
        lambda _thread_id: pending.pop() if pending else None
    )
    supervisor.abort_turn = lambda *_args: gate.set()  # type: ignore[attr-defined]
    app = _app(supervisor)
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/shared-sandboxes/{thread_id}/turn"
    )
    response = await endpoint(
        "athr_source", SharedTurnRequest(token="lease", prompt="work", model="model")
    )
    try:
        first = json.loads(await asyncio.wait_for(anext(response.body_iterator), timeout=1))
        assert first["kind"] == "resource_checkpoint"
        assert first["turn_resources"]["state"] == "collecting"
        assert turn_started.wait(1)
        assert not gate.is_set()
    finally:
        gate.set()
        await response.body_iterator.aclose()


class _TerminalProcess(TerminalProcess):
    def __init__(self) -> None:
        self.inputs: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.killed = False

    def chunks(self):
        yield from ()

    def write(self, data: bytes) -> None:
        self.inputs.append(data)

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def poll(self) -> int | None:
        return 0 if self.killed else None

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return 0


class _SuspendableTerminalProcess(_TerminalProcess):
    def __init__(self, *, fail_resume: bool = False) -> None:
        super().__init__()
        self.suspended = False
        self.fail_resume = fail_resume
        self.suspend_calls = 0

    def suspend(self) -> None:
        self.suspend_calls += 1
        self.suspended = True

    def resume(self) -> None:
        self.suspended = False
        if self.fail_resume:
            raise SandboxError("resume failed after CONT")


class _WorkspaceBackend(RetainedChatBackend):
    def __init__(self) -> None:
        self.spec = SimpleNamespace(thread_id="athr_source", shared_live=True)
        self.manager = SimpleNamespace(
            require_network=lambda _spec: None,
            import_base_bundle=self._import_base_bundle,
        )
        self.processes: list[_TerminalProcess] = []
        self.imports: list[tuple[bytes, str, dict[str, str]]] = []
        self.spawned = []

    def spawn_terminal(self, spec, *, rows, cols):
        assert rows > 0 and cols > 0
        self.spawned.append(spec)
        # A shell stays open until it is killed; one that exits at once gives
        # its slot back, which is not what the capacity tests are about.
        process = _RunningTerminalProcess()
        self.processes.append(process)
        return process

    @staticmethod
    def base_env():
        return {}

    def spawn(self, spec):
        self.spawned.append(spec)
        return SimpleNamespace(
            lines=lambda: iter(()),
            kill=lambda: None,
            wait=lambda: 0,
            stderr_text=lambda: "",
        )

    def _import_base_bundle(self, _spec, bundle: bytes, *, commit: str, env):
        self.imports.append((bundle, commit, env))


def _descriptor(thread_id: str = "athr_source") -> SharedSessionDescriptor:
    return SharedSessionDescriptor(
        thread_id=thread_id,
        origin_thread_id="athr_source",
        sandbox_id="asbx_shared",
        generation=3,
        container_id="container-shared",
        container_name="freeinference-chat-shared",
        docker_status="running",
        running=True,
        group_busy=False,
        busy=False,
        terminal_count=0,
        operation_count=0,
        snapshotting=False,
        activation_generation=4,
        process_generation=2,
        residency_generation=7,
        resident_state="live",
        resident_slot_id=0,
        last_settled_job_id="ajob_source",
        native_session_id="native-source",
        idle_deadline_monotonic=1900.0,
        context_status="live",
    )


class _SharedSupervisor:
    idle_timeout_s = 30.0

    def set_idle_timeout_s(self, idle_timeout_s: float) -> None:
        self.idle_timeout_s = idle_timeout_s

    def __init__(self, *, events: list[str] | None = None) -> None:
        self.workspace_backend = _WorkspaceBackend()
        self.calls: list[tuple] = []
        self.start_seqs: list[int | None] = []
        self.restores: list[object] = []
        self.imports = self.workspace_backend.imports
        self.started = False
        self.closed = False
        self.quiesced = False
        self.shutdown_stopped = False
        self.shutdown_include_running: bool | None = None
        self.shutdown_error: Exception | None = None
        self.pending_archive_work = False
        self.events = events if events is not None else []
        self.archive_hook = None
        self.archive_prepare_hook = None
        self.archive_invalidate_hook = None
        self.archive_prepare_start_hook = None
        self.archive_complete_start_hook = None
        self.archive_markerless_prepare_hook = None
        self.threads = {"athr_source", "athr_child"}

    def start(self):
        self.started = True
        self.events.append("supervisor.start")

    def quiesce(self):
        self.quiesced = True
        self.events.append("supervisor.quiesce")

    def stop_all_for_shutdown(self, *, include_running: bool = True):
        self.shutdown_stopped = True
        self.shutdown_include_running = include_running
        self.events.append("supervisor.stop_all")
        if self.shutdown_error is not None:
            raise self.shutdown_error

    def has_pending_archive_work(self) -> bool:
        return self.pending_archive_work

    def close(self):
        self.closed = True
        self.events.append("supervisor.close")

    def configure_stopped_archive_hook(self, hook):
        self.archive_hook = hook

    def configure_stopped_archive_prepare_hook(self, hook):
        self.archive_prepare_hook = hook

    def configure_archive_transition_hooks(
        self,
        *,
        invalidate_final,
        prepare_start,
        complete_start,
        prepare_markerless_final,
    ):
        self.archive_invalidate_hook = invalidate_final
        self.archive_prepare_start_hook = prepare_start
        self.archive_complete_start_hook = complete_start
        self.archive_markerless_prepare_hook = prepare_markerless_final

    def owns(self, thread_id: str) -> bool:
        return thread_id in self.threads

    def status(self, thread_id: str):
        if not self.owns(thread_id):
            raise SandboxError("unknown member")
        return _descriptor(thread_id)

    def list(self):
        return [_descriptor("athr_source"), _descriptor("athr_child")]

    def list_for_thread(self, thread_id: str):
        if not self.owns(thread_id):
            raise SandboxError("unknown member")
        return [_descriptor("athr_source"), _descriptor("athr_child")]

    def register_manifest(self, spec, members, *, expected_container_id=None, start_seq=None):
        self.calls.append(("manifest", spec, members, expected_container_id))
        self.start_seqs.append(start_seq)
        self.threads.update(member.thread_id for member in members)
        return []

    def ensure_resident(self, thread_id, token, launch, profiling=None):
        self.calls.append(("resident", thread_id, profiling))
        return _descriptor(thread_id)

    def acquire(
        self,
        spec,
        *,
        thread_id,
        owner,
        activation_generation,
        has_settled_context,
        manifest,
        expected_container_id=None,
        profiling=None,
        start_seq=None,
        restore=None,
    ):
        self.calls.append(("acquire", thread_id, profiling))
        self.start_seqs.append(start_seq)
        self.restores.append(restore)
        return SharedSessionLease(
            thread_id=thread_id,
            origin_thread_id=spec.thread_id,
            sandbox_id=spec.sandbox_id,
            container_id="container-shared",
            container_name=spec.container_name,
            token="lease",
            activation_generation=activation_generation,
            process_generation=1,
            context_status="live",
            resident_state="live",
            resident_slot_id=0,
            container_action="reuse",
            stages=[],
        )

    def operation_opened(self, thread_id: str):
        self.calls.append(("operation_opened", thread_id))
        return "operation-token", _descriptor(thread_id)

    def operation_closed(self, thread_id: str, token: str):
        self.calls.append(("operation_closed", thread_id, token))
        return _descriptor(thread_id)

    def terminal_opened(self, thread_id: str):
        self.calls.append(("terminal_opened", thread_id))
        return "terminal-token", _descriptor(thread_id)

    def terminal_closed(self, thread_id: str, token: str, **kwargs):
        self.calls.append(("terminal_closed", thread_id, token, kwargs))
        return _descriptor(thread_id)

    def backend(self, thread_id: str):
        assert self.owns(thread_id)
        self.calls.append(("backend", thread_id))
        return self.workspace_backend

    def capture_snapshot(self, thread_id: str, *, capture):
        self.calls.append(("snapshot", thread_id))
        return {"patch": "diff --git a/a.txt b/a.txt\n"}

    def pending_questions(self, thread_id: str, *, owner: str):
        self.calls.append(("questions", thread_id, owner))
        return [
            {
                "request_id": "request-one",
                "questions": [
                    {
                        "id": "approach",
                        "header": "Approach",
                        "question": "Choose an approach",
                        "multiple": False,
                        "options": [{"label": "First", "description": "First option"}],
                        "allow_custom": True,
                    }
                ],
            }
        ]

    def answer_question(self, thread_id: str, **kwargs):
        self.calls.append(("answer_question", thread_id, kwargs))
        return InputOutcome(
            kwargs["owner"] == "attempt:1", None if kwargs["owner"] == "attempt:1" else "turn_ended"
        )


def _launch() -> dict[str, object]:
    """The minimum resident launch the sandbox manager's request model accepts."""
    return {
        "runtime_id": "claude-code",
        "runtime_version": "2.1.220",
        "native_session_id": "native-source",
        "model": "glm-5.1",
        "gateway_base_url": "http://gateway",
        "credential": "grant",
        "slot_id": 0,
        "residency_generation": 1,
    }


def _identity() -> dict[str, object]:
    return {
        "sandbox_id": "asbx_shared",
        "generation": 3,
        "origin_thread_id": "athr_source",
        "runtime_id": "claude-code",
        "runtime_version": "2.1.220",
        "sandbox_image_digest": "sha256:image",
        "expected_container_id": "container-shared",
        "resident_slot_capacity": 8,
        "memory_limit": "4g",
        "cpu_limit": "2",
        "stop_timeout_s": 10,
        "members": [
            {
                "thread_id": "athr_source",
                "native_session_id": "native-source",
                "context_status": "live",
                "activation_generation": 4,
                "process_generation": 2,
                "residency_generation": 7,
                "resident_state": "live",
                "resident_slot_id": 0,
                "last_settled_job_id": "ajob_source",
            }
        ],
    }


def _app(supervisor: _SharedSupervisor):
    return create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
    )


def test_sandbox_manager_requires_a_container_backend_without_an_injected_supervisor():
    with pytest.raises(ValueError, match="require the container or kata backend"):
        create_app(backend=ProcessBackend(acknowledged_unsafe=True), token="x")


class _ArchiveService:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self) -> None:
        self.events.append("archive.start")

    def close(self) -> None:
        self.events.append("archive.close")

    def queue_final(self, *_args, **_kwargs) -> None:
        self.events.append("archive.final")


class _ArchiveController(_ArchiveService):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.enabled = False
        self.revision: int | None = None

    def apply(self, settings):
        self.enabled = bool(settings["enabled"])
        self.revision = int(settings["revision"])
        return self.status()

    def status(self):
        return {
            "configured": self.revision is not None,
            "enabled": self.enabled,
            "running": True,
            "applied_revision": self.revision,
        }

    def prepare_final(self, *_args, **_kwargs) -> None:
        return

    def invalidate_final(self, *_args, **_kwargs) -> None:
        return

    def prepare_start(self, *_args, **_kwargs) -> None:
        return

    def complete_start(self, *_args, **_kwargs) -> None:
        return

    def prepare_markerless_final(self, *_args, **_kwargs) -> bool:
        return self.revision is not None and not self.enabled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("/shared-sandboxes/athr_source/archive-initial/status", {}),
        (
            "/shared-sandboxes/athr_source/archive-initial",
            {"base_sha": "a" * 40},
        ),
    ],
)
async def test_initial_archive_routes_return_only_sanitized_failure_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    extra: dict[str, object],
) -> None:
    supervisor = _SharedSupervisor()
    unsafe = "https://s3.invalid/private-object?credential=secret s3://private-bucket/private-key"
    failure = SandboxError(unsafe)
    safe = (
        "archive failure (phase=initial, type=ClientError, code=AccessDenied, status=403); "
        "archive failure (phase=fail_stop_final, type=ClientError, code=SlowDown, status=503)"
    )
    observed: list[BaseException] = []

    def fail_capture(*_args, **_kwargs):
        raise failure

    def safe_detail(error: BaseException) -> str:
        observed.append(error)
        return safe

    supervisor.capture_initial_archive = fail_capture  # type: ignore[attr-defined]
    monkeypatch.setattr(sandbox_manager_mod, "archive_failure_http_detail", safe_detail)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox_manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
        sandbox_archive_service=_ArchiveService([]),  # type: ignore[arg-type]
    )
    payload = {
        **_identity(),
        "token": "lease-token",
        "preparation_id": "ajob_initial",
        **extra,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.post(
            path,
            headers={"X-Agent-Workspace-Token": "sandbox_manager-secret"},
            json=payload,
        )

    assert response.status_code == 503
    assert response.json() == {"detail": safe}
    assert unsafe not in response.text
    assert observed == [failure]


@pytest.mark.asyncio
async def test_sandbox_manager_lifespan_preflights_without_a_host_worktree():
    supervisor = _SharedSupervisor()
    backend = ProcessBackend(acknowledged_unsafe=True)
    roots: list[object] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        backend, "preflight", lambda *, workdir_root=None: roots.append(workdir_root)
    )
    app = create_app(
        backend=backend,
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
    )
    try:
        async with app.router.lifespan_context(app):
            assert roots == [None]
            assert supervisor.started is True
    finally:
        monkeypatch.undo()
    assert supervisor.closed is True
    assert supervisor.shutdown_stopped is False


@pytest.mark.asyncio
async def test_archive_shutdown_quiesces_then_strictly_stops_before_cleanup() -> None:
    events: list[str] = []
    supervisor = _SharedSupervisor(events=events)
    archive = _ArchiveService(events)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
        sandbox_archive_service=archive,  # type: ignore[arg-type]
    )

    async with app.router.lifespan_context(app):
        assert supervisor.started is True

    assert events == [
        "archive.start",
        "supervisor.start",
        "supervisor.quiesce",
        "supervisor.stop_all",
        "supervisor.close",
        "archive.close",
    ]


@pytest.mark.asyncio
async def test_archive_shutdown_surfaces_sanitized_stop_failure_before_closing_service(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    supervisor = _SharedSupervisor(events=events)
    unsafe = "https://user:secret@example.com/private-final?token=leaked"
    provider_error = RuntimeError(unsafe)
    archive_error = SandboxError("final sandbox archive could not be stored")
    archive_error.__cause__ = provider_error
    supervisor.shutdown_error = archive_error
    archive = _ArchiveService(events)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
        sandbox_archive_service=archive,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="shutdown cleanup failed") as raised:
        async with app.router.lifespan_context(app):
            pass

    assert events[-4:] == [
        "supervisor.quiesce",
        "supervisor.stop_all",
        "supervisor.close",
        "archive.close",
    ]
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "sandbox_manager_cleanup_failed"
    )
    assert record.component == "shared_sandbox_stop_all"  # type: ignore[attr-defined]
    assert record.error_type == "SandboxError"  # type: ignore[attr-defined]
    assert record.exc_info is None
    assert unsafe not in caplog.text
    current: BaseException | None = raised.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        assert unsafe not in str(current)
        current = current.__cause__ or current.__context__
    assert raised.value.__cause__ is not None
    assert raised.value.__cause__.__cause__ is None


@pytest.mark.asyncio
async def test_disabled_dynamic_policy_still_strictly_stops_on_clean_sandbox_manager_shutdown() -> (
    None
):
    events: list[str] = []
    supervisor = _SharedSupervisor(events=events)
    controller = _ArchiveController(events)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
        sandbox_archive_controller=controller,  # type: ignore[arg-type]
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://sandbox_manager",
        ) as client:
            response = await client.post(
                "/sandbox-archive-settings",
                headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
                json={"settings": {"enabled": False, "revision": 7}},
            )
        assert response.status_code == 200
        assert supervisor.started is True
        assert supervisor.archive_markerless_prepare_hook is not None
        assert supervisor.archive_markerless_prepare_hook(None, None) is True

    assert supervisor.shutdown_stopped is True
    assert supervisor.shutdown_include_running is True
    assert events[-4:] == [
        "supervisor.quiesce",
        "supervisor.stop_all",
        "supervisor.close",
        "archive.close",
    ]


@pytest.mark.asyncio
async def test_health_and_sandbox_inventory_are_authenticated_and_shared_only():
    app = _app(_SharedSupervisor())
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        denied = await client.get("/sandboxes")
        health = await client.get("/health", headers=headers)
        listed = await client.get("/sandboxes", headers=headers)

    assert denied.status_code == 401
    assert health.json()["ok"] is True
    rows = listed.json()["sandboxes"]
    assert len(rows) == 1
    assert rows[0]["shared_live"] is True
    assert {member["thread_id"] for member in rows[0]["members"]} == {
        "athr_source",
        "athr_child",
    }


@pytest.mark.asyncio
async def test_a_non_ascii_sandbox_manager_credential_is_refused_rather_than_failing():
    """Comparing a caller's header must not raise, whatever characters it holds."""
    app = _app(_SharedSupervisor())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get("/sandboxes", headers={"X-Agent-Workspace-Token": b"caf\xe9"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_targeted_inventory_is_authenticated_and_isolated_from_full_inventory_failure():
    supervisor = _SharedSupervisor()
    calls = []

    def failed_inventory():
        calls.append(("list",))
        raise SandboxError("unrelated sandbox reconciliation failed")

    def targeted_inventory(thread_id):
        calls.append(("list_for_thread", thread_id))
        return [_descriptor("athr_source"), _descriptor("athr_child")]

    supervisor.list = failed_inventory
    supervisor.list_for_thread = targeted_inventory
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        denied = await client.get("/sandboxes", params={"thread_id": "athr_child"})
        assert denied.status_code == 401
        assert calls == []
        failed = await client.get("/sandboxes", headers=headers)
        response = await client.get(
            "/sandboxes", headers=headers, params={"thread_id": "athr_child"}
        )

    assert failed.status_code == 503
    assert response.status_code == 200
    assert calls == [("list",), ("list_for_thread", "athr_child")]
    row = response.json()["sandboxes"][0]
    assert row["thread_id"] == "athr_source"
    assert {member["thread_id"] for member in row["members"]} == {"athr_source", "athr_child"}


@pytest.mark.asyncio
async def test_targeted_inventory_rejects_unknown_members_without_falling_back_to_full_inventory():
    supervisor = _SharedSupervisor()
    supervisor.list = lambda: pytest.fail("targeted inventory must not list the host")
    app = _app(supervisor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get(
            "/sandboxes",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            params={"thread_id": "athr_unknown"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "unknown member"}


@pytest.mark.asyncio
async def test_targeted_shared_status_serializes_force_delete_proof_fields():
    supervisor = _SharedSupervisor()
    supervisor.status = lambda thread_id: replace(
        _descriptor(thread_id),
        docker_status="stopped",
        running=False,
        group_busy=False,
        busy=False,
        terminal_count=0,
        operation_count=0,
        snapshotting=False,
    )
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        denied = await client.get("/shared-sandboxes/athr_source")
        response = await client.get("/shared-sandboxes/athr_source", headers=headers)

    assert denied.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert {
        key: payload[key]
        for key in (
            "thread_id",
            "origin_thread_id",
            "sandbox_id",
            "generation",
            "container_id",
            "docker_status",
            "running",
            "group_busy",
            "busy",
            "terminal_count",
            "operation_count",
            "snapshotting",
        )
    } == {
        "thread_id": "athr_source",
        "origin_thread_id": "athr_source",
        "sandbox_id": "asbx_shared",
        "generation": 3,
        "container_id": "container-shared",
        "docker_status": "stopped",
        "running": False,
        "group_busy": False,
        "busy": False,
        "terminal_count": 0,
        "operation_count": 0,
        "snapshotting": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "athr_source", "athr_child"])
@pytest.mark.parametrize("origin_count,child_count", [(0, 2), (1, 2), (2, 0), (0, 0)])
async def test_sandbox_inventory_reports_terminals_across_all_shared_members(
    origin_count, child_count, thread_id
):
    supervisor = _SharedSupervisor()
    descriptors = [
        replace(_descriptor("athr_source"), terminal_count=origin_count),
        replace(_descriptor("athr_child"), terminal_count=child_count),
    ]
    supervisor.list = lambda: descriptors
    supervisor.list_for_thread = lambda _thread_id: descriptors
    app = _app(supervisor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get(
            "/sandboxes",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            params={} if thread_id is None else {"thread_id": thread_id},
        )

    assert response.status_code == 200
    rows = response.json()["sandboxes"]
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "athr_source"
    assert rows[0]["terminal_count"] == origin_count + child_count


@pytest.mark.asyncio
async def test_native_question_routes_require_authentication_and_forward_exact_answers():
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    path = "/shared-sandboxes/athr_source/questions"
    payload = {
        "owner": "attempt:1",
        "request_id": "request-one",
        "answers": {"approach": ["First", "A custom answer"]},
        "reject": False,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        denied_read = await client.get(path, params={"owner": "attempt:1"})
        denied_reply = await client.post(path, json=payload)
        assert supervisor.calls == []
        pending = await client.get(path, headers=headers, params={"owner": "attempt:1"})
        answer = await client.post(path, headers=headers, json=payload)
        reject = await client.post(
            path, headers=headers, json={**payload, "answers": {}, "reject": True}
        )
        stale = await client.post(path, headers=headers, json={**payload, "owner": "attempt:2"})

    assert denied_read.status_code == denied_reply.status_code == 401
    assert pending.status_code == 200
    assert pending.json()["questions"][0]["request_id"] == "request-one"
    assert pending.json()["questions"][0]["questions"][0]["allow_custom"] is True
    assert answer.json() == reject.json() == {"accepted": True, "reason": None}
    assert stale.json() == {"accepted": False, "reason": "turn_ended"}
    assert supervisor.calls == [
        ("questions", "athr_source", "attempt:1"),
        ("answer_question", "athr_source", payload),
        ("answer_question", "athr_source", {**payload, "answers": {}, "reject": True}),
        ("answer_question", "athr_source", {**payload, "owner": "attempt:2"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"owner": ""},
        {"owner": "a" * 129},
        {"request_id": ""},
        {"request_id": "r" * 129},
        {"answers": {"": ["answer"]}},
        {"answers": {"q" * 129: ["answer"]}},
        {"answers": {str(i): ["answer"] for i in range(11)}},
        {"answers": {"question": ["answer"] * 21}},
        {"answers": {"question": ["a" * 10_001]}},
        {"answers": {"question": "answer"}},
    ],
)
async def test_native_question_answers_are_bounded_before_transport(invalid):
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/questions",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            json={"owner": "attempt:1", "request_id": "request-one", "answers": {}, **invalid},
        )
    assert response.status_code == 422
    assert supervisor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"owner": ""}, {"owner": "o" * 129}])
async def test_native_question_reads_require_a_bounded_owner(params):
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get(
            "/shared-sandboxes/athr_source/questions",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            params=params,
        )
    assert response.status_code == 422
    assert supervisor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_inventory_syncs_one_group_and_reads_only_cached_resources(running):
    supervisor = _SharedSupervisor()
    supervisor.list = lambda: [
        replace(_descriptor("athr_source"), running=running),
        replace(_descriptor("athr_child"), running=running),
    ]
    app = _app(supervisor)
    calls = []
    sample = {"sample_id": "resource-sample", "sample_age_ms": 4000, "memory_bytes": 1024}
    app.state.sandbox_resource_collector = SimpleNamespace(
        sync_targets=lambda rows: calls.append(("sync", [dict(row) for row in rows])),
        latest=lambda container: calls.append(("latest", container)) or sample,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get(
            "/sandboxes", headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"}
        )
    assert response.status_code == 200
    assert len(calls[0][1]) == 1
    assert calls[0][1][0]["container_id"] == "container-shared"
    row = response.json()["sandboxes"][0]
    if running:
        assert calls[1:] == [("latest", "container-shared")]
        assert row["resource_usage"] == sample
    else:
        assert len(calls) == 1
        assert row["resource_usage"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_targeted_inventory_reads_cached_resources_without_syncing_collection_targets(
    running,
):
    supervisor = _SharedSupervisor()
    supervisor.list_for_thread = lambda _thread_id: [
        replace(_descriptor("athr_source"), running=running),
        replace(_descriptor("athr_child"), running=running),
    ]
    app = _app(supervisor)
    calls = []
    sample = {"sample_id": "resource-sample", "sample_age_ms": 4000, "memory_bytes": 1024}
    app.state.sandbox_resource_collector = SimpleNamespace(
        sync_targets=lambda _rows: pytest.fail("partial inventory must not evict other groups"),
        latest=lambda container: calls.append(("latest", container)) or sample,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get(
            "/sandboxes",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            params={"thread_id": "athr_child"},
        )

    assert response.status_code == 200
    row = response.json()["sandboxes"][0]
    assert calls == ([("latest", "container-shared")] if running else [])
    assert row["resource_usage"] == (sample if running else None)


@pytest.mark.asyncio
async def test_resource_collector_is_owned_and_cleaned_up_by_sandbox_manager_lifespan(monkeypatch):
    calls = []
    collector = SimpleNamespace(
        start=lambda: calls.append("start"),
        close=lambda: calls.append("close"),
    )

    def factory():
        calls.append("construct")
        return collector

    monkeypatch.setattr(sandbox_manager_mod, "SandboxResourceCollector", factory)
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    assert calls == []
    assert app.state.sandbox_resource_collector is None
    with pytest.raises(RuntimeError, match="request failure"):
        async with app.router.lifespan_context(app):
            assert calls == ["construct", "start"]
            assert app.state.sandbox_resource_collector is collector
            raise RuntimeError("request failure")
    assert calls == ["construct", "start", "close"]
    assert app.state.sandbox_resource_collector is None
    assert supervisor.closed


@pytest.mark.asyncio
async def test_legacy_lifecycle_and_host_workspace_routes_do_not_exist():
    app = _app(_SharedSupervisor())
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        old_acquire = await client.post("/sandboxes/athr_source/acquire", headers=headers, json={})
        host_workspace = await client.get("/workspaces/ajob_old/files", headers=headers)

    assert old_acquire.status_code == 404
    assert host_workspace.status_code == 404
    assert host_workspace.json()["detail"] == "shared chat workspace is not materialized"
    for method in (
        "acquire_chat_sandbox",
        "note_chat_process_started",
        "settle_chat_sandbox",
        "abandon_chat_sandbox",
        "chat_sandbox_status",
    ):
        assert not hasattr(SandboxManagerClient, method)


@pytest.mark.asyncio
async def test_an_invalid_profiling_context_never_rejects_a_sandbox_request():
    """Measurement settings must not be able to fail a startup.

    Regression: the descriptor accepted a 129 character run id while this
    request model capped it at 128, so the same acquire answered 200 without a
    selection and 422 with one -- before the supervisor was reached.
    """
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    identity = _identity()
    live = {
        "run_id": "run-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "expires_at": time.time() + 60,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        plain = await client.post(
            "/shared-sandboxes/athr_source/acquire",
            headers=headers,
            json={**identity, "owner": "attempt:1", "activation_generation": 4},
        )
        selected = await client.post(
            "/shared-sandboxes/athr_source/acquire",
            headers=headers,
            json={
                **identity,
                "owner": "attempt:1",
                "activation_generation": 4,
                "profiling": live,
            },
        )
        plain_resident = await client.post(
            "/shared-sandboxes/athr_source/resident",
            headers=headers,
            json={"token": "lease", "launch": _launch()},
        )
        selected_resident = await client.post(
            "/shared-sandboxes/athr_source/resident",
            headers=headers,
            json={"token": "lease", "launch": _launch(), "profiling": live},
        )
        # Deliberately unusable selections on BOTH endpoints: a measurement
        # setting must never be able to reject the sandbox request carrying it.
        for broken in (
            {**live, "run_id": "r" * 200},
            {**live, "run_id": None, "expires_at": "soon"},
            {**live, "measurement_id": 17, "operation": ["not", "a", "string"]},
            "not even an object",
            {},
        ):
            rejected = await client.post(
                "/shared-sandboxes/athr_source/acquire",
                headers=headers,
                json={
                    **identity,
                    "owner": "attempt:1",
                    "activation_generation": 4,
                    "profiling": broken,
                },
            )
            assert rejected.status_code == 200, ("acquire", broken)
            rejected_resident = await client.post(
                "/shared-sandboxes/athr_source/resident",
                headers=headers,
                json={"token": "lease", "launch": _launch(), "profiling": broken},
            )
            assert rejected_resident.status_code == 200, ("resident", broken)

    assert plain.status_code == 200
    assert selected.status_code == 200
    assert plain_resident.status_code == 200
    assert selected_resident.status_code == 200
    acquired = [call for call in supervisor.calls if call[0] == "acquire"]
    residents = [call for call in supervisor.calls if call[0] == "resident"]
    assert acquired[0][2] is None
    assert acquired[1][2] == live
    assert residents[0][2] is None
    assert residents[1][2] == live


def _runner_client(app) -> SessionSupervisorClient:
    """The runner's own client, wired to this sandbox manager app in-process.

    The client is synchronous and the app is ASGI, so each request runs the app
    on an event loop of its own. Every answer the client reads is one the sandbox manager
    produced, never a body written by the test.
    """

    def forward(request: httpx.Request) -> httpx.Response:
        async def answer() -> httpx.Response:
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://sandbox_manager"
            ) as sandbox_manager:
                answered = await sandbox_manager.request(
                    request.method,
                    request.url.path,
                    headers=dict(request.headers),
                    content=request.content,
                )
            return httpx.Response(
                answered.status_code,
                headers={"content-type": answered.headers.get("content-type", "")},
                content=answered.content,
            )

        return asyncio.run(answer())

    client = SessionSupervisorClient("http://sandbox_manager", "sandbox-manager-secret")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(forward),
        headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
    )
    return client


def _resident_launch(**changes: object) -> ResidentLaunch:
    """What the runner sends for a first turn whose claim bound one MCP server."""
    fields: dict[str, object] = {
        **_launch(),
        "thread_id": "athr_source",
        "native_session_id": None,
        "reasoning_effort": None,
        "mcp_server_ids": ("deepwiki",),
        # What a launch carries from a host with no MCP relay of its own.
        "mcp_base_url": "",
        "mcp_credential": "mcp-token",
    }
    fields.update(changes)
    return ResidentLaunch(**fields)  # type: ignore[arg-type]


def test_runner_github_token_survives_sandbox_manager_roundtrip_without_entering_the_response():
    launches = []

    class RecordingSupervisor(_SharedSupervisor):
        def ensure_resident(self, thread_id, token, launch, profiling=None):
            launches.append(launch)
            return _descriptor(thread_id)

    client = _runner_client(_app(RecordingSupervisor()))
    try:
        answer = client.ensure_shared_resident(
            "athr_source",
            token="lease",
            launch=_resident_launch(github_token="sandbox-write-token", github_repo="owner/repo"),
        )
    finally:
        client.close()

    assert launches[0].github_token == "sandbox-write-token"
    assert launches[0].github_repo == "owner/repo"
    assert "sandbox-write-token" not in repr(launches[0])
    assert "sandbox-write-token" not in str(answer)


@pytest.mark.parametrize(
    "refusal",
    [
        RuntimeMCPUnavailableError(
            "MCP was requested but no relay address was configured for this run"
        ),
        RuntimeEffortUnsupportedError(
            "runtime 'pi' cannot express reasoning effort 'xhigh' "
            "(declares ['minimal', 'low', 'medium', 'high'])"
        ),
    ],
)
def test_a_runtime_refusal_reaches_the_runner_in_the_runtimes_own_words(refusal):
    """Regression: a refused resident launch read as an opaque platform failure.

    The runtime refuses a launch it cannot run as configured, with a reason
    written for the owner. Neither refusal is a driver or sandbox error, so the
    sandbox manager answered Starlette's plain-text 500, and the turn's error event said
    only "shared Session Supervisor rejected the lifecycle transition".
    """

    class _RefusingSupervisor(_SharedSupervisor):
        def ensure_resident(self, thread_id, token, launch, profiling=None):
            raise refusal

    client = _runner_client(_app(_RefusingSupervisor()))
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            client.ensure_shared_resident("athr_source", token="lease", launch=_resident_launch())
    finally:
        client.close()

    assert raised.value.status_code == 422
    assert str(raised.value) == str(refusal)


def test_an_unexpected_sandbox_manager_failure_names_its_type_and_route_but_not_its_message():
    """A failure no route anticipated still answers a body the runner can read.

    The message stays in the sandbox manager's log: the same answer reaches the owner's
    browser through the control plane, which shows only what is safe to show.
    """

    class _CrashingSupervisor(_SharedSupervisor):
        def ensure_resident(self, thread_id, token, launch, profiling=None):
            raise OSError(24, "Too many open files", "/var/lib/agent/private-state")

    client = _runner_client(_app(_CrashingSupervisor()))
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            client.ensure_shared_resident("athr_source", token="lease", launch=_resident_launch())
    finally:
        client.close()

    assert raised.value.status_code == 500
    assert str(raised.value) == (
        "sandbox manager failed unexpectedly "
        "(OSError in POST /shared-sandboxes/{thread_id}/resident)"
    )


def test_a_request_the_sandbox_manager_rejects_names_the_field_but_never_its_value():
    """FastAPI's validation ``detail`` is a list, which the runner used to drop.

    The field path is what makes the rejection actionable. The value is left
    out because the field likeliest to exceed its bound is a credential.
    """
    credential = "grant-" + "x" * 20_000
    client = _runner_client(_app(_SharedSupervisor()))
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            client.ensure_shared_resident(
                "athr_source", token="lease", launch=_resident_launch(credential=credential)
            )
    finally:
        client.close()

    message = str(raised.value)
    assert raised.value.status_code == 422
    assert message == (
        "shared Session Supervisor rejected the lifecycle transition (resident): "
        "HTTP 422 Unprocessable Entity "
        "(launch.credential: String should have at most 16384 characters)"
    )
    assert "grant-" not in message


@pytest.mark.asyncio
async def test_the_sandbox_manager_launches_tools_through_its_hosts_own_mcp_relay(monkeypatch):
    """Like the gateway's, the MCP relay is this host's, whatever a caller names.

    It is the relay the host rules admit for the group, so any other address
    would only be refused at the bridge. A launch without tools is given none.
    """
    launches: list[ResidentLaunch] = []

    class _RecordingSupervisor(_SharedSupervisor):
        def ensure_resident(self, thread_id, token, launch, profiling=None):
            launches.append(launch)
            return _descriptor(thread_id)

    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://agent-mcp:8000")
    app = _app(_RecordingSupervisor())
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    with_tools = {
        **_launch(),
        "mcp_server_ids": ["deepwiki"],
        "mcp_base_url": "http://elsewhere:9000",
        "mcp_credential": "mcp-token",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        for launch in (with_tools, _launch()):
            answered = await client.post(
                "/shared-sandboxes/athr_source/resident",
                headers=headers,
                json={"token": "lease", "launch": launch},
            )
            assert answered.status_code == 200, answered.text

    tools, plain = launches
    assert tools.mcp_base_url == "http://agent-mcp:8000"
    assert tools.mcp_credential == "mcp-token"
    assert plain.mcp_base_url == ""


def test_a_crash_an_older_sandbox_manager_answers_as_plain_text_still_names_the_call():
    """A sandbox manager without the JSON answer, mid-rollout, still yields a findable error."""
    client = SessionSupervisorClient("http://sandbox_manager", "sandbox-manager-secret")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, text="Internal Server Error")
        )
    )
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            client.settle_shared(
                "athr_source", token="lease", job_id="ajob_source", context_status="new"
            )
    finally:
        client.close()

    assert raised.value.status_code == 500
    assert str(raised.value) == (
        "shared Session Supervisor rejected the lifecycle transition (settle): "
        "HTTP 500 Internal Server Error"
    )


@pytest.mark.asyncio
async def test_shared_registration_snapshot_and_base_import_keep_identity():
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    identity = _identity()
    bundle = b"trusted-git-bundle"
    commit_sha = "b" * 40

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        registered = await client.post(
            "/shared-sandboxes/athr_source/register", headers=headers, json=identity
        )
        captured = await client.post(
            "/shared-sandboxes/athr_source/snapshot",
            headers=headers,
            json={**identity, "snapshot_id": 9, "workspace_epoch": 12, "base_sha": "a" * 40},
        )
        imported = await client.post(
            "/shared-sandboxes/athr_source/import-base",
            headers=headers,
            json={
                **identity,
                "snapshot_id": 9,
                "commit_sha": commit_sha,
                "bundle_b64": base64.b64encode(bundle).decode(),
            },
        )

    patch = "diff --git a/a.txt b/a.txt\n"
    assert registered.status_code == 200
    assert registered.json()["sandbox_id"] == "asbx_shared"
    assert captured.json()["content_sha256"] == hashlib.sha256(patch.encode()).hexdigest()
    assert imported.json()["workspace_head_preserved"] is True
    assert supervisor.imports == [
        (
            bundle,
            commit_sha,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "/workspace",
            },
        )
    ]


@pytest.mark.asyncio
async def test_a_refused_snapshot_patch_keeps_its_final_status(monkeypatch):
    """A patch that is not exact UTF-8 is refused for good, not retried.

    The snapshot coordinator marks a capture that answered 502 dirty and
    retries one that answered 503, so redacting the refusal's detail must not
    turn it into a host failure.
    """
    supervisor = _SharedSupervisor()
    supervisor.capture_snapshot = (  # type: ignore[method-assign]
        lambda _thread_id, *, capture: capture(object())
    )

    def refuse(*_args, **_kwargs):
        raise HTTPException(status_code=502, detail="workspace patch is not exact UTF-8 text")

    monkeypatch.setattr(sandbox_manager_mod, "_run_git", refuse)
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        captured = await client.post(
            "/shared-sandboxes/athr_source/snapshot",
            headers=headers,
            json={**_identity(), "snapshot_id": 9, "workspace_epoch": 12, "base_sha": "a" * 40},
        )

    assert captured.status_code == 502
    assert captured.json()["detail"] == "workspace patch is not exact UTF-8 text"


@pytest.mark.asyncio
async def test_base_import_resolves_its_workspace_off_the_event_loop(monkeypatch):
    """The supervisor's metadata lock is never waited on by the event loop."""
    supervisor = _SharedSupervisor()
    loop_thread = threading.current_thread()
    callers: list[threading.Thread] = []
    resolve = supervisor.backend

    def backend(thread_id: str):
        callers.append(threading.current_thread())
        return resolve(thread_id)

    monkeypatch.setattr(supervisor, "backend", backend)
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        imported = await client.post(
            "/shared-sandboxes/athr_source/import-base",
            headers=headers,
            json={
                **_identity(),
                "snapshot_id": 9,
                "commit_sha": "b" * 40,
                "bundle_b64": base64.b64encode(b"trusted-git-bundle").decode(),
            },
        )

    assert imported.status_code == 200
    assert callers and all(caller is not loop_thread for caller in callers)


@pytest.mark.asyncio
async def test_file_routes_use_the_shared_container_not_a_host_directory(monkeypatch):
    supervisor = _SharedSupervisor()
    reads: list[tuple[object, str]] = []
    writes: list[tuple[object, str, str]] = []

    def read(backend, relative, **_kwargs):
        reads.append((backend, relative))
        return {
            "path": relative,
            "kind": "file",
            "content": "before\n",
            "size": 7,
            "binary": False,
            "truncated": False,
            "writable": True,
            "source": "workspace",
            "omitted_reason": None,
        }

    def write(backend, relative, content, **_kwargs):
        writes.append((backend, relative, content))
        return {**read(backend, relative), "content": content, "size": len(content)}

    monkeypatch.setattr("cloud_agent_host.sandbox_manager.read_retained_file", read)
    monkeypatch.setattr("cloud_agent_host.sandbox_manager.write_retained_file", write)
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        fetched = await client.get("/workspaces/athr_source/files?path=README.md", headers=headers)
        updated = await client.put(
            "/workspaces/athr_source/files?path=README.md",
            headers=headers,
            json={"content": "after\n"},
        )

    assert fetched.status_code == updated.status_code == 200
    assert all(item[0] is supervisor.workspace_backend for item in reads)
    assert writes == [(supervisor.workspace_backend, "README.md", "after\n")]
    assert ("operation_opened", "athr_source") in supervisor.calls


@pytest.mark.asyncio
async def test_interactive_terminal_runs_in_the_shared_container():
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        created = await client.post(
            "/workspaces/athr_source/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        terminal_id = created.json()["id"]
        deleted = await client.delete(
            f"/workspaces/athr_source/terminals/{terminal_id}", headers=headers
        )

    assert created.status_code == deleted.status_code == 200
    assert supervisor.workspace_backend.spawned[0].workdir == "/workspace"
    assert ("terminal_opened", "athr_source") in supervisor.calls
    assert any(call[0] == "terminal_closed" for call in supervisor.calls)


@pytest.mark.asyncio
async def test_fork_siblings_share_terminal_visibility_capacity_and_suspension():
    """Member URLs authenticate separately but address one workspace PTY scope."""
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        created = await client.post(
            "/workspaces/athr_source/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        terminal_id = created.json()["id"]
        child_list = await client.get("/workspaces/athr_child/terminals", headers=headers)
        suspended = await client.post(
            "/workspaces/athr_child/terminals/suspend",
            headers=headers,
            json={"lease_generation": 3, "job_id": "ajob_child"},
        )
        blocked = await client.post(
            "/workspaces/athr_source/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        resumed = await client.post(
            "/workspaces/athr_source/terminals/resume",
            headers=headers,
            json={"lease_generation": 3, "job_id": "ajob_child"},
        )
        accepted = await client.post(
            "/workspaces/athr_child/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        accepted_terminal_id = accepted.json()["id"]
        third = await client.post(
            "/workspaces/athr_source/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        fourth = await client.post(
            "/workspaces/athr_child/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        over_capacity = await client.post(
            "/workspaces/athr_source/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        terminal_ids = [
            terminal_id,
            accepted_terminal_id,
            third.json()["id"],
            fourth.json()["id"],
        ]
        deleted = [
            await client.delete(f"/workspaces/athr_child/terminals/{current}", headers=headers)
            for current in terminal_ids
        ]

    assert created.status_code == child_list.status_code == 200
    assert child_list.json()["terminals"][0]["id"] == terminal_id
    assert suspended.status_code == resumed.status_code == 200
    assert blocked.status_code == 409
    assert accepted.status_code == third.status_code == fourth.status_code == 200
    assert over_capacity.status_code == 409
    assert all(response.status_code == 200 for response in deleted)


@pytest.mark.asyncio
async def test_refused_terminal_creation_releases_its_activity_token():
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        for _ in range(MAX_TERMINAL_SESSIONS_PER_WORKSPACE):
            created = await client.post(
                "/workspaces/athr_source/terminals",
                headers=headers,
                json={"rows": 30, "cols": 100},
            )
            assert created.status_code == 200
        refused = await client.post(
            "/workspaces/athr_source/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )

    assert refused.status_code == 409
    assert len([call for call in supervisor.calls if call[0] == "terminal_opened"]) == (
        MAX_TERMINAL_SESSIONS_PER_WORKSPACE + 1
    )
    failed_cleanup = [
        call for call in supervisor.calls if call[0] == "terminal_closed" and call[3] == {}
    ]
    assert failed_cleanup == [("terminal_closed", "athr_source", "terminal-token", {})]


def test_shared_one_shot_terminal_uses_group_writable_umask():
    backend = _WorkspaceBackend()
    result = _run_terminal(
        backend,  # type: ignore[arg-type]
        "/workspace",
        TerminalRequest(command="touch shared.txt", cwd="/workspace", timeout_seconds=1),
    )
    assert result["exit_code"] == 0
    assert backend.spawned[0].argv[-1].startswith("umask 0002; ")


@pytest.mark.asyncio
async def test_the_runner_sets_the_sandbox_managers_runtime_settings_over_its_own():
    """Apply runtime overrides and reset the complete configuration to defaults."""
    supervisor = _SharedSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
        terminal_idle_ttl_s=1800,
        terminal_hard_ttl_s=28800,
        terminal_max_sessions=64,
        session_idle_timeout_s=1800,
    )
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    own = {
        **default_values(SANDBOX_MANAGER_SETTING_KEYS),
        "terminal_idle_ttl_s": 1800,
        "terminal_hard_ttl_s": 28800,
        "terminal_max_sessions": 64,
        "chat_idle_timeout_s": 1800,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        denied = await client.post("/host-settings", json={"settings": {}})
        applied = await client.post(
            "/host-settings",
            headers=headers,
            json={
                "settings": {
                    "terminal_max_sessions": 16,
                    "chat_idle_timeout_s": 600,
                    # Not the sandbox manager's, and outside its bounds: both ignored.
                    "agent_timeout_s": 900,
                    "terminal_idle_ttl_s": 1,
                }
            },
        )
        terminals = app.state.terminal_manager
        assert (terminals._max_sessions, terminals._idle_ttl_s) == (16, 1800)
        assert supervisor.idle_timeout_s == 600
        cleared = await client.post("/host-settings", headers=headers, json={"settings": {}})

    assert denied.status_code == 401
    assert applied.json() == {
        "applied": {**own, "terminal_max_sessions": 16, "chat_idle_timeout_s": 600},
        "local": own,
    }
    assert cleared.json() == {"applied": own, "local": own}
    assert terminals._max_sessions == 64
    assert supervisor.idle_timeout_s == 1800


@pytest.mark.asyncio
async def test_reset_uses_defaults_after_the_sandbox_manager_bootstrapped_from_environment(
    monkeypatch,
):
    monkeypatch.setenv("AGENT_TERMINAL_MAX_SESSIONS", "99")
    monkeypatch.setenv("AGENT_CHAT_IDLE_TIMEOUT_S", "600")
    monkeypatch.setenv("AGENT_TERMINAL_INPUT_MAX_KIB", "2")
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    assert app.state.terminal_manager._max_sessions == 99

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        reset = await client.post("/host-settings", headers=headers, json={"settings": {}})

    assert reset.status_code == 200
    assert reset.json()["applied"] == default_values(SANDBOX_MANAGER_SETTING_KEYS)
    assert reset.json()["local"]["terminal_max_sessions"] == 99
    assert reset.json()["local"]["chat_idle_timeout_s"] == 600
    assert reset.json()["local"]["terminal_input_max_kib"] == 2
    assert app.state.terminal_manager._max_sessions == 64
    assert supervisor.idle_timeout_s == 1800


@pytest.mark.asyncio
async def test_admin_workspace_terminal_limits_are_advertised_and_enforced(monkeypatch):
    supervisor = _SharedSupervisor()
    process = _RunningTerminalProcess()
    monkeypatch.setattr(supervisor.workspace_backend, "spawn_terminal", lambda *_a, **_k: process)
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        configured = await client.post(
            "/host-settings",
            headers=headers,
            json={
                "settings": {"terminal_max_sessions_per_workspace": 1, "terminal_input_max_kib": 1}
            },
        )
        first = await client.post(
            "/workspaces/athr_source/terminals", headers=headers, json={"rows": 24, "cols": 80}
        )
        second = await client.post(
            "/workspaces/athr_child/terminals", headers=headers, json={"rows": 24, "cols": 80}
        )
        listed = await client.get("/workspaces/athr_child/terminals", headers=headers)
        terminal_id = first.json()["id"]
        rejected = await client.post(
            f"/workspaces/athr_source/terminals/{terminal_id}/input",
            headers=headers,
            json={"data": base64.b64encode(b"x" * 1025).decode()},
        )
        accepted = await client.post(
            f"/workspaces/athr_source/terminals/{terminal_id}/input",
            headers=headers,
            json={"data": base64.b64encode(b"x" * 1024).decode()},
        )
        await client.delete(f"/workspaces/athr_source/terminals/{terminal_id}", headers=headers)

    assert configured.status_code == first.status_code == accepted.status_code == 200
    assert second.status_code == 409
    assert rejected.status_code == 413
    assert listed.json()["max_sessions"] == 1
    assert listed.json()["max_input_bytes"] == 1024
    assert [row["id"] for row in listed.json()["terminals"]] == [terminal_id]


@pytest.mark.asyncio
async def test_admin_file_limits_reach_the_retained_workspace_reader_and_writer(monkeypatch):
    seen: list[dict] = []

    def read(_backend, relative, **kwargs):
        kwargs.pop("observer", None)
        seen.append(kwargs)
        return {"path": relative, "kind": "directory", "entries": []}

    def write(_backend, relative, _content, **kwargs):
        kwargs.pop("observer", None)
        seen.append(kwargs)
        return {"path": relative, "kind": "file", "content": "saved"}

    monkeypatch.setattr("cloud_agent_host.sandbox_manager.read_retained_file", read)
    monkeypatch.setattr("cloud_agent_host.sandbox_manager.write_retained_file", write)
    app = _app(_SharedSupervisor())
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        configured = await client.post(
            "/host-settings",
            headers=headers,
            json={
                "settings": {
                    "workspace_file_write_max_mib": 1,
                    "workspace_file_preview_max_kib": 64,
                    "workspace_directory_max_entries": 20,
                }
            },
        )
        listed = await client.get("/workspaces/athr_source/files", headers=headers)
        saved = await client.put(
            "/workspaces/athr_source/files?path=a.txt", headers=headers, json={"content": "saved"}
        )

    assert configured.status_code == listed.status_code == saved.status_code == 200
    assert seen == [
        {"max_preview_bytes": 64 * 1024, "max_directory_entries": 20},
        {
            "max_write_bytes": 1024 * 1024,
            "max_preview_bytes": 64 * 1024,
            "max_directory_entries": 20,
        },
    ]


def test_lower_terminal_output_limits_apply_to_existing_terminals():
    manager = _TerminalManager(idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8)
    session = _TerminalSession(
        terminal_id="term_live", process=_TerminalProcess(), workdir=None, rows=24, cols=80, now=0
    )
    manager._sessions["asbx_shared"] = {session.id: session}
    session._append_output(b"a" * 100)
    session._append_output(b"b" * 100)
    session.open_stream()

    manager.configure(
        idle_ttl_s=1800,
        hard_ttl_s=28800,
        max_sessions=8,
        limits=_TerminalLimits(buffer_bytes=100, buffer_events=2, output_bytes=150, streams=1),
    )

    assert [(event.kind, event.data) for event in session.events_after(0, wait_s=0)] == [
        ("reset", b""),
        ("output", b"b" * 100),
    ]
    with pytest.raises(HTTPException) as refused:
        session.open_stream()
    assert refused.value.status_code == 429


@pytest.mark.asyncio
async def test_lower_command_output_limit_stops_an_oversized_command(monkeypatch):
    supervisor = _SharedSupervisor()
    killed: list[bool] = []

    def spawn(_spec):
        return SimpleNamespace(
            lines=lambda: iter(["x" * (1024 * 1024 + 1)]),
            kill=lambda: killed.append(True),
            wait=lambda: 0,
            stderr_text=lambda: "",
        )

    monkeypatch.setattr(supervisor.workspace_backend, "spawn", spawn)
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        configured = await client.post(
            "/host-settings",
            headers=headers,
            json={"settings": {"workspace_command_output_max_mib": 1}},
        )
        command = await client.post(
            "/workspaces/athr_source/terminal",
            headers=headers,
            json={"command": "print_many_bytes"},
        )

    assert configured.status_code == 200
    assert command.status_code == 413
    assert killed == [True]


@pytest.mark.asyncio
async def test_owner_commands_are_bounded_and_never_wait_on_the_lifecycle_pool(monkeypatch):
    """A flood of slow owner commands is refused, not queued ahead of turns.

    Commands and Git inspection run on a pool of their own, bounded for each
    workspace and for the sandbox manager; every lifecycle call keeps the
    loop's default pool, so a turn's settle or acquire never waits behind them.
    """
    monkeypatch.setattr(sandbox_manager_mod, "MAX_WORKSPACE_COMMANDS", 2)
    monkeypatch.setattr(sandbox_manager_mod, "MAX_WORKSPACE_COMMANDS_PER_WORKSPACE", 1)
    supervisor = _SharedSupervisor()
    supervisor.threads.add("athr_third")
    release = threading.Event()

    def spawn(_spec):
        release.wait(10)
        return SimpleNamespace(
            lines=lambda: iter(()), kill=lambda: None, wait=lambda: 0, stderr_text=lambda: ""
        )

    monkeypatch.setattr(supervisor.workspace_backend, "spawn", spawn)
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        commands = [
            asyncio.create_task(
                client.post(
                    f"/workspaces/{workspace}/terminal",
                    headers=headers,
                    json={"command": "sleep 119"},
                )
            )
            for workspace in ("athr_source", "athr_child", "athr_third")
            for _ in range(4)
        ]
        try:
            deadline = time.monotonic() + 5
            while sum(task.done() for task in commands) < 10 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            refused = [task.result().status_code for task in commands if task.done()]
            status = await asyncio.wait_for(
                client.get("/shared-sandboxes/athr_source", headers=headers), 2
            )
        finally:
            release.set()
        answered = [response.status_code for response in await asyncio.gather(*commands)]
        again = await client.post(
            "/workspaces/athr_source/terminal", headers=headers, json={"command": "true"}
        )

    assert refused == [429] * 10
    assert status.status_code == 200
    assert sorted(answered) == [200, 200] + [429] * 10
    assert again.status_code == 200, "a finished command gives its slot back"


class _CommandReceiptManager:
    """Stands in for the Docker manager behind a bounded retained command.

    The command's process receipt names its own process group, as ``setsid``
    makes it in the container, so honouring it stops exactly that group. The
    container itself must never be stopped: siblings run in it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.processes: list[subprocess.Popen[str]] = []

    def kill_member_process(self, _spec, *, pidfile: str, user_uid: int, user_gid: int) -> None:
        self.calls.append(("kill_member_process", pidfile))
        for process in self.processes:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)

    def stop(self, _spec) -> None:
        self.calls.append(("stop", ""))


class _LocalRetainedBackend:
    """Run one bounded command as the retained exec production uses, on this host.

    The pipes are opened exactly as ``ChatSandboxManager.spawn`` opens them for
    ``docker exec``, so the sandbox manager reads the same text-mode pipe it
    reads in a container; only the process runs here instead.
    """

    def __init__(self, workdir: str) -> None:
        self.manager = _CommandReceiptManager()
        self.spec = SimpleNamespace(container_name="freeinference-chat-local")
        self.workdir = workdir

    @staticmethod
    def base_env() -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}

    def spawn(self, spec):
        process = subprocess.Popen(
            spec.argv,
            cwd=self.workdir,
            env=spec.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.manager.processes.append(process)
        return _RetainedExecProcess(
            process,
            manager=self.manager,  # type: ignore[arg-type]
            spec=self.spec,  # type: ignore[arg-type]
            process_pidfile=f"{ACTIVATION_TMPFS}/command-{'0' * 32}.pid",
            user_uid=10001,
            user_gid=10001,
        )


def _finish_within(seconds: float, function):
    """Run ``function`` on a thread, failing instead of hanging the suite."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = function()
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"the call was still running after {seconds}s"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["result"]


def test_one_shot_command_output_survives_a_byte_that_is_not_utf8(tmp_path):
    """A stray byte is replaced; it must not drop the output or wedge the read.

    Enough output follows it to fill a pipe: had the read stopped at the byte,
    the command could never exit and the sandbox manager would wait on it
    forever.
    """
    backend = _LocalRetainedBackend(str(tmp_path))
    request = TerminalRequest(
        command="printf 'hello\\n\\377'; head -c 400000 /dev/zero | tr '\\000' y",
        cwd=str(tmp_path),
        timeout_seconds=20,
    )

    result = _finish_within(30, lambda: _run_terminal(backend, str(tmp_path), request))  # type: ignore[arg-type]

    assert result["exit_code"] == 0
    assert result["output"] == "hello\n\ufffd" + "y" * 400000
    assert backend.manager.calls == []


def test_one_shot_command_deadline_covers_a_command_that_closed_its_output(tmp_path):
    backend = _LocalRetainedBackend(str(tmp_path))
    request = TerminalRequest(
        command="exec >/dev/null; sleep 10", cwd=str(tmp_path), timeout_seconds=1
    )
    started = time.monotonic()

    with pytest.raises(HTTPException) as timed_out:
        _finish_within(8, lambda: _run_terminal(backend, str(tmp_path), request))  # type: ignore[arg-type]

    assert timed_out.value.status_code == 408
    assert time.monotonic() - started < 5
    # Only the command's own process group is stopped, never the container.
    assert [name for name, _ in backend.manager.calls] == ["kill_member_process"]


def test_oversized_output_without_newlines_is_refused_before_it_is_buffered(tmp_path):
    """The limit holds for what the sandbox manager holds, not just what it returns."""
    backend = _LocalRetainedBackend(str(tmp_path))
    limit = 1024 * 1024
    request = TerminalRequest(
        command="head -c 33554432 /dev/zero | tr '\\000' x",
        cwd=str(tmp_path),
        timeout_seconds=20,
    )

    tracemalloc.start()
    try:
        with pytest.raises(HTTPException) as refused:
            _finish_within(
                30,
                lambda: _run_terminal(
                    backend,  # type: ignore[arg-type]
                    str(tmp_path),
                    request,
                    max_output_bytes=limit,
                ),
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert refused.value.status_code == 413
    assert peak < 8 * limit
    assert [name for name, _ in backend.manager.calls] == ["kill_member_process"]


class _ReceiptGoneManager(_CommandReceiptManager):
    """The reaper reports failure, as when the command finished just before it.

    Nothing of the command is left running afterwards, as in that case; only
    the report differs.
    """

    def kill_member_process(self, _spec, *, pidfile: str, user_uid: int, user_gid: int) -> None:
        super().kill_member_process(_spec, pidfile=pidfile, user_uid=user_uid, user_gid=user_gid)
        raise SandboxError("resident process cleanup failed: ")


@pytest.mark.parametrize(
    ("command", "timeout_seconds", "status_code"),
    [
        # Just past the limit: the command can finish writing, and exit,
        # before the sandbox manager has read far enough to stop it.
        ("head -c 1100000 /dev/zero | tr '\\000' x", 20, 413),
        ("sleep 10", 1, 408),
    ],
)
def test_a_command_stopped_for_overrunning_answers_why_even_if_its_kill_fails(
    tmp_path, command, timeout_seconds, status_code
):
    """A kill that finds nothing left to signal must not replace the answer.

    The owner asked why the command was stopped; a cleanup error in its place
    reads as a broken workspace. The Docker client is killed either way.
    """
    backend = _LocalRetainedBackend(str(tmp_path))
    backend.manager = _ReceiptGoneManager()
    request = TerminalRequest(command=command, cwd=str(tmp_path), timeout_seconds=timeout_seconds)

    with pytest.raises(HTTPException) as stopped:
        _finish_within(
            30,
            lambda: _run_terminal(
                backend,  # type: ignore[arg-type]
                str(tmp_path),
                request,
                max_output_bytes=1024 * 1024,
            ),
        )

    assert stopped.value.status_code == status_code
    assert [name for name, _ in backend.manager.calls] == ["kill_member_process"]
    # The client was killed all the same; a kill is delivered, not awaited.
    assert all(process.wait(timeout=5) is not None for process in backend.manager.processes)


def test_a_sandbox_manager_command_carries_its_own_process_receipt(monkeypatch):
    """Stopping a timed-out or oversized command must never stop its group.

    The group's container runs every sibling's turn, so a bounded command is
    started under a process receipt of its own, and its kill is refused rather
    than widened when there is none.
    """
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    spawned: list[dict[str, object]] = []
    monkeypatch.setattr(manager, "require_network", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        manager, "spawn", lambda _spec, _argv, **kwargs: spawned.append(kwargs) or "process"
    )
    retained = RetainedChatBackend(manager, SimpleNamespace(sandbox_image_digest=None))  # type: ignore[arg-type]

    retained.spawn(SandboxSpec(argv=["/bin/true"], workdir="/workspace"))

    assert re.fullmatch(
        rf"{re.escape(ACTIVATION_TMPFS)}/command-[0-9a-f]{{32}}\.pid",
        str(spawned[0]["process_pidfile"]),
    )
    unreceipted_manager = _CommandReceiptManager()
    client = subprocess.Popen(
        ["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    unreceipted = _RetainedExecProcess(
        client,
        manager=unreceipted_manager,  # type: ignore[arg-type]
        spec=SimpleNamespace(container_name="freeinference-chat-local"),  # type: ignore[arg-type]
        process_pidfile=None,
        user_uid=None,
        user_gid=None,
    )
    with pytest.raises(SandboxError, match="no process-scoped kill receipt"):
        unreceipted.kill()
    assert unreceipted_manager.calls == []
    assert client.wait(timeout=5) != 0


def _repository(root) -> None:
    """A one-commit repository, committed with a hermetic Git identity."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    for argv in (
        ["git", "init", "--quiet"],
        ["git", "add", "notes.txt"],
        ["git", "commit", "--quiet", "-m", "base"],
    ):
        subprocess.run(argv, cwd=root, env=env, check=True, capture_output=True)


def test_git_inspection_never_rewrites_the_shared_index(tmp_path, monkeypatch):
    """The Git tab must not take index.lock from an agent's concurrent git add.

    A tracked file whose cached stat no longer matches is what makes an
    ordinary ``git status`` refresh the index and write it back.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes.txt").write_text("plain\n")
    _repository(tmp_path)
    os.utime(tmp_path / "notes.txt", (1_000_000_000, 1_000_000_000))
    index = tmp_path / ".git" / "index"
    before = (index.stat().st_ino, index.stat().st_mtime_ns, index.read_bytes())

    inspected = _finish_within(
        30,
        lambda: sandbox_manager_mod._run_git(
            ProcessBackend(acknowledged_unsafe=True), str(tmp_path), timeout_s=20
        ),
    )

    assert inspected["changes"] == []
    assert (index.stat().st_ino, index.stat().st_mtime_ns, index.read_bytes()) == before


def test_git_inspection_shows_a_patch_that_is_not_utf8_but_never_captures_one(
    tmp_path, monkeypatch
):
    """The Git tab may show a replacement character; a snapshot may not store one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes.txt").write_text("plain\n")
    _repository(tmp_path)
    (tmp_path / "notes.txt").write_bytes(b"caf\xe9\n")
    backend = ProcessBackend(acknowledged_unsafe=True)

    shown = _finish_within(
        30, lambda: sandbox_manager_mod._run_git(backend, str(tmp_path), timeout_s=20)
    )
    with pytest.raises(HTTPException) as refused:
        _finish_within(
            30,
            lambda: sandbox_manager_mod._run_git(
                backend, str(tmp_path), timeout_s=20, exact_patch=True
            ),
        )

    assert "+caf\ufffd\n" in shown["patch"]
    assert shown["changes"] == [{"code": " M", "path": "notes.txt"}]
    assert refused.value.status_code == 502
    assert refused.value.detail == "workspace patch is not exact UTF-8 text"


def test_lower_terminal_total_output_limit_stops_the_process():
    class LoudTerminal(_TerminalProcess):
        def chunks(self):
            yield b"x" * 1025

    process = LoudTerminal()
    session = _TerminalSession(
        terminal_id="term_loud",
        process=process,
        workdir=None,
        rows=24,
        cols=80,
        now=0,
        limits=_TerminalLimits(output_bytes=1024),
    )

    session._pump()

    assert process.killed
    events = session.events_after(0, wait_s=0)
    assert any(b"output limit exceeded" in event.data for event in events)
    assert events[-1].kind == "exit"


class _RunningTerminalProcess(_SuspendableTerminalProcess):
    """A shell that stays open, silent, until it is killed."""

    def __init__(self) -> None:
        super().__init__()
        self._killed = threading.Event()

    def chunks(self):
        self._killed.wait(timeout=5)
        yield from ()

    def kill(self) -> None:
        super().kill()
        self._killed.set()


def test_a_shortened_terminal_lifetime_closes_terminals_already_past_it():
    terminals = _TerminalManager(idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8)
    process = _RunningTerminalProcess()
    session = _TerminalSession(
        terminal_id="term_old",
        process=process,
        workdir=None,
        rows=24,
        cols=80,
        now=time.monotonic() - 600,
        idle_ttl_s=None,
        on_closed=None,
    )
    session.start()
    terminals._sessions["athr_source"] = {"term_old": session}

    terminals.reap()
    assert "athr_source" in terminals._sessions, "ten minutes is inside half an hour"

    terminals.configure(idle_ttl_s=300, hard_ttl_s=28800, max_sessions=8)
    assert terminals._reap_interval() == 30.0
    terminals.reap()
    assert "athr_source" not in terminals._sessions
    assert process.killed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/workspaces/athr_source/terminals", None),
        ("POST", "/workspaces/athr_source/terminals/suspend", {"lease_generation": 1}),
        ("POST", "/workspaces/athr_source/terminals/resume", {"lease_generation": 1}),
        ("POST", "/workspaces/athr_source/terminals/resume-settled", {}),
        ("GET", "/workspaces/athr_source/terminals/term_missing/stream", None),
        ("GET", "/workspaces/athr_source/terminals/term_missing/output", None),
        ("POST", "/workspaces/athr_source/terminals/term_missing/input", {"data": "eA=="}),
        ("POST", "/workspaces/athr_source/terminals/term_missing/resize", {"rows": 24, "cols": 80}),
        ("DELETE", "/workspaces/athr_source/terminals/term_missing", None),
        ("POST", "/workspaces/athr_source/terminals", {"rows": 24, "cols": 80}),
    ],
)
async def test_terminal_routes_resolve_their_workspace_off_the_event_loop(method, path, body):
    """A Docker inspect behind a keystroke must not stall every other request.

    Resolving a member's terminal scope reads the container under the
    supervisor's lock, and a busy daemon can take seconds to answer it.
    """
    supervisor = _SharedSupervisor()
    status = supervisor.status
    backend = supervisor.backend
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    inspection_started = asyncio.Event()
    release_inspection = threading.Event()

    def wait_for_event_loop():
        assert threading.get_ident() != loop_thread, "workspace inspection ran on the event loop"
        loop.call_soon_threadsafe(inspection_started.set)
        assert release_inspection.wait(5), "event loop could not release workspace inspection"

    def inspecting_status(thread_id: str):
        wait_for_event_loop()
        return status(thread_id)

    def inspecting_backend(thread_id: str):
        wait_for_event_loop()
        return backend(thread_id)

    supervisor.status = inspecting_status  # type: ignore[method-assign]
    supervisor.backend = inspecting_backend  # type: ignore[method-assign]
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        answered = asyncio.create_task(client.request(method, path, headers=headers, json=body))
        try:
            # Prove that the loop runs while inspection is still blocked. A
            # wall-clock request budget also measures lazy route setup and
            # shared-runner scheduling, neither of which is the regression.
            await asyncio.wait_for(inspection_started.wait(), timeout=5)
            assert not answered.done()
        finally:
            release_inspection.set()
            response = await asyncio.wait_for(answered, timeout=5)
        if path.endswith("/terminals") and method == "POST":
            await client.delete(
                f"/workspaces/athr_source/terminals/{response.json()['id']}", headers=headers
            )

    assert response.status_code in {200, 404}


@pytest.mark.parametrize("blocked", ["write", "suspend"])
def test_a_blocked_terminal_write_or_signal_never_holds_up_its_viewers(blocked):
    """Input into a shell that reads nothing must not freeze its output or viewers.

    A PTY write blocks once the shell stops reading, and a suspension is a
    Docker exec. The output pump and every viewer, on the event loop too,
    take the session's condition, so neither may hold it while it waits.
    """
    entered = threading.Event()
    release = threading.Event()

    class Stuck(_SuspendableTerminalProcess):
        def write(self, data: bytes) -> None:
            entered.set()
            release.wait(5)
            super().write(data)

        def suspend(self) -> None:
            entered.set()
            release.wait(5)
            super().suspend()

    session = _TerminalSession(
        terminal_id="term_stuck", process=Stuck(), workdir=None, rows=24, cols=80, now=0
    )
    call = (
        functools.partial(session.write, b"x", now=1.0) if blocked == "write" else session.suspend
    )
    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    assert entered.wait(2)

    def view() -> None:
        session._append_output(b"output")
        session.descriptor()

    viewer = threading.Thread(target=view, daemon=True)
    viewer.start()
    viewer.join(1)
    stuck = viewer.is_alive()
    release.set()
    caller.join(5)

    assert not stuck, f"a blocked {blocked} held the session's viewers"
    assert [event.data for event in session.events_after(0, wait_s=0)] == [b"output"]


@pytest.mark.parametrize(
    ("idle", "hard", "sessions"),
    [(0, 60, 8), (60, -1, 8), (60, 60, 0), (60, 60, 4097)],
)
def test_terminal_limits_are_refused_outside_the_bounds_the_sandbox_manager_starts_with(
    idle, hard, sessions
):
    terminals = _TerminalManager(idle_ttl_s=30, hard_ttl_s=60, max_sessions=8)

    with pytest.raises(ValueError):
        terminals.configure(idle_ttl_s=idle, hard_ttl_s=hard, max_sessions=sessions)
    assert (terminals._idle_ttl_s, terminals._hard_ttl_s, terminals._max_sessions) == (30, 60, 8)


def test_terminal_suspensions_are_reference_counted_per_concurrent_turn():
    terminals = _TerminalManager(idle_ttl_s=30, hard_ttl_s=60, max_sessions=8)

    terminals.suspend("athr_source", lease_generation=2, job_id="job-parent")
    terminals.suspend("athr_source", lease_generation=1, job_id="job-child")
    assert terminals.resume("athr_source", lease_generation=2, job_id="job-parent") == {
        "ok": True,
        "resumed": 0,
    }
    assert "athr_source" in terminals._suspended_workspaces
    assert terminals.resume_settled("athr_source", job_id="job-parent") == {
        "ok": True,
        "resumed": 0,
    }
    assert "athr_source" in terminals._suspended_workspaces
    assert terminals.resume("athr_source", lease_generation=1, job_id="job-child") == {
        "ok": True,
        "resumed": 0,
    }
    assert "athr_source" not in terminals._suspended_workspaces


def test_legacy_settled_resume_resets_its_per_job_generation():
    terminals = _TerminalManager(idle_ttl_s=30, hard_ttl_s=60, max_sessions=8)

    terminals.suspend("athr_source", lease_generation=2)
    assert terminals.resume_settled("athr_source") == {"ok": True, "resumed": 0}
    terminals.suspend("athr_source", lease_generation=1)

    assert terminals._workspace_generations["athr_source"] == 1
    assert "athr_source" in terminals._suspended_workspaces


def test_partial_terminal_resume_refreezes_every_process(tmp_path):
    """One failed PTY cannot leave an earlier sibling running behind the barrier."""
    terminals = _TerminalManager(idle_ttl_s=30, hard_ttl_s=60, max_sessions=8)
    processes = [_SuspendableTerminalProcess(), _SuspendableTerminalProcess(fail_resume=True)]
    sessions = [
        _TerminalSession(
            terminal_id=f"term_{index}",
            process=process,
            workdir=tmp_path,
            rows=24,
            cols=80,
            now=0,
        )
        for index, process in enumerate(processes)
    ]
    terminals._sessions["athr_source"] = {session.id: session for session in sessions}
    terminals.suspend("athr_source", lease_generation=1, job_id="job-parent")

    with pytest.raises(HTTPException) as raised:
        terminals.resume("athr_source", lease_generation=1, job_id="job-parent")

    assert raised.value.status_code == 503
    assert all(process.suspended for process in processes)
    assert processes[0].suspend_calls == 2
    assert processes[1].suspend_calls == 2
    assert "athr_source" in terminals._suspended_workspaces


class _SlowToStopTerminalProcess(_SuspendableTerminalProcess):
    """A terminal whose STOP, a Docker exec in production, waits to be released."""

    def __init__(self) -> None:
        super().__init__()
        self.stopping = threading.Event()
        self.release = threading.Event()

    def suspend(self) -> None:
        self.stopping.set()
        assert self.release.wait(5)
        super().suspend()


def _listed_session(terminals: _TerminalManager, workspace_id: str, process, tmp_path):
    session = _TerminalSession(
        terminal_id=f"term_{len(terminals._sessions.get(workspace_id, {}))}_{workspace_id}",
        process=process,
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=time.monotonic(),
    )
    terminals._sessions.setdefault(workspace_id, {})[session.id] = session
    return session


def test_a_slow_terminal_suspension_never_holds_up_another_workspace(tmp_path):
    """Every terminal request on the sandbox manager takes the manager's lock.

    Suspending a workspace signals each of its terminals through Docker, which
    can take seconds; holding that lock meanwhile stalled every keystroke,
    listing and attach in every other workspace on the host.
    """
    terminals = _TerminalManager(idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8)
    slow = _SlowToStopTerminalProcess()
    _listed_session(terminals, "athr_source", slow, tmp_path)
    other = _listed_session(terminals, "athr_other", _TerminalProcess(), tmp_path)
    suspension = threading.Thread(
        target=functools.partial(
            terminals.suspend, "athr_source", lease_generation=1, job_id="job-parent"
        ),
        daemon=True,
    )
    suspension.start()
    assert slow.stopping.wait(2)

    try:
        controlled, listed = _finish_within(
            1,
            lambda: (
                terminals.get_for_control("athr_other", other.id),
                terminals.list("athr_other"),
            ),
        )
    finally:
        slow.release.set()
        suspension.join(5)

    assert controlled is other
    assert [item["id"] for item in listed] == [other.id]
    assert slow.suspended
    assert terminals._workspace_locks == {}


def test_a_resume_waits_for_its_own_workspaces_suspension_to_finish(tmp_path):
    """A workspace's freeze and thaw still run one at a time.

    Releasing the turn's token mid-freeze would let a terminal the freeze had
    not reached yet be stopped after the thaw, with no turn left to resume it.
    """
    terminals = _TerminalManager(idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8)
    slow = _SlowToStopTerminalProcess()
    processes = [slow, _SuspendableTerminalProcess()]
    for process in processes:
        _listed_session(terminals, "athr_source", process, tmp_path)
    suspension = threading.Thread(
        target=functools.partial(
            terminals.suspend, "athr_source", lease_generation=1, job_id="job-parent"
        ),
        daemon=True,
    )
    suspension.start()
    assert slow.stopping.wait(2)
    resumption = threading.Thread(
        target=functools.partial(
            terminals.resume, "athr_source", lease_generation=1, job_id="job-parent"
        ),
        daemon=True,
    )
    resumption.start()
    resumption.join(0.3)
    tokens_mid_freeze = set(terminals._workspace_suspensions.get("athr_source", ()))
    slow.release.set()
    suspension.join(5)
    resumption.join(5)

    assert tokens_mid_freeze == {"job-parent"}
    assert [process.suspended for process in processes] == [False, False]
    assert "athr_source" not in terminals._suspended_workspaces
    assert terminals._workspace_locks == {}


def test_terminal_ring_is_bounded_and_reports_cursor_reset(tmp_path):
    session = _TerminalSession(
        terminal_id="term_test",
        process=_TerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )
    for _ in range(MAX_TERMINAL_BUFFER_EVENTS + 1):
        session._append_output(b"x")

    events = session.events_after(0, wait_s=0)
    assert len(events) == MAX_TERMINAL_BUFFER_EVENTS
    assert events[0].kind == "reset"
    assert events[0].reason == "output_truncated"
    assert _terminal_sse_frame(events[0]).startswith(f"id: {events[0].seq}\nevent: reset\n")

    session = _TerminalSession(
        terminal_id="term_large",
        process=_TerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )
    session._append_output(b"a" + b"b" * MAX_TERMINAL_BUFFER_BYTES)
    events = session.events_after(0, wait_s=0)
    assert [(event.kind, len(event.data)) for event in events] == [
        ("reset", 0),
        ("output", MAX_TERMINAL_BUFFER_BYTES),
    ]


def test_terminal_idle_guard_cannot_outlive_the_chat_clock(tmp_path):
    session = _TerminalSession(
        terminal_id="term_test",
        process=_TerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=100,
        idle_ttl_s=10,
    )
    assert session.expired(now=109, idle_ttl_s=30, hard_ttl_s=60) is False
    assert session.expired(now=110, idle_ttl_s=30, hard_ttl_s=60) is True


class _Spawner:
    """Hands ``_TerminalManager.create`` the next prepared process."""

    def __init__(self, *processes: TerminalProcess) -> None:
        self.processes = list(processes)

    @staticmethod
    def base_env() -> dict[str, str]:
        return {}

    def spawn_terminal(self, _spec, *, rows: int, cols: int) -> TerminalProcess:
        return self.processes.pop(0)


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.01)


def _state(
    terminals: _TerminalManager, terminal_id: str, workspace_id: str = "athr_source"
) -> str | None:
    return next(
        (row["state"] for row in terminals.list(workspace_id) if row["id"] == terminal_id), None
    )


def test_an_exited_shell_gives_back_its_terminal_slot():
    """`exit` must not leave the workspace stuck at its terminal limit."""
    terminals = _TerminalManager(
        idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8, max_sessions_per_workspace=2
    )
    running, exited, replacement = (
        _RunningTerminalProcess(),
        _TerminalProcess(),  # its output ends at once: the owner typed `exit`
        _RunningTerminalProcess(),
    )
    spawner = _Spawner(running, exited, replacement, _RunningTerminalProcess())

    def create() -> dict:
        return terminals.create("athr_source", "/workspace", rows=24, cols=80, backend=spawner)

    first = create()
    gone = create()
    _wait_for(lambda: _state(terminals, gone["id"]) == "exited")
    assert [row["id"] for row in terminals.list("athr_source")] == [first["id"], gone["id"]]

    third = create()

    # The exited shell made room and was dropped to stay within the bound.
    assert [row["id"] for row in terminals.list("athr_source")] == [first["id"], third["id"]]
    with pytest.raises(HTTPException) as refused:
        create()
    assert refused.value.status_code == 409
    terminals.close()


def test_an_exited_shell_stays_listed_while_there_is_room():
    terminals = _TerminalManager(
        idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8, max_sessions_per_workspace=4
    )
    spawner = _Spawner(_TerminalProcess(), _RunningTerminalProcess())
    gone = terminals.create("athr_source", "/workspace", rows=24, cols=80, backend=spawner)
    _wait_for(lambda: _state(terminals, gone["id"]) == "exited")

    live = terminals.create("athr_source", "/workspace", rows=24, cols=80, backend=spawner)

    assert {row["id"]: row["state"] for row in terminals.list("athr_source")} == {
        gone["id"]: "exited",
        live["id"]: "running",
    }
    terminals.close()


def test_exited_shells_across_workspaces_stay_within_the_sandbox_manager_limit():
    """Exited shells keep their output, so the sandbox-manager-wide bound still holds them."""
    terminals = _TerminalManager(
        idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=2, max_sessions_per_workspace=2
    )
    oldest = _TerminalProcess()
    spawner = _Spawner(
        oldest,
        _TerminalProcess(),
        _RunningTerminalProcess(),
        _RunningTerminalProcess(),
        _RunningTerminalProcess(),
    )

    def create(workspace_id: str) -> dict:
        return terminals.create(workspace_id, "/workspace", rows=24, cols=80, backend=spawner)

    def listed() -> dict[str, list[str]]:
        return {
            workspace_id: [row["id"] for row in terminals.list(workspace_id)]
            for workspace_id in ("athr_one", "athr_two", "athr_three")
        }

    first = create("athr_one")
    second = create("athr_two")
    _wait_for(lambda: _state(terminals, first["id"], "athr_one") == "exited")
    _wait_for(lambda: _state(terminals, second["id"], "athr_two") == "exited")

    # Neither exited shell refuses a third terminal, but together they would
    # exceed the sandbox manager's bound: the oldest of them, in another chat, goes.
    live = create("athr_three")
    assert listed() == {"athr_one": [], "athr_two": [second["id"]], "athr_three": [live["id"]]}
    assert oldest.killed
    assert "athr_one" not in terminals._sessions

    another = create("athr_three")
    assert listed() == {"athr_one": [], "athr_two": [], "athr_three": [live["id"], another["id"]]}

    # Running shells still refuse one past the limit.
    with pytest.raises(HTTPException) as refused:
        create("athr_one")
    assert refused.value.status_code == 503
    terminals.close()


def test_the_descriptor_names_the_shell_the_process_runs():
    class Bash(_RunningTerminalProcess):
        shell = "bash"

    terminals = _TerminalManager(idle_ttl_s=1800, hard_ttl_s=28800, max_sessions=8)
    spawner = _Spawner(Bash(), _RunningTerminalProcess())

    bash = terminals.create("athr_source", "/workspace", rows=24, cols=80, backend=spawner)
    plain = terminals.create("athr_source", "/workspace", rows=24, cols=80, backend=spawner)

    assert (bash["shell"], plain["shell"]) == ("bash", "sh")
    terminals.close()


@pytest.mark.asyncio
async def test_a_terminal_stream_first_says_where_its_history_ends(monkeypatch):
    """A new viewer must know which bytes were produced before it attached."""

    class Finished(_TerminalProcess):
        def chunks(self):
            yield b"\x1b[6n"  # a cursor-position query, long since answered

    supervisor = _SharedSupervisor()
    monkeypatch.setattr(
        supervisor.workspace_backend, "spawn_terminal", lambda *_a, **_k: Finished()
    )
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        created = await client.post(
            "/workspaces/athr_source/terminals", headers=headers, json={"rows": 24, "cols": 80}
        )
        terminal_id = created.json()["id"]
        terminals = app.state.terminal_manager
        _wait_for(lambda: _state(terminals, terminal_id) == "exited")
        replay = await client.get(
            f"/workspaces/athr_source/terminals/{terminal_id}/stream", headers=headers
        )
        resumed = await client.get(
            f"/workspaces/athr_source/terminals/{terminal_id}/stream?after=1", headers=headers
        )

    output = base64.b64encode(b"\x1b[6n").decode()
    assert replay.text == (
        'event: attached\ndata: {"seq":2}\n\n'
        f'id: 1\nevent: output\ndata: {{"seq":1,"data":"{output}"}}\n\n'
        'id: 2\nevent: exit\ndata: {"seq":2,"exit_code":0}\n\n'
    )
    assert resumed.text == (
        'event: attached\ndata: {"seq":2}\n\nid: 2\nevent: exit\ndata: {"seq":2,"exit_code":0}\n\n'
    )


@pytest.mark.asyncio
async def test_activation_boundary_failure_is_not_reported_as_success():
    class Turn:
        def lines(self):
            return iter(())

        def wait(self):
            return SimpleNamespace(
                status="completed",
                final_output="done",
                exit_code=0,
                usage=None,
                raw=None,
                native_turn_settled=False,
            )

        def stderr_text(self):
            return ""

    supervisor = _SharedSupervisor()
    supervisor.start_turn = lambda *_args, **_kwargs: Turn()  # type: ignore[attr-defined]
    supervisor.complete_turn = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        SandboxError("member permission normalization failed")
    )
    supervisor.abort_turn = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    app = _app(supervisor)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            json={
                "token": "lease",
                "prompt": "test",
                "model": "model",
                "reasoning_effort": None,
            },
        )

    frames = [json.loads(line) for line in response.text.splitlines() if line]
    assert len(frames) == 1
    assert frames[0]["kind"] == "error"
    assert frames[0]["detail"] == "member permission normalization failed"
    assert frames[0]["resources"]["process_observation_complete"] is False
    assert frames[0]["resources"]["tool_calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_shared_turn_returns_closed_member_telemetry_and_exact_tool_marks(failed):
    marks: dict[str, float] = {}

    class Turn:
        @staticmethod
        def lines():
            yield SimpleNamespace(
                event_type="tool_use",
                payload={"id": "toolu_exact", "name": "Bash"},
                raw_line='{"type":"tool_use"}',
            )
            marks["pre"] = time.monotonic()
            time.sleep(0.001)
            marks["post"] = time.monotonic()
            yield SimpleNamespace(
                event_type="tool_result",
                payload={"tool_use_id": "toolu_exact"},
                raw_line='{"type":"tool_result"}',
            )

        @staticmethod
        def wait():
            return SimpleNamespace(
                status="failed" if failed else "completed",
                final_output="" if failed else "done",
                exit_code=1 if failed else 0,
                usage=None,
                raw=None,
                native_turn_settled=failed,
            )

        @staticmethod
        def stderr_text():
            return ""

    process = GuestProcess(
        process_seq=1,
        uid=11001,
        pid=412,
        tgid=412,
        ppid=41,
        comm="tiny",
        executable="/bin/tiny",
        started_guest_mono_ns=1_001_000_000,
        exited_guest_mono_ns=1_001_600_000,
        started_host_mono=1e20,
        exited_host_mono=1e20,
        exit_code=0,
        observed_fork=True,
        observed_exec=True,
        observed_exit=True,
    )

    supervisor = _SharedSupervisor()
    supervisor.start_turn = lambda *_args, **_kwargs: Turn()  # type: ignore[attr-defined]
    supervisor.complete_turn = (  # type: ignore[attr-defined]
        lambda *_args, **_kwargs: SharedTurnTelemetry(
            observation=GuestObservation(
                source="kata-guest-ebpf-v1",
                complete=True,
                lost_events=0,
                truncated=False,
                processes=(process,),
                origin_guest_mono_ns=1_000_000_000,
            ),
            tool_marks=(
                ToolMark(marks["pre"], "pre", "toolu_exact"),
                ToolMark(marks["post"], "post", "toolu_exact"),
            ),
        )
    )
    supervisor.abort_turn = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        AssertionError("completed turn was aborted")
    )
    app = _app(supervisor)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            json={
                "token": "lease",
                "prompt": "test",
                "model": "model",
                "reasoning_effort": None,
            },
        )

    frames = [json.loads(line) for line in response.text.splitlines() if line]
    result = next(frame for frame in frames if frame["kind"] == "result")
    assert result["native_turn_settled"] is failed
    assert result["status"] == ("failed" if failed else "completed")
    resources = result["resources"]
    assert resources["sampler_version"] == "shared-member-v1"
    assert resources["coverage"] == 0.0
    assert resources["process_observer"] == "kata-guest-ebpf-v1"
    assert resources["process_observation_complete"] is True
    assert resources["processes"][0]["pid"] == 412
    assert resources["tool_calls"][0]["timing_source"] == "exact"


def test_build_turn_resources_adopts_late_announced_guest_marks_and_logs_them(caplog):
    """A short call whose announcing line reached the sandbox manager after it ran:
    the guest pair is adopted, its process is attributed on the guest clock,
    and the log carries the lag for a reviewer to check per call."""
    from cloud_agent_host.sandbox_manager import build_turn_resources
    from cloud_agent_host.tool_windows import ToolWindowTracker

    tracker = ToolWindowTracker()
    tracker.observe("tool_use", {"id": "toolu_late", "name": "Read"}, mono=100.050)
    tracker.observe("tool_result", {"tool_use_id": "toolu_late"}, mono=100.120)
    process = GuestProcess(
        process_seq=1,
        uid=11001,
        pid=412,
        tgid=412,
        ppid=41,
        comm="rg",
        executable="/usr/bin/rg",
        started_guest_mono_ns=5_005_000_000,
        exited_guest_mono_ns=5_015_000_000,
        started_host_mono=100.005,
        exited_host_mono=100.015,
        exit_code=0,
        observed_fork=True,
        observed_exec=True,
        observed_exit=True,
        cpu_usec=1_200,
        mem_peak_bytes=4096,
        io_read_bytes=0,
        io_write_bytes=0,
    )
    telemetry = SharedTurnTelemetry(
        observation=GuestObservation(
            source="kata-guest-ebpf-v1",
            complete=True,
            lost_events=0,
            truncated=False,
            processes=(process,),
            origin_guest_mono_ns=5_000_000_000,
            clock_error_ns=150_000_000,
        ),
        tool_marks=(
            ToolMark(100.000, "pre", "toolu_late", guest_mono_ns=5_000_000_000),
            ToolMark(100.021, "post", "toolu_late", guest_mono_ns=5_021_000_000),
        ),
    )

    with caplog.at_level(logging.INFO, logger="cloud_agent_host.sandbox_manager"):
        resources = build_turn_resources(telemetry, tracker, thread_id="athr_late")

    (call,) = resources["tool_calls"]
    assert call["timing_source"] == "exact"
    assert call["duration_ms"] == 21
    assert call["process_count"] == 1
    assert call["process_cpu_usec"] == 1_200
    assert call["process_resources_partial"] is False
    assert resources["processes"][0]["tool_call_seqs"] == [1]
    line = next(
        record.getMessage() for record in caplog.records if "tool marks" in record.getMessage()
    )
    assert "thread=athr_late" in line
    assert "applied=1" in line
    assert "1:Read:applied:lag=+50/+99:msg=70ms:exact=21ms" in line


@pytest.mark.asyncio
async def test_inventory_rows_carry_each_groups_latest_checkpoint(tmp_path):
    from cloud_agent_host.sandbox_checkpoints import (
        SandboxCheckpointReceipt,
        SandboxCheckpointStore,
    )

    supervisor = _SharedSupervisor()
    supervisor.list_for_thread = lambda _thread_id: [_descriptor("athr_source")]
    app = _app(supervisor)
    store = SandboxCheckpointStore(tmp_path)
    receipt = SandboxCheckpointReceipt(
        checkpoint_id="c" * 32,
        sandbox_id=_descriptor("athr_source").sandbox_id,
        generation=1,
        container_id="container-shared",
        base_image="sha256:" + "b" * 64,
        runtime_id="claude-code",
        runtime_version=None,
        object_key="key",
        version_id="v1",
        sha256="e" * 64,
        size_bytes=10,
        new_layer_bytes=5,
        layer_count=2,
        created_at="2026-10-13T12:00:00+00:00",
    )
    store.save(receipt)
    app.state.sandbox_checkpoint_store = store
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.get(
            "/sandboxes",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            params={"thread_id": "athr_source"},
        )
        app.state.sandbox_checkpoint_store = None
        without_store = await client.get(
            "/sandboxes",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            params={"thread_id": "athr_source"},
        )

    assert response.status_code == 200
    assert response.json()["sandboxes"][0]["checkpoint"] == receipt.as_dict()
    assert without_store.json()["sandboxes"][0]["checkpoint"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("starting", [True, False])
async def test_only_a_starting_turn_has_its_members_ssh_keys_written(monkeypatch, starting):
    """The runner says its turn starts and masks exactly those keys; a push cannot say so."""
    from cloud_agent_shared.ssh_access import SshAccess

    supervisor = _SharedSupervisor()
    supervisor.workspace_backend.manager.inspect = lambda _name: SimpleNamespace(running=True)
    supervisor.workspace_backend.spec.container_name = "container-shared"
    applied: list[str | None] = []

    def apply(_manager, _spec, access, *, starting_thread_id):
        applied.append(starting_thread_id)
        return {"newest": True, "revision": access.revision}

    monkeypatch.setattr(sandbox_manager_mod, "apply_ssh_access", apply)
    body = {"access": SshAccess(revision=3, members=[]).model_dump(mode="json")}
    async with AsyncClient(
        transport=ASGITransport(app=_app(supervisor)), base_url="http://sandbox_manager"
    ) as client:
        response = await client.put(
            "/workspaces/athr_source/ssh-access",
            json={**body, "starting": True} if starting else body,
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"newest": True, "revision": 3, "running": True}
    assert applied == ["athr_source" if starting else None]
