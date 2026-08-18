"""Unit tests for the agent job runner.

Covers the parts where being wrong is expensive: losing the lease must stop
the run rather than race a replacement, cancellation must actually kill the
agent, and the runner must never acquire a git write path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cloud_agent_host import enrollment, runner as runner_mod
from cloud_agent_host.runner import (
    ClaimedJob,
    LeaseLost,
    build_patch,
    conversation_prompt,
    run_agent,
)
from cloud_agent_host.runtimes import ClaudeCodeRuntime, GenericRuntime, RuntimeMCPConfig
from cloud_agent_host.sandbox import ProcessBackend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_JOB = ClaimedJob(
    job_id="ajob_1",
    attempt_id=7,
    attempt_no=1,
    repo="o/n",
    base_sha="abc",
    task_prompt="do it",
    runtime="generic",
    model="glm-5.1",
    worker_token="ajt.a.b",
    sandbox_token="ajt.model.b",
)


def test_follow_up_prompt_replays_runtime_neutral_conversation():
    """A harness switch still receives the prior thread and current request."""
    job = ClaimedJob(
        **{
            **_JOB.__dict__,
            "task_prompt": "now add tests",
            "context_messages": [
                {"role": "user", "content": "fix the timeout"},
                {"role": "assistant", "content": "I updated the middleware"},
            ],
        }
    )

    prompt = conversation_prompt(job)

    assert "USER: fix the timeout" in prompt
    assert "ASSISTANT: I updated the middleware" in prompt
    assert prompt.endswith("NEW USER REQUEST:\nnow add tests")


@pytest.fixture(autouse=True)
def _allow_unisolated_sandbox(monkeypatch):
    """These tests exercise runner logic, not sandbox policy.

    The process backend refuses to start unisolated unless an operator opts
    in — that refusal is asserted in test_agent_sandbox.py; here we accept it
    so the runner's own behaviour is what is under test.
    """
    monkeypatch.setenv("AGENT_SANDBOX_ALLOW_UNISOLATED", "1")


class FakeControl:
    """Records what the runner reported."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.artifacts: dict[str, str] = {}
        self.finished: tuple[str, str | None] | None = None
        self.job_id = "ajob_1"
        self.continuity: str | None = None

    def append_event(self, event) -> None:
        self.events.append((event.event_type, event.payload))

    def report_continuity(self, mode: str) -> None:
        self.continuity = mode

    def save_artifact(self, kind: str, content: str) -> None:
        self.artifacts[kind] = content

    def suspend_terminals(self, phase: str) -> None:
        self.append_event(
            type("Event", (), {"event_type": "lifecycle", "payload": {"phase": phase}})()
        )

    def resume_terminals(self) -> None:
        self.append_event(
            type(
                "Event",
                (),
                {"event_type": "lifecycle", "payload": {"phase": "workspace_ready"}},
            )()
        )

    def finish(self, state: str, detail: str | None = None) -> None:
        self.finished = (state, detail)

    def close(self) -> None:
        pass


class FakeHeart:
    """Stand-in heartbeater with directly settable flags."""

    def __init__(self, *, cancel: bool = False, lost: bool = False) -> None:
        self.cancel_requested = cancel
        self.lease_lost = lost

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _echo_runtime(lines: int = 3, *, sleep: float = 0.0) -> GenericRuntime:
    """A runtime whose 'agent' is a python one-liner emitting N lines.

    Deliberately a single line: the template is shell-*split*, never
    shell-*executed*, so an embedded newline would be mangled rather than
    interpreted — which is exactly the property that makes GenericRuntime safe.
    """
    script = (
        f"for i in range({lines}): "
        f"print('line %d' % i, flush=True); __import__('time').sleep({sleep})"
    )
    return GenericRuntime(f"{sys.executable} -c {script!r}")


