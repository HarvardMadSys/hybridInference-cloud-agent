"""A program's sealed key is refused when it is too short to be masked.

The runner masks a sandbox program's keys only from ``MIN_SECRET_CHARS`` up, as
it masks an environment Secret, so a new shorter key is refused where it is
saved: a hand-added program, a pasted configuration's preview and its save. A
short key stored before the rule still opens at the claim.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from tests.unit.test_mcp_connections_api import _ENCRYPTION_KEY, FakeStore, _client, _seed

from cloud_agent.mcp import sandbox_servers
from cloud_agent.mcp.models import (
    ConnectionConfig,
    ExecutionLocation,
    LaunchSpec,
    McpConnection,
    Transport,
)
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent_shared.secret_redaction import MIN_SECRET_CHARS

_REFUSAL = (
    "Secret API_KEY must contain at least 8 characters; "
    "a shorter value cannot be masked in the chat's output"
)

_LAUNCH = {
    "command": "npx",
    "args": ["-y", "@scope/search-mcp@1.0.0"],
    "env": {"API_KEY": "secret://API_KEY"},
}

_PROGRAM = {
    "label": "Search",
    "transport": "stdio",
    "location": "sandbox",
    "auth_kind": "none",
    "launch": _LAUNCH,
}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the deployment a key, so a program's key can be sealed at all."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)


def _message(response: Any) -> str:
    return response.json()["detail"]["error"]["message"]


def _paste(env: dict[str, str]) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "search": {
                    "command": "npx",
                    "args": ["-y", "@scope/search-mcp@1.0.0"],
                    "env": env,
                }
            }
        }
    )


def test_the_rule_is_the_environment_secrets_minimum() -> None:
    assert MIN_SECRET_CHARS == 8


@pytest.mark.parametrize("value", ["short", "1234567", "  abc   "])
def test_a_new_program_with_a_short_key_is_refused(value: str) -> None:
    store = FakeStore()
    with _client(store) as client:
        response = client.post(
            "/v1/agent/mcp/connections", json={**_PROGRAM, "env_secrets": {"API_KEY": value}}
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == {
        "type": "invalid_request_error",
        "message": _REFUSAL,
    }
    assert not store.connections


def test_a_new_program_with_an_eight_character_key_is_saved_sealed() -> None:
    store = FakeStore()
    with _client(store) as client:
        response = client.post(
            "/v1/agent/mcp/connections",
            json={**_PROGRAM, "env_secrets": {"API_KEY": "12345678"}},
        )

    assert response.status_code == 201, response.text
    (row,) = store.connections.values()
    sealed = row["launch_spec"]["env_secrets"]["API_KEY"]
    assert mcp_cipher(_ENCRYPTION_KEY).decrypt(sealed) == "12345678"


def test_a_supplied_value_the_launch_does_not_name_is_not_judged() -> None:
    """Only a value that is sealed is a program's key."""
    store = FakeStore()
    with _client(store) as client:
        response = client.post(
            "/v1/agent/mcp/connections",
            json={**_PROGRAM, "env_secrets": {"API_KEY": "12345678", "UNUSED": "x"}},
        )
    assert response.status_code == 201, response.text


def test_an_edit_cannot_seal_a_key_short_or_not() -> None:
    """An edit carries no key: one sent beside the launch is refused, one inside it dropped."""
    store = FakeStore()
    connection_id = _seed(
        store,
        transport="stdio",
        location="sandbox",
        url=None,
        launch_spec={**_LAUNCH, "mounts": []},
        auth_kind="none",
        auth_config={},
        access_token_ciphertext=None,
    )
    with _client(store) as client:
        beside = client.patch(
            f"/v1/agent/mcp/connections/{connection_id}",
            json={
                "expected_config_revision": 1,
                "launch": _LAUNCH,
                "env_secrets": {"API_KEY": "abc"},
            },
        )
        inside = client.patch(
            f"/v1/agent/mcp/connections/{connection_id}",
            json={
                "expected_config_revision": 1,
                "launch": {**_LAUNCH, "env_secrets": {"API_KEY": "abc"}},
            },
        )

    assert beside.status_code == 422
    assert inside.status_code == 200, inside.text
    assert "env_secrets" not in store.connections[connection_id]["launch_spec"]


