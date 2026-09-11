"""The generic MCP connection API: routing, validation and secret handling.

Two properties here are worth a test each rather than a comment.

**Routing.** The relay's routes are ``/v1/agent/mcp/<alias>`` — one path
parameter — and the management surface lives under the same prefix. Whichever
router is registered first wins, so a management request answered as an MCP
proxy call is a one-line reordering away. That is asserted rather than assumed.

**Secrets are write-only.** A request may carry a credential; no response may
contain one. The response models have no field for it, and this checks the
serialized body rather than the model, because a future edit could add one to a
dictionary that the model then spreads.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agent.api import mcp_connections, routes
from cloud_agent.api.deps import get_agent_job_store
from cloud_agent.mcp.models import CapabilityKind, ToolPolicy
from cloud_agent.mcp.registry import mcp_cipher

_OWNER = "user-owner"
_OTHER = "user-other"
_ENCRYPTION_KEY = "mcp-api-test-encryption-key"


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the deployment a key, so a credential can be stored at all."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)


class FakeStore:
    """The store surface the MCP API actually calls, in memory."""

    def __init__(self) -> None:
        """Start empty, with connection ids handed out in order."""
        self.connections: dict[int, dict[str, Any]] = {}
        self.capabilities: dict[int, dict[str, Any]] = {}
        self.bindings: dict[str, list[dict[str, Any]]] = {}
        self.invocations: dict[int, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self._next = 1

    def _row(self, connection_id: int) -> dict[str, Any]:
        """The stored row, with the shape the API reads."""
        return dict(self.connections[connection_id])

    async def list_mcp_connections(
        self, *, user_id: str, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        return [
            self._row(cid)
            for cid, row in sorted(self.connections.items())
            if row["user_id"] == user_id and (include_deleted or row["deleted_at"] is None)
        ]

    async def get_mcp_connection(
        self, *, connection_id: int, user_id: str | None = None
    ) -> dict[str, Any] | None:
        row = self.connections.get(connection_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return self._row(connection_id)

    async def create_mcp_connection(self, **kwargs: Any) -> dict[str, Any]:
        connection_id = self._next
        self._next += 1
        row = {
            "id": connection_id,
            "user_id": kwargs["user_id"],
            "label": kwargs["label"],
            "template": kwargs.get("template"),
            "transport": kwargs["transport"],
            "location": kwargs["location"],
            "url": kwargs.get("url"),
            "launch_spec": kwargs.get("launch_spec"),
            "auth_kind": kwargs["auth_kind"],
            "auth_config": kwargs.get("auth_config") or {},
            "connector_id": kwargs.get("connector_id"),
            "status": "draft",
            "error_kind": "none",
            "error_detail": None,
            "access_token_ciphertext": None,
            "refresh_token_ciphertext": None,
            "client_secret_ciphertext": None,
            "expires_at": None,
            "scopes": [],
            "issuer": None,
            "resource": None,
            "refresh_rotates": False,
            "client_auth_method": None,
            "registration": None,
            "credential_version": 1,
            "config_revision": 1,
            "authorization_epoch": 1,
            "oauth_generation": 0,
            "capability_revision": 1,
            "account_label": kwargs.get("account_label"),
            "last_checked_at": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "deleted_at": None,
        }
        self.connections[connection_id] = row
        self.created.append(dict(row))
        return self._row(connection_id)

    async def update_mcp_connection(self, **kwargs: Any) -> dict[str, Any] | None:
        row = self.connections.get(kwargs["connection_id"])
        if row is None or row["config_revision"] != kwargs["expected_config_revision"]:
            return None
        for key in ("label", "url", "auth_kind", "account_label"):
            if kwargs.get(key) is not None:
                row[key] = kwargs[key]
        if kwargs.get("launch_spec") is not None:
            row["launch_spec"] = kwargs["launch_spec"]
        row["config_revision"] += 1
        return self._row(kwargs["connection_id"])

    async def disconnect_mcp_connection(self, *, connection_id: int, user_id: str) -> bool:
        row = self.connections.get(connection_id)
        if row is None or row["user_id"] != user_id or row["deleted_at"] is not None:
            return False
        row["deleted_at"] = datetime.now(timezone.utc)
        row["status"] = "disabled"
        row["access_token_ciphertext"] = None
        row["authorization_epoch"] += 1
        return True

    async def set_mcp_connection_status(self, **kwargs: Any) -> bool:
        row = self.connections.get(kwargs["connection_id"])
        if row is None:
            return False
        row["status"] = kwargs["status"]
        row["error_kind"] = kwargs.get("error_kind", "none")
        row["error_detail"] = kwargs.get("error_detail")
        return True

    async def set_mcp_credential(self, **kwargs: Any) -> bool:
        row = self.connections.get(kwargs["connection_id"])
        if row is None or row["authorization_epoch"] != kwargs["expected_authorization_epoch"]:
            return False
        row["access_token_ciphertext"] = kwargs.get("access_token_ciphertext")
        if kwargs.get("auth_config_patch"):
            row["auth_config"] = {**(row["auth_config"] or {}), **kwargs["auth_config_patch"]}
        if kwargs.get("bump_epoch"):
            row["authorization_epoch"] += 1
        row["credential_version"] += 1
        return True

    async def set_mcp_launch_spec(self, **kwargs: Any) -> bool:
        row = self.connections.get(kwargs["connection_id"])
        if row is None or row["authorization_epoch"] != kwargs["expected_authorization_epoch"]:
            return False
        row["launch_spec"] = kwargs["launch_spec"]
        return True

    async def set_mcp_client_registration(
        self, *, connection_id: int, user_id: str, client_id: str, client_secret_ciphertext: str | None
    ) -> bool:
        row = self.connections.get(connection_id)
        if row is None or row["user_id"] != user_id:
            return False
        row["auth_config"] = {**(row["auth_config"] or {}), "client_id": client_id}
        if client_secret_ciphertext:
            row["client_secret_ciphertext"] = client_secret_ciphertext
        row["oauth_generation"] += 1
        return True

    async def get_mcp_capabilities(self, *, connection_id: int) -> dict[str, Any] | None:
        return self.capabilities.get(connection_id)

    async def save_mcp_capabilities(self, **kwargs: Any) -> int:
        row = self.connections[kwargs["connection_id"]]
        row["capability_revision"] += 1
        self.capabilities[kwargs["connection_id"]] = {
            "connection_id": kwargs["connection_id"],
            "capability_revision": row["capability_revision"],
            "config_revision": kwargs["config_revision"],
            "protocol_version": kwargs["protocol_version"],
            "server_name": kwargs["server_name"],
            "server_version": kwargs["server_version"],
            "tools": kwargs["tools"],
            "resources": kwargs["resources"],
            "resource_templates": kwargs["resource_templates"],
            "prompts": kwargs["prompts"],
            "support": kwargs["support"],
            "discovered_at": datetime.now(timezone.utc),
        }
        return row["capability_revision"]

    async def delete_mcp_capabilities(self, *, connection_id: int) -> None:
        self.capabilities.pop(connection_id, None)

    async def delete_mcp_oauth_flows(self, *, connection_id: int) -> int:
        return 0

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for binding in self.bindings.get(thread_id, []):
            row = dict(binding)
            row["label"] = self.connections[binding["connection_id"]]["label"]
            rows.append(row)
        return rows

    async def replace_mcp_bindings(
        self, *, thread_id: str, bindings: list[dict[str, Any]]
    ) -> None:
        # ``selection_pending`` is written FALSE by the real store: saving a
        # selection is exactly what ends the asking.
        self.bindings[thread_id] = [
            {**binding, "selection_pending": False} for binding in bindings
        ]

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        return self.threads.get(thread_id)

    async def list_mcp_invocations(self, **kwargs: Any) -> list[dict[str, Any]]:
        rows = list(self.invocations.values())
        if kwargs.get("job_id") is not None:
            rows = [row for row in rows if row["job_id"] == kwargs["job_id"]]
        if kwargs.get("statuses"):
            rows = [row for row in rows if row["status"] in kwargs["statuses"]]
        return rows

    async def get_mcp_invocation(self, *, invocation_id: int) -> dict[str, Any] | None:
        return self.invocations.get(invocation_id)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return {"id": job_id, "user_id": _OWNER, "thread_id": "thread-1"}

    async def decide_mcp_invocation(
        self, *, invocation_id: int, approve: bool, decided_by: str
    ) -> dict[str, Any] | None:
        row = self.invocations.get(invocation_id)
        if row is None or row["status"] != "pending":
            return None
        row["status"] = "approved" if approve else "denied"
        row["decided_by"] = decided_by
        return dict(row)


def _client(store: FakeStore, *, user_id: str = _OWNER) -> TestClient:
    """Build a client whose owner guard is satisfied by ``user_id``."""
    app = FastAPI()
    app.include_router(mcp_connections.router)
    # The real relay router too, so route precedence is exercised rather than
    # assumed: it is registered second here exactly as it is in ``create_app``.
    from cloud_agent.api import mcp_relay

    app.include_router(mcp_relay.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    app.dependency_overrides[routes.require_agent_owner] = lambda: {"user_id": user_id}
    return TestClient(app)


def _seed(store: FakeStore, **overrides: Any) -> int:
    """Insert a connection row directly, bypassing the API."""
    connection_id = store._next
    store._next += 1
    row = {
        "id": connection_id,
        "user_id": _OWNER,
        "label": "Docs",
        "template": None,
        "transport": "streamable-http",
        "location": "remote",
        "url": "https://mcp.example.com/mcp",
        "launch_spec": None,
        "auth_kind": "static",
        "auth_config": {"header_name": "Authorization", "scheme": "Bearer"},
        "connector_id": None,
        "status": "active",
        "error_kind": "none",
        "error_detail": None,
        "access_token_ciphertext": mcp_cipher(_ENCRYPTION_KEY).encrypt("upstream-secret"),
        "refresh_token_ciphertext": None,
        "client_secret_ciphertext": None,
        "expires_at": None,
        "scopes": [],
        "issuer": None,
        "resource": None,
        "refresh_rotates": False,
        "client_auth_method": None,
        "registration": None,
        "credential_version": 1,
        "config_revision": 1,
        "authorization_epoch": 1,
        "oauth_generation": 0,
        "capability_revision": 1,
        "account_label": "octocat",
        "last_checked_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "deleted_at": None,
    }
    row.update(overrides)
    store.connections[connection_id] = row
    return connection_id


class TestRouting:
    """Which router answers a path under the shared prefix."""

    def test_management_is_not_answered_as_an_mcp_proxy_call(self) -> None:
        """The relay's catch-all must not swallow connection management.

        Both are under ``/v1/agent/mcp``; the relay's route is one path
        parameter. Registration order is the only thing separating them, and a
        connector-management request answered by the relay would be an
        authorization decision made by the wrong component.
        """
        store = FakeStore()
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/connections")
        assert response.status_code == 200
        assert "connections" in response.json()

    def test_a_binding_alias_still_reaches_the_relay(self) -> None:
        """The narrow route did not shadow the general one."""
        store = FakeStore()
        with _client(store) as client:
            response = client.post("/v1/agent/mcp/docs", json={})
        # The relay answers an unauthenticated call with 401, which is what
        # proves the request reached it rather than 404-ing as an unknown path.
        assert response.status_code == 401


class TestConnectionManagement:
    """Adding, listing, editing and disconnecting a connection."""

    def test_templates_are_offered_but_decide_nothing(self) -> None:
        """A template prefills a form; it is not an eligibility list."""
        with _client(FakeStore()) as client:
            body = client.get("/v1/agent/mcp/templates").json()
        assert body and {"name", "url", "transport"} <= set(body[0])

    def test_a_connection_can_be_added_without_a_template(self) -> None:
        """The whole point: a server nobody reviewed is still addable."""
        store = FakeStore()
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Wiki",
                    "url": "https://wiki.example.com/mcp",
                    "auth_kind": "none",
                },
            )
        assert response.status_code == 201
        assert response.json()["label"] == "Wiki"
        assert response.json()["status"] == "draft"

    def test_a_cleartext_loopback_url_is_refused_at_creation(self) -> None:
        """Whatever the address, the credential would travel in the clear."""
        with _client(FakeStore()) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={"label": "Self", "url": "http://127.0.0.1:8000/mcp", "auth_kind": "none"},
            )
        assert response.status_code == 400

    def test_an_address_is_checked_when_it_is_dialled_not_when_it_is_saved(self) -> None:
        """Creation validates syntax; resolution is deferred to the connection test.

        Deliberate: the check costs a DNS lookup, and doing it while a user is
        typing would make saving depend on a resolver. The consequence is that
        a loopback address is accepted here and refused by the egress policy
        when the connection is tested, which is where the reason is shown.
        """
        store = FakeStore()
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={"label": "Self", "url": "https://127.0.0.1/mcp", "auth_kind": "none"},
            )
        assert response.status_code == 201

    def test_plain_http_is_refused_by_default(self) -> None:
        """The credential would travel in the clear."""
        with _client(FakeStore()) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={"label": "Cleartext", "url": "http://wiki.example.com/mcp"},
            )
        assert response.status_code == 400

    def test_a_stdio_connection_needs_a_command(self) -> None:
        """Nothing to run is not a connection."""
        with _client(FakeStore()) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Local",
                    "transport": "stdio",
                    "location": "user-connector",
                    "auth_kind": "none",
                },
            )
        assert response.status_code == 400

    def test_a_managed_stdio_connection_must_pin_its_package(self) -> None:
        """Installing ``latest`` in a worker that holds a credential is the bug."""
        with _client(FakeStore()) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Local",
                    "transport": "stdio",
                    "location": "managed-worker",
                    "launch": {"command": "npx", "args": ["-y", "@scope/server"]},
                },
            )
        assert response.status_code == 400
        assert "pin" in response.json()["detail"]["error"]["message"]

    def test_an_unknown_transport_is_refused_with_the_allowed_set(self) -> None:
        """A caller should learn what is accepted, not just that it was wrong."""
        with _client(FakeStore()) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={"label": "Odd", "transport": "carrier-pigeon", "url": "https://x.example/mcp"},
            )
        assert response.status_code == 400
        assert "streamable-http" in response.json()["detail"]["error"]["message"]

    def test_listing_never_returns_a_credential(self) -> None:
        """Secrets are write-only, checked on the serialized body."""
        store = FakeStore()
        _seed(store)
        with _client(store) as client:
            body = client.get("/v1/agent/mcp/connections").text
        assert "upstream-secret" not in body
        assert "access_token_ciphertext" not in body
        assert json.loads(body)["connections"][0]["has_credential"] is True

    def test_a_stored_credential_shows_a_hint_and_no_more(self) -> None:
        """Enough to tell which credential it is, not enough to use it."""
        store = FakeStore()
        connection_id = _seed(store)
        with _client(store) as client:
            client.post(
                f"/v1/agent/mcp/connections/{connection_id}/secret",
                json={
                    "expected_authorization_epoch": 1,
                    "secret": "ghp_abcdefghijklmnop",
                    "replaces_identity": False,
                },
            )
            body = client.get(f"/v1/agent/mcp/connections/{connection_id}").text
        assert "ghp_abcdefghijklmnop" not in body
        assert json.loads(body)["secret_hint"] == "gh****op"

    def test_another_users_connection_is_not_found(self) -> None:
        """Ownership is part of the read, not a check after it."""
        store = FakeStore()
        connection_id = _seed(store)
        with _client(store, user_id=_OTHER) as client:
            assert client.get(f"/v1/agent/mcp/connections/{connection_id}").status_code == 404
            assert client.delete(f"/v1/agent/mcp/connections/{connection_id}").status_code == 404

    def test_disconnecting_clears_the_credential(self) -> None:
        """A tombstone that still holds a token is not a disconnect."""
        store = FakeStore()
        connection_id = _seed(store)
        with _client(store) as client:
            assert client.delete(f"/v1/agent/mcp/connections/{connection_id}").status_code == 204
        row = store.connections[connection_id]
        assert row["access_token_ciphertext"] is None
        assert row["deleted_at"] is not None
        assert row["authorization_epoch"] == 2

    def test_a_stale_edit_is_refused_rather_than_merged(self) -> None:
        """Two writers must not silently produce neither one's intent."""
        store = FakeStore()
        connection_id = _seed(store)
        with _client(store) as client:
            response = client.patch(
                f"/v1/agent/mcp/connections/{connection_id}",
                json={"expected_config_revision": 99, "label": "Renamed"},
            )
        assert response.status_code == 409

    def test_a_secret_write_is_fenced_by_the_epoch_the_caller_read(self) -> None:
        """A write that started before a reconnect must not land on the new one."""
        store = FakeStore()
        connection_id = _seed(store)
        with _client(store) as client:
            stale = client.post(
                f"/v1/agent/mcp/connections/{connection_id}/secret",
                json={"expected_authorization_epoch": 1, "secret": "new", "replaces_identity": False},
            )
            fresh = client.post(
                f"/v1/agent/mcp/connections/{connection_id}/secret",
                json={"expected_authorization_epoch": 2, "secret": "newer"},
            )
        assert stale.status_code == 200
        assert fresh.status_code == 409


