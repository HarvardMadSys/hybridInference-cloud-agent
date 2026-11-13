"""A sandbox reports the tools its programs listed, through the relay's own endpoint.

The control plane cannot reach a program that runs in a chat's sandbox, so the
sandbox's gateway reports what it listed (``cloud_agent.mcp.sandbox_tools``).
What carries the weight:

- **It is the relay, not a route beside it.** The relay listener serves the
  bare relay and nothing beneath it, so the report is one more method there.
- **It writes a tool list and nothing else**, for that chat's own enabled
  bindings of the owner's sandbox connections: never a service's, never
  another owner's.
- **An unchanged list is not stored.** Storing moves the capability revision,
  which marks approvals of the earlier one stale.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.servers.test_mcp_relay_host import (
    _ENCRYPTION_KEY,
    _HOST_URL,
    _OWNER,
    FakeStore,
    _binding,
    _connection,
    _residency_auth,
    _rpc,
)
from tests.support.storage import OneFake

from cloud_agent.api import mcp_relay
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp import sandbox_tools

pytestmark = pytest.mark.asyncio

_TOOLS = [{"name": "navigate", "description": "Open a page", "inputSchema": {"type": "object"}}]


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "mcp-host-secret-value-32-bytes-xx")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)
    monkeypatch.setattr(mcp_relay.web_search, "configured", lambda: False)


def _program(connection_id: int, label: str, **overrides: Any) -> dict[str, Any]:
    launch = {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.83"], "env": {}}
    return _connection(
        connection_id,
        label,
        transport="stdio",
        location="sandbox",
        url=None,
        launch_spec=launch,
        auth_kind="none",
        auth_config={},
        access_token_ciphertext=None,
        **overrides,
    )


class ReportingStore(FakeStore):
    """The relay's fake, plus what storing a reported list writes."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.saved: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.bindings if row["thread_id"] == thread_id]

    async def save_mcp_capabilities(self, **kwargs: Any) -> int:
        self.saved.append(kwargs)
        connection = self.connections[kwargs["connection_id"]]
        connection["capability_revision"] += 1
        self.snapshots[kwargs["connection_id"]] = {
            "connection_id": kwargs["connection_id"],
            "capability_revision": connection["capability_revision"],
            "config_revision": kwargs["config_revision"],
            "protocol_version": kwargs["protocol_version"],
            "tools": kwargs["tools"],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }
        return connection["capability_revision"]

    async def set_mcp_connection_status(self, **kwargs: Any) -> bool:
        self.statuses.append(kwargs)
        return True


@pytest.fixture()
def store() -> ReportingStore:
    return ReportingStore(
        connections={
            7: _connection(7, "GitHub"),
            9: _program(9, "Browser"),
            10: _program(10, "Theirs", user_id="someone-else"),
            11: _program(11, "Off"),
        },
        bindings=[
            _binding(7, "github"),
            _binding(9, "browser", transport="stdio", location="sandbox"),
            _binding(10, "theirs", transport="stdio", location="sandbox"),
            _binding(11, "off", transport="stdio", location="sandbox", enabled=False),
        ],
        snapshots={},
    )


@pytest.fixture()
async def client(store: ReportingStore) -> AsyncClient:
    app = FastAPI()
    app.include_router(mcp_relay.router)
    app.dependency_overrides[get_storage] = lambda: OneFake(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api


def _report(**servers: Any) -> dict[str, Any]:
    return _rpc(sandbox_tools.METHOD, servers=servers)


async def test_a_programs_tools_are_stored_for_the_chats_own_sandbox_binding(
    client: AsyncClient, store: ReportingStore
) -> None:
    every = dict.fromkeys(("browser", "github", "theirs", "off", "unbound"), _TOOLS)
    response = await client.post(_HOST_URL, json=_report(**every), headers=_residency_auth())

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"stored": ["browser"]}
    [saved] = store.saved
    assert saved["connection_id"] == 9
    assert saved["tools"] == [
        {
            "name": "navigate",
            "description": "Open a page",
            "input_schema": {"type": "object"},
            "annotations": {},
        }
    ]
    assert store.connections[9]["user_id"] == _OWNER
    assert [status["status"] for status in store.statuses] == ["active"]


async def test_an_unchanged_list_is_not_stored_again(
    client: AsyncClient, store: ReportingStore
) -> None:
    for _ in range(2):
        await client.post(_HOST_URL, json=_report(browser=_TOOLS), headers=_residency_auth())
    assert len(store.saved) == 1
    changed = [*_TOOLS, {"name": "click"}]
    response = await client.post(
        _HOST_URL, json=_report(browser=changed), headers=_residency_auth()
    )
    assert response.json()["result"] == {"stored": ["browser"]}
    assert len(store.saved) == 2


async def test_a_report_needs_the_members_credential_and_a_valid_body(
    client: AsyncClient, store: ReportingStore
) -> None:
    refused = await client.post(_HOST_URL, json=_report(browser=_TOOLS))
    assert refused.status_code == 401
    malformed = await client.post(
        _HOST_URL, json=_rpc(sandbox_tools.METHOD, tools=_TOOLS), headers=_residency_auth()
    )
    assert malformed.json()["error"]["code"] == -32602
    assert not store.saved
