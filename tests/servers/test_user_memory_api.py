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
from cloud_agent.memory_consolidation import MemoryConsolidationError
from cloud_agent.user_avatar import generate_user_avatar
from cloud_agent.user_memory import (
    MemoryRevisionConflict,
    ProjectMemoryView,
    UserMemoryConflict,
    UserMemoryNotFound,
    build_project_memory,
    build_user_memory,
)

PATH = "/v1/agent/user-memory"
PROJECT_PATH = "/v1/agent/project-memory"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "memory-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(user_memory.router)
    application.include_router(user_memory.project_router)
    install_admin_no_store(application, prefixes=(PATH, PROJECT_PATH))
    repository = AsyncMock()
    repository.refresh.side_effect = lambda **_: build_user_memory([], None)
    repository.set_enabled.side_effect = lambda **kw: {
        **build_user_memory([], None),
        "enabled": kw["enabled"],
    }
    repository.clear.side_effect = lambda **_: build_user_memory([], None)
    repository.refresh_project.side_effect = lambda **kw: ProjectMemoryView(
        repo=kw["repo"]
    ).model_dump()
    repository.set_project_enabled.side_effect = lambda **kw: ProjectMemoryView(
        repo=kw["repo"], enabled=kw["enabled"]
    ).model_dump()
    repository.clear_project.side_effect = lambda **kw: ProjectMemoryView(
        repo=kw["repo"]
    ).model_dump()
    repository.list_memory_projects.return_value = ["owner/name"]
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


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", PATH, None),
        ("PATCH", PATH, {"enabled": False}),
        ("DELETE", PATH, None),
        (
            "PATCH",
            PATH + "/entries/memory-1",
            {"content": "Prefer concise replies.", "expected_revision": 0},
        ),
        ("DELETE", PATH + "/entries/memory-1", {"expected_revision": 0}),
        ("POST", PATH + "/consolidate", {"revision": 0, "model": "chosen-model"}),
    ],
)
@pytest.mark.parametrize("credential", [None, "ajt.worker-token", "agr.inference-token"])
async def test_nonowner_credentials_cannot_read_or_change_memory(
    app, method, path, body, credential
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {credential}"} if credential else {},
        )
    assert response.status_code in (401, 403)
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.refresh.assert_not_awaited()
    app.state.repository.set_enabled.assert_not_awaited()
    app.state.repository.clear.assert_not_awaited()
    app.state.repository.update_entry.assert_not_awaited()
    app.state.repository.delete_entry.assert_not_awaited()
    app.state.repository.replace_memories.assert_not_awaited()


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
    main_paths = create_app().openapi()["paths"]
    mcp_paths = create_mcp_app().openapi()["paths"]
    for path in (
        PATH,
        PATH + "/entries/{memory_id}",
        PATH + "/consolidate",
        PROJECT_PATH,
        PROJECT_PATH + "/projects",
        PROJECT_PATH + "/entries/{memory_id}",
        PROJECT_PATH + "/consolidate",
    ):
        assert path in main_paths
        assert path not in mcp_paths


