"""Profile APIs persist only their admitted owner's validated display data."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from tests.unit.test_user_profile import data_url, picture

from cloud_agent import identity
from cloud_agent.api import user_profile
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.app import create_app, create_mcp_app, install_admin_no_store
from cloud_agent.gateway import GatewayError
from cloud_agent.user_profile import MAX_PROFILE_REQUEST_BYTES, ProfileConflict, ProfileView

PATH = "/v1/agent/profile"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "profile-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(user_profile.router)
    install_admin_no_store(application, prefixes=(PATH,))
    repository = AsyncMock()
    repository.get_user_profile.return_value = ProfileView()
    repository.save_user_profile.return_value = ProfileView(
        first_name="沐昕", last_name="田", revision=1
    )
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    gateway.user_email.return_value = "owner@example.com"
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(user_profile=repository)
    application.state.repository = repository
    application.state.gateway = gateway
    return application


def headers(owner="owner-1"):
    return {"Authorization": f"Bearer {identity.mint_session(user_id=owner)}"}


async def test_owner_is_taken_from_session_and_email_is_read_only(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        read = await client.get(PATH + "?user_id=another", headers=headers())
        saved = await client.patch(
            PATH,
            headers=headers(),
            json={"first_name": "  沐昕  ", "last_name": "田", "expected_revision": 0},
        )
    assert read.status_code == saved.status_code == 200
    assert read.json()["email"] == "owner@example.com"
    assert read.headers["Cache-Control"] == saved.headers["Cache-Control"] == "no-store"
    app.state.repository.get_user_profile.assert_awaited_once_with(user_id="owner-1")
    app.state.repository.save_user_profile.assert_awaited_once_with(
        user_id="owner-1",
        first_name="沐昕",
        last_name="田",
        expected_revision=0,
        replace_image=False,
        image_data_url=None,
    )
    assert all(call.args == ("owner-1",) for call in app.state.gateway.user_email.await_args_list)


async def test_image_is_sanitized_before_storage_and_null_removes_it(app):
    raw = data_url(picture())
    body = {"first_name": "", "last_name": "", "expected_revision": 0, "image_data_url": raw}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        upload = await client.patch(PATH, headers=headers(), json=body)
        stored = app.state.repository.save_user_profile.await_args.kwargs
        removed = await client.patch(PATH, headers=headers(), json={**body, "image_data_url": None})
    assert upload.status_code == removed.status_code == 200
    assert stored["replace_image"] is True
    assert stored["image_data_url"] != raw
    assert stored["image_data_url"].startswith("data:image/png;base64,")
    assert app.state.repository.save_user_profile.await_args.kwargs["image_data_url"] is None
    assert app.state.repository.save_user_profile.await_args.kwargs["replace_image"] is True


@pytest.mark.parametrize(
    "extra",
    [
        {"email": "private@example.com"},
        {"user_id": "another"},
        {"first_name": "x" * 101},
        {"image_data_url": "data:image/png;base64,not-an-image"},
    ],
)
async def test_invalid_updates_do_not_echo_personal_data_or_write(app, extra):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        result = await client.patch(
            PATH,
            headers=headers(),
            json={"first_name": "private-name", "last_name": "", "expected_revision": 0, **extra},
        )
    assert result.status_code == 422
    assert "private" not in result.text
    app.state.repository.save_user_profile.assert_not_awaited()


async def test_chunked_upload_cannot_bypass_body_limit(app):
    async def chunks():
        for _ in range(5):
            yield b"x" * (MAX_PROFILE_REQUEST_BYTES // 4)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        result = await client.patch(PATH, headers=headers(), content=chunks())
    assert result.status_code == 413
    app.state.repository.save_user_profile.assert_not_awaited()


async def test_revision_conflicts_and_gateway_failures_do_not_report_success(app):
    body = {"first_name": "A", "last_name": "B", "expected_revision": 2}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        app.state.repository.save_user_profile.side_effect = ProfileConflict()
        conflict = await client.patch(PATH, headers=headers(), json=body)
        app.state.repository.save_user_profile.reset_mock()
        app.state.gateway.user_email.side_effect = GatewayError("unavailable")
        unavailable = await client.patch(PATH, headers=headers(), json=body)
    assert conflict.status_code == 409
    assert unavailable.status_code == 503
    app.state.repository.save_user_profile.assert_not_awaited()


async def test_unauthenticated_and_revoked_users_cannot_read_or_write(app):
    body = {"first_name": "", "last_name": "", "expected_revision": 0}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        assert (await client.get(PATH)).status_code == 401
        app.state.gateway.agent_access.side_effect = lambda owner: {
            "user_id": owner,
            "allowed": False,
            "permissions": [],
        }
        assert (await client.get(PATH, headers=headers())).status_code == 403
        assert (await client.patch(PATH, headers=headers(), json=body)).status_code == 403
    app.state.repository.get_user_profile.assert_not_awaited()
    app.state.repository.save_user_profile.assert_not_awaited()


def test_profile_routes_are_only_on_the_main_listener():
    assert PATH in create_app().openapi()["paths"]
    assert PATH not in create_mcp_app().openapi()["paths"]
