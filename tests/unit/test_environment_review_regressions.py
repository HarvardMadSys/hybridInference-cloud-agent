"""Review regressions: credentials, snapshot membership, execution receipts and scheduling."""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest
from tests.unit.test_agent_sandbox_manager import (
    _SlowSpawner,
    _Spawner,
    _TerminalProcess,
    _wait_for,
)

from cloud_agent.sandbox_manager_client import SandboxManagerClient, SandboxManagerError
from cloud_agent_host import environment_tests
from cloud_agent_host.action_receipts import ActionReceipts
from cloud_agent_host.environment_snapshot import check_snapshot_secrets
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_manager import _TerminalManager
from cloud_agent_shared.environment import (
    BoundEnvironment,
    EnvironmentAction,
    EnvironmentInputError,
    EnvironmentVariable,
    VariableEdit,
    merge_variables,
    parse_env,
)
from cloud_agent_shared.secret_redaction import SecretRedactor


def test_short_secrets_require_explicit_non_sensitive_classification():
    imported = parse_env("DEBUG=1\nTOKEN=example-private-token")
    assert all(v.secret for v in imported)
    with pytest.raises(EnvironmentInputError, match="at least 8"):
        merge_variables(imported)
    with pytest.raises(ValueError, match="at least 8"):
        VariableEdit(name="DEBUG", value="1")
    reviewed = merge_variables(
        parse_env("DEBUG=1\nTOKEN=example-private-token", ordinary_names=["DEBUG"])
    )
    bound = BoundEnvironment(binding_id="b", variables=reviewed)
    assert bound.values("runtime")["DEBUG"] == "1"
    assert (
        SecretRedactor(bound.secrets()).text("1 test passed; example-private-token")
        == "1 test passed; [secret:TOKEN]"
    )
    with pytest.raises(EnvironmentInputError, match="this import"):
        parse_env("DEBUG=1", ordinary_names=["MISSING"])


def test_multiline_secret_does_not_turn_short_lines_or_pem_headers_into_global_patterns():
    delimiter = "PRIVATE " + "KEY"
    value = f"-----BEGIN {delimiter}-----\nexample-private-body\n1\n-----END {delimiter}-----"
    redactor = SecretRedactor({"KEY": value})
    ordinary = "1 test passed\n-----BEGIN PRIVATE KEY-----"
    assert redactor.text(ordinary) == ordinary
    assert not redactor.contains(ordinary.encode())
    assert redactor.text(value) == "[secret:KEY]"
    assert redactor.contains(b"example-private-body")
    for index in range(1, len(value)):
        stream = SecretRedactor({"KEY": value})
        assert (
            stream.feed(value[:index]) + stream.feed(value[index:]) + stream.flush()
            == "[secret:KEY]"
        )


@pytest.fixture
def git_workspace(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, stderr=subprocess.STDOUT)

    git("init", "-q")
    (tmp_path / "tracked").write_text("baseline\n")
    (tmp_path / ".gitignore").write_text("ignored\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()

    def capture(_spec, argv, *, env, workdir, timeout_s):
        return subprocess.run(
            [sys.executable, *argv[1:]],
            cwd=workdir,
            env={**os.environ, **env},
            capture_output=True,
            timeout=timeout_s,
        )

    backend = SimpleNamespace(spec=object(), manager=SimpleNamespace(exec_capture=capture))
    return tmp_path, git, base, backend


@pytest.mark.parametrize("kind", ["untracked", "binary", "staged", "tracked", "symlink"])
def test_snapshot_scans_every_new_secret_blob_without_changing_the_user_index(git_workspace, kind):
    root, git, base, backend = git_workspace
    secret = "example-private-credential"
    path = root / ("tracked" if kind == "tracked" else ".env.local")
    if kind == "symlink":
        path.symlink_to(secret)
    else:
        path.write_bytes((b"\0\xff" if kind == "binary" else b"TOKEN=") + secret.encode())
    if kind == "staged":
        git("add", ".env.local")
    index = (root / ".git/index").read_bytes()
    bound = BoundEnvironment(
        binding_id="b", variables=[EnvironmentVariable(name="TOKEN", value=secret)]
    )
    with pytest.raises(SandboxError, match="Secret value"):
        check_snapshot_secrets(backend, base, bound, workdir=str(root))
    assert (root / ".git/index").read_bytes() == index


def test_snapshot_allows_ordinary_flags_ignored_files_and_secret_removal(git_workspace):
    root, git, base, backend = git_workspace
    value = "example-private-credential"
    (root / "tracked").write_text(value)
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "baseline secret",
    )
    base = git("rev-parse", "HEAD").decode().strip()
    (root / "tracked").write_text("1 test passed")
    (root / "ignored").write_text(value)
    bound = BoundEnvironment(
        binding_id="b",
        variables=[
            EnvironmentVariable(name="TOKEN", value=value),
            EnvironmentVariable(name="DEBUG", value="1", secret=False),
        ],
    )
    check_snapshot_secrets(backend, base, bound, workdir=str(root))


