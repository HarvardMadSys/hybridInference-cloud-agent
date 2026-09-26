"""Regression tests for the shared-only workspace broker."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from cloud_agent.workspace_broker_client import WorkspaceBrokerClient
from cloud_agent_host.chat_sandbox import RetainedChatBackend
from cloud_agent_host.guest_observer import GuestObservation, GuestProcess
from cloud_agent_host.sandbox import ProcessBackend, SandboxError, TerminalProcess
from cloud_agent_host.shared_session_supervisor import (
    SharedSessionDescriptor,
    SharedSessionLease,
    SharedTurnTelemetry,
)
from cloud_agent_host.tool_windows import ToolMark
from cloud_agent_host.workspace_broker import (
    MAX_TERMINAL_BUFFER_BYTES,
    MAX_TERMINAL_BUFFER_EVENTS,
    MAX_TERMINAL_SESSIONS_PER_WORKSPACE,
    TerminalRequest,
    _run_terminal,
    _terminal_sse_frame,
    _TerminalManager,
    _TerminalSession,
    create_app,
)


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
        process = _TerminalProcess()
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

    def __init__(self) -> None:
        self.workspace_backend = _WorkspaceBackend()
        self.calls: list[tuple] = []
        self.imports = self.workspace_backend.imports
        self.started = False
        self.closed = False
        self.threads = {"athr_source", "athr_child"}

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def owns(self, thread_id: str) -> bool:
        return thread_id in self.threads

    def status(self, thread_id: str):
        if not self.owns(thread_id):
            raise SandboxError("unknown member")
        return _descriptor(thread_id)

    def list(self):
        return [_descriptor("athr_source"), _descriptor("athr_child")]

    def register_manifest(self, spec, members, *, expected_container_id=None):
        self.calls.append(("manifest", spec, members, expected_container_id))
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
    ):
        self.calls.append(("acquire", thread_id, profiling))
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


def _launch() -> dict[str, object]:
    """The minimum resident launch the broker's request model accepts."""
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
        token="broker-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
    )


def test_broker_requires_a_container_backend_without_an_injected_supervisor():
    with pytest.raises(ValueError, match="require the container or kata backend"):
        create_app(backend=ProcessBackend(acknowledged_unsafe=True), token="x")


@pytest.mark.asyncio
async def test_broker_lifespan_preflights_without_a_host_worktree():
    supervisor = _SharedSupervisor()
    backend = ProcessBackend(acknowledged_unsafe=True)
    roots: list[object] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        backend, "preflight", lambda *, workdir_root=None: roots.append(workdir_root)
    )
    app = create_app(
        backend=backend,
        token="broker-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
    )
    try:
        async with app.router.lifespan_context(app):
            assert roots == [None]
            assert supervisor.started is True
    finally:
        monkeypatch.undo()
    assert supervisor.closed is True


@pytest.mark.asyncio
async def test_health_and_sandbox_inventory_are_authenticated_and_shared_only():
    app = _app(_SharedSupervisor())
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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
async def test_legacy_lifecycle_and_host_workspace_routes_do_not_exist():
    app = _app(_SharedSupervisor())
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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
        assert not hasattr(WorkspaceBrokerClient, method)


@pytest.mark.asyncio
async def test_an_invalid_profiling_context_never_rejects_a_sandbox_request():
    """Measurement settings must not be able to fail a startup.

    Regression: the descriptor accepted a 129 character run id while this
    request model capped it at 128, so the same acquire answered 200 without a
    selection and 422 with one -- before the supervisor was reached.
    """
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    identity = _identity()
    live = {
        "run_id": "run-1",
        "measurement_id": "msm_x",
        "operation": "attempt.startup",
        "expires_at": time.time() + 60,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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


@pytest.mark.asyncio
async def test_shared_registration_snapshot_and_base_import_keep_identity():
    supervisor = _SharedSupervisor()
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    identity = _identity()
    bundle = b"trusted-git-bundle"
    commit_sha = "b" * 40

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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

    monkeypatch.setattr("cloud_agent_host.workspace_broker.read_retained_file", read)
    monkeypatch.setattr("cloud_agent_host.workspace_broker.write_retained_file", write)
    app = _app(supervisor)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
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
    """A short call whose announcing line reached the broker after it ran:
    the guest pair is adopted, its process is attributed on the guest clock,
    and the log carries the lag for a reviewer to check per call."""
    from cloud_agent_host.tool_windows import ToolWindowTracker
    from cloud_agent_host.workspace_broker import build_turn_resources

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

    with caplog.at_level(logging.INFO, logger="cloud_agent_host.workspace_broker"):
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
