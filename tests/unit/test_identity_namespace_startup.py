"""Reject a foreign database identity before starting any Agent background work."""

from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI

from cloud_agent.app import lifespan
from cloud_agent.identity import IdentityNotConfigured
from cloud_agent.identity_namespace import IdentityNamespaceError


@pytest.mark.parametrize("background", [True, False])
async def test_namespace_mismatch_stops_both_listeners_and_closes_pool(monkeypatch, background):
    pool = AsyncMock()
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.setenv("GATEWAY_ISSUER", "https://new.example")
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("cloud_agent.storage.store.AgentJobStore.initialize", AsyncMock())
    check = AsyncMock(side_effect=IdentityNamespaceError("issuer mismatch"))
    monkeypatch.setattr("cloud_agent.identity_namespace.ensure_gateway_namespace", check)
    app = FastAPI()
    with pytest.raises(IdentityNamespaceError):
        async with lifespan(app, background=background):
            pytest.fail("A foreign namespace must never start serving")
    check.assert_awaited_once_with(pool, "https://new.example")
    pool.close.assert_awaited_once()
    assert not hasattr(app.state, "gateway")
    assert not hasattr(app.state, "reaper_task")


async def test_configured_gateway_requires_an_explicit_issuer(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("cloud_agent.storage.store.AgentJobStore.initialize", AsyncMock())
    with pytest.raises(IdentityNotConfigured):
        async with lifespan(FastAPI(), background=False):
            pytest.fail("The transport URL must never stand in for the issuer")
    pool.close.assert_awaited_once()