def test_snapshot_scan_never_imports_from_the_workspace(git_workspace):
    root, _, base, _ = git_workspace
    # The change adds the project's own module, named like the standard library's.
    (root / "types.py").write_text("UserId = int\n")

    def capture(_spec, argv, *, env, workdir, timeout_s):
        # The scan exactly as the sandbox runs it, with the system interpreter.
        return subprocess.run(
            argv,
            cwd=workdir,
            env={"PATH": "/usr/bin:/bin", **env},
            capture_output=True,
            timeout=timeout_s,
        )

    backend = SimpleNamespace(spec=object(), manager=SimpleNamespace(exec_capture=capture))
    bound = BoundEnvironment(
        binding_id="b",
        variables=[EnvironmentVariable(name="TOKEN", value="example-private-credential")],
    )
    check_snapshot_secrets(backend, base, bound, workdir=str(root))


def test_completed_action_is_not_reexecuted_after_reaping_or_broker_restart(tmp_path):
    receipts = ActionReceipts(tmp_path)
    manager = _TerminalManager(
        idle_ttl_s=30, hard_ttl_s=60, max_sessions=4, action_receipts=receipts
    )
    bound = BoundEnvironment(
        binding_id="b", actions=[EnvironmentAction(id="deploy", name="Deploy", command="deploy")]
    )
    backend = _Spawner(_TerminalProcess(), _TerminalProcess())
    args = {
        "rows": 24,
        "cols": 80,
        "backend": backend,
        "environment": bound,
        "action_id": "deploy",
        "run_id": "one-run",
    }
    first = manager.create("workspace", "/workspace", **args)
    _wait_for(lambda: receipts.read("workspace", first["id"])["state"] == "exited")
    released = []
    assert (
        manager.create("workspace", "/workspace", on_closed=released.append, **args)["id"]
        == first["id"]
    )
    assert len(released) == 1, "a duplicate request must release its own admission token"
    manager.delete("workspace", first["id"])
    restarted = _TerminalManager(
        idle_ttl_s=30, hard_ttl_s=60, max_sessions=4, action_receipts=ActionReceipts(tmp_path)
    )
    assert restarted.output("workspace", first["id"])["exit_code"] == 0
    again = restarted.create("workspace", "/workspace", **args)
    assert again["state"] == "exited"
    assert len(backend.processes) == 1


def test_action_result_disk_failure_releases_activity_but_still_blocks_replay(
    tmp_path, monkeypatch
):
    receipts = ActionReceipts(tmp_path)
    released = threading.Event()

    def fail_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(receipts, "finish", fail_write)
    manager = _TerminalManager(
        idle_ttl_s=30, hard_ttl_s=60, max_sessions=4, action_receipts=receipts
    )
    bound = BoundEnvironment(
        binding_id="b", actions=[EnvironmentAction(id="deploy", name="Deploy", command="deploy")]
    )
    backend = _Spawner(_TerminalProcess(), _TerminalProcess())
    args = {
        "rows": 24,
        "cols": 80,
        "backend": backend,
        "environment": bound,
        "action_id": "deploy",
        "run_id": "one-run",
    }
    manager.create("w", "/workspace", on_closed=lambda _: released.set(), **args)
    assert released.wait(2), "a result write failure must not hold workspace activity"
    manager.close()
    restarted = _TerminalManager(
        idle_ttl_s=30, hard_ttl_s=60, max_sessions=4, action_receipts=ActionReceipts(tmp_path)
    )
    assert restarted.create("w", "/workspace", **args)["state"] == "unknown"
    assert len(backend.processes) == 1