async def test_edit_normalizes_content_and_uses_authenticated_owner_revision(app):
    profile = activity_profile("I prefer concise replies.")
    original = profile["memories"][0]
    edited = {**original, "content": "Prefer detailed replies.", "user_edited": True}
    app.state.repository.update_entry.return_value = {
        **profile,
        "memories": [edited],
        "revision": 4,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH + "/entries/" + original["id"] + "?user_id=owner-2",
            json={"content": "  Prefer\n detailed replies.  ", "expected_revision": 3},
            headers=session_headers(),
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["revision"] == 4
    assert response.json()["memories"] == [edited]
    assert response.json()["avatar"] == generate_user_avatar("owner-1").model_dump()
    app.state.repository.update_entry.assert_awaited_once_with(
        user_id="owner-1",
        memory_id=original["id"],
        content="Prefer detailed replies.",
        expected_revision=3,
    )


async def test_delete_uses_authenticated_owner_and_returns_updated_snapshot(app):
    app.state.repository.delete_entry.return_value = {**build_user_memory([], None), "revision": 5}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            "DELETE",
            PATH + "/entries/memory-1?user_id=owner-2",
            json={"expected_revision": 4},
            headers=session_headers(),
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["memories"] == []
    assert response.json()["revision"] == 5
    app.state.repository.delete_entry.assert_awaited_once_with(
        user_id="owner-1", memory_id="memory-1", expected_revision=4
    )


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("error", "status"), [(UserMemoryConflict, 409), (UserMemoryNotFound, 404)]
)
async def test_entry_errors_are_private_and_have_stable_status(app, method, error, status):
    operation = (
        app.state.repository.update_entry
        if method == "PATCH"
        else app.state.repository.delete_entry
    )
    operation.side_effect = error("private storage details")
    body = {"expected_revision": 0}
    if method == "PATCH":
        body["content"] = "Prefer concise replies."
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method, PATH + "/entries/memory-1", json=body, headers=session_headers()
        )
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert "private storage details" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"content": "", "expected_revision": 0},
        {"content": " \n\t", "expected_revision": 0},
        {"content": "a" * 501, "expected_revision": 0},
        {"content": 5, "expected_revision": 0},
        {"content": "Prefer concise replies."},
        {"content": "Prefer concise replies.", "expected_revision": -1},
        {"content": "Prefer concise replies.", "expected_revision": True},
        {"content": "Prefer concise replies.", "expected_revision": "0"},
        {"content": "Prefer concise replies.", "expected_revision": 0, "user_id": "owner-2"},
        {"content": "Prefer concise replies.", "expected_revision": 0, "kind": "task"},
        {"content": "Prefer concise replies.", "expected_revision": 0, "source_repo": "other/repo"},
    ],
)
async def test_invalid_edits_do_not_reach_storage(app, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH + "/entries/memory-1", json=body, headers=session_headers()
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.update_entry.assert_not_awaited()


@pytest.mark.parametrize("mode", ["credential", "overlong", "missing_revision", "unexpected_field"])
async def test_credential_edits_are_rejected_without_reflecting_content(app, mode):
    content = "Remember that password=private-test-value"
    body = {"content": content, "expected_revision": 0}
    if mode == "overlong":
        body["content"] = content + "x" * 500
    elif mode == "missing_revision":
        del body["expected_revision"]
    elif mode == "unexpected_field":
        body["password"] = "private-test-value"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH + "/entries/memory-1",
            json=body,
            headers=session_headers(),
        )
    assert response.status_code == 422
    assert "private-test-value" not in response.text
    app.state.repository.update_entry.assert_not_awaited()


async def test_invalid_preference_scope_returns_validation_error(app):
    app.state.repository.update_entry.side_effect = ValueError("private storage details")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH + "/entries/memory-1",
            json={"content": "Use short replies for this task.", "expected_revision": 0},
            headers=session_headers(),
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert "private storage details" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expected_revision": -1},
        {"expected_revision": True},
        {"expected_revision": 0, "user_id": "owner-2"},
    ],
)
async def test_invalid_delete_does_not_reach_storage(app, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            "DELETE", PATH + "/entries/memory-1", json=body, headers=session_headers()
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.delete_entry.assert_not_awaited()


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_revoked_admission_cannot_edit_or_delete_entries(app, method):
    app.state.gateway.agent_access.side_effect = None
    app.state.gateway.agent_access.return_value = {
        "user_id": "owner-1",
        "allowed": False,
        "permissions": [],
    }
    body = {"expected_revision": 0}
    if method == "PATCH":
        body["content"] = "Prefer concise replies."
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method, PATH + "/entries/memory-1", json=body, headers=session_headers()
        )
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.update_entry.assert_not_awaited()
    app.state.repository.delete_entry.assert_not_awaited()


async def test_owner_can_edit_content_scope_and_remove_entries(app):
    current = build_project_memory(
        [
            {
                "job_id": "owner-job",
                "repo": "owner/repo",
                "content": "I prefer small changes.",
                "title": "Recent work",
            }
        ],
        {"revision": 7},
        "owner/repo",
    )
    app.state.repository.refresh.side_effect = None
    app.state.repository.refresh.return_value = current
    app.state.repository.replace_memories.side_effect = lambda **kw: {
        **current,
        "memories": kw["memories"],
        "revision": 8,
    }
    entry = current["memories"][0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH,
            headers=session_headers(),
            json={
                "revision": 7,
                "memories": [
                    {"id": entry["id"], "content": "Keep changes small.", "scope": "user"}
                ],
            },
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.json()["memories"]) == 1
    memory = response.json()["memories"][0]
    assert memory["content"] == "Keep changes small." and memory["scope"] == "user"
    assert memory["source_job_id"] == "owner-job" and memory["source_repo"] == "owner/repo"
    saved = app.state.repository.replace_memories.await_args.kwargs
    assert saved["user_id"] == "owner-1" and saved["revision"] == 7


