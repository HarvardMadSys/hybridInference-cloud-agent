"""Unit tests for the retained-only agent runner."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from cloud_agent_host import control_plane_client, runner as runner_mod
from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.runner import ClaimedJob, LeaseLost
from cloud_agent_host.runtimes import ClaudeCodeRuntime, get_runtime
from cloud_agent_host.sandbox import ContainerBackend, ProcessBackend, SandboxError
from cloud_agent_host.session_supervisor_client import SessionSupervisorError
from cloud_agent_shared import operation_profiling


@pytest.fixture(autouse=True)
def _reset_profiling_cache():
    """Keep the profiler's process-wide cache from leaking between tests."""
    operation_profiling.reset_cache()
    yield
    operation_profiling.reset_cache()


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
        "runtime_versions": dict(runner_mod.SHARED_SANDBOX_RUNTIME_PINS),
        "runtime_versions_available": {
            key: [value] for key, value in runner_mod.SHARED_SANDBOX_RUNTIME_PINS.items()
        },
        "helper_versions": dict(runner_mod.SHARED_SANDBOX_HELPER_PINS),
    }


def _configure_host(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_HOST_SLOTS", "3")
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", "http://sandbox_manager:8092")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "sandbox-manager-token")


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
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-restore-v1",
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
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-restore-v1",
    ]

    ready_file.write_text("different-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-restore-v1",
    ]

    ready_file.write_text("target-sha\n")
    assert runner_mod._shared_live_host_capabilities(ContainerBackend(image="img"), _stamp()) == [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-restore-v1",
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
        ("AGENT_HOST_SLOTS", "129"),
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


def test_a_claim_names_protocol_v2_for_a_control_plane_older_than_this_release(monkeypatch):
    """Hosts roll out before the API, so a replacement runner polls the old one.

    That control plane refuses every claim without the image's shared-sandbox
    protocol, which this image no longer labels. The runner states the v2 its
    image would have declared, and only once its own checks have passed.
    """
    _configure_host(monkeypatch)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    assert runner_mod._claim_attestation(backend)["shared_sandbox_protocol"] == "v2"

    def without_fork_helper() -> dict:
        stamp = _stamp()
        del stamp["helper_versions"]["claude-session-fork"]
        return stamp

    monkeypatch.setattr(backend, "version_stamp", without_fork_helper)
    refused = runner_mod._claim_attestation(backend)
    assert refused["runner_capabilities"] == []
    assert "shared_sandbox_protocol" not in refused


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
    """A sandbox manager restart must not renew a full idle window on every beat."""

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

    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda **_kwargs: Supervisor())

    rows = runner_mod._retained_sandbox_heartbeat_rows()

    assert rows is not None
    assert rows[0]["activity_active"] is None
    assert rows[0]["idle_remaining_seconds"] is None


@pytest.mark.parametrize("invalid_usage", [False, True])
@pytest.mark.parametrize("terminal_count", [2, 4096, 4097])
def test_retained_heartbeat_passes_group_resources_and_terminal_count(
    monkeypatch, invalid_usage, terminal_count
):
    sample = {
        "sample_id": "resource-sample",
        "sample_age_ms": 4000,
        "memory_bytes": "invalid" if invalid_usage else 1024,
        "cpu_cores": 1.5,
    }
    supervisor = SimpleNamespace(
        list=lambda: [
            {
                "thread_id": "athr_source",
                "sandbox_id": "asbx_source",
                "container_id": "container-source",
                "generation": 1,
                "activation_generation": 1,
                "running": True,
                "shared_live": True,
                "terminal_count": terminal_count,
                "members": [],
                "resource_usage": sample,
            }
        ],
        close=Mock(),
    )
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda **_kwargs: supervisor)
    rows = runner_mod._retained_sandbox_heartbeat_rows()
    assert rows is not None
    assert rows[0]["container_id"] == "container-source"
    assert rows[0]["activity_active"] is True
    assert rows[0]["terminal_count"] == (terminal_count if terminal_count <= 4096 else None)
    if invalid_usage:
        assert rows[0]["resource_usage"] is None
    else:
        assert rows[0]["resource_usage"]["sample_id"] == "resource-sample"
        assert rows[0]["resource_usage"]["sample_age_ms"] == 4000
        assert rows[0]["resource_usage"]["memory_bytes"] == 1024
        assert rows[0]["resource_usage"]["scope"] == "container"
    supervisor.close.assert_called_once()


def test_a_sandbox_manager_that_never_answers_costs_the_host_beat_only_its_observation(
    monkeypatch,
):
    """The observation runs on the host-heartbeat thread, ahead of the beat itself.

    It used the sandbox manager client's 180-second ceiling, so a sandbox
    manager that accepted the request and never answered held the beat for
    three minutes -- twice the silence after which the control plane counts
    the host offline and refuses its claims.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    # Connections complete in the backlog and are never read or answered.
    listener.listen(8)
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "sandbox-manager-token")
    monkeypatch.setattr(runner_mod, "_HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S", 0.2)
    observed: list[Any] = []
    beat = threading.Thread(
        target=lambda: observed.append(runner_mod._retained_sandbox_heartbeat_rows()),
        daemon=True,
    )
    try:
        beat.start()
        beat.join(timeout=10)
        assert not beat.is_alive(), "the host beat waited on the sandbox manager"
        # "Not observed this beat", never an authoritative empty list.
        assert observed == [None]
    finally:
        listener.close()


def test_residency_cleanup_waits_on_a_silent_sandbox_manager_no_longer_than_the_beat(
    monkeypatch,
):
    """The cleanup a beat's answer asks for runs on the same heartbeat thread.

    It still used the 180-second sandbox manager client, so a sandbox manager
    that never answered held the next beat for three minutes and the host went
    offline.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_URL", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "sandbox-manager-token")
    monkeypatch.setattr(runner_mod, "_HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S", 0.2)
    beat = threading.Thread(
        target=runner_mod._handle_host_residencies_lost,
        args=([{"thread_id": "athr_1", "generation": 3}],),
        daemon=True,
    )
    try:
        beat.start()
        beat.join(timeout=10)
        assert not beat.is_alive(), "the host beat waited on the sandbox manager"
    finally:
        listener.close()


def test_residency_cleanup_leaves_the_rest_to_a_later_beat_once_its_time_is_spent(
    monkeypatch,
):
    """A slow sandbox manager gets one call per beat, not one ceiling per lost member.

    Each verdict is named again by every beat while the sandbox manager still
    reports the member live, so nothing is lost by stopping; waiting out one
    ceiling per member would hold the next beat past the host's offline
    threshold.
    """
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: clock.now)
    members = [{"thread_id": f"athr_{index}"} for index in range(3)]
    fenced: list[tuple[str, int]] = []

    class SandboxManager:
        def list(self):
            return [{"members": members}]

        def shared_residency_lost(self, thread_id, *, generation):
            fenced.append((thread_id, generation))
            # A call that used the whole ceiling before it was answered.
            clock.now += runner_mod._HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S
            raise SessionSupervisorError(503, "shared Session Supervisor is unavailable")

        def close(self):
            pass

    timeouts: list[float | None] = []

    def sandbox_manager(*, timeout=None):
        timeouts.append(timeout)
        return SandboxManager()

    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", sandbox_manager)

    runner_mod._handle_host_residencies_lost(
        [{"thread_id": f"athr_{index}", "generation": index + 1} for index in range(3)]
    )

    assert timeouts == [runner_mod._HOST_BEAT_SANDBOX_MANAGER_TIMEOUT_S]
    assert fenced == [("athr_0", 1)]


