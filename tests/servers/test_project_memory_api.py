"""Project memory is isolated by both authenticated owner and repository."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from tests.servers.test_user_memory_api import (
    PATH as USER_PATH,
    PROJECT_PATH as PATH,
    app as app,
    session_headers,
)

from cloud_agent.api import user_memory
from cloud_agent.memory_consolidation import MemoryConsolidationError
from cloud_agent.user_memory import (
    MemoryRevisionConflict,
    ProjectMemoryView,
    UserMemoryConflict,
    UserMemoryNotFound,
    build_project_memory,
)

_PROJECT_OPERATIONS = [
    ("GET", PATH, None),
    ("PATCH", PATH, {"enabled": False}),
    ("DELETE", PATH, None),
    ("PATCH", PATH + "/entries/memory-1", {"content": "Run tests.", "expected_revision": 0}),
    ("DELETE", PATH + "/entries/memory-1", {"expected_revision": 0}),
    ("POST", PATH + "/consolidate", {"revision": 0}),
]


async def test_project_reads_use_only_authenticated_owner_and_exact_repo(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        first = await client.get(
            PATH,
            params={"repo": "owner/name", "user_id": "owner-2"},
            headers=session_headers(),
        )
        other_repo = await client.get(
            PATH, params={"repo": "owner/second"}, headers=session_headers()
        )
        other_owner = await client.get(
            PATH, params={"repo": "owner/name"}, headers=session_headers("owner-2")
        )
    assert first.status_code == other_repo.status_code == other_owner.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["repo"] == "owner/name"
    assert other_repo.json()["repo"] == "owner/second"
    assert "avatar" not in first.json()
    assert [call.kwargs for call in app.state.repository.refresh_project.await_args_list] == [
        {"user_id": "owner-1", "repo": "owner/name"},
        {"user_id": "owner-1", "repo": "owner/second"},
        {"user_id": "owner-2", "repo": "owner/name"},
    ]
    app.state.repository.refresh.assert_not_awaited()


async def test_lists_only_authenticated_owners_projects(app):
    app.state.repository.list_memory_projects.side_effect = [["owner/name"], ["other/private"]]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        first = await client.get(
            PATH + "/projects", params={"user_id": "owner-2"}, headers=session_headers()
        )
        other = await client.get(PATH + "/projects", headers=session_headers("owner-2"))
    assert first.status_code == other.status_code == 200
    assert first.json() == ["owner/name"]
    assert other.json() == ["other/private"]
    assert first.headers["Cache-Control"] == other.headers["Cache-Control"] == "no-store"
    assert [call.kwargs for call in app.state.repository.list_memory_projects.await_args_list] == [
        {"user_id": "owner-1"},
        {"user_id": "owner-2"},
    ]


async def test_project_pause_and_reset_do_not_change_user_or_other_project_memory(app):
    states = {
        "owner/name": ProjectMemoryView(repo="owner/name"),
        "owner/second": ProjectMemoryView(repo="owner/second"),
    }

    async def set_enabled(*, user_id, repo, enabled):
        states[repo].enabled = enabled
        return states[repo].model_dump()

    app.state.repository.set_project_enabled.side_effect = set_enabled
    app.state.repository.refresh_project.side_effect = lambda **kw: states[kw["repo"]].model_dump()
    app.state.repository.clear_project.side_effect = lambda **kw: states[kw["repo"]].model_dump()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        paused = await client.patch(
            PATH, params={"repo": "owner/name"}, json={"enabled": False}, headers=session_headers()
        )
        cleared = await client.delete(
            PATH, params={"repo": "owner/name"}, headers=session_headers()
        )
        other_project = await client.get(
            PATH, params={"repo": "owner/second"}, headers=session_headers()
        )
        global_memory = await client.get(USER_PATH, headers=session_headers())
    assert all(
        response.status_code == 200 for response in (paused, cleared, other_project, global_memory)
    )
    assert paused.json()["enabled"] is cleared.json()["enabled"] is False
    assert other_project.json()["enabled"] is global_memory.json()["enabled"] is True
    app.state.repository.set_project_enabled.assert_awaited_once_with(
        user_id="owner-1", repo="owner/name", enabled=False
    )
    app.state.repository.clear_project.assert_awaited_once_with(
        user_id="owner-1", repo="owner/name"
    )
    app.state.repository.set_enabled.assert_not_awaited()
    app.state.repository.clear.assert_not_awaited()


async def test_user_pause_and_reset_do_not_change_project_memory(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        paused = await client.patch(USER_PATH, json={"enabled": False}, headers=session_headers())
        cleared = await client.delete(USER_PATH, headers=session_headers())
        project = await client.get(PATH, params={"repo": "owner/name"}, headers=session_headers())
    assert paused.status_code == cleared.status_code == project.status_code == 200
    assert paused.json()["enabled"] is False
    assert project.json()["enabled"] is True
    app.state.repository.set_project_enabled.assert_not_awaited()
    app.state.repository.clear_project.assert_not_awaited()


@pytest.mark.parametrize(("method", "path", "body"), _PROJECT_OPERATIONS)
@pytest.mark.parametrize("credential", [None, "ajt.worker-token", "agr.inference-token"])
async def test_nonowner_credentials_cannot_read_or_change_project_memory(
    app, method, path, body, credential
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method,
            path,
            params={"repo": "owner/name"},
            json=body,
            headers={"Authorization": f"Bearer {credential}"} if credential else {},
        )
    assert response.status_code in (401, 403)
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.refresh_project.assert_not_awaited()
    app.state.repository.set_project_enabled.assert_not_awaited()
    app.state.repository.clear_project.assert_not_awaited()
    app.state.repository.update_project_entry.assert_not_awaited()
    app.state.repository.delete_project_entry.assert_not_awaited()
    app.state.repository.replace_project_memories.assert_not_awaited()


async def test_project_list_requires_owner_admission(app):
    app.state.gateway.agent_access.side_effect = None
    app.state.gateway.agent_access.return_value = {
        "user_id": "owner-1",
        "allowed": False,
        "permissions": [],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        anonymous = await client.get(PATH + "/projects")
        denied = await client.get(PATH + "/projects", headers=session_headers())
    assert anonymous.status_code == 401
    assert denied.status_code == 403
    assert anonymous.headers["Cache-Control"] == denied.headers["Cache-Control"] == "no-store"
    app.state.repository.list_memory_projects.assert_not_awaited()


@pytest.mark.parametrize(("method", "path", "body"), _PROJECT_OPERATIONS)
async def test_revoked_admission_cannot_access_project_memory(app, method, path, body):
    app.state.gateway.agent_access.side_effect = None
    app.state.gateway.agent_access.return_value = {
        "user_id": "owner-1",
        "allowed": False,
        "permissions": [],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method,
            path,
            params={"repo": "owner/name"},
            json=body,
            headers=session_headers(),
        )
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.refresh_project.assert_not_awaited()
    app.state.repository.set_project_enabled.assert_not_awaited()
    app.state.repository.clear_project.assert_not_awaited()
    app.state.repository.update_project_entry.assert_not_awaited()
    app.state.repository.delete_project_entry.assert_not_awaited()
    app.state.repository.replace_project_memories.assert_not_awaited()


@pytest.mark.parametrize(("method", "path", "body"), _PROJECT_OPERATIONS)
@pytest.mark.parametrize("repo", [None, "", " ", "a" * 141])
async def test_project_repo_is_required_and_bounded(app, method, path, body, repo):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method,
            path,
            params={"repo": repo} if repo is not None else {},
            json=body,
            headers=session_headers(),
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.refresh_project.assert_not_awaited()
    app.state.repository.set_project_enabled.assert_not_awaited()
    app.state.repository.clear_project.assert_not_awaited()
    app.state.repository.update_project_entry.assert_not_awaited()
    app.state.repository.delete_project_entry.assert_not_awaited()
    app.state.repository.replace_project_memories.assert_not_awaited()


@pytest.mark.parametrize(
    "body", [{"enabled": "yes"}, {"enabled": 0}, {"enabled": True, "user_id": "owner-2"}]
)
async def test_project_settings_are_strict_and_cannot_select_an_owner(app, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH, params={"repo": "owner/name"}, json=body, headers=session_headers()
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.set_project_enabled.assert_not_awaited()


def project_profile():
    return build_project_memory(
        [{"job_id": "owner-job", "repo": "owner/name", "content": "Always run tests."}],
        {"revision": 3},
        "owner/name",
    )


async def test_project_entry_edit_normalizes_content_and_fences_owner_repo_revision(app):
    current = project_profile()
    memory = current["memories"][0]
    edited = {**memory, "content": "Run pytest.", "user_edited": True}
    app.state.repository.update_project_entry.return_value = {
        **current,
        "memories": [edited],
        "revision": 4,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH + "/entries/" + memory["id"],
            params={"repo": "owner/name", "user_id": "owner-2"},
            json={"content": "  Run\n pytest.  ", "expected_revision": 3},
            headers=session_headers(),
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["memories"] == [edited]
    assert response.json()["revision"] == 4
    app.state.repository.update_project_entry.assert_awaited_once_with(
        user_id="owner-1",
        repo="owner/name",
        memory_id=memory["id"],
        content="Run pytest.",
        expected_revision=3,
    )
    app.state.repository.update_entry.assert_not_awaited()


async def test_project_entry_delete_forwards_owner_repo_and_revision(app):
    app.state.repository.delete_project_entry.return_value = {
        **project_profile(),
        "memories": [],
        "revision": 4,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            "DELETE",
            PATH + "/entries/memory-1",
            params={"repo": "owner/name", "user_id": "owner-2"},
            json={"expected_revision": 3},
            headers=session_headers(),
        )
    assert response.status_code == 200
    assert response.json()["memories"] == []
    assert response.json()["revision"] == 4
    app.state.repository.delete_project_entry.assert_awaited_once_with(
        user_id="owner-1",
        repo="owner/name",
        memory_id="memory-1",
        expected_revision=3,
    )
    app.state.repository.delete_entry.assert_not_awaited()


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("error", "status"), [(UserMemoryConflict, 409), (UserMemoryNotFound, 404)]
)
async def test_project_entry_errors_are_private_and_no_store(app, method, error, status):
    operation = (
        app.state.repository.update_project_entry
        if method == "PATCH"
        else app.state.repository.delete_project_entry
    )
    operation.side_effect = error("private storage details")
    body = {"expected_revision": 3}
    if method == "PATCH":
        body["content"] = "Run tests."
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.request(
            method,
            PATH + "/entries/memory-1",
            params={"repo": "owner/name"},
            json=body,
            headers=session_headers(),
        )
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert "private storage details" not in response.text


async def test_project_invalid_entry_validation_does_not_echo_credentials(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH + "/entries/memory-1",
            params={"repo": "owner/name"},
            json={"content": "api_key=private-test-value", "expected_revision": "3"},
            headers=session_headers(),
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert "private-test-value" not in response.text
    app.state.repository.update_project_entry.assert_not_awaited()


@pytest.mark.parametrize("scope", ["repo", "user"])
async def test_project_bulk_edits_validate_owned_sources_and_allow_explicit_scope_changes(
    app, scope
):
    current = project_profile()
    entry = current["memories"][0]
    app.state.repository.refresh_project.side_effect = None
    app.state.repository.refresh_project.return_value = current
    app.state.repository.replace_project_memories.side_effect = lambda **kw: {
        **current,
        "memories": [] if scope == "user" else kw["memories"],
        "revision": 4,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH,
            params={"repo": "owner/name"},
            headers=session_headers(),
            json={
                "revision": 3,
                "memories": [{"id": entry["id"], "content": "Run tests.", "scope": scope}],
            },
        )
    assert response.status_code == 200
    saved = app.state.repository.replace_project_memories.await_args.kwargs
    assert (
        saved["user_id"] == "owner-1" and saved["repo"] == "owner/name" and saved["revision"] == 3
    )
    assert saved["memories"][0]["scope"] == scope
    assert saved["memories"][0]["source_job_id"] == "owner-job"
    assert saved["memories"][0]["source_repo"] == "owner/name"
    app.state.repository.replace_memories.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["stale", "unknown", "provenance"])
async def test_project_bulk_edits_reject_stale_or_unowned_sources(app, invalid):
    current = project_profile()
    app.state.repository.refresh_project.side_effect = None
    app.state.repository.refresh_project.return_value = current
    entry = {"id": current["memories"][0]["id"], "content": "Run tests.", "scope": "repo"}
    if invalid == "unknown":
        entry["id"] = "other-owner-memory"
    elif invalid == "provenance":
        entry["source_repo"] = "another/private"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.patch(
            PATH,
            params={"repo": "owner/name"},
            headers=session_headers(),
            json={"revision": 2 if invalid == "stale" else 3, "memories": [entry]},
        )
    assert response.status_code == (409 if invalid == "stale" else 422)
    app.state.repository.replace_project_memories.assert_not_awaited()


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("model", [None, "chosen-model"])
async def test_project_consolidation_fences_owner_repo_and_slow_model_results(
    app, monkeypatch, conflict, model
):
    current = project_profile()
    app.state.repository.refresh_project.side_effect = None
    app.state.repository.refresh_project.return_value = current
    consolidate = AsyncMock(return_value=current["memories"])
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    if conflict:
        app.state.repository.replace_project_memories.side_effect = MemoryRevisionConflict()
    else:
        app.state.repository.replace_project_memories.return_value = {**current, "revision": 4}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        body = {"revision": 3}
        if model is not None:
            body["model"] = model
        response = await client.post(
            PATH + "/consolidate",
            params={"repo": "owner/name"},
            headers=session_headers(),
            json=body,
        )
    assert response.status_code == (409 if conflict else 200)
    assert response.headers["Cache-Control"] == "no-store"
    assert consolidate.await_args.kwargs["user_id"] == "owner-1"
    assert consolidate.await_args.kwargs["requested_model"] == model
    app.state.repository.replace_project_memories.assert_awaited_once_with(
        user_id="owner-1",
        repo="owner/name",
        revision=3,
        memories=current["memories"],
    )


@pytest.mark.parametrize("project", [False, True])
async def test_consolidation_scope_transfer_limits_return_private_validation_error(
    app, monkeypatch, project
):
    current = project_profile()
    refresh = app.state.repository.refresh_project if project else app.state.repository.refresh
    refresh.side_effect = None
    refresh.return_value = current
    replace = (
        app.state.repository.replace_project_memories
        if project
        else app.state.repository.replace_memories
    )
    replace.side_effect = ValueError("private destination memory details")
    monkeypatch.setattr(
        user_memory, "consolidate_memory", AsyncMock(return_value=current["memories"])
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            (PATH if project else USER_PATH) + "/consolidate",
            params={"repo": "owner/name"} if project else {},
            headers=session_headers(),
            json={"revision": 3},
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert "private destination memory details" not in response.text
    replace.assert_awaited_once()


@pytest.mark.parametrize("cleared_during_inference", [False, True])
async def test_empty_project_consolidation_preserves_memory_and_returns_latest_snapshot(
    app, monkeypatch, cleared_during_inference
):
    current = project_profile()
    latest = {**current, "memories": [], "revision": 4} if cleared_during_inference else current
    app.state.repository.refresh_project.side_effect = [current, latest]
    consolidate = AsyncMock(return_value=[])
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate",
            params={"repo": "owner/name"},
            headers=session_headers(),
            json={"revision": 3, "model": "chosen-model"},
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == ProjectMemoryView.model_validate(latest).model_dump(mode="json")
    assert consolidate.await_args.kwargs["requested_model"] == "chosen-model"
    assert [call.kwargs for call in app.state.repository.refresh_project.await_args_list] == [
        {"user_id": "owner-1", "repo": "owner/name"},
        {"user_id": "owner-1", "repo": "owner/name"},
    ]
    app.state.repository.replace_project_memories.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 429, 502, 503])
async def test_project_consolidation_failure_preserves_memory(app, monkeypatch, status):
    app.state.repository.refresh_project.side_effect = None
    app.state.repository.refresh_project.return_value = project_profile()
    consolidate = AsyncMock(side_effect=MemoryConsolidationError("Please try again.", status))
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate",
            params={"repo": "owner/name"},
            headers=session_headers(),
            json={"revision": 3, "model": "chosen-model"},
        )
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "Please try again."}
    assert consolidate.await_args.kwargs["requested_model"] == "chosen-model"
    app.state.repository.replace_project_memories.assert_not_awaited()


async def test_stale_project_consolidation_does_not_call_model_or_replace_memory(app, monkeypatch):
    app.state.repository.refresh_project.side_effect = None
    app.state.repository.refresh_project.return_value = project_profile()
    consolidate = AsyncMock()
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate",
            params={"repo": "owner/name"},
            headers=session_headers(),
            json={"revision": 2, "model": "chosen-model"},
        )
    assert response.status_code == 409
    consolidate.assert_not_awaited()
    app.state.repository.replace_project_memories.assert_not_awaited()


@pytest.mark.parametrize("model", ["", "x" * 201, 123, False, ["chosen-model"]])
async def test_invalid_project_consolidation_model_does_not_reach_model_or_storage(
    app, monkeypatch, model
):
    consolidate = AsyncMock()
    monkeypatch.setattr(user_memory, "consolidate_memory", consolidate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        response = await client.post(
            PATH + "/consolidate",
            params={"repo": "owner/name"},
            headers=session_headers(),
            json={"revision": 3, "model": model},
        )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    consolidate.assert_not_awaited()
    app.state.repository.refresh_project.assert_not_awaited()
    app.state.repository.replace_project_memories.assert_not_awaited()