def test_agent_output_streams_back_as_events(tmp_path):
    """Every line the agent prints reaches the control plane."""
    control, heart = FakeControl(), FakeHeart()
    code, _tail, _errors = run_agent(
        _echo_runtime(3),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 0
    assert [payload["text"] for _kind, payload in control.events] == [
        "line 0",
        "line 1",
        "line 2",
    ]


def test_runner_supplies_trusted_runtime_boundaries(tmp_path):
    """MCP and sandbox policy come from the trusted runner, never the checkout."""
    captured = []

    class _ExternallySandboxedProcessBackend(ProcessBackend):
        @property
        def provides_isolation(self) -> bool:
            return True

    class _CapturingRuntime(GenericRuntime):
        def prepare(self, *, mcp_config, provides_isolation, **kwargs):
            captured.append((mcp_config, provides_isolation))
            return super().prepare(
                mcp_config=mcp_config,
                provides_isolation=provides_isolation,
                **kwargs,
            )

    runtime = _CapturingRuntime(f"{sys.executable} -c " + repr("print('ok')"))
    for backend in (
        ProcessBackend(acknowledged_unsafe=True),
        _ExternallySandboxedProcessBackend(acknowledged_unsafe=True),
    ):
        run_agent(
            runtime,
            job=_JOB,
            workdir=str(tmp_path),
            gateway_base_url="http://gw",
            control=FakeControl(),
            heart=FakeHeart(),
            timeout_s=30,
            backend=backend,
        )

    assert captured == [
        (RuntimeMCPConfig(), False),
        (RuntimeMCPConfig(), True),
    ]


def test_losing_the_lease_aborts_instead_of_racing(tmp_path):
    """A superseded attempt stops rather than continuing beside its replacement."""
    control, heart = FakeControl(), FakeHeart(lost=True)
    with pytest.raises(LeaseLost):
        run_agent(
            _echo_runtime(50, sleep=0.02),
            job=_JOB,
            workdir=str(tmp_path),
            gateway_base_url="http://gw",
            control=control,
            heart=heart,
            timeout_s=30,
            backend=ProcessBackend(acknowledged_unsafe=True),
        )


def test_cancellation_kills_the_agent(tmp_path):
    """Owner cancellation, delivered via heartbeat, actually stops the process."""
    control, heart = FakeControl(), FakeHeart(cancel=True)
    code, _tail, _errors = run_agent(
        _echo_runtime(200, sleep=0.02),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 130
    assert ("lifecycle", {"phase": "cancelled_by_owner"}) in control.events


def test_agent_timeout_is_enforced(tmp_path):
    """A runaway agent is killed rather than holding the job forever."""
    control, heart = FakeControl(), FakeHeart()
    code, _tail, _errors = run_agent(
        _echo_runtime(500, sleep=0.05),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=0.15,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 124
    assert any(kind == "error" for kind, _ in control.events)


def test_agent_environment_is_hermetic(tmp_path, monkeypatch):
    """The agent inherits only what it needs — not the CI runner's secrets."""
    monkeypatch.setenv("A_CI_SECRET", "super-secret-value")
    control, heart = FakeControl(), FakeHeart()
    dumper = GenericRuntime(
        f"{sys.executable} -c " + repr("import os;print('A_CI_SECRET' in os.environ)")
    )
    run_agent(
        dumper,
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert control.events[0][1]["text"] == "False"


def test_build_patch_captures_new_and_modified_files(tmp_path):
    """The patch includes untracked files, which a bare git diff would miss."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(args, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "existing.txt").write_text("before\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")

    (repo / "existing.txt").write_text("after\n")
    (repo / "brand_new.txt").write_text("hello\n")

    patch = build_patch(str(repo))
    assert "existing.txt" in patch
    assert "brand_new.txt" in patch, "untracked files must appear in the patch"
    assert "+after" in patch


def test_build_patch_is_empty_when_nothing_changed(tmp_path):
    """A job that changed nothing produces no patch rather than a bogus one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(args, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")

    assert build_patch(str(repo)).strip() == ""


def test_missing_runtime_binary_fails_the_job_loudly(monkeypatch, tmp_path):
    """A job whose runtime is absent fails now, not after a lease timeout."""
    control = FakeControl()
    monkeypatch.setattr(
        runner_mod,
        "claim",
        lambda **_kwargs: ClaimedJob(
            job_id="ajob_1",
            attempt_id=7,
            attempt_no=1,
            repo="o/n",
            base_sha=None,
            task_prompt="p",
            runtime="claude-code",
            model="m",
            worker_token="ajt.a.b",
            sandbox_token="ajt.model.b",
        ),
    )
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *a, **k: control)
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "has_binary", lambda _name: False)

    code = runner_mod.run_once(
        base_url="http://gw",
        dispatcher_token="d",
        worker_id="w",
        workdir=str(tmp_path),
        backend=backend,
    )
    assert code == 2
    assert control.finished[0] == "failed"
    assert "not installed" in control.finished[1]


def test_live_runner_reuses_one_durable_workspace_per_job(monkeypatch, tmp_path):
    """A retry addresses the same tree and never reapplies its saved context patch."""

    class Control(FakeControl):
        def finish(self, state, detail=None, **_kwargs):
            self.finished = (state, detail)

    class Backend(_CountingBackend):
        def adopt_workdir(self, _path: str) -> None:
            pass

    job = ClaimedJob(
        **{
            **_JOB.__dict__,
            "thread_id": "athr_durable",
            "base_sha": "abcdef1",
            "context_patch": "diff --git a/a b/a\n",
        }
    )
    control = Control()
    workdirs: list[str] = []
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: job)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_args, **_kwargs: control)
    monkeypatch.setattr(runner_mod, "Heartbeater", lambda *_args, **_kwargs: FakeHeart())
    runtime = type("Runtime", (), {"binary": "fake"})()
    monkeypatch.setattr(runner_mod, "get_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(
        runner_mod,
        "existing_checkout_sha",
        lambda workdir, _repo: workdirs.append(workdir) or "abcdef1",
    )
    monkeypatch.setattr(
        runner_mod,
        "align_existing_checkout",
        lambda workdir, **_kwargs: workdirs.append(workdir) or "abcdef1",
    )
    monkeypatch.setattr(runner_mod, "checkout_output_branch", lambda *_args, **_kwargs: "agent/x")
    applied: list[str] = []
    monkeypatch.setattr(
        runner_mod, "apply_context_patch", lambda *_args, **_kwargs: applied.append("patch")
    )
    monkeypatch.setattr(runner_mod, "run_agent", lambda *_args, **_kwargs: (0, "", []))
    monkeypatch.setattr(runner_mod, "build_patch", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(runner_mod, "save_workspace_snapshot", lambda *_args, **_kwargs: True)

    code = runner_mod.run_once(
        base_url="http://gw",
        dispatcher_token="d",
        worker_id="w",
        workdir=str(tmp_path / "unused"),
        workspace_root=str(tmp_path / "workspaces"),
        backend=Backend(),
    )

    expected = str(tmp_path / "workspaces" / "ajob_1")
    assert code == 0
    assert workdirs == [expected]
    assert applied == [], "the saved patch is already present in a reused live worktree"
    assert (
        "lifecycle",
        {
            "phase": "started",
            "runtime": job.runtime,
            "attempt_no": job.attempt_no,
            "sandbox_backend": "counting",
        },
    ) in control.events


def test_workspace_ready_follows_context_restore_and_setup(monkeypatch, tmp_path):
    """Terminal readiness is emitted only after trusted workspace preparation."""

    timeline: list[str] = []

    class Control(FakeControl):
        def append_event(self, event) -> None:
            if event.event_type == "lifecycle":
                timeline.append(event.payload["phase"])
            super().append_event(event)

        def finish(self, state, detail=None, **_kwargs):
            self.finished = (state, detail)

    class Backend(_CountingBackend):
        def adopt_workdir(self, _path: str) -> None:
            pass

    job = ClaimedJob(
        **{
            **_JOB.__dict__,
            "thread_id": "athr_follow_up",
            "parent_job_id": "ajob_parent",
            "context_patch": "diff --git a/a b/a\n",
            "setup_script": "uv sync",
        }
    )
    control = Control()
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: job)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_args, **_kwargs: control)
    monkeypatch.setattr(runner_mod, "Heartbeater", lambda *_args, **_kwargs: FakeHeart())
    runtime = type("Runtime", (), {"binary": "fake"})()
    monkeypatch.setattr(runner_mod, "get_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(runner_mod, "existing_checkout_sha", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner_mod, "prepare_worktree", lambda **_kwargs: "abcdef1")
    monkeypatch.setattr(runner_mod, "checkout_output_branch", lambda *_args, **_kwargs: "agent/x")
    monkeypatch.setattr(
        runner_mod,
        "apply_context_patch",
        lambda *_args, **_kwargs: timeline.append("context_patch_applied"),
    )
    monkeypatch.setattr(runner_mod, "build_cache_from_env", lambda: object())
    setup = type(
        "Setup",
        (),
        {"restored_from_cache": False, "ran": True, "exit_code": 0, "detail": ""},
    )()
    monkeypatch.setattr(
        runner_mod,
        "run_setup",
        lambda **_kwargs: timeline.append("setup_finished") or setup,
    )
    monkeypatch.setattr(
        runner_mod,
        "run_agent",
        lambda *_args, **_kwargs: timeline.append("agent_started") or (0, "", []),
    )
    monkeypatch.setattr(
        runner_mod,
        "build_patch",
        lambda *_args, **_kwargs: timeline.append("patch_captured") or "",
    )
    monkeypatch.setattr(runner_mod, "save_workspace_snapshot", lambda *_args, **_kwargs: True)

    code = runner_mod.run_once(
        base_url="http://gw",
        dispatcher_token="d",
        worker_id="w",
        workdir=str(tmp_path / "unused"),
        workspace_root=str(tmp_path / "workspaces"),
        backend=Backend(),
    )

    assert code == 0
    assert timeline.index("workspace_preparing") < timeline.index("checked_out")
    assert timeline.index("checked_out") < timeline.index("context_patch_applied")
    assert timeline.index("context_patch_applied") < timeline.index("context_restored")
    assert timeline.index("context_restored") < timeline.index("setup_finished")
    assert timeline.index("setup_finished") < timeline.index("setup")
    assert timeline.index("setup") < timeline.index("workspace_ready")
    assert timeline.index("workspace_ready") < timeline.index("agent_started")
    assert timeline.index("agent_started") < timeline.index("workspace_finalizing")
    assert timeline.index("workspace_finalizing") < timeline.index("patch_captured")


def test_empty_queue_is_a_clean_no_op(monkeypatch, tmp_path):
    """Nothing queued is success, not an error the CI would flag."""
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: None)
    assert (
        runner_mod.run_once(
            base_url="http://gw",
            dispatcher_token="d",
            worker_id="w",
            workdir=str(tmp_path),
        )
        == 0
    )


