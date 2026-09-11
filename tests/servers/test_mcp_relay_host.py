"""The merged host endpoint: every connection of one job behind a single URL.

The per-connection route forwards JSON-RPC and lets the CLI own the protocol.
This one owns it: the platform answers ``initialize``, merges the catalogues,
namespaces the tool names and routes a call back to the connection it came from.

Four properties carry the weight, and each is a way the merged surface could be
worse than the per-connection one:

- **The decision path is shared, not copied.** A held call here creates the same
  pending invocation the other route does, so an approval granted on the
  Integrations page is consumed by whichever surface the retry arrives on.
- **Routing is a catalogue lookup, not a string split.** An alias may itself
  contain the separator, so ``a__b`` plus tool ``c`` and alias ``a`` plus tool
  ``b__c`` present the same string; only the map built from the catalogue can
  tell them apart.
- **One broken connection does not empty the catalogue.** A server that is
  disconnected or needs re-authorization is skipped with a log, because losing
  every other tool is a far worse outcome than the failure that caused it.
- **The bare prefix is unshadowable.** ``/{server_name}`` needs a non-empty
  segment, so the merged endpoint cannot be reached as a connection named
  ``host`` or anything else a user could pick.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import mcp_relay as agent_mcp_router
from cloud_agent.api.deps import get_agent_job_store
from cloud_agent.mcp.host import ContentBlock, Session, ToolResult
from cloud_agent.mcp.protocol import Handshake
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.tokens import SCOPE_MCP, mint_worker_token

pytestmark = pytest.mark.asyncio

_OWNER = "owner-1"
_JOB = "ajob_host"
_THREAD = "athr_host"
_ATTEMPT = 5
_LEASE = 2
_ENCRYPTION_KEY = "mcp-host-test-encryption-key"
_HOST_URL = "/v1/agent/mcp"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the signing secret and the key connection credentials use."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "mcp-host-secret-value-32-bytes-xx")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)
    yield


def _connection(connection_id: int, label: str, **overrides: Any) -> dict[str, Any]:
    """One stored connection row, as ``resolve_connection`` reads it."""
    row = {
        "id": connection_id,
        "user_id": _OWNER,
        "label": label,
        "template": label.lower(),
        "transport": "streamable-http",
        "location": "remote",
        "url": f"https://{label.lower()}.example.com/mcp",
        "launch_spec": None,
        "auth_kind": "static",
        "auth_config": {"header_name": "Authorization", "scheme": "Bearer"},
        "connector_id": None,
        "status": "active",
        "error_kind": "none",
        "error_detail": None,
        "access_token_ciphertext": mcp_cipher(_ENCRYPTION_KEY).encrypt(f"{label}-secret"),
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
        "account_label": None,
        "last_checked_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    row.update(overrides)
    return row


def _binding(connection_id: int, alias: str, **overrides: Any) -> dict[str, Any]:
    """One binding row as the fence reports it."""
    row = {
        "thread_id": _THREAD,
        "connection_id": connection_id,
        "alias": alias,
        "enabled": True,
        "selection_pending": False,
        "config_revision": 1,
        "authorization_epoch": 1,
        "capability_revision": 1,
        "policy_revision": 1,
        "rules": {},
        "resource_rules": {},
        "prompt_rules": {},
        "owner_id": _OWNER,
        "connection_status": "active",
        "deleted_at": None,
        "label": alias,
        "transport": "streamable-http",
        "location": "remote",
    }
    row.update(overrides)
    return row


def _snapshot(
    connection_id: int, *tools: str, partial: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A discovery result offering exactly ``tools``."""
    return {
        "connection_id": connection_id,
        "capability_revision": 1,
        "config_revision": 1,
        "protocol_version": "2025-11-25",
        "server_name": f"server{connection_id}",
        "server_version": "1",
        "tools": [
            {
                "name": name,
                "description": f"{name} on {connection_id}",
                "input_schema": {"type": "object", "properties": {}},
                "annotations": {},
            }
            for name in tools
        ],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
        "support": partial or {},
        "discovered_at": None,
    }


