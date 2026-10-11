"""Generated avatars and memory are available only to their admitted owner."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.api import user_memory
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.app import create_app, create_mcp_app, install_admin_no_store
from cloud_agent.user_avatar import generate_user_avatar
from cloud_agent.user_memory import build_user_memory

PATH = "/v1/agent/user-memory"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "memory-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(user_memory.router)
    install_admin_no_store(application, prefixes=(PATH,))
    repository = AsyncMock()
    repository.refresh.side_effect = lambda **_: build_user_memory([], None)
    repository.set_enabled.side_effect = lambda **kw: {
        **build_user_memory([], None),
        "enabled": kw["enabled"],
    }
    repository.clear.side_effect = lambda **_: build_user_memory([], None)
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(user_memory=repository)
    application.state.repository = repository
    application.state.gateway = gateway
    return application


def session_headers(user_id="owner-1"):
    return {"Authorization": f"Bearer {identity.mint_session(user_id=user_id)}"}


async def test_reads_create_distinct_avatars_and_refresh_only_authenticated_owner(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        first = await client.get(PATH + "?user_id=owner-2", headers=session_headers())
        again = await client.get(PATH, headers=session_headers())
        other = await client.get(PATH, headers=session_headers("owner-2"))
    assert first.status_code == other.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["avatar"] == again.json()["avatar"]
    assert first.json()["avatar"] != other.json()["avatar"]
    assert first.json()["avatar"]["data_url"].startswith("data:image/svg+xml;base64,")
    assert [call.kwargs for call in app.state.repository.refresh.await_args_list] == [
        {"user_id": "owner-1"},
        {"user_id": "owner-1"},
        {"user_id": "owner-2"},
    ]


async def test_settings_and_clear_keep_the_owner_avatar(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        disabled = await client.patch(PATH, json={"enabled": False}, headers=session_headers())
        cleared = await client.delete(PATH, headers=session_headers())
    assert disabled.status_code == cleared.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["avatar"] == cleared.json()["avatar"]
    assert cleared.json()["memories"] == cleared.json()["suggestions"] == []
    app.state.repository.set_enabled.assert_awaited_once_with(user_id="owner-1", enabled=False)
    app.state.repository.clear.assert_awaited_once_with(user_id="owner-1")


def activity_profile(content, *, enabled=True):
    return build_user_memory(
        [{"job_id": "owner-job", "content": content, "title": "Recent work"}],
        {"enabled": enabled},
    )


async def test_avatar_evolves_from_refreshed_work_areas_for_the_same_owner(app):
    backend = activity_profile("Build a backend API endpoint.")
    frontend = activity_profile("Build a frontend UI with React.")
    app.state.repository.refresh.side_effect = [backend, backend, frontend, frontend]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        initial = await client.get(PATH, headers=session_headers())
        unchanged = await client.get(PATH, headers=session_headers())
        evolved = await client.get(PATH, headers=session_headers())
        other_owner = await client.get(PATH, headers=session_headers("owner-2"))
    assert (
        initial.status_code
        == unchanged.status_code
        == evolved.status_code
        == other_owner.status_code
        == 200
    )
    assert initial.json()["avatar"] == unchanged.json()["avatar"]
    assert initial.json()["avatar"] != evolved.json()["avatar"]
    assert evolved.json()["avatar"] != other_owner.json()["avatar"]
    assert initial.json()["portrait"]["focus_areas"] == ["Backend and APIs"]
    assert evolved.json()["portrait"]["focus_areas"] == ["Frontend and UI"]
    assert [call.kwargs for call in app.state.repository.refresh.await_args_list] == [
        {"user_id": "owner-1"},
        {"user_id": "owner-1"},
        {"user_id": "owner-1"},
        {"user_id": "owner-2"},
    ]


async def test_paused_avatar_uses_saved_activity_clear_resets_and_resume_updates(app):
    backend = activity_profile("Build a backend API endpoint.")
    paused_backend = {**backend, "enabled": False}
    frontend = activity_profile("Build a frontend UI with React.")
    app.state.repository.refresh.side_effect = [backend, paused_backend]
    app.state.repository.set_enabled.side_effect = [paused_backend, frontend]
    app.state.repository.clear.side_effect = None
    app.state.repository.clear.return_value = build_user_memory([], {"enabled": False})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        initial = await client.get(PATH, headers=session_headers())
        disabled = await client.patch(PATH, json={"enabled": False}, headers=session_headers())
        paused_read = await client.get(PATH, headers=session_headers())
        cleared = await client.delete(PATH, headers=session_headers())
        resumed = await client.patch(PATH, json={"enabled": True}, headers=session_headers())
    assert all(
        response.status_code == 200
        for response in (initial, disabled, paused_read, cleared, resumed)
    )
    assert initial.json()["avatar"] == disabled.json()["avatar"] == paused_read.json()["avatar"]
    assert cleared.json()["avatar"] == generate_user_avatar("owner-1").model_dump()
    assert cleared.json()["avatar"] != initial.json()["avatar"]
    assert cleared.json()["enabled"] is False
    assert cleared.json()["portrait"]["focus_areas"] == []
    assert resumed.json()["avatar"] != initial.json()["avatar"]
    assert resumed.json()["avatar"] != cleared.json()["avatar"]
    assert resumed.json()["enabled"] is True


@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
@pytest.mark.parametrize("credential", [None, "ajt.worker-token", "agr.inference-token"])
async def test_nonowner_credentials_cannot_read_or_change_memory(app, method, credential):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method,
            PATH,
            json={"enabled": False} if method == "PATCH" else None,
            headers={"Authorization": f"Bearer {credential}"} if credential else {},
        )
    assert response.status_code in (401, 403)
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.refresh.assert_not_awaited()
    app.state.repository.set_enabled.assert_not_awaited()
    app.state.repository.clear.assert_not_awaited()


async def test_revoked_admission_and_invalid_settings_do_not_touch_memory(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        invalid = await client.patch(
            PATH, json={"enabled": "yes", "user_id": "owner-2"}, headers=session_headers()
        )
        assert invalid.status_code == 422
        app.state.gateway.agent_access.side_effect = None
        app.state.gateway.agent_access.return_value = {
            "user_id": "owner-1",
            "allowed": False,
            "permissions": [],
        }
        denied = await client.get(PATH, headers=session_headers())
    assert denied.status_code == 403
    assert invalid.headers["Cache-Control"] == denied.headers["Cache-Control"] == "no-store"
    app.state.repository.refresh.assert_not_awaited()
    app.state.repository.set_enabled.assert_not_awaited()


def test_memory_routes_are_only_on_the_main_listener():
    assert PATH in create_app().openapi()["paths"]
    assert PATH not in create_mcp_app().openapi()["paths"]