def test_runner_source_contains_no_push_path():
    """The runner must never gain a git write path — patch-out is the contract."""
    source = Path(runner_mod.__file__).read_text()
    assert '"push"' not in source
    assert "git push" not in source


def test_failed_tool_calls_are_reported_not_swallowed(tmp_path):
    """A denied tool call must reach the caller, not be trusted away.

    Found on the first real run: Claude Code's permission gate denied the
    Edit, the model then reported "I've added the docstring" anyway, and the
    job was recorded as a clean success that changed nothing. The runner now
    reports what the tools did rather than what the agent said about them.
    """
    lines = [
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Edit","input":{}}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","is_error":true,"content":"permission denied"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Done!"}]}}',
    ]
    script = f"import json;[print(line, flush=True) for line in json.loads({json.dumps(lines)!r})]"

    class _ScriptedClaude(ClaudeCodeRuntime):
        """Claude Code's event parsing, driven by a scripted stdout."""

        def prepare(self, **_kwargs):
            return [sys.executable, "-c", script], {}

    control = FakeControl()
    code, _tail, errors = run_agent(
        _ScriptedClaude(),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=FakeHeart(),
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 0
    assert len(errors) == 1
    assert "permission denied" in errors[0]
    # The owner sees it in the stream, not only in the final detail string.
    assert any(kind == "error" for kind, _payload in control.events)


class _CountingBackend:
    """Records how often a supplied backend is preflighted."""

    name = "counting"
    provides_isolation = False

    def __init__(self) -> None:
        self.preflights = 0

    def preflight(self, workdir_root: str | None = None) -> None:
        self.preflights += 1

    def has_binary(self, _name: str) -> bool:
        return True

    def base_env(self) -> dict[str, str]:
        return {}

    def sandbox_metadata(self) -> dict[str, str]:
        return {"sandbox_backend": self.name}


def test_a_supplied_backend_is_not_preflighted_per_claim(monkeypatch, tmp_path):
    """An idle standing runner must not re-probe Docker on every poll.

    Preflight spawns several `docker` subprocesses and logs the shared-kernel
    warning. Running it per claim meant an idle runner polling every 5s burned
    ~17k probes and wrote ~17k warning lines a day — visible only once a
    runner actually stood up and idled.
    """
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: None)
    backend = _CountingBackend()

    for _ in range(3):
        runner_mod.run_once(
            base_url="http://gw",
            dispatcher_token="d",
            worker_id="w",
            workdir=str(tmp_path),
            backend=backend,
        )

    assert backend.preflights == 0, "run_once preflighted a backend its caller already preflighted"


