"""What a host reports is stored even when it holds text Postgres cannot keep as sent.

A tool that prints NUL-separated output puts NUL characters in it, and a CLI
that cuts a string between the two halves of a surrogate pair leaves an
unpaired surrogate in its JSON. Both are ordinary, and Postgres keeps neither:
``text`` and ``jsonb`` refuse NUL, and a surrogate is not UTF-8 at all. These
drive the routes a runner calls, and the owner's routes that store what a
sandbox manager answers, over the real store, with the bytes a host sends.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.integration.servers import test_pending_message_queue as pending_message_queue
from tests.integration.servers.test_pending_message_queue import (
    _OWNER,
    _claim_body,
    api as api,
    pool as pool,
)

from cloud_agent.api import environments
from cloud_agent.sandbox_manager_client import SandboxManagerClient

if TYPE_CHECKING:
    from tests.integration.servers.test_pending_message_queue import Api

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: What a tool printed -- ``cat /proc/self/cmdline``, or a line a CLI cut in the
#: middle of an emoji -- and what is stored for it.
_OUTPUT = {
    "nul": ("cat\x00/proc/self/cmdline\x00", "cat\ufffd/proc/self/cmdline\ufffd"),
    "surrogate": ("build ok \ud83d", "build ok \ufffd"),
}


async def _post(api: Api, turn: dict[str, Any], path: str, body: dict[str, Any]) -> Any:
    """Send ``body`` as a runner's JSON encoder writes it, escapes and all."""
    return await api.client.post(
        f"/v1/agent/worker/jobs/{turn['job_id']}/{path}",
        content=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {turn['worker_token']}",
            "Content-Type": "application/json",
        },
    )


@pytest.mark.parametrize("output", ["nul", "surrogate"])
async def test_tool_output_and_a_message_holding_unstorable_text_are_stored(api: Api, output: str):
    printed, stored = _OUTPUT[output]
    job = await api.create()
    turn = await api.start(job)

    tool = await _post(
        api,
        turn,
        "events",
        {
            "event_type": "tool_result",
            "payload": {"tool_use_id": "toolu_1", "content": printed},
            "event_seq": 1,
        },
    )
    message = await _post(
        api,
        turn,
        "events",
        {"event_type": "message", "payload": {"text": f"It printed {printed}"}, "event_seq": 2},
    )

    assert tool.status_code == 201, tool.text
    assert message.status_code == 201, message.text
    events = (await api.client.get(f"/v1/agent/jobs/{job['id']}/events")).json()["events"]
    assert [event["payload"] for event in events[-2:]] == [
        {"tool_use_id": "toolu_1", "content": stored},
        {"text": f"It printed {stored}"},
    ]
    transcript = (await api.thread(job["id"]))["messages"]
    assert (transcript[-1]["role"], transcript[-1]["content"]) == (
        "assistant",
        f"It printed {stored}",
    )