def test_residency_cleanup_fences_every_named_member_a_prompt_sandbox_manager_holds(
    monkeypatch,
):
    fenced: list[tuple[str, int]] = []
    sandbox_manager = SimpleNamespace(
        list=lambda: [{"members": [{"thread_id": "athr_1"}, {"thread_id": "athr_2"}]}],
        shared_residency_lost=lambda thread_id, *, generation: fenced.append(
            (thread_id, generation)
        ),
        close=Mock(),
    )
    monkeypatch.setattr(
        runner_mod, "_session_supervisor_from_env", lambda **_kwargs: sandbox_manager
    )

    runner_mod._handle_host_residencies_lost(
        [
            {"thread_id": "athr_1", "generation": 4},
            {"thread_id": "athr_gone", "generation": 1},
            {"thread_id": "athr_2", "generation": 7},
        ]
    )

    assert fenced == [("athr_1", 4), ("athr_2", 7)]
    sandbox_manager.close.assert_called_once()


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
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "sandbox-archive-settings-v1",
        "sandbox-checkpoints-v1",
        "sandbox-restore-v1",
        "environment-v1",
        "environment-snapshot-v1",
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


@pytest.mark.parametrize(
    "error", [SandboxError("image inspection failed"), httpx.ReadTimeout("timeout")]
)
@pytest.mark.parametrize("finish_unavailable", [False, True])
def test_preparation_error_outside_shared_handler_finishes_before_stopping_heartbeat(
    monkeypatch, error, finish_unavailable, caplog
):
    """Post-claim setup errors must not leave a running lease with no heartbeater."""
    _configure_host(monkeypatch)
    backend = ContainerBackend(image="img")
    # The first inspection builds claim attestation; the second is post-claim.
    monkeypatch.setattr(backend, "version_stamp", Mock(side_effect=[_stamp(), error]))
    monkeypatch.setattr(runner_mod, "claim", Mock(return_value=_JOB))
    control = Mock()
    heart = Mock()
    order = []

    def finish(*args, **kwargs):
        order.append("finish")
        if finish_unavailable:
            raise httpx.ReadTimeout("finish unavailable")

    control.finish.side_effect = finish
    heart.stop.side_effect = lambda: order.append("stop")
    monkeypatch.setattr(runner_mod, "ControlPlane", Mock(return_value=control))
    monkeypatch.setattr(runner_mod, "Heartbeater", Mock(return_value=heart))

    code = runner_mod.run_once(
        base_url="http://control",
        host_credential="host-credential",
        worker_id="runner-1",
        backend=backend,
        host="host-a",
    )

    assert code == 2
    assert order == ["finish", "stop"]
    control.finish.assert_called_once()
    assert control.finish.call_args.args[0] == "failed"
    assert type(error).__name__ in control.finish.call_args.args[1]
    assert "operation=attempt_dispatch" in caplog.text
    if finish_unavailable:
        assert "operation=finish" in caplog.text
    heart.start.assert_called_once()
    control.close.assert_called_once()


def test_preparation_lease_rejection_does_not_attempt_terminal_write(monkeypatch):
    """An already fenced preparation path cannot overwrite a newer owner."""
    _configure_host(monkeypatch)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(
        backend, "version_stamp", Mock(side_effect=[_stamp(), LeaseLost("expired")])
    )
    monkeypatch.setattr(runner_mod, "claim", Mock(return_value=_JOB))
    control = Mock()
    heart = Mock()
    monkeypatch.setattr(runner_mod, "ControlPlane", Mock(return_value=control))
    monkeypatch.setattr(runner_mod, "Heartbeater", Mock(return_value=heart))

    assert (
        runner_mod.run_once(
            base_url="http://control",
            host_credential="host-credential",
            worker_id="runner-1",
            backend=backend,
            host="host-a",
        )
        == 3
    )

    control.finish.assert_not_called()
    heart.stop.assert_called_once()
    control.close.assert_called_once()


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
    monkeypatch,
    code,
    frame,
    runtime_id,
    observed_session_id="native-existing",
    configure=None,
    measurement=None,
    container_action="unknown",
    resident_action="unknown",
    lease_stages=None,
    resident_stages=None,
    sandbox_manager_records=True,
    sandbox_manager_truncated=False,
    thread_sandbox=None,
    heart=None,
    job_changes=None,
):
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    manager = Mock()
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
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
    control.mark_chat_sandbox_running.return_value = {"residency_generation": 11}
    supervisor = Mock()

    def acquire_shared(_thread_id, **kwargs):
        context = kwargs.get("profiling") or {}
        return {
            "token": "lease",
            "container_id": "same-container",
            "activation_generation": 3,
            "context_status": "live",
            "resident_state": "live",
            # What the sandbox manager reports it did to the container.
            "container_action": container_action,
            "stages": list(lease_stages or []),
            # Echoed like the real sandbox manager: which call this answers, and whether
            # this process collected for it at all.
            "profiling_call_id": context.get("call_id"),
            "profiling_recorded": sandbox_manager_records,
            "profiling_truncated": sandbox_manager_truncated,
        }

    def ensure_shared_resident(_thread_id, **kwargs):
        context = kwargs.get("profiling") or {}
        return {
            "context_status": "live",
            "process_generation": 1,
            "resident_action": resident_action,
            "stages": list(resident_stages or []),
            "profiling_call_id": context.get("call_id"),
            "profiling_recorded": sandbox_manager_records,
            "profiling_truncated": sandbox_manager_truncated,
        }

    supervisor.acquire_shared.side_effect = acquire_shared
    supervisor.ensure_shared_resident.side_effect = ensure_shared_resident
    supervisor.settle_shared.return_value = {"native_session_id": observed_session_id}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod,
        "_stream_shared_agent_turn",
        lambda **_kwargs: (code, "API error", [], frame, None),
    )
    finish = Mock()
    if configure is not None:
        configure(control, supervisor, finish)

    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime(runtime_id),
        job=replace(
            _JOB,
            runtime=runtime_id,
            runtime_version=runner_mod.SHARED_SANDBOX_RUNTIME_PINS[runtime_id],
            thread_sandbox=thread_sandbox,
            **(job_changes or {}),
        ),
        control=control,
        heart=heart or SimpleNamespace(cancel_requested=False, lease_lost=False),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
        measurement=measurement,
    )

    return result, control, supervisor, finish


@pytest.mark.parametrize("follow_up", [False, True])
def test_only_the_runner_creating_a_group_reports_its_fork_helper_version(monkeypatch, follow_up):
    """Fork asks a group what its own image speaks, not what the host runs by then."""
    existing = None
    if follow_up:
        spec = ChatSandboxSpec(
            thread_id=_JOB.thread_id,
            sandbox_id="asbx_existing",
            generation=1,
            runtime_id="claude-code",
        )
        existing = {
            "shared_live": True,
            "sandbox_id": spec.sandbox_id,
            "origin_thread_id": spec.thread_id,
            "generation": spec.generation,
            "host": "host-a",
            "container_name": spec.container_name,
            "runtime_id": spec.runtime_id,
            "runtime_version": runner_mod.SHARED_SANDBOX_RUNTIME_PINS["claude-code"],
            "sandbox_image_digest": "sha256:the-image-it-was-created-from",
            "execution_profile": "trusted-v1",
        }

    _result, control, _supervisor, _finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", thread_sandbox=existing
    )

    reservation = control.reserve_chat_sandbox.call_args.args[0]
    assert reservation["fork_helper_version"] == (
        None if follow_up else runner_mod.SHARED_SANDBOX_HELPER_PINS[runner_mod.FORK_HELPER]
    )