def test_a_self_built_backend_is_still_preflighted_before_claiming(monkeypatch, tmp_path):
    """The one-shot path keeps failing fast on a misconfigured host."""
    backend = _CountingBackend()
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    claims: list[bool] = []

    def _claim(**_kwargs):
        claims.append(True)
        return None

    monkeypatch.setattr(runner_mod, "claim", _claim)

    runner_mod.run_once(
        base_url="http://gw",
        dispatcher_token="d",
        worker_id="w",
        workdir=str(tmp_path),
    )

    assert backend.preflights == 1
    assert claims, "preflight must not have replaced the claim"


def test_only_the_claim_call_reports_an_unreachable_gateway(monkeypatch):
    """A transport failure after a claim must not be logged as "nothing claimed".

    "Gateway unreachable, retrying" tells an operator no job was taken. Raised
    for a failure during event reporting or a terminal transition, it hides an
    attempt that is still running until its lease expires — the one they need
    to go look at.
    """
    import httpx

    from cloud_agent_host.runner import ClaimUnreachable, claim

    def dead_post(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", dead_post)

    with pytest.raises(ClaimUnreachable):
        claim(base_url="http://gateway.invalid", dispatcher_token="t", worker_id="w", lease_ttl=60)

    # The same error raised anywhere else stays a plain transport error, so the
    # loop's handler reports a job rather than an empty claim.
    assert not issubclass(httpx.ConnectError, ClaimUnreachable)


# ── Host reporting ─────────────────────────────────────────────────────


def test_a_runner_reports_no_host_unless_one_is_configured(monkeypatch):
    """Unset must mean "no host", never a guess.

    ``socket.gethostname()`` is the container id in every containerised
    deployment, so guessing would fill the operator's machine list with rows of
    hex — one per replica, another set after every restart.
    """
    monkeypatch.delenv("AGENT_RUNNER_HOST", raising=False)
    assert runner_mod.default_runner_host() is None

    monkeypatch.setenv("AGENT_RUNNER_HOST", "  ")
    assert runner_mod.default_runner_host() is None

    monkeypatch.setenv("AGENT_RUNNER_HOST", " runner-b ")
    assert runner_mod.default_runner_host() == "runner-b"


def test_replicas_on_one_machine_share_a_host_but_not_a_worker_id(monkeypatch):
    """The pool is machines; ``lease_owner`` is still per-replica."""
    monkeypatch.setenv("AGENT_RUNNER_HOST", "runner-b")
    monkeypatch.setenv("AGENT_WORKER_ID", "runner")

    assert runner_mod.default_runner_host() == "runner-b"
    assert runner_mod.default_worker_id() != "runner-b"


def test_claim_sends_the_host_only_when_there_is_one(monkeypatch):
    """An unreporting runner must post exactly the payload it always did."""
    sent: list[dict] = []

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return None

    def _post(_url, *, json, headers, timeout):
        sent.append(json)
        return _Response()

    monkeypatch.setattr(runner_mod.httpx, "post", _post)

    runner_mod.claim(base_url="http://gw", dispatcher_token="t", worker_id="w1", lease_ttl=60)
    runner_mod.claim(
        base_url="http://gw", dispatcher_token="t", worker_id="w1", lease_ttl=60, host="runner-b"
    )

    assert "host" not in sent[0]
    assert sent[1]["host"] == "runner-b"


def test_the_host_flag_defaults_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_RUNNER_HOST", "runner-a")

    args = runner_mod.build_parser().parse_args([])

    assert args.host == "runner-a"


def test_the_control_plane_and_the_gateway_are_separate_destinations():
    """**They were one value, and after the split they are two services.**

    The runner claims work, reports events and renews its lease against the
    control plane; the sandbox calls models against the gateway. Pointing a
    single value at either one breaks whichever half it is not — the runner
    claims nothing, or every job dies at its first model call.

    `AGENT_CONTROL_PLANE_URL` falls back to the gateway's so a host that has
    not been reconfigured keeps behaving exactly as before: wrong in the same
    way, rather than newly broken, which is the only safe default while both
    shapes are deployed.
    """
    import os
    from unittest.mock import patch

    from cloud_agent_host.runner import build_parser

    with patch.dict(
        os.environ,
        {"AGENT_CONTROL_PLANE_URL": "http://control:8000", "AGENT_GATEWAY_URL": "http://gw:8080"},
        clear=False,
    ):
        args = build_parser().parse_args([])
    assert args.base_url == "http://control:8000"
    assert args.gateway_url == "http://gw:8080"

    # Unset control-plane URL: the old single-value shape, unchanged.
    with patch.dict(os.environ, {"AGENT_GATEWAY_URL": "http://gw:8080"}, clear=True):
        legacy = build_parser().parse_args([])
    assert legacy.base_url == "http://gw:8080"
    assert legacy.gateway_url == "http://gw:8080"


# ---------------------------------------------------------------------------
# Which credential the loop claims with
# ---------------------------------------------------------------------------


def _drive_loop(monkeypatch, tmp_path, *, identity):
    """Run `run_forever` for exactly one pass and return the token it claimed with."""
    monkeypatch.setattr(runner_mod, "ensure_enrolled", lambda **kw: identity)
    monkeypatch.setattr(runner_mod, "_housekeeping", lambda *a, **kw: None)

    class _Beat:
        def __init__(self, **kw):
            self._identity = kw["identity"]
            self.removed = __import__("threading").Event()

        credential = property(lambda self: self._identity.credential)
        claimable = True

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runner_mod, "HostHeartbeat", _Beat)

    seen: dict[str, str] = {}

    def _run_once(**kwargs):
        seen["token"] = kwargs["dispatcher_token"]
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_mod, "run_once", _run_once)

    class _Backend:
        name = "process"

        def preflight(self, workdir_root):
            return None

        def check_gateway_reachable(self, url):
            return None

    runner_mod.run_forever(
        gateway_url="http://gw",
        base_url="http://cp",
        dispatcher_token="pool-wide-dispatcher-token",
        worker_id="w1",
        workdir_root=str(tmp_path),
        backend=_Backend(),
        host="h1",
    )
    return seen["token"]


