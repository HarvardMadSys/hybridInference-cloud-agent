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
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from cloud_agent.api import mcp_connections, routes
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp.models import CapabilityKind, ToolPolicy
from cloud_agent.mcp.registry import mcp_cipher
from tests.support.storage import AllCapabilities, OneFake

_OWNER = "user-owner"
_OTHER = "user-other"
_ENCRYPTION_KEY = "mcp-api-test-encryption-key"


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the deployment a key, so a credential can be stored at all."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)


class FakeStore(AllCapabilities):
    """The store surface the MCP API actually calls, in memory."""

    def __init__(self) -> None:
        """Start empty, with connection ids handed out in order."""
        self.connections: dict[int, dict[str, Any]] = {}
        self.capabilities: dict[int, dict[str, Any]] = {}
        self.bindings: dict[str, list[dict[str, Any]]] = {}
        self.invocations: dict[int, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.connectors: dict[int, dict[str, Any]] = {}
        self.connector_connections: dict[int, set[int]] = {}
        #: One pending OAuth flow, keyed by the state hash the callback presents.
        self.oauth_flow: dict[str, Any] | None = None
        self.oauth_state_hash: str | None = None
        self.credential_writes: list[dict[str, Any]] = []
        self._next = 1
        self._next_connector = 1

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
            "request_timeout_s": kwargs.get("request_timeout_s"),
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
        for key in ("label", "url", "auth_kind", "account_label", "request_timeout_s"):
            if kwargs.get(key) is not None:
                row[key] = kwargs[key]
        if kwargs.get("launch_spec") is not None:
            row["launch_spec"] = kwargs["launch_spec"]
        if any(
            kwargs.get(key) is not None
            for key in ("transport", "location", "url", "launch_spec", "auth_kind", "auth_config")
        ):
            row["config_revision"] += 1
            row["authorization_epoch"] += 1
            row.update(status="draft", error_kind="none", error_detail=None)
            self.capabilities.pop(kwargs["connection_id"], None)
            if self.oauth_flow and str(self.oauth_flow["connection_id"]) == str(
                kwargs["connection_id"]
            ):
                self.oauth_flow = None
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
        if row is None or row["deleted_at"] is not None:
            return False
        for version in (
            "config_revision",
            "authorization_epoch",
            "capability_revision",
            "credential_version",
        ):
            expected = kwargs.get(f"expected_{version}")
            if expected is not None and row[version] != expected:
                return False
        row["status"] = kwargs["status"]
        row["error_kind"] = kwargs.get("error_kind", "none")
        row["error_detail"] = kwargs.get("error_detail")
        return True

    async def set_mcp_credential(self, **kwargs: Any) -> bool:
        row = self.connections.get(kwargs["connection_id"])
        if row is None or row["authorization_epoch"] != kwargs["expected_authorization_epoch"]:
            return False
        self.credential_writes.append(dict(kwargs))
        row["access_token_ciphertext"] = kwargs.get("access_token_ciphertext")
        if kwargs.get("refresh_token_ciphertext"):
            row["refresh_token_ciphertext"] = kwargs["refresh_token_ciphertext"]
        if kwargs.get("client_secret_ciphertext") is not None:
            row["client_secret_ciphertext"] = kwargs["client_secret_ciphertext"]
        if kwargs.get("auth_config_patch"):
            row["auth_config"] = {**(row["auth_config"] or {}), **kwargs["auth_config_patch"]}
        for key in (
            "expires_at",
            "issuer",
            "resource",
            "client_auth_method",
            "registration",
            "token_endpoint",
            "revocation_endpoint",
            "registration_endpoint",
        ):
            if kwargs.get(key) is not None:
                row[key] = kwargs[key]
        if kwargs.get("bump_epoch"):
            row["authorization_epoch"] += 1
        row["credential_version"] += 1
        if kwargs.get("reset_status"):
            row.update(status="draft", error_kind="none", error_detail=None, last_checked_at=None)
        return True

    async def consume_mcp_oauth_flow(self, *, state_hash: str) -> dict[str, Any] | None:
        """Hand back a flow once, for the state hash the caller presents."""
        if self.oauth_flow is None or state_hash != self.oauth_state_hash:
            return None
        flow, self.oauth_flow = self.oauth_flow, None
        return flow

    async def set_mcp_launch_spec(self, **kwargs: Any) -> bool:
        row = self.connections.get(kwargs["connection_id"])
        if row is None or row["authorization_epoch"] != kwargs["expected_authorization_epoch"]:
            return False
        row["launch_spec"] = kwargs["launch_spec"]
        return True

    async def set_mcp_client_registration(
        self,
        *,
        connection_id: int,
        user_id: str,
        client_id: str,
        client_secret_ciphertext: str | None,
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

    # -- Connectors --

    async def create_mcp_connector(
        self, *, user_id: str, name: str, registration_hash: str
    ) -> dict[str, Any]:
        connector_id = self._next_connector
        self._next_connector += 1
        row = {
            "id": connector_id,
            "user_id": user_id,
            "name": name,
            "status": "pending",
            "mounts": [],
            "registration_hash": registration_hash,
            "created_at": datetime.now(timezone.utc),
            "last_seen_at": None,
            "revoked_at": None,
        }
        self.connectors[connector_id] = row
        return dict(row)

    async def list_mcp_connectors(self, *, user_id: str) -> list[dict[str, Any]]:
        return [
            dict(row) for _, row in sorted(self.connectors.items()) if row["user_id"] == user_id
        ]

    async def update_mcp_connector(
        self, *, connector_id: int, user_id: str, name: str | None = None, mounts: Any = None
    ) -> dict[str, Any] | None:
        row = self.connectors.get(connector_id)
        if row is None or row["user_id"] != user_id or row["status"] == "revoked":
            return None
        if name is not None:
            row["name"] = name
        if mounts is not None:
            row["mounts"] = list(mounts)
        return dict(row)

    async def revoke_mcp_connector(self, *, connector_id: int, user_id: str) -> bool:
        row = self.connectors.get(connector_id)
        if row is None or row["user_id"] != user_id or row["status"] == "revoked":
            return False
        row["status"] = "revoked"
        row["revoked_at"] = datetime.now(timezone.utc)
        for connection in self.connections.values():
            if connection.get("connector_id") == connector_id:
                # The real store moves both versions here; the API never reads
                # them, so this fake only records the detach.
                connection["connector_id"] = None
        self.connector_connections.pop(connector_id, None)
        return True

    async def bind_mcp_connection_connector(
        self, *, connection_id: int, user_id: str, connector_id: int
    ) -> dict[str, Any] | None:
        row = self.connections.get(connection_id)
        connector = self.connectors.get(connector_id)
        if (
            row is None
            or row["user_id"] != user_id
            or row["deleted_at"] is not None
            or connector is None
            or connector["user_id"] != user_id
            or connector["status"] == "revoked"
        ):
            return None
        row["connector_id"] = connector_id
        row["config_revision"] += 1
        row["authorization_epoch"] += 1
        self.connector_connections.setdefault(connector_id, set()).add(connection_id)
        return self._row(connection_id)

    async def unbind_mcp_connection_connector(
        self, *, connection_id: int, user_id: str
    ) -> dict[str, Any] | None:
        row = self.connections.get(connection_id)
        if row is None or row["user_id"] != user_id or row["connector_id"] is None:
            return None
        self.connector_connections.get(row["connector_id"], set()).discard(connection_id)
        row["connector_id"] = None
        row["config_revision"] += 1
        row["authorization_epoch"] += 1
        return self._row(connection_id)

    async def list_mcp_connector_connections(self, *, connector_id: int) -> list[int]:
        return sorted(self.connector_connections.get(connector_id, set()))

    async def save_mcp_capabilities(self, **kwargs: Any) -> int:
        row = self.connections[kwargs["connection_id"]]
        if row["deleted_at"] is not None or row["config_revision"] != kwargs["config_revision"]:
            return 0
        for version in ("authorization_epoch", "capability_revision", "credential_version"):
            expected = kwargs.get(f"expected_{version}")
            if expected is not None and row[version] != expected:
                return 0
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

    async def replace_mcp_bindings(self, *, thread_id: str, bindings: list[dict[str, Any]]) -> None:
        # ``selection_pending`` is written FALSE by the real store: saving a
        # selection is exactly what ends the asking.
        self.bindings[thread_id] = [{**binding, "selection_pending": False} for binding in bindings]

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
    app.dependency_overrides[get_storage] = lambda: OneFake(store)
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
                json={
                    "label": "Odd",
                    "transport": "carrier-pigeon",
                    "url": "https://x.example/mcp",
                },
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

    async def test_edit_cannot_erase_a_discovery_of_the_new_target(self, monkeypatch):
        """A second request can discover the edited target before PATCH returns."""
        store = FakeStore()
        connection_id = _seed(store)
        update = store.update_mcp_connection

        async def update_then_discover(**kwargs):
            row = await update(**kwargs)
            await store.save_mcp_capabilities(
                connection_id=connection_id,
                config_revision=row["config_revision"],
                protocol_version="2025-11-25",
                server_name="new-target",
                server_version="1",
                tools=[],
                resources=[],
                resource_templates=[],
                prompts=[],
                support={},
            )
            return row

        monkeypatch.setattr(store, "update_mcp_connection", update_then_discover)
        await mcp_connections.update_connection(
            connection_id=connection_id,
            body=mcp_connections.UpdateConnectionRequest(
                expected_config_revision=1, url="https://new.example.com/mcp"
            ),
            user={"user_id": _OWNER},
            store=store,
        )
        assert store.capabilities[connection_id]["server_name"] == "new-target"

    def test_a_secret_write_is_fenced_by_the_epoch_the_caller_read(self) -> None:
        """A write that started before a reconnect must not land on the new one."""
        store = FakeStore()
        connection_id = _seed(store)
        with _client(store) as client:
            stale = client.post(
                f"/v1/agent/mcp/connections/{connection_id}/secret",
                json={
                    "expected_authorization_epoch": 1,
                    "secret": "new",
                    "replaces_identity": False,
                },
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


class TestTheDynamicClientIdentity:
    """What the callback does with the client the flow authorized as.

    A dynamic registration is decided while the browser is being sent away, so
    until the code comes back the client id exists only in the flow row. A
    connection that stops there holds a token it cannot renew: the first refresh
    presents an empty client id, and the authorization server refuses a grant it
    never issued to "nobody". The identity is therefore written with the
    credential it belongs to, under the same fence.
    """

    _STATE = "the-authorization-state"

    @pytest.mark.parametrize(
        "web_base", ["https://agents.example.com", "https://example.com/agents/", ""]
    )
    @pytest.mark.parametrize("ok", [True, False])
    def test_callback_returns_to_the_web_integrations_page(self, monkeypatch, web_base, ok):
        monkeypatch.setenv("AGENT_WEB_BASE_URL", web_base)
        monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", "https://api.example.com/agents/api")
        message = "authorization denied & retry allowed" if not ok else ""
        response = mcp_connections._callback_redirect("3", ok=ok, message=message)
        target = response.headers["location"]
        assert target.split("?")[0] == f"{web_base.rstrip('/')}/integrations"
        query = parse_qs(urlsplit(target).query)
        assert query["mcp_authorized"] == ["1" if ok else "0"]
        assert query["mcp_connection"] == ["3"]
        if not ok:
            assert query["mcp_message"] == [message]

    def _flow(
        self,
        *,
        connection_id: int,
        client_id: str,
        secret_ciphertext: str | None,
    ) -> dict[str, Any]:
        """One pending flow, as ``consume_mcp_oauth_flow`` returns it."""
        from cloud_agent.mcp.registry import oauth_cipher

        return {
            "connection_id": str(connection_id),
            "config_revision": 1,
            "authorization_epoch": 1,
            "oauth_generation": 1,
            "code_verifier_ciphertext": oauth_cipher(_ENCRYPTION_KEY).encrypt("verifier"),
            "client_id": client_id,
            "client_secret_ciphertext": secret_ciphertext,
            "client_auth_method": "client_secret_basic" if secret_ciphertext else "none",
            "registration": "dcr",
            "issuer": "https://auth.example.com",
            "resource": "https://mcp.example.com/mcp",
            "redirect_uri": "https://app.example.com/v1/agent/mcp/oauth/callback/1",
            "scopes": ["read"],
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "revocation_endpoint": None,
            "registration_endpoint": "https://auth.example.com/register",
        }

    async def _complete(
        self, store: FakeStore, connection_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> str:
        """Drive the real callback with a stub token exchange, and return its target."""
        from cloud_agent.mcp import oauth

        store.oauth_state_hash = oauth.state_hash(self._STATE)

        async def token_exchange(request: Any, *, policy: Any) -> dict[str, Any]:
            return {
                "access_token": "dynamic-access",
                "refresh_token": "dynamic-refresh",
                "expires_in": 3600,
            }

        monkeypatch.setattr(mcp_connections, "_post_form", token_exchange)
        response = await mcp_connections.oauth_callback(
            str(connection_id),
            Request({"type": "http"}),
            state=self._STATE,
            code="the-code",
            error="",
            store=store,
        )
        return str(response.headers["location"])

    async def test_a_registered_client_is_stored_with_the_credential_it_obtained(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the first refresh uses it, which is the point of storing it."""
        from cloud_agent.mcp import refresh
        from cloud_agent.mcp.registry import mcp_cipher

        store = FakeStore()
        connection_id = _seed(store, auth_kind="oauth", auth_config={}, oauth_generation=1)
        # The generation a started flow leaves on the row: starting one moves
        # it, and the flow carries the value it moved to.
        store.oauth_flow = self._flow(
            connection_id=connection_id,
            client_id="review-dynamic-client",
            secret_ciphertext=None,
        )
        assert "mcp_authorized=1" in await self._complete(store, connection_id, monkeypatch)

        stored = store.connections[connection_id]
        assert stored["auth_config"]["client_id"] == "review-dynamic-client"
        assert stored["registration"] == "dcr"
        # The loop the finding was about: a refresh built from the row the
        # callback left behind names the client the registration produced.
        request = refresh._build_request(stored, cipher=mcp_cipher(_ENCRYPTION_KEY))
        assert request.client.client_id == "review-dynamic-client"
        assert request.client.auth_method == "none"

    async def test_an_authorization_that_returned_a_refresh_token_is_not_marked_rotating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whether the issuer rotates is learned from a refresh, not assumed here.

        Assuming it from the presence of a refresh token made every issuer look
        like a rotating one, so the first refresh response that omitted the
        field -- which RFC 6749 §6 allows -- was reported as a lost rotation and
        forced the owner to reconnect a working connection.
        """
        store = FakeStore()
        connection_id = _seed(store, auth_kind="oauth", auth_config={}, oauth_generation=1)
        store.oauth_flow = self._flow(
            connection_id=connection_id,
            client_id="review-dynamic-client",
            secret_ciphertext=None,
        )
        assert "mcp_authorized=1" in await self._complete(store, connection_id, monkeypatch)

        assert store.credential_writes[-1]["refresh_rotates"] is False

    async def test_a_registered_secret_travels_with_the_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A confidential dynamic client keeps its secret under the OAuth domain."""
        from cloud_agent.mcp import refresh
        from cloud_agent.mcp.registry import mcp_cipher, oauth_cipher

        secret_ciphertext = oauth_cipher(_ENCRYPTION_KEY).encrypt("dynamic-secret")
        store = FakeStore()
        connection_id = _seed(store, auth_kind="oauth", auth_config={}, oauth_generation=1)
        # The generation a started flow leaves on the row: starting one moves
        # it, and the flow carries the value it moved to.
        store.oauth_flow = self._flow(
            connection_id=connection_id,
            client_id="review-confidential-client",
            secret_ciphertext=secret_ciphertext,
        )
        assert "mcp_authorized=1" in await self._complete(store, connection_id, monkeypatch)

        stored = store.connections[connection_id]
        assert stored["client_secret_ciphertext"] == secret_ciphertext
        request = refresh._build_request(stored, cipher=mcp_cipher(_ENCRYPTION_KEY))
        assert request.client.client_id == "review-confidential-client"
        assert request.client.client_secret == "dynamic-secret"
        assert request.client.auth_method == "client_secret_basic"

    async def test_callback_cannot_overwrite_a_test_of_its_new_credential(self, monkeypatch):
        """Discovery can finish after the token commit but before callback return."""
        store = FakeStore()
        connection_id = _seed(store, auth_kind="oauth", oauth_generation=1)
        store.oauth_flow = self._flow(
            connection_id=connection_id, client_id="client", secret_ciphertext=None
        )
        write_credential = store.set_mcp_credential

        async def write_then_test(**kwargs):
            written = await write_credential(**kwargs)
            assert written
            await store.set_mcp_connection_status(connection_id=connection_id, status="active")
            return written

        monkeypatch.setattr(store, "set_mcp_credential", write_then_test)
        assert "mcp_authorized=1" in await self._complete(store, connection_id, monkeypatch)
        assert store.connections[connection_id]["status"] == "active"


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

    @pytest.mark.parametrize("scheme", ["Bearer", "Basic", ""])
    def test_saving_keeps_the_imported_authorization_scheme(self, scheme: str) -> None:
        """Saving must send the same Authorization header shown in the paste."""
        store = FakeStore()
        header = f"{scheme} private-token".strip()
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={
                    "text": json.dumps(
                        {
                            "mcpServers": {
                                "wiki": {
                                    "url": "https://wiki.example.com/mcp",
                                    "headers": {"Authorization": header},
                                }
                            }
                        }
                    ),
                    "save": True,
                },
            )
        assert response.status_code == 200
        stored = store.connections[1]
        assert stored["auth_config"]["scheme"] == scheme
        assert mcp_cipher().decrypt(stored["access_token_ciphertext"]) == "private-token"
        assert "private-token" not in response.text

    def test_missing_credentials_are_scoped_to_each_imported_server(self) -> None:
        """Two services using Authorization still need two separate tokens."""
        store = FakeStore()
        servers = {
            name: {
                "url": f"https://{name}.example.com/mcp",
                "headers": {"Authorization": "Bearer ${env:API_KEY}"},
            }
            for name in ("wiki", "tracker")
        }
        servers["public"] = {"url": "https://public.example.com/mcp"}
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={
                    "text": json.dumps({"mcpServers": servers}),
                    "save": True,
                    "env_secrets": {"UNRELATED": "do-not-use"},
                    "server_secrets": {
                        "wiki": {"Authorization": "wiki-token"},
                        "tracker": {"Authorization": "tracker-token"},
                    },
                },
            )
        assert response.status_code == 200
        for cid, token in ((1, "wiki-token"), (2, "tracker-token")):
            stored = store.connections[cid]
            assert mcp_cipher().decrypt(stored["access_token_ciphertext"]) == token
            assert stored["auth_config"]["scheme"] == "Bearer"
            assert token not in response.text
        assert store.connections[3]["access_token_ciphertext"] is None


class TestBindings:
    """A session's capability selection."""

    @pytest.mark.parametrize("duplicate", ["alias", "connection"])
    def test_duplicate_binding_identities_are_rejected_without_writing(self, duplicate) -> None:
        store = FakeStore()
        first, second = _seed(store), _seed(store)
        self._with_capabilities(store, first)
        self._with_capabilities(store, second)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            response = client.put(
                "/v1/agent/mcp/threads/thread-1/bindings",
                json={
                    "bindings": [
                        {"connection_id": first, "alias": "docs", "tools": {"search": "allow"}},
                        {
                            "connection_id": second if duplicate == "alias" else first,
                            "alias": "docs" if duplicate == "alias" else "other",
                            "tools": {"search": "allow"},
                        },
                    ]
                },
            )
        assert response.status_code == 400
        assert "thread-1" not in store.bindings

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
                    "bindings": [{"connection_id": connection_id, "tools": {"delete_all": "allow"}}]
                },
            )
        assert response.status_code == 400
        assert "delete_all" in response.json()["detail"]["error"]["message"]

    def test_an_empty_selection_does_not_enable_the_connection(self) -> None:
        """ "I chose nothing" and "I chose everything" must not be one row."""
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

    def test_an_alias_that_cannot_be_a_url_segment_is_refused(self) -> None:
        """The alias is a relay path segment and a tool-name prefix at once.

        A slash would produce a path the relay cannot match, and anything
        outside the grammar would be mangled into a tool name the model cannot
        call -- both of which read to the agent as a broken server.
        """
        store = FakeStore()
        connection_id = _seed(store)
        self._with_capabilities(store, connection_id)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        for alias in ("../etc", "docs/api", "with space", "-leading", "x" * 41):
            with _client(store) as client:
                response = client.put(
                    "/v1/agent/mcp/threads/thread-1/bindings",
                    json={
                        "bindings": [
                            {
                                "connection_id": connection_id,
                                "alias": alias,
                                "tools": {"search": "allow"},
                            }
                        ]
                    },
                )
            assert response.status_code == 400, alias

    def test_an_alias_is_derived_when_the_caller_gives_none(self) -> None:
        """A default that is valid by construction, so the common case is silent."""
        store = FakeStore()
        connection_id = _seed(store, template="docs")
        self._with_capabilities(store, connection_id)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            response = client.put(
                "/v1/agent/mcp/threads/thread-1/bindings",
                json={"bindings": [{"connection_id": connection_id, "tools": {"search": "allow"}}]},
            )
        assert response.status_code == 200
        assert store.bindings["thread-1"][0]["alias"] == "docs"

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


class TestConnectors:
    """Registering the user's own machine, and what may be routed to it."""

    def _stdio(self, store: FakeStore) -> int:
        """One connection that runs on the user's machine."""
        return _seed(
            store,
            transport="stdio",
            location="user-connector",
            url=None,
            launch_spec={"command": "npx", "args": ["server@1.0.0"], "env": {}, "mounts": []},
            template=None,
        )

    def test_registering_shows_the_secret_once_and_never_again(self) -> None:
        """A registration secret that can be read back is a second copy of it."""
        store = FakeStore()
        with _client(store) as client:
            created = client.post("/v1/agent/mcp/connectors", json={"name": "My laptop"})
            assert created.status_code == 201
            secret = created.json()["registration_secret"]
            assert secret
            listed = client.get("/v1/agent/mcp/connectors").text
        assert secret not in listed
        # Stored hashed: the row holds a digest, not the value.
        assert store.connectors[1]["registration_hash"] != secret
        assert "registration_hash" not in listed

    def test_a_connector_carries_the_resources_its_owner_named(self) -> None:
        """Advisory on the platform side; the connector enforces its own."""
        store = FakeStore()
        with _client(store) as client:
            client.post(
                "/v1/agent/mcp/connectors",
                json={"name": "Laptop", "mounts": ["/Users/me/data"]},
            )
            body = client.get("/v1/agent/mcp/connectors").json()
        assert body[0]["mounts"] == ["/Users/me/data"]
        assert body[0]["status"] == "pending"
        # And it says plainly that nothing serves it yet.
        assert body[0]["runtime"] == "unavailable"

    def test_only_a_local_connection_can_be_routed_to_a_connector(self) -> None:
        """A server the platform can reach directly is not sent the long way."""
        store = FakeStore()
        remote = _seed(store)
        with _client(store) as client:
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"})
            response = client.post(
                "/v1/agent/mcp/connectors/1/connections", json={"connection_id": remote}
            )
        assert response.status_code == 400
        assert "own machine" in response.json()["detail"]["error"]["message"]

    def test_binding_records_the_route_from_both_directions(self) -> None:
        """The connection names the connector, and the connector names it back.

        Divergence between the two is the failure that matters: a connector told
        it may serve something the connection does not route to (or the reverse)
        is one of the two halves deciding on its own.
        """
        store = FakeStore()
        connection = self._stdio(store)
        with _client(store) as client:
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"})
            response = client.post(
                "/v1/agent/mcp/connectors/1/connections", json={"connection_id": connection}
            )
        assert response.status_code == 200
        assert response.json()["connection_ids"] == [connection]
        assert store.connections[connection]["connector_id"] == 1
        # Routing a local process somewhere is a change of target, so the
        # versions a decision is fenced on move with it.
        assert store.connections[connection]["config_revision"] == 2
        assert store.connections[connection]["authorization_epoch"] == 2

    def test_revoking_detaches_everything_that_routed_through_it(self) -> None:
        """One decision, so one transaction: a revoked connector serves nothing."""
        store = FakeStore()
        connection = self._stdio(store)
        with _client(store) as client:
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"})
            client.post(
                "/v1/agent/mcp/connectors/1/connections", json={"connection_id": connection}
            )
            assert client.delete("/v1/agent/mcp/connectors/1").status_code == 204
            body = client.get("/v1/agent/mcp/connectors").json()
        assert body[0]["status"] == "revoked"
        assert body[0]["connection_ids"] == []
        assert store.connections[connection]["connector_id"] is None

    def test_a_revoked_connector_cannot_be_bound_to_again(self) -> None:
        """Revocation is not a status the caller can route around."""
        store = FakeStore()
        connection = self._stdio(store)
        with _client(store) as client:
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"})
            client.delete("/v1/agent/mcp/connectors/1")
            response = client.post(
                "/v1/agent/mcp/connectors/1/connections", json={"connection_id": connection}
            )
        assert response.status_code == 409

    def test_another_users_connector_is_not_found(self) -> None:
        """Ownership is part of the read, not a check after it."""
        store = FakeStore()
        connection = self._stdio(store)
        store.connectors[9] = {
            "id": 9,
            "user_id": _OTHER,
            "name": "Theirs",
            "status": "pending",
            "mounts": [],
            "created_at": None,
            "last_seen_at": None,
            "revoked_at": None,
        }
        with _client(store) as client:
            assert client.delete("/v1/agent/mcp/connectors/9").status_code == 404
            assert (
                client.post(
                    "/v1/agent/mcp/connectors/9/connections", json={"connection_id": connection}
                ).status_code
                == 404
            )

    def test_unbinding_a_connection_that_is_not_routed_here_is_not_found(self) -> None:
        """Silence would let a caller believe it had stopped something."""
        store = FakeStore()
        connection = self._stdio(store)
        with _client(store) as client:
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"})
            response = client.delete(f"/v1/agent/mcp/connectors/1/connections/{connection}")
        assert response.status_code == 404


class TestMcpAvailableOnDeployment:
    """A normal deployment immediately supports connections and bindings."""

    @pytest.mark.parametrize("legacy_value", ["0", "off", ""])
    def test_no_rollout_flag_is_needed(self, monkeypatch, legacy_value):
        monkeypatch.setenv("AGENT_MCP_ENABLED", legacy_value)
        store = FakeStore()
        connection = _seed(store)
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/connections/{connection}/secret",
                json={"expected_authorization_epoch": 1, "secret": "test-token"},
            )
        assert response.status_code == 200


class TestPerServiceTimeouts:
    """One service's patience is not every service's limit."""

    def test_a_timeout_round_trips_and_has_bounds(self) -> None:
        """A bound that cannot be set is a constant with extra steps."""
        store = FakeStore()
        with _client(store) as client:
            created = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Slow search",
                    "transport": "streamable-http",
                    "location": "remote",
                    "url": "https://mcp.example.com/mcp",
                    "auth_kind": "none",
                    "request_timeout_s": 45,
                },
            )
            assert created.status_code == 201, created.text
            assert created.json()["request_timeout_s"] == 45

            listed = client.get("/v1/agent/mcp/connections").json()
            assert listed["connections"][0]["request_timeout_s"] == 45

            # Below the floor and above the ceiling are both refused: a five
            # second bound fails every real server, and a day-long one holds a
            # job open past any attempt.
            for value in (1, 3600):
                refused = client.post(
                    "/v1/agent/mcp/connections",
                    json={
                        "label": "No",
                        "transport": "streamable-http",
                        "location": "remote",
                        "url": "https://mcp.example.com/mcp",
                        "auth_kind": "none",
                        "request_timeout_s": value,
                    },
                )
                assert refused.status_code == 422, value

    def test_omitting_it_leaves_the_default(self) -> None:
        """Which is what every connection had before this was a choice."""
        store = FakeStore()
        with _client(store) as client:
            created = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Ordinary",
                    "transport": "streamable-http",
                    "location": "remote",
                    "url": "https://mcp.example.com/mcp",
                    "auth_kind": "none",
                },
            )
            assert created.status_code == 201, created.text
            assert created.json()["request_timeout_s"] is None


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