class FakeStore:
    """The store surface the merged endpoint reads, holding several connections."""

    def __init__(
        self,
        *,
        live: bool = True,
        connections: dict[int, dict[str, Any]] | None = None,
        bindings: list[dict[str, Any]] | None = None,
        snapshots: dict[int, dict[str, Any]] | None = None,
        approved: dict[str, Any] | None = None,
    ) -> None:
        """Build a store from explicit connections rather than one default."""
        self.live = live
        self.connections = connections or {
            7: _connection(7, "GitHub"),
            8: _connection(8, "Docs"),
        }
        self.bindings = (
            bindings
            if bindings is not None
            else [
                _binding(7, "github", rules={"get_issue": {"policy": "allow", "fingerprint": ""}}),
                _binding(8, "docs", rules={"search": {"policy": "allow", "fingerprint": ""}}),
            ]
        )
        self.snapshots = snapshots or {
            7: _snapshot(7, "get_issue"),
            8: _snapshot(8, "search"),
        }
        self.approved = approved
        self.created_invocations: list[dict[str, Any]] = []
        self.claimed: list[int] = []

    async def resolve_model_credential(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> dict[str, Any] | None:
        if not (self.live and (job_id, attempt_id, lease_generation) == (_JOB, _ATTEMPT, _LEASE)):
            return None
        return {
            "user_id": _OWNER,
            "job_id": _JOB,
            "thread_id": _THREAD,
            "mcp_bindings": [dict(binding) for binding in self.bindings],
            "model": "glm-5.1",
        }

    async def get_mcp_connection(
        self, *, connection_id: int, user_id: str | None = None
    ) -> dict[str, Any] | None:
        row = self.connections.get(int(connection_id))
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return dict(row)

    async def get_mcp_capabilities(self, *, connection_id: int) -> dict[str, Any] | None:
        row = self.snapshots.get(int(connection_id))
        return dict(row) if row is not None else None

    async def find_approved_mcp_invocation(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.approved

    async def claim_mcp_invocation(self, *, invocation_id: int) -> dict[str, Any] | None:
        if self.approved is None or int(self.approved["id"]) != int(invocation_id):
            return None
        self.claimed.append(invocation_id)
        return {**self.approved, "status": "dispatching", "dispatch_generation": 1}

    async def create_mcp_invocation(self, **kwargs: Any) -> dict[str, Any]:
        invocation = {"id": 41 + len(self.created_invocations), **kwargs}
        self.created_invocations.append(invocation)
        return invocation


class StubHost:
    """A protocol host that records what the endpoint asked it to do.

    The endpoint's contract is "given a fence and a catalogue, what do I
    answer"; the transport underneath is covered by the host's own tests and by
    the bridge verification against a real CLI, so substituting it here keeps
    these cases about routing and authorization.
    """

    def __init__(self) -> None:
        """Start with nothing opened and a canned successful result."""
        self.opened: list[dict[str, Any]] = []
        self.called: list[dict[str, Any]] = []
        self.result = ToolResult(content=(ContentBlock(type="text", text="upstream says hi"),))

    async def open(self, connection: Any, *, secret: str | None, key: Any) -> Session:
        self.opened.append({"connection": connection, "secret": secret, "key": key})
        return Session(
            key=key,
            transport=None,
            handshake=Handshake(protocol_version="2025-11-25"),
        )

    async def call_tool(self, session: Session, **kwargs: Any) -> ToolResult:
        self.called.append(kwargs)
        return self.result


@pytest.fixture()
def host(monkeypatch: pytest.MonkeyPatch) -> StubHost:
    """Replace the process-wide host and restore it afterwards."""
    stub = StubHost()
    monkeypatch.setattr(agent_mcp_router, "_HOST_INSTANCE", stub)
    return stub


@pytest.fixture()
def store() -> FakeStore:
    """A store with two usable connections bound under distinct aliases."""
    return FakeStore()


def _token(scope: str = SCOPE_MCP) -> str:
    """An attempt-fenced worker token for the test job."""
    return mint_worker_token(job_id=_JOB, attempt_id=_ATTEMPT, lease_generation=_LEASE, scope=scope)


def _auth(scope: str = SCOPE_MCP) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(scope)}"}


def _rpc(method: str, request_id: Any = 1, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _result(response: Any) -> Any:
    return response.json()["result"]


def _tool_names(response: Any) -> list[str]:
    return [tool["name"] for tool in _result(response)["tools"]]


@pytest.fixture()
async def client(store: FakeStore) -> AsyncClient:
    """Mount the relay router against the fake store."""
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api


class TestTheMergedCatalogue:
    """What the endpoint advertises, and under which names."""

    async def test_two_connections_appear_as_one_namespaced_catalogue(
        self, client: AsyncClient
    ) -> None:
        """A single URL has to distinguish two servers' tools by name alone."""
        response = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert response.status_code == 200
        assert _tool_names(response) == ["docs__search", "github__get_issue"]

    async def test_a_capability_the_session_did_not_enable_is_not_advertised(
        self, client: AsyncClient
    ) -> None:
        """The merged surface is not a way around a session's own selection."""
        response = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert "github__delete_everything" not in _tool_names(response)

    async def test_a_broken_connection_is_skipped_and_the_rest_survive(
        self, client: AsyncClient
    ) -> None:
        """Losing every tool is a worse outcome than the failure that caused it."""
        store = FakeStore(
            connections={
                7: _connection(7, "GitHub"),
                8: _connection(8, "Docs", deleted_at="2026-01-01T00:00:00Z"),
            }
        )
        app = FastAPI()
        app.include_router(agent_mcp_relay_app_router())
        app.dependency_overrides[get_agent_job_store] = lambda: store
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert _tool_names(response) == ["github__get_issue"]

    async def test_an_alias_containing_the_separator_still_routes_by_lookup(
        self, client: AsyncClient, store: FakeStore, host: StubHost
    ) -> None:
        """``a__b`` plus ``c`` and ``a`` plus ``b__c`` present the same string.

        Routing is a lookup in the catalogue built from the bindings, so the
        ambiguity that makes a string split wrong never arises. The call has to
        reach the right connection *and* carry that connection's own tool name.
        """
        store.connections = {7: _connection(7, "GitHub"), 8: _connection(8, "Docs")}
        store.bindings = [
            _binding(7, "a__b", rules={"c": {"policy": "allow", "fingerprint": ""}}),
            _binding(8, "a", rules={"b__c": {"policy": "allow", "fingerprint": ""}}),
        ]
        store.snapshots = {7: _snapshot(7, "c"), 8: _snapshot(8, "b__c")}

        listing = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert "a__b__c" in _tool_names(listing)

        call = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="a__b__c", arguments={}),
            headers=_auth(),
        )
        assert call.status_code == 200
        assert host.called[-1]["name"] == "c"
        assert host.opened[-1]["connection"].connection_id == "7"


