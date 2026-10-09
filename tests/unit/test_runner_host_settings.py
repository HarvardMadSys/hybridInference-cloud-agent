"""A turn runs with the runtime settings the runner resolved when it handed it over."""

from __future__ import annotations

import io
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.host_runtime import LAUNCH_STARTED, LaunchResult, encode_turn
from cloud_agent_host.runner import ClaimedJob
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend, ProcessBackend

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

_PAYLOAD = {
    "job_id": "ajob_1",
    "thread_id": "athr_1",
    "attempt_id": 7,
    "attempt_no": 1,
    "repo": "o/n",
    "task_prompt": "do it",
    "runtime": "claude-code",
    "model": "m",
    "worker_token": "ajt.a.b",
    "sandbox_token": "grant",
}


def test_the_spawn_payload_keeps_the_settings_the_turn_runs_with():
    handed = runner_mod._child_turn_payload(
        {
            "job_id": "ajob_1",
            "_worker_id": "parent-only",
            "_host_settings": {"agent_timeout_s": 900},
        }
    )

    assert handed == {"job_id": "ajob_1", "_host_settings": {"agent_timeout_s": 900}}


def test_a_turn_is_handed_the_settings_as_of_its_handover():
    seen: list[dict[str, Any]] = []

    class _Turns:
        def launch(self, handed: dict[str, Any]) -> Any:
            seen.append(handed)
            return LaunchResult(LAUNCH_STARTED, None)

    runner_mod._hand_over_turn(
        dict(_PAYLOAD),
        turns=_Turns(),
        base_url="http://control",
        worker_id="runner-a-1",
        host_settings={"agent_timeout_s": 900.0},
    )
    runner_mod._hand_over_turn(
        dict(_PAYLOAD), turns=_Turns(), base_url="http://control", worker_id="runner-a-2"
    )

    assert seen[0]["_host_settings"] == {"agent_timeout_s": 900.0}
    assert "_host_settings" not in seen[1]


def _serve(monkeypatch, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the child's side of one handover and return what it ran the turn with."""
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    ran: list[dict[str, Any]] = []
    monkeypatch.setattr(runner_mod, "run_turn", lambda *_a, **kwargs: ran.append(kwargs) or 0)
    try:
        assert (
            runner_mod._serve_turn(
                handover_fd=write_fd,
                base_url="http://control",
                gateway_url="http://gateway",
                worker_id="runner-a",
                lease_ttl=120.0,
                agent_timeout_s=3600.0,
                host="host-a",
            )
            == 0
        )
    finally:
        os.close(read_fd)
    (kwargs,) = ran
    return kwargs


def test_a_turn_runs_with_the_settings_it_was_handed(monkeypatch):
    kwargs = _serve(
        monkeypatch,
        {
            **_PAYLOAD,
            "_host_settings": {
                "agent_timeout_s": 900,
                "source_import_max_mib": 4096,
                # The broker's, not the runner's, and a value no turn can use:
                # both are dropped rather than acted on.
                "terminal_max_sessions": 16,
                "chat_idle_timeout_s": -5,
            },
        },
    )

    assert kwargs["agent_timeout_s"] == 900.0
    assert kwargs["host_settings"] == {"agent_timeout_s": 900.0, "source_import_max_mib": 4096.0}


def test_a_turn_handed_no_settings_runs_with_the_hosts_own(monkeypatch):
    kwargs = _serve(monkeypatch, dict(_PAYLOAD))

    assert kwargs["agent_timeout_s"] == 3600.0
    assert kwargs["host_settings"] == {}


def _stamp() -> dict[str, Any]:
    return {
        "sandbox_image_digest": "sha256:shared-image",
        "runtime_versions": dict(runner_mod.SHARED_SANDBOX_RUNTIME_PINS),
        "runtime_versions_available": {
            key: [value] for key, value in runner_mod.SHARED_SANDBOX_RUNTIME_PINS.items()
        },
        "helper_versions": dict(runner_mod.SHARED_SANDBOX_HELPER_PINS),
    }


def _run_new_chat(monkeypatch, host_settings: dict[str, float] | None) -> SimpleNamespace:
    """Drive a first turn of a new chat to its settlement, with collaborators mocked."""
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: Mock())
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    checkouts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner_mod,
        "_prepare_retained_checkout",
        lambda **kwargs: checkouts.append(kwargs) or "b" * 40,
    )
    control = Mock(spec=runner_mod.ControlPlane)
    control.mark_chat_sandbox_running.return_value = {}
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **seed,
        "thread_id": _JOB.thread_id,
        "generation": 1,
        "activation_generation": 3,
        "resident_slot_id": 0,
    }
    supervisor = Mock()
    supervisor.acquire_shared.return_value = {
        "token": "lease",
        "container_id": "same-container",
        "activation_generation": 3,
        "context_status": "new",
        "resident_state": "live",
    }
    supervisor.ensure_shared_resident.return_value = {
        "context_status": "new",
        "process_generation": 1,
    }
    supervisor.settle_shared.return_value = {"native_session_id": "native-new"}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod, "_stream_shared_agent_turn", lambda **_kwargs: (0, "", [], None)
    )
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    monkeypatch.setenv("AGENT_CHAT_IDLE_TIMEOUT_S", "1800")

    finish = Mock()
    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=replace(_JOB),
        control=control,
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
        host_settings=host_settings,
    )
    assert control.settle_chat_sandbox.called, finish.call_args
    reserved = control.reserve_chat_sandbox.call_args.args[0]
    settled = control.settle_chat_sandbox.call_args.kwargs
    return SimpleNamespace(result=result, reserved=reserved, settled=settled, checkouts=checkouts)