@pytest.mark.parametrize("verified", [True, False, None])
def test_a_turn_relies_on_the_bridge_proof_its_sandbox_manager_states(monkeypatch, verified):
    """One bridge check per turn: the sandbox manager's, when its lease says the call made one.

    ``None`` is a sandbox manager from before the field, whose turn checks as it always
    did. The check before the CLI starts goes through the turn's backend either
    way, which is what decides whether that proof still stands.
    """
    built: list[tuple[dict[str, Any], Any]] = []

    def configure(_control, supervisor, _finish):
        answer = supervisor.acquire_shared.side_effect

        def acquire_shared(*args, **kwargs):
            lease = answer(*args, **kwargs)
            if verified is not None:
                lease["network_verified"] = verified
            return lease

        supervisor.acquire_shared.side_effect = acquire_shared
        stub = runner_mod.RetainedChatBackend

        def backend(*args, **kwargs):
            retained = stub(*args, **kwargs)
            built.append((kwargs, retained))
            return retained

        monkeypatch.setattr(runner_mod, "RetainedChatBackend", backend)

    _run_shared_boundary_case(monkeypatch, 0, None, "claude-code", configure=configure)

    [(kwargs, retained)] = built
    if verified is True:
        assert isinstance(kwargs["network_proved_at"], float)
    else:
        assert kwargs["network_proved_at"] is None
    assert retained.require_network.call_count == 1
    assert set(retained.require_network.call_args.kwargs) == {"observer"}


@pytest.mark.parametrize(
    "failure", [SandboxError("setup failed"), httpx.ReadTimeout("setup stalled")]
)
def test_error_upload_failure_does_not_skip_terminal_transition(monkeypatch, failure):
    """An unavailable event endpoint must not strand a failed attempt as running."""

    def configure(control, supervisor, finish):
        control.suspend_terminals.side_effect = failure

        def append(event):
            if event.event_type == "error":
                raise httpx.ReadTimeout("error upload failed")

        control.append_event.side_effect = append

    result, control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", configure=configure
    )

    assert result == 2
    finish.assert_called_once()
    assert finish.call_args.args[0] == "failed"
    assert str(failure) in finish.call_args.args[1]
    control.admit_chat_turn.assert_not_called()
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is False


def test_a_stop_before_admission_spends_no_prompt_and_keeps_the_context(monkeypatch):
    """A Stop that arrives while the turn is resuming is honoured before admission.

    It used to be read only inside the stream, after the prompt had been
    admitted and dispatched: the turn spent tokens, was interrupted, and its
    intact conversation was abandoned as lost.
    """
    stream = Mock()

    def configure(_control, _supervisor, _finish):
        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch,
        0,
        None,
        "claude-code",
        configure=configure,
        heart=SimpleNamespace(cancel_requested=True, lease_lost=False),
    )

    assert result == 0
    control.admit_chat_turn.assert_not_called()
    stream.assert_not_called()
    assert supervisor.abandon_shared.call_args.kwargs["lost"] is False
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is False
    finish.assert_called_once()
    assert finish.call_args.args[:2] == ("cancelled", "cancelled by owner")
    supervisor.settle_shared.assert_not_called()


def test_a_lease_lost_before_admission_admits_nothing(monkeypatch):
    stream = Mock()

    def configure(_control, _supervisor, _finish):
        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch,
        0,
        None,
        "claude-code",
        configure=configure,
        heart=SimpleNamespace(cancel_requested=True, lease_lost=True),
    )

    assert result == 3
    control.admit_chat_turn.assert_not_called()
    stream.assert_not_called()
    finish.assert_not_called()
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is False


def _probe_then_replace(control, supervisor, *, residency: dict) -> None:
    """The live process refuses the probe, so a residency is established anew."""
    control.establish_shared_residency.return_value = {
        "credential": "grant-2",
        "generation": 12,
        "model": _JOB.model,
        **residency,
    }
    answer = supervisor.ensure_shared_resident.side_effect

    def refuse_first(*args, **kwargs):
        if supervisor.ensure_shared_resident.call_count == 1:
            raise SessionSupervisorError(503, "resident process restart requires a credential")
        return answer(*args, **kwargs)

    supervisor.ensure_shared_resident.side_effect = refuse_first


@pytest.mark.parametrize(
    ("residency", "opened_with"),
    [
        ({"mcp_credential": "amr.residency-12"}, "amr.residency-12"),
        # A control plane from before residency credentials: this turn's own token.
        ({}, "ajt.turn-token"),
    ],
)
def test_a_live_probe_carries_no_mcp_credential_and_a_new_process_gets_one(
    monkeypatch, residency, opened_with
):
    """The probe leaves the held credential to the process; a new one gets the residency's."""
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://relay")
    result, _control, supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        0,
        None,
        "claude-code",
        configure=lambda control, supervisor, _finish: _probe_then_replace(
            control, supervisor, residency=residency
        ),
        job_changes={"mcp_bindings": [":platform:memory"], "mcp_token": "ajt.turn-token"},
    )

    assert result == 0
    probe, opened = (call.kwargs["launch"] for call in supervisor.ensure_shared_resident.mock_calls)
    assert (probe.mcp_base_url, probe.mcp_credential, probe.credential) == (
        "http://relay",
        "",
        None,
    )
    assert (opened.mcp_credential, opened.credential) == (opened_with, "grant-2")
    assert opened.residency_generation == 12


def test_a_turn_without_mcp_opens_its_process_with_no_mcp_credential(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://relay")
    _result, _control, supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        0,
        None,
        "claude-code",
        configure=lambda control, supervisor, _finish: _probe_then_replace(
            control, supervisor, residency={"mcp_credential": "amr.residency-12"}
        ),
        job_changes={"mcp_bindings": [], "mcp_token": "ajt.turn-token"},
    )

    opened = supervisor.ensure_shared_resident.mock_calls[-1].kwargs["launch"]
    assert (opened.mcp_base_url, opened.mcp_credential) == ("", "")


def test_a_stop_during_the_container_start_prepares_nothing_more(monkeypatch):
    """A Stop seen once the container is up is honoured before the workspace work.

    The heartbeat now keeps renewing after a Stop, so a turn that only looked
    for it right before admission went through every remaining startup step
    first, and the owner waited for all of them to see the cancel.
    """
    heart = SimpleNamespace(cancel_requested=False, lease_lost=False)

    def configure(_control, supervisor, _finish):
        acquire = supervisor.acquire_shared.side_effect

        def stopped_while_starting(*args, **kwargs):
            heart.cancel_requested = True
            return acquire(*args, **kwargs)

        supervisor.acquire_shared.side_effect = stopped_while_starting

    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", configure=configure, heart=heart
    )

    assert result == 0
    control.suspend_terminals.assert_not_called()
    supervisor.ensure_shared_resident.assert_not_called()
    control.admit_chat_turn.assert_not_called()
    assert supervisor.abandon_shared.call_args.kwargs["lost"] is False
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is False
    finish.assert_called_once()
    assert finish.call_args.args[:2] == ("cancelled", "cancelled by owner")


