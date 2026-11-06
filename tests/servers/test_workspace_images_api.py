"""A chat's workspace images: one image as its own bytes, and the artifacts' screenshots."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.servers.test_agent_jobs_api import (  # noqa: F401 - shared pytest fixtures
    _OTHER,
    FakeStorage,
    _api_key_secret,
    _attach_shared_workspace,
    _build_app,
    _create_job,
)

from cloud_agent.sandbox_manager import sandbox_manager_routing
from cloud_agent.sandbox_manager.sandbox_manager_client import SandboxManagerError

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def store() -> FakeStorage:
    """A fresh in-memory store, as the jobs API tests use."""
    return FakeStorage()


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _SandboxManager:
    def __init__(self, image: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.image = image or {
            "path": "shot.png",
            "kind": "file",
            "content": None,
            "content_base64": base64.b64encode(_PNG).decode("ascii"),
            "media_type": "image/png",
            "size": len(_PNG),
            "binary": True,
            "truncated": False,
            "source": "workspace",
        }

    def with_start_seq(self, _start_seq: int | None):
        return self

    async def register_shared_sandbox(self, _sandbox: dict[str, Any]):
        return None

    async def file_image(self, workspace_id: str, path: str) -> dict[str, Any]:
        self.calls.append(("image", workspace_id, path))
        return self.image

    async def artifact_images(self, workspace_id: str, since: float) -> dict[str, Any]:
        self.calls.append(("list", workspace_id, since))
        return {
            "images": [
                {
                    "path": ".cloud-agent-artifacts/playwright-mcp/page.png",
                    "size": 2048,
                    "modified_at": 1_800_000_100.5,
                }
            ],
            "truncated": False,
        }


async def _owned_and_foreign(client: AsyncClient, store: FakeStorage) -> tuple[str, str, str]:
    job_id = await _create_job(client)
    workspace_id = _attach_shared_workspace(store, job_id)
    foreign = await store.conversation_commands.create_job(
        user_id=_OTHER,
        repo="owner/other",
        task_prompt="not yours",
        runtime="claude-code",
        model="glm-5.1",
    )
    _attach_shared_workspace(store, foreign["id"])
    return job_id, workspace_id, foreign["id"]


async def test_an_image_comes_back_as_its_own_bytes_for_its_owner_only(
    store: FakeStorage, monkeypatch
):
    sandbox_manager = _SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        job_id, workspace_id, foreign = await _owned_and_foreign(c, store)
        fetched = await c.get(f"/v1/agent/jobs/{job_id}/files/image", params={"path": "shot.png"})
        refused = await c.get(f"/v1/agent/jobs/{foreign}/files/image", params={"path": "shot.png"})
        traversal = await c.get(
            f"/v1/agent/jobs/{job_id}/files/image", params={"path": "../shot.png"}
        )

    assert fetched.status_code == 200
    assert fetched.content == _PNG
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in fetched.headers["content-security-policy"]
    assert refused.status_code == 404
    assert traversal.status_code == 400
    assert sandbox_manager.calls == [("image", workspace_id, "shot.png")]


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        ({"kind": "file", "omitted_reason": "too_large", "size": 99_000_000}, 413),
        ({"kind": "file", "omitted_reason": "not_an_image", "content": None}, 415),
        ({"kind": "file", "media_type": "image/png", "content_base64": "%%%"}, 502),
    ],
    ids=["too-large", "not-an-image", "unreadable"],
)
async def test_a_file_the_image_read_cannot_show_says_why(
    store: FakeStorage, monkeypatch, answer: dict[str, Any], status: int
):
    sandbox_manager = _SandboxManager(image={"path": "x", **answer})
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        job_id, _workspace_id, _foreign = await _owned_and_foreign(c, store)
        response = await c.get(f"/v1/agent/jobs/{job_id}/files/image", params={"path": "x"})

    assert response.status_code == status


async def test_the_artifacts_screenshots_are_listed_since_a_time(store: FakeStorage, monkeypatch):
    sandbox_manager = _SandboxManager()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        job_id, workspace_id, foreign = await _owned_and_foreign(c, store)
        listed = await c.get(
            f"/v1/agent/jobs/{job_id}/artifacts/images", params={"since": "1800000000"}
        )
        refused = await c.get(f"/v1/agent/jobs/{foreign}/artifacts/images")
        negative = await c.get(f"/v1/agent/jobs/{job_id}/artifacts/images", params={"since": -1})

    assert listed.status_code == 200
    assert listed.json() == {
        "images": [
            {
                "path": ".cloud-agent-artifacts/playwright-mcp/page.png",
                "size": 2048,
                "modified_at": 1_800_000_100.5,
            }
        ],
        "truncated": False,
    }
    assert refused.status_code == 404
    assert negative.status_code == 422
    assert sandbox_manager.calls == [("list", workspace_id, 1_800_000_000.0)]


async def test_a_workspace_that_was_never_made_is_a_conflict(store: FakeStorage, monkeypatch):
    class Missing(_SandboxManager):
        async def artifact_images(self, workspace_id: str, since: float) -> dict[str, Any]:
            raise SandboxManagerError(404, "shared chat workspace is not materialized")

    sandbox_manager = Missing()
    monkeypatch.setattr(sandbox_manager_routing, "_client_for", lambda _host: sandbox_manager)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app(store)), base_url="http://t"
    ) as c:
        job_id, _workspace_id, _foreign = await _owned_and_foreign(c, store)
        response = await c.get(f"/v1/agent/jobs/{job_id}/artifacts/images")

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "workspace_unavailable"