class _CountingBody(httpx.AsyncByteStream):
    """A reply body that records how much of itself was read."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count
        self.pulled = 0

    async def __aiter__(self) -> Any:
        for _ in range(self._count):
            self.pulled += 1
            yield self._chunk

    async def aclose(self) -> None:
        """Nothing to release; the bytes are in memory already."""


def _answer_with(monkeypatch: pytest.MonkeyPatch, stream: Any) -> None:
    """Make the next ``httpx.AsyncClient`` answer with ``stream`` as its body."""
    import httpx

    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> Any:
        kwargs.pop("transport", None)
        return real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=stream
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class TestTheOAuthPostsAreBounded:
    """The two posts whose replies this service reads and keeps itself.

    Registration returns a client secret and the token endpoint returns the
    access and refresh tokens. Both are served by the authorization server --
    the one input in the OAuth path the user did not choose -- so the size of
    the body has to be decided here, while it arrives, rather than after it is
    already in memory.
    """

    async def test_a_registration_reply_is_refused_while_it_is_still_arriving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from cloud_agent.mcp import transports as transports_module
        from cloud_agent.mcp.net_policy import EgressPolicy

        monkeypatch.setattr(transports_module, "MAX_RESPONSE_BYTES", 1024)
        stream = _CountingBody(b"x" * 256, 64)
        _answer_with(monkeypatch, stream)

        with pytest.raises(HTTPException):
            await mcp_connections._post_json(
                None,
                "https://127.0.0.1:9/register",
                {"client_name": "cloud-agent"},
                policy=EgressPolicy(allow_private=True),
            )
        assert stream.pulled < 64, "the whole registration reply was read before the limit applied"

    async def test_a_token_reply_is_refused_while_it_is_still_arriving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_agent.mcp import transports as transports_module
        from cloud_agent.mcp.net_policy import EgressPolicy
        from cloud_agent.mcp.transports import TransportError

        monkeypatch.setattr(transports_module, "MAX_RESPONSE_BYTES", 1024)
        stream = _CountingBody(b"x" * 256, 64)
        _answer_with(monkeypatch, stream)

        class _Request:
            """The parts of a token request ``_post_form`` reads."""

            url = "https://127.0.0.1:9/token"

            def form(self) -> dict[str, str]:
                return {"grant_type": "authorization_code", "code": "the-code"}

            def headers(self) -> dict[str, str]:
                return {"content-type": "application/x-www-form-urlencoded"}

        with pytest.raises(TransportError):
            await mcp_connections._post_form(_Request(), policy=EgressPolicy(allow_private=True))
        assert stream.pulled < 64, "the whole token reply was read before the limit applied"