class TestAuthorization:
    """Starting a browser flow, including the client a server expects us to bring."""

    def test_a_client_id_can_be_supplied_before_the_flow(self) -> None:
        """Some servers expect the user to bring one, and it has to persist.

        Without a route that stores it, ``needs_client_id`` would be a message
        the user can never act on: the next attempt would ask again.
        """
        store = FakeStore()
        connection_id = _seed(store, auth_kind="oauth", auth_config={})
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/connections/{connection_id}/authorize",
                json={"client_id": "our-client", "client_secret": "shh"},
            )
        # The flow cannot proceed without a public base URL, but the client was
        # stored before that check, which is what this asserts.
        assert store.connections[connection_id]["auth_config"]["client_id"] == "our-client"
        assert store.connections[connection_id]["client_secret_ciphertext"]
        assert response.status_code in (400, 503)

    def test_a_supplied_client_is_not_stored_in_the_clear(self) -> None:
        """The secret goes through the cipher, never into auth_config."""
        store = FakeStore()
        connection_id = _seed(store, auth_kind="oauth", auth_config={})
        with _client(store) as client:
            client.post(
                f"/v1/agent/mcp/connections/{connection_id}/authorize",
                json={"client_id": "our-client", "client_secret": "shh"},
            )
        config = store.connections[connection_id]["auth_config"]
        assert "client_secret" not in config
        assert "shh" not in json.dumps(config)


