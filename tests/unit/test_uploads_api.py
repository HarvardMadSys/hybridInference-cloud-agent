"""Staging uploads over HTTP: decoding, bounds, ownership and removal."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agent.api import uploads
from cloud_agent.api.deps import get_storage
from cloud_agent.api.routes import require_agent_owner
from cloud_agent.app import create_mcp_app
from cloud_agent.attachments.models import MAX_ATTACHMENT_FILE_BYTES, AttachmentError

ROW = {
    "id": 7,
    "user_id": "owner",
    "job_id": None,
    "thread_id": None,
    "path": "specs/api.md",
    "content_type": "text/markdown",
    "size_bytes": 12,
}


@pytest.fixture
def api():
    repository = SimpleNamespace(
        create_attachment=AsyncMock(return_value=ROW),
        delete_attachment=AsyncMock(return_value=True),
    )
    app = FastAPI()
    app.include_router(uploads.router)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": "owner"}
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace(attachments=repository)
    with TestClient(app) as client:
        yield client, repository


def _body(content: bytes = b"hello world!", path: str = "specs/api.md") -> dict:
    return {
        "path": path,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_type": "text/markdown",
    }


def test_uploading_is_absent_from_the_sandbox_listener() -> None:
    """A sandbox must not be able to write into its own uploads."""
    routes = {getattr(route, "path", "") for route in create_mcp_app().routes}
    assert not any(path.startswith("/v1/agent/uploads") for path in routes)


def test_a_staged_upload_answers_with_its_id_and_size(api) -> None:
    """The id is what the create request will name; the bytes never come back."""
    client, repository = api
    response = client.post("/v1/agent/uploads", json=_body())
    assert response.status_code == 201
    assert response.json() == {
        "id": 7,
        "path": "specs/api.md",
        "size_bytes": 12,
        "content_type": "text/markdown",
    }
    assert repository.create_attachment.await_args.kwargs["content"] == b"hello world!"
    assert repository.create_attachment.await_args.kwargs["user_id"] == "owner"


def test_the_directory_structure_of_the_upload_is_what_is_stored(api) -> None:
    """A folder upload keeps its shape under /uploads."""
    client, repository = api
    client.post("/v1/agent/uploads", json=_body(path="specs/diagrams/flow.svg"))
    assert repository.create_attachment.await_args.kwargs["path"] == "specs/diagrams/flow.svg"


@pytest.mark.parametrize("path", ["../escape.md", "/abs.md", ".git/config"])
def test_an_unsafe_path_is_refused_before_anything_is_stored(api, path: str) -> None:
    """The browser checks this too; this is the one that counts."""
    client, repository = api
    response = client.post("/v1/agent/uploads", json=_body(path=path))
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "invalid_upload"
    repository.create_attachment.assert_not_awaited()


def test_content_that_is_not_base64_is_a_refusal_rather_than_a_crash(api) -> None:
    """A malformed body is the caller's mistake, not a 500."""
    client, repository = api
    response = client.post(
        "/v1/agent/uploads",
        json={"path": "a.md", "content_base64": "not base64!!", "content_type": "text/plain"},
    )
    assert response.status_code == 400
    repository.create_attachment.assert_not_awaited()


def test_an_oversized_body_is_refused_by_the_schema_before_it_is_decoded(api) -> None:
    """Decoding first would mean holding the oversized payload in memory."""
    client, repository = api
    oversized = "A" * ((MAX_ATTACHMENT_FILE_BYTES // 3 + 1) * 4 + 64)
    response = client.post(
        "/v1/agent/uploads",
        json={"path": "big.bin", "content_base64": oversized, "content_type": "text/plain"},
    )
    assert response.status_code == 422
    repository.create_attachment.assert_not_awaited()


def test_a_quota_refusal_reaches_the_owner_as_a_correctable_error(api) -> None:
    """The owner can remove a file and try again; a 500 would not say so."""
    client, repository = api
    repository.create_attachment.side_effect = AttachmentError("Each file must be 5 MB or smaller.")
    response = client.post("/v1/agent/uploads", json=_body())
    assert response.status_code == 400
    assert "5 MB" in response.json()["detail"]["error"]["message"]


def test_a_staged_upload_can_be_removed_before_the_task_runs(api) -> None:
    """Removing a chip has to remove the bytes it stood for."""
    client, repository = api
    assert client.delete("/v1/agent/uploads/7").status_code == 204
    assert repository.delete_attachment.await_args.kwargs == {
        "user_id": "owner",
        "attachment_id": 7,
    }


def test_an_upload_a_task_already_took_is_no_longer_the_owners_to_delete(api) -> None:
    """Once a turn accepted it, the turn owns it; deleting the chat removes it."""
    client, repository = api
    repository.delete_attachment.return_value = False
    response = client.delete("/v1/agent/uploads/7")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["type"] == "upload_not_found"
