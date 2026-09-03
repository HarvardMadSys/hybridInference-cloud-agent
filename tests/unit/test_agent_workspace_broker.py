"""Regression tests for durable, owner-editable agent workspaces."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from cloud_agent_host.chat_sandbox import RetainedChatBackend
from cloud_agent_host.guest_observer import GuestObservation, GuestProcess
from cloud_agent_host.sandbox import (
    ProcessBackend,
    SandboxBackend,
    SandboxError,
    SandboxSpec,
    TerminalProcess,
)
from cloud_agent_host.session_supervisor import SessionDescriptor, SessionLease
from cloud_agent_host.shared_session_supervisor import SharedTurnTelemetry
from cloud_agent_host.workspace_broker import (
    MAX_TERMINAL_BUFFER_BYTES,
    MAX_TERMINAL_BUFFER_EVENTS,
    TerminalRequest,
    _restartable_mode_covers_every_runtime,
    _run_terminal,
    _terminal_sse_frame,
    _TerminalSession,
    create_app,
)
from cloud_agent_host.workspace_paths import (
    WorkspaceIdError,
    purge_stale_workspaces,
    workspace_path,
)


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _workspace(tmp_path):
    root = tmp_path / "workspaces"
    repo = root / "ajob_test"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "agent@example.test")
    _git(repo, "config", "user.name", "Agent")
    (repo / "README.md").write_text("before\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "base")
    return root, repo


def test_partial_restartable_broker_rollout_keeps_legacy_mount_preflight(monkeypatch):
    monkeypatch.setenv("AGENT_RESTARTABLE_RUNTIMES", "claude-code")
    assert _restartable_mode_covers_every_runtime(True) is False

    monkeypatch.delenv("AGENT_RESTARTABLE_RUNTIMES")
    assert _restartable_mode_covers_every_runtime(True) is True


class _FakeTerminalProcess(TerminalProcess):
    """Deterministic byte terminal for broker protocol tests."""

    def __init__(self) -> None:
        self.inputs: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.killed = False
        self.suspended = False
        self.suspensions: list[bool] = []
        self._natural_exit = False
        self._output: queue.Queue[bytes | None] = queue.Queue()
        self._output.put(b"prompt> ")
        self._lock = threading.Lock()

    def chunks(self):
        while (chunk := self._output.get()) is not None:
            yield chunk

    def write(self, data: bytes) -> None:
        self.inputs.append(data)
        if data == b"exit\n":
            self._output.put(b"bye\r\n")
            self._natural_exit = True
            self._output.put(None)

    def resize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def suspend(self) -> None:
        self.suspended = True
        self.suspensions.append(True)

    def resume(self) -> None:
        self.suspended = False
        self.suspensions.append(False)

    def kill(self) -> None:
        with self._lock:
            if self.killed or self._natural_exit:
                return
            self.killed = True
            self._output.put(None)

    def wait(self) -> int:
        return 137 if self.killed else 0


class _FakeTerminalBackend(SandboxBackend):
    """Backend that records PTY operations without starting host processes."""

    def __init__(self) -> None:
        self.processes: list[_FakeTerminalProcess] = []
        self.specs: list[SandboxSpec] = []

    def spawn(self, spec: SandboxSpec):
        raise AssertionError("the legacy command path is not used by this fixture")

    def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
        process = _FakeTerminalProcess()
        process.sizes.append((rows, cols))
        self.specs.append(spec)
        self.processes.append(process)
        return process


class _RecordingTerminalBackend(_FakeTerminalBackend):
    """Record broker-only startup preparation without touching Docker."""

    def __init__(self) -> None:
        super().__init__()
        self.prepared_roots: list[str] = []

    def prepare_terminal_broker(self, workdir_root: str) -> None:
        self.prepared_roots.append(workdir_root)


async def _wait_for_last_seq(
    client: AsyncClient,
    headers: dict[str, str],
    terminal_id: str,
    expected: int,
) -> None:
    """Wait for the background output pump without timing-based assertions."""
    for _ in range(100):
        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        descriptor = next(
            terminal for terminal in listing.json()["terminals"] if terminal["id"] == terminal_id
        )
        if descriptor["last_seq"] >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"terminal did not reach sequence {expected}")


def test_workspace_path_rejects_directory_escape(tmp_path):
    with pytest.raises(WorkspaceIdError):
        workspace_path(tmp_path, "../other")


def test_workspace_ttl_removes_only_expired_real_directories(tmp_path):
    root = tmp_path / "workspaces"
    stale = root / "athr_stale"
    fresh = root / "athr_fresh"
    stale.mkdir(parents=True)
    fresh.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "athr_link")
    os.utime(stale, (10, 10))
    os.utime(fresh, (90, 90))

    removed = purge_stale_workspaces(root, ttl_seconds=50, now=100)

    assert removed == ["athr_stale"]
    assert not stale.exists()
    assert fresh.exists()
    assert outside.exists(), "cleanup must not follow a workspace symlink"


@pytest.mark.asyncio
async def test_broker_browses_and_writes_the_real_worktree(tmp_path):
    root, repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        listing = await client.get("/workspaces/ajob_test/files", headers=headers)
        saved = await client.put(
            "/workspaces/ajob_test/files",
            params={"path": "README.md"},
            json={"content": "after\n"},
            headers=headers,
        )

    assert listing.status_code == 200
    assert listing.json()["source"] == "workspace"
    assert listing.json()["writable"] is True
    assert saved.status_code == 200
    assert saved.json()["content"] == "after\n"
    assert repo.joinpath("README.md").read_text() == "after\n"


@pytest.mark.asyncio
async def test_broker_refuses_auth_bypass_and_symlink_writes(tmp_path):
    root, repo = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, repo / "escape")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "private.txt").write_text("private")
    os.symlink(outside_dir, repo / "escape-dir")
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        unauthenticated = await client.get("/workspaces/ajob_test/files")
        write = await client.put(
            "/workspaces/ajob_test/files",
            params={"path": "escape"},
            json={"content": "stolen"},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )
        symlink = await client.get(
            "/workspaces/ajob_test/files",
            params={"path": "escape"},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )
        nested = await client.get(
            "/workspaces/ajob_test/files",
            params={"path": "escape-dir/private.txt"},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )

    assert unauthenticated.status_code == 401
    assert write.status_code == 409
    assert symlink.status_code == 200
    assert symlink.json()["kind"] == "symlink"
    assert "secret" not in str(symlink.json())
    assert nested.status_code == 400
    assert outside.read_text() == "secret"


@pytest.mark.asyncio
async def test_broker_terminal_and_git_operate_on_the_same_worktree(tmp_path):
    root, repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        terminal = await client.post(
            "/workspaces/ajob_test/terminal",
            json={"command": "printf terminal > terminal.txt", "cwd": str(repo)},
            headers=headers,
        )
        git = await client.get("/workspaces/ajob_test/git", headers=headers)

    assert terminal.status_code == 200
    assert terminal.json()["exit_code"] == 0
    assert repo.joinpath("terminal.txt").read_text() == "terminal"
    assert git.status_code == 200
    assert git.json()["available"] is True
    assert any(change["path"] == "terminal.txt" for change in git.json()["changes"])
    assert "terminal.txt" in git.json()["patch"]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert status == "?? terminal.txt\n", "Git inspection must not mutate the real index"


def test_shared_retained_one_shot_terminal_uses_group_writable_umask():
    class Process:
        @staticmethod
        def lines():
            return iter(())

        @staticmethod
        def kill():
            raise AssertionError("the bounded command must not be killed")

        @staticmethod
        def wait():
            return 0

        @staticmethod
        def stderr_text():
            return ""

    class Backend(RetainedChatBackend):
        def __init__(self):
            self.spec = SimpleNamespace(shared_live=True)
            self.seen: SandboxSpec | None = None

        @staticmethod
        def base_env():
            return {}

        def spawn(self, spec):
            self.seen = spec
            return Process()

    backend = Backend()

    result = _run_terminal(
        backend,
        "/workspace",
        TerminalRequest(command="touch shared.txt", cwd="/workspace", timeout_seconds=1),
    )

    assert result["exit_code"] == 0
    assert backend.seen is not None
    assert backend.seen.argv[-1].startswith("umask 0002; ")


@pytest.mark.asyncio
async def test_broker_git_keeps_local_commits_in_the_base_diff(tmp_path):
    root, repo = _workspace(tmp_path)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "committed.txt").write_text("from terminal\n")
    _git(repo, "add", "committed.txt")
    _git(repo, "commit", "--quiet", "-m", "terminal commit")
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        response = await client.get(
            "/workspaces/ajob_test/git",
            params={"base_sha": base_sha},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )

    assert response.status_code == 200
    assert "committed.txt" in response.json()["patch"]


@pytest.mark.asyncio
async def test_broker_terminal_session_create_list_input_resize_and_resume_stream(tmp_path):
    """The private protocol carries raw PTY bytes with resumable sequence ids."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post(
            "/workspaces/ajob_test/terminals",
            json={"rows": 30, "cols": 120},
            headers=headers,
        )
        assert created.status_code == 200
        terminal = created.json()
        terminal_id = terminal["id"]
        assert terminal == {
            "id": terminal_id,
            "shell": "sh",
            "state": "running",
            "cwd": "/workspace",
            "rows": 30,
            "cols": 120,
            "last_seq": terminal["last_seq"],
        }

        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["terminals"][0]["id"] == terminal_id

        resized = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/resize",
            json={"rows": 42, "cols": 132},
            headers=headers,
        )
        sent = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": base64.b64encode(b"exit\n").decode()},
            headers=headers,
        )
        assert resized.status_code == 200
        assert resized.json()["rows"] == 42
        assert resized.json()["cols"] == 132
        assert sent.status_code == 200
        assert backend.processes[0].inputs == [b"exit\n"]
        assert backend.processes[0].sizes == [(30, 120), (42, 132)]

        await _wait_for_last_seq(client, headers, terminal_id, 3)
        stream = await client.get(
            f"/workspaces/ajob_test/terminals/{terminal_id}/stream",
            params={"after": 1},
            headers=headers,
        )

    assert stream.status_code == 200
    assert "id: 1\n" not in stream.text
    assert 'id: 2\nevent: output\ndata: {"seq":2,"data":"YnllDQo="}\n\n' in stream.text
    assert 'id: 3\nevent: exit\ndata: {"seq":3,"exit_code":0}\n\n' in stream.text


