"""Reject a foreign database identity before starting any Agent background work."""

from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI

from cloud_agent.app import lifespan
from cloud_agent.identity import IdentityNotConfigured
from cloud_agent.identity_namespace import IdentityNamespaceError
from cloud_agent.settings import apply_settings


@pytest.mark.parametrize("background", [True, False])
async def test_namespace_mismatch_stops_both_listeners_and_closes_pool(monkeypatch, background):
    pool = AsyncMock()
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.setenv("GATEWAY_ISSUER", "https://new.example")
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock())
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


@pytest.mark.parametrize("saved", [False, True])
async def test_incomplete_gateway_keeps_setup_available_but_requires_an_issuer_for_identity(
    monkeypatch, saved
):
    pool = AsyncMock()
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    if saved:
        monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))

    async def initialize(repository, *, seed):
        apply_settings(1, {"gateway_base_url": "https://gateway.example"})

    monkeypatch.setattr("cloud_agent.settings.initialize_settings", initialize)
    check = AsyncMock(return_value=None)
    monkeypatch.setattr("cloud_agent.identity_namespace.ensure_gateway_namespace", check)
    monkeypatch.setattr("cloud_agent.bootstrap.lifespan.listen", AsyncMock())
    app = FastAPI()
    async with lifespan(app, background=False):
        from cloud_agent.identity import gateway_issuer
        from cloud_agent.settings import settings_response

        assert app.state.storage is not None
        assert app.state.gateway_issuer is None
        assert settings_response(1, {"gateway_base_url": "https://gateway.example"})["setup"][
            "required"
        ]
        with pytest.raises(IdentityNotConfigured):
            gateway_issuer()
        assert not app.state.gateway.configured
    check.assert_awaited_once_with(pool, None)
    pool.close.assert_awaited_once()
