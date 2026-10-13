"""Persisted limits change advertised bounds and their consumers without a restart."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cloud_agent import settings, task_titles
from cloud_agent.api import uploads
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.attachments.models import AttachmentError, AttachmentLimits
from cloud_agent.attachments.selection import check_selection as check_attachments
from cloud_agent.goals.retry_policy import is_exhausted, wakeup_backoff
from cloud_agent.skills.imports import parse_skill_document
from cloud_agent.skills.models import SkillContent, SkillError, SkillLimits
from cloud_agent.skills.selection import check_selection as check_skills
from cloud_agent.storage.repositories.attachments import PostgresAttachmentRepository
from cloud_agent.storage.repositories.goals import PostgresGoalRepository
from cloud_agent.storage.repositories.skills import PostgresSkillRepository


@pytest.fixture
def saved_settings(monkeypatch):
    """Install real committed snapshots and restore the prior process state."""
    monkeypatch.setattr(settings, "_values", None)
    monkeypatch.setattr(settings, "_revision", -1)
    revision = 0

    def save(**changes):
        nonlocal revision
        revision += 1
        settings.apply_settings(revision, changes)

    return save


def _database():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetchrow = AsyncMock(return_value={"files": 0, "bytes": 0, "count": 0})
    conn.transaction.return_value = AsyncMock()
    acquire = AsyncMock()
    acquire.__aenter__.return_value = conn
    return SimpleNamespace(acquire=lambda: acquire), conn


def test_upload_advertisement_and_selections_follow_saved_limits(saved_settings):
    saved_settings(
        **{
            "attachments.max_files": 2,
            "attachments.max_total_bytes": 8,
            "attachments.max_file_bytes": 4,
        }
    )
    assert AttachmentLimits().model_dump() == {
        "max_files": 2,
        "max_total_bytes": 8,
        "max_file_bytes": 4,
        "max_path_length": 512,
    }
    check_attachments([{"size_bytes": 4}, {"size_bytes": 4}])
    with pytest.raises(AttachmentError, match="8 bytes"):
        check_attachments([{"size_bytes": 5}, {"size_bytes": 4}])
    saved_settings(**{"attachments.max_files": 1})
    assert AttachmentLimits().max_files == 1
    with pytest.raises(AttachmentError, match="at most 1"):
        check_attachments([{"size_bytes": 1}, {"size_bytes": 1}])


def test_upload_api_uses_saved_limit_before_writing(saved_settings):
    repository = SimpleNamespace(create_attachment=AsyncMock())
    app = FastAPI()
    app.include_router(uploads.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "owner"}
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(attachments=repository)
    saved_settings(**{"attachments.max_file_bytes": 4})
    with TestClient(app) as client:
        response = client.post(
            "/v1/agent/uploads",
            json={"path": "a.txt", "content_base64": base64.b64encode(b"hello").decode()},
        )
    assert response.status_code == 400
    assert "4 bytes" in response.json()["detail"]["error"]["message"]
    repository.create_attachment.assert_not_awaited()


@pytest.mark.parametrize(
    ("changes", "staged", "content", "message"),
    [
        ({"attachments.max_file_bytes": 4}, {"files": 0, "bytes": 0}, b"hello", "4 bytes"),
        ({"attachments.max_files": 1}, {"files": 1, "bytes": 0}, b"hi", "at most 1"),
        (
            {"attachments.max_file_bytes": 4, "attachments.max_total_bytes": 6},
            {"files": 1, "bytes": 4},
            b"bye",
            "6 bytes",
        ),
    ],
)
async def test_staging_repository_enforces_saved_quotas(
    saved_settings, changes, staged, content, message
):
    saved_settings(**changes)
    pool, conn = _database()
    conn.fetchrow.return_value = staged
    with pytest.raises(AttachmentError, match=message):
        await PostgresAttachmentRepository(pool).create_attachment(
            user_id="owner", path="a.txt", content=content, content_type="text/plain"
        )
    assert all("INSERT" not in call.args[0] for call in conn.fetchrow.await_args_list)


def test_skill_validation_import_and_selections_follow_saved_limits(saved_settings):
    saved_settings(
        **{"skills.max_skill_bytes": 8, "skills.max_selection_bytes": 10, "skills.max_selected": 1}
    )
    assert SkillLimits().max_skill_bytes == 8
    assert SkillLimits().max_selected == 1
    SkillContent(name="r", description="d", instructions="字字")
    with pytest.raises(ValidationError, match="8 bytes"):
        SkillContent(name="r", description="d", instructions="字字字")
    with pytest.raises(SkillError, match="8 bytes"):
        parse_skill_document("---\nname: r\ndescription: d\n---\nhello")
    with pytest.raises(SkillError, match="at most 1"):
        check_skills([{"instructions": "a"}, {"instructions": "b"}])
    with pytest.raises(SkillError, match="10 bytes"):
        check_skills([{"instructions": "x" * 11}])
    saved_settings(**{"skills.max_skill_bytes": 16})
    assert SkillLimits().max_skill_bytes == 16
    SkillContent(name="r", description="d", instructions="字字字")


async def test_skill_library_and_defaults_follow_saved_limits(saved_settings):
    saved_settings(**{"skills.max_library": 1, "skills.max_selected": 1})
    pool, conn = _database()
    conn.fetchval.return_value = 1
    repository = PostgresSkillRepository(pool)
    with pytest.raises(SkillError, match="at most 1 skills"):
        await repository.create_skill(user_id="owner", values={})
    conn.fetchrow.return_value = {"count": 1, "bytes": 1}
    with pytest.raises(SkillError, match="at most 1 default"):
        await repository._check_defaults(
            conn, "owner", {"default_enabled": True, "instructions": "hi"}
        )
    saved_settings(**{"skills.max_skill_bytes": 4, "skills.max_selection_bytes": 5})
    conn.fetchrow.return_value = {"count": 1, "bytes": 4}
    with pytest.raises(SkillError, match="5 bytes"):
        await repository._check_defaults(
            conn, "owner", {"default_enabled": True, "instructions": "hi"}
        )


async def test_goal_recovery_and_retries_follow_saved_settings(saved_settings):
    saved_settings(
        **{
            "goals.arm_after_seconds": 120.0,
            "goals.stalled_after_seconds": 1800.0,
            "goals.wakeup_backoff_seconds": 2.0,
            "goals.wakeup_backoff_max_seconds": 10.0,
            "goals.max_wakeup_attempts": 3,
        }
    )
    assert [wakeup_backoff(attempt) for attempt in (1, 2, 3, 4, 1000)] == [2, 4, 8, 10, 10]
    assert not is_exhausted(2)
    assert is_exhausted(3)
    pool, conn = _database()
    assert await PostgresGoalRepository(pool).sweep_goal_scheduling() == {
        "rearmed": [],
        "stalled": [],
    }
    delay_parameters = [call.args[1] for call in conn.fetch.await_args_list if len(call.args) > 1]
    assert delay_parameters == [120.0, 1800.0]
    saved_settings(**{"goals.max_wakeup_attempts": 5})
    assert not is_exhausted(3)


async def test_title_request_uses_saved_url_bounds_and_retry_count(saved_settings, monkeypatch):
    saved_settings(
        **{
            "gateway_base_url": "https://saved.example",
            "task_titles.max_chars": 12,
            "task_titles.input_max_chars": 8,
            "task_titles.max_tokens": 32,
            "task_titles.max_attempts": 2,
        }
    )
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://old.example")
    monkeypatch.setattr(task_titles, "_RETRY_DELAYS_S", (0.0,) * 5)
    assert task_titles.fallback_title("a" * 30) == "a" * 11 + "…"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503)

    store = SimpleNamespace(
        get_thread_title_prompt=AsyncMock(return_value="x" * 100),
        set_generated_thread_title=AsyncMock(),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await task_titles.generate_thread_title(
            store,
            job_id="job",
            attempt_id=1,
            lease_generation=1,
            model="model",
            sandbox_token="grant",
            client=client,
        )
    assert len(requests) == 2
    assert all(request.url.host == "saved.example" for request in requests)
    body = json.loads(requests[0].content)
    assert body["max_tokens"] == 32
    assert body["messages"][1]["content"] == "x" * 8
    assert "at most 12 Unicode characters" in body["messages"][0]["content"]
    store.set_generated_thread_title.assert_not_awaited()
