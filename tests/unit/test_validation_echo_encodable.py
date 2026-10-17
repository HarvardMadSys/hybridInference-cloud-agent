"""A 422 that echoes half a surrogate pair is still answered as a 422.

FastAPI's validation answer repeats the input it refused, and Starlette writes
JSON as UTF-8, which has no surrogates: a body cut inside an emoji used to
turn its own refusal into a 500. These go through ``create_app``; nothing
reaches a store, since validation refuses first.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cloud_agent.api._owner_auth import authenticate_agent_owner
from cloud_agent.api.deps import get_gateway_client
from cloud_agent.app import create_app


class _Gateway:
    async def agent_access(self, user_id: str) -> dict[str, object]:
        return {"user_id": user_id, "allowed": True, "permissions": ["agent.use"]}


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_gateway_client] = _Gateway
    app.dependency_overrides[authenticate_agent_owner] = lambda: {
        "user_id": "user-owner",
        "role": "free",
        "authenticated": True,
    }
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        (
            "/v1/agent/skills",
            {"name": "review \ud83d", "description": "Review.", "instructions": "Read it."},
            "name",
        ),
        ("/v1/agent/threads/athr_0123456789abcdef/messages", {"prompt": "and \ud83d"}, "prompt"),
    ],
)
def test_a_refused_field_holding_half_a_surrogate_pair_is_answered_422(
    client: TestClient, path: str, body: dict[str, str], field: str
):
    response = client.post(
        path,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", field]
    assert error["input"] == body[field].replace("\ud83d", "\ufffd")
