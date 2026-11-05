"""Saving, testing and offering MCP connections that run in the task sandbox."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from tests.unit.test_mcp_connections_api import (
    _ENCRYPTION_KEY,
    _OWNER,
    FakeStore,
    _client,
    _seed,
)

from cloud_agent.mcp import package_pins, sandbox_servers

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


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the deployment a key, so a program's key can be sealed at all."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)


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


def test_a_program_keeps_its_keys_sealed() -> None:
    """A program may use an API key, as any command the agent runs may; it is stored sealed."""
    launch = {**_PLAYWRIGHT["launch"], "env": {"API_KEY": "secret://API_KEY"}}
    body = {**_PLAYWRIGHT, "launch": launch, "env_secrets": {"API_KEY": "key-value"}}
    store = FakeStore()
    with _client(store) as client:
        response = client.post("/v1/agent/mcp/connections", json=body)

    assert response.status_code == 201, response.text
    assert "key-value" not in response.text
    (row,) = store.connections.values()
    assert row["location"] == "sandbox"
    sealed = row["launch_spec"]["env_secrets"]["API_KEY"]
    assert sealed and sealed != "key-value"


def test_a_program_takes_keys_as_environment_values_not_a_sign_in() -> None:
    """A command has no sign-in to run; its keys are environment values."""
    store = FakeStore()
    with _client(store) as client:
        response = client.post(
            "/v1/agent/mcp/connections",
            json={**_PLAYWRIGHT, "auth_kind": "static", "secret": "token-value"},
        )
    assert response.status_code == 400
    assert "environment values" in _message(response)
    assert not store.connections


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


_SELECTED = {
    "thread_id": "thread-1",
    "enabled": True,
    "selection_pending": False,
    "config_revision": 1,
    "authorization_epoch": 1,
    "capability_revision": 1,
    "policy_revision": 1,
    "rules": {},
}


def test_a_chats_bindings_say_which_are_programs() -> None:
    """A program has no per-tool approval, so the composer must tell it from a service."""
    store = FakeStore()
    service = _seed(store)
    program = _seed(
        store,
        transport="stdio",
        location="sandbox",
        url=None,
        launch_spec=dict(_PLAYWRIGHT["launch"]),
        auth_kind="none",
        auth_config={},
        access_token_ciphertext=None,
    )
    store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
    store.bindings["thread-1"] = [
        {**_SELECTED, "connection_id": service, "alias": "docs"},
        {**_SELECTED, "connection_id": program, "alias": "browser"},
    ]
    with _client(store) as client:
        response = client.get("/v1/agent/mcp/threads/thread-1/bindings")

    assert response.status_code == 200, response.text
    locations = {item["alias"]: item["location"] for item in response.json()["bindings"]}
    assert locations == {"docs": "remote", "browser": "sandbox"}


_README_PASTE = json.dumps(
    {"mcpServers": {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}
)


def _current_release(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    answer = httpx.MockTransport(lambda _request: httpx.Response(200, json={"version": version}))
    monkeypatch.setattr(package_pins, "releases", package_pins.Releases(transport=answer))


def test_a_pasted_readme_command_is_shown_and_saved_at_its_current_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``@latest``, or no version, as READMEs write it, is pinned before the owner confirms."""
    _current_release(monkeypatch, "0.0.83")
    store = FakeStore()
    with _client(store) as client:
        preview = client.post("/v1/agent/mcp/connections/import", json={"text": _README_PASTE})
        saved = client.post(
            "/v1/agent/mcp/connections/import", json={"text": _README_PASTE, "save": True}
        )

    assert preview.status_code == 200, preview.text
    (draft,) = preview.json()["drafts"]
    assert draft["location"] == "sandbox"
    assert draft["argv"] == ["npx", "@playwright/mcp@0.0.83"]
    assert "pinned to @playwright/mcp 0.0.83, its current release" in draft["warnings"]
    assert saved.status_code == 200, saved.text
    (row,) = store.connections.values()
    assert row["location"] == "sandbox"
    assert row["launch_spec"]["args"] == ["@playwright/mcp@0.0.83"]


def test_a_paste_whose_release_cannot_be_looked_up_says_so_and_saves_nothing() -> None:
    """The registry is unreachable here (tests/unit/conftest.py)."""
    store = FakeStore()
    with _client(store) as client:
        preview = client.post("/v1/agent/mcp/connections/import", json={"text": _README_PASTE})
        saved = client.post(
            "/v1/agent/mcp/connections/import", json={"text": _README_PASTE, "save": True}
        )

    (draft,) = preview.json()["drafts"]
    assert draft["argv"] == ["npx", "@playwright/mcp@latest"]
    assert any("could not be looked up" in warning for warning in draft["warnings"])
    assert saved.status_code == 400
    assert "exact release" in _message(saved)
    assert not store.connections