@pytest.mark.asyncio
async def test_broker_suspends_and_resumes_terminal_process_trees(tmp_path):
    """Protected workspace phases freeze retained PTYs without closing them."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers=headers,
        )
        terminal_id = created.json()["id"]
        suspended = await client.post(
            "/workspaces/ajob_test/terminals/suspend",
            json={"lease_generation": 1},
            headers=headers,
        )
        blocked_input = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": "eA=="},
            headers=headers,
        )
        blocked_resize = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/resize",
            json={"rows": 42, "cols": 132},
            headers=headers,
        )
        blocked_create = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers=headers,
        )
        resumed = await client.post(
            "/workspaces/ajob_test/terminals/resume",
            json={"lease_generation": 1},
            headers=headers,
        )
        accepted_input = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": "eA=="},
            headers=headers,
        )
        suspended_by_retry = await client.post(
            "/workspaces/ajob_test/terminals/suspend",
            json={"lease_generation": 2},
            headers=headers,
        )
        stale_resume = await client.post(
            "/workspaces/ajob_test/terminals/resume",
            json={"lease_generation": 1},
            headers=headers,
        )
        current_resume = await client.post(
            "/workspaces/ajob_test/terminals/resume",
            json={"lease_generation": 2},
            headers=headers,
        )
        await client.post(
            "/workspaces/ajob_test/terminals/suspend",
            json={"lease_generation": 3},
            headers=headers,
        )
        settled_resume = await client.post(
            "/workspaces/ajob_test/terminals/resume-settled",
            headers=headers,
        )
        stale_suspend = await client.post(
            "/workspaces/ajob_test/terminals/suspend",
            json={"lease_generation": 3},
            headers=headers,
        )
        settled_input = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": "eQ=="},
            headers=headers,
        )
        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)

    assert suspended.json() == {"ok": True, "suspended": 1}
    assert blocked_input.status_code == 409
    assert blocked_resize.status_code == 409
    assert blocked_create.status_code == 409
    assert resumed.json() == {"ok": True, "resumed": 1}
    assert accepted_input.status_code == 200
    assert suspended_by_retry.status_code == 200
    assert stale_resume.status_code == 409
    assert current_resume.status_code == 200
    assert settled_resume.json() == {"ok": True, "resumed": 1}
    assert stale_suspend.status_code == 409
    assert settled_input.status_code == 200
    assert backend.processes[0].suspensions == [True, False, True, False, True, False]
    assert backend.processes[0].inputs == [b"x", b"y"]
    assert listing.json()["terminals"][0]["id"] == terminal_id


@pytest.mark.asyncio
async def test_partial_terminal_resume_keeps_the_workspace_control_fence_closed(tmp_path):
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        first = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        suspended = await client.post(
            "/workspaces/ajob_test/terminals/suspend",
            json={"lease_generation": 1},
            headers=headers,
        )

        def fail_resume() -> None:
            raise SandboxError("resume failed")

        backend.processes[1].resume = fail_resume  # type: ignore[method-assign]
        partial_resume = await client.post(
            "/workspaces/ajob_test/terminals/resume",
            json={"lease_generation": 1},
            headers=headers,
        )
        leaked_input = await client.post(
            f"/workspaces/ajob_test/terminals/{first.json()['id']}/input",
            json={"data": "eA=="},
            headers=headers,
        )
        leaked_resize = await client.post(
            f"/workspaces/ajob_test/terminals/{first.json()['id']}/resize",
            json={"rows": 42, "cols": 132},
            headers=headers,
        )

    assert suspended.status_code == 200
    assert partial_resume.status_code == 503
    assert leaked_input.status_code == 409
    assert leaked_resize.status_code == 409
    assert backend.processes[0].inputs == []


@pytest.mark.asyncio
async def test_broker_terminal_kill_is_idempotent_and_workspace_bound(tmp_path):
    """An opaque id cannot operate another workspace and delete never leaks it."""
    root, _repo = _workspace(tmp_path)
    other = root / "ajob_other"
    other.mkdir()
    _git(other, "init", "--quiet")
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers=headers,
        )
        terminal_id = created.json()["id"]
        cross_workspace = await client.post(
            f"/workspaces/ajob_other/terminals/{terminal_id}/input",
            json={"data": "eA=="},
            headers=headers,
        )
        cross_delete = await client.delete(
            f"/workspaces/ajob_other/terminals/{terminal_id}", headers=headers
        )
        still_live = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        killed = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )
        killed_again = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )
        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)

    assert cross_workspace.status_code == 404
    assert cross_delete.status_code == 200
    assert cross_delete.json()["state"] == "closed"
    assert still_live.json()["terminals"][0]["id"] == terminal_id
    assert killed.status_code == 200
    assert killed.json()["state"] == "closed"
    assert backend.processes[0].killed is True
    assert killed_again.status_code == 200
    assert killed_again.json()["state"] == "closed"
    assert killed_again.json()["last_seq"] == 0
    assert listing.json() == {"terminals": []}


@pytest.mark.asyncio
async def test_broker_retains_terminal_when_kill_cleanup_needs_retry(tmp_path):
    """A transient cleanup failure keeps the opaque id available for retry."""

    class _RetryProcess(_FakeTerminalProcess):
        def __init__(self) -> None:
            super().__init__()
            self.kill_attempts = 0

        def kill(self) -> None:
            self.kill_attempts += 1
            if self.kill_attempts == 1:
                raise SandboxError("daemon unavailable")
            super().kill()

    class _RetryBackend(_FakeTerminalBackend):
        def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
            process = _RetryProcess()
            process.sizes.append((rows, cols))
            self.specs.append(spec)
            self.processes.append(process)
            return process

    root, _repo = _workspace(tmp_path)
    backend = _RetryBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        terminal_id = created.json()["id"]
        failed = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )
        retained = app.state.terminal_manager._sessions["ajob_test"][terminal_id]
        retained_state = retained.descriptor()["state"]
        retried = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )

    assert failed.status_code == 503
    assert "retry kill" in failed.json()["detail"]
    assert retained_state == "cleanup_failed"
    assert backend.processes[0].kill_attempts == 2
    assert retried.status_code == 200
    assert retried.json()["state"] == "closed"
    assert app.state.terminal_manager._sessions == {}


def test_terminal_output_limit_stops_a_runaway_process(monkeypatch, tmp_path):
    """A noisy command cannot stream unbounded bytes through the trusted broker."""
    monkeypatch.setattr("cloud_agent_host.workspace_broker.MAX_TERMINAL_TOTAL_OUTPUT_BYTES", 10)
    process = _FakeTerminalProcess()
    process._output.put(b"overflow")
    session = _TerminalSession(
        terminal_id="term_test",
        process=process,
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )

    session.start()
    session._pump_thread.join(timeout=2)
    events = session.events_after(0, wait_s=0)

    assert process.killed is True
    assert session.descriptor()["state"] == "exited"
    assert any(b"output limit exceeded" in event.data for event in events)
    assert events[-1].kind == "exit"


def test_terminal_stream_viewers_are_bounded_per_session(tmp_path):
    """Duplicate SSE viewers cannot create unbounded broker waiters."""
    process = _FakeTerminalProcess()
    session = _TerminalSession(
        terminal_id="term_test",
        process=process,
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )
    session.start()

    for _ in range(4):
        session.open_stream()
    with pytest.raises(HTTPException) as excinfo:
        session.open_stream()
    session.close_stream()
    session.open_stream()

    assert excinfo.value.status_code == 429
    session.close()


def test_retained_terminal_idle_guard_cannot_outlive_the_chat_clock(tmp_path):
    """A retained PTY uses the earlier terminal/chat idle boundary."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=100,
        idle_ttl_s=10,
    )

    assert session.expired(now=109, idle_ttl_s=30, hard_ttl_s=60) is False
    assert session.expired(now=110, idle_ttl_s=30, hard_ttl_s=60) is True