@pytest.mark.parametrize(
    "invalid", ["stale", "unknown", "provenance", "length", "empty", "duplicate", "total", "global"]
)
async def test_invalid_edits_do_not_mutate_saved_memory(app, invalid):
    current = build_project_memory(
        [
            {
                "job_id": f"job-{index}",
                "repo": "owner/repo",
                "content": f"Always use setting {index}.",
            }
            for index in range(12)
        ],
        {"revision": 7},
        "owner/repo",
    )
    app.state.repository.refresh.side_effect = None
    app.state.repository.refresh.return_value = current
    edits = [{"id": current["memories"][0]["id"], "content": "Run tests.", "scope": "repo"}]
    body = {"revision": 6 if invalid == "stale" else 7, "memories": edits}
    if invalid == "unknown":
        edits[0]["id"] = "other-owner-memory"
    elif invalid == "provenance":
        edits[0]["source_repo"] = "other-owner/repo"
    elif invalid == "length":
        edits[0]["content"] = "x" * 241
    elif invalid == "empty":
        edits[0]["content"] = "   "
    elif invalid == "duplicate":
        edits.append(edits[0])
    elif invalid in ("total", "global"):
        body["memories"] = [
            {
                "id": memory["id"],
                "content": "x" * 240,
                "scope": "user" if invalid == "global" else "repo",
            }
            for memory in current["memories"]
        ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(PATH, headers=session_headers(), json=body)
    assert response.status_code == (409 if invalid == "stale" else 422)
    app.state.repository.replace_memories.assert_not_awaited()


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("model", [None, "chosen-model"])
async def test_consolidation_runs_for_owner_and_fences_slow_model_results(
    app, monkeypatch, conflict, model
):
    current = activity_profile("I prefer concise replies.")
    app.state.repository.refresh.side_effect = None
    app.state.repository.refresh.return_value = current
    consolidate = AsyncMock(return_value=current["memories"])
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    if conflict:
        app.state.repository.replace_memories.side_effect = MemoryRevisionConflict()
    else:
        app.state.repository.replace_memories.return_value = {**current, "revision": 1}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        body = {"revision": 0}
        if model is not None:
            body["model"] = model
        response = await client.post(PATH + "/consolidate", headers=session_headers(), json=body)
    assert response.status_code == (409 if conflict else 200)
    assert response.headers["Cache-Control"] == "no-store"
    assert consolidate.await_args.kwargs["user_id"] == "owner-1"
    assert consolidate.await_args.kwargs["requested_model"] == model
    app.state.repository.replace_memories.assert_awaited_once_with(
        user_id="owner-1",
        revision=0,
        memories=current["memories"],
    )


@pytest.mark.parametrize("cleared_during_inference", [False, True])
async def test_empty_consolidation_preserves_saved_memory_and_returns_latest_snapshot(
    app, monkeypatch, cleared_during_inference
):
    current = {**activity_profile("I prefer concise replies."), "revision": 7}
    latest = {**build_user_memory([], None), "revision": 8} if cleared_during_inference else current
    app.state.repository.refresh.side_effect = [current, latest]
    consolidate = AsyncMock(return_value=[])
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate",
            headers=session_headers(),
            json={"revision": 7, "model": "chosen-model"},
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == user_memory._view(latest, "owner-1").model_dump(mode="json")
    assert consolidate.await_args.kwargs["requested_model"] == "chosen-model"
    assert [call.kwargs for call in app.state.repository.refresh.await_args_list] == [
        {"user_id": "owner-1"},
        {"user_id": "owner-1"},
    ]
    app.state.repository.replace_memories.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 429, 502, 503])
async def test_consolidation_failure_never_replaces_saved_memory(app, monkeypatch, status):
    current = {**activity_profile("I prefer concise replies."), "revision": 7}
    app.state.repository.refresh.side_effect = None
    app.state.repository.refresh.return_value = current
    consolidate = AsyncMock(side_effect=MemoryConsolidationError("Please try again.", status))
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate", headers=session_headers(), json={"revision": 7}
        )
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Please try again."}
    app.state.repository.replace_memories.assert_not_awaited()


async def test_stale_consolidation_does_not_call_model_or_replace_memory(app, monkeypatch):
    app.state.repository.refresh.side_effect = None
    app.state.repository.refresh.return_value = {
        **activity_profile("I prefer concise replies."),
        "revision": 8,
    }
    consolidate = AsyncMock()
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate",
            headers=session_headers(),
            json={"revision": 7, "model": "chosen-model"},
        )
    assert response.status_code == 409
    consolidate.assert_not_awaited()
    app.state.repository.replace_memories.assert_not_awaited()


@pytest.mark.parametrize("model", ["", "x" * 201, 123, False, ["chosen-model"]])
async def test_invalid_consolidation_model_does_not_reach_inference_or_storage(
    app, monkeypatch, model
):
    consolidate = AsyncMock()
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate",
            headers=session_headers(),
            json={"revision": 0, "model": model},
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    consolidate.assert_not_awaited()
    app.state.repository.refresh.assert_not_awaited()
    app.state.repository.replace_memories.assert_not_awaited()
