"""Connections are reused across calls, and released when the process ends.

Every one of these paths used to build an `httpx.AsyncClient` per call, so a
request paid a TCP connect — and a TLS handshake, where the peer is behind TLS
— before it did anything. On the interactive paths that is not occasional:
admission runs on every owner route including terminal input, repository
entitlement runs on every file read, and both were reconnecting each time.

`app.py` already carried a comment saying the grant client shared a pool. It
did not; the seam existed and nothing was passed to it. That is the reason
these are tests and not a comment: a claim about pooling that only a human
reads is a claim nothing checks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import broker_routing
from cloud_agent.app import lifespan
from cloud_agent.github_app import AppConfig, GitHubAppCredentials

_KEY = "-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n-----END RSA PRIVATE KEY-----"


def _credentials(**kwargs) -> GitHubAppCredentials:
    return GitHubAppCredentials(AppConfig(app_id="1", private_key=_KEY), **kwargs)


async def test_the_lifespan_gives_both_gateway_clients_one_pool(monkeypatch) -> None:
    """The sharing `app.py` has claimed in a comment since E9, now actual."""
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace", AsyncMock(return_value=None)
    )
    app = FastAPI()

    async with lifespan(app, background=False):
        shared = app.state.http
        assert app.state.gateway._client is shared
        assert app.state.grants._client is shared
        assert not shared.is_closed

    assert shared.is_closed, "the pool outlived the application that owned it"


async def test_the_github_app_keeps_one_client_for_its_lifetime() -> None:
    """Entitlement resolves through this object on every workspace request."""
    credentials = _credentials()

    async with credentials._http() as first:
        pass
    async with credentials._http() as second:
        pass

    assert first is second
    assert not first.is_closed

    await credentials.aclose()
    assert first.is_closed


async def test_a_borrowed_github_client_survives_aclose() -> None:
    """A client passed in belongs to the caller; closing it is not ours to do."""
    borrowed = httpx.AsyncClient()
    credentials = _credentials(client=borrowed)

    async with credentials._http() as client:
        assert client is borrowed

    await credentials.aclose()
    assert not borrowed.is_closed
    await borrowed.aclose()


@pytest.fixture
async def broker_pools(monkeypatch):
    """Broker pools are process state; hand each test a clean, closed-after set."""
    monkeypatch.setenv(broker_routing.ENV_BROKER_TOKEN, "broker-token")
    await broker_routing.close_broker_clients()
    yield
    await broker_routing.close_broker_clients()


async def test_one_pool_per_broker_url(broker_pools) -> None:
    """A client is built per request, so the pool cannot live on the client."""
    first = broker_routing._client_for({"broker_url": "http://host-a:9000"})
    second = broker_routing._client_for({"broker_url": "http://host-a:9000"})
    other = broker_routing._client_for({"broker_url": "http://host-b:9000"})

    assert first is not second, "these are still built per request"
    assert first._client is second._client
    assert other._client is not first._client


async def test_closing_the_pools_lets_the_next_request_open_a_new_one(broker_pools) -> None:
    """Shutdown must release them, and must not leave a closed one behind."""
    before = broker_routing._client_for({"broker_url": "http://host-a:9000"})._client

    await broker_routing.close_broker_clients()
    assert before.is_closed

    after = broker_routing._client_for({"broker_url": "http://host-a:9000"})._client
    assert after is not before
    assert not after.is_closed


async def test_a_host_with_no_broker_url_still_gets_no_client(broker_pools) -> None:
    """Pooling must not turn an unreachable host row into a reachable one."""
    assert broker_routing._client_for({"broker_url": ""}) is None
    assert broker_routing._client_for({}) is None
