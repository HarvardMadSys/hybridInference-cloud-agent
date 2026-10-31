"""Local accounts and owner-provided inference, through the real app and Postgres.

Account HTTP follows #1030 and model-connection HTTP follows #1029 at
1440c4874404417f495e07761dd3f45e4481e930. Only mail, DNS and provider transport
are substituted. Verification still parses a streaming tool-call round trip;
identity, admission, encryption, model selection and persistence are real.
These cases stop at task creation, without claiming host or provider E2E or
separate quota at the upstream account behind an owner's key.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from tests.integration.storage.test_independent_accounts_acceptance import (
    _ADMIN as ADMIN_TOKEN,
    _mail_token,
)
from tests.support.local_accounts import LOCAL_SECRET, PASSWORD, close_pool, open_pool
from tests.unit.test_model_connection_anthropic_check import _KEY as ROTATED_PROVIDER_KEY
from tests.unit.test_model_connection_compat_check import _KEY as PROVIDER_KEY, _chunk, _sse

from cloud_agent.app import create_app
from cloud_agent.application import model_connection_check
from cloud_agent.application.model_connections import model_connection_cipher
from cloud_agent.bootstrap.identity import attach_owner_directory, bind_identity_source
from cloud_agent.settings import reset_settings
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_WEB = "https://agents.example"
_CONNECTIONS = "/v1/agent/model-connections"
_JOBS = "/v1/agent/jobs"
_CONFIG = "/v1/agent/config"
_MODEL = "same-model"
_BASE = "https://freeinference.example/v1"
_OTHER_BASE = "https://models.example/v1"


class _Provider:
    """A Chat Completions provider; the other native protocols answer 404."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.failing_hosts: set[str] = set()
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.api_key = PROVIDER_KEY

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host in {"freeinference.example", "models.example"}
        assert PROVIDER_KEY not in str(request.url)
        self.requests.append(request)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if request.url.host in self.failing_hosts:
            # An upstream failure may echo the key; owner responses must not.
            return httpx.Response(401, json={"error": PROVIDER_KEY})
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": "Protocol not offered by this test provider"})
        assert request.headers["authorization"] == f"Bearer {self.api_key}"
        body = json.loads(request.content)
        assert body["model"] == _MODEL and body["stream"] is True
        tool_result = next((m for m in body["messages"] if m["role"] == "tool"), None)
        if tool_result is not None:
            assert tool_result["tool_call_id"] == "call_check"
            return _sse(_chunk({"content": tool_result["content"]}), _chunk({}, "stop"))
        nonce = re.search(r"check-[0-9a-f]+", json.dumps(body["messages"]))
        assert nonce is not None
        call = {
            "index": 0,
            "id": "call_check",
            "type": "function",
            "function": {
                "name": body["tools"][0]["function"]["name"],
                "arguments": json.dumps({"value": nonce.group()}),
            },
        }
        return _sse(_chunk({"role": "assistant", "tool_calls": [call]}), _chunk({}, "tool_calls"))


@pytest_asyncio.fixture
async def byok(monkeypatch):
    reset_settings()
    for name, value in {
        "AGENT_LOCAL_SESSION_SECRET": LOCAL_SECRET,
        "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY": LOCAL_SECRET,
        "AGENT_MODEL_CONNECTIONS_ENABLED": "true",
        "AGENT_LOCAL_SIGNUP_ENABLED": "true",
        "AGENT_SESSION_COOKIE_SECURE": "true",
        "AGENT_WEB_BASE_URL": _WEB,
        "ADMIN_TOKEN": ADMIN_TOKEN,
        # A legacy deployment default must confer no model access in local mode.
        "AGENT_DEFAULT_MODEL": _MODEL,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    monkeypatch.delenv("AGENT_EGRESS_PROTECTED_CIDRS", raising=False)

    async def forbidden_network(*args, **kwargs):
        pytest.fail("Local-account/BYOK acceptance attempted unmocked network I/O")

    def forbidden_sync_network(*args, **kwargs):
        pytest.fail("Local-account/BYOK acceptance attempted synchronous network I/O")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_sync_network)
    provider = _Provider()

    async def public_dns(host: str, port: int) -> list[str]:
        assert host in {"freeinference.example", "models.example"} and port == 443
        return ["8.8.8.8"]

    monkeypatch.setattr(model_connection_check, "_system_resolve", public_dns)
    monkeypatch.setattr(
        model_connection_check, "pinned_transport", lambda: httpx.MockTransport(provider)
    )
    mails = []

    async def capture_mail(mail):
        mails.append(mail)
        return True

    pool = await open_pool()
    try:
        app = create_app()
        app.state.storage = Storage(pool)
        app.state.source_control = None
        app.state.account_mailer = SimpleNamespace(configured=True, send=capture_mail)
        await bind_identity_source(pool)
        attach_owner_directory(app, app.state.storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_WEB
        ) as client:
            yield SimpleNamespace(client=client, app=app, pool=pool, mails=mails, provider=provider)
    finally:
        await close_pool(pool)
        reset_settings()