def test_a_stop_during_the_resident_start_is_honoured_right_before_admission(monkeypatch):
    """The last startup step can carry the Stop too; the prompt is still never admitted."""
    heart = SimpleNamespace(cancel_requested=False, lease_lost=False)

    def configure(_control, supervisor, _finish):
        start = supervisor.ensure_shared_resident.side_effect

        def stopped_while_launching(*args, **kwargs):
            heart.cancel_requested = True
            return start(*args, **kwargs)

        supervisor.ensure_shared_resident.side_effect = stopped_while_launching

    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", configure=configure, heart=heart
    )

    assert result == 0
    supervisor.ensure_shared_resident.assert_called_once()
    control.admit_chat_turn.assert_not_called()
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is False
    assert finish.call_args.args[:2] == ("cancelled", "cancelled by owner")


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [(httpx.ReadTimeout("completion notice unavailable"), 0), (LeaseLost("expired"), 3)],
)
def test_settled_success_survives_a_failed_completion_event_upload(
    monkeypatch, error, expected_code
):
    """A completed native turn must not fail because its final notice is unavailable."""

    def configure(control, supervisor, finish):
        def append(event):
            if event.payload.get("phase") == "shared_workspace_settled":
                raise error

        control.append_event.side_effect = append

    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", configure=configure
    )

    assert result == expected_code
    supervisor.settle_shared.assert_called_once()
    control.settle_chat_sandbox.assert_called_once()
    assert control.record_chat_process.call_args.kwargs["residency_generation"] == 11
    assert control.admit_chat_turn.call_args.kwargs["residency_generation"] == 11
    assert control.settle_chat_sandbox.call_args.kwargs["residency_generation"] == 11
    control.abandon_chat_sandbox.assert_not_called()
    if expected_code == 0:
        finish.assert_called_once()
        assert finish.call_args.args[0] == "succeeded"
    else:
        finish.assert_not_called()


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://control.internal/v1/agent/worker/jobs/ajob_1/finish")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status} for {request.url}", request=request, response=response)


@pytest.mark.parametrize(
    ("code", "frame", "state", "expected_code"),
    [
        (0, {"kind": "result", "status": "completed"}, "succeeded", 0),
        (1, {"kind": "result", "status": "failed", "native_turn_settled": True}, "failed", 1),
    ],
)
@pytest.mark.parametrize(
    "failures",
    [
        [_status_error(503)],
        [httpx.ReadTimeout("finish answer lost"), _status_error(502)],
        [httpx.ReadTimeout("down")] * 3,
        [_status_error(500)],
    ],
)
def test_a_settled_turn_is_never_reported_failed_because_its_finish_was(
    monkeypatch, caplog, code, frame, state, expected_code, failures
):
    """Settlement decided the outcome; a finish error is retried, never downgraded.

    The closing transition used to be one request: a 503 there fell into the
    generic handler, which reported the settled, successful turn as failed --
    with the control plane's URL in the owner's detail.
    """
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _seconds: None)
    pending = list(failures)

    def configure(_control, _supervisor, finish):
        def answer(*_args, **_kwargs):
            if pending:
                raise pending.pop(0)

        finish.side_effect = answer

    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch, code, frame, "claude-code", configure=configure
    )

    assert result == expected_code
    supervisor.settle_shared.assert_called_once()
    control.settle_chat_sandbox.assert_called_once()
    supervisor.abandon_shared.assert_not_called()
    control.abandon_chat_sandbox.assert_not_called()
    # Every try reports the settled outcome itself, with its own detail.
    assert {call.args[0] for call in finish.call_args_list} == {state}
    assert len({call.args[1] for call in finish.call_args_list}) == 1
    assert "control.internal" not in str(finish.call_args_list)
    # A 500 is the control plane refusing the transition, not a lost answer.
    retried = not any(
        isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 500
        for error in failures
    )
    assert finish.call_count == (min(len(failures) + 1, 3) if retried else 1)
    if finish.call_count == len(failures):
        assert "operation=finish " in caplog.text
    for call in control.append_event.call_args_list:
        assert call.args[0].event_type != "error"


def test_a_settled_turn_whose_finish_is_fenced_on_retry_reports_nothing_more(monkeypatch):
    """A retry refused by the fence is usually the first try having landed."""
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _seconds: None)

    def configure(_control, _supervisor, finish):
        finish.side_effect = [httpx.ReadTimeout("answer lost"), LeaseLost("finished")]

    result, control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch,
        0,
        {"kind": "result", "status": "completed"},
        "claude-code",
        configure=configure,
    )

    assert result == 3
    assert [call.args[0] for call in finish.call_args_list] == ["succeeded", "succeeded"]
    control.abandon_chat_sandbox.assert_not_called()


@pytest.mark.parametrize(
    ("admitted", "code", "heart"),
    [
        (False, 0, SimpleNamespace(cancel_requested=True, lease_lost=False)),
        (True, 130, None),
    ],
    ids=["stop_before_admission", "stop_after_admission"],
)
@pytest.mark.parametrize(
    "failures",
    [
        [_status_error(503)],
        [httpx.ReadTimeout("finish answer lost"), _status_error(502)],
        [httpx.ReadTimeout("down")] * 3,
    ],
)
def test_a_stopped_turn_is_never_reported_failed_because_its_finish_was(
    monkeypatch, admitted, code, heart, failures
):
    """The owner's Stop decided the outcome; a finish error is retried, never downgraded.

    Both cancel paths finished with one request: a 503 there fell into the
    generic handler, which reported the cancelled turn as a failure -- with the
    control plane's URL in the owner's detail.
    """
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _seconds: None)
    pending = list(failures)

    def configure(_control, _supervisor, finish):
        def answer(*_args, **_kwargs):
            if pending:
                raise pending.pop(0)

        finish.side_effect = answer

    result, control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch,
        code,
        {"kind": "result", "status": "cancelled"},
        "claude-code",
        configure=configure,
        heart=heart,
    )

    assert result == 0
    assert {call.args[:2] for call in finish.call_args_list} == {
        ("cancelled", "cancelled by owner")
    }
    assert finish.call_count == min(len(failures) + 1, 3)
    assert control.admit_chat_turn.called is admitted
    control.abandon_chat_sandbox.assert_called_once()
    assert control.abandon_chat_sandbox.call_args.kwargs["lost"] is admitted
    for call in control.append_event.call_args_list:
        assert call.args[0].event_type != "error"


def test_a_control_plane_error_reaches_the_owner_without_its_address(monkeypatch):
    """httpx names the URL it called; the owner's failure detail must not."""

    def configure(control, _supervisor, _finish):
        control.suspend_terminals.side_effect = _status_error(503)

    result, control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch,
        0,
        {"kind": "result", "status": "completed"},
        "claude-code",
        configure=configure,
    )

    assert result == 2
    finish.assert_called_once()
    state, detail = finish.call_args.args[:2]
    assert state == "failed"
    assert detail == "shared live chat failed unexpectedly: the control plane answered HTTP 503"
    reported = [call.args[0].payload for call in control.append_event.call_args_list]
    assert {"text": detail} in reported
    assert "control.internal" not in str(finish.call_args_list) + str(reported)


@pytest.mark.parametrize(
    ("error", "described"),
    [
        (_status_error(503), "the control plane answered HTTP 503"),
        # A transport error names no address, and its text is what it always was.
        (httpx.ConnectError("[Errno 111] Connection refused"), "[Errno 111] Connection refused"),
    ],
)
def test_a_refused_reservation_reaches_the_owner_without_the_address(monkeypatch, error, described):
    def configure(control, _supervisor, _finish):
        control.reserve_chat_sandbox.side_effect = error

    result, _control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", configure=configure
    )

    assert result == 2
    finish.assert_called_once()
    state, detail = finish.call_args.args[:2]
    assert state == "failed"
    assert detail == f"could not reserve shared chat sandbox: {described}"
    assert "control.internal" not in detail


