"""Owner scoping, revision errors, and review/confirm behavior over HTTP."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agent.api import skills
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_agent_app_credentials, get_storage
from cloud_agent.app import create_app, create_mcp_app
from cloud_agent.skills.imports import SkillImportNotFound
from cloud_agent.skills.models import SkillConflict

NOW = datetime.now(timezone.utc)
ROW = {
    "id": 1,
    "user_id": "owner",
    "name": "Review",
    "description": "Review changes",
    "instructions": "Read the diff.",
    "source": "created",
    "source_url": None,
    "source_revision": None,
    "default_enabled": False,
    "revision": 1,
    "created_at": NOW,
    "updated_at": NOW,
}


@pytest.fixture
def api():
    repository = SimpleNamespace(
        list_skills=AsyncMock(return_value=[ROW]),
        get_skill=AsyncMock(
            side_effect=lambda *, skill_id, user_id: (
                dict(ROW) if skill_id == 1 and user_id == "owner" else None
            )
        ),
        create_skill=AsyncMock(return_value=ROW),
        update_skill=AsyncMock(return_value={**ROW, "revision": 2}),
        delete_skill=AsyncMock(return_value=True),
    )
    storage = SimpleNamespace(skills=repository, source_control=SimpleNamespace())
    app = FastAPI()
    app.include_router(skills.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "owner"}
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_agent_app_credentials] = lambda: None
    with TestClient(app) as client:
        yield client, repository, storage


def test_skill_management_is_absent_from_sandbox_listener():
    assert "/v1/agent/skills" in create_app().openapi()["paths"]
    assert not any(
        path.startswith("/v1/agent/skills") for path in create_mcp_app().openapi()["paths"]
    )
    for route in skills.router.routes:
        assert any(
            dependency.call is require_agent_owner for dependency in route.dependant.dependencies
        )


def test_list_hides_bodies_and_read_write_routes_scope_by_owner(api):
    client, repository, _ = api
    response = client.get("/v1/agent/skills")
    assert response.status_code == 200
    assert "instructions" not in response.json()["skills"][0]
    assert "user_id" not in response.json()["skills"][0]
    assert client.get("/v1/agent/skills/1").json()["instructions"] == "Read the diff."
    assert client.get("/v1/agent/skills/2").status_code == 404
    assert (
        client.patch(
            "/v1/agent/skills/2", json={"revision": 1, "default_enabled": True}
        ).status_code
        == 404
    )
    repository.update_skill.assert_not_awaited()
    assert (
        client.patch(
            "/v1/agent/skills/1", json={"revision": 1, "default_enabled": True}
        ).status_code
        == 200
    )
    repository.update_skill.assert_awaited_once_with(
        user_id="owner", skill_id=1, revision=1, values={"default_enabled": True}
    )


def test_custom_create_validates_fields_and_cannot_forge_github_provenance(api):
    client, repository, _ = api
    body = {"name": " Review ", "description": " Review changes ", "instructions": "Read the diff."}
    assert client.post("/v1/agent/skills", json={**body, "source": "github"}).status_code == 422
    assert client.post("/v1/agent/skills", json={**body, "instructions": "  "}).status_code == 422
    assert client.post("/v1/agent/skills", json={**body, "user_id": "other"}).status_code == 422
    assert client.post("/v1/agent/skills", json=body).status_code == 201
    repository.create_skill.assert_awaited_once_with(
        user_id="owner",
        values={
            "name": "Review",
            "description": "Review changes",
            "instructions": "Read the diff.",
            "source": "created",
            "default_enabled": False,
        },
    )


def test_stale_edits_and_removals_are_conflicts(api):
    client, repository, _ = api
    repository.update_skill.side_effect = SkillConflict("Refresh this skill.")
    repository.delete_skill.side_effect = SkillConflict("Refresh this skill.")
    assert (
        client.patch(
            "/v1/agent/skills/1", json={"revision": 1, "default_enabled": True}
        ).status_code
        == 409
    )
    assert client.delete("/v1/agent/skills/1?revision=1").status_code == 409
    repository.delete_skill.assert_awaited_once_with(user_id="owner", skill_id=1, revision=1)
    assert client.delete("/v1/agent/skills/1").status_code == 422


def test_uploaded_preview_writes_nothing(api):
    client, repository, _ = api
    response = client.post(
        "/v1/agent/skills/import/preview",
        json={"content": "---\nname: Review\ndescription: Review changes\n---\nRead the diff."},
    )
    assert response.status_code == 200
    assert response.json()["instructions"] == "Read the diff."
    assert response.json()["source"] == "uploaded"
    repository.create_skill.assert_not_awaited()
    assert (
        client.post(
            "/v1/agent/skills/import/preview", json={"url": "x", "content": "x"}
        ).status_code
        == 400
    )


def test_github_confirm_refuses_content_changed_since_preview(api, monkeypatch):
    client, repository, _ = api
    preview = {
        "name": "Review",
        "description": "Review changes",
        "instructions": "Read the diff.",
        "source": "github",
        "source_url": "https://github.com/example/skills",
        "source_revision": "a" * 40,
        "content_digest": "a" * 64,
    }
    fetch = AsyncMock(
        side_effect=[dict(preview), {**preview, "content_digest": "b" * 64}, dict(preview)]
    )
    monkeypatch.setattr(skills, "fetch_github_skill", fetch)
    assert (
        client.post(
            "/v1/agent/skills/import/preview", json={"url": preview["source_url"]}
        ).status_code
        == 200
    )
    repository.create_skill.assert_not_awaited()
    body = {"url": preview["source_url"], "content_digest": "a" * 64, "name": "My review"}
    assert client.post("/v1/agent/skills/import", json=body).status_code == 409
    repository.create_skill.assert_not_awaited()
    assert client.post("/v1/agent/skills/import", json=body).status_code == 201
    saved = repository.create_skill.call_args.kwargs
    assert saved["user_id"] == "owner"
    assert saved["values"]["name"] == "My review"
    assert saved["values"]["source_revision"] == "a" * 40


@pytest.mark.asyncio
async def test_private_import_checks_entitlement_before_minting_credentials(monkeypatch):
    from cloud_agent.entitlement import RepoNotAllowed

    token_for = AsyncMock(return_value="scoped-token")
    credentials = SimpleNamespace(token_for=token_for)
    storage = SimpleNamespace(source_control=object())
    fetch = AsyncMock(side_effect=SkillImportNotFound("Not found"))
    entitled = AsyncMock(side_effect=RepoNotAllowed("Not allowed"))
    monkeypatch.setattr(skills, "fetch_github_skill", fetch)
    monkeypatch.setattr(skills, "require_entitled_repo", entitled)
    with pytest.raises(ValueError, match="cannot access"):
        await skills._github_preview(
            "https://github.com/example/private",
            user_id="other",
            store=storage,
            credentials=credentials,
        )
    token_for.assert_not_awaited()
    assert fetch.await_count == 1


def test_github_content_is_read_only(api):
    client, repository, _ = api
    repository.get_skill.side_effect = None
    repository.get_skill.return_value = {**ROW, "source": "github"}
    assert (
        client.patch(
            "/v1/agent/skills/1", json={"revision": 1, "instructions": "Different"}
        ).status_code
        == 400
    )
    repository.update_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_entitled_import_skips_anonymous_probe_even_when_it_would_be_rate_limited(
    monkeypatch,
):
    credentials = object()
    entitled = AsyncMock()

    async def fetch(url, **kwargs):
        assert kwargs == {"credentials": credentials}
        return {"instructions": "Approved content"}

    monkeypatch.setattr(skills, "require_entitled_repo", entitled)
    monkeypatch.setattr(skills, "fetch_github_skill", fetch)
    result = await skills._github_preview(
        "https://github.com/example/private?plain=1",
        user_id="owner",
        store=SimpleNamespace(source_control=object()),
        credentials=credentials,
    )
    assert result == {"instructions": "Approved content"}
    entitled.assert_awaited_once()
