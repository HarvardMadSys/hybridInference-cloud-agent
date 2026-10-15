"""A restore that fails on this host hands the turn back instead of failing it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from tests.unit.test_runner_host_settings import _JOB, _stamp
from tests.unit.test_runner_source_prefetch import Source

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_host.session_supervisor_client import SessionSupervisorError

_RESTORE = {
    "checkpoint_id": "c" * 32,
    "object_key": "cloud-agent/sandbox/v2/" + "d" * 64 + "/g1/" + "c" * 32 + ".oci.tar",
    "version_id": "version-1",
    "sha256": "e" * 64,
    "size_bytes": 4096,
    "base_image": "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64,
    "format_version": 1,
}


def _restoring_turn(
    monkeypatch, *, acquire_error: Exception, group_after_abandon: dict[str, Any]
) -> SimpleNamespace:
    source = Source()
    monkeypatch.setattr(runner_mod, "prepare_worktree", source.prepare_worktree)
    monkeypatch.setattr(runner_mod, "build_source_archive", source.build_source_archive)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    manager = Mock()
    manager.backend = backend
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    control = Mock(spec=runner_mod.ControlPlane)
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **seed,
        "thread_id": _JOB.thread_id,
        "generation": 2,
        "activation_generation": 3,
        "resident_slot_id": 0,
        "state": "restoring",
        "start_seq": 9,
        "restore": _RESTORE,
        "last_settled_job_id": "ajob_0",
        "native_session_id": "native-1",
    }
    control.abandon_chat_sandbox.return_value = group_after_abandon
    control.release_turn.return_value = True
    supervisor = Mock()
    supervisor.acquire_shared.side_effect = acquire_error
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setenv("AGENT_SHARED_RESIDENT_CAPACITY", "8")
    monkeypatch.setenv("AGENT_CHAT_IDLE_TIMEOUT_S", "1800")
    finish = Mock()
    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=_JOB,
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
        host_settings={},
    )
    return SimpleNamespace(result=result, finish=finish, control=control, supervisor=supervisor)


def test_a_failed_restore_returns_the_group_to_its_checkpoint_and_the_turn_to_the_queue(
    monkeypatch,
):
    turn = _restoring_turn(
        monkeypatch,
        acquire_error=SessionSupervisorError(424, "checkpoint blob does not match its digest"),
        group_after_abandon={"state": "cold"},
    )

    kwargs = turn.supervisor.acquire_shared.call_args.kwargs
    assert kwargs["restore"] == _RESTORE
    assert kwargs["start_seq"] == 9
    assert kwargs["expected_container_id"] is None
    abandon = turn.control.abandon_chat_sandbox.call_args.kwargs
    assert abandon["lost"] is False
    assert abandon["sandbox_generation"] == 2
    turn.control.release_turn.assert_called_once_with("sandbox_restore_failed")
    turn.finish.assert_not_called()
    assert turn.result == 0


def test_a_restore_out_of_attempts_fails_the_turn(monkeypatch):
    turn = _restoring_turn(
        monkeypatch,
        acquire_error=SessionSupervisorError(424, "sandbox image could not be pulled"),
        group_after_abandon={"state": "lost"},
    )

    turn.control.release_turn.assert_not_called()
    assert turn.finish.call_args.args[0] == "failed"
    assert "sandbox restore failed" in turn.finish.call_args.args[1]
    assert turn.result == 2


@pytest.mark.parametrize("status", [410, 503])
def test_other_failures_of_a_restoring_turn_are_not_hand_backs(monkeypatch, status):
    turn = _restoring_turn(
        monkeypatch,
        acquire_error=SessionSupervisorError(status, "something else"),
        group_after_abandon={"state": "cold"},
    )

    turn.control.release_turn.assert_not_called()
    assert turn.result == 2