@pytest.mark.parametrize("stage", ["admission", "supervisor_settle", "control_settle"])
def test_lost_lifecycle_ack_recovers_without_repeating_the_agent_turn(monkeypatch, stage):
    from cloud_agent_host import runner as runner_mod

    monkeypatch.setattr(runner_mod.time, "sleep", lambda _seconds: None)
    stream = Mock(return_value=(0, "", [], {"kind": "result", "status": "completed"}, None))

    def configure(control, supervisor, finish):
        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)
        if stage == "admission":
            control.admit_chat_turn.side_effect = [httpx.ReadTimeout("ack lost"), None]
        elif stage == "supervisor_settle":
            supervisor.settle_shared.side_effect = [
                runner_mod.SessionSupervisorError(503, "ack lost"),
                {"native_session_id": "native-existing"},
            ]
        else:
            control.settle_chat_sandbox.side_effect = [httpx.ReadTimeout("ack lost"), {}]

    result, control, supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 0, None, "claude-code", configure=configure
    )
    assert result == 0
    stream.assert_called_once()
    assert control.admit_chat_turn.call_count == (2 if stage == "admission" else 1)
    assert supervisor.settle_shared.call_count == (2 if stage == "supervisor_settle" else 1)
    assert control.settle_chat_sandbox.call_count == (2 if stage == "control_settle" else 1)
    supervisor.abandon_shared.assert_not_called()
    control.abandon_chat_sandbox.assert_not_called()
    finish.assert_called_once()
    assert finish.call_args.args[0] == "succeeded"


@pytest.mark.parametrize("status", [409, 410, 422, 503])
def test_receipt_retry_is_bounded_and_does_not_retry_rejected_fences(monkeypatch, status):
    from cloud_agent_host import runner as runner_mod

    sleep = Mock()
    monkeypatch.setattr(runner_mod.time, "sleep", sleep)
    error = runner_mod.SessionSupervisorError(status, "rejected")
    request = Mock(side_effect=error)
    with pytest.raises(runner_mod.SessionSupervisorError):
        runner_mod._retry_lifecycle_receipt("settle", "ajob_test", request)
    assert request.call_count == (3 if status == 503 else 1)
    assert sleep.call_count == (2 if status == 503 else 0)


def test_control_lease_rejection_never_retries_settlement(monkeypatch):
    from cloud_agent_host import runner as runner_mod

    request = Mock(side_effect=LeaseLost("superseded"))
    with pytest.raises(LeaseLost):
        runner_mod._retry_lifecycle_receipt("settle", "ajob_test", request)
    request.assert_called_once()


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


@pytest.mark.parametrize("settled", [True, False])
@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_a_failed_turn_is_explained_by_what_it_reported(monkeypatch, settled, runtime_id):
    """The CLI's own reason beats the end of its output, settled or not."""
    frame = {"kind": "result", "status": "failed", "native_turn_settled": settled}
    reported = "agent reported an error: API Error: Stream idle timeout - no chunks received"

    def configure(_control, _supervisor, _finish):
        monkeypatch.setattr(
            runner_mod,
            "_stream_shared_agent_turn",
            lambda **_kwargs: (1, '"uuid":"tail-of-a-json-result"}\n', [], frame, reported),
        )

    result, _control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 1, frame, runtime_id, configure=configure
    )

    assert result == 1
    assert finish.call_args.args[:2] == ("failed", reported)


@pytest.mark.parametrize(
    ("settled", "expected"),
    [
        (True, "agent failed at native turn boundary: API error"),
        (False, "agent exited 1: API error"),
    ],
)
def test_a_failed_turn_that_reported_nothing_keeps_its_output_tail(monkeypatch, settled, expected):
    frame = {"kind": "result", "status": "failed", "native_turn_settled": settled}

    _result, _control, _supervisor, finish = _run_shared_boundary_case(
        monkeypatch, 1, frame, "claude-code"
    )

    assert finish.call_args.args[:2] == ("failed", expected)


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
@pytest.mark.parametrize("code", [0, 1])
def test_shared_runner_never_persists_a_different_session_returned_by_the_sandbox_manager(
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
def test_shared_interrupt_drains_the_sandbox_managers_terminal_resource_frame(
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

    code, _tail, _errors, result, _reported = runner_mod._stream_shared_agent_turn(
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


def _run_frames(frames):
    return runner_mod._stream_shared_agent_turn(
        supervisor=_Supervisor(frames),
        job=_JOB,
        control=_Control(),
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        lease_token="lease",
        timeout_s=30,
    )


def test_a_turn_reports_its_clis_last_error_as_the_reason():
    """A Codex turn refused with 403 printed a config warning last."""
    *_, reported = _run_frames(
        [
            {
                "kind": "event",
                "event_type": "error",
                "payload": {"text": "an earlier error"},
                "raw_line": "raw\n",
            },
            {
                "kind": "event",
                "event_type": "error",
                "payload": {
                    "text": "unexpected status 403 Forbidden: This account has no active API key"
                },
                "raw_line": "raw\n",
            },
            {"kind": "result", "status": "failed", "exit_code": 1, "stderr": "WARN bubblewrap"},
        ]
    )

    assert reported == (
        "agent reported an error: unexpected status 403 Forbidden: "
        "This account has no active API key"
    )


def test_a_turn_reports_the_sandboxs_failure_when_the_cli_reported_none():
    code, tail, _errors, _result, reported = _run_frames(
        [
            {"kind": "event", "event_type": "message", "payload": {"text": "done"}},
            {"kind": "error", "detail": "shared member activation boundary failed: chmod"},
        ]
    )

    assert code == 1
    assert "shared member activation boundary failed" in tail
    assert reported == "sandbox failed: shared member activation boundary failed: chmod"


def test_a_turn_that_reported_no_error_has_no_reason():
    *_, reported = _run_frames(
        [
            {"kind": "event", "event_type": "error", "payload": {"text": "  "}},
            {"kind": "result", "status": "completed", "exit_code": 0, "stderr": ""},
        ]
    )

    assert reported is None


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi", "opencode", "kilo"])
def test_shared_turn_streams_events_and_requires_a_result(runtime_id):
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

    code, tail, errors, result, _reported = runner_mod._stream_shared_agent_turn(
        supervisor=supervisor,
        job=replace(_JOB, runtime=runtime_id),
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        lease_token="lease",
        timeout_s=30,
    )

    assert (code, tail, errors) == (0, "raw\n", [])
    assert result is not None and result["status"] == "completed"
    assert control.events == [
        ("lifecycle", {"phase": "turn_started"}),
        ("assistant", {"text": "done"}),
    ]


def test_shared_turn_reports_dispatch_before_waiting_for_runtime_output():
    control = _Control()

    def frames():
        assert control.events == [("lifecycle", {"phase": "turn_started"})]
        yield {"kind": "result", "status": "completed", "exit_code": 0}

    supervisor = _Supervisor()
    supervisor.stream.frames = frames

    code, _tail, _errors, _result, _reported = runner_mod._stream_shared_agent_turn(
        supervisor=supervisor,
        job=_JOB,
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        lease_token="lease",
        timeout_s=30,
    )

    assert code == 0
    assert control.events == [("lifecycle", {"phase": "turn_started"})]
    assert supervisor.stream.closed is True


def test_shared_turn_does_not_report_dispatch_when_prompt_handoff_is_rejected():
    supervisor = _Supervisor()
    supervisor.start_shared_turn = Mock(side_effect=SessionSupervisorError(409, "turn rejected"))
    control = _Control()

    with pytest.raises(SessionSupervisorError, match="turn rejected"):
        runner_mod._stream_shared_agent_turn(
            supervisor=supervisor,
            job=_JOB,
            control=control,
            heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
            lease_token="lease",
            timeout_s=30,
        )

    assert control.events == []


def _virtual_clock(monkeypatch) -> SimpleNamespace:
    """Make the runner's waits advance a fake clock instead of real time.

    The ``time`` module is shared, so its control-plane client's event retries
    wait on the same fake clock.
    """
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(runner_mod.time, "sleep", sleep)
    return clock


def _event_control(endpoint) -> runner_mod.ControlPlane:
    """A real worker client whose only wire is ``endpoint``."""
    control = runner_mod.ControlPlane("https://control.test", "test-token")
    control.close()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    control.job_id = _JOB.job_id
    return control


def test_a_control_plane_restart_mid_turn_does_not_lose_the_turn(monkeypatch):
    """Events refused for ten seconds are replayed; the turn still settles.

    A deploy recreates the control-plane container, so connections are refused
    for a while. That used to escape the stream after one immediate retry, and
    the turn failed with its native context marked lost.
    """
    clock = _virtual_clock(monkeypatch)
    delivered: list[dict[str, Any]] = []
    refused: list[int] = []

    def endpoint(request):
        body = json.loads(request.content)
        if body["event_type"] == "assistant" and clock.now < 10:
            refused.append(body["event_seq"])
            raise httpx.ConnectError("connection refused", request=request)
        delivered.append(body)
        return httpx.Response(201, json={"event_id": len(delivered)})

    events = _event_control(endpoint)
    stream = _Stream(
        [
            {"kind": "event", "event_type": "assistant", "payload": {"text": "hi"}},
            {"kind": "result", "status": "completed", "exit_code": 0},
        ]
    )
    stream_turn = runner_mod._stream_shared_agent_turn

    def configure(control, supervisor, _finish):
        control.append_event.side_effect = events.append_event
        supervisor.start_shared_turn.return_value = stream
        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream_turn)

    try:
        result, control, supervisor, finish = _run_shared_boundary_case(
            monkeypatch, 0, None, "claude-code", configure=configure
        )
    finally:
        events.close()

    assert result == 0
    finish.assert_called_once()
    assert finish.call_args.args[0] == "succeeded"
    supervisor.abandon_shared.assert_not_called()
    control.abandon_chat_sandbox.assert_not_called()
    assert len(refused) > 2 and clock.now >= 10
    # Every try carried the same number, so the owner's stream holds it once.
    assistant = [body for body in delivered if body["event_type"] == "assistant"]
    assert [body["event_seq"] for body in assistant] == [refused[0]]
    assert set(refused) == {refused[0]}
    assert [body["event_seq"] for body in delivered] == list(range(1, len(delivered) + 1))


@pytest.mark.parametrize(
    ("answers", "outcome", "requests"),
    [
        ([httpx.ConnectError("refused"), 502, 503, 201], None, 4),
        ([408, 429, 500, 504, 201], None, 5),
        ([409], LeaseLost, 1),
        ([422], httpx.HTTPStatusError, 1),
        ([401], httpx.HTTPStatusError, 1),
    ],
)
def test_event_upload_retries_only_what_a_replay_can_fix(monkeypatch, answers, outcome, requests):
    """Transient refusals back off and replay; a fence or a rejection does not."""
    clock = _virtual_clock(monkeypatch)
    pending = list(answers)
    bodies: list[dict[str, Any]] = []

    def endpoint(request):
        bodies.append(json.loads(request.content))
        answer = pending.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"event_id": 1})

    control = _event_control(endpoint)
    event = runner_mod.NormalizedEvent("assistant", {"text": "hi"})
    try:
        if outcome is None:
            control.append_event(event)
        else:
            with pytest.raises(outcome):
                control.append_event(event)
    finally:
        control.close()

    assert len(bodies) == requests
    assert all(body["event_seq"] == 1 for body in bodies)
    assert clock.sleeps == ([1.0, 2.0, 4.0, 8.0][: requests - 1] if outcome is None else [])


