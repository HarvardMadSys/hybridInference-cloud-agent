"""A local account goes from sign-up to a task on its own model, over the real application.

Registration, verification, sign-in, the waiting list, approval, model
connection storage, automatic verification, ``/config``, task admission,
follow-ups and the claim all run their production code against migrated
PostgreSQL. Three things are replaced: mail delivery (recorded), the
provider (an in-process OpenAI-compatible Chat Completions fake behind the
real verification logic; only DNS and the socket are faked), and the host
(a claim request). Every Gateway client method that would make an HTTP call
fails the test, so nothing here passes by consulting the old service.

These are regressions for the integration, not provider or sandbox
end-to-end acceptance: no real provider, mail server or Kata host is used.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from tests.integration.servers.test_pending_message_queue import _claim_body as _shared_claim
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations
from tests.support.model_connections import with_connection_capability
from tests.support.owners import on_connection

from cloud_agent.api import worker
from cloud_agent.api.deps import get_agent_app_credentials
from cloud_agent.api.worker import verify_claiming_host
from cloud_agent.application import model_connection_check
from cloud_agent.bootstrap.identity import attach_owner_directory, bind_identity_source
from cloud_agent.settings import reset_settings
from cloud_agent.storage.assembly import Storage
from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_CAPABILITY

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ADMIN = "test-only-local-onboarding-admin"
_PASSWORD = "PrivateExample73!"
_PROVIDER_KEY = "sk-test-onboarding-provider-key"
_BASE_URL = "https://provider.example/v1"
_MODEL = "provider/coder-1"
_HOST = "runner-onboarding"
_IMAGE = "sha256:queue-test-image"


def _db_config() -> dict[str, Any]:
    database = os.environ.get("TEST_DB_NAME", "hybridinference_test_db")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    if worker != "master":
        database += f"_{worker}"
    if "_test_" not in database:
        pytest.fail("Onboarding regressions require an explicitly named test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": database,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


def _sse(*events: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


class _ChatOnlyProvider:
    """An OpenAI-compatible provider that serves Chat Completions and nothing else."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": "not found"})
        assert request.headers["authorization"] == f"Bearer {_PROVIDER_KEY}"
        messages = json.loads(request.content)["messages"]
        tool = next((m for m in messages if m.get("role") == "tool"), None)
        if tool is not None:
            return _sse(
                {"choices": [{"delta": {"content": f"The result is {tool['content']}."}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            )
        match = re.search(r"check-[0-9a-f]+", json.dumps(messages))
        assert match is not None
        call = {"name": "cloud_agent_check", "arguments": json.dumps({"value": match.group()})}
        return _sse(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": call}]}}
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )


async def _public_address(host: str, port: int) -> list[str]:
    assert (host, port) == ("provider.example", 443)
    return ["93.184.216.34"]


@pytest_asyncio.fixture
async def env(monkeypatch):
    reset_settings()
    config = _db_config()
    apply_migrations(config)
    pool = await asyncpg.create_pool(**config, min_size=1, max_size=8)
    async with pool.acquire() as connection:
        await reset_agent_tables(connection)
    for key, value in {
        "AGENT_LOCAL_SESSION_SECRET": "onboarding-test-session-secret-over-32-bytes",
        "AGENT_WEB_BASE_URL": "https://agents.example",
        "AGENT_LOCAL_SIGNUP_ENABLED": "true",
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": "onboarding-test-connection-key",
        "AGENT_MODEL_CONNECTIONS_ENABLED": "true",
        "AGENT_CONTROL_TOKEN_SECRET": "onboarding-control-secret",
        "AGENT_SANDBOX_MANAGER_TOKEN": "onboarding-sandbox-manager-secret",
        "ADMIN_TOKEN": _ADMIN,
    }.items():
        monkeypatch.setenv(key, value)

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("onboarding reached the network outside the provider stand-in")

    # The provider is reached only through the pinned stand-in transport.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    provider = _ChatOnlyProvider()
    transport = httpx.MockTransport(provider)
    monkeypatch.setattr(model_connection_check, "_system_resolve", _public_address)
    monkeypatch.setattr(model_connection_check, "pinned_transport", lambda: transport)

    async def no_sandbox_manager(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(worker, "_worker_sandbox_manager", no_sandbox_manager)
    mails: list[Any] = []

    async def record_mail(mail: Any) -> bool:
        mails.append(mail)
        return True

    from cloud_agent.app import create_app

    app = create_app()
    storage = Storage(pool)
    app.state.storage = storage
    app.state.account_mailer = SimpleNamespace(configured=True, send=record_mail)
    await bind_identity_source(pool)
    attach_owner_directory(app, storage)
    await storage.host_pool.register_host(
        host=_HOST, credential_hash="h", capabilities=[SHARED_SANDBOX_CAPABILITY], slots_total=2
    )
    await storage.host_pool.record_host_heartbeat(
        host=_HOST, capabilities=[SHARED_SANDBOX_CAPABILITY], sandbox_image_digest=_IMAGE
    )
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    app.dependency_overrides[verify_claiming_host] = lambda: {"host": _HOST, "credential_hash": "h"}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://agents.example"
        ) as client:
            yield SimpleNamespace(client=client, mails=mails, pool=pool, provider=provider)
    finally:
        async with pool.acquire() as connection:
            await reset_agent_tables(connection)
        await pool.close()
        reset_settings()


def _token(mail: Any) -> str:
    return parse_qs(urlsplit(mail.link).fragment)["token"][0]


async def _approved_account(env: SimpleNamespace, email: str) -> dict[str, Any]:
    """Sign up, verify, sign in and be approved; returns ``/v1/session/me``."""
    client = env.client
    client.cookies.clear()
    credentials = {"email": email, "password": _PASSWORD}
    assert (
        await client.post("/v1/auth/signup", json={**credentials, "accepted_tos": True})
    ).status_code == 200
    verify = await client.post("/v1/auth/verify-email", json={"token": _token(env.mails[-1])})
    assert verify.status_code == 200, verify.text
    assert (await client.post("/v1/auth/login", json=credentials)).status_code == 200
    applied = await client.post("/v1/waitlist", json={"purpose": "my own coding tasks"})
    assert applied.status_code == 200, applied.text
    # Pending: no model connections, no tasks, nothing that reaches a provider.
    assert (await client.get("/v1/agent/model-connections")).status_code == 403
    assert (await client.get("/v1/agent/config")).status_code == 403
    me = (await client.get("/v1/session/me")).json()
    approved = await client.patch(
        f"/v1/admin/accounts/{me['id']}",
        headers={"Authorization": f"Bearer {_ADMIN}"},
        json={"action": "approve", "expected_version": applied.json()["version"]},
    )
    assert approved.status_code == 200, approved.text
    return (await client.get("/v1/session/me")).json()


async def _claim(env: SimpleNamespace) -> Any:
    body = with_connection_capability(_shared_claim("onboarding"))
    response = await env.client.post("/v1/agent/worker/claim", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_an_approved_account_sets_up_its_own_model_and_runs_a_task_on_it(env):
    client = env.client
    me = await _approved_account(env, "first@example.edu")
    assert me["agent_access"]["allowed"] is True

    # Approval is not model readiness: nothing is offered and setup is asked for.
    config = (await client.get("/v1/agent/config")).json()
    assert not {"models", "model_reasoning_efforts", "gateway_inference"} & config.keys()
    assert config["model_connections"] == []
    assert config["model_connections_enabled"] is True
    assert config["model_connection_required"] is True
    assert config["default_model"] is None and config["default_model_connection_id"] is None
    refused = await client.post(
        "/v1/agent/jobs",
        json={"repo": None, "task_prompt": "fix it", "model": _MODEL, "runtime": "opencode"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["type"] == "model_connection_required"
    # The Gateway's features are gone, not answered for it.
    pin = await client.get("/v1/agent/model-preference")
    assert pin.status_code == 404
    rewrite = await client.post(
        "/v1/agent/prompts/rewrite", json={"prompt": "fix it", "model": _MODEL}
    )
    assert rewrite.status_code == 404

    created = await client.post(
        "/v1/agent/model-connections",
        json={
            "name": "FreeInference",
            "base_url": _BASE_URL,
            "api_key": _PROVIDER_KEY,
            "models": [_MODEL],
        },
    )
    assert created.status_code == 201, created.text
    connection = created.json()
    assert _PROVIDER_KEY not in created.text and connection["has_api_key"] is True
    # The save itself completes the real streamed verification before persisting.
    assert connection["model_status"][0]["status"] == "ready"
    assert "opencode" in connection["model_status"][0]["runtimes"]
    chat_requests = [
        request
        for request in env.provider.requests
        if request.url.path.endswith("/chat/completions")
    ]
    assert len(chat_requests) == 2
    config = (await client.get("/v1/agent/config")).json()
    assert config["model_connection_required"] is False
    runtimes = config["model_connections"][0]["model_runtimes"][_MODEL]
    assert "opencode" in runtimes

    job = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": None,
            "task_prompt": "fix it",
            "model": _MODEL,
            "model_connection_id": connection["id"],
            "runtime": "opencode",
        },
    )
    assert job.status_code == 201, job.text
    job_body = job.json()
    assert (job_body["model"], job_body["model_connection_id"]) == (_MODEL, connection["id"])
    # The next new task offers the same source again, never a Gateway namesake.
    config = (await client.get("/v1/agent/config")).json()
    assert (config["default_model"], config["default_model_connection_id"]) == (
        _MODEL,
        connection["id"],
    )

    claim = await _claim(env)
    assert claim is not None and claim["job_id"] == job_body["id"]
    assert claim["model_connection"]["api_key"] == _PROVIDER_KEY
    assert claim["model_connection"]["base_url"] == _BASE_URL
    assert not claim.get("sandbox_token")
    async with env.pool.acquire() as conn:
        grants = await conn.fetchval(
            "SELECT count(*) FROM agent_attempts WHERE job_id = $1 AND grant_id IS NOT NULL",
            job_body["id"],
        )
    assert grants == 0


async def test_another_account_cannot_see_or_run_on_someone_elses_connection(env):
    client = env.client
    await _approved_account(env, "owner@example.edu")
    created = await client.post(
        "/v1/agent/model-connections",
        json={"name": "Mine", "base_url": _BASE_URL, "api_key": _PROVIDER_KEY, "models": [_MODEL]},
    )
    assert created.status_code == 201, created.text
    connection_id = created.json()["id"]

    await _approved_account(env, "other@example.edu")
    listed = await client.get("/v1/agent/model-connections")
    assert listed.status_code == 200 and listed.json()["connections"] == []
    config = (await client.get("/v1/agent/config")).json()
    assert config["model_connections"] == [] and config["model_connection_required"] is True
    borrowed = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": None,
            "task_prompt": "use theirs",
            "model": _MODEL,
            "model_connection_id": connection_id,
            "runtime": "opencode",
        },
    )
    assert borrowed.status_code == 400
    assert borrowed.json()["detail"]["error"]["reason"] == "not_found"
    requests_before = len(env.provider.requests)
    for method, body in (
        ("PATCH", {"enabled": False}),
        ("PATCH", {"models": [_MODEL, "another-model"]}),
        ("DELETE", None),
    ):
        response = await client.request(
            method, f"/v1/agent/model-connections/{connection_id}", json=body
        )
        assert response.status_code == 404, (method, response.text)
    assert len(env.provider.requests) == requests_before


async def test_revoked_access_stops_connection_management_and_new_turns(env):
    client = env.client
    me = await _approved_account(env, "revoked@example.edu")
    created = await client.post(
        "/v1/agent/model-connections",
        json={"name": "Mine", "base_url": _BASE_URL, "api_key": _PROVIDER_KEY, "models": [_MODEL]},
    )
    assert created.status_code == 201, created.text
    connection_id = created.json()["id"]
    job = await client.post(
        "/v1/agent/jobs",
        json={
            "repo": None,
            "task_prompt": "fix it",
            "model": _MODEL,
            "model_connection_id": connection_id,
            "runtime": "opencode",
        },
    )
    assert job.status_code == 201, job.text
    version = me["waitlist"]["version"]
    revoked = await client.patch(
        f"/v1/admin/accounts/{me['id']}",
        headers={"Authorization": f"Bearer {_ADMIN}"},
        json={"action": "revoke", "expected_version": version},
    )
    assert revoked.status_code == 200, revoked.text
    requests_before = len(env.provider.requests)

    # Still signed in, and told so; nothing else.
    status = (await client.get("/v1/session/me")).json()
    assert status["waitlist"]["status"] == "revoked"
    for method, path, body in (
        ("GET", "/v1/agent/model-connections", None),
        (
            "PATCH",
            f"/v1/agent/model-connections/{connection_id}",
            {"models": [_MODEL, "another-model"]},
        ),
        ("PATCH", f"/v1/agent/model-connections/{connection_id}", {"name": "Renamed"}),
        ("GET", "/v1/agent/config", None),
        ("POST", f"/v1/agent/threads/{job.json()['thread_id']}/messages", {"prompt": "more"}),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.text)
    assert len(env.provider.requests) == requests_before
    # The queued turn is not handed to a host with the owner's key.
    claim = await _claim(env)
    assert claim is None


async def test_follow_ups_keep_their_personal_source_and_never_fall_back(env):
    client = env.client
    await _approved_account(env, "follow@example.edu")
    created = await client.post(
        "/v1/agent/model-connections",
        json={"name": "Mine", "base_url": _BASE_URL, "api_key": _PROVIDER_KEY, "models": [_MODEL]},
    )
    assert created.status_code == 201, created.text
    connection_id = created.json()["id"]
    source = {"model": _MODEL, "model_connection_id": connection_id}
    job = await client.post(
        "/v1/agent/jobs",
        json=on_connection(
            {"repo": None, "task_prompt": "fix it", "runtime": "opencode", **source}
        ),
    )
    assert job.status_code == 201, job.text
    thread_id = job.json()["thread_id"]
    assert (await _claim(env))["job_id"] == job.json()["id"]

    # The same model id without its connection names no source, and nothing runs it.
    sourceless = await client.post(
        f"/v1/agent/threads/{thread_id}/messages", json={"prompt": "next", "model": _MODEL}
    )
    assert sourceless.status_code == 409, sourceless.text
    assert sourceless.json()["detail"]["error"]["type"] == "model_connection_required"
    # Waiting behind the running turn, with the source it was sent with.
    waiting = await client.post(
        f"/v1/agent/threads/{thread_id}/messages", json={"prompt": "next", **source}
    )
    assert waiting.status_code in (200, 201, 202), waiting.text
    inherited = await client.post(f"/v1/agent/threads/{thread_id}/messages", json={"prompt": "+"})
    assert inherited.status_code in (200, 201, 202), inherited.text
    async with env.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prompt, model, model_connection_id FROM agent_pending_messages "
            "WHERE thread_id = $1 ORDER BY position",
            thread_id,
        )
    assert [tuple(row) for row in rows] == [
        ("next", _MODEL, connection_id),
        ("+", None, None),
    ]

    # Disabling the connection holds the line rather than running elsewhere.
    disabled = await client.patch(
        f"/v1/agent/model-connections/{connection_id}", json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    config = (await client.get("/v1/agent/config")).json()
    assert config["model_connections"] == [] and config["model_connection_required"] is True
    refused = await client.post(
        "/v1/agent/jobs",
        json=on_connection({"repo": None, "task_prompt": "again", "runtime": "opencode", **source}),
    )
    assert refused.status_code == 400
    assert refused.json()["detail"]["error"]["reason"] == "disabled"
