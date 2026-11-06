"""Editing a program connection keeps the keys the edit does not mention.

A program's keys are its launch's ``secret://NAME`` values, stored sealed with
the launch. An edit follows the write-only convention every other credential
editor uses: a key the edit leaves out, or sends blank, keeps its stored
value; a key it sends replaces it; a key whose reference the new launch drops
is removed with it. A new key is judged by the short-key rule, a kept one is
not, and no response carries one.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.test_mcp_connections_api import _ENCRYPTION_KEY, FakeStore, _client, _seed

from cloud_agent.mcp import sandbox_servers
from cloud_agent.mcp.registry import connection_from_row, mcp_cipher

_KEY = "search-key-0001"
_REFUSAL = (
    "Secret API_KEY must contain at least 8 characters; "
    "a shorter value cannot be masked in the chat's output"
)

_LAUNCH = {
    "command": "npx",
    "args": ["-y", "@scope/search-mcp@1.0.0"],
    "env": {"API_KEY": "secret://API_KEY", "REGION": "eu"},
}

_PROGRAM = {
    "label": "Search",
    "transport": "stdio",
    "location": "sandbox",
    "auth_kind": "none",
    "launch": _LAUNCH,
    "env_secrets": {"API_KEY": _KEY},
}

#: The same program at a newer release: an edit of its launch, not of its keys.
_BUMPED = {**_LAUNCH, "args": ["-y", "@scope/search-mcp@1.1.0"]}


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the deployment a key, so a program's key can be sealed at all."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)


def _message(response: Any) -> str:
    return response.json()["detail"]["error"]["message"]


def _stored_keys(store: FakeStore, connection_id: int) -> dict[str, str]:
    """The program's keys as stored, opened."""
    sealed = store.connections[connection_id]["launch_spec"].get("env_secrets") or {}
    cipher = mcp_cipher(_ENCRYPTION_KEY)
    return {name: cipher.decrypt(value) for name, value in sealed.items()}


def _create(client: Any, **overrides: Any) -> dict[str, Any]:
    created = client.post("/v1/agent/mcp/connections", json={**_PROGRAM, **overrides})
    assert created.status_code == 201, created.text
    return created.json()


def _patch(client: Any, connection: dict[str, Any], **body: Any) -> Any:
    return client.patch(
        f"/v1/agent/mcp/connections/{connection['id']}",
        json={"expected_config_revision": connection["config_revision"], **body},
    )


def test_editing_a_programs_launch_keeps_the_key_it_does_not_mention() -> None:
    """The bug: a new release of the program came back without its key."""
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client)
        edited = _patch(client, connection, launch=_BUMPED)

    assert edited.status_code == 200, edited.text
    assert edited.json()["argv"] == ["npx", "-y", "@scope/search-mcp@1.1.0"]
    assert _KEY not in edited.text
    assert _stored_keys(store, connection["id"]) == {"API_KEY": _KEY}
    # What the user sees: the program still starts in a chat, with its key.
    row = store.connections[connection["id"]]
    server = sandbox_servers.sandbox_mcp_server(connection_from_row(row), "search")
    assert server is not None
    assert server.secret_env == {"API_KEY": _KEY}


def test_a_blank_key_in_an_edit_keeps_the_stored_one() -> None:
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client)
        edited = _patch(client, connection, launch=_BUMPED, env_secrets={"API_KEY": ""})

    assert edited.status_code == 200, edited.text
    assert _stored_keys(store, connection["id"]) == {"API_KEY": _KEY}


def test_an_edit_that_sets_a_key_replaces_it_and_keeps_the_others() -> None:
    launch = {**_LAUNCH, "env": {**_LAUNCH["env"], "TOKEN": "secret://TOKEN"}}
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client, launch=launch, env_secrets={"API_KEY": _KEY, "TOKEN": "t" * 9})
        edited = _patch(
            client,
            connection,
            launch={**launch, "args": _BUMPED["args"]},
            env_secrets={"API_KEY": "search-key-0002"},
        )

    assert edited.status_code == 200, edited.text
    assert "search-key-0002" not in edited.text
    assert _stored_keys(store, connection["id"]) == {"API_KEY": "search-key-0002", "TOKEN": "t" * 9}