def test_event_upload_gives_up_inside_its_budget(monkeypatch):
    """A control plane that never answers ends the turn before the lease would."""
    clock = _virtual_clock(monkeypatch)
    tries: list[float] = []

    def endpoint(request):
        tries.append(clock.now)
        raise httpx.ConnectError("refused", request=request)

    control = _event_control(endpoint)
    try:
        with pytest.raises(httpx.ConnectError):
            control.append_event(runner_mod.NormalizedEvent("assistant", {"text": "hi"}))
    finally:
        control.close()

    assert len(tries) > 2
    assert max(clock.sleeps) == control_plane_client._EVENT_RETRY_MAX_DELAY_S
    assert clock.now <= control_plane_client._EVENT_RETRY_BUDGET_S < runner_mod.DEFAULT_LEASE_TTL_S


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
    # The executor count is the supervisor's pool, sized from the control
    # plane; a replica count read from the environment would be a second,
    # stale answer to the same question.
    assert "AGENT_RUNNER_REPLICAS" not in source
    assert "def _run_retained_attempt" not in source
    assert "def run_agent" not in source
    assert "AGENT_RESTARTABLE_SANDBOX" not in source
    assert "AGENT_SHARED_LIVE_SANDBOX" not in source


def _enable_profiling(monkeypatch, **overrides):
    """Turn startup profiling on with a run that selects only this operation."""
    run = {
        "run_id": "runner-stages-1",
        "operations": [runner_mod.STARTUP_OPERATION],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "max_operations": 5,
    }
    run.update(overrides)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_RUN, json.dumps(run))
    operation_profiling.reset_cache()


def _profiled_measurement():
    """Return a live measurement wired to the runner's startup operation."""
    return operation_profiling.start_operation(
        operation=runner_mod.STARTUP_OPERATION, host="host-a", worker_id="runner-1"
    )


