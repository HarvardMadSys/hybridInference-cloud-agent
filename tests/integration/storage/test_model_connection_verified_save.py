"""Verify before saving, through the real routes and real PostgreSQL.

The provider is faked at ``verify_model``; everything else -- the hourly
budget, the insert, the guarded update and the rows they leave -- is the
production code against the database.
"""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.integration.storage.test_agent_job_store import pool, store  # noqa: F401

from cloud_agent.api import model_connections as api
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.application import model_connection_check as check
from cloud_agent.application.model_connections import model_connection_cipher

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

PATH = "/v1/agent/model-connections"
_KEY = "sk-fixture-owner-key-0123456789"
_WORKS = {
    "checked_at": "2026-10-28T00:00:00+00:00",
    "protocols": {
        "chat_completions": {"status": "supported", "category": "supported", "http_status": None},
        "responses": {"status": "unsupported", "category": "endpoint_missing", "http_status": 404},
        "anthropic_messages": {
            "status": "unsupported",
            "category": "endpoint_missing",
            "http_status": 404,
        },
    },
}
_FAILS = {
    "checked_at": "2026-10-28T00:00:00+00:00",
    "protocols": {
        protocol: {"status": "failed", "category": "auth", "http_status": 401}
        for protocol in ("chat_completions", "responses", "anthropic_messages")
    },
}


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "verified-save-test-key-0123456789ab")
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "true")


class _Provider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.answer: dict[str, Any] = _WORKS
        self.during: Any = None

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs["model"])
        if self.during is not None:
            await self.during()
            self.during = None
        return self.answer


@pytest.fixture
def provider(monkeypatch) -> _Provider:
    fake = _Provider()
    monkeypatch.setattr(check, "verify_model", fake)
    return fake


def _client(store) -> AsyncClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "user-1"}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_CREATE = {
    "name": "Mine",
    "base_url": "https://models.example/v1",
    "api_key": _KEY,
    "models": ["gpt-4.1", "o4-mini"],
}


async def _rows(pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch("SELECT * FROM agent_model_connections")]


async def _checks(pool) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM agent_model_connection_checks"))


async def test_a_failed_create_leaves_no_connection_and_no_key(pool, store, provider):
    provider.answer = _FAILS
    async with _client(store) as client:
        refused = await client.post(PATH, json=_CREATE)

    assert refused.status_code == 422, refused.text
    assert _KEY not in refused.text
    assert await _rows(pool) == []
    # The attempt still counted against the owner's budget, one per model.
    assert await _checks(pool) == 2 and provider.calls == ["gpt-4.1", "o4-mini"]


async def test_a_verified_create_lands_with_its_facts_in_one_insert(pool, store, provider):
    async with _client(store) as client:
        created = await client.post(PATH, json=_CREATE)

    assert created.status_code == 201, created.text
    [row] = await _rows(pool)
    assert model_connection_cipher().decrypt(row["api_key_ciphertext"]) == _KEY
    stored = await store.model_connections.get_model_connection(
        user_id="user-1", connection_id=row["id"]
    )
    assert {m: r["revision"] for m, r in stored["model_checks"].items()} == {
        "gpt-4.1": 1,
        "o4-mini": 1,
    }


async def test_a_create_past_the_budget_dials_nothing_and_stores_nothing(pool, store, provider):
    reserved = await store.model_connections.reserve_model_connection_checks(
        user_id="user-1", count=check.CHECKS_PER_HOUR - 1, limit=check.CHECKS_PER_HOUR, seconds=3600
    )
    assert reserved
    async with _client(store) as client:
        refused = await client.post(PATH, json=_CREATE)

    assert refused.status_code == 429, refused.text
    assert provider.calls == [] and await _rows(pool) == []
    assert await _checks(pool) == check.CHECKS_PER_HOUR - 1


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "sk-fixture-replacement-0000"},
        {"base_url": "https://moved.example/v1"},
        {"models": ["gpt-4.1", "o4-mini", "gpt-5"]},
    ],
)
async def test_a_failed_edit_leaves_the_working_row_byte_for_byte(pool, store, provider, change):
    async with _client(store) as client:
        assert (await client.post(PATH, json=_CREATE)).status_code == 201
        [before] = await _rows(pool)
        provider.answer = _FAILS
        refused = await client.patch(f"{PATH}/{before['id']}", json=change)

    assert refused.status_code == 422, refused.text
    assert await _rows(pool) == [before]


async def test_a_disable_made_while_a_new_key_is_verified_wins(pool, store, provider):
    async with _client(store) as client:
        created = (await client.post(PATH, json=_CREATE)).json()

        async def disable() -> None:
            await store.model_connections.update_model_connection(
                user_id="user-1", connection_id=created["id"], changes={"enabled": False}
            )

        provider.during = disable
        late = await client.patch(
            f"{PATH}/{created['id']}", json={"api_key": "sk-fixture-replacement-0000"}
        )

    assert late.status_code == 409, late.text
    [row] = await _rows(pool)
    assert row["enabled"] is False
    assert model_connection_cipher().decrypt(row["api_key_ciphertext"]) == _KEY
