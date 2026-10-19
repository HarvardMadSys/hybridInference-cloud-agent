"""A body a person sends is refused with a 422 when it holds text Postgres cannot take.

A NUL in a prompt, a title, a goal, an environment's name or an MCP connection
used to reach the first query that read it and fail there with a 500, and
half a surrogate pair failed the same way in a field no check had measured.
What a host reports is still kept, with the characters replaced. These go
through the application ``create_app`` builds, over the real store.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.integration.servers.test_pending_message_queue import (
    _HOST,
    _HOST_CREDENTIAL,
    _IMAGE_DIGEST,
    _OWNER,
    _REPO,
    _claim_body,
    _Gateway,
    _Grants,
    pool as pool,
)

from cloud_agent.api import worker
from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_grant_client,
    get_log_store,
    get_storage,
)
from cloud_agent.api.worker import verify_claiming_host
from cloud_agent.app import create_app
from cloud_agent.storage.assembly import Storage
from cloud_agent_shared.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def http(pool, monkeypatch):
    """The real application, an owner with a team, an environment and a chat, and a host."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "person-bodies-key")
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", _REPO)
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "person-bodies-secret")
    monkeypatch.setenv("AGENT_SANDBOX_MANAGER_TOKEN", "person-bodies-sandbox-manager")
    store = Storage(pool)
    await store.host_pool.register_host(
        host=_HOST,
        credential_hash=_HOST_CREDENTIAL,
        capabilities=[SHARED_SANDBOX_CAPABILITY],
        slots_total=4,
    )
    await store.host_pool.record_host_heartbeat(
        host=_HOST,
        capabilities=[SHARED_SANDBOX_CAPABILITY, NATIVE_FORK_AT_ANCHOR_CAPABILITY],
        sandbox_image_digest=_IMAGE_DIGEST,
    )

    async def no_sandbox_manager(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(worker, "_worker_sandbox_manager", no_sandbox_manager)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_gateway_client] = _Gateway
    app.dependency_overrides[get_grant_client] = _Grants
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[get_log_store] = lambda: None
    app.dependency_overrides[authenticate_agent_owner] = lambda: {
        "user_id": _OWNER,
        "role": "free",
        "authenticated": True,
    }
    app.dependency_overrides[verify_claiming_host] = lambda: {
        "host": _HOST,
        "credential_hash": _HOST_CREDENTIAL,
    }
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        team = await store.teams.create_team(_OWNER, name="Platform", request_key="bodies")
        environment = await client.post(
            "/v1/agent/environments",
            json={"name": "Development", "repo": _REPO, "revision": 0, "setup_script": ""},
        )
        assert environment.status_code == 201, environment.text
        job = await client.post(
            "/v1/agent/jobs",
            json={"repo": _REPO, "task_prompt": "fix it", "model": "glm-5.1", "base_sha": "a" * 40},
        )
        assert job.status_code == 201, job.text
        yield (
            client,
            {
                "team": team.id,
                "env": environment.json()["id"],
                "job": job.json()["id"],
                "thread": job.json()["thread_id"],
            },
        )


def _send(client: AsyncClient, method: str, path: str, body: dict[str, Any]) -> Any:
    """Send ``body`` as a browser's JSON encoder writes it, escapes and all."""
    return client.request(
        method,
        path,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "POST",
            "/v1/agent/jobs",
            {"repo": _REPO, "task_prompt": "fix\x00it", "model": "glm-5.1", "base_sha": "a" * 40},
        ),
        ("POST", "/v1/agent/threads/{thread}/messages", {"prompt": "and\x00then"}),
        ("PATCH", "/v1/agent/jobs/{job}/title", {"title": "A\x00title"}),
        ("PUT", "/v1/agent/threads/{thread}/goal", {"goal": {"objective": "Ship\x00it"}}),
        ("POST", "/v1/agent/environments", {"name": "Dev\x00", "repo": _REPO, "revision": 0}),
        (
            "POST",
            "/v1/agent/environments",
            {"name": "Team dev", "repo": _REPO, "revision": 0, "team_id": "{team}\x00"},
        ),
        (
            "POST",
            "/v1/agent/environment-runs",
            {"environment_id": "{env}\x00", "environment_revision": 1, "global_revision": 0},
        ),
        (
            "POST",
            "/v1/agent/environment-import/preview",
            {"environment_id": "{env}\x00", "revision": 1, "text": "A=1"},
        ),
        ("POST", "/v1/agent/mcp/connections", {"label": "Docs\x00", "url": "https://mcp.test/"}),
        ("POST", "/v1/agent/mcp/connections", {"label": "Docs", "url": "https://mcp.test/\ud83d"}),
    ],
)
async def test_a_body_postgres_cannot_take_is_refused(
    http: tuple[AsyncClient, dict[str, str]], method: str, path: str, body: dict[str, Any]
):
    client, ids = http
    filled = json.loads(
        json.dumps(body).replace("{team}", ids["team"]).replace("{env}", ids["env"])
    )

    response = await _send(client, method, path.format(**ids), filled)

    assert response.status_code == 422, response.text


async def test_what_a_host_reports_is_still_kept(http: tuple[AsyncClient, dict[str, str]]):
    """Only a person's body is refused; a turn's tool output is stored with U+FFFD."""
    client, ids = http
    claim = await client.post("/v1/agent/worker/claim", json=_claim_body("w"))
    assert claim.status_code == 200, claim.text
    token = claim.json()["worker_token"]

    event = await client.post(
        f"/v1/agent/worker/jobs/{ids['job']}/events",
        content=json.dumps(
            {"event_type": "tool_result", "payload": {"content": "cat\x00/proc/self/cmdline"}}
        ).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert event.status_code == 201, event.text
    events = (await client.get(f"/v1/agent/jobs/{ids['job']}/events")).json()["events"]
    assert events[-1]["payload"] == {"content": "cat\ufffd/proc/self/cmdline"}