def test_uncertain_action_receipt_never_authorizes_a_replay(tmp_path):
    receipts = ActionReceipts(tmp_path)
    assert receipts.reserve("w", "term_run", "bound", "deploy") is None
    restarted = ActionReceipts(tmp_path)
    assert restarted.reserve("w", "term_run", "bound", "deploy")["state"] == "unknown"
    with pytest.raises(SandboxError, match="identity conflicts"):
        restarted.reserve("w", "term_run", "another", "deploy")
    assert restarted.read("another-workspace", "term_run") is None


def test_stop_before_delayed_action_launch_persists_a_tombstone(tmp_path):
    receipts = ActionReceipts(tmp_path)
    receipts.stop("w", "term_run")
    assert ActionReceipts(tmp_path).reserve("w", "term_run", "bound", "deploy")["state"] == "closed"


class _RecordingSlowSpawner(_SlowSpawner):
    def __init__(self) -> None:
        super().__init__()
        self.processes: list = []

    def spawn_terminal(self, spec, *, rows: int, cols: int):
        process = super().spawn_terminal(spec, rows=rows, cols=cols)
        self.processes.append(process)
        return process


def test_stopping_an_action_while_its_terminal_spawns_stops_it(tmp_path):
    """A Stop that lands while an action's terminal spawns closes that terminal.

    Creating an action's terminal reserves its receipt and then spawns the PTY
    under the workspace's own lock only. A delete in between found no
    terminal, and its tombstone leaves a reserved receipt alone, so the
    command the owner had stopped started once its terminal was listed.
    """
    receipts = ActionReceipts(tmp_path)
    manager = _TerminalManager(
        idle_ttl_s=30, hard_ttl_s=60, max_sessions=4, action_receipts=receipts
    )
    bound = BoundEnvironment(
        binding_id="b", actions=[EnvironmentAction(id="deploy", name="Deploy", command="deploy")]
    )
    run_id = "8f2c2a54-0b8e-4e1c-9a51-3f5d2c7b9e10"
    spawner = _RecordingSlowSpawner()
    creation = threading.Thread(
        target=functools.partial(
            manager.create,
            "workspace",
            "/workspace",
            rows=24,
            cols=80,
            backend=spawner,
            environment=bound,
            action_id="deploy",
            run_id=run_id,
        ),
        daemon=True,
    )
    creation.start()
    assert spawner.spawning.wait(2)
    stopped: list[dict] = []
    stop = threading.Thread(
        target=lambda: stopped.append(manager.delete("workspace", "term_" + run_id)),
        daemon=True,
    )
    try:
        stop.start()
        stop.join(0.3)
        stopped_mid_spawn = not stop.is_alive()
    finally:
        spawner.release.set()
        creation.join(5)
        stop.join(5)

    assert not stopped_mid_spawn, "the Stop waits for the terminal it stops"
    assert [descriptor["state"] for descriptor in stopped] == ["closed"]
    [process] = spawner.processes
    assert process.killed
    assert manager.list("workspace") == []
    assert receipts.read("workspace", "term_" + run_id)["state"] == "closed"
    manager.close()


def test_environment_test_poll_does_not_block_and_can_be_cancelled(monkeypatch):
    entered = threading.Event()
    exited = threading.Event()
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        entered.set()
        kwargs["stop"].wait(2)
        exited.set()
        return True

    monkeypatch.setattr(environment_tests, "claim_and_execute", execute)
    worker = environment_tests.EnvironmentTestWorker("http://control", object())
    worker.poll("credential")
    assert entered.wait(1)
    assert worker.busy
    # This is reached before the long-running test completes; another poll does not launch twice.
    assert not exited.is_set()
    worker.poll("credential")
    assert len(calls) == 1
    worker.close()
    assert exited.is_set()
    assert not worker.busy
    worker.poll("credential")
    assert len(calls) == 1


