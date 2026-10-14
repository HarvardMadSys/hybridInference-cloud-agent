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
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from tests.support.storage import AllCapabilities, OneFake

from cloud_agent.api import mcp_connections
from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp.errors import (
    McpConnectionCreateConflict,
    McpCredentialWriteRejected,
)
from cloud_agent.mcp.models import CapabilityKind, ToolPolicy
from cloud_agent.mcp.registry import mcp_cipher

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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
        self.jobs: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.connectors: dict[int, dict[str, Any]] = {}
        self.connector_connections: dict[int, set[int]] = {}
        #: One pending OAuth flow, keyed by the state hash the callback presents.
        self.oauth_flow: dict[str, Any] | None = None
        self.oauth_state_hash: str | None = None
        self.credential_writes: list[dict[str, Any]] = []
        self.reject_credential_writes = False
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
        connector_id = kwargs.get("connector_id")
        if connector_id is not None and kwargs["location"] != "user-connector":
            raise McpConnectionCreateConflict(
                "a connector can only serve a user-connector connection"
            )
        if connector_id is not None:
            connector = self.connectors.get(connector_id)
            if (
                connector is None
                or connector["user_id"] != kwargs["user_id"]
                or connector["status"] == "revoked"
            ):
                raise McpConnectionCreateConflict("that connector is no longer available")
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
        if connector_id is not None:
            self.connector_connections.setdefault(connector_id, set()).add(connection_id)
        try:
            if self.reject_credential_writes and kwargs.get("access_token_ciphertext") is not None:
                raise RuntimeError("injected credential write rejection")
            if kwargs.get("access_token_ciphertext") is not None:
                written = await self.set_mcp_credential(
                    connection_id=connection_id,
                    expected_authorization_epoch=row["authorization_epoch"],
                    access_token_ciphertext=kwargs["access_token_ciphertext"],
                    auth_config_patch=kwargs.get("auth_config_patch"),
                )
                if not written:
                    raise McpCredentialWriteRejected()
            if kwargs.get("sealed_launch_spec") is not None:
                written = await self.set_mcp_launch_spec(
                    connection_id=connection_id,
                    expected_authorization_epoch=row["authorization_epoch"],
                    launch_spec=kwargs["sealed_launch_spec"],
                )
                if not written:
                    raise McpCredentialWriteRejected()
        except McpCredentialWriteRejected:
            self.connections.pop(connection_id, None)
            self.created.pop()
            if connector_id is not None:
                self.connector_connections.get(connector_id, set()).discard(connection_id)
            raise
        return self._row(connection_id)

    async def create_mcp_connections(
        self, *, connections: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Insert every draft, rolling back the whole batch when one fails."""
        before_connections = deepcopy(self.connections)
        before_created = deepcopy(self.created)
        before_connector_connections = deepcopy(self.connector_connections)
        before_next = self._next
        try:
            return [await self.create_mcp_connection(**dict(draft)) for draft in connections]
        except Exception:
            self.connections = before_connections
            self.created = before_created
            self.connector_connections = before_connector_connections
            self._next = before_next
            raise

    async def update_mcp_connection(self, **kwargs: Any) -> dict[str, Any] | None:
        row = self.connections.get(kwargs["connection_id"])
        if row is None or row["config_revision"] != kwargs["expected_config_revision"]:
            return None
        for key in (
            "label",
            "transport",
            "location",
            "url",
            "auth_kind",
            "account_label",
            "request_timeout_s",
        ):
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
        if row["location"] != "user-connector":
            row["connector_id"] = None
        for connector_id, connection_ids in self.connector_connections.items():
            if connector_id != row["connector_id"]:
                connection_ids.discard(kwargs["connection_id"])
        return self._row(kwargs["connection_id"])

    async def disconnect_mcp_connection(self, *, connection_id: int, user_id: str) -> bool:
        row = self.connections.get(connection_id)
        if row is None or row["user_id"] != user_id or row["deleted_at"] is not None:
            return False
        row["deleted_at"] = datetime.now(timezone.utc)
        row["status"] = "disabled"
        row["access_token_ciphertext"] = None
        row["authorization_epoch"] += 1
        row["connector_id"] = None
        for connection_ids in self.connector_connections.values():
            connection_ids.discard(connection_id)
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

    async def consume_mcp_oauth_flow(
        self, *, state_hash: str, user_id: str
    ) -> dict[str, Any] | None:
        """Hand back a flow once, for its state hash and the account that started it."""
        if self.oauth_flow is None or state_hash != self.oauth_state_hash:
            return None
        if self.oauth_flow.get("user_id") != user_id:
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
        previous = (row["auth_config"] or {}).get("client_id")
        row["auth_config"] = {**(row["auth_config"] or {}), "client_id": client_id}
        if client_secret_ciphertext:
            row["client_secret_ciphertext"] = client_secret_ciphertext
        elif previous != client_id:
            row["client_secret_ciphertext"] = None
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
        for members in self.connector_connections.values():
            members.discard(connection_id)
        self.connector_connections.setdefault(connector_id, set()).add(connection_id)
        return self._row(connection_id)

    async def unbind_mcp_connection_connector(
        self, *, connection_id: int, user_id: str, connector_id: int
    ) -> dict[str, Any] | None:
        row = self.connections.get(connection_id)
        if row is None or row["user_id"] != user_id or row.get("connector_id") != connector_id:
            return None
        self.connector_connections.get(connector_id, set()).discard(connection_id)
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

    async def create_mcp_oauth_flow(self, **kwargs: Any) -> int | None:
        """Allocate the next generation and keep a single pending flow."""
        row = self.connections.get(kwargs["connection_id"])
        if row is None or row["user_id"] != kwargs["user_id"] or row["deleted_at"] is not None:
            return None
        if row["config_revision"] != kwargs["config_revision"]:
            return None
        if row["authorization_epoch"] != kwargs["authorization_epoch"]:
            return None
        if row["oauth_generation"] != kwargs["expected_oauth_generation"]:
            return None
        row["oauth_generation"] += 1
        generation = int(row["oauth_generation"])
        self.oauth_flow = {**kwargs, "oauth_generation": generation}
        self.oauth_state_hash = kwargs["state_hash"]
        return generation

    async def delete_mcp_oauth_flows(self, *, connection_id: int) -> int:
        if self.oauth_flow and str(self.oauth_flow.get("connection_id")) == str(connection_id):
            self.oauth_flow = None
            self.oauth_state_hash = None
            return 1
        return 0

    async def list_mcp_bindings(self, *, thread_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for binding in self.bindings.get(thread_id, []):
            row = dict(binding)
            row["label"] = self.connections[binding["connection_id"]]["label"]
            rows.append(row)
        return rows

    async def replace_mcp_bindings(
        self, *, thread_id: str, bindings: list[dict[str, Any]], merge: bool = False
    ) -> None:
        supplied = {item["connection_id"] for item in bindings}
        previous = self.bindings.get(thread_id, [])
        self.bindings[thread_id] = [
            {**binding, "selection_pending": binding.get("selection_pending", False)}
            for binding in bindings
        ] + [
            row if merge else {**row, "enabled": False}
            for row in previous
            if row["connection_id"] not in supplied
        ]

    async def set_mcp_connection_enabled(
        self, *, connection_id: int, user_id: str, enabled: bool
    ) -> dict[str, Any] | None:
        row = self.connections.get(connection_id)
        if row is None or row["user_id"] != user_id or row["deleted_at"] is not None:
            return None
        if row.get("enabled", True) != enabled:
            row["authorization_epoch"] += 1
        row["enabled"] = enabled
        return dict(row)

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        return self.threads.get(thread_id)

    async def list_mcp_invocations(self, **kwargs: Any) -> list[dict[str, Any]]:
        rows = list(self.invocations.values())
        if kwargs.get("user_id") is not None:
            owned = []
            for row in rows:
                job = await self.get_job(row["job_id"])
                if (
                    job is not None
                    and job.get("user_id") == kwargs["user_id"]
                    and job.get("deleted_at") is None
                ):
                    owned.append(row)
            rows = owned
        if kwargs.get("job_id") is not None:
            rows = [row for row in rows if row["job_id"] == kwargs["job_id"]]
        if kwargs.get("thread_id") is not None:
            rows = [row for row in rows if row["thread_id"] == kwargs["thread_id"]]
        if kwargs.get("statuses"):
            rows = [row for row in rows if row["status"] in kwargs["statuses"]]
            rows = [
                row
                for row in rows
                if row["status"] != "pending" or row["expires_at"] > datetime.now(timezone.utc)
            ]
        if kwargs.get("before_id") is not None:
            rows = [row for row in rows if row["id"] < kwargs["before_id"]]
        return sorted(rows, key=lambda row: row["id"], reverse=True)[: kwargs.get("limit", 50)]

    async def get_mcp_invocation(self, *, invocation_id: int) -> dict[str, Any] | None:
        return self.invocations.get(invocation_id)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id, {"id": job_id, "user_id": _OWNER, "thread_id": "thread-1"})

    async def decide_mcp_invocation(
        self,
        *,
        invocation_id: int,
        approve: bool,
        decided_by: str,
        always_allow_rule: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = self.invocations.get(invocation_id)
        if row is None or row["status"] != "pending":
            return None
        row["status"] = "approved" if approve else "denied"
        row["decided_by"] = decided_by
        row["always_allow_rule"] = always_allow_rule
        row["always_allow_status"] = "pending" if always_allow_rule else None
        return dict(row)


def _client(store: FakeStore, *, user_id: str = _OWNER) -> TestClient:
    """Build a client whose owner guard is satisfied by ``user_id``.

    The OAuth callback reads the session itself rather than through the owner
    guard, so it is answered as ``user_id`` too: the browser returning from a
    provider is the one that started the flow.
    """
    app = FastAPI()
    # Management alone, as ``create_app`` mounts it: the relay's tool routes
    # are served by the MCP-only listener and nowhere else.
    app.include_router(mcp_connections.router)
    app.dependency_overrides[get_storage] = lambda: OneFake(store)
    app.dependency_overrides[require_agent_owner] = lambda: {"user_id": user_id}
    app.dependency_overrides[mcp_connections._callback_session_user] = lambda: user_id
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

    def test_management_is_answered_by_management(self) -> None:
        """Connection management keeps working under the relay's prefix.

        Both surfaces are under ``/v1/agent/mcp``, but they are never mounted
        on the same application any more, so neither can shadow the other.
        """
        store = FakeStore()
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/connections")
        assert response.status_code == 200
        assert "connections" in response.json()

    def test_a_binding_alias_is_not_a_route_here(self) -> None:
        """A tool call finds nothing on the owner-facing application.

        Not a 401: a refusal would mean the relay is mounted and deciding. The
        tool surface lives on the MCP-only listener, and
        ``test_mcp_listener_isolation`` checks the real applications.
        """
        store = FakeStore()
        with _client(store) as client:
            response = client.post("/v1/agent/mcp/docs", json={})
        assert response.status_code == 404


class TestConnectionManagement:
    """Adding, listing, editing and disconnecting a connection."""

    @pytest.mark.parametrize("importing", [False, True])
    def test_retry_resumes_oauth_without_resetting_the_connection(self, monkeypatch, importing):
        store = FakeStore()
        connection_id = _seed(
            store, auth_kind="oauth", access_token_ciphertext=None, status="error"
        )
        existing = store._row(connection_id)
        create = AsyncMock(return_value=existing)
        monkeypatch.setattr(store, "create_mcp_connection", create)
        with _client(store) as client:
            if importing:
                response = client.post(
                    "/v1/agent/mcp/connections/import",
                    json={
                        "save": True,
                        "text": json.dumps({"mcpServers": {"GitHub": {"url": existing["url"]}}}),
                    },
                )
                result = response.json()["saved"][0]
            else:
                response = client.post(
                    "/v1/agent/mcp/connections",
                    json={"label": "GitHub", "url": existing["url"], "auth_kind": "none"},
                )
                result = response.json()
        assert response.is_success
        assert create.await_args.kwargs["reuse_pending"] is True
        assert result["id"] == connection_id
        assert result["auth_kind"] == "oauth"
        assert store.connections[connection_id] == existing
        assert store.credential_writes == []

    @pytest.mark.parametrize("importing", [False, True])
    def test_saving_credentials_never_reuses_another_connection(self, monkeypatch, importing):
        store = FakeStore()
        create = AsyncMock(wraps=store.create_mcp_connection)
        monkeypatch.setattr(store, "create_mcp_connection", create)
        with _client(store) as client:
            if importing:
                response = client.post(
                    "/v1/agent/mcp/connections/import",
                    json={
                        "save": True,
                        "text": json.dumps(
                            {
                                "mcpServers": {
                                    "GitHub": {
                                        "url": "https://mcp.example.com/mcp",
                                        "headers": {"Authorization": "Bearer new-account-token"},
                                    }
                                }
                            }
                        ),
                    },
                )
            else:
                response = client.post(
                    "/v1/agent/mcp/connections",
                    json={
                        "label": "GitHub",
                        "url": "https://mcp.example.com/mcp",
                        "auth_kind": "static",
                        "secret": "new-account-token",
                    },
                )
        assert response.is_success
        assert create.await_args.kwargs["reuse_pending"] is False

    def test_a_missing_encryption_key_leaves_no_connection(self, monkeypatch) -> None:
        """The row used to commit before encryption, so a 503 still created it."""
        monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
        store = FakeStore()
        with _client(store) as client:
            first = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "GitHub",
                    "url": "https://mcp.example.com/mcp",
                    "auth_kind": "static",
                    "secret": "github-test-token",
                },
            )
            second = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "GitHub",
                    "url": "https://mcp.example.com/mcp",
                    "auth_kind": "static",
                    "secret": "github-test-token",
                },
            )
        assert first.status_code == 503
        assert second.status_code == 503
        assert store.connections == {}
        assert store.created == []

    def test_a_rejected_credential_write_leaves_no_connection(self, monkeypatch) -> None:
        """Retrying a refused secret write must not pile up credential-less rows."""
        store = FakeStore()
        original = store.set_mcp_credential
        fail = True

        async def reject_once(**kwargs: Any) -> bool:
            nonlocal fail
            if fail:
                return False
            return await original(**kwargs)

        monkeypatch.setattr(store, "set_mcp_credential", reject_once)
        payload = {
            "label": "GitHub",
            "url": "https://mcp.example.com/mcp",
            "auth_kind": "static",
            "secret": "github-test-token",
        }
        with _client(store) as client:
            rejected = client.post("/v1/agent/mcp/connections", json=payload)
            assert rejected.status_code == 409
            assert store.connections == {}
            fail = False
            created = client.post("/v1/agent/mcp/connections", json=payload)
        assert created.status_code == 201
        assert list(store.connections) == [created.json()["id"]]
        stored = store.connections[created.json()["id"]]
        assert mcp_cipher(_ENCRYPTION_KEY).decrypt(stored["access_token_ciphertext"]) == (
            "github-test-token"
        )

    def test_a_rejected_env_secret_write_leaves_no_connection(self, monkeypatch) -> None:
        """A stdio secret is part of the same insert as the connection."""
        store = FakeStore()

        async def reject(**kwargs: Any) -> bool:
            del kwargs
            return False

        monkeypatch.setattr(store, "set_mcp_launch_spec", reject)
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Local",
                    "transport": "stdio",
                    "location": "user-connector",
                    "auth_kind": "none",
                    "launch": {
                        "command": "npx",
                        "args": ["server@1.0.0"],
                        "env": {"API_KEY": "secret://API_KEY"},
                    },
                    "env_secrets": {"API_KEY": "literal"},
                },
            )
        assert response.status_code == 409
        assert store.connections == {}

    def test_connector_revoked_after_preflight_returns_conflict_without_a_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The repository fence closes the create route's connector TOCTOU."""
        store = FakeStore()
        original_create = store.create_mcp_connection
        with _client(store) as client:
            connector = client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"}).json()[
                "connector"
            ]

            async def revoke_then_create(**kwargs: Any) -> dict[str, Any]:
                store.connectors[connector["id"]]["status"] = "revoked"
                return await original_create(**kwargs)

            monkeypatch.setattr(store, "create_mcp_connection", revoke_then_create)
            response = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Local",
                    "transport": "stdio",
                    "location": "user-connector",
                    "launch": {
                        "command": "npx",
                        "args": ["-y", "@scope/server@1.0.0"],
                    },
                    "connector_id": connector["id"],
                },
            )

        assert response.status_code == 409
        assert store.connections == {}
        assert store.created == []

    def test_direct_connector_create_records_both_sides_of_the_route(self) -> None:
        """Creation publishes the route and connector authority as one decision."""
        store = FakeStore()
        with _client(store) as client:
            connector = client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"}).json()[
                "connector"
            ]
            created = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Local",
                    "transport": "stdio",
                    "location": "user-connector",
                    "launch": {
                        "command": "npx",
                        "args": ["-y", "@scope/server@1.0.0"],
                    },
                    "connector_id": connector["id"],
                },
            )

        assert created.status_code == 201
        connection_id = created.json()["id"]
        assert store.connections[connection_id]["connector_id"] == connector["id"]
        assert store.connector_connections[connector["id"]] == {connection_id}

    def test_direct_connector_create_rejects_a_non_connector_location(self) -> None:
        """A remote target cannot carry authority for a user-run connector."""
        store = FakeStore()
        with _client(store) as client:
            connector = client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"}).json()[
                "connector"
            ]
            response = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Remote",
                    "url": "https://mcp.example.com/mcp",
                    "connector_id": connector["id"],
                },
            )

        assert response.status_code == 400
        assert "only valid" in response.json()["detail"]["error"]["message"]
        assert store.connections == {}
        assert store.connector_connections == {}

    def test_templates_are_offered_but_decide_nothing(self) -> None:
        """A template prefills a form; it is not an eligibility list."""
        with _client(FakeStore()) as client:
            body = client.get("/v1/agent/mcp/templates").json()
        assert body and {"name", "url", "transport"} <= set(body[0])

    @pytest.mark.parametrize(
        "name,url,auth_kind,documentation_url",
        [
            (
                "github",
                "https://api.githubcopilot.com/mcp/",
                "static",
                "https://github.com/github/github-mcp-server",
            ),
            (
                "slack",
                "https://mcp.slack.com/mcp",
                "oauth",
                "https://docs.slack.dev/ai/slack-mcp-server/",
            ),
            (
                "google-drive",
                "https://drivemcp.googleapis.com/mcp/v1",
                "oauth",
                "https://developers.google.com/workspace/drive/api/guides/configure-mcp-server",
            ),
        ],
    )
    def test_common_service_presets_can_be_added_without_the_registry(
        self, name, url, auth_kind, documentation_url
    ) -> None:
        """Known services carry an actionable recipe even before a directory sync."""
        store = FakeStore()
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/connections")
            assert response.status_code == 200
            templates = {item["name"]: item for item in response.json()["templates"]}
            assert {"linear", "notion", "context7"} <= templates.keys()
            template = templates[name]
            assert template["url"] == url
            assert template["auth_kind"] == auth_kind
            assert template["documentation_url"] == documentation_url
            assert template["notes"]
            created = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": template["label"],
                    "url": template["url"],
                    "transport": template["transport"],
                    "auth_kind": template["auth_kind"],
                    "template": template["name"],
                    **({"secret": "github-test-token"} if auth_kind == "static" else {}),
                },
            )
        assert created.status_code == 201, created.text
        assert created.json()["template"] == name
        assert created.json()["auth_kind"] == auth_kind
        assert "github-test-token" not in created.text
        row = store.connections[created.json()["id"]]
        if auth_kind == "static":
            assert mcp_cipher(_ENCRYPTION_KEY).decrypt(row["access_token_ciphertext"]) == (
                "github-test-token"
            )

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

    @pytest.mark.parametrize(
        "location,launch,reason",
        [
            ("managed-worker", {"command": "npx", "args": ["-y", "@scope/server"]}, "pin"),
            ("user-connector", {"command": "sh", "args": ["-c", "server"]}, "shell"),
            ("user-connector", {"args": ["server"]}, "command"),
        ],
    )
    def test_an_edited_launch_is_checked_like_a_new_one(self, location, launch, reason) -> None:
        """A PATCH is not a way around the launch policy creation enforces."""
        store = FakeStore()
        stored = {"command": "uvx", "args": ["server==1.0"], "env": {}, "cwd": None, "mounts": []}
        connection_id = _seed(
            store,
            transport="stdio",
            location=location,
            url=None,
            auth_kind="none",
            launch_spec=dict(stored),
        )
        with _client(store) as client:
            response = client.patch(
                f"/v1/agent/mcp/connections/{connection_id}",
                json={"expected_config_revision": 1, "launch": launch},
            )
        assert response.status_code == 400
        assert reason in response.json()["detail"]["error"]["message"]
        assert store.connections[connection_id]["launch_spec"] == stored
        assert store.connections[connection_id]["config_revision"] == 1

    def test_an_edited_launch_is_stored_as_validated_not_as_sent(self) -> None:
        """Mounts and stored secrets are not the edit's to supply."""
        store = FakeStore()
        connection_id = _seed(
            store,
            transport="stdio",
            location="user-connector",
            url=None,
            auth_kind="none",
            launch_spec={"command": "uvx", "args": ["server==1.0"], "env": {}, "mounts": []},
        )
        with _client(store) as client:
            response = client.patch(
                f"/v1/agent/mcp/connections/{connection_id}",
                json={
                    "expected_config_revision": 1,
                    "launch": {
                        "command": "uvx",
                        "args": ["server==2.0"],
                        "env": {"TOKEN": "secret://TOKEN"},
                        "mounts": [{"source": "/", "target": "host", "read_only": False}],
                        "env_secrets": {"TOKEN": "supplied-ciphertext"},
                    },
                },
            )
        assert response.status_code == 200, response.text
        assert store.connections[connection_id]["launch_spec"] == {
            "command": "uvx",
            "args": ["server==2.0"],
            "env": {"TOKEN": "secret://TOKEN"},
            "cwd": None,
            "mounts": [],
        }

    def test_a_remote_connection_cannot_be_given_a_launch(self) -> None:
        """A process spec on a URL connection is refused, not stored beside it."""
        store = FakeStore()
        connection_id = _seed(store)
        with _client(store) as client:
            response = client.patch(
                f"/v1/agent/mcp/connections/{connection_id}",
                json={"expected_config_revision": 1, "launch": {"command": "uvx"}},
            )
        assert response.status_code == 400
        assert store.connections[connection_id]["launch_spec"] is None

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

    @pytest.mark.parametrize(
        "endpoint",
        [
            "javascript:alert(document.domain)",
            "data:text/html,<script>alert(1)</script>",
            "/authorize",
            "//login.example.com/authorize",
            "https://user@login.example.com/authorize",
        ],
    )
    async def test_unsafe_redirect_is_refused_before_the_flow_is_persisted(
        self, monkeypatch: pytest.MonkeyPatch, endpoint: str
    ) -> None:
        """The API also guards metadata loaded without its discovery parser."""
        from fastapi import HTTPException

        from cloud_agent.mcp import oauth

        monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", "https://api.example.com")
        metadata = oauth.AuthorizationServerMetadata(
            issuer="https://auth.example.com",
            authorization_endpoint=endpoint,
            token_endpoint="https://auth.example.com/token",
        )
        monkeypatch.setattr(
            oauth, "discover_authorization_server", AsyncMock(return_value=(None, metadata))
        )
        store = FakeStore()
        connection_id = _seed(
            store, auth_kind="oauth", auth_config={"client_id": "registered-client"}
        )
        persist = AsyncMock()
        monkeypatch.setattr(store, "create_mcp_oauth_flow", persist, raising=False)
        with pytest.raises(HTTPException) as raised:
            await mcp_connections.authorize_connection(
                connection_id=connection_id,
                user={"user_id": _OWNER},
                store=store,
            )
        assert raised.value.status_code == 400
        assert "invalid authorization endpoint" in str(raised.value.detail)
        persist.assert_not_called()

    async def test_a_stale_start_returns_conflict_without_an_authorization_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API exposes a lost version fence instead of returning a dead URL."""
        from fastapi import HTTPException

        from cloud_agent.mcp import oauth

        monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", "https://api.example.com")
        metadata = oauth.AuthorizationServerMetadata(
            issuer="https://auth.example.com",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
        )
        monkeypatch.setattr(
            oauth, "discover_authorization_server", AsyncMock(return_value=(None, metadata))
        )
        store = FakeStore()
        connection_id = _seed(
            store,
            auth_kind="oauth",
            auth_config={"client_id": "registered-client"},
        )
        persist = AsyncMock(return_value=None)
        monkeypatch.setattr(store, "create_mcp_oauth_flow", persist, raising=False)

        with pytest.raises(HTTPException) as raised:
            await mcp_connections.authorize_connection(
                connection_id=connection_id,
                user={"user_id": _OWNER},
                store=store,
            )

        assert raised.value.status_code == 409
        assert "changed" in str(raised.value.detail)
        assert persist.await_args.kwargs["config_revision"] == 1
        assert persist.await_args.kwargs["authorization_epoch"] == 1
        assert persist.await_args.kwargs["expected_oauth_generation"] == 0
        assert "oauth_generation" not in persist.await_args.kwargs

    @pytest.mark.parametrize(
        "session_base,override,expected_base",
        [
            ("https://example.com/agents/api", "", "https://example.com/agents/api"),
            (
                "https://example.com/agents/api",
                "https://mcp.example.com/api/",
                "https://mcp.example.com/api",
            ),
        ],
    )
    def test_registration_required_response_names_the_callback_to_register(
        self, monkeypatch, session_base, override, expected_base
    ) -> None:
        """A user bringing an OAuth app needs this deployment's precise callback."""
        from cloud_agent.mcp import oauth

        monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", override)
        monkeypatch.setenv("AGENT_SESSION_REDIRECT_URI", f"{session_base}/v1/session/callback")
        metadata = oauth.AuthorizationServerMetadata(
            issuer="https://mcp.slack.com",
            authorization_endpoint="https://mcp.slack.com/authorize",
            token_endpoint="https://mcp.slack.com/token",
            token_endpoint_auth_methods_supported=("client_secret_basic",),
        )
        monkeypatch.setattr(
            oauth, "discover_authorization_server", AsyncMock(return_value=(None, metadata))
        )
        store = FakeStore()
        connection_id = _seed(store, url="https://mcp.slack.com/mcp", auth_kind="oauth")
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/connections/{connection_id}/authorize",
                headers={"Host": "untrusted.example", "X-Forwarded-Host": "untrusted.example"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["needs_client_id"] is True
        assert body["authorization_url"] is None
        assert body["redirect_uri"] == (
            f"{expected_base}/v1/agent/mcp/oauth/callback/{connection_id}"
        )
        assert store.oauth_flow is None

    @pytest.mark.parametrize(
        "resource_url,metadata_resource,stored_resource,expected_resource",
        [
            ("https://mcp.slack.com/mcp", "https://mcp.slack.com", None, "https://mcp.slack.com"),
            ("https://mcp.slack.com/mcp", None, "https://mcp.slack.com", "https://mcp.slack.com"),
            (
                "https://drivemcp.googleapis.com/mcp/v1",
                None,
                None,
                "https://drivemcp.googleapis.com/mcp/v1",
            ),
            (
                "https://mcp.example.com/tenant-b",
                None,
                "https://mcp.example.com/tenant-a",
                "https://mcp.example.com/tenant-b",
            ),
        ],
    )
    def test_a_supplied_confidential_client_completes_authorization(
        self, monkeypatch, resource_url, metadata_resource, stored_resource, expected_resource
    ) -> None:
        """Authorization, redemption and refresh preserve the verified OAuth resource."""
        from cloud_agent.mcp import oauth, refresh
        from cloud_agent.mcp.registry import oauth_cipher

        base_url = "https://example.com/agents/api"
        monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", base_url)
        monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://example.com/agents")
        issuer = f"https://{urlsplit(resource_url).hostname}"
        metadata = oauth.AuthorizationServerMetadata(
            issuer=issuer,
            authorization_endpoint=f"{issuer}/authorize",
            token_endpoint=f"{issuer}/token",
            token_endpoint_auth_methods_supported=("client_secret_basic",),
        )
        resource_metadata = (
            oauth.ProtectedResourceMetadata(
                resource=metadata_resource, authorization_servers=(issuer,)
            )
            if metadata_resource
            else None
        )
        discover = AsyncMock(return_value=(resource_metadata, metadata))
        monkeypatch.setattr(oauth, "discover_authorization_server", discover)
        exchange = AsyncMock(
            return_value={
                "access_token": "service-test-token",
                "refresh_token": "test-refresh-token",
            }
        )
        register = AsyncMock()
        monkeypatch.setattr(mcp_connections, "_post_form", exchange)
        monkeypatch.setattr(mcp_connections, "_register_client", register)
        store = FakeStore()
        connection_id = _seed(
            store,
            url=resource_url,
            auth_kind="oauth",
            auth_config={},
            resource=stored_resource,
            issuer=issuer if stored_resource else None,
        )

        async def save_flow(**kwargs):
            generation = int(kwargs["expected_oauth_generation"]) + 1
            store.oauth_flow = {**kwargs, "oauth_generation": generation}
            store.oauth_state_hash = kwargs["state_hash"]
            store.connections[connection_id]["oauth_generation"] = generation
            return generation

        monkeypatch.setattr(store, "create_mcp_oauth_flow", save_flow, raising=False)
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/connections/{connection_id}/authorize",
                json={"client_id": "service-test-client", "client_secret": "service-test-secret"},
            )
            assert response.status_code == 200, response.text
            assert "service-test-secret" not in response.text
            query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
            assert query["client_id"] == ["service-test-client"]
            assert query["resource"] == [expected_resource]
            assert store.oauth_flow["resource"] == expected_resource
            assert query["redirect_uri"] == [
                f"{base_url}/v1/agent/mcp/oauth/callback/{connection_id}"
            ]
            callback = client.get(
                f"/v1/agent/mcp/oauth/callback/{connection_id}",
                params={"state": query["state"][0], "code": "service-test-code"},
                follow_redirects=False,
            )
        assert callback.status_code == 302
        assert parse_qs(urlsplit(callback.headers["location"]).query)["mcp_authorized"] == ["1"]
        register.assert_not_awaited()
        assert discover.call_args.kwargs["preregistered_issuer"] == (
            issuer if stored_resource else None
        )
        request = exchange.call_args.args[0]
        assert request.client.client_id == "service-test-client"
        assert request.client.client_secret == "service-test-secret"
        assert request.client.auth_method == "client_secret_basic"
        assert request.form()["resource"] == expected_resource
        row = store.connections[connection_id]
        assert row["registration"] == "preregistered"
        assert row["resource"] == expected_resource
        assert oauth_cipher(_ENCRYPTION_KEY).decrypt(row["client_secret_ciphertext"]) == (
            "service-test-secret"
        )
        assert mcp_cipher(_ENCRYPTION_KEY).decrypt(row["access_token_ciphertext"]) == (
            "service-test-token"
        )
        refresh_request = refresh._build_request(row, cipher=mcp_cipher(_ENCRYPTION_KEY))
        assert refresh_request.form()["resource"] == expected_resource

    @pytest.mark.parametrize(
        "session_base,override,expected_base",
        [
            ("https://agents.example.com/api", "", "https://agents.example.com/api"),
            ("https://example.com/agents/api", "", "https://example.com/agents/api"),
            ("http://127.0.0.1:13002/api", "", "http://127.0.0.1:13002/api"),
            (
                "https://agents.example.com/api",
                "https://mcp.example.com/api/",
                "https://mcp.example.com/api",
            ),
        ],
    )
    def test_sign_in_reuses_the_public_session_callback_through_token_exchange(
        self, monkeypatch, session_base, override, expected_base
    ) -> None:
        """Registration, authorization and redemption agree without a second URL setting."""
        from cloud_agent.mcp import oauth

        monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", override)
        monkeypatch.setenv("AGENT_SESSION_REDIRECT_URI", f"{session_base}/v1/session/callback")
        monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://web.example.com/agents")
        metadata = oauth.AuthorizationServerMetadata(
            issuer="https://mcp.notion.com",
            authorization_endpoint="https://mcp.notion.com/authorize",
            token_endpoint="https://mcp.notion.com/token",
            registration_endpoint="https://mcp.notion.com/register",
            scopes_supported=("default",),
        )
        monkeypatch.setattr(
            oauth, "discover_authorization_server", AsyncMock(return_value=(None, metadata))
        )
        register = AsyncMock(
            return_value={"client_id": "notion-client", "token_endpoint_auth_method": "none"}
        )
        exchange = AsyncMock(
            return_value={
                "access_token": "notion-token",
                "refresh_token": "refresh",
                "expires_in": 3600,
            }
        )
        monkeypatch.setattr(mcp_connections, "_post_json", register)
        monkeypatch.setattr(mcp_connections, "_post_form", exchange)
        store = FakeStore()
        connection_id = _seed(
            store, url="https://mcp.notion.com/mcp", auth_kind="oauth", auth_config={}
        )

        async def save_flow(**kwargs):
            generation = int(kwargs["expected_oauth_generation"]) + 1
            store.oauth_flow = {**kwargs, "oauth_generation": generation}
            store.oauth_state_hash = kwargs["state_hash"]
            store.connections[connection_id]["oauth_generation"] = generation
            return generation

        monkeypatch.setattr(store, "create_mcp_oauth_flow", save_flow, raising=False)
        expected = f"{expected_base}/v1/agent/mcp/oauth/callback/{connection_id}"
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/connections/{connection_id}/authorize",
                headers={"Host": "untrusted.example", "X-Forwarded-Host": "untrusted.example"},
            )
            assert response.status_code == 200, response.text
            query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
            assert query["redirect_uri"] == [expected]
            assert query["code_challenge_method"] == ["S256"]
            assert register.call_args.args[2]["redirect_uris"] == [expected]
            callback = client.get(
                f"/v1/agent/mcp/oauth/callback/{connection_id}",
                params={"state": query["state"][0], "code": "authorization-code"},
                follow_redirects=False,
            )
        assert callback.status_code == 302
        assert callback.headers["location"].startswith(
            "https://web.example.com/agents/integrations?"
        )
        assert parse_qs(urlsplit(callback.headers["location"]).query)["mcp_authorized"] == ["1"]
        assert exchange.call_args.args[0].form()["redirect_uri"] == expected
        assert (
            mcp_cipher(_ENCRYPTION_KEY).decrypt(
                store.connections[connection_id]["access_token_ciphertext"]
            )
            == "notion-token"
        )

    @pytest.mark.parametrize(
        "callback",
        [
            "",
            "https://example.com/integrations",
            "https://example.com/v1/session/callback?next=other",
        ],
    )
    def test_missing_or_unrecognized_callback_does_not_guess_an_address(
        self, monkeypatch, callback
    ):
        monkeypatch.delenv("AGENT_MCP_PUBLIC_BASE_URL", raising=False)
        monkeypatch.setenv("AGENT_SESSION_REDIRECT_URI", callback)
        assert mcp_connections._public_base_url() == ""

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

    def test_only_the_account_that_started_a_flow_can_finish_it(self, monkeypatch) -> None:
        """A consent approved in somebody else's browser stores nothing anywhere.

        The callback reads the real session cookie here rather than an
        override. A browser signed in as another account, and one with no
        usable session, are both refused before the code is exchanged, and the
        flow is left for the account that started it, which can still finish.
        """
        from cloud_agent import identity
        from cloud_agent.mcp import oauth

        monkeypatch.setenv(identity.ENV_SESSION_SECRET, "mcp-callback-session-secret-for-tests")
        monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
        monkeypatch.setenv("AGENT_MCP_PUBLIC_BASE_URL", "https://example.com/agents/api")
        monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://example.com/agents")
        metadata = oauth.AuthorizationServerMetadata(
            issuer="https://auth.example.com",
            authorization_endpoint="https://auth.example.com/authorize",
            token_endpoint="https://auth.example.com/token",
        )
        monkeypatch.setattr(
            oauth, "discover_authorization_server", AsyncMock(return_value=(None, metadata))
        )
        exchange = AsyncMock(return_value={"access_token": "owner-token"})
        monkeypatch.setattr(mcp_connections, "_post_form", exchange)
        store = FakeStore()
        connection_id = _seed(
            store, auth_kind="oauth", auth_config={"client_id": "registered-client"}
        )

        async def save_flow(**kwargs):
            generation = int(kwargs["expected_oauth_generation"]) + 1
            store.oauth_flow = {**kwargs, "oauth_generation": generation}
            store.oauth_state_hash = kwargs["state_hash"]
            store.connections[connection_id]["oauth_generation"] = generation
            return generation

        monkeypatch.setattr(store, "create_mcp_oauth_flow", save_flow, raising=False)
        with _client(store) as client:
            # Started as the owner; finished by whatever session the browser has.
            del client.app.dependency_overrides[mcp_connections._callback_session_user]
            started = client.post(f"/v1/agent/mcp/connections/{connection_id}/authorize")
            assert started.status_code == 200, started.text
            state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]

            def finish(session: str | None) -> httpx.Response:
                client.cookies.clear()
                if session is not None:
                    client.cookies.set(identity.SESSION_COOKIE, session)
                return client.get(
                    f"/v1/agent/mcp/oauth/callback/{connection_id}",
                    params={"state": state, "code": "consented-code"},
                    follow_redirects=False,
                )

            for session, reason in (
                (identity.mint_session(user_id=_OTHER), "started by another account"),
                (None, "sign in to the account that started this authorization"),
                ("not-a-session", "sign in to the account that started this authorization"),
            ):
                refused = finish(session)
                assert refused.status_code == 302
                assert refused.headers["location"].startswith(
                    "https://example.com/agents/integrations?"
                )
                result = parse_qs(urlsplit(refused.headers["location"]).query)
                assert result["mcp_authorized"] == ["0"]
                assert result["mcp_connection"] == [str(connection_id)]
                assert reason in result["mcp_message"][0]
                exchange.assert_not_awaited()
                assert store.credential_writes == []
                assert store.oauth_flow is not None

            completed = finish(identity.mint_session(user_id=_OWNER))
        assert parse_qs(urlsplit(completed.headers["location"]).query)["mcp_authorized"] == ["1"]
        exchange.assert_awaited_once()
        assert store.oauth_flow is None
        assert (
            mcp_cipher(_ENCRYPTION_KEY).decrypt(
                store.connections[connection_id]["access_token_ciphertext"]
            )
            == "owner-token"
        )


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
            "user_id": _OWNER,
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
            session_user=_OWNER,
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


