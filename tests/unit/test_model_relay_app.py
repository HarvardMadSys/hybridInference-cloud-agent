"""The model relay listener: nothing but the relay and a health check, with its own client."""

from __future__ import annotations

import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from cloud_agent import app as app_module
from cloud_agent.tokens import mint_residency_model_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "model-relay-app-test-secret")


async def test_the_listener_owns_no_background_work_and_forwards_with_its_own_client(monkeypatch):
    seen: list[dict] = []

    @contextlib.asynccontextmanager
    async def recording(app, **kwargs):
        seen.append(kwargs)
        yield

    monkeypatch.setattr(app_module, "lifespan", recording)
    relay = app_module.create_model_relay_app()
    async with relay.router.lifespan_context(relay):
        client = relay.state.model_relay_http
        # Pinned dials, no proxy from the environment, no redirects followed.
        assert client.follow_redirects is False and client.trust_env is False
        assert not client.is_closed
    assert client.is_closed
    assert seen == [{"background": False}]


async def test_the_listener_contains_only_the_relay(monkeypatch):
    @contextlib.asynccontextmanager
    async def nothing(app, **kwargs):
        yield

    monkeypatch.setattr(app_module, "lifespan", nothing)
    relay = app_module.create_model_relay_app()

    async with AsyncClient(transport=ASGITransport(app=relay), base_url="http://relay") as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        # Every other path is the relay's, and without a placeholder it answers nothing.
        for path in ("/docs", "/openapi.json", "/v1/agent/jobs", "/admin/agent/hosts", "/mcp"):
            response = await client.get(path)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "invalid_credential"


@pytest.mark.parametrize("factory", ["create_app", "create_mcp_app", "create_github_relay_app"])
async def test_no_other_listener_serves_the_model_relay(factory, monkeypatch):
    """A placeholder carried out of a sandbox buys nothing at the public edge or another relay."""

    @contextlib.asynccontextmanager
    async def nothing(app, **kwargs):
        yield

    monkeypatch.setattr(app_module, "lifespan", nothing)
    other = getattr(app_module, factory)()
    placeholder = mint_residency_model_token(thread_id="athr_1", residency_generation=1)
    async with AsyncClient(transport=ASGITransport(app=other), base_url="http://other") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {placeholder}"},
            json={"model": "m", "messages": []},
        )
    # Not the relay's answer in any form: no such route, or another relay's refusal.
    assert response.status_code in (401, 404, 405, 421)
    assert "invalid_credential" not in response.text and "provided_unavailable" not in response.text