def test_an_enrolled_host_claims_with_its_own_credential(monkeypatch, tmp_path) -> None:
    """The whole point of enrolling, and it was not wired.

    The dispatcher token is shared by every machine in the pool, so a claim
    carrying it says "a runner" and not "which". The control plane reads the
    host from a host credential rather than from the request body — so with
    the shared token, drain, pin and revoke are decisions about a machine that
    cannot be told apart from its neighbours. Enrolling and then claiming with
    the shared token buys nothing at all.
    """
    identity = enrollment.HostIdentity(host="h1", credential="aht.h1.secret")

    token = _drive_loop(monkeypatch, tmp_path, identity=identity)

    assert token == "aht.h1.secret", (
        "an enrolled host claimed with the pool-wide dispatcher token, which "
        "makes every per-host control decorative"
    )


def test_a_host_with_no_pool_still_claims_with_the_dispatcher_token(monkeypatch, tmp_path) -> None:
    """The single-host shape predates the pool and must keep working."""
    assert _drive_loop(monkeypatch, tmp_path, identity=None) == "pool-wide-dispatcher-token"


def test_a_lost_lease_drops_the_residency_before_unwinding(tmp_path):
    """Review finding 2: a dead residency must stop beating at the kill.

    The drop happens at the moment the lease loss aborts the turn — not
    after the exception has unwound through outer handlers that may swallow
    it — so the heartbeat thread can never again report a generation whose
    workspace just lost its turn.
    """
    import dataclasses

    from cloud_agent_host.residency import ResidentEntry

    control, heart = FakeControl(), FakeHeart(lost=True)
    dropped: list[tuple[str, bool, int | None]] = []

    class _Pool:
        def drop(self, thread_id: str, *, release: bool, generation: int | None = None) -> None:
            dropped.append((thread_id, release, generation))

    entry = ResidentEntry(
        thread_id="t-hot",
        generation=3,
        session_id="sess-1",
        workdir=str(tmp_path),
        state_dir=f"{tmp_path}.state",
        runtime_id="generic",
        model="m",
    )
    with pytest.raises(LeaseLost):
        run_agent(
            _echo_runtime(50, sleep=0.02),
            job=dataclasses.replace(_JOB, thread_id="t-hot"),
            workdir=str(tmp_path),
            gateway_base_url="http://gw",
            control=control,
            heart=heart,
            timeout_s=30,
            backend=ProcessBackend(acknowledged_unsafe=True),
            resident=entry,
            residents=_Pool(),
        )

    assert dropped == [("t-hot", True, 3)]