def test_a_key_can_be_replaced_without_resending_the_launch() -> None:
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client)
        edited = _patch(client, connection, env_secrets={"API_KEY": "search-key-0002"})

    assert edited.status_code == 200, edited.text
    assert edited.json()["config_revision"] == connection["config_revision"] + 1
    stored = store.connections[connection["id"]]["launch_spec"]
    assert {key: stored[key] for key in ("command", "args", "env")} == _LAUNCH
    assert _stored_keys(store, connection["id"]) == {"API_KEY": "search-key-0002"}


def test_dropping_a_keys_reference_removes_the_key() -> None:
    """The launch names a program's keys, so a launch without the name removes it."""
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client)
        edited = _patch(client, connection, launch={**_BUMPED, "env": {"REGION": "eu"}})

    assert edited.status_code == 200, edited.text
    assert _stored_keys(store, connection["id"]) == {}


def test_a_new_short_key_is_refused_and_nothing_changes() -> None:
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client)
        before = dict(store.connections[connection["id"]])
        refused = _patch(client, connection, launch=_BUMPED, env_secrets={"API_KEY": "abc"})

    assert refused.status_code == 400, refused.text
    assert _message(refused) == _REFUSAL
    assert store.connections[connection["id"]] == before


def test_a_short_key_stored_before_the_rule_is_kept_by_an_edit() -> None:
    """The rule judges a key when it is set; a kept key is not being set."""
    store = FakeStore()
    connection_id = _seed(
        store,
        transport="stdio",
        location="sandbox",
        url=None,
        launch_spec={
            **_LAUNCH,
            "mounts": [],
            "env_secrets": {"API_KEY": mcp_cipher(_ENCRYPTION_KEY).encrypt("abc")},
        },
        auth_kind="none",
        auth_config={},
        access_token_ciphertext=None,
    )
    with _client(store) as client:
        edited = client.patch(
            f"/v1/agent/mcp/connections/{connection_id}",
            json={"expected_config_revision": 1, "launch": _BUMPED},
        )

    assert edited.status_code == 200, edited.text
    assert _stored_keys(store, connection_id) == {"API_KEY": "abc"}


def test_a_sealed_value_inside_the_submitted_launch_is_never_stored() -> None:
    """Only the platform seals a key: ciphertext in the document is dropped."""
    forged = mcp_cipher(_ENCRYPTION_KEY).encrypt("forged-key-value")
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client)
        edited = _patch(client, connection, launch={**_BUMPED, "env_secrets": {"API_KEY": forged}})

    assert edited.status_code == 200, edited.text
    assert _stored_keys(store, connection["id"]) == {"API_KEY": _KEY}


def test_a_remote_connection_has_no_keys_to_edit() -> None:
    store = FakeStore()
    connection_id = _seed(store)
    with _client(store) as client:
        refused = client.patch(
            f"/v1/agent/mcp/connections/{connection_id}",
            json={"expected_config_revision": 1, "env_secrets": {"API_KEY": _KEY}},
        )

    assert refused.status_code == 400, refused.text
    assert _message(refused) == "a remote connection has no process to launch"


def test_a_stale_edit_keeps_the_keys_and_is_refused() -> None:
    store = FakeStore()
    with _client(store) as client:
        connection = _create(client)
        refused = client.patch(
            f"/v1/agent/mcp/connections/{connection['id']}",
            json={
                "expected_config_revision": connection["config_revision"] + 1,
                "env_secrets": {"API_KEY": "search-key-0002"},
            },
        )

    assert refused.status_code == 409, refused.text
    assert _stored_keys(store, connection["id"]) == {"API_KEY": _KEY}