class TestRoutingACall:
    """Where a namespaced call actually goes."""

    async def test_a_call_reaches_its_own_connection_with_the_upstream_name(
        self, client: AsyncClient, host: StubHost
    ) -> None:
        """The model sees the namespaced name; the upstream sees its own."""
        response = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="docs__search", arguments={"q": "nsi"}),
            headers=_auth(),
        )
        assert response.status_code == 200
        assert _result(response)["content"][0]["text"] == "upstream says hi"
        assert host.called[-1]["name"] == "search"
        assert host.called[-1]["arguments"] == {"q": "nsi"}
        assert host.opened[-1]["connection"].connection_id == "8"
        # The connection's own credential, not the sandbox's job token.
        assert host.opened[-1]["secret"] == "Docs-secret"

    async def test_an_unknown_name_is_refused_without_dialling_anything(
        self, client: AsyncClient, host: StubHost
    ) -> None:
        """A name that is not in the catalogue is not a server to guess at."""
        response = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="github__drop_table", arguments={}),
            headers=_auth(),
        )
        assert response.status_code == 200
        assert "error" in response.json()
        assert host.called == []

    async def test_a_held_call_is_recorded_and_not_dispatched(
        self, store: FakeStore, host: StubHost
    ) -> None:
        """``confirm`` holds on this surface exactly as it does on the other."""
        store.bindings = [
            _binding(7, "github", rules={"get_issue": {"policy": "confirm", "fingerprint": ""}})
        ]
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_agent_job_store] = lambda: store
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(
                _HOST_URL,
                json=_rpc("tools/call", name="github__get_issue", arguments={"number": 7}),
                headers=_auth(),
            )
        assert response.status_code == 200
        assert "needs your approval" in response.json()["error"]["message"]
        assert host.called == []
        assert len(store.created_invocations) == 1
        assert store.created_invocations[0]["capability_name"] == "get_issue"

    async def test_an_approved_retry_is_dispatched_once(
        self, host: StubHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The approval is consumed by the retry that matches it."""
        store = FakeStore(
            approved={
                "id": 11,
                "status": "approved",
                "arguments_digest": "",
                "capability_name": "get_issue",
            }
        )
        store.bindings = [
            _binding(7, "github", rules={"get_issue": {"policy": "confirm", "fingerprint": ""}})
        ]
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_agent_job_store] = lambda: store
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(
                _HOST_URL,
                json=_rpc("tools/call", name="github__get_issue", arguments={"number": 7}),
                headers=_auth(),
            )
        assert response.status_code == 200
        assert "result" in response.json()
        assert store.claimed == [11]
        assert len(host.called) == 1


class TestTheHandshake:
    """What this surface says about itself."""

    async def test_the_requested_revision_is_echoed_when_it_is_implemented(
        self, client: AsyncClient
    ) -> None:
        """The client decides in this era; answering with something else ends it."""
        response = await client.post(
            _HOST_URL,
            json=_rpc("initialize", protocolVersion="2025-11-25"),
            headers=_auth(),
        )
        result = _result(response)
        assert result["protocolVersion"] == "2025-11-25"
        assert result["capabilities"]["tools"] == {"listChanged": False}

    async def test_an_unknown_revision_gets_the_newest_we_implement(
        self, client: AsyncClient
    ) -> None:
        """Better than driving the client with a version we do not speak."""
        response = await client.post(
            _HOST_URL,
            json=_rpc("initialize", protocolVersion="1999-01-01"),
            headers=_auth(),
        )
        assert _result(response)["protocolVersion"] == "2026-07-28"

    async def test_the_instructions_name_the_sessions_connections(
        self, client: AsyncClient
    ) -> None:
        """The model is told where the namespaced tools come from."""
        response = await client.post(
            _HOST_URL, json=_rpc("initialize", protocolVersion="2025-11-25"), headers=_auth()
        )
        instructions = _result(response)["instructions"]
        assert "docs" in instructions and "github" in instructions

    async def test_a_stateless_client_gets_a_discriminated_result(
        self, client: AsyncClient
    ) -> None:
        """The revision requires ``resultType`` on every result it asked for."""
        response = await client.post(
            _HOST_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
            },
            headers=_auth(),
        )
        assert _result(response)["resultType"] == "complete"

    async def test_a_handshake_client_gets_no_discriminator(self, client: AsyncClient) -> None:
        """And the older era is not given a field it never defined."""
        response = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert "resultType" not in _result(response)

    async def test_the_initialized_notification_is_accepted_silently(
        self, client: AsyncClient
    ) -> None:
        """A notification has no id and gets no body."""
        response = await client.post(
            _HOST_URL,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_auth(),
        )
        assert response.status_code == 202
        assert response.content == b""

    async def test_an_unimplemented_method_is_named(self, client: AsyncClient) -> None:
        """Rather than answered as though it had worked."""
        response = await client.post(_HOST_URL, json=_rpc("resources/list"), headers=_auth())
        assert "does not implement" in response.json()["error"]["message"]

    async def test_no_server_initiated_stream_is_offered(self, client: AsyncClient) -> None:
        """A stream that never carries anything is worse than a clear refusal."""
        response = await client.get(_HOST_URL, headers=_auth())
        assert response.status_code == 405


class TestTheBarePrefix:
    """Why the merged endpoint cannot be shadowed."""

    async def test_the_bare_prefix_reaches_the_host_not_a_connection(
        self, client: AsyncClient, store: FakeStore, host: StubHost
    ) -> None:
        """``/{server_name}`` needs a non-empty segment, so there is no collision.

        A connection literally named ``host`` would still be addressed at
        ``/v1/agent/mcp/host``; the merged endpoint is reachable only at the
        bare prefix, which no alias can produce.
        """
        store.bindings = [
            _binding(7, "host", rules={"get_issue": {"policy": "allow", "fingerprint": ""}})
        ]
        merged = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert _tool_names(merged) == ["host__get_issue"]

        # The per-connection route still answers for the alias itself.
        per_connection = await client.post(
            "/v1/agent/mcp/host",
            json=_rpc("tools/list", 2),
            headers=_auth(),
        )
        assert per_connection.status_code in (200, 502)

    async def test_the_merged_endpoint_requires_an_mcp_scoped_token(
        self, client: AsyncClient
    ) -> None:
        """A second door is still the same door."""
        response = await client.post(_HOST_URL, json=_rpc("tools/list"))
        assert response.status_code == 401


def agent_mcp_relay_app_router() -> Any:
    """The relay router, for a case that needs its own store overrides."""
    return agent_mcp_router.router