def test_a_lease_lost_at_finish_still_drops_the_residency(tmp_path, monkeypatch):
    """Review (second pass): the post-run_agent leak.

    The turn succeeds, the agent's session has advanced — and then the lease
    is lost during finish. Without the finish-confirmation guard the
    residency survived, and the retry resumed a session the failed attempt
    had already advanced, re-executing the same prompt. The guard drops it on
    every unconfirmed exit.
    """
    import dataclasses

    from cloud_agent_host.residency import ResidentEntry

    job = dataclasses.replace(_JOB, thread_id="t-hot", base_sha=None)
    dropped: list[tuple[str, bool, int | None]] = []

    class _Pool:
        def lookup(self, thread_id):
            return entry if thread_id == "t-hot" else None

        def drop(self, thread_id: str, *, release: bool, generation: int | None = None) -> None:
            dropped.append((thread_id, release, generation))

    entry = ResidentEntry(
        thread_id="t-hot",
        generation=5,
        session_id="sess-1",
        workdir=str(tmp_path),
        state_dir=f"{tmp_path}.state",
        runtime_id="generic",
        model="m",
    )

    class _FinishLosesLease(FakeControl):
        def __init__(self) -> None:
            super().__init__()
            self.job_id = job.job_id

        def finish(self, state: str, detail: str | None = None, **_kw) -> None:
            raise LeaseLost("superseded during finish")

        def suspend_terminals(self, phase: str) -> None:
            pass

        def resume_terminals(self) -> None:
            pass

    class _EchoBackend(ProcessBackend):
        def has_binary(self, name: str) -> bool:
            return True

    monkeypatch.setattr(runner_mod, "claim", lambda **_kw: job)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *a, **k: _FinishLosesLease())
    monkeypatch.setattr(runner_mod, "Heartbeater", lambda *a, **k: FakeHeart(), raising=True)
    monkeypatch.setattr(runner_mod, "existing_checkout_sha", lambda *a, **k: "abc")
    monkeypatch.setattr(runner_mod, "align_existing_checkout", lambda *a, **k: "abc")
    monkeypatch.setattr(runner_mod, "build_patch", lambda *a, **k: "")
    monkeypatch.setattr(runner_mod, "save_workspace_snapshot", lambda *a, **k: True)
    runtime = _echo_runtime(2)
    monkeypatch.setattr(runner_mod, "get_runtime", lambda *a, **k: runtime)

    code = runner_mod.run_once(
        base_url="http://cp",
        dispatcher_token="tok",
        worker_id="w1",
        workdir=str(tmp_path),
        backend=_EchoBackend(acknowledged_unsafe=True),
        residents=_Pool(),  # type: ignore[arg-type]
    )

    assert code == 3  # the LeaseLost exit
    # The receipt is redeemed generation-scoped: exactly the borrowed one.
    assert ("t-hot", True, 5) in dropped


# ── true HOT: the live process between turns (design §7 + §14) ─────────


