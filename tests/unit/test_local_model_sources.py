"""Local accounts run only on their own model connections; nothing reaches a Gateway catalog or grant."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.accounts import config
from cloud_agent.accounts.directory import (
    LocalModeGateway,
    LocalModeGrantClient,
    ModelConnectionRequired,
)
from cloud_agent.application.job_admission import model_catalog_unavailable
from cloud_agent.application.model_connections import model_connections_enabled
from cloud_agent.bootstrap.identity import bind_identity_source
from cloud_agent.pr_titles import generate_pr_title

pytestmark = pytest.mark.asyncio

_SECRET = "local-session-secret-for-tests-0123456789"


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv(config.ENV_AUTH_MODE, "local")
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, _SECRET)
    monkeypatch.delenv("AGENT_MODEL_CONNECTIONS_ENABLED", raising=False)


@pytest.mark.parametrize("mode", ["gateway", "local"])
async def test_both_identity_modes_honor_the_explicit_rollout_gate(monkeypatch, mode):
    monkeypatch.setenv(config.ENV_AUTH_MODE, mode)
    monkeypatch.delenv("AGENT_MODEL_CONNECTIONS_ENABLED", raising=False)
    assert model_connections_enabled() is False
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "false")
    assert model_connections_enabled() is False
    monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", "true")
    assert model_connections_enabled() is True


async def test_local_mode_will_not_start_without_the_connection_key(local, monkeypatch):
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
    pool = SimpleNamespace()  # touching the database at all would fail
    with pytest.raises(config.LocalAuthNotConfigured, match="AGENT_SOURCE_CONTROL_ENCRYPTION_KEY"):
        await bind_identity_source(SimpleNamespace(state=SimpleNamespace()), pool)


async def test_a_gateway_model_lookup_is_a_verdict_not_an_outage(local):
    gateway = LocalModeGateway(directory=AsyncMock())
    with pytest.raises(ModelConnectionRequired) as refused:
        await gateway.model_catalog_bundle("01J0000000000000000000WNER")
    answer = model_catalog_unavailable(refused.value)
    assert answer.status_code == 409
    assert answer.detail["error"]["type"] == "model_connection_required"


async def test_a_personal_source_publication_never_asks_the_gateway_for_a_title(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example/")
    grants = AsyncMock()
    job = {
        "job_id": "ajob_1",
        "snapshot_id": 1,
        "publish_attempt_count": 0,
        "user_id": "owner",
        "model": "provider/coder-1",
        "model_connection_id": "mconn_" + "0" * 32,
        "session_messages": [{"role": "user", "content": "Fix it."}],
    }
    assert await generate_pr_title(job, ["a.py"], grants=grants) is None
    grants.mint.assert_not_called()


async def test_the_local_grant_client_refuses_without_a_network_call():
    grants = LocalModeGrantClient()
    assert grants.configured is False
    with pytest.raises(Exception, match="not available to local accounts"):
        await grants.mint(user_id="u", job_id="j", attempt_id="a", models=["m"])