@pytest.mark.asyncio
async def test_passive_terminal_polling_does_not_refresh_idle_activity(tmp_path):
    """An open SSE viewer cannot keep an otherwise idle retained chat alive."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=100,
        idle_ttl_s=10,
    )

    assert session.events_after(0, wait_s=0) == []
    assert await session.events_after_async(0, wait_s=0) == []
    assert session.last_active_at == 100
    assert session.expired(now=110, idle_ttl_s=30, hard_ttl_s=60) is True


@pytest.mark.asyncio
async def test_terminal_output_wakes_every_async_viewer(tmp_path):
    """One viewer cannot clear another viewer's output notification."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )
    first = asyncio.create_task(session.events_after_async(0, wait_s=1))
    second = asyncio.create_task(session.events_after_async(0, wait_s=1))
    await asyncio.sleep(0)

    session._append_output(b"ready")
    first_events, second_events = await asyncio.gather(first, second)

    assert [event.data for event in first_events] == [b"ready"]
    assert [event.data for event in second_events] == [b"ready"]


@pytest.mark.asyncio
async def test_broker_terminal_auth_validation_input_bound_and_session_limit(tmp_path):
    """Broker auth and all request/resource bounds fail before unsafe work."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        unauthenticated = await client.post(
            "/workspaces/ajob_test/terminals", json={"rows": 24, "cols": 80}
        )
        bad_rows = await client.post(
            "/workspaces/ajob_test/terminals",
            json={"rows": 1, "cols": 80},
            headers=headers,
        )
        bad_cols = await client.post(
            "/workspaces/ajob_test/terminals",
            json={"rows": 24, "cols": 501},
            headers=headers,
        )
        created = [
            await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
            for _ in range(4)
        ]
        over_limit = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        terminal_id = created[0].json()["id"]
        invalid_base64 = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": "not base64!"},
            headers=headers,
        )
        too_large = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": base64.b64encode(b"x" * (64 * 1024 + 1)).decode()},
            headers=headers,
        )
        negative_resume = await client.get(
            f"/workspaces/ajob_test/terminals/{terminal_id}/stream",
            params={"after": -1},
            headers=headers,
        )
        for response in created:
            await client.delete(
                f"/workspaces/ajob_test/terminals/{response.json()['id']}",
                headers=headers,
            )

    assert unauthenticated.status_code == 401
    assert bad_rows.status_code == 422
    assert bad_cols.status_code == 422
    assert all(response.status_code == 200 for response in created)
    assert over_limit.status_code == 409
    assert invalid_base64.status_code == 400
    assert too_large.status_code == 413
    assert negative_resume.status_code == 422


@pytest.mark.asyncio
async def test_broker_terminal_global_limit_bounds_all_workspaces(tmp_path):
    """Many jobs cannot collectively exhaust the broker host with PTYs."""
    root, _repo = _workspace(tmp_path)
    other = root / "ajob_other"
    other.mkdir()
    _git(other, "init", "--quiet")
    backend = _FakeTerminalBackend()
    app = create_app(
        backend=backend,
        workdir_root=str(root),
        token="broker-secret",
        terminal_max_sessions=2,
    )
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        first = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        second = await client.post("/workspaces/ajob_other/terminals", json={}, headers=headers)
        rejected = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        await client.delete(
            f"/workspaces/ajob_test/terminals/{first.json()['id']}", headers=headers
        )
        replacement = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        await client.delete(
            f"/workspaces/ajob_other/terminals/{second.json()['id']}", headers=headers
        )
        await client.delete(
            f"/workspaces/ajob_test/terminals/{replacement.json()['id']}", headers=headers
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert rejected.status_code == 503
    assert replacement.status_code == 200


@pytest.mark.asyncio
async def test_broker_terminal_idle_ttl_and_shutdown_cleanup_kill_processes(tmp_path):
    """Forgotten sessions are killed on TTL and broker shutdown."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(
        backend=backend,
        workdir_root=str(root),
        token="broker-secret",
        terminal_idle_ttl_s=0.1,
        terminal_hard_ttl_s=60,
    )
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        first = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        await _wait_for_last_seq(client, headers, first.json()["id"], 1)
        await asyncio.sleep(0.12)
        expired = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        second = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)

    assert expired.json() == {"terminals": []}
    assert backend.processes[0].killed is True
    assert second.status_code == 200
    app.state.terminal_manager.close()
    assert backend.processes[1].killed is True


