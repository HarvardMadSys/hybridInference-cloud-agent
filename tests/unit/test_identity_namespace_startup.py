"""Reject a foreign database identity before starting any Agent background work."""

from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI
from tests.support.owners import use_session_secret

from cloud_agent.app import lifespan
from cloud_agent.identity_namespace import IdentityNamespaceError


@pytest.mark.parametrize("background", [True, False])
async def test_a_refused_database_stops_both_listeners_and_closes_the_pool(monkeypatch, background):
    pool = AsyncMock()
    use_session_secret(monkeypatch)
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "namespace-startup-test-key")
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock())
    check = AsyncMock(side_effect=IdentityNamespaceError("bound to a Gateway"))
    monkeypatch.setattr("cloud_agent.identity_namespace.ensure_local_namespace", check)
    app = FastAPI()
    with pytest.raises(IdentityNamespaceError):
        async with lifespan(app, background=background):
            pytest.fail("A refused database must never start serving")
    check.assert_awaited_once_with(pool)
    pool.close.assert_awaited_once()
    assert not hasattr(app.state, "owners")
    assert not hasattr(app.state, "reaper_task")