class TestTheClientSecretIsNotACredential:
    """A client secret authenticates to the authorization server, never upstream.

    A user who brings a client id and secret has them stored before the browser
    flow starts, so an abandoned or failed sign-in leaves a row holding a client
    secret and no access token. That row needs authorizing; it has no credential.
    """

    def test_an_unfinished_sign_in_needs_authorizing_rather_than_reconnecting(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = FakeStore()
        connection_id = _seed(
            store, auth_kind="oauth", auth_config={}, access_token_ciphertext=None
        )
        with _client(store) as client:
            client.post(
                f"/v1/agent/mcp/connections/{connection_id}/authorize",
                json={"client_id": "our-client", "client_secret": "shh"},
            )
            assert store.connections[connection_id]["client_secret_ciphertext"]
            view = client.get(f"/v1/agent/mcp/connections/{connection_id}").json()
            with caplog.at_level("WARNING"):
                result = client.post(f"/v1/agent/mcp/connections/{connection_id}/test").json()
        assert view["has_credential"] is False
        assert result["ok"] is False
        assert result["error_kind"] == "needs_authorization"
        assert result["message"] == "This connection needs to be authorized."
        assert "agent_mcp_credential_unreadable" not in caplog.text

    async def test_a_client_secret_is_never_resolved_as_the_bearer(self) -> None:
        """Not even one the credential cipher could read."""
        from cloud_agent.mcp.registry import resolve_connection, server_for

        store = FakeStore()
        connection_id = _seed(
            store,
            auth_kind="oauth",
            auth_config={},
            access_token_ciphertext=None,
            client_secret_ciphertext=mcp_cipher(_ENCRYPTION_KEY).encrypt("client-secret"),
        )
        resolved = await resolve_connection(store, row=store._row(connection_id))
        assert not resolved.usable
        assert "client-secret" not in json.dumps(dict(resolved.credential))
        assert resolved.error == "This connection needs to be authorized."
        assert "client-secret" not in json.dumps(
            server_for(resolved, _binding(connection_id)).headers
        )


class TestATokenStaysWithItsResource:
    """An OAuth token is sent only to an address its resource covers.

    Editing a connection's address used to keep its token, so rows edited before
    edits cleared credentials still hold one minted for the old server.
    """

    def test_a_token_for_the_old_address_is_not_sent_to_the_new_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = FakeStore()
        connection_id = _seed(
            store,
            auth_kind="oauth",
            auth_config={"client_id": "client"},
            url="https://elsewhere.example.com/mcp",
            issuer="https://auth.example.com",
            resource="https://mcp.example.com/mcp",
        )
        discover = AsyncMock(side_effect=AssertionError("the old token was sent"))
        monkeypatch.setattr(mcp_connections.McpHost, "discover", discover)
        with _client(store) as client:
            result = client.post(f"/v1/agent/mcp/connections/{connection_id}/test").json()
        discover.assert_not_called()
        assert result["ok"] is False
        assert result["error_kind"] == "needs_authorization"
        assert "another address" in result["message"]

    @pytest.mark.parametrize(
        "resource",
        [
            "https://mcp.example.com/mcp",
            # Discovery accepts a resource that differs only by a trailing
            # slash, and stores the server's spelling of it.
            "https://mcp.example.com/mcp/",
            "https://mcp.example.com/",
            None,
        ],
    )
    async def test_a_covering_or_unrecorded_resource_is_still_used(
        self, resource: str | None
    ) -> None:
        """Connections signed in for their own address are unaffected."""
        from cloud_agent.mcp.registry import resolve_connection

        store = FakeStore()
        connection_id = _seed(
            store,
            auth_kind="oauth",
            auth_config={"client_id": "client"},
            issuer="https://auth.example.com",
            resource=resource,
        )
        resolved = await resolve_connection(store, row=store._row(connection_id))
        assert resolved.usable
        assert resolved.credential["secret"] == "upstream-secret"


def _binding(connection_id: int) -> Any:
    """A default selection for one connection, as a new conversation gets."""
    from cloud_agent.mcp.registry import binding_from_row

    return binding_from_row(
        {"thread_id": "thread-1", "connection_id": connection_id, "alias": "docs"}
    )


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

    def test_later_credential_rejection_rolls_back_the_whole_import(self) -> None:
        """An import reports no success while an earlier draft remains committed."""
        store = FakeStore()
        store.reject_credential_writes = True
        request = {
            "text": json.dumps(
                {
                    "mcpServers": {
                        "public": {"url": "https://public.example.com/mcp"},
                        "private": {
                            "url": "https://private.example.com/mcp",
                            "headers": {"Authorization": "Bearer private-token"},
                        },
                    }
                }
            ),
            "save": True,
        }
        with (
            _client(store) as client,
            pytest.raises(RuntimeError, match="credential write rejection"),
        ):
            client.post("/v1/agent/mcp/connections/import", json=request)
        assert store.connections == {}
        assert store.created == []

        store.reject_credential_writes = False
        with _client(store) as client:
            retried = client.post("/v1/agent/mcp/connections/import", json=request)
        assert retried.status_code == 200
        assert [row["id"] for row in retried.json()["saved"]] == [1, 2]
        assert sorted(store.connections) == [1, 2]

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

    def test_a_later_draft_failing_to_store_a_secret_saves_nothing(self, monkeypatch) -> None:
        """Import reports one error, so it must not keep the drafts that fitted."""
        store = FakeStore()
        original = store.set_mcp_credential
        calls = 0

        async def reject_the_second(**kwargs: Any) -> bool:
            nonlocal calls
            calls += 1
            if calls >= 2:
                return False
            return await original(**kwargs)

        monkeypatch.setattr(store, "set_mcp_credential", reject_the_second)
        servers = {
            name: {
                "url": f"https://{name}.example.com/mcp",
                "headers": {"Authorization": "Bearer ${env:API_KEY}"},
            }
            for name in ("wiki", "tracker")
        }
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={
                    "text": json.dumps({"mcpServers": servers}),
                    "save": True,
                    "server_secrets": {
                        "wiki": {"Authorization": "wiki-token"},
                        "tracker": {"Authorization": "tracker-token"},
                    },
                },
            )
        assert response.status_code == 409
        assert store.connections == {}
        assert store.created == []

    def test_a_missing_key_does_not_keep_an_earlier_imported_draft(self, monkeypatch) -> None:
        """Encryption of a later secret happens before any draft is inserted."""
        monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
        store = FakeStore()
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={
                    "text": json.dumps(
                        {
                            "mcpServers": {
                                "public": {"url": "https://public.example.com/mcp"},
                                "wiki": {
                                    "url": "https://wiki.example.com/mcp",
                                    "headers": {"Authorization": "Bearer literal"},
                                },
                            }
                        }
                    ),
                    "save": True,
                },
            )
        assert response.status_code == 503
        assert store.connections == {}
        assert store.created == []

    @pytest.mark.parametrize(
        ("entry", "locations", "refusal"),
        [
            ({"url": "http://wiki.example.com/mcp"}, {}, "http is not allowed"),
            ({"url": "https://wiki.example.com/mcp"}, {"wiki": "managed-worker"}, "stdio"),
            ({"command": "npx", "args": ["-y", "@scope/wiki@1.0.0"]}, {"wiki": "remote"}, "stdio"),
            ({"command": "npx", "args": ["-y", "@scope/wiki"]}, {}, "pin"),
            ({"command": "bash", "args": ["-c", "npx -y @scope/wiki@1.0.0"]}, {}, "shell"),
            (
                {"command": "npx", "args": ["-y", "@scope/wiki@1.0.0", "~/notes"]},
                {"wiki": "managed-worker"},
                "absolute path",
            ),
        ],
    )
    def test_a_draft_that_create_would_refuse_saves_nothing(
        self, entry: dict[str, Any], locations: dict[str, str], refusal: str
    ) -> None:
        """Saving checks each draft the way adding one by hand does.

        The import used to store whatever the paste and the chosen locations
        said -- a cleartext address, an address saved as a worker process, an
        unpinned package, a shell, a home-relative mount -- which are rows the
        connection form refuses and a future runner would trust.
        """
        store = FakeStore()
        paste = {"mcpServers": {"public": {"url": "https://public.example.com/mcp"}, "wiki": entry}}
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={"text": json.dumps(paste), "save": True, "locations": locations},
            )
        assert response.status_code == 400
        assert refusal in response.json()["detail"]["error"]["message"]
        assert store.connections == {}
        assert store.created == []

    def test_a_saved_draft_is_stored_in_the_form_create_stores(self) -> None:
        """The address normalised, and a worker's mounts the ones its plan accepts."""
        store = FakeStore()
        paste = {
            "mcpServers": {
                "wiki": {"url": "https://Wiki.Example.com/mcp#tools"},
                "files": {"command": "npx", "args": ["-y", "@scope/files@1.2.3", "/srv/data"]},
            }
        }
        with _client(store) as client:
            response = client.post(
                "/v1/agent/mcp/connections/import",
                json={
                    "text": json.dumps(paste),
                    "save": True,
                    "locations": {"files": "managed-worker"},
                },
            )
        assert response.status_code == 200, response.text
        stored = {row["label"]: row for row in store.connections.values()}
        assert stored["wiki"]["url"] == "https://wiki.example.com/mcp"
        assert stored["files"]["location"] == "managed-worker"
        assert stored["files"]["launch_spec"]["args"] == ["-y", "@scope/files@1.2.3", "/srv/data"]
        assert stored["files"]["launch_spec"]["mounts"] == [
            {"source": "/srv/data", "target": "/workspace/mcp/0", "read_only": True}
        ]


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

    @pytest.mark.parametrize("attempt_id", [41, None])
    def test_lists_and_decisions_report_the_attempt_that_raised_the_call(
        self, attempt_id: int | None
    ) -> None:
        """A retried job cannot make an earlier approval appear to belong to its new attempt."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        if attempt_id is not None:
            store.invocations[invocation_id]["attempt_id"] = attempt_id
        store.jobs["job-1"] = {
            "id": "job-1",
            "user_id": _OWNER,
            "current_attempt_id": 42,
        }
        with _client(store) as client:
            listed = client.get(
                "/v1/agent/mcp/invocations", params={"job_id": "job-1", "pending_only": True}
            )
            decided = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision", json={"approve": True}
            )
        assert listed.status_code == decided.status_code == 200
        assert listed.json()["invocations"][0]["attempt_id"] == attempt_id
        assert decided.json()["attempt_id"] == attempt_id

    @pytest.mark.parametrize("kind", ["tool", "prompt", "resource", "resource_template"])
    def test_always_allow_records_the_exact_held_capability(self, kind) -> None:
        """An approval carries its server-derived permission intent in the same write."""
        from cloud_agent.mcp.models import Capability, CapabilityKind

        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        row = store.invocations[invocation_id]
        row.update(capability_revision=1, capability_kind=kind)
        surfaces = {
            "tool": "tools",
            "prompt": "prompts",
            "resource": "resources",
            "resource_template": "resource_templates",
        }
        entry = {
            "name": "create_issue",
            "description": "Review this capability",
            "input_schema": {"type": "object"},
        }
        store.capabilities[connection_id] = {
            "capability_revision": 1,
            surfaces[kind]: [entry],
        }
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision",
                json={"approve": True, "always_allow": True},
            )
        assert response.status_code == 200
        assert response.json()["always_allow_status"] == "pending"
        assert store.invocations[invocation_id]["always_allow_rule"] == {
            "policy": "allow",
            "fingerprint": Capability(
                kind=CapabilityKind(kind),
                name="create_issue",
                description=entry["description"],
                input_schema=entry["input_schema"],
            ).fingerprint(),
        }
        assert store.bindings == {}

    @pytest.mark.parametrize("change", ["missing", "revision", "deny", "other-owner"])
    def test_invalid_always_allow_does_not_write_a_decision(self, change) -> None:
        """Neither stale definitions nor a client assertion authorize a saved permission."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.invocations[invocation_id]["capability_revision"] = 1
        store.capabilities[connection_id] = {
            "capability_revision": 2 if change == "revision" else 1,
            "tools": [] if change == "missing" else [{"name": "create_issue"}],
        }
        with _client(store, user_id=_OTHER if change == "other-owner" else _OWNER) as client:
            response = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision",
                json={"approve": change != "deny", "always_allow": True},
            )
        assert response.status_code == (
            404 if change == "other-owner" else 422 if change == "deny" else 409
        )
        assert store.invocations[invocation_id]["status"] == "pending"

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

    def test_deleted_conversation_history_is_not_accessible_by_job(self) -> None:
        """A retained ledger row does not make a deleted conversation visible."""
        store = FakeStore()
        connection_id = _seed(store)
        self._pending(store, connection_id)
        store.jobs["job-1"] = {
            "id": "job-1",
            "user_id": _OWNER,
            "deleted_at": datetime.now(timezone.utc),
        }
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/invocations", params={"job_id": "job-1"})
        assert response.status_code == 404

    @pytest.mark.parametrize("approve", [True, False])
    def test_deleted_conversation_calls_cannot_be_decided(self, approve: bool) -> None:
        """Neither an approval nor a denial may edit a deleted conversation."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.jobs["job-1"] = {
            "id": "job-1",
            "user_id": _OWNER,
            "deleted_at": datetime.now(timezone.utc),
        }
        store.decide_mcp_invocation = AsyncMock(  # type: ignore[method-assign]
            wraps=store.decide_mcp_invocation
        )
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision", json={"approve": approve}
            )
        assert response.status_code == 404
        store.decide_mcp_invocation.assert_not_awaited()
        assert store.invocations[invocation_id]["status"] == "pending"

    @pytest.mark.parametrize("scoped", [False, True])
    def test_deleted_conversation_calls_are_omitted_from_inboxes(self, scoped: bool) -> None:
        """Both the owner-wide and conversation inbox hide tombstoned calls."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.jobs["job-1"] = {
            "id": "job-1",
            "user_id": _OWNER,
            "deleted_at": datetime.now(timezone.utc),
        }
        with _client(store) as client:
            params: dict[str, Any] = {"pending_only": True}
            if scoped:
                params["thread_id"] = "thread-1"
            response = client.get("/v1/agent/mcp/invocations", params=params)
        assert response.status_code == 200
        assert response.json()["invocations"] == []
        assert response.json()["has_more"] is False
        assert store.invocations[invocation_id]["status"] == "pending"

    def test_listing_requires_a_job_or_a_conversation(self) -> None:
        """History remains scoped even though the pending inbox is account-wide."""
        with _client(FakeStore()) as client:
            assert client.get("/v1/agent/mcp/invocations").status_code == 400

    def test_the_global_pending_inbox_is_scoped_to_the_owner(self) -> None:
        """Foreign calls and their decrypted arguments never enter the inbox."""
        store = FakeStore()
        connection_id = _seed(store)
        mine = self._pending(store, connection_id)
        store.threads["thread-1"] = {
            "id": "thread-1",
            "user_id": _OWNER,
            "title": "Repair the integration",
        }
        store.invocations[2] = dict(
            store.invocations[mine],
            id=2,
            job_id="other-job",
            thread_id="other-thread",
            arguments_ciphertext=mcp_connections.mcp_invocation_cipher().encrypt(
                json.dumps({"private": "another owner's document"})
            ),
        )
        store.jobs["other-job"] = {"id": "other-job", "user_id": _OTHER}
        store.invocations[3] = dict(store.invocations[mine], id=3, status="completed")
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/invocations", params={"pending_only": True})
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["invocations"]] == [mine]
        assert response.json()["invocations"][0]["thread_title"] == "Repair the integration"
        assert "another owner's document" not in response.text
        assert response.json()["has_more"] is False
        assert response.json()["next_before_id"] is None

    def test_inbox_pagination_does_not_skip_calls_when_new_calls_arrive(self) -> None:
        """A later insert must not shift the continuation page or repeat a call."""
        store = FakeStore()
        connection_id = _seed(store)
        first_id = self._pending(store, connection_id)
        for invocation_id in (2, 3, 4):
            store.invocations[invocation_id] = dict(store.invocations[first_id], id=invocation_id)
        with _client(store) as client:
            first = client.get(
                "/v1/agent/mcp/invocations", params={"pending_only": True, "limit": 2}
            ).json()
            store.invocations[5] = dict(store.invocations[first_id], id=5)
            second = client.get(
                "/v1/agent/mcp/invocations",
                params={"pending_only": True, "limit": 2, "before_id": first["next_before_id"]},
            ).json()
        assert [row["id"] for row in first["invocations"]] == [4, 3]
        assert first["has_more"] is True
        assert first["next_before_id"] == 3
        assert [row["id"] for row in second["invocations"]] == [2, 1]
        assert second["has_more"] is False
        assert second["next_before_id"] is None

    def test_history_pages_include_calls_older_than_the_default_window(self) -> None:
        """Older history remains reachable past the previous fifty-call cutoff."""
        store = FakeStore()
        connection_id = _seed(store)
        first_id = self._pending(store, connection_id)
        for invocation_id in range(2, 56):
            store.invocations[invocation_id] = dict(
                store.invocations[first_id], id=invocation_id, status="completed"
            )
        with _client(store) as client:
            first = client.get("/v1/agent/mcp/invocations", params={"job_id": "job-1"}).json()
            second = client.get(
                "/v1/agent/mcp/invocations",
                params={"job_id": "job-1", "before_id": first["next_before_id"]},
            ).json()
        assert len(first["invocations"]) == 50
        assert first["next_before_id"] == 6
        assert [row["id"] for row in second["invocations"]] == [5, 4, 3, 2, 1]
        assert second["has_more"] is False

    @pytest.mark.parametrize(
        "params", [{"limit": 0}, {"limit": 101}, {"before_id": 0}, {"before_id": 2**63}]
    )
    def test_invocation_pages_reject_invalid_bounds(self, params: dict[str, int]) -> None:
        """Clients cannot request unbounded pages or an invalid continuation."""
        with _client(FakeStore()) as client:
            response = client.get(
                "/v1/agent/mcp/invocations", params={"pending_only": True, **params}
            )
        assert response.status_code == 422

    @pytest.mark.parametrize("status", ["pending", "approved"])
    def test_history_displays_elapsed_deadlines_without_mutating_a_call(self, status: str) -> None:
        """A lagging expiry worker must not leave an actionable-looking card."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.invocations[invocation_id].update(
            status=status, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        with _client(store) as client:
            history = client.get("/v1/agent/mcp/invocations", params={"job_id": "job-1"}).json()
            inbox = client.get("/v1/agent/mcp/invocations", params={"pending_only": True}).json()
        assert history["invocations"][0]["status"] == "expired"
        assert "expired" in history["invocations"][0]["guidance"]
        assert inbox["invocations"] == []
        assert store.invocations[invocation_id]["status"] == status

    def test_thread_titles_are_owned_and_cached_per_page(self) -> None:
        """Metadata cannot leak a foreign title or require one query per call."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.invocations[2] = dict(store.invocations[invocation_id], id=2)
        store.invocations[3] = dict(
            store.invocations[invocation_id], id=3, thread_id="foreign-thread"
        )
        store.threads["thread-1"] = {"user_id": _OWNER, "title": "Owned conversation"}
        store.threads["foreign-thread"] = {"user_id": _OTHER, "title": "Private title"}
        store.get_thread = AsyncMock(wraps=store.get_thread)  # type: ignore[method-assign]
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/invocations", params={"pending_only": True})
        views = response.json()["invocations"]
        assert views[0]["thread_title"] is None
        assert [view["thread_title"] for view in views[1:]] == [
            "Owned conversation",
            "Owned conversation",
        ]
        assert store.get_thread.await_count == 2

    def _catalogue(self, store: FakeStore, connection_id: int, revision: int) -> None:
        """Store a discovered catalogue under ``revision``."""
        store.capabilities[connection_id] = {
            "connection_id": connection_id,
            "capability_revision": revision,
            "tools": [
                {
                    "name": "create_issue",
                    "description": "Open an issue in a repository.",
                    "input_schema": {"type": "object"},
                    "annotations": {"title": "Create issue", "destructiveHint": False},
                }
            ],
            "resources": [],
            "resource_templates": [],
            "prompts": [
                {"name": "create_issue", "description": "A prompt of the same name."},
            ],
        }

    def test_a_held_call_carries_what_the_server_says_it_does(self) -> None:
        """The owner decides on the tool's own description, not only its name."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.invocations[invocation_id].update(capability_kind="tool", capability_revision=3)
        self._catalogue(store, connection_id, revision=3)
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/invocations", params={"job_id": "job-1"})
        assert response.status_code == 200
        view = response.json()["invocations"][0]
        assert view["capability_kind"] == "tool"
        assert view["capability_description"] == "Open an issue in a repository."
        assert view["capability_annotations"] == {"title": "Create issue", "destructiveHint": False}

    def test_a_replaced_catalogue_does_not_describe_an_older_call(self) -> None:
        """A newer discovery may describe a different tool under the same name."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.invocations[invocation_id].update(capability_kind="tool", capability_revision=2)
        self._catalogue(store, connection_id, revision=3)
        with _client(store) as client:
            response = client.get("/v1/agent/mcp/invocations", params={"job_id": "job-1"})
        view = response.json()["invocations"][0]
        assert view["capability_description"] is None
        assert view["capability_annotations"] == {}

    def test_a_held_call_is_described_from_its_own_surface(self) -> None:
        """A prompt and a tool may share a name; the call's kind picks which."""
        store = FakeStore()
        connection_id = _seed(store)
        invocation_id = self._pending(store, connection_id)
        store.invocations[invocation_id].update(capability_kind="prompt", capability_revision=3)
        self._catalogue(store, connection_id, revision=3)
        with _client(store) as client:
            response = client.post(
                f"/v1/agent/mcp/invocations/{invocation_id}/decision", json={"approve": True}
            )
        view = response.json()
        assert view["capability_kind"] == "prompt"
        assert view["capability_description"] == "A prompt of the same name."


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

    def test_disconnect_removes_the_connector_route_and_authority(self) -> None:
        """A tombstone cannot remain in a connector's authorized set."""
        store = FakeStore()
        with _client(store) as client:
            connector = client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"}).json()[
                "connector"
            ]
            created = client.post(
                "/v1/agent/mcp/connections",
                json={
                    "label": "Local",
                    "transport": "stdio",
                    "location": "user-connector",
                    "launch": {
                        "command": "npx",
                        "args": ["-y", "@scope/server@1.0.0"],
                    },
                    "connector_id": connector["id"],
                },
            )
            connection_id = created.json()["id"]
            removed = client.delete(f"/v1/agent/mcp/connections/{connection_id}")

        assert removed.status_code == 204
        assert store.connections[connection_id]["connector_id"] is None
        assert store.connector_connections[connector["id"]] == set()

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

    def test_a_new_connection_cannot_name_another_users_connector(self) -> None:
        """Creation reads the connector under the caller's ownership, as binding does."""
        store = FakeStore()
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
        launch = {"command": "uvx", "args": ["server==1.0"]}
        body = {
            "label": "Local",
            "transport": "stdio",
            "location": "user-connector",
            "launch": launch,
        }
        with _client(store) as client:
            refused = client.post("/v1/agent/mcp/connections", json={**body, "connector_id": 9})
            assert refused.status_code == 404
            assert store.connections == {}
            own = client.post("/v1/agent/mcp/connectors", json={"name": "Mine"}).json()
            created = client.post(
                "/v1/agent/mcp/connections",
                json={**body, "connector_id": own["connector"]["id"]},
            )
        assert created.status_code == 201, created.text
        assert created.json()["connector_id"] == own["connector"]["id"]

    def test_unbinding_a_connection_that_is_not_routed_here_is_not_found(self) -> None:
        """Silence would let a caller believe it had stopped something."""
        store = FakeStore()
        connection = self._stdio(store)
        with _client(store) as client:
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop"})
            response = client.delete(f"/v1/agent/mcp/connectors/1/connections/{connection}")
        assert response.status_code == 404

    def test_unbinding_through_another_connector_leaves_the_real_binding(self) -> None:
        """The path's connector is the fence, not a lookup that happens first.

        Verifying that connector A exists and then clearing connection C is how
        a stale tab detaches C from B. The binding, its versions, and B's
        authorized set have to be the ones the request found.
        """
        store = FakeStore()
        connection = self._stdio(store)
        with _client(store) as client:
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop A"})
            client.post("/v1/agent/mcp/connectors", json={"name": "Laptop B"})
            bound = client.post(
                "/v1/agent/mcp/connectors/2/connections", json={"connection_id": connection}
            )
            assert bound.status_code == 200
            before = dict(store.connections[connection])
            response = client.delete(f"/v1/agent/mcp/connectors/1/connections/{connection}")
        assert response.status_code == 404
        assert store.connections[connection]["connector_id"] == 2
        assert store.connections[connection]["config_revision"] == before["config_revision"]
        assert store.connections[connection]["authorization_epoch"] == before["authorization_epoch"]
        assert store.connector_connections.get(1, set()) == set()
        assert connection in store.connector_connections[2]


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


