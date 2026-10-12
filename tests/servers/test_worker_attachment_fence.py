"""Worker uploads require the current runner's live capability fence."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api.deps import get_storage
from cloud_agent.api.worker import router
from cloud_agent.attachments.models import AttachmentLeaseLost
from cloud_agent.tokens import SCOPE_MCP, SCOPE_MODEL, mint_worker_token

pytestmark = pytest.mark.asyncio


@pytest.fixture
def attachments():
    """Expose only the atomic worker reads, never unfenced thread reads."""
    return SimpleNamespace(
        list_worker_attachments=AsyncMock(return_value=[]),
        read_worker_attachment=AsyncMock(return_value=None),
    )


@pytest.fixture
async def client(monkeypatch, attachments):
    """Use the real worker auth and routing with a narrow storage boundary."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "worker-attachment-test-secret")
    app = FastAPI()
    app.include_router(router)

    async def storage():
        return SimpleNamespace(attachments=attachments)

    app.dependency_overrides[get_storage] = storage
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def authorization(**overrides):
    """Mint a synthetic token whose entire fence must reach storage."""
    token = mint_worker_token(
        **{"job_id": "job-current", "attempt_id": 23, "lease_generation": 7, **overrides}
    )
    return {"Authorization": f"Bearer {token}"}


async def test_current_worker_restores_manifest_and_bytes(client, attachments):
    """A valid current runner still restores every upload in its conversation."""
    attachments.list_worker_attachments.return_value = [
        {"id": 11, "path": "earlier.txt", "size_bytes": 7, "content_type": "text/plain"}
    ]
    attachments.read_worker_attachment.return_value = ("earlier.txt", b"payload", "text/plain")
    listed = await client.get(
        "/v1/agent/worker/jobs/job-current/attachments", headers=authorization()
    )
    assert listed.status_code == 200
    assert listed.json()["attachments"][0]["path"] == "earlier.txt"
    content = await client.get(
        "/v1/agent/worker/jobs/job-current/attachments/11", headers=authorization()
    )
    assert content.status_code == 200
    assert content.json() == {
        "id": 11,
        "path": "earlier.txt",
        "content_type": "text/plain",
        "content_base64": base64.b64encode(b"payload").decode("ascii"),
    }
    fence = {"job_id": "job-current", "attempt_id": 23, "lease_generation": 7}
    attachments.list_worker_attachments.assert_awaited_once_with(**fence)
    attachments.read_worker_attachment.assert_awaited_once_with(**fence, attachment_id=11)


@pytest.mark.parametrize("suffix", ["", "/11"])
async def test_a_lost_storage_fence_stops_both_reads(client, attachments, suffix):
    """The runner's GET path recognizes the same lease_lost response as writes."""
    attachments.list_worker_attachments.side_effect = AttachmentLeaseLost
    attachments.read_worker_attachment.side_effect = AttachmentLeaseLost
    response = await client.get(
        f"/v1/agent/worker/jobs/job-current/attachments{suffix}", headers=authorization()
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["type"] == "lease_lost"


async def test_empty_manifest_and_missing_upload_keep_distinct_results(client):
    """No uploads is a valid listing; a missing id is still a 404."""
    listed = await client.get(
        "/v1/agent/worker/jobs/job-current/attachments", headers=authorization()
    )
    assert listed.status_code == 200
    assert listed.json() == {"attachments": []}
    missing = await client.get(
        "/v1/agent/worker/jobs/job-current/attachments/11", headers=authorization()
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"]["type"] == "upload_not_found"


@pytest.mark.parametrize("suffix", ["", "/11"])
@pytest.mark.parametrize(
    ("claims", "status"),
    [({"job_id": "job-other"}, 409), ({"scope": SCOPE_MCP}, 403), ({"scope": SCOPE_MODEL}, 403)],
)
async def test_another_job_or_sandbox_scope_never_reaches_uploads(
    client, attachments, suffix, claims, status
):
    """Signed capabilities retain their job and runner-only scope restrictions."""
    response = await client.get(
        f"/v1/agent/worker/jobs/job-current/attachments{suffix}", headers=authorization(**claims)
    )
    assert response.status_code == status
    attachments.list_worker_attachments.assert_not_awaited()
    attachments.read_worker_attachment.assert_not_awaited()


@pytest.mark.parametrize("suffix", ["", "/11"])
@pytest.mark.parametrize("token", [None, "invalid"])
async def test_unsigned_requests_never_reach_uploads(client, attachments, suffix, token):
    """Neither metadata nor bytes are exposed before authenticating the caller."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    response = await client.get(
        f"/v1/agent/worker/jobs/job-current/attachments{suffix}", headers=headers
    )
    assert response.status_code == 401
    attachments.list_worker_attachments.assert_not_awaited()
    attachments.read_worker_attachment.assert_not_awaited()
