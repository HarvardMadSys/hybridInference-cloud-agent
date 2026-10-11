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
from cloud_agent_host.chat_sandbox import CHAT_UPLOADS_DIR, ChatSandboxManager
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


def test_a_zero_disk_quota_survives_the_turn_handover(monkeypatch):
    """Turning a quota off cannot fall back to a positive bootstrap quota."""
    monkeypatch.setenv("AGENT_SANDBOX_DISK_MIB", "32768")

    kwargs = _serve(monkeypatch, {**_PAYLOAD, "_host_settings": {"sandbox_disk_mib": 0}})

    assert kwargs["host_settings"] == {"sandbox_disk_mib": 0}


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


def _run_new_chat(
    monkeypatch,
    host_settings: dict[str, float] | None,
    job: ClaimedJob = _JOB,
    *,
    reservation_change: dict[str, Any] | None = None,
    reservation_omits_disk: bool = False,
    initial_archive_status: dict[str, object] | None = None,
) -> SimpleNamespace:
    """Drive a first turn of a new chat to its settlement, with collaborators mocked."""
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    manager = Mock()
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: manager)
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    checkouts: list[dict[str, Any]] = []
    prepared = SimpleNamespace(import_into=lambda *_args, **_kwargs: "b" * 40, discard=lambda: None)
    monkeypatch.setattr(
        runner_mod,
        "_prefetch_retained_source",
        lambda **kwargs: checkouts.append(kwargs) or prepared,
    )
    control = Mock(spec=runner_mod.ControlPlane)
    control.mark_chat_sandbox_running.return_value = {}
    if job.thread_attachment_count:
        control.list_attachments.return_value = [{"id": 17}]
        control.read_attachment.return_value = ("replacement.txt", b"replacement")

    def reserve(seed):
        response = {
            **seed,
            "thread_id": _JOB.thread_id,
            "generation": 1,
            "activation_generation": 3,
            "resident_slot_id": 0,
            **(reservation_change or {}),
        }
        if reservation_omits_disk:
            response.pop("disk_limit_mib", None)
        return response

    control.reserve_chat_sandbox.side_effect = reserve
    supervisor = Mock()
    supervisor.acquire_shared.return_value = {
        "token": "lease",
        "container_id": "same-container",
        "activation_generation": 3,
        "context_status": "new",
        "resident_state": "live",
    }
    supervisor.initial_archive_status.return_value = (
        {
            "enabled": False,
            "prepared": False,
            "stored": False,
        }
        if initial_archive_status is None
        else initial_archive_status
    )
    supervisor.capture_initial_archive.return_value = {
        "enabled": False,
        "stored": False,
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
        job=replace(job),
        control=control,
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
        host_settings=host_settings,
    )
    reserved = control.reserve_chat_sandbox.call_args.args[0]
    if result != 0:
        assert not supervisor.acquire_shared.called
        assert not control.settle_chat_sandbox.called
        return SimpleNamespace(
            result=result,
            reserved=reserved,
            failure=finish.call_args.args[1],
            control=control,
            manager=manager,
            supervisor=supervisor,
            finish=finish,
        )
    assert control.settle_chat_sandbox.called, finish.call_args
    settled = control.settle_chat_sandbox.call_args.kwargs
    launches = [call.kwargs["launch"] for call in supervisor.ensure_shared_resident.call_args_list]
    return SimpleNamespace(
        result=result,
        reserved=reserved,
        settled=settled,
        checkouts=checkouts,
        launches=launches,
        spec=supervisor.acquire_shared.call_args.args[0],
        control=control,
        manager=manager,
        supervisor=supervisor,
        finish=finish,
    )


def test_recovered_initial_archive_imports_a_replacement_jobs_new_uploads(monkeypatch):
    recovered_base = "c" * 40
    replacement = replace(
        _JOB,
        job_id="ajob_replacement",
        thread_attachment_count=1,
        turn_attachment_count=1,
    )

    run = _run_new_chat(
        monkeypatch,
        None,
        replacement,
        initial_archive_status={
            "enabled": True,
            "prepared": True,
            "stored": True,
            "base_sha": recovered_base,
        },
    )

    assert run.result == 0
    assert run.supervisor.capture_initial_archive.called is False
    assert run.supervisor.initial_archive_status.call_args.kwargs["preparation_id"] == (
        replacement.job_id
    )
    [upload] = run.manager.import_archive.call_args_list
    assert upload.args[1] == CHAT_UPLOADS_DIR
    assert run.finish.call_args.kwargs["base_sha"] == recovered_base


def test_new_sandbox_resource_settings_reach_the_docker_limits(monkeypatch):
    run = _run_new_chat(
        monkeypatch,
        {"sandbox_cpu_millicores": 750, "sandbox_memory_mib": 8192, "sandbox_disk_mib": 20480},
    )

    assert run.result == 0
    assert run.reserved["cpu_limit"] == "0.75"
    assert run.reserved["memory_limit"] == "8192m"
    assert run.reserved["disk_limit_mib"] == 20480
    command = ChatSandboxManager(ContainerBackend(image="img")).build_create_command(run.spec)
    assert command[command.index("--cpus") + 1] == "0.75"
    assert command[command.index("--memory") + 1] == "8192m"
    assert command[command.index("--storage-opt") + 1] == "size=20480M"