class TestAutomaticServerAccess:
    def test_connection_can_be_paused_without_losing_credentials(self) -> None:
        store = FakeStore()
        cid = _seed(store, access_token_ciphertext="encrypted-credential")
        with _client(store) as client:
            paused = client.put(f"/v1/agent/mcp/connections/{cid}/enabled", json={"enabled": False})
            assert paused.status_code == 200
            assert paused.json()["enabled"] is False
            assert paused.json()["has_credential"] is True
            assert "encrypted-credential" not in paused.text
            resumed = client.put(f"/v1/agent/mcp/connections/{cid}/enabled", json={"enabled": True})
        assert resumed.json()["enabled"] is True
        assert store.connections[cid]["access_token_ciphertext"] == "encrypted-credential"

    def test_cannot_toggle_another_owners_connection(self) -> None:
        store = FakeStore()
        cid = _seed(store, user_id="someone-else")
        with _client(store) as client:
            response = client.put(
                f"/v1/agent/mcp/connections/{cid}/enabled", json={"enabled": False}
            )
        assert response.status_code == 404
        assert store.connections[cid].get("enabled", True) is True

    def test_default_access_can_be_enabled_before_discovery_and_disabled_again(self) -> None:
        store = FakeStore()
        cid = _seed(store)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            for enabled in (True, False):
                response = client.put(
                    f"/v1/agent/mcp/threads/thread-1/bindings/{cid}",
                    json={"connection_id": cid, "enabled": enabled, "use_defaults": True},
                )
                assert response.status_code == 200, response.text
                row = response.json()["bindings"][0]
                assert row["enabled"] is enabled
                assert row["selection_pending"] is True
                assert row["tools"] == {}

    def test_editing_one_server_preserves_the_other_servers_fences_and_rules(self) -> None:
        store = FakeStore()
        first, second = _seed(store), _seed(store)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            for cid in (first, second):
                response = client.put(
                    f"/v1/agent/mcp/threads/thread-1/bindings/{cid}",
                    json={"connection_id": cid, "use_defaults": True},
                )
                assert response.status_code == 200
            other = dict(
                next(row for row in store.bindings["thread-1"] if row["connection_id"] == second)
            )
            store.connections[second]["authorization_epoch"] += 1
            response = client.put(
                f"/v1/agent/mcp/threads/thread-1/bindings/{first}",
                json={"connection_id": first, "enabled": False, "use_defaults": True},
            )
        assert response.status_code == 200
        assert (
            next(row for row in store.bindings["thread-1"] if row["connection_id"] == second)
            == other
        )

    def test_an_override_cannot_reuse_another_servers_alias(self) -> None:
        store = FakeStore()
        first, second = _seed(store), _seed(store)
        store.threads["thread-1"] = {"id": "thread-1", "user_id": _OWNER}
        with _client(store) as client:
            for cid, expected in ((first, 200), (second, 400)):
                response = client.put(
                    f"/v1/agent/mcp/threads/thread-1/bindings/{cid}",
                    json={"connection_id": cid, "alias": "wiki", "use_defaults": True},
                )
                assert response.status_code == expected