@pytest.mark.asyncio
async def test_broker_lifespan_runs_terminal_cleanup_only_for_its_workdir_root(tmp_path):
    """Broker startup, rather than ordinary runner preflight, owns crash cleanup."""
    root, _repo = _workspace(tmp_path)
    backend = _RecordingTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")

    async with app.router.lifespan_context(app):
        assert backend.prepared_roots == [str(root)]


@pytest.mark.asyncio
async def test_failed_retained_reconciliation_does_not_start_terminal_reaper(tmp_path):
    """A failed ASGI startup must not leak an unowned terminal thread."""
    root, _repo = _workspace(tmp_path)
    backend = _RecordingTerminalBackend()

    class FailingSupervisor:
        idle_timeout_s = 30.0

        def __init__(self) -> None:
            self.closed = False

        def start(self) -> None:
            raise SandboxError("retained Docker cleanup failed")

        def close(self) -> None:
            self.closed = True

    supervisor = FailingSupervisor()
    app = create_app(
        backend=backend,
        workdir_root=str(root),
        token="broker-secret",
        session_supervisor=supervisor,  # type: ignore[arg-type]
    )
    terminal_started = False

    def record_terminal_start() -> None:
        nonlocal terminal_started
        terminal_started = True

    app.state.terminal_manager.start = record_terminal_start

    with pytest.raises(SandboxError, match="retained Docker cleanup failed"):
        async with app.router.lifespan_context(app):
            raise AssertionError("failed reconciliation entered the serving lifespan")

    assert terminal_started is False
    assert supervisor.closed is False