async def _login(state, email: str = "byok-owner@example.edu", *, client=None) -> dict[str, Any]:
    client = client or state.client
    credentials = {"email": email, "password": PASSWORD}
    response = await client.post("/v1/auth/signup", json={**credentials, "accepted_tos": True})
    assert response.status_code == 200, response.text
    assert state.mails[-1].to == email
    token = _mail_token(state.mails[-1])
    verified = await client.post("/v1/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    response = await client.post("/v1/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    # One account system: the session names no mode to choose between.
    assert "auth_mode" not in response.json()
    assert "HttpOnly" in response.headers["set-cookie"]
    assert response.json()["agent_access"]["allowed"] is False
    return response.json()


async def _review(state, owner, action: str, *, client=None):
    client = client or state.client
    current = await client.get("/v1/waitlist")
    assert current.status_code == 200, current.text
    response = await state.client.patch(
        f"/v1/admin/accounts/{owner['id']}",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"action": action, "expected_version": current.json()["version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _approve(state, owner, *, client=None) -> None:
    client = client or state.client
    pending = await client.post("/v1/waitlist", json={"purpose": "Use my own inference account"})
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"
    await _review(state, owner, "approve", client=client)
    current = await client.get("/v1/session/me")
    assert current.status_code == 200 and current.json()["agent_access"]["allowed"] is True


def _write_only(response: httpx.Response) -> None:
    assert PROVIDER_KEY not in response.text
    assert ROTATED_PROVIDER_KEY not in response.text
    assert '"api_key"' not in response.text and '"api_key_ciphertext"' not in response.text


async def _connect(state, *, client=None, name="My FreeInference", base_url=_BASE):
    client = client or state.client
    before = len(state.provider.requests)
    response = await client.post(
        _CONNECTIONS,
        json={"name": name, "base_url": base_url, "api_key": PROVIDER_KEY, "models": [_MODEL]},
    )
    assert response.status_code == 201, response.text
    _write_only(response)
    assert response.headers["cache-control"] == "no-store"
    view = response.json()
    assert view["has_api_key"] is True
    assert "protocol_mode" not in view and "manual_protocols" not in view
    assert view["model_status"][0]["status"] == "ready"
    assert "opencode" in view["model_status"][0]["runtimes"]
    calls = [
        request
        for request in state.provider.requests[before:]
        if request.url.path.endswith("/chat/completions")
    ]
    assert len(calls) == 2, "Readiness must include the streamed tool result round trip"
    assert all(str(request.url) == view["base_url"] + "/chat/completions" for request in calls)
    return view


async def _config(client):
    response = await client.get(_CONFIG)
    assert response.status_code == 200, response.text
    _write_only(response)
    body = response.json()
    # No platform model catalog: an account's models are its own connections'.
    assert not {"models", "model_reasoning_efforts", "gateway_inference"} & body.keys()
    assert body["model_connections_enabled"] is True
    usable = any(
        runtimes
        for entry in body["model_connections"]
        for runtimes in entry["model_runtimes"].values()
    )
    assert body["model_connection_required"] is (not usable)
    return body


async def _create_job(client, connection_id=None):
    body = {
        "repo": None,
        "task_prompt": "Inspect this temporary workspace",
        "runtime": "opencode",
        "model": _MODEL,
    }
    if connection_id is not None:
        body["model_connection_id"] = connection_id
    return await client.post(_JOBS, json=body)


async def _job_count(state):
    async with state.pool.acquire() as connection:
        return await connection.fetchval("SELECT count(*) FROM agent_jobs")


async def _no_source(client):
    response = await _create_job(client)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["type"] == "model_connection_required"


async def test_local_accounts_honor_an_explicitly_disabled_model_connection_gate(byok, monkeypatch):
    state = byok
    owner = await _login(state)
    await _approve(state, owner)
    connection = await _connect(state)
    calls = len(state.provider.requests)

    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "false")
    response = await state.client.get(_CONFIG)
    assert response.status_code == 200, response.text
    config = response.json()
    assert not {"models", "model_reasoning_efforts", "gateway_inference"} & config.keys()
    assert config["model_connections_enabled"] is False
    assert config["model_connections"] == [] and config["model_connection_required"] is True
    assert config["default_model"] is None and config["default_model_connection_id"] is None

    created = await state.client.post(
        _CONNECTIONS,
        json={
            "name": "Disabled gate",
            "base_url": _BASE,
            "api_key": PROVIDER_KEY,
            "models": [_MODEL],
        },
    )
    verified = await state.client.patch(
        f"{_CONNECTIONS}/{connection['id']}", json={"base_url": _OTHER_BASE}
    )
    task = await _create_job(state.client, connection["id"])
    for refused in (created, verified):
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["error"]["type"] == "model_connections_disabled"
        _write_only(refused)
    assert task.status_code == 400, task.text
    assert task.json()["detail"]["error"]["type"] == "model_connection_unusable"
    assert task.json()["detail"]["error"]["reason"] == "feature_disabled"
    _write_only(task)
    assert len(state.provider.requests) == calls and await _job_count(state) == 0


async def test_approved_local_account_uses_only_its_encrypted_selected_source(byok):
    state = byok
    client = state.client
    owner = await _login(state)
    assert (await client.get(_CONNECTIONS)).status_code == 403
    pending = await client.post("/v1/waitlist", json={"purpose": "Use my own inference account"})
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    for path in (_CONNECTIONS, _CONFIG):
        assert (await client.get(path)).status_code == 403
    denied = await client.post(
        _CONNECTIONS,
        json={"name": "Pending", "base_url": _BASE, "api_key": PROVIDER_KEY, "models": [_MODEL]},
    )
    assert denied.status_code == 403
    assert (await _create_job(client)).status_code == 403
    assert state.provider.requests == [] and await _job_count(state) == 0
    await _review(state, owner, "approve")

    empty = await _config(client)
    assert empty["model_connections"] == []
    assert empty["default_model"] is None and empty["default_model_connection_id"] is None
    await _no_source(client)
    connection = await _connect(state)
    async with state.pool.acquire() as db:
        row = await db.fetchrow(
            "SELECT * FROM agent_model_connections WHERE id = $1", connection["id"]
        )
    assert row["user_id"] == owner["id"]
    assert PROVIDER_KEY not in str(dict(row))
    assert model_connection_cipher().decrypt(row["api_key_ciphertext"]) == PROVIDER_KEY
    checks = json.loads(row["model_checks"])
    assert checks[_MODEL]["revision"] == row["config_revision"]
    assert checks[_MODEL]["protocols"]["chat_completions"]["status"] == "supported"
    listed = await client.get(_CONNECTIONS)
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    _write_only(listed)
    assert [entry["id"] for entry in listed.json()["connections"]] == [connection["id"]]
    options = await _config(client)
    option = next(
        entry for entry in options["model_connections"] if entry["id"] == connection["id"]
    )
    assert option["models"] == [_MODEL] and "opencode" in option["model_runtimes"][_MODEL]
    assert options["default_model"] is None and options["default_model_connection_id"] is None
    await _no_source(client)

    created = await _create_job(client, connection["id"])
    assert created.status_code == 201, created.text
    _write_only(created)
    job = created.json()
    assert (job["model"], job["model_connection_id"]) == (_MODEL, connection["id"])
    async with state.pool.acquire() as db:
        row = await db.fetchrow(
            "SELECT user_id, model, model_connection_id, state FROM agent_jobs WHERE id = $1",
            job["id"],
        )
    assert dict(row) == {
        "user_id": owner["id"],
        "model": _MODEL,
        "model_connection_id": connection["id"],
        "state": "queued",
    }
    remembered = await _config(client)
    assert (remembered["default_model"], remembered["default_model_connection_id"]) == (
        _MODEL,
        connection["id"],
    )
    calls = len(state.provider.requests)
    await _review(state, owner, "revoke")
    assert (await client.get("/v1/session/me")).json()["agent_access"]["allowed"] is False
    for path in (_CONNECTIONS, _CONFIG):
        assert (await client.get(path)).status_code == 403
    refused = await client.patch(
        f"{_CONNECTIONS}/{connection['id']}", json={"base_url": _OTHER_BASE}
    )
    assert refused.status_code == 403
    assert (await _create_job(client, connection["id"])).status_code == 403
    assert len(state.provider.requests) == calls and await _job_count(state) == 1
    async with state.pool.acquire() as db:
        assert (
            await db.fetchval("SELECT state FROM agent_jobs WHERE id = $1", job["id"])
            == "cancelled"
        )


async def test_another_approved_account_cannot_read_verify_or_run_the_owners_connection(byok):
    state = byok
    owner = await _login(state)
    await _approve(state, owner)
    private = await _connect(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=state.app), base_url=_WEB
    ) as other:
        outsider = await _login(state, "byok-other@example.edu", client=other)
        await _approve(state, outsider, client=other)
        own = await _connect(state, client=other, name="My other provider", base_url=_OTHER_BASE)
        calls = len(state.provider.requests)
        listed = await other.get(_CONNECTIONS)
        assert [entry["id"] for entry in listed.json()["connections"]] == [own["id"]]
        options = await _config(other)
        assert [entry["id"] for entry in options["model_connections"]] == [own["id"]]
        changed = await other.patch(f"{_CONNECTIONS}/{private['id']}", json={"enabled": False})
        deleted = await other.delete(f"{_CONNECTIONS}/{private['id']}")
        verified = await other.patch(
            f"{_CONNECTIONS}/{private['id']}", json={"base_url": _OTHER_BASE}
        )
        assert [changed.status_code, deleted.status_code, verified.status_code] == [404, 404, 404]
        refused = await _create_job(other, private["id"])
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"]["error"]["reason"] == "not_found"
        assert await _job_count(state) == 0 and len(state.provider.requests) == calls
        allowed = await _create_job(other, own["id"])
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["model_connection_id"] == own["id"]
    async with state.pool.acquire() as db:
        assert (
            await db.fetchval(
                "SELECT enabled FROM agent_model_connections WHERE id = $1", private["id"]
            )
            is True
        )


@pytest.mark.parametrize("unavailable", ["disabled", "deleted"])
async def test_unusable_selected_source_never_falls_back_to_another_same_name_model(
    byok, unavailable
):
    state = byok
    client = state.client
    owner = await _login(state)
    await _approve(state, owner)
    selected = await _connect(state)
    alternative = await _connect(state, name="Same model elsewhere", base_url=_OTHER_BASE)
    created = await _create_job(client, selected["id"])
    assert created.status_code == 201, created.text
    if unavailable == "disabled":
        changed = await client.patch(f"{_CONNECTIONS}/{selected['id']}", json={"enabled": False})
        assert changed.status_code == 200, changed.text
    else:
        assert (await client.delete(f"{_CONNECTIONS}/{selected['id']}")).status_code == 204
        async with state.pool.acquire() as db:
            assert (
                await db.fetchval(
                    "SELECT api_key_ciphertext FROM agent_model_connections WHERE id = $1",
                    selected["id"],
                )
                is None
            )
    before = await _job_count(state)
    calls = len(state.provider.requests)
    options = await _config(client)
    assert options["default_model"] is None and options["default_model_connection_id"] is None
    available = {entry["id"]: entry for entry in options["model_connections"]}
    assert "opencode" in available[alternative["id"]]["model_runtimes"][_MODEL]
    assert selected["id"] not in available
    refused = await _create_job(client, selected["id"])
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error"]["reason"] == unavailable
    await _no_source(client)
    assert await _job_count(state) == before and len(state.provider.requests) == calls


async def test_failed_verification_leaves_no_saved_connection_or_key(byok):
    state = byok
    client = state.client
    owner = await _login(state)
    await _approve(state, owner)
    state.provider.failing_hosts.add("freeinference.example")
    state.provider.release = asyncio.Event()
    saving = asyncio.create_task(
        client.post(
            _CONNECTIONS,
            json={
                "name": "Unusable",
                "base_url": _BASE,
                "api_key": PROVIDER_KEY,
                "models": [_MODEL],
            },
        )
    )
    try:
        # A real provider request has started, but its result has not arrived.
        await asyncio.wait_for(state.provider.entered.wait(), timeout=5)
        async with state.pool.acquire() as db:
            assert await db.fetchval("SELECT count(*) FROM agent_model_connections") == 0
        assert (await _config(client))["model_connections"] == []
    finally:
        state.provider.release.set()
        refused = await asyncio.wait_for(saving, timeout=5)
    assert refused.status_code == 422, refused.text
    assert refused.headers["cache-control"] == "no-store"
    _write_only(refused)
    error = refused.json()["detail"]["error"]
    assert error["type"] == "model_connection_verification_failed"
    assert [
        (result["model"], result["status"], result["runtimes"]) for result in error["models"]
    ] == [(_MODEL, "failed", [])]
    async with state.pool.acquire() as db:
        assert await db.fetchval("SELECT count(*) FROM agent_model_connections") == 0
    listed = await client.get(_CONNECTIONS)
    assert listed.status_code == 200 and listed.json()["connections"] == []
    empty = await _config(client)
    assert empty["model_connection_required"] is True and empty["model_connections"] == []
    assert empty["default_model"] is None and empty["default_model_connection_id"] is None
    await _no_source(client)
    assert await _job_count(state) == 0


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("action", ["revoke", "suspend"])
async def test_account_access_ending_during_verification_refuses_the_late_save(
    byok, operation, action
):
    """A successful provider check grants no permission after account access ends."""
    state = byok
    client = state.client
    owner = await _login(state)
    await _approve(state, owner)
    if operation == "update":
        connection = await _connect(state)
        state.provider.api_key = ROTATED_PROVIDER_KEY
        method, path = "PATCH", f"{_CONNECTIONS}/{connection['id']}"
        body = {"api_key": ROTATED_PROVIDER_KEY}
    else:
        method, path = "POST", _CONNECTIONS
        body = {
            "name": "Late connection",
            "base_url": _BASE,
            "api_key": PROVIDER_KEY,
            "models": [_MODEL],
        }
    async with state.pool.acquire() as db:
        before = [dict(row) for row in await db.fetch("SELECT * FROM agent_model_connections")]
    calls_before = len(state.provider.requests)
    state.provider.entered.clear()
    state.provider.release = asyncio.Event()
    saving = asyncio.create_task(client.request(method, path, json=body))
    try:
        await asyncio.wait_for(state.provider.entered.wait(), timeout=5)
        assert not saving.done(), "The save must still be waiting for its provider"
        # The real admin transaction and cleanup finish before verification resumes.
        await _review(state, owner, action)
    finally:
        state.provider.release.set()
        refused = await asyncio.wait_for(saving, timeout=5)
    completed_checks = [
        request
        for request in state.provider.requests[calls_before:]
        if request.url.path.endswith("/chat/completions")
    ]
    assert len(completed_checks) == 2, "The provider successfully completed the tool round trip"
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["error"]["type"] == "model_connection_access_ended"
    assert refused.headers["cache-control"] == "no-store"
    _write_only(refused)
    async with state.pool.acquire() as db:
        after = [dict(row) for row in await db.fetch("SELECT * FROM agent_model_connections")]
    assert after == before, "A late save must neither add a connection nor rotate a stored key"