def test_a_disabled_disk_quota_takes_precedence_over_the_bootstrap_value(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_DISK_MIB", "32768")

    run = _run_new_chat(monkeypatch, {"sandbox_disk_mib": 0})

    assert run.reserved["disk_limit_mib"] == 0
    command = ChatSandboxManager(ContainerBackend(image="img")).build_create_command(run.spec)
    assert "--storage-opt" not in command


@pytest.mark.parametrize(
    "change",
    [
        {"disk_limit_mib": 0},
        {"disk_limit_mib": None},
        {"memory_limit": "4g"},
        {"cpu_limit": "2"},
    ],
)
def test_a_reservation_cannot_drop_or_change_the_requested_resource_limits(monkeypatch, change):
    run = _run_new_chat(
        monkeypatch,
        {"sandbox_cpu_millicores": 750, "sandbox_memory_mib": 8192, "sandbox_disk_mib": 20480},
        reservation_change=change,
    )

    assert run.result == 2
    assert "did not confirm" in run.failure


@pytest.mark.parametrize("disk_limit_mib", [0, 20480])
def test_a_response_omitting_disk_only_confirms_an_unbounded_disk(monkeypatch, disk_limit_mib):
    run = _run_new_chat(
        monkeypatch,
        {"sandbox_disk_mib": disk_limit_mib},
        reservation_omits_disk=True,
    )

    assert run.result == (2 if disk_limit_mib else 0)
    if disk_limit_mib:
        assert "did not confirm" in run.failure


def test_container_resources_bootstrap_from_the_hosts_environment(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_CPU_MILLICORES", "3500")
    monkeypatch.setenv("AGENT_SANDBOX_MEMORY_MIB", "12288")
    monkeypatch.setenv("AGENT_SANDBOX_DISK_MIB", "40960")

    local = runner_mod._runner_local_settings(3600)
    run = _run_new_chat(monkeypatch, None)

    assert local["sandbox_cpu_millicores"] == 3500
    assert local["sandbox_memory_mib"] == 12288
    assert local["sandbox_disk_mib"] == 40960
    assert run.reserved["cpu_limit"] == "3.5"
    assert run.reserved["memory_limit"] == "12288m"
    assert run.reserved["disk_limit_mib"] == 40960


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
@pytest.mark.parametrize("disk_limit_mib", [None, 0, 16384])
def test_an_existing_sandbox_keeps_the_capacity_and_resources_it_was_created_with(
    monkeypatch, capacity, disk_limit_mib
):
    """Settings changes preserve the retained group, including an unbounded legacy disk."""
    settings = {
        "sandbox_cpu_millicores": 4000,
        "sandbox_memory_mib": 8192,
        "sandbox_disk_mib": 32768,
        **({} if capacity is None else {"shared_resident_capacity": capacity}),
    }
    existing = {
        "shared_live": True,
        "runtime_id": "claude-code",
        "execution_profile": _JOB.execution_profile,
        "resident_slot_capacity": 4,
        "sandbox_id": "asbx_existing",
        "host": "host-a",
        "memory_limit": "6g",
        "cpu_limit": "3.5",
    }
    if disk_limit_mib is not None:
        existing["disk_limit_mib"] = disk_limit_mib
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

    reserved = control.reserve_chat_sandbox.call_args.args[0]
    assert reserved["resident_slot_capacity"] == 4
    assert reserved["memory_limit"] == "6g"
    assert reserved["cpu_limit"] == "3.5"
    assert reserved["disk_limit_mib"] == (disk_limit_mib or 0)


def test_a_turn_with_tools_reaches_them_through_this_hosts_own_mcp_relay(monkeypatch):
    """The relay a member CLI is told is the one this host's rules admit.

    A claim from an older control plane still names an address of its own. It
    decides nothing now: that copy had to match every host's tunnel alias, and
    left unset it failed every turn whose owner had connected a tool.
    """
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://agent-mcp:8000")
    job = ClaimedJob.from_response(
        {
            **_PAYLOAD,
            "runtime_version": _JOB.runtime_version,
            "mcp_token": "mcp-token",
            "mcp_bindings": ["deepwiki"],
            "mcp_base_url": "http://stale-copy:9000",
        }
    )

    run = _run_new_chat(monkeypatch, None, job)

    assert run.result == 0
    (launch,) = run.launches
    assert launch.mcp_server_ids == ("deepwiki",)
    assert launch.mcp_base_url == "http://agent-mcp:8000"
    assert launch.mcp_credential == "mcp-token"


def test_a_turn_without_tools_is_given_no_relay(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_MCP_URL", "http://agent-mcp:8000")

    run = _run_new_chat(monkeypatch, None)

    (launch,) = run.launches
    assert launch.mcp_server_ids == ()
    assert launch.mcp_base_url == ""
    assert launch.mcp_credential == ""


def test_a_turn_with_tools_on_a_host_without_a_relay_says_the_site_is_misconfigured(
    monkeypatch,
):
    """Refused before any container work, in words the owner can act on.

    The runtime would refuse the tools only at the resident launch, after a
    checkout the turn could never use, with a reason only an operator reads.
    The chat shows a failure starting "Site misconfigured:" as an alert.
    """
    monkeypatch.delenv("AGENT_SANDBOX_MCP_URL", raising=False)
    backend = ContainerBackend(image="img")
    stamp = Mock(side_effect=_stamp)
    monkeypatch.setattr(backend, "version_stamp", stamp)
    supervisors = Mock()
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", supervisors)
    control = Mock(spec=runner_mod.ControlPlane)
    finish = Mock()

    result = runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=replace(_JOB, mcp_token="mcp-token", mcp_bindings=["deepwiki"]),
        control=control,
        heart=SimpleNamespace(),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=finish,
    )

    assert result == 2
    finish.assert_called_once_with("failed", runner_mod.SITE_MISCONFIGURED_MCP)
    assert runner_mod.SITE_MISCONFIGURED_MCP.startswith("Site misconfigured:")
    assert not stamp.called
    assert not control.reserve_chat_sandbox.called
    assert not supervisors.called
