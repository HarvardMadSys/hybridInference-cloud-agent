"""Unit tests for the retained-only agent runner."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.runner import ClaimedJob, LeaseLost
from cloud_agent_host.runtimes import ClaudeCodeRuntime, get_runtime
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


def test_pending_deployment_heartbeats_but_does_not_poll(monkeypatch, tmp_path):
    _configure_host(monkeypatch)
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "target-sha")
    ready_file = tmp_path / "shared-live-ready"
    monkeypatch.setenv("AGENT_SHARED_LIVE_READY_FILE", str(ready_file))
    backend = ContainerBackend(image="img")

    ready_file.write_text("pending:target-sha\n")
    assert runner_mod.SHARED_SANDBOX_CAPABILITY in runner_mod._shared_live_host_capabilities(
        backend, _stamp()
    )
    assert runner_mod._shared_live_deployment_ready(backend, _stamp()) is False

    ready_file.write_text("target-sha\n")
    assert runner_mod._shared_live_deployment_ready(backend, _stamp()) is True


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


@pytest.mark.parametrize("helper_id", runner_mod.SHARED_SANDBOX_HELPER_PINS)
def test_shared_live_attestation_requires_every_pinned_runtime_and_helper(monkeypatch, helper_id):
    _configure_host(monkeypatch)
    stamp = _stamp()
    stamp["runtime_versions"]["codex"] = "wrong"
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), stamp) == []

    stamp = _stamp()
    stamp["helper_versions"][helper_id] = "wrong"
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), stamp) == []
    del stamp["helper_versions"][helper_id]
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


def test_reconstructed_running_group_reports_unknown_activity(monkeypatch):
    """A broker restart must not renew a full idle window on every beat."""

    class Supervisor:
        def list(self):
            return [
                {
                    "thread_id": "athr_reconstructed",
                    "sandbox_id": "asbx_reconstructed",
                    "container_id": "container-reconstructed",
                    "generation": 1,
                    "activation_generation": 0,
                    "running": True,
                    "shared_live": True,
                    "group_busy": False,
                    "terminal_count": 0,
                    "operation_count": 0,
                    "workspace_suspended": False,
                    "idle_deadline_monotonic": None,
                    "members": [],
                }
            ]

        def close(self):
            pass

    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", Supervisor)

    rows = runner_mod._retained_sandbox_heartbeat_rows()

    assert rows is not None
    assert rows[0]["activity_active"] is None
    assert rows[0]["idle_remaining_seconds"] is None


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


def _run_shared_boundary_case(
    monkeypatch, code, frame, runtime_id, observed_session_id="native-existing"
):
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    manager = Mock()
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    control = Mock()
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **seed,
        "thread_id": _JOB.thread_id,
        "generation": 1,
        "activation_generation": 3,
        "last_settled_job_id": "ajob_previous",
        "native_session_id": "native-existing",
        "resident_slot_id": 0,
    }
    control.mark_chat_sandbox_running.return_value = {}
    supervisor = Mock()
    supervisor.acquire_shared.return_value = {
        "token": "lease",
        "container_id": "same-container",
        "activation_generation": 3,
        "context_status": "live",
        "resident_state": "live",
    }
    supervisor.ensure_shared_resident.return_value = {
        "context_status": "live",
        "process_generation": 1,
    }
    supervisor.settle_shared.return_value = {"native_session_id": observed_session_id}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod, "_stream_shared_agent_turn", lambda **_kwargs: (code, "API error", [], frame)
    )
    finish = Mock()

    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime(runtime_id),
        job=replace(
            _JOB,
            runtime=runtime_id,
            runtime_version=runner_mod.SHARED_SANDBOX_RUNTIME_PINS[runtime_id],
        ),
        control=control,
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
    )

    return result, control, supervisor, finish


@pytest.mark.parametrize(
    ("code", "frame", "preserved"),
    [
        (1, {"kind": "result", "status": "failed", "native_turn_settled": True}, True),
        (1, {"kind": "result", "status": "failed"}, False),
        (1, {"kind": "result", "status": "failed", "native_turn_settled": False}, False),
        (1, {"kind": "error", "status": "failed", "native_turn_settled": True}, False),
        (124, {"kind": "result", "status": "failed", "native_turn_settled": True}, False),
        (130, {"kind": "result", "status": "cancelled", "native_turn_settled": True}, False),
    ],
)
@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_native_failure_preserves_only_a_verified_turn_boundary(
    monkeypatch, code, frame, preserved, runtime_id
):
    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch, code, frame, runtime_id
    )
    assert result == (0 if code == 130 else 1)
    assert finish.call_args.args[0] == ("cancelled" if code == 130 else "failed")
    if preserved:
        supervisor.settle_shared.assert_called_once()
        control.settle_chat_sandbox.assert_called_once()
        assert (
            control.settle_chat_sandbox.call_args.kwargs["native_session_id"] == "native-existing"
        )
        supervisor.abandon_shared.assert_not_called()
        control.abandon_chat_sandbox.assert_not_called()
    else:
        supervisor.settle_shared.assert_not_called()
        control.settle_chat_sandbox.assert_not_called()
        assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is True


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
@pytest.mark.parametrize("code", [0, 1])
def test_shared_runner_never_persists_a_different_session_returned_by_the_broker(
    monkeypatch, runtime_id, code
):
    frame = {
        "kind": "result",
        "status": "completed" if code == 0 else "failed",
        "native_turn_settled": True,
    }
    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch, code, frame, runtime_id, observed_session_id="unexpected-new-session"
    )
    assert result == 2
    assert finish.call_args.args[0] == "failed"
    assert "different native session" in finish.call_args.args[1]
    supervisor.settle_shared.assert_called_once()
    control.settle_chat_sandbox.assert_not_called()
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is True


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


@pytest.mark.parametrize(
    ("heart", "timeout_s", "expected_code"),
    [
        (SimpleNamespace(cancel_requested=True, lease_lost=False), 30.0, 130),
        (SimpleNamespace(cancel_requested=False, lease_lost=False), -1.0, 124),
    ],
)
def test_shared_interrupt_drains_the_brokers_terminal_resource_frame(
    heart,
    timeout_s,
    expected_code,
):
    resources = {
        "process_observer": "kata-guest-ebpf-v1",
        "process_observation_complete": True,
        "processes": [{"pid": 412}],
    }
    supervisor = _Supervisor(
        [
            {
                "kind": "result",
                "status": "cancelled",
                "exit_code": 130,
                "resources": resources,
            }
        ]
    )
    control = _Control()

    code, _tail, _errors, result = runner_mod._stream_shared_agent_turn(
        supervisor=supervisor,
        job=_JOB,
        control=control,
        heart=heart,
        lease_token="lease",
        timeout_s=timeout_s,
    )

    assert code == expected_code
    assert result is not None and result["resources"] is resources
    assert supervisor.interrupted is True
    if expected_code == 130:
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