def test_startup_measurement_records_every_runner_stage(monkeypatch, caplog, tmp_path):
    """One selected attempt reports its stages, its path, and its dispatch."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    caplog.set_level(logging.INFO, logger=operation_profiling.__name__)
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        # Stand in for the supervisor round trip, but keep the boundary the
        # real helper owns: the prompt handoff is what closes the measurement.
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, _control, _supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="reuse",
        lease_stages=[{"name": "phase_network", "duration_us": 120}],
        resident_stages=[{"name": "cli_spawn", "duration_us": 240}],
    )

    assert result == 0
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["startup_outcome"] == "dispatched"
    assert record["measurement_complete"] is True
    assert record["producers"] == ["runner", "broker"]
    assert [stage["stage"] for stage in record["stages"]] == [
        "validate",
        "reserve",
        "supervisor_acquire",
        "sandbox_running",
        "terminal_suspend",
        "native_workspace_resume",
        "runtime_preflight",
        "agent_network",
        "terminal_resume",
        "resident_start",
        "prompt_admission",
        "prompt_dispatch",
    ]
    assert all(stage["duration_us"] >= 0 for stage in record["stages"])
    assert record["facts"]["startup_path"] == "reuse_live"
    assert record["facts"]["context_status"] == "live"
    assert record["facts"]["workspace_action"] == "reuse"


@pytest.mark.parametrize(
    ("container_action", "resident_action", "expected"),
    [
        # The container was created by this activation.
        ("create", "new", "new_group"),
        ("create", "resume", "new_group"),
        # It existed, was stopped, and this activation started it.
        ("start", "resume", "resume_stopped"),
        # It was running; only the member's CLI was reopened.
        ("reuse", "resume", "resume_running"),
        ("reuse", "new", "resume_running"),
        # Both the container and the compatible CLI were reused.
        ("reuse", "reuse", "reuse_live"),
        # Actions that do not identify a path stay unknown rather than guessed.
        ("unknown", "reuse", "unknown"),
        ("start", "unknown", "resume_stopped"),
        # A running container with no reported resident action says nothing
        # about how the CLI got there, so it does not become a resume.
        ("reuse", "unknown", "unknown"),
    ],
)
def test_startup_path_comes_from_the_actions_the_supervisor_reported(
    monkeypatch, tmp_path, container_action, resident_action, expected
):
    """The four paths differ by what was done, not by how long it took.

    A stop followed by a start and a container that never stopped both end up
    running, so the container's state after the fact cannot classify them.
    """
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, _control, _supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action=container_action,
        resident_action=resident_action,
    )

    assert result == 0
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["facts"]["startup_path"] == expected
    assert record["facts"]["container_action"] == container_action
    assert record["facts"]["resident_action"] == resident_action


def test_sandbox_manager_sub_stages_hang_off_the_stage_they_happened_in(monkeypatch, tmp_path):
    """Durations from the sandbox manager are attached, attributed and not placed.

    The sandbox manager has its own monotonic clock, so its sub-stages carry a duration
    and a producer but no offset on the runner's timeline.
    """
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, *_ = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="start",
        resident_action="resume",
        lease_stages=[
            {"name": "container_start", "duration_us": 690_000, "outcome": "ok"},
            {"name": "service_hosts_sync", "duration_us": 1_200, "outcome": "ok"},
        ],
        resident_stages=[{"name": "cli_spawn", "duration_us": 240_000, "outcome": "ok"}],
    )

    assert result == 0
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    acquire = next(item for item in record["stages"] if item["stage"] == "supervisor_acquire")
    resident = next(item for item in record["stages"] if item["stage"] == "resident_start")
    assert acquire["children"] == [
        {
            "stage": "container_start",
            "duration_us": 690_000,
            "outcome": "ok",
            "source": "broker",
            "call_id": "c1",
        },
        {
            "stage": "service_hosts_sync",
            "duration_us": 1_200,
            "outcome": "ok",
            "source": "broker",
            "call_id": "c1",
        },
    ]
    assert resident["children"] == [
        {
            "stage": "cli_spawn",
            "duration_us": 240_000,
            "outcome": "ok",
            "source": "broker",
            "call_id": "c2",
        }
    ]
    # The sandbox manager contributed, and the record says so.
    assert record["producers"] == ["runner", "broker"]
    # Every child is attributable, and no child pretends to a timeline offset.
    for child in acquire["children"] + resident["children"]:
        assert "start_offset_us" not in child
        assert child["source"] == "broker"


def test_a_sandbox_manager_that_is_not_profiling_leaves_the_record_partial(monkeypatch, tmp_path):
    """A missing producer is visible, not silently absent."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="reuse",
        lease_stages=[],
        resident_stages=[],
        sandbox_manager_records=False,
    )

    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["producers"] == ["runner"]
    # The sandbox manager was asked for its fragment and did not answer, so the record
    # is partial rather than a quiet but complete timeline.
    assert record["missing_producers"] == ["broker"]
    assert record["missing_fragments"] == [
        "broker:c1:supervisor_acquire",
        "broker:c2:resident_start",
    ]
    assert record["measurement_complete"] is False
    acquire = next(item for item in record["stages"] if item["stage"] == "supervisor_acquire")
    assert "children" not in acquire


def test_startup_measurement_reports_the_stage_a_pre_dispatch_failure_stopped_at(
    monkeypatch, caplog, tmp_path
):
    """A failure before dispatch is one record naming the stage, not a gap."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    caplog.set_level(logging.INFO, logger=operation_profiling.__name__)
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(control, _supervisor, _finish):
        control.suspend_terminals.side_effect = SandboxError("terminal suspend failed")

    result, _control, _supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        lease_stages=[{"name": "phase_network", "duration_us": 120}],
    )

    assert result == 2
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["startup_outcome"] == "failed"
    assert record["failure_stage"] == "terminal_suspend"
    assert record["measurement_complete"] is True
    assert record["duration_us"] >= 0
    # The stage that failed is the last one and it has a duration. Without the
    # closing boundary the slow stage -- the one worth profiling -- had none.
    failing = record["stages"][-1]
    assert failing["stage"] == "terminal_suspend"
    assert failing["duration_us"] >= 0


def test_stream_helper_closes_the_measurement_at_the_prompt_handoff(monkeypatch, tmp_path):
    """The real stream helper, not a stub, closes the startup measurement."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None
    started: list[dict] = []

    class _Stream:
        def frames(self):
            yield {"kind": "result", "status": "completed", "exit_code": 0}

        def close(self):
            return None

    def start_shared_turn(_thread_id, **kwargs):
        started.append(kwargs)
        return _Stream()

    code, _tail, _errors, _frame, _reported = runner_mod._stream_shared_agent_turn(
        supervisor=SimpleNamespace(start_shared_turn=start_shared_turn),
        job=_JOB,
        control=SimpleNamespace(append_event=lambda _event: None),
        heart=SimpleNamespace(lease_lost=False, cancel_requested=False),
        lease_token="lease",
        timeout_s=5,
        measurement=measurement,
    )

    assert code == 0
    assert started[0]["prompt"] == _JOB.task_prompt
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["startup_outcome"] == "dispatched"
    assert measurement.finished is True


def test_a_failed_prompt_handoff_is_a_failure_stage_with_a_duration(monkeypatch, tmp_path):
    """A dispatch that raises reports `turn_stream`, not a dispatched sample."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**_kwargs):
            raise SandboxError("the supervisor refused the prompt")

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, _control, _supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        lease_stages=[{"name": "phase_network", "duration_us": 120}],
        resident_stages=[{"name": "cli_spawn", "duration_us": 240}],
    )

    assert result == 2
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["startup_outcome"] == "failed"
    assert record["failure_stage"] == "turn_stream"
    # The handoff stage is closed even though the call never returned.
    assert record["stages"][-1]["stage"] == "prompt_dispatch"
    assert record["stages"][-1]["duration_us"] >= 0
    assert record["measurement_complete"] is True


def test_a_poll_with_no_job_does_not_spend_the_only_profiling_slot(monkeypatch, tmp_path):
    """An empty claim must leave a one-operation experiment able to measure.

    Regression: the budget was consumed when the measurement was created, so a
    single empty claim -- the normal result of polling an idle queue -- used up
    a `max_operations: 1` run before it measured anything.
    """
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch, max_operations=1)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))

    class Control:
        job_id = ""

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class Heart:
        lease_lost = False
        cancel_requested = False

        def start(self):
            return None

        def stop(self):
            return None

    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_args, **_kwargs: Control())
    monkeypatch.setattr(runner_mod, "Heartbeater", lambda *_args, **_kwargs: Heart())
    monkeypatch.setattr(
        runner_mod,
        "_run_shared_live_attempt",
        lambda **kwargs: 0 if kwargs["measurement"] is not None else 7,
    )
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

    # The queue is empty, so the run still has its only slot for a real attempt.
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: _JOB)
    assert (
        runner_mod.run_once(
            base_url="http://control",
            host_credential="host-credential",
            worker_id="runner-1",
            backend=backend,
            host="host-a",
        )
        == 0
    ), "the claimed attempt was not measured"


def test_profiling_off_adds_no_record_and_no_measurement(monkeypatch, caplog, tmp_path):
    """The default deployment must not pay for, or emit, any of this."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    operation_profiling.reset_cache()
    caplog.set_level(logging.INFO, logger=operation_profiling.__name__)
    dispatched: list[dict] = []

    class Control:
        job_id = ""

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class Heart:
        lease_lost = False
        cancel_requested = False

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: _JOB)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_args, **_kwargs: Control())
    monkeypatch.setattr(runner_mod, "Heartbeater", lambda *_args, **_kwargs: Heart())
    monkeypatch.setattr(
        runner_mod, "_run_shared_live_attempt", lambda **kwargs: dispatched.append(kwargs) or 0
    )

    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
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

    assert dispatched[0]["measurement"] is None
    assert not sink.exists()
    assert "agent_operation_profile" not in caplog.text