def test_a_new_chat_takes_its_capacity_import_limit_and_idle_timeout_from_the_settings(
    monkeypatch,
):
    run = _run_new_chat(
        monkeypatch,
        {"shared_resident_capacity": 16, "source_import_max_mib": 4096, "chat_idle_timeout_s": 600},
    )

    assert run.result == 0
    assert run.reserved["resident_slot_capacity"] == 16
    assert run.checkouts[0]["max_bytes"] == 4096 * 1024 * 1024
    assert run.settled["idle_timeout_seconds"] == 600


def test_with_no_settings_a_new_chat_reads_the_environment_as_before(monkeypatch):
    run = _run_new_chat(monkeypatch, None)

    assert run.result == 0
    assert run.reserved["resident_slot_capacity"] == 8
    assert run.checkouts[0]["max_bytes"] is None, "the checkout reads its own limit"
    assert run.settled["idle_timeout_seconds"] == 1800


@pytest.mark.parametrize("capacity", [16, None])
def test_an_existing_sandbox_keeps_the_capacity_it_was_created_with(monkeypatch, capacity):
    """Member slots are built into the container, so a setting cannot resize one."""
    settings = {} if capacity is None else {"shared_resident_capacity": capacity}
    existing = {
        "shared_live": True,
        "runtime_id": "claude-code",
        "execution_profile": _JOB.execution_profile,
        "resident_slot_capacity": 4,
        "sandbox_id": "asbx_existing",
        "host": "host-a",
    }
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    control = Mock()
    control.reserve_chat_sandbox.side_effect = RuntimeError("stop after the reservation request")
    finish = Mock()
    job = replace(_JOB, thread_sandbox=existing)
    existing["container_name"] = runner_mod.ChatSandboxSpec(
        thread_id=job.thread_id,
        sandbox_id="asbx_existing",
        generation=1,
        runtime_id="claude-code",
        runtime_version=None,
        sandbox_image_digest=None,
        shared_live=True,
        resident_slot_capacity=4,
    ).container_name

    runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=job,
        control=control,
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
        host_settings=settings,
    )

    assert control.reserve_chat_sandbox.call_args.args[0]["resident_slot_capacity"] == 4