def test_a_pasted_short_key_is_refused_in_the_preview_and_at_the_save() -> None:
    store = FakeStore()
    with _client(store) as client:
        preview = client.post(
            "/v1/agent/mcp/connections/import", json={"text": _paste({"API_KEY": "abc"})}
        )
        saved = client.post(
            "/v1/agent/mcp/connections/import",
            json={"text": _paste({"API_KEY": "abc"}), "save": True},
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["drafts"] == []
    assert preview.json()["rejected"] == [{"name": "search", "reason": _REFUSAL}]
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] == []
    assert saved.json()["rejected"] == [{"name": "search", "reason": _REFUSAL}]
    assert not store.connections


def test_a_paste_the_owner_runs_elsewhere_is_not_judged_as_a_program() -> None:
    """The owner's chosen location wins over the one the paste implies, as at the save."""
    store = FakeStore()
    body = {"text": _paste({"API_KEY": "abc"}), "locations": {"search": "user-connector"}}
    with _client(store) as client:
        preview = client.post("/v1/agent/mcp/connections/import", json=body)

    assert preview.status_code == 200, preview.text
    assert preview.json()["rejected"] == []
    assert [draft["label"] for draft in preview.json()["drafts"]] == ["search"]


def test_a_pasted_eight_character_key_is_previewed_and_saved() -> None:
    store = FakeStore()
    with _client(store) as client:
        preview = client.post(
            "/v1/agent/mcp/connections/import", json={"text": _paste({"API_KEY": "12345678"})}
        )
        saved = client.post(
            "/v1/agent/mcp/connections/import",
            json={"text": _paste({"API_KEY": "12345678"}), "save": True},
        )

    assert preview.status_code == 200, preview.text
    assert [draft["label"] for draft in preview.json()["drafts"]] == ["search"]
    assert preview.json()["rejected"] == []
    assert saved.status_code == 200, saved.text
    (row,) = store.connections.values()
    sealed = row["launch_spec"]["env_secrets"]["API_KEY"]
    assert mcp_cipher(_ENCRYPTION_KEY).decrypt(sealed) == "12345678"


def test_a_short_key_typed_in_for_a_paste_is_refused_at_the_save() -> None:
    """A value the paste left to the owner is judged when it is sealed."""
    text = _paste({"API_KEY": "${env:API_KEY}"})
    store = FakeStore()
    with _client(store) as client:
        preview = client.post("/v1/agent/mcp/connections/import", json={"text": text})
        saved = client.post(
            "/v1/agent/mcp/connections/import",
            json={"text": text, "save": True, "server_secrets": {"search": {"API_KEY": "abc"}}},
        )

    assert preview.status_code == 200, preview.text
    assert [draft["label"] for draft in preview.json()["drafts"]] == ["search"]
    assert saved.status_code == 400, saved.text
    assert _message(saved) == f"search: {_REFUSAL}"
    assert not store.connections


def test_a_short_key_stored_before_the_rule_still_opens_at_the_claim(monkeypatch) -> None:
    """Refused only when saved: a stored program keeps running, unmasked as before."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "sandbox-mcp-test-key")
    launch = replace(
        LaunchSpec(
            command="npx", args=("-y", "@scope/search-mcp@1.0.0"), env={"KEY": "secret://KEY"}
        ),
        sealed_env={"KEY": mcp_cipher().encrypt("abc")},
    )
    connection = McpConnection(
        connection_id="7",
        user_id="u",
        label="x",
        config=ConnectionConfig(
            transport=Transport.STDIO, location=ExecutionLocation.SANDBOX, launch=launch
        ),
    )

    server = sandbox_servers.sandbox_mcp_server(connection, "search")

    assert server is not None
    assert server.secret_env == {"KEY": "abc"}
    assert server.secrets() == {}


def test_only_a_sandbox_programs_key_is_judged() -> None:
    assert sandbox_servers.short_key_refusal(ExecutionLocation.SANDBOX, {"API_KEY": "abc"}) == (
        _REFUSAL
    )
    assert sandbox_servers.short_key_refusal(ExecutionLocation.USER_CONNECTOR, {"K": "a"}) is None
    assert sandbox_servers.short_key_refusal(ExecutionLocation.SANDBOX, {"K": ""}) is None