def test_the_runner_hands_its_selection_to_the_sandbox_manager(monkeypatch, tmp_path):
    """The sandbox manager serves one call at a time; only the runner knows the attempt."""
    _configure_host(monkeypatch)
    _enable_profiling(monkeypatch)
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    _result, _control, supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="reuse",
    )

    acquire_context = supervisor.acquire_shared.call_args.kwargs["profiling"]
    resident_context = supervisor.ensure_shared_resident.call_args.kwargs["profiling"]
    assert acquire_context["run_id"] == "runner-stages-1"
    assert acquire_context["operation"] == runner_mod.STARTUP_OPERATION
    assert acquire_context["expires_at"] > time.time()
    assert resident_context["run_id"] == "runner-stages-1"


def test_the_runner_offers_the_sandbox_manager_nothing_when_it_is_not_profiling(monkeypatch):
    """No selection means the sandbox manager has nothing to inherit."""
    _configure_host(monkeypatch)
    operation_profiling.reset_cache()

    def configure(_control, _supervisor, _finish):
        def stream(**_kwargs):
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    _result, _control, supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
    )

    assert supervisor.acquire_shared.call_args.kwargs["profiling"] is None
    assert supervisor.ensure_shared_resident.call_args.kwargs["profiling"] is None


def test_an_acquire_fragment_does_not_cover_a_missing_resident_fragment(monkeypatch, tmp_path):
    """Every call this attempt made to the sandbox manager owes a fragment.

    Regression: the acquire answered, the resident call's timing never
    arrived, and the record was still reported complete.
    """
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, *_ = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="start",
        resident_action="resume",
        lease_stages=[{"name": "container_start", "duration_us": 690_000}],
        resident_stages=[],
        sandbox_manager_records=False,
    )

    assert result == 0
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["missing_fragments"] == [
        "broker:c1:supervisor_acquire",
        "broker:c2:resident_start",
    ]
    assert record["measurement_complete"] is False


def test_a_truncated_sandbox_manager_fragment_makes_the_record_partial(monkeypatch, tmp_path):
    """The sandbox manager's own truncation must survive the boundary."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, *_ = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="start",
        resident_action="resume",
        lease_stages=[{"name": "container_start", "duration_us": 2_000_000, "truncated": True}],
        resident_stages=[{"name": "cli_spawn", "duration_us": 240}],
    )

    assert result == 0
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["truncated"] is True
    assert record["measurement_complete"] is False
    assert "missing_fragments" not in record


def test_a_reuse_that_does_no_timed_work_is_a_complete_fragment(monkeypatch, tmp_path):
    """An answered call with no stages is a skip, not a missing measurement.

    Regression: the supervisor's reuse path legitimately runs no `cli_spawn`
    and answers with no stages, which the previous rule read as "never heard
    from the sandbox manager" and marked every normal reuse incomplete.
    """
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, *_ = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="reuse",
        # The sandbox manager answered both calls; neither had timed work to report.
        lease_stages=[],
        resident_stages=[],
    )

    assert result == 0
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["facts"]["startup_path"] == "reuse_live"
    assert "missing_fragments" not in record
    assert record["measurement_complete"] is True


def test_a_retried_call_is_a_second_fragment_that_must_also_answer(monkeypatch, tmp_path):
    """Two attempts at the same stage are two fragments, not one.

    Regression: expectations were a set keyed by stage, so a failed resident
    call was erased by its successful retry and the record claimed completeness
    while the first call had never been accounted for.
    """
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None
    attempts: list[str] = []

    def configure(control, supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)
        # The retry rotates the residency grant before reopening the process.
        control.establish_shared_residency.return_value = {
            "credential": "grant-2",
            "generation": 2,
            "model": _JOB.model,
        }

        def flaky(_thread_id, **kwargs):
            context = kwargs.get("profiling") or {}
            attempts.append(context.get("call_id"))
            if len(attempts) == 1:
                # The first attempt never answered.
                raise SessionSupervisorError(503, "sandbox manager restarted mid-call")
            return {
                "context_status": "live",
                "process_generation": 2,
                "resident_action": "resume",
                "stages": [{"name": "cli_spawn", "duration_us": 240}],
                "profiling_call_id": context.get("call_id"),
                "profiling_recorded": True,
            }

        supervisor.ensure_shared_resident.side_effect = flaky

    result, *_ = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="resume",
        lease_stages=[{"name": "phase_network", "duration_us": 120}],
    )

    assert result == 0
    assert attempts == ["c2", "c3"], "each attempt must carry its own call id"
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    assert record["missing_fragments"] == ["broker:c2:resident_start"]
    assert record["measurement_complete"] is False
    # The retry's timing is still reported.
    resident = next(item for item in record["stages"] if item["stage"] == "resident_start")
    assert [child["call_id"] for child in resident["children"]] == ["c3"]


def test_a_sandbox_manager_cut_short_by_its_own_deadline_keeps_the_record_incomplete(
    monkeypatch, tmp_path
):
    """A collection that stopped at the sandbox manager's deadline is not complete.

    Regression, end to end: the runner's deadline had ten minutes left while
    the sandbox manager's had one second, the member-lock wait spent it, and the CLI
    start was never timed. The answer was `stages: []` with
    `profiling_recorded: true`, which the runner read as "collected, nothing to
    time" and reported `measurement_complete: true`.
    """
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, *_ = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="resume",
        # The acquire got its stages out; the resident call ran out of time.
        lease_stages=[{"name": "phase_network", "duration_us": 120}],
        resident_stages=[],
        sandbox_manager_truncated=True,
    )

    assert result == 0
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    # Both calls answered, so nothing is "missing" -- the record is partial.
    assert "missing_fragments" not in record
    assert record["truncated"] is True
    assert record["truncated_fragments"] == ["broker:c1", "broker:c2"]
    assert record["measurement_complete"] is False
    # What the sandbox manager did manage to measure is still reported.
    acquire = next(item for item in record["stages"] if item["stage"] == "supervisor_acquire")
    assert [child["stage"] for child in acquire["children"]] == ["phase_network"]


def test_environment_capability_disappears_when_broker_or_container_support_is_lost(monkeypatch):
    _configure_host(monkeypatch)
    backend = ContainerBackend(image="img")
    assert "environment-v1" in runner_mod._environment_host_capabilities(backend, _stamp())
    monkeypatch.delenv("AGENT_SANDBOX_MANAGER_TOKEN")
    assert runner_mod._environment_host_capabilities(backend, _stamp()) == []
    assert runner_mod._environment_host_capabilities(SimpleNamespace(), _stamp()) == []


@pytest.mark.parametrize("checkpoint", [{"checkpoint_id": "c" * 32}, "not-a-receipt", None])
def test_retained_heartbeat_forwards_the_latest_checkpoint_receipt(monkeypatch, checkpoint):
    supervisor = SimpleNamespace(
        list=lambda: [
            {
                "thread_id": "athr_source",
                "sandbox_id": "asbx_source",
                "container_id": "container-source",
                "generation": 1,
                "activation_generation": 1,
                "running": False,
                "shared_live": True,
                "members": [],
                "checkpoint": checkpoint,
            }
        ],
        close=Mock(),
    )
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda **_kwargs: supervisor)

    rows = runner_mod._retained_sandbox_heartbeat_rows()

    assert rows is not None
    assert rows[0]["checkpoint"] == (checkpoint if isinstance(checkpoint, dict) else None)