@pytest.mark.parametrize("output", ["nul", "surrogate"])
async def test_a_failed_turn_ends_with_the_reason_its_sandbox_printed(api: Api, output: str):
    """A runner's reason quotes the CLI's last words, as it releases and then ends a turn.

    Refusing the reason refused the report: the activation stayed held and the
    turn sat on its lease until the reaper ended it, without that reason.
    """
    printed, stored = _OUTPUT[output]
    job = await api.create()
    turn = await api.start(job)

    abandoned = await _post(
        api,
        turn,
        "sandbox/abandon",
        {
            "sandbox_generation": turn["fence"]["sandbox_generation"],
            "activation_generation": turn["fence"]["activation_generation"],
            "lost": True,
            "detail": f"agent exited 1: {printed}",
        },
    )
    finished = await _post(
        api, turn, "finish", {"state": "failed", "detail": f"agent exited 1: {printed}"}
    )

    assert abandoned.status_code == 200, abandoned.text
    assert finished.status_code == 200, finished.text
    ended = (await api.client.get(f"/v1/agent/jobs/{job['id']}")).json()
    assert (ended["state"], ended["detail"]) == ("failed", f"agent exited 1: {stored}")
    async with api.pool.acquire() as conn:
        released = await conn.fetchrow(
            "SELECT active_attempt_id, last_error FROM agent_thread_sandbox_memberships "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    assert dict(released) == {
        "active_attempt_id": None,
        "last_error": f"agent exited 1: {stored}",
    }


#: What an environment's setup script or saved command printed, and what is kept.
_PRINTED, _KEPT = _OUTPUT["nul"]


async def _environment(api: Api, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An environment with a setup script and one saved command."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "unstorable-environment-key")
    monkeypatch.setattr(
        "cloud_agent.environment_source.resolve_test_commit", AsyncMock(return_value="a" * 40)
    )
    api.app.include_router(environments.router)
    response = await api.client.post(
        "/v1/agent/environments",
        json={
            "name": "Development",
            "repo": "owner/name",
            "revision": 0,
            "setup_script": "npm ci",
            "actions": [{"id": "test", "name": "Run tests", "command": "npm test", "cwd": "."}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _sandbox_manager(replies: dict[str, dict[str, Any]]) -> SandboxManagerClient:
    """The real client, talking to a sandbox manager that answers by path suffix."""

    def answer(request: httpx.Request) -> httpx.Response:
        for suffix, reply in replies.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, content=json.dumps(reply).encode())
        return httpx.Response(404, json={"detail": "Not Found"})

    return SandboxManagerClient(
        "http://sandbox-manager.test",
        "sandbox-manager-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )


async def _bound_job(api: Api, env: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """A chat that uses ``env``, and the binding its workspace was given."""
    job = await api.create(environment_id=env["id"], environment_revision=1, global_revision=0)
    view = await api.client.get(f"/v1/agent/jobs/{job['id']}/thread/environment")
    assert view.status_code == 200, view.text
    return job, view.json()["binding_id"]


async def test_an_environment_test_whose_setup_printed_a_nul_is_recorded(api: Api, monkeypatch):
    """A host reports a test's output while it runs, and once more when it ends.

    The host stops a test whose report is refused, and its last report carries
    the same output: the test failed for no reason of its own and never ended.
    """
    env = await _environment(api, monkeypatch)
    queued = await api.client.post(
        "/v1/agent/environment-runs",
        json={"environment_id": env["id"], "environment_revision": 1, "global_revision": 0},
    )
    assert queued.status_code == 201, queued.text
    attestation = _claim_body("environment-host")["attestation"]
    attestation["runner_capabilities"].append("environment-v1")
    leased = await api.client.post(
        "/v1/agent/worker/environment-runs/claim",
        json={"capabilities": ["environment-v1"], "attestation": attestation},
    )
    assert leased.status_code == 200, leased.text
    run = leased.json()

    reports = [
        await api.client.post(
            f"/v1/agent/worker/environment-runs/{run['id']}",
            content=json.dumps(
                {
                    "generation": run["lease_generation"],
                    "state": state,
                    "cleanup_complete": state != "running",
                    "steps": [{"phase": "setup", "state": state, "output": _PRINTED}],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        for state in ("running", "succeeded")
    ]

    assert [report.status_code for report in reports] == [200, 200], reports[0].text
    ended = (await api.client.get(f"/v1/agent/environment-runs/{run['id']}")).json()
    assert ended["state"] == "succeeded"
    assert [step["output"] for step in ended["steps"]] == [_KEPT]


async def test_a_saved_command_that_printed_a_nul_reports_its_result(api: Api, monkeypatch):
    """Each look at a running command stores the summary its terminal holds.

    A summary Postgres refused answered every look with a 500, so the command
    never read as finished.
    """
    env = await _environment(api, monkeypatch)
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(return_value=None),
    )
    job, binding_id = await _bound_job(api, env)
    run = await api.store.environments.create_environment_action_run(
        _OWNER, job["thread_id"], binding_id, "test", "unstorable-command"
    )
    terminal = f"term_{run['id']}"
    sandbox_manager = _sandbox_manager(
        {
            f"terminals/{terminal}/output": {
                "id": terminal,
                "state": "exited",
                "exit_code": 1,
                "output": _PRINTED,
            }
        }
    )
    monkeypatch.setattr(
        environments, "_action_context", AsyncMock(return_value=(job, sandbox_manager, None))
    )

    looked = await api.client.get(f"/v1/agent/environment-action-runs/{run['id']}")

    assert looked.status_code == 200, looked.text
    assert (looked.json()["state"], looked.json()["output"]) == ("failed", _KEPT)


async def test_a_preparation_that_printed_a_nul_is_recorded(api: Api, monkeypatch):
    """Retrying a workspace's preparation stores the steps its sandbox manager ran."""
    env = await _environment(api, monkeypatch)
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(return_value=None),
    )
    job, binding_id = await _bound_job(api, env)
    receipt = {
        "binding_id": binding_id,
        "container_id": f"container-{job['thread_id']}",
        "boot_id": "boot-1",
        "steps": [{"phase": "setup", "state": "failed", "output": _PRINTED}],
    }
    sandbox_manager = _sandbox_manager(
        {
            "/health": {"capabilities": ["environment-v1"]},
            # Told the fence the preparation took before it may start the group.
            "/register": {},
            "environment/prepare": receipt,
        }
    )
    monkeypatch.setattr(
        "cloud_agent.sandbox_manager_routing.sandbox_manager_for_job",
        AsyncMock(return_value=sandbox_manager),
    )
    # Only a host that can prepare the environment may claim the chat's turn.
    claim = _claim_body("w")
    claim["attestation"]["runner_capabilities"].append("environment-v1")
    monkeypatch.setattr(pending_message_queue, "_claim_body", lambda _worker_id: claim)
    await api.start(job)

    retried = await api.client.post(f"/v1/agent/jobs/{job['id']}/thread/environment/retry")

    assert retried.status_code == 200, retried.text
    async with api.pool.acquire() as conn:
        steps = await conn.fetchval(
            "SELECT steps FROM agent_environment_preparation WHERE binding_id = $1", binding_id
        )
    assert [step["output"] for step in json.loads(steps)] == [_KEPT]