def test_environment_orphan_poll_is_throttled(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(environment_tests.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(environment_tests, "claim_and_execute", lambda **kw: calls.append(kw))
    worker = environment_tests.EnvironmentTestWorker("http://control", object())
    for instant in (100.0, 106.0, 112.0, 161.0):
        now[0] = instant
        worker.poll("credential")
        worker._thread.join(1)
    worker.close()
    assert [call["cleanup_orphans"] for call in calls] == [True, False, False, True]


def _environment_worker(monkeypatch, tmp_path, prepare, status):
    """A worker whose first claim runs one test run against stand-ins.

    The run is handed the stop event the worker gives its claim, as the real
    claim does; ``prepare`` stands in for installation and ``status`` answers
    every report. Returns the worker, its claims and the run's reports.
    """
    claims = []
    reports = []

    class _RunManager:
        def __init__(self, backend):
            self.backend = backend

        def create(self, spec):
            pass

        def start(self, spec):
            return SimpleNamespace(container_id="container-1")

        def prepare_shared_workspace(self, spec):
            pass

        def inspect(self, name):
            return None

        def remove_environment_test(self, spec, *, container_id):
            pass

    class _Source:
        def __init__(self, job, **kwargs):
            pass

        def import_into(self, manager, spec):
            return "c" * 40

        def discard(self):
            pass

    class _Preparation:
        root = tmp_path

        def prepare(self, backend, environment, *, callback, cancel):
            prepare(callback, cancel)

    def post(path, json):
        reports.append(json)
        return httpx.Response(status, request=httpx.Request("POST", f"http://control{path}"))

    backend = SimpleNamespace(
        uid=1000, gid=1000, version_stamp=lambda: {"sandbox_image_digest": "sha256:" + "a" * 64}
    )
    environment = BoundEnvironment(binding_id="b", setup_script="make").model_dump(mode="json")

    def claim_and_execute(**kwargs):
        claims.append(kwargs)
        if len(claims) == 1:
            environment_tests.execute_test(
                client=SimpleNamespace(post=post),
                run={
                    "id": "run-1",
                    "lease_generation": 1,
                    "repo": "org/repo",
                    "base_sha": "c" * 40,
                    "environment": environment,
                    "purpose": "test",
                },
                backend=backend,
                stop=kwargs["stop"],
            )
        return True

    monkeypatch.setattr(environment_tests, "ChatSandboxManager", _RunManager)
    monkeypatch.setattr(environment_tests, "RetainedChatBackend", lambda manager, spec: None)
    monkeypatch.setattr(environment_tests, "EnvironmentPreparation", _Preparation)
    monkeypatch.setattr(environment_tests, "claim_and_execute", claim_and_execute)
    monkeypatch.setattr("cloud_agent_host.runner._SourcePrefetch", _Source)
    return environment_tests.EnvironmentTestWorker("http://control", backend), claims, reports


@pytest.mark.parametrize("refused", ["report", "heartbeat"])
def test_a_cancelled_environment_run_leaves_the_worker_polling(monkeypatch, tmp_path, refused):
    """A refused report or a failed heartbeat ends that run, never the host's worker."""
    now = [100.0]
    monkeypatch.setattr(environment_tests.time, "monotonic", lambda: now[0])

    def prepare(callback, cancel):
        if refused == "report":
            # The owner cancelled the run, so its next report is refused.
            callback({"phase": "setup", "state": "running", "output": "installing"})
        else:
            # A heartbeat falls due and the control plane cannot be reached.
            now[0] += 11
            assert cancel.wait(15)

    worker, claims, reports = _environment_worker(
        monkeypatch, tmp_path, prepare, 409 if refused == "report" else 503
    )
    worker.poll("credential")
    worker._thread.join(20)
    assert reports[-1]["state"] == "cancelled"
    assert not worker._stop.is_set()
    now[0] += 6
    worker.poll("credential")
    worker._thread.join(1)
    worker.close()
    assert len(claims) == 2


def test_closing_the_environment_worker_still_cancels_its_run(monkeypatch, tmp_path):
    started = threading.Event()

    def prepare(callback, cancel):
        started.set()
        assert cancel.wait(5)

    worker, _, reports = _environment_worker(monkeypatch, tmp_path, prepare, 200)
    worker.poll("credential")
    assert started.wait(5)
    worker.close()
    assert not worker.busy
    assert reports[-1]["state"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
@pytest.mark.parametrize("operation", ["terminal", "create_terminal"])
async def test_terminal_prepares_at_the_broker_once_and_empty_bindings_need_no_capability(
    configured, operation
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"capabilities": ["environment-v1"]}
            if request.url.path == "/health"
            else {"id": "term"},
        )

    bound = BoundEnvironment(binding_id="b", setup_script="install" if configured else "")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        broker = SandboxManagerClient("http://broker", "credential", client=client)
        args = (
            {"command": "echo 1", "cwd": "/workspace", "timeout_seconds": 60}
            if operation == "terminal"
            else {"rows": 24, "cols": 80}
        )
        await getattr(broker, operation)("workspace", environment=bound, **args)
    assert len(requests) == (2 if configured else 1)
    assert not any("environment/prepare" in str(request.url) for request in requests)
    assert (b'"environment"' in requests[-1].content) == configured


@pytest.mark.asyncio
async def test_old_broker_receives_no_secret_bearing_payload():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"capabilities": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        broker = SandboxManagerClient("http://broker", "credential", client=client)
        with pytest.raises(SandboxManagerError, match="upgraded"):
            await broker.create_terminal(
                "workspace",
                rows=24,
                cols=80,
                environment=BoundEnvironment(binding_id="b", setup_script="install"),
            )
    assert len(requests) == 1
    assert requests[0].url.path == "/health"


@pytest.mark.asyncio
async def test_empty_binding_snapshot_uses_the_existing_broker_protocol(monkeypatch):
    from unittest.mock import AsyncMock

    from cloud_agent import sandbox_manager_routing
    from cloud_agent.workers.snapshots import _capture_due_shared_snapshot

    snapshot = {
        "snapshot_id": 1,
        "sandbox_id": "sandbox",
        "generation": 1,
        "container_id": "container",
        "workspace_epoch": 2,
        "base_sha": "a" * 40,
        "anchor_job_id": "job",
    }
    receipt = {**snapshot, "sandbox_generation": 1, "patch": "", "content_sha256": "digest"}
    broker = SimpleNamespace(capture_shared_snapshot=AsyncMock(return_value=receipt))
    monkeypatch.setattr(
        sandbox_manager_routing, "sandbox_manager_for_job", AsyncMock(return_value=broker)
    )
    store = SimpleNamespace(
        host_pool=object(),
        snapshot=SimpleNamespace(
            claim_due_sandbox_snapshot=AsyncMock(return_value=snapshot),
            record_sandbox_snapshot_capture=AsyncMock(return_value={"id": 1}),
        ),
        environments=SimpleNamespace(
            get_job_environment=AsyncMock(return_value=BoundEnvironment(binding_id="empty"))
        ),
    )
    assert await _capture_due_shared_snapshot(store)
    assert "environment" not in broker.capture_shared_snapshot.call_args.args[0]


@pytest.mark.asyncio
async def test_empty_binding_fork_reaches_coordination_without_environment_preparation(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from cloud_agent.application import session_fork

    broker = SimpleNamespace(prepare_environment=AsyncMock(side_effect=RuntimeError("old broker")))
    monkeypatch.setattr(session_fork, "sandbox_manager_for_job", AsyncMock(return_value=broker))
    store = SimpleNamespace(
        host_pool=object(),
        environments=SimpleNamespace(
            get_environment_binding=AsyncMock(return_value=BoundEnvironment(binding_id="empty"))
        ),
        session_fork=SimpleNamespace(
            claim_session_fork_materialization=AsyncMock(return_value=False)
        ),
    )
    operation = {
        "id": "fork",
        "runtime_id": "claude-code",
        "source_thread_id": "thread",
        "resident_slot_id": 1,
        "source_job": {"user_id": "alice", "environment_binding_id": "empty"},
        "group_members": [{"thread_id": "thread"}],
    }
    with pytest.raises(HTTPException) as failure:
        await session_fork.materialize_session_fork(
            operation, job_id="job", job_store=store, grants=None
        )
    assert failure.value.detail["error"]["type"] == "fork_in_progress"
    broker.prepare_environment.assert_not_awaited()
    store.session_fork.claim_session_fork_materialization.assert_awaited_once()
