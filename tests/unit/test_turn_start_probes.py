"""Probes a turn's start no longer repeats once their answer cannot change.

A follow-up turn used to ask a running container two questions whose answers
were already settled: whether the member-process reaper is installed (asked
at every census and reap, as root, behind any install into that container),
and, from the runner, whether a CLI that is live in it has a binary. Each is a
``docker exec`` on the critical path, 100-300 ms under Kata.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.chat_sandbox import (
    MEMBER_REAPER_TARGET,
    ChatSandboxManager,
    ChatSandboxSpec,
)
from cloud_agent_host.chat_sandbox_inspect import container_inspection
from cloud_agent_host.install_locks import install_lock
from cloud_agent_host.runner import ClaimedJob, ControlPlane
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend, SandboxError


def _spec(generation: int = 3) -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_01",
        sandbox_id="asbx_01",
        generation=generation,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image-pinned",
    )


def _running(container_id: str) -> Any:
    return container_inspection(
        {"Id": container_id, "Name": "/chat", "State": {"Status": "running", "Running": True}}
    )


def _manager(monkeypatch, answers: list[int] | None = None):
    """A manager whose execs answer ``answers`` in order, then succeed."""
    manager = ChatSandboxManager(ContainerBackend(image="image", sandbox_namespace="ns"))
    execs: list[list[str]] = []
    pending = list(answers or [])

    def exec_capture(_spec, argv, **_kwargs):
        execs.append(argv)
        code = pending.pop(0) if pending else 0
        return SimpleNamespace(returncode=code, stdout=b"", stderr=b"")

    monkeypatch.setattr(manager, "exec_capture", exec_capture)
    return manager, execs


def test_a_reaper_found_in_a_proven_container_is_not_probed_again(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))

    manager.ensure_member_process_reaper(spec)
    manager.ensure_member_process_reaper(spec)

    assert len(execs) == 1
    assert MEMBER_REAPER_TARGET in execs[0]


def test_an_unproven_container_is_probed_every_time(monkeypatch):
    """Without the exact container a proof names, there is nothing to remember it by."""
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(_spec(generation=4), _running("container-other"))

    manager.ensure_member_process_reaper(spec)
    manager.ensure_member_process_reaper(spec)

    assert len(execs) == 2


def test_a_replacement_under_the_same_name_is_probed_again(monkeypatch):
    manager, execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))
    manager.ensure_member_process_reaper(spec)

    manager._proven.forget(spec)
    manager._proven.record(spec, _running("container-two"))
    manager.ensure_member_process_reaper(spec)
    manager.ensure_member_process_reaper(spec)

    assert len(execs) == 2


def test_forgetting_a_container_forgets_what_was_found_in_it(monkeypatch):
    manager, _execs = _manager(monkeypatch)
    spec = _spec()
    first = _running("container-one")
    manager._proven.record(spec, first)
    manager.ensure_member_process_reaper(spec)
    assert manager._proven.holds_helper(first, MEMBER_REAPER_TARGET)

    manager._proven.forget(spec)

    assert not manager._proven.holds_helper(first, MEMBER_REAPER_TARGET)


@pytest.fixture
def reaper_source(tmp_path, monkeypatch):
    source = tmp_path / "member-process-reaper"
    source.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MEMBER_PROCESS_REAPER_SOURCE", str(source))
    return source


def test_an_installed_reaper_is_remembered(monkeypatch, reaper_source):
    manager, execs = _manager(monkeypatch, answers=[1, 0])
    spec = _spec()
    proven = _running("container-one")
    manager._proven.record(spec, proven)
    monkeypatch.setattr(manager, "_require", lambda _spec: proven)
    monkeypatch.setattr(
        manager, "_run", lambda argv, **_kwargs: SimpleNamespace(returncode=0, stderr="")
    )

    manager.ensure_member_process_reaper(spec)
    manager.ensure_member_process_reaper(spec)

    assert len(execs) == 2, "one probe, one install, then nothing"


def test_a_failed_install_is_not_remembered(monkeypatch, reaper_source):
    manager, execs = _manager(monkeypatch, answers=[1, 1, 0, 0])
    spec = _spec()
    proven = _running("container-one")
    manager._proven.record(spec, proven)
    monkeypatch.setattr(manager, "_require", lambda _spec: proven)
    monkeypatch.setattr(
        manager, "_run", lambda argv, **_kwargs: SimpleNamespace(returncode=0, stderr="")
    )

    with pytest.raises(SandboxError, match="activation failed"):
        manager.ensure_member_process_reaper(spec)
    manager.ensure_member_process_reaper(spec)

    assert len(execs) == 4, "probe, install, cleanup; then probed again"


def test_a_known_reaper_does_not_wait_behind_an_install_into_its_container(monkeypatch):
    """Runtime and MCP installs hold the container's install lock for minutes."""
    manager, _execs = _manager(monkeypatch)
    spec = _spec()
    manager._proven.record(spec, _running("container-one"))
    manager.ensure_member_process_reaper(spec)

    with install_lock(spec.container_name):
        census = threading.Thread(
            target=manager.ensure_member_process_reaper, args=(spec,), daemon=True
        )
        census.start()
        census.join(5)
        assert not census.is_alive(), "a settled answer waited for the install lock"


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