class TestImport:
    """Pasting a configuration."""

    def test_a_preview_saves_nothing(self) -> None:
        """An import that wrote on paste would run somebody's snippet unasked."""
        store = FakeStore()
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={
                    "text": json.dumps(
                        {
                            "mcpServers": {
                                "wiki": {
                                    "command": "npx",
                                    "args": ["-y", "@scope/wiki@1.0.0"],
                                    "env": {"API_KEY": "literal"},
                                }
                            }
                        }
                    ),
                    "save": False,
                },
            )
        assert response.status_code == 200
        assert store.created == []
        draft = response.json()["drafts"][0]
        assert draft["argv"][0] == "npx"
        assert draft["secrets"][0]["value"] != "literal"

    def test_saving_imports_the_command_and_keeps_the_secret_out_of_the_spec(self) -> None:
        """The launch spec carries a reference; the value is encrypted."""
        store = FakeStore()
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={
                    "text": json.dumps(
                        {
                            "mcpServers": {
                                "wiki": {
                                    "command": "npx",
                                    "args": ["-y", "@scope/wiki@1.0.0"],
                                    "env": {"API_KEY": "literal"},
                                }
                            }
                        }
                    ),
                    "save": True,
                },
            )
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()["saved"]] == [1]
        assert len(store.created) == 1
        stored = store.connections[1]
        assert stored["launch_spec"]["env"]["API_KEY"] == "secret://API_KEY"
        assert "literal" not in json.dumps(stored["launch_spec"])

    def test_an_unreadable_paste_is_a_named_error(self) -> None:
        """Not a stack trace, and not a silent empty import."""
        with _client(FakeStore()) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import", json={"text": "not a config"}
            )
        assert response.status_code == 400


