"""Unit tests for the retained-only agent runner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.runner import ClaimedJob, LeaseLost
from cloud_agent_host.runtimes import ClaudeCodeRuntime
from cloud_agent_host.sandbox import ContainerBackend, ProcessBackend, SandboxError

_JOB = ClaimedJob(
    job_id="ajob_1",
    attempt_id=7,
    attempt_no=1,
    repo="o/n",
    base_sha="a" * 40,
    task_prompt="do it",
    runtime="claude-code",
    model="model",
    worker_token="ajt.a.b",
    sandbox_token="grant",
    thread_id="athr_1",
    runtime_version=runner_mod.SHARED_SANDBOX_RUNTIME_PINS["claude-code"],
)


def _stamp() -> dict:
    return {
        "sandbox_image_digest": "sha256:shared-image",
        "shared_sandbox_protocol": runner_mod.SHARED_SANDBOX_PROTOCOL,
        "runtime_versions": dict(runner_mod.SHARED_SANDBOX_RUNTIME_PINS),
        "runtime_versions_available": {
            key: [value] for key, value in runner_mod.SHARED_SANDBOX_RUNTIME_PINS.items()
        },
        "helper_versions": dict(runner_mod.SHARED_SANDBOX_HELPER_PINS),
    }


def _configure_host(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_HOST_SLOTS", "3")
    monkeypatch.setenv("AGENT_RUNNER_REPLICAS", "3")
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_URL", "http://broker:8092")
    monkeypatch.setenv("AGENT_WORKSPACE_BROKER_TOKEN", "broker-token")


def test_shared_live_is_the_only_capability_without_feature_switches(monkeypatch):
    _configure_host(monkeypatch)
    # Old rollout variables are deliberately inert when stale values remain in
    # a deployment's .env after the cutover.
    monkeypatch.setenv("AGENT_RESTARTABLE_SANDBOX", "0")
    monkeypatch.setenv("AGENT_SHARED_LIVE_SANDBOX", "0")
    monkeypatch.setenv("AGENT_RESTARTABLE_RUNTIMES", "claude-code")

    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
    ]


def test_exact_deployment_capability_requires_the_completed_marker(monkeypatch, tmp_path):
    _configure_host(monkeypatch)
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "target-sha")
    ready_file = tmp_path / "shared-live-ready"
    monkeypatch.setenv("AGENT_SHARED_LIVE_READY_FILE", str(ready_file))

    ready_file.write_text("pending:target-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
    ]

    ready_file.write_text("different-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
    ]

    ready_file.write_text("target-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "shared-sandbox-deployment:target-sha",
    ]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_HOST_SLOTS", "1"),
        ("AGENT_HOST_SLOTS", "nope"),
        ("AGENT_RUNNER_REPLICAS", "1"),
        ("AGENT_RUNNER_REPLICAS", "nope"),
        ("AGENT_SHARED_RESIDENT_CAPACITY", "1"),
        ("AGENT_SHARED_RESIDENT_CAPACITY", "129"),
    ],
)
def test_shared_live_attestation_rejects_an_incomplete_host(monkeypatch, name, value):
    _configure_host(monkeypatch)
    monkeypatch.setenv(name, value)

    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == []


def test_shared_live_attestation_requires_every_pinned_runtime_and_helper(monkeypatch):
    _configure_host(monkeypatch)
    stamp = _stamp()
    stamp["runtime_versions"]["codex"] = "wrong"
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), stamp) == []

    stamp = _stamp()
    stamp["helper_versions"]["claude-agent-sdk"] = "wrong"
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), stamp) == []


def test_shared_live_attestation_refuses_a_non_container_backend(monkeypatch):
    _configure_host(monkeypatch)
    assert (
        runner_mod._shared_live_host_capabilities(
            ProcessBackend(acknowledged_unsafe=True), _stamp()
        )
        == []
    )


def test_chat_idle_timeout_has_one_canonical_setting(monkeypatch):
    monkeypatch.setenv("AGENT_SESSION_IDLE_TIMEOUT_S", "12")
    monkeypatch.delenv("AGENT_CHAT_IDLE_TIMEOUT_S", raising=False)
    assert runner_mod._chat_idle_timeout_s() == runner_mod.DEFAULT_CHAT_IDLE_TIMEOUT_S

    monkeypatch.setenv("AGENT_CHAT_IDLE_TIMEOUT_S", "34")
    assert runner_mod._chat_idle_timeout_s() == 34


def test_resume_requires_the_exact_native_session():
    assert (
        runner_mod._verified_native_session_id(
            previous_session_id="native-one",
            observed_session_id="native-one",
            has_context=True,
        )
        == "native-one"
    )
    with pytest.raises(SandboxError, match="resumed a different native session"):
        runner_mod._verified_native_session_id(
            previous_session_id="native-one",
            observed_session_id="native-two",
            has_context=True,
        )


def test_first_turn_requires_a_native_session_locator():
    with pytest.raises(SandboxError, match="without a native session locator"):
        runner_mod._verified_native_session_id(
            previous_session_id=None,
            observed_session_id=None,
            has_context=False,
        )


def test_run_once_dispatches_only_to_shared_live(monkeypatch):
    _configure_host(monkeypatch)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    claim_calls: list[dict] = []
    dispatched: list[dict] = []

    class Control:
        job_id = ""

        def finish(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    class Heart:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    def claim(**kwargs):
        claim_calls.append(kwargs)
        return _JOB

    monkeypatch.setattr(runner_mod, "claim", claim)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_args, **_kwargs: Control())
    monkeypatch.setattr(runner_mod, "Heartbeater", Heart)
    monkeypatch.setattr(
        runner_mod,
        "_run_shared_live_attempt",
        lambda **kwargs: dispatched.append(kwargs) or 0,
    )

    assert (
        runner_mod.run_once(
            base_url="http://control",
            gateway_url="http://gateway",
            host_credential="host-credential",
            worker_id="runner-1",
            backend=backend,
            host="host-a",
        )
        == 0
    )
    assert claim_calls[0]["attestation"]["runner_capabilities"] == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
    ]
    assert dispatched[0]["job"] is _JOB
    assert dispatched[0]["gateway_base_url"] == "http://gateway"


def test_run_once_empty_queue_is_clean(monkeypatch):
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: None)
    assert (
        runner_mod.run_once(
            base_url="http://control",
            host_credential="host-credential",
            worker_id="runner-1",
            backend=backend,
            host="host-a",
        )
        == 0
    )


def test_historical_one_to_one_sandbox_fails_closed():
    backend = ContainerBackend(image="img")
    backend.version_stamp = _stamp  # type: ignore[method-assign]
    finished: list[tuple[str, str | None]] = []
    job = ClaimedJob(
        **{
            **_JOB.__dict__,
            "thread_sandbox": {
                "sandbox_id": "asbx_old",
                "shared_live": False,
                "runtime_id": "claude-code",
                "execution_profile": "trusted-v1",
            },
        }
    )

    result = runner_mod._run_shared_live_attempt(
        runtime=ClaudeCodeRuntime(),
        job=job,
        control=SimpleNamespace(),
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=lambda state, detail=None, **_kwargs: finished.append((state, detail)),
    )

    assert result == 2
    assert finished == [("failed", "the control plane returned a non-shared sandbox mapping")]


class _Stream:
    def __init__(self, frames):
        self._frames = frames
        self.closed = False

    def frames(self):
        yield from self._frames

    def close(self):
        self.closed = True


class _Supervisor:
    def __init__(self, frames=()):
        self.stream = _Stream(frames)
        self.interrupted = False

    def start_shared_turn(self, *_args, **_kwargs):
        return self.stream

    def interrupt_shared(self, *_args, **_kwargs):
        self.interrupted = True


class _Control:
    def __init__(self):
        self.events = []

    def append_event(self, event):
        self.events.append((event.event_type, event.payload))


def test_shared_turn_cancellation_interrupts_the_live_cli():
    supervisor = _Supervisor()
    control = _Control()

    result = runner_mod._stream_shared_agent_turn(
        supervisor=supervisor,
        job=_JOB,
        control=control,
        heart=SimpleNamespace(cancel_requested=True, lease_lost=False),
        lease_token="lease",
        timeout_s=30,
    )

    assert result[:3] == (130, "", [])
    assert supervisor.interrupted is True
    assert ("lifecycle", {"phase": "cancelled_by_owner"}) in control.events
    assert supervisor.stream.closed is True


def test_shared_turn_lease_loss_interrupts_and_fences():
    supervisor = _Supervisor()
    with pytest.raises(LeaseLost):
        runner_mod._stream_shared_agent_turn(
            supervisor=supervisor,
            job=_JOB,
            control=_Control(),
            heart=SimpleNamespace(cancel_requested=False, lease_lost=True),
            lease_token="lease",
            timeout_s=30,
        )
    assert supervisor.interrupted is True
    assert supervisor.stream.closed is True


def test_shared_turn_streams_events_and_requires_a_result():
    supervisor = _Supervisor(
        [
            {
                "kind": "event",
                "event_type": "assistant",
                "payload": {"text": "done"},
                "raw_line": "raw\n",
            },
            {
                "kind": "result",
                "status": "completed",
                "exit_code": 0,
                "stderr": "",
            },
        ]
    )
    control = _Control()

    code, tail, errors, result = runner_mod._stream_shared_agent_turn(
        supervisor=supervisor,
        job=_JOB,
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        lease_token="lease",
        timeout_s=30,
    )

    assert (code, tail, errors) == (0, "raw\n", [])
    assert result is not None and result["status"] == "completed"
    assert control.events == [("assistant", {"text": "done"})]


def test_run_forever_refuses_to_enroll_a_host_that_cannot_attest(monkeypatch):
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "preflight", lambda **_kwargs: None)
    monkeypatch.setattr(runner_mod, "daemon_reachable", lambda: None)

    with pytest.raises(RuntimeError, match="cannot attest shared-sandbox-v1"):
        runner_mod.run_forever(
            base_url="http://control",
            gateway_url="http://gateway",
            worker_id="runner",
            backend=backend,
            host="host-a",
        )


def test_runner_source_contains_no_push_or_legacy_execution_path():
    source = __import__("inspect").getsource(runner_mod)
    assert "git push" not in source
    assert "def _run_retained_attempt" not in source
    assert "def run_agent" not in source
    assert "AGENT_RESTARTABLE_SANDBOX" not in source
    assert "AGENT_SHARED_LIVE_SANDBOX" not in source