@pytest.mark.asyncio
async def test_broker_explains_that_process_backend_cannot_host_persistent_terminals(tmp_path):
    """Unsafe local PTYs are rejected with an actionable owner-facing response."""
    root, _repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        response = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Interactive terminals require the container or kata sandbox backend."
    }


def test_terminal_ring_limits_many_one_byte_events_and_signals_cursor_reset(tmp_path):
    """An event-count eviction is explicit and the retained cursor remains resumable."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
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
    assert events[1].kind == "output"
    assert events[1].seq == events[0].seq + 1
    assert events[-1].seq == MAX_TERMINAL_BUFFER_EVENTS + 1
    assert _terminal_sse_frame(events[0]) == (
        f"id: {events[0].seq}\nevent: reset\n"
        f'data: {{"seq":{events[0].seq},"reason":"output_truncated"}}\n\n'
    )

    resumed = session.events_after(events[0].seq, wait_s=0)
    assert resumed[0].kind == "output"
    assert all(event.kind != "reset" for event in resumed)


def test_terminal_ring_marks_a_single_oversized_output_before_its_retained_tail(tmp_path):
    """Trimming inside one backend chunk also emits a separately resumable reset."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )

    session._append_output(b"a" + b"b" * MAX_TERMINAL_BUFFER_BYTES)

    events = session.events_after(0, wait_s=0)
    assert [(event.seq, event.kind) for event in events] == [(1, "reset"), (2, "output")]
    assert events[0].reason == "output_truncated"
    assert events[1].data == b"b" * MAX_TERMINAL_BUFFER_BYTES
    assert session.events_after(1, wait_s=0) == [events[1]]


class _FakeSessionSupervisor:
    """Record the broker's private restartable-sandbox RPC contract."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.idle_timeout_s = 30.0
        self.workspace_backend = _FakeTerminalBackend()
        self.workspace_backend.manager = SimpleNamespace(
            require_network_phase=lambda spec, phase: self.calls.append(("network", spec, phase))
        )
        self.workspace_backend.spec = SimpleNamespace(thread_id="athr_1")

    def acquire(
        self,
        spec,
        *,
        owner: str,
        has_settled_context: bool,
        activation_generation: int,
        live_process_proven: bool,
        expected_container_id: str | None,
    ):
        self.calls.append(
            (
                "acquire",
                spec,
                owner,
                has_settled_context,
                activation_generation,
                live_process_proven,
                expected_container_id,
            )
        )
        return SessionLease(
            thread_id=spec.thread_id,
            sandbox_id=spec.sandbox_id,
            container_id="container-one",
            container_name=spec.container_name,
            token="lease-token",
            activation_generation=2,
            context_status="restored" if has_settled_context else "new",
        )

    def note_process_started(self, thread_id: str, token: str):
        self.calls.append(("process", thread_id, token))
        return self._descriptor(thread_id, process_generation=4)

    def settle(self, thread_id: str, token: str, *, job_id: str, context_status: str):
        self.calls.append(("settle", thread_id, token, job_id, context_status))
        return self._descriptor(
            thread_id,
            context_status=context_status,
            last_settled_job_id=job_id,
        )

    def abandon(self, thread_id: str, token: str, *, lost: bool):
        self.calls.append(("abandon", thread_id, token, lost))
        return self._descriptor(thread_id, context_status="lost" if lost else "new")

    def status(self, thread_id: str):
        self.calls.append(("status", thread_id))
        return self._descriptor(thread_id)

    def reconcile(self):
        self.calls.append(("reconcile",))
        return [self._descriptor("athr_1")]

    def stop_if_idle(
        self,
        thread_id: str,
        *,
        generation: int,
        activation_generation: int,
        reason: str = "idle",
    ):
        self.calls.append(("stop_if_idle", thread_id, generation, activation_generation, reason))
        descriptor = self._descriptor(thread_id)
        return replace(
            descriptor,
            docker_status="stopped",
            running=False,
            activation_generation=activation_generation,
            idle_deadline_monotonic=None,
        )

    def operation_opened(self, thread_id: str):
        self.calls.append(("operation_opened", thread_id))
        return "operation-token", self._descriptor(thread_id)

    def operation_closed(self, thread_id: str, token: str):
        self.calls.append(("operation_closed", thread_id, token))
        return self._descriptor(thread_id)

    def suspend_workspace(self, thread_id: str):
        self.calls.append(("suspend_workspace", thread_id))
        return self._descriptor(thread_id)

    def wait_workspace_operations(self, thread_id: str):
        self.calls.append(("wait_workspace_operations", thread_id))
        return self._descriptor(thread_id)

    def resume_workspace(self, thread_id: str):
        self.calls.append(("resume_workspace", thread_id))
        return self._descriptor(thread_id)

    def terminal_opened(self, thread_id: str):
        self.calls.append(("terminal_opened", thread_id))
        return "terminal-token", self._descriptor(thread_id)

    def terminal_closed(
        self,
        thread_id: str,
        token: str,
        *,
        last_activity_monotonic: float | None = None,
    ):
        self.calls.append(("terminal_closed", thread_id, token, last_activity_monotonic))
        return self._descriptor(thread_id)

    def backend(self, thread_id: str):
        self.calls.append(("backend", thread_id))
        return self.workspace_backend

    @staticmethod
    def _descriptor(
        thread_id: str,
        *,
        process_generation: int = 3,
        context_status: str = "live",
        last_settled_job_id: str | None = "ajob_1",
    ) -> SessionDescriptor:
        return SessionDescriptor(
            thread_id=thread_id,
            sandbox_id="asbx_1",
            generation=1,
            container_id="container-one",
            container_name="freeinference-chat-one",
            docker_status="running",
            running=True,
            busy=False,
            terminal_count=0,
            operation_count=0,
            workspace_suspended=False,
            activation_generation=2,
            process_generation=process_generation,
            last_settled_job_id=last_settled_job_id,
            idle_deadline_monotonic=1900.0,
            context_status=context_status,
        )


class _FakeSharedSessionSupervisor:
    """Record shared snapshot/import RPCs without requiring Docker."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.imports: list[tuple[bytes, str, dict[str, str]]] = []
        self.manager = SimpleNamespace(import_base_bundle=self._import_base_bundle)
        self.workspace_backend = SimpleNamespace(
            manager=self.manager,
            spec=SimpleNamespace(sandbox_id="asbx_shared"),
        )

    def register_manifest(self, spec, members, *, expected_container_id=None):
        self.calls.append(("manifest", spec, members, expected_container_id))
        return []

    def capture_snapshot(self, thread_id: str, *, capture):
        self.calls.append(("snapshot", thread_id))
        return {"patch": "diff --git a/a.txt b/a.txt\n"}

    def operation_opened(self, thread_id: str):
        self.calls.append(("operation_opened", thread_id))
        return "shared-operation", None

    def backend(self, thread_id: str):
        self.calls.append(("backend", thread_id))
        return self.workspace_backend

    def operation_closed(self, thread_id: str, token: str):
        self.calls.append(("operation_closed", thread_id, token))
        return None

    def _import_base_bundle(self, spec, bundle: bytes, *, commit: str, env):
        self.imports.append((bundle, commit, env))