def _stamp() -> dict[str, Any]:
    return {
        "sandbox_image_digest": "sha256:shared-image",
        "runtime_versions": dict(runner_mod.SHARED_SANDBOX_RUNTIME_PINS),
        "runtime_versions_available": {
            key: [value] for key, value in runner_mod.SHARED_SANDBOX_RUNTIME_PINS.items()
        },
        "helper_versions": dict(runner_mod.SHARED_SANDBOX_HELPER_PINS),
    }


def _run_turn(monkeypatch, *, resident_state: str, has_binary: bool) -> tuple[Mock, Mock]:
    """Run one shared-live attempt against mocks; return the retained backend and finish."""
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = has_binary
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: Mock())
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    prepared = SimpleNamespace(import_into=lambda *_args, **_kwargs: "b" * 40, discard=lambda: None)
    monkeypatch.setattr(runner_mod, "_prefetch_retained_source", lambda **_kwargs: prepared)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    control = Mock(spec=ControlPlane)
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **seed,
        "thread_id": _JOB.thread_id,
        "generation": 1,
        "activation_generation": 3,
        "resident_slot_id": 4,
    }
    control.mark_chat_sandbox_running.return_value = {}
    control.establish_shared_residency.return_value = {"credential": "residency-grant"}
    supervisor = Mock()
    supervisor.acquire_shared.return_value = {
        "token": "lease",
        "container_id": "same-container",
        "activation_generation": 3,
        "context_status": "new",
        "resident_state": resident_state,
    }
    supervisor.initial_archive_status.return_value = {
        "enabled": False,
        "prepared": False,
        "stored": False,
    }
    supervisor.capture_initial_archive.return_value = {"enabled": False, "stored": False}
    supervisor.ensure_shared_resident.return_value = {
        "context_status": "new",
        "process_generation": 1,
    }
    supervisor.settle_shared.return_value = {"native_session_id": "native-1"}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod, "_stream_shared_agent_turn", lambda **_kwargs: (0, "", [], None, None)
    )
    finish = Mock()
    runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=_JOB,
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
    )
    return retained, finish


def test_a_live_cli_is_not_asked_whether_its_binary_exists(monkeypatch):
    retained, finish = _run_turn(monkeypatch, resident_state="live", has_binary=False)

    retained.has_binary.assert_not_called()
    assert not any("is not installed" in str(call) for call in finish.call_args_list)


@pytest.mark.parametrize("resident_state", ["stopped", "restarting", "lost"])
def test_a_cli_that_must_be_opened_still_needs_its_binary(monkeypatch, resident_state):
    retained, finish = _run_turn(monkeypatch, resident_state=resident_state, has_binary=False)

    retained.has_binary.assert_called_once_with(get_runtime("claude-code").binary)
    (outcome, detail), _kwargs = finish.call_args
    assert outcome == "failed"
    assert "is not installed" in detail