class _ResidentProcess:
    """A live resident process: scripted stdout, recorded stdin."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.feed: list[str] = []
        self.killed = False
        self.stdin_closed = False
        self._exit = 0

    def write_line(self, text: str) -> None:
        self.sent.append(text)

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def lines(self):
        while self.feed:
            yield self.feed.pop(0)

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        return 1 if self.killed else None

    def wait(self) -> int:
        return self._exit

    def stderr_text(self) -> str:
        return ""


class _LiveSpawnBackend:
    """The backend surface run_agent touches, spawning a scripted process."""

    provides_isolation = True

    def __init__(self, process: _ResidentProcess) -> None:
        self.process = process
        self.specs: list = []

    def base_env(self) -> dict[str, str]:
        return {"PATH": "/usr/bin"}

    def adopt_workdir(self, path: str) -> None:
        return None

    def spawn(self, spec):
        self.specs.append(spec)
        return self.process


class _DetachPool:
    """Records pool calls the hot path makes."""

    def __init__(self) -> None:
        self.detached: list[str] = []
        self.dropped: list[tuple[str, bool, int | None]] = []

    def detach_session(self, thread_id: str) -> None:
        self.detached.append(thread_id)

    def drop(self, thread_id: str, *, release: bool, generation: int | None = None) -> None:
        self.dropped.append((thread_id, release, generation))


def _result_line() -> str:
    return json.dumps({"type": "result", "subtype": "success"})


def _init_line(session_id: str) -> str:
    # The CLI announces its session id in the ``system/init`` event — the one
    # place the runtime adapter captures it from.
    return json.dumps({"type": "system", "subtype": "init", "session_id": session_id})


def _answer_line(text: str = "done") -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}], "usage": {}}}
    )


def _resident_entry(tmp_path, **overrides):
    from cloud_agent_host.residency import ResidentEntry

    state = Path(f"{tmp_path}/state")
    state.mkdir(parents=True, exist_ok=True)
    fields = {
        "thread_id": "t-hot",
        "generation": 3,
        "session_id": "sess-prev",
        "workdir": str(tmp_path),
        "state_dir": str(state),
        "runtime_id": "claude-code",
        "model": "glm-5.1",
        "credential": "residency-grant-token",
        "grant_model": "glm-5.1",
    }
    fields.update(overrides)
    return ResidentEntry(**fields)


def _spawned_live_session(runtime: ClaudeCodeRuntime, process: _ResidentProcess, state_dir: str):
    """A live session that already ran a turn — the process predates this one."""
    from cloud_agent_host.drivers import ClaudeCodeDriver, LaunchContext, TurnInput

    context = LaunchContext(
        workdir="/w",
        gateway_base_url="http://gw",
        credential="residency-grant-token",
        provides_isolation=True,
        spawn=lambda argv, env: (_ for _ in ()).throw(AssertionError("one-shot spawn used")),
        state_dir=state_dir,
        spawn_live=lambda argv, env: process,
    )
    session = ClaudeCodeDriver(runtime).launch_live(context)
    process.feed = [_result_line()]
    session.start_turn(TurnInput(prompt="first", model="glm-5.1")).wait()
    return session


def _hot_job(**overrides):
    import dataclasses

    fields = {"thread_id": "t-hot", "runtime": "claude-code", **overrides}
    return dataclasses.replace(_JOB, **fields)


def test_a_turn_on_the_surviving_process_reports_hot(tmp_path):
    """The product point of #57: the follow-up is a stdin line into the same
    process, labelled ``hot`` only because the process predates the turn —
    and the survivor is handed out for the pool to keep."""
    runtime = ClaudeCodeRuntime()
    process = _ResidentProcess()
    entry = _resident_entry(tmp_path)
    entry.live_session = _spawned_live_session(runtime, process, entry.state_dir)
    control = FakeControl()
    live_out: dict = {}

    process.feed = [_init_line("sess-live"), _answer_line(), _result_line()]
    exit_code, _tail, _errors = run_agent(
        runtime,
        job=_hot_job(),
        workdir=entry.workdir,
        gateway_base_url="http://gw",
        control=control,
        heart=FakeHeart(),
        timeout_s=30,
        backend=_LiveSpawnBackend(process),
        state_dir=entry.state_dir,
        resident=entry,
        residents=_DetachPool(),
        live_out=live_out,
    )

    assert exit_code == 0
    assert control.continuity == "hot"
    # The turn crossed as stdin, not as a new process.
    assert json.loads(process.sent[-1])["message"]["content"][0]["text"] == "do it"
    assert live_out["session"] is entry.live_session
    assert live_out["session_id"] == "sess-live"
    assert not process.killed


def test_the_first_live_turn_spawns_on_the_residency_grant(tmp_path):
    """No process yet: the turn spawns one over the resident workspace, on the
    residency's credential and with a live stdin — warm_native this turn
    (the process is fresh), hot from the next."""
    runtime = ClaudeCodeRuntime()
    process = _ResidentProcess()
    process.feed = [_init_line("sess-new"), _answer_line(), _result_line()]
    entry = _resident_entry(tmp_path)
    backend = _LiveSpawnBackend(process)
    control = FakeControl()
    live_out: dict = {}

    exit_code, _tail, _errors = run_agent(
        runtime,
        job=_hot_job(),
        workdir=entry.workdir,
        gateway_base_url="http://gw",
        control=control,
        heart=FakeHeart(),
        timeout_s=30,
        backend=backend,
        state_dir=entry.state_dir,
        resident=entry,
        residents=_DetachPool(),
        live_out=live_out,
    )

    assert exit_code == 0
    assert control.continuity == "warm_native"
    (spec,) = backend.specs
    assert spec.live_input is True
    assert spec.argv[spec.argv.index("--resume") + 1] == "sess-prev"
    assert live_out["session"] is not None
    assert live_out["session_id"] == "sess-new"
    assert not process.killed  # alive for the next turn


def test_a_model_switch_detaches_the_live_process_and_rides_warm(tmp_path):
    """The idle process answers for another model and this turn will advance
    the session past its memory: it goes now, the workspace stays."""

    class _Stale:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    entry = _resident_entry(tmp_path, runtime_id="generic", grant_model="other-model")
    entry.live_session = _Stale()
    pool = _DetachPool()
    control = FakeControl()

    exit_code, _tail, _errors = run_agent(
        _echo_runtime(2),
        job=_hot_job(runtime="generic"),
        workdir=entry.workdir,
        gateway_base_url="http://gw",
        control=control,
        heart=FakeHeart(),
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
        state_dir=entry.state_dir,
        resident=entry,
        residents=pool,
    )

    assert exit_code == 0
    assert pool.detached == ["t-hot"]
    assert control.continuity != "hot"


def test_a_fresh_live_session_is_closed_when_its_turn_fails(tmp_path):
    """The stream ends without a result: the process died mid-turn. A spawn
    this turn owns must not outlive its failure as an orphan holding a key."""
    runtime = ClaudeCodeRuntime()
    process = _ResidentProcess()
    process.feed = [_answer_line()]  # no result line: death
    entry = _resident_entry(tmp_path)
    control = FakeControl()
    live_out: dict = {}

    exit_code, _tail, _errors = run_agent(
        runtime,
        job=_hot_job(),
        workdir=entry.workdir,
        gateway_base_url="http://gw",
        control=control,
        heart=FakeHeart(),
        timeout_s=30,
        backend=_LiveSpawnBackend(process),
        state_dir=entry.state_dir,
        resident=entry,
        residents=_DetachPool(),
        live_out=live_out,
    )

    assert exit_code != 0
    assert "session" not in live_out
    assert process.killed and process.stdin_closed


def test_promotion_attaches_the_surviving_session_under_the_new_generation(tmp_path, monkeypatch):
    """run_once's half of the hand-off: whatever run_agent left in live_out is
    attached with the promoted generation — and closed when promotion
    declines, because a process without a residency has no owner."""
    import dataclasses

    job = dataclasses.replace(_JOB, thread_id="t-hot", base_sha=None)

    class _Session:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _Pool:
        def __init__(self, generation) -> None:
            self.generation = generation
            self.offered: list[dict] = []
            self.attached: list[tuple] = []

        def lookup(self, thread_id):
            return None

        def offer(self, **kw):
            self.offered.append(kw)
            return self.generation

        def attach_session(self, thread_id, generation, session):
            self.attached.append((thread_id, generation, session))
            return True

        def drop(self, *a, **kw):
            pass

    class _Control(FakeControl):
        def __init__(self) -> None:
            super().__init__()
            self.job_id = job.job_id

        def finish(self, state, detail=None, **_kw):
            self.finished = (state, detail)

    class _EchoBackend(ProcessBackend):
        def has_binary(self, name: str) -> bool:
            return True

    session = _Session()

    def _fake_run_agent(*_args, **kwargs):
        kwargs["live_out"]["session"] = session
        kwargs["live_out"]["session_id"] = "sess-9"
        return 0, "", []

    monkeypatch.setattr(runner_mod, "claim", lambda **_kw: job)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *a, **k: _Control())
    monkeypatch.setattr(runner_mod, "Heartbeater", lambda *a, **k: FakeHeart())
    monkeypatch.setattr(runner_mod, "existing_checkout_sha", lambda *a, **k: "abc")
    monkeypatch.setattr(runner_mod, "align_existing_checkout", lambda *a, **k: "abc")
    monkeypatch.setattr(runner_mod, "build_patch", lambda *a, **k: "")
    monkeypatch.setattr(runner_mod, "save_workspace_snapshot", lambda *a, **k: True)
    monkeypatch.setattr(runner_mod, "run_agent", _fake_run_agent)
    runtime = ClaudeCodeRuntime()
    monkeypatch.setattr(runner_mod, "get_runtime", lambda *a, **k: runtime)

    pool = _Pool(generation=7)
    code = runner_mod.run_once(
        base_url="http://cp",
        dispatcher_token="tok",
        worker_id="w1",
        workdir=str(tmp_path),
        backend=_EchoBackend(acknowledged_unsafe=True),
        residents=pool,  # type: ignore[arg-type]
    )
    assert code == 0
    assert pool.offered and pool.offered[0]["session_id"] == "sess-9"
    assert pool.attached == [("t-hot", 7, session)]
    assert not session.closed

    # Promotion declined: the survivor is closed, not leaked.
    orphan = _Session()

    def _fake_run_agent_orphan(*_args, **kwargs):
        kwargs["live_out"]["session"] = orphan
        kwargs["live_out"]["session_id"] = "sess-10"
        return 0, "", []

    monkeypatch.setattr(runner_mod, "run_agent", _fake_run_agent_orphan)
    declined = _Pool(generation=None)
    code = runner_mod.run_once(
        base_url="http://cp",
        dispatcher_token="tok",
        worker_id="w1",
        workdir=str(tmp_path),
        backend=_EchoBackend(acknowledged_unsafe=True),
        residents=declined,  # type: ignore[arg-type]
    )
    assert code == 0
    assert declined.attached == []
    assert orphan.closed


def test_promotion_rekeys_the_residency_on_a_model_switch(tmp_path):
    """D6 restart + §14: the grant is model-scoped, so refreshing in place
    after a model switch would pin the thread warm forever. The switch drops
    the old generation and establishes a successor keyed to the new model."""
    import dataclasses
    from types import SimpleNamespace

    entry = _resident_entry(tmp_path, grant_model="old-model", generation=4)

    class _Pool:
        def __init__(self) -> None:
            self.dropped: list[tuple[str, bool, int | None]] = []
            self.offered: list[dict] = []

        def lookup(self, thread_id):
            return entry if thread_id == "t-hot" else None

        def drop(self, thread_id, *, release, generation=None):
            self.dropped.append((thread_id, release, generation))

        def offer(self, **kw):
            self.offered.append(kw)
            return 5

    pool = _Pool()
    generation = runner_mod._promote_to_resident(
        FakeControl(),
        runtime=SimpleNamespace(name="claude-code", session_id="sess-2"),
        job=dataclasses.replace(_JOB, thread_id="t-hot", model="new-model"),
        backend=SimpleNamespace(),
        workdir=str(tmp_path),
        state_dir=f"{tmp_path}/state",
        residents=pool,  # type: ignore[arg-type]
    )

    assert pool.dropped == [("t-hot", True, 4)]
    assert generation == 5

    # A workspace-only residency (no grant) is refreshed, never re-keyed:
    # there is no model-scoped credential to go stale.
    entry = _resident_entry(tmp_path, credential=None, grant_model=None, generation=4)
    pool = _Pool()
    runner_mod._promote_to_resident(
        FakeControl(),
        runtime=SimpleNamespace(name="claude-code", session_id="sess-2"),
        job=dataclasses.replace(_JOB, thread_id="t-hot", model="new-model"),
        backend=SimpleNamespace(),
        workdir=str(tmp_path),
        state_dir=f"{tmp_path}/state",
        residents=pool,  # type: ignore[arg-type]
    )
    assert pool.dropped == []