def _shared_snapshot_identity() -> dict[str, object]:
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


@pytest.mark.asyncio
async def test_broker_exposes_authenticated_restartable_sandbox_lifecycle(tmp_path):
    root, _repo = _workspace(tmp_path)
    supervisor = _FakeSessionSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        session_supervisor=supervisor,
    )
    transport = ASGITransport(app=app)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        unauthenticated = await client.get("/sandboxes/athr_1")
        acquired = await client.post(
            "/sandboxes/athr_1/acquire",
            headers=headers,
            json={
                "sandbox_id": "asbx_1",
                "generation": 1,
                "activation_generation": 2,
                "runtime_id": "claude-code",
                "runtime_version": "2.1.220",
                "owner": "attempt:9:generation:2",
                "has_settled_context": True,
                "expected_container_id": "container-one",
            },
        )
        process = await client.post(
            "/sandboxes/athr_1/process-started",
            headers=headers,
            json={"token": "lease-token"},
        )
        settled = await client.post(
            "/sandboxes/athr_1/settle",
            headers=headers,
            json={
                "token": "lease-token",
                "job_id": "ajob_2",
                "context_status": "restored",
            },
        )
        status = await client.get("/sandboxes/athr_1", headers=headers)
        observed = await client.get("/sandboxes", headers=headers)
        stopped = await client.post(
            "/sandboxes/athr_1/stop-idle",
            headers=headers,
            json={"generation": 1, "activation_generation": 2},
        )

    assert unauthenticated.status_code == 401
    assert acquired.status_code == 200
    assert acquired.json()["context_status"] == "restored"
    assert acquired.json()["token"] == "lease-token"
    assert process.json()["process_generation"] == 4
    assert settled.json()["last_settled_job_id"] == "ajob_2"
    assert status.json()["container_id"] == "container-one"
    assert observed.json()["sandboxes"] == [
        {
            "thread_id": "athr_1",
            "sandbox_id": "asbx_1",
            "generation": 1,
            "container_id": "container-one",
            "container_name": "freeinference-chat-one",
            "docker_status": "running",
            "running": True,
            "busy": False,
            "terminal_count": 0,
            "operation_count": 0,
            "workspace_suspended": False,
            "activation_generation": 2,
            "process_generation": 3,
            "last_settled_job_id": "ajob_1",
            "idle_deadline_monotonic": 1900.0,
            "context_status": "live",
        }
    ]
    assert stopped.json()["running"] is False
    assert [call[0] for call in supervisor.calls] == [
        "acquire",
        "process",
        "settle",
        "status",
        "reconcile",
        "stop_if_idle",
    ]


@pytest.mark.asyncio
async def test_broker_accepts_a_generation_zero_pre_activation_idle_stop(tmp_path):
    root, _repo = _workspace(tmp_path)
    supervisor = _FakeSessionSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        session_supervisor=supervisor,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/sandboxes/athr_fork/stop-idle",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json={"generation": 1, "activation_generation": 0},
        )

    assert response.status_code == 200, response.text
    assert response.json()["activation_generation"] == 0
    assert supervisor.calls == [("stop_if_idle", "athr_fork", 1, 0, "idle")]


@pytest.mark.asyncio
async def test_broker_rehydrates_shared_child_aliases_without_starting_the_group(tmp_path):
    root, _repo = _workspace(tmp_path)
    supervisor = _FakeSharedSessionSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        shared_session_supervisor=supervisor,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/register",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json=_shared_snapshot_identity(),
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "sandbox_id": "asbx_shared", "member_count": 0}
    assert [call[0] for call in supervisor.calls] == ["manifest"]


@pytest.mark.asyncio
async def test_shared_turn_reports_activation_boundary_failure_before_result(tmp_path):
    """A failed process/file boundary cannot be hidden behind a success frame."""

    class Turn:
        @staticmethod
        def lines():
            return iter(())

        @staticmethod
        def wait():
            return SimpleNamespace(
                status="completed",
                final_output="done",
                exit_code=0,
                usage=None,
                raw=None,
            )

        @staticmethod
        def stderr_text():
            return ""

    class BoundaryFailureSupervisor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def start_turn(self, *_args, **_kwargs):
            self.calls.append("start")
            return Turn()

        def complete_turn(self, *_args, **_kwargs):
            self.calls.append("complete")
            raise SandboxError("member permission normalization failed")

        def abort_turn(self, *_args, **_kwargs):
            self.calls.append("abort")

    root, _repo = _workspace(tmp_path)
    supervisor = BoundaryFailureSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        shared_session_supervisor=supervisor,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json={
                "token": "lease-token",
                "prompt": "test",
                "model": "model",
                "reasoning_effort": None,
            },
        )

    frames = [json.loads(line) for line in response.text.splitlines() if line]
    assert response.status_code == 200
    assert frames[0]["kind"] == "error"
    assert frames[0]["detail"] == "member permission normalization failed"
    assert frames[0]["resources"]["process_observation_complete"] is False
    assert frames[0]["resources"]["tool_calls"] == []
    assert supervisor.calls == ["start", "complete", "abort"]