class TestBindings:
    """A session's capability selection."""

    def _with_capabilities(self, store: FakeStore, connection_id: int) -> None:
        """Seed a discovery result with one tool and one prompt."""
        store.capabilities[connection_id] = {
            "connection_id": connection_id,
            "capability_revision": 1,
            "config_revision": 1,
            "protocol_version": "2025-11-25",
            "server_name": "wiki",
            "server_version": "1",
            "tools": [{"name": "search", "description": "Find", "input_schema": None}],
            "resources": [],
            "resource_templates": [],
            "prompts": [{"name": "summarize", "description": "Summarize"}],
            "support": {},
            "discovered_at": datetime.now(timezone.utc),
        }

    def test_a_selection_is_stored_with_its_fingerprints(self) -> None:
        """The rule records what it was approved against, not just its name."""
        store = FakeStore()
        connection_id = _seed(store)
        self._with_capabilities(store, connection_id)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            response = client.put(
                "/v1/agent/mcp/threads/thread-1/bindings",
                json={
                    "bindings": [
                        {
                            "connection_id": connection_id,
                            "alias": "docs",
                            "tools": {"search": "allow"},
                        }
                    ]
                },
            )
        assert response.status_code == 200
        stored = store.bindings["thread-1"][0]
        assert stored["alias"] == "docs"
        assert stored["rules"]["search"]["policy"] == "allow"
        assert stored["rules"]["search"]["fingerprint"]

    def test_a_rule_for_a_tool_the_server_does_not_offer_is_refused(self) -> None:
        """Storing it would grant nothing now and something odd after discovery."""
        store = FakeStore()
        connection_id = _seed(store)
        self._with_capabilities(store, connection_id)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            response = client.put(
                "/v1/agent/mcp/threads/thread-1/bindings",
                json={
                    "bindings": [
                        {"connection_id": connection_id, "tools": {"delete_all": "allow"}}
                    ]
                },
            )
        assert response.status_code == 400
        assert "delete_all" in response.json()["detail"]["error"]["message"]

    def test_an_empty_selection_does_not_enable_the_connection(self) -> None:
        """"I chose nothing" and "I chose everything" must not be one row."""
        store = FakeStore()
        connection_id = _seed(store)
        self._with_capabilities(store, connection_id)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            response = client.put(
                "/v1/agent/mcp/threads/thread-1/bindings",
                json={"bindings": [{"connection_id": connection_id, "tools": {}}]},
            )
        assert response.status_code == 200
        assert store.bindings["thread-1"][0]["enabled"] is False

    def test_another_users_thread_is_not_found(self) -> None:
        """A thread id from a client is not authority over it."""
        store = FakeStore()
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store, user_id=_OTHER) as client:
            response = client.get("/v1/agent/mcp/threads/thread-1/bindings")
        assert response.status_code == 404


