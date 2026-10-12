"""Prompt rewriting requires a current owner session and Agent permission."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.api import prompt_rewrite
from cloud_agent.api.deps import get_gateway_client, get_grant_client
from cloud_agent.prompt_rewrite import PromptRewriteError, PromptRewritePrompt


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(
        identity.ENV_SESSION_SECRET, "rewrite-test-session-secret-long-enough-for-hs256"
    )
    app = FastAPI()
    app.include_router(prompt_rewrite.router)
    gateway = AsyncMock()
    gateway.agent_access.return_value = {
        "user_id": "owner-1",
        "allowed": True,
        "permissions": ["agent.use"],
    }
    app.state.gateway = gateway
    app.state.grants = AsyncMock()

    async def gateway_dependency():
        return app.state.gateway

    async def grants_dependency():
        return app.state.grants

    app.dependency_overrides[get_gateway_client] = gateway_dependency
    app.dependency_overrides[get_grant_client] = grants_dependency
    service = AsyncMock(return_value=PromptRewritePrompt(prompt="A more detailed prompt"))
    monkeypatch.setattr(prompt_rewrite, "rewrite_prompt", service)
    app.state.rewrite = service
    return app


def session_headers():
    return {"Authorization": f"Bearer {identity.mint_session(user_id='owner-1')}"}


async def test_rewrite_endpoint_authenticates_owner_without_creating_job(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            "/v1/agent/prompts/rewrite",
            json={"prompt": "Draft", "model": "allowed-model"},
            headers=session_headers(),
        )
    assert response.status_code == 200
    assert response.json() == {"kind": "prompt", "prompt": "A more detailed prompt"}
    app.state.gateway.agent_access.assert_awaited_once_with("owner-1")
    assert app.state.rewrite.await_args.kwargs["user_id"] == "owner-1"
    assert not hasattr(app.state, "storage")


@pytest.mark.parametrize("credential", [None, "ajt.worker-token", "agr.inference-token"])
async def test_rewrite_rejects_missing_or_nonowner_credentials(app, credential):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            "/v1/agent/prompts/rewrite",
            json={"prompt": "Draft", "model": "allowed-model"},
            headers={"Authorization": f"Bearer {credential}"} if credential else {},
        )
    assert response.status_code in (401, 403)
    app.state.rewrite.assert_not_awaited()


async def test_revoked_agent_access_rejects_rewriting(app):
    app.state.gateway.agent_access.return_value.update(allowed=False, permissions=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            "/v1/agent/prompts/rewrite",
            json={"prompt": "Draft", "model": "allowed-model"},
            headers=session_headers(),
        )
    assert response.status_code == 403
    app.state.rewrite.assert_not_awaited()


async def test_rewrite_returns_public_error_envelope(app):
    app.state.rewrite.side_effect = PromptRewriteError(
        "The model did not return a complete rewrite. Please try again.",
        status_code=502,
        error_type="invalid_rewrite",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            "/v1/agent/prompts/rewrite",
            json={"prompt": "Draft", "model": "allowed-model"},
            headers=session_headers(),
        )
    assert response.status_code == 502
    assert response.json()["detail"]["error"]["type"] == "invalid_rewrite"


async def test_rewrite_rejects_oversized_prompt_before_calling_model(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            "/v1/agent/prompts/rewrite",
            json={"prompt": "x" * 32001, "model": "allowed-model"},
            headers=session_headers(),
        )
    assert response.status_code == 422
    app.state.rewrite.assert_not_awaited()