@pytest.mark.asyncio
async def test_shared_stream_error_still_returns_closed_guest_telemetry(tmp_path):
    """An exception cannot turn a captured short process into fallback zero."""

    class Turn:
        @staticmethod
        def lines():
            yield SimpleNamespace(
                event_type="tool_use",
                payload={"id": "toolu_failed", "name": "Bash"},
                raw_line='{"type":"tool_use"}',
            )
            raise RuntimeError("stream exploded")

        @staticmethod
        def stderr_text():
            return ""

    process = GuestProcess(
        process_seq=1,
        uid=11001,
        pid=700,
        tgid=700,
        ppid=41,
        comm="tiny",
        executable="/bin/tiny",
        started_guest_mono_ns=10,
        exited_guest_mono_ns=20,
        started_host_mono=1e20,
        exited_host_mono=1e20,
        exit_code=1,
        observed_fork=True,
        observed_exec=True,
        observed_exit=True,
    )
    telemetry = SharedTurnTelemetry(
        observation=GuestObservation(
            source="kata-guest-ebpf-v1",
            complete=True,
            lost_events=0,
            truncated=False,
            processes=(process,),
            origin_guest_mono_ns=0,
        )
    )

    class Supervisor:
        @staticmethod
        def start_turn(*_args, **_kwargs):
            return Turn()

        @staticmethod
        def abort_turn(*_args, **_kwargs):
            return telemetry

    root, _repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        shared_session_supervisor=Supervisor(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json={
                "token": "lease-token",
                "prompt": "test",
                "model": "model",
                "reasoning_effort": None,
            },
        )

    frames = [json.loads(line) for line in response.text.splitlines() if line]
    error = next(frame for frame in frames if frame["kind"] == "error")
    assert error["detail"] == "stream exploded"
    assert error["resources"]["process_observation_complete"] is True
    assert error["resources"]["processes"][0]["pid"] == 700


@pytest.mark.asyncio
async def test_shared_turn_returns_uid_filtered_guest_processes_without_sampler_reads(tmp_path):
    """A sub-tick process reaches the runner in the result frame as an event row."""

    class Turn:
        @staticmethod
        def lines():
            return iter(
                [
                    SimpleNamespace(
                        event_type="tool_use",
                        payload={"id": "toolu_tiny", "name": "Bash"},
                        raw_line='{"type":"tool_use"}',
                    )
                ]
            )

        @staticmethod
        def wait():
            return SimpleNamespace(
                status="completed",
                final_output="done",
                exit_code=0,
                usage=None,
                raw=None,
            )

        @staticmethod
        def stderr_text():
            return ""

    class ObserverSupervisor:
        @staticmethod
        def start_turn(*_args, **_kwargs):
            return Turn()

        @staticmethod
        def complete_turn(*_args, **_kwargs):
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
                # The call remains open at turn end, so any later process start
                # is conservatively attributable without patching the clock.
                started_host_mono=1e20,
                exited_host_mono=1e20,
                exit_code=0,
                observed_fork=True,
                observed_exec=True,
                observed_exit=True,
            )
            return GuestObservation(
                source="kata-guest-ebpf-v1",
                complete=True,
                lost_events=0,
                truncated=False,
                processes=(process,),
                origin_guest_mono_ns=1_000_000_000,
            )

        @staticmethod
        def abort_turn(*_args, **_kwargs):
            raise AssertionError("completed turn was aborted")

    root, _repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        shared_session_supervisor=ObserverSupervisor(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json={
                "token": "lease-token",
                "prompt": "test",
                "model": "model",
                "reasoning_effort": None,
            },
        )

    frames = [json.loads(line) for line in response.text.splitlines() if line]
    result = next(frame for frame in frames if frame["kind"] == "result")
    resources = result["resources"]
    assert resources["sample_count"] == 0
    assert resources["coverage"] == 0.0
    assert resources["process_observation_complete"] is True
    assert resources["process_events_lost"] == 0
    assert resources["tool_calls"][0]["process_count"] == 1
    assert resources["processes"] == [
        {
            "process_seq": 1,
            "uid": 11001,
            "pid": 412,
            "tgid": 412,
            "ppid": 41,
            "comm": "tiny",
            "executable": "/bin/tiny",
            "started_offset_ms": 1,
            "duration_ms": 1,
            "exit_code": 0,
            "observed_fork": True,
            "observed_exec": True,
            "observed_exit": True,
            "tool_call_seqs": [1],
            "cpu_usec": None,
            "mem_peak_bytes": None,
            "io_read_bytes": None,
            "io_write_bytes": None,
            "io_rchar_bytes": None,
            "io_wchar_bytes": None,
        }
    ]


@pytest.mark.asyncio
async def test_shared_turn_applies_exact_guest_tool_marks(tmp_path):
    """Shared-live must consume hook marks, not leave every call message-timed."""
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
                status="completed",
                final_output="done",
                exit_code=0,
                usage=None,
                raw=None,
            )

        @staticmethod
        def stderr_text():
            return ""

    class Supervisor:
        @staticmethod
        def start_turn(*_args, **_kwargs):
            return Turn()

        @staticmethod
        def complete_turn(*_args, **_kwargs):
            return SharedTurnTelemetry(
                observation=GuestObservation.unavailable("not Kata in this fixture"),
                tool_marks=(
                    (marks["pre"], "pre", "toolu_exact"),
                    (marks["post"], "post", "toolu_exact"),
                ),
            )

        @staticmethod
        def abort_turn(*_args, **_kwargs):
            raise AssertionError("completed turn was aborted")

    root, _repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        shared_session_supervisor=Supervisor(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/turn",
            headers={"X-Agent-Workspace-Token": "broker-secret"},
            json={
                "token": "lease-token",
                "prompt": "test",
                "model": "model",
                "reasoning_effort": None,
            },
        )

    frames = [json.loads(line) for line in response.text.splitlines() if line]
    result = next(frame for frame in frames if frame["kind"] == "result")
    (tool_call,) = result["resources"]["tool_calls"]
    assert tool_call["timing_source"] == "exact"
    assert tool_call["duration_ms"] <= 2