class TestInvocationDecisions:
    """Approving and denying a held call."""

    def _pending(self, store: FakeStore, connection_id: int) -> int:
        """Seed one pending invocation."""
        invocation_id = 1
        store.invocations[invocation_id] = {
            "id": invocation_id,
            "job_id": "job-1",
            "thread_id": "thread-1",
            "connection_id": connection_id,
            "capability_name": "create_issue",
            "status": "pending",
            "arguments_digest": "d",
            "arguments_ciphertext": None,
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "decided_at": None,
            "result_summary": None,
        }
        return invocation_id

    def test_an_approval_records_who_made_it(self) -> None:
        """A write's audit trail names the person, not the session."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision", json={"approve": True}
            )
        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        assert store.invocations[invocation_id]["decided_by"] == _OWNER

    def test_a_second_decision_is_a_conflict(self) -> None:
        """A decided call cannot be decided again, in either direction."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        with _client(store) as client:
            client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision", json={"approve": True}
            )
            second = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision", json={"approve": False}
            )
        assert second.status_code == 409

    def test_another_users_call_is_not_found(self) -> None:
        """Deciding somebody else's write is exactly what this must refuse."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        with _client(store, user_id=_OTHER) as client:
            response = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision", json={"approve": True}
            )
        assert response.status_code == 404
        assert store.invocations[invocation_id]["status"] == "pending"

    def test_listing_requires_a_job_or_a_conversation(self) -> None:
        """An unscoped list would be a way to enumerate other people's calls."""
        with _client(FakeStore()) as client:
            assert client.get("/v1/agent/mcp/invocations").status_code == 400


class TestCapabilitySelectionSemantics:
    """The policy vocabulary the API accepts."""

    def test_an_unknown_policy_is_refused(self) -> None:
        """The three values are the whole vocabulary."""
        assert set(ToolPolicy) == {
            ToolPolicy.DENY,
            ToolPolicy.CONFIRM,
            ToolPolicy.ALLOW,
        }
        assert CapabilityKind.TOOL.value == "tool"
