"""A program's keys survive an edit of its launch, through the real routes and PostgreSQL.

A program's keys are stored sealed inside its ``launch_spec`` column, which an
edit of the launch replaces whole. The route tests pin what the code hands the
store; this pins the row the database is left with, read back the way a claim
reads it.
"""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.integration.storage.test_agent_job_store import pool, store  # noqa: F401

from cloud_agent.api import mcp_connections as api
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp import sandbox_servers
from cloud_agent.mcp.registry import connection_from_row

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_OWNER = "user-mcp-keys"
_PATH = "/v1/agent/mcp/connections"
_KEY = "search-key-0001"
_LAUNCH = {
    "command": "npx",
    "args": ["-y", "@scope/search-mcp@1.0.0"],
    "env": {"API_KEY": "secret://API_KEY"},
}


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "mcp-key-edit-test-key-0123456789")


def _client(store) -> AsyncClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": _OWNER}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _opened_keys(store, connection_id: int) -> dict[str, str] | None:
    """The keys a claim would hand the sandbox, or ``None`` if it would refuse the program."""
    row = await store.mcp.get_mcp_connection(connection_id=connection_id, user_id=_OWNER)
    server = sandbox_servers.sandbox_mcp_server(connection_from_row(row), "search")
    return None if server is None else dict(server.secret_env)


async def _create(client: AsyncClient) -> dict[str, Any]:
    created = await client.post(
        _PATH,
        json={
            "label": "Search",
            "transport": "stdio",
            "location": "sandbox",
            "auth_kind": "none",
            "launch": _LAUNCH,
            "env_secrets": {"API_KEY": _KEY},
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_editing_the_launch_keeps_the_stored_key(store) -> None:
    async with _client(store) as client:
        connection = await _create(client)
        edited = await client.patch(
            f"{_PATH}/{connection['id']}",
            json={
                "expected_config_revision": connection["config_revision"],
                "launch": {**_LAUNCH, "args": ["-y", "@scope/search-mcp@1.1.0"]},
            },
        )

    assert edited.status_code == 200, edited.text
    assert _KEY not in edited.text
    assert edited.json()["argv"] == ["npx", "-y", "@scope/search-mcp@1.1.0"]
    assert await _opened_keys(store, connection["id"]) == {"API_KEY": _KEY}


async def test_an_edit_that_sets_the_key_replaces_it(store) -> None:
    async with _client(store) as client:
        connection = await _create(client)
        edited = await client.patch(
            f"{_PATH}/{connection['id']}",
            json={
                "expected_config_revision": connection["config_revision"],
                "env_secrets": {"API_KEY": "search-key-0002"},
            },
        )

    assert edited.status_code == 200, edited.text
    assert "search-key-0002" not in edited.text
    assert await _opened_keys(store, connection["id"]) == {"API_KEY": "search-key-0002"}


async def test_a_launch_without_the_reference_removes_the_key(store) -> None:
    async with _client(store) as client:
        connection = await _create(client)
        edited = await client.patch(
            f"{_PATH}/{connection['id']}",
            json={
                "expected_config_revision": connection["config_revision"],
                "launch": {**_LAUNCH, "env": {}},
            },
        )

    assert edited.status_code == 200, edited.text
    row = await store.mcp.get_mcp_connection(connection_id=connection["id"], user_id=_OWNER)
    assert "env_secrets" not in row["launch_spec"]
    assert await _opened_keys(store, connection["id"]) == {}