@pytest.mark.asyncio
async def test_shared_snapshot_receipt_and_base_import_preserve_workspace_identity(tmp_path):
    root, _repo = _workspace(tmp_path)
    shared = _FakeSharedSessionSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        session_supervisor=_FakeSessionSupervisor(),
        shared_session_supervisor=shared,
    )
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    identity = _shared_snapshot_identity()
    bundle = b"trusted-git-bundle"
    commit_sha = "b" * 40

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        captured = await client.post(
            "/shared-sandboxes/athr_source/snapshot",
            headers=headers,
            json={
                **identity,
                "snapshot_id": 9,
                "workspace_epoch": 12,
                "base_sha": "a" * 40,
            },
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

    assert captured.status_code == 200, captured.text
    patch = "diff --git a/a.txt b/a.txt\n"
    assert captured.json() == {
        "snapshot_id": 9,
        "sandbox_id": "asbx_shared",
        "sandbox_generation": 3,
        "container_id": "container-shared",
        "workspace_epoch": 12,
        "base_sha": "a" * 40,
        "patch": patch,
        "content_sha256": hashlib.sha256(patch.encode()).hexdigest(),
    }
    assert imported.status_code == 200, imported.text
    assert imported.json()["workspace_head_preserved"] is True
    assert shared.imports == [
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
    assert shared.calls[-3:] == [
        ("operation_opened", "athr_source"),
        ("backend", "athr_source"),
        ("operation_closed", "athr_source", "shared-operation"),
    ]


@pytest.mark.asyncio
async def test_retained_workspace_suspension_wraps_terminal_and_file_barriers(tmp_path):
    root, _repo = _workspace(tmp_path)
    supervisor = _FakeSessionSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        session_supervisor=supervisor,
    )
    transport = ASGITransport(app=app)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        suspended = await client.post(
            "/workspaces/athr_1/terminals/suspend",
            headers=headers,
            json={"lease_generation": 9},
        )
        resumed = await client.post(
            "/workspaces/athr_1/terminals/resume",
            headers=headers,
            json={"lease_generation": 9},
        )

    assert suspended.status_code == 200, suspended.text
    assert resumed.status_code == 200, resumed.text
    assert [call[0] for call in supervisor.calls] == [
        "suspend_workspace",
        "wait_workspace_operations",
        "resume_workspace",
    ]


@pytest.mark.asyncio
async def test_broker_keeps_legacy_routes_and_reports_restartable_mode_disabled(tmp_path):
    root, _repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        restartable_enabled=False,
    )
    transport = ASGITransport(app=app)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        legacy = await client.get("/workspaces/ajob_test/files", headers=headers)
        retained = await client.get("/sandboxes/athr_1", headers=headers)

    assert legacy.status_code == 200
    assert retained.status_code == 409
    assert retained.json() == {"detail": "restartable chat sandboxes are not enabled on this host"}


@pytest.mark.asyncio
async def test_retained_file_routes_use_container_operation_not_a_host_directory(
    tmp_path, monkeypatch
):
    root, _repo = _workspace(tmp_path)
    supervisor = _FakeSessionSupervisor()
    reads: list[tuple[str, str]] = []
    writes: list[tuple[str, str, str]] = []

    def fake_read(backend, relative, **_limits):
        reads.append((backend.spec.thread_id, relative))
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

    def fake_write(backend, relative, content, **_limits):
        writes.append((backend.spec.thread_id, relative, content))
        return {**fake_read(backend, relative), "content": content, "size": len(content)}

    monkeypatch.setattr("cloud_agent_host.workspace_broker.read_retained_file", fake_read)
    monkeypatch.setattr("cloud_agent_host.workspace_broker.write_retained_file", fake_write)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        session_supervisor=supervisor,
    )
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        read = await client.get("/workspaces/athr_1/files?path=README.md", headers=headers)
        written = await client.put(
            "/workspaces/athr_1/files?path=README.md",
            headers=headers,
            json={"content": "after\n"},
        )

    assert read.status_code == 200
    assert written.status_code == 200
    assert reads == [("athr_1", "README.md"), ("athr_1", "README.md")]
    assert writes == [("athr_1", "README.md", "after\n")]
    assert not (root / "athr_1").exists()
    assert [call[0] for call in supervisor.calls] == [
        "operation_opened",
        "backend",
        "network",
        "operation_closed",
        "operation_opened",
        "backend",
        "network",
        "operation_closed",
    ]


@pytest.mark.asyncio
async def test_retained_terminal_execs_in_same_chat_and_releases_idle_block(tmp_path):
    root, _repo = _workspace(tmp_path)
    supervisor = _FakeSessionSupervisor()
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
        session_supervisor=supervisor,
    )
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://broker") as client:
        created = await client.post(
            "/workspaces/athr_1/terminals",
            headers=headers,
            json={"rows": 30, "cols": 100},
        )
        terminal_id = created.json()["id"]
        deleted = await client.delete(
            f"/workspaces/athr_1/terminals/{terminal_id}", headers=headers
        )

    assert created.status_code == 200
    assert deleted.status_code == 200
    assert supervisor.workspace_backend.specs[0].workdir == "/workspace"
    assert not (root / "athr_1").exists()
    assert ("terminal_opened", "athr_1") in supervisor.calls
    terminal_close = next(call for call in supervisor.calls if call[0] == "terminal_closed")
    assert terminal_close[:3] == ("terminal_closed", "athr_1", "terminal-token")
    assert isinstance(terminal_close[3], float)
