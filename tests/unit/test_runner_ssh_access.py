"""The runner applies a claim's SSH state before anything runs in the sandbox.

The heavy collaborators are mocks, as in the turn-reporting tests: the subject
is the runner's own ordering, what it hands the sandbox manager, and what it
keeps out of output.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from tests.support.ssh_keys import host_key, private_key

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.runner import ClaimedJob, ControlPlane
from cloud_agent_host.runtimes import get_runtime
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_shared.ssh_access import SshAccess, SshMemberAccess, SshServerAccess

_KEY = private_key()
_HOST_KEY = host_key()
_ACCESS = SshAccess(
    revision=5,
    members=[
        SshMemberAccess(
            thread_id="athr_1",
            servers=[
                SshServerAccess(
                    server_id="3f9c2a1e-0000-4000-8000-000000000001",
                    name="gpu",
                    address="93.184.216.34",
                    port=22,
                    username="ubuntu",
                    host_key_type="ssh-ed25519",
                    host_key=_HOST_KEY,
                    private_key=_KEY,
                )
            ],
        )
    ],
)
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


def _run(monkeypatch, job: ClaimedJob) -> Mock:
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    retained = Mock()
    retained.sandbox_metadata.return_value = {}
    retained.has_binary.return_value = True
    monkeypatch.setattr(runner_mod, "ChatSandboxManager", lambda _backend: Mock())
    monkeypatch.setattr(runner_mod, "RetainedChatBackend", lambda *_args, **_kwargs: retained)
    monkeypatch.setattr(runner_mod, "_resident_base_ready", lambda *_args: True)
    prepared = SimpleNamespace(import_into=lambda *_args, **_kwargs: "b" * 40, discard=lambda: None)
    monkeypatch.setattr(runner_mod, "_prefetch_retained_source", lambda **_kwargs: prepared)
    monkeypatch.setattr(runner_mod, "runtime_version_unavailable", lambda *_args: None)
    control = Mock(spec=ControlPlane)
    control.reserve_chat_sandbox.side_effect = lambda seed: {
        **seed,
        "thread_id": job.thread_id,
        "generation": 1,
        "activation_generation": 3,
        "resident_slot_id": 4,
    }
    control.mark_chat_sandbox_running.return_value = {}
    supervisor = Mock()
    supervisor.acquire_shared.return_value = {
        "token": "lease",
        "container_id": "same-container",
        "activation_generation": 3,
        "context_status": "new",
        "resident_state": "live",
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
    supervisor.settle_shared.return_value = {"native_session_id": "native-new"}
    supervisor.sync_ssh_access.return_value = {"newest": True, "revision": 5}
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", lambda: supervisor)
    monkeypatch.setattr(
        runner_mod, "_stream_shared_agent_turn", lambda **_kwargs: (0, "", [], None, None)
    )
    runner_mod._run_shared_live_attempt(
        runtime=get_runtime("claude-code"),
        job=job,
        control=control,
        heart=SimpleNamespace(cancel_requested=False, lease_lost=False),
        backend=backend,
        host="host-a",
        gateway_base_url="http://gateway",
        agent_timeout_s=30,
        finish_attempt=Mock(),
    )
    return supervisor


def test_the_group_is_converged_with_this_member_in_its_slot_before_the_agent_starts(
    monkeypatch,
) -> None:
    supervisor = _run(monkeypatch, replace(_JOB, ssh_access=_ACCESS))

    calls = [name for name, _args, _kwargs in supervisor.mock_calls]
    assert calls.index("sync_ssh_access") < calls.index("ensure_shared_resident")
    (thread_id, access), kwargs = supervisor.sync_ssh_access.call_args
    assert thread_id == "athr_1"
    assert kwargs == {"starting": True}, "its keys are written, and they are the keys it masks"
    assert access.revision == 5
    assert [(member.thread_id, member.slot_id) for member in access.members] == [("athr_1", 4)]


def test_a_chat_without_ssh_servers_costs_no_call(monkeypatch) -> None:
    supervisor = _run(monkeypatch, _JOB)
    supervisor.sync_ssh_access.assert_not_called()


def test_the_keys_are_masked_in_everything_the_runner_reports() -> None:
    values = runner_mod._claim_secret_values(replace(_JOB, ssh_access=_ACCESS))
    assert values == {"ssh-key:gpu": _KEY}
    assert runner_mod._claim_secret_values(_JOB) == {}


def test_the_runner_tells_the_sandbox_manager_its_turn_is_starting() -> None:
    import json

    import httpx

    from cloud_agent_host.session_supervisor_client import SessionSupervisorClient

    sent: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"newest": True, "revision": 5, "running": True})

    client = SessionSupervisorClient("http://sandbox_manager", "secret")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(answer))

    client.sync_ssh_access("athr_1", _ACCESS, starting=True)

    assert sent[0]["starting"] is True
    assert sent[0]["access"]["revision"] == 5
