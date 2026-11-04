"""Saving, testing and offering MCP connections that run in the task sandbox."""

from __future__ import annotations

from typing import Any

from tests.unit.test_mcp_connections_api import _OWNER, FakeStore, _client, _seed

from cloud_agent.mcp import sandbox_servers

_PLAYWRIGHT = {
    "label": "Browser",
    "transport": "stdio",
    "location": "sandbox",
    "auth_kind": "none",
    "launch": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@0.0.83", "--cdp-endpoint", "${CLOUD_AGENT_BROWSER_CDP}"],
    },
}


def _message(response: Any) -> str:
    return response.json()["detail"]["error"]["message"]


def test_a_pinned_package_is_saved_as_a_sandbox_connection() -> None:
    store = FakeStore()
    with _client(store) as client:
        response = client.post("/v1/agent/mcp/connections", json=_PLAYWRIGHT)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["location"] == "sandbox"
    assert body["transport"] == "stdio"


def test_a_sandbox_connection_must_pin_an_exact_release() -> None:
    launch = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
    with _client(FakeStore()) as client:
        response = client.post("/v1/agent/mcp/connections", json={**_PLAYWRIGHT, "launch": launch})
    assert response.status_code == 400
    assert "exact release" in _message(response)


def test_a_sandbox_connection_runs_only_a_package_runner() -> None:
    launch = {"command": "node", "args": ["server.js"]}
    with _client(FakeStore()) as client:
        response = client.post("/v1/agent/mcp/connections", json={**_PLAYWRIGHT, "launch": launch})
    assert response.status_code == 400
    assert "npx or uvx" in _message(response)


def test_nothing_secret_is_accepted_for_the_sandbox() -> None:
    """The sandbox also runs the repository's code, which could read it."""
    attempts = [
        {**_PLAYWRIGHT, "auth_kind": "static", "secret": "token-value"},
        {**_PLAYWRIGHT, "env_secrets": {"API_KEY": "value"}},
        {**_PLAYWRIGHT, "launch": {**_PLAYWRIGHT["launch"], "env": {"K": "secret://K"}}},
    ]
    store = FakeStore()
    with _client(store) as client:
        for body in attempts:
            response = client.post("/v1/agent/mcp/connections", json=body)
            assert response.status_code == 400, body
            assert "secret" in _message(response) or "credential" in _message(response)
    assert not any(row.get("location") == "sandbox" for row in store.connections.values())


def test_testing_a_sandbox_connection_explains_instead_of_failing_it() -> None:
    """The control plane cannot reach it; an unreachable verdict would stop it."""
    store = FakeStore()
    connection_id = _seed(
        store,
        transport="stdio",
        location="sandbox",
        url=None,
        launch_spec=dict(_PLAYWRIGHT["launch"]),
        auth_kind="none",
        auth_config={},
        access_token_ciphertext=None,
        status="draft",
    )
    with _client(store) as client:
        response = client.post(f"/v1/agent/mcp/connections/{connection_id}/test")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["message"] == sandbox_servers.TEST_MESSAGE
    assert store.connections[connection_id]["status"] == "draft"


def test_the_browser_presets_are_offered_and_accepted_as_templates() -> None:
    with _client(FakeStore()) as client:
        listed = client.get("/v1/agent/mcp/connections").json()["templates"]
        sandbox = [template for template in listed if template.get("location") == "sandbox"]
        assert [template["name"] for template in sandbox] == [
            template["name"] for template in sandbox_servers.TEMPLATES
        ]
        for template in sandbox:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": template["label"],
                    "transport": template["transport"],
                    "location": template["location"],
                    "auth_kind": template["auth_kind"],
                    "launch": template["launch"],
                    "template": template["name"],
                },
            )
            assert response.status_code == 201, response.text


def test_a_sandbox_connection_keeps_its_owner() -> None:
    store = FakeStore()
    with _client(store) as client:
        created = client.post("/v1/agent/mcp/connections", json=_PLAYWRIGHT).json()
    with _client(store, user_id="someone-else") as client:
        assert client.get(f"/v1/agent/mcp/connections/{created['id']}").status_code == 404
    assert store.connections[int(created["id"])]["user_id"] == _OWNER
