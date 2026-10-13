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

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.storage import AllCapabilities, OneFake

from cloud_agent.api import mcp_relay as agent_mcp_router
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp import runtime as mcp_runtime
from cloud_agent.mcp.host import ContentBlock, Session, ToolResult
from cloud_agent.mcp.protocol import Handshake
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.ports.web_search import WebSearchAdmission
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
    # The target above is loopback and plain HTTP, so the deployment has to
    # admit both for the guard to let the per-connection route through.
    monkeypatch.setenv("AGENT_MCP_ALLOW_PRIVATE_TARGETS", "1")
    monkeypatch.setenv("AGENT_MCP_ALLOW_PLAIN_HTTP", "1")
    monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: False)
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
        # A loopback literal: the relay validates the target it dials, and a
        # name that does not resolve is refused before the stub host sees it.
        "url": "http://127.0.0.1:9/mcp",
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
    connection_id: int,
    *tools: str,
    resources: tuple[str, ...] = (),
    prompts: tuple[str, ...] = (),
    partial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A discovery result offering exactly what it is given."""
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
        "resources": [
            {"name": uri, "description": f"{uri} on {connection_id}", "input_schema": None}
            for uri in resources
        ],
        "resource_templates": [],
        "prompts": [
            {"name": name, "description": f"{name} on {connection_id}"} for name in prompts
        ],
        "support": partial or {},
        "discovered_at": None,
    }


class FakeStore(AllCapabilities):
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
        self.wait_action = "expired"
        self.finished = []
        self.invocation_created = asyncio.Event()
        self.search_admission = WebSearchAdmission(call_id="search-call-1", reason="admitted")
        self.search_admission_error = False
        self.search_acquired: list[dict[str, Any]] = []
        self.search_released: list[str] = []

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

    async def claim_mcp_invocation(
        self, *, invocation_id: int, **fence: Any
    ) -> dict[str, Any] | None:
        if self.approved is None or int(self.approved["id"]) != int(invocation_id):
            return None
        self.claimed.append(invocation_id)
        return {**self.approved, "status": "dispatching", "dispatch_generation": 1}

    async def create_mcp_invocation(self, **kwargs: Any) -> dict[str, Any]:
        invocation = {"id": 41 + len(self.created_invocations), "status": "pending", **kwargs}
        self.created_invocations.append(invocation)
        self.invocation_created.set()
        return invocation

    async def get_mcp_invocation(self, *, invocation_id):
        if self.approved is not None and self.approved["id"] == invocation_id:
            return self.approved
        row = next(item for item in self.created_invocations if item["id"] == invocation_id)
        return {**row, "status": self.wait_action}

    async def expire_mcp_invocation(self, *, invocation_id):
        row = next(item for item in self.created_invocations if item["id"] == invocation_id)
        row["status"] = "expired"
        return True

    async def finish_mcp_invocation(self, **kwargs):
        self.finished.append(kwargs)
        return True

    async def acquire_web_search(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> WebSearchAdmission:
        if self.search_admission_error:
            raise RuntimeError("web search admission storage unavailable")
        self.search_acquired.append(
            {
                "job_id": job_id,
                "attempt_id": attempt_id,
                "lease_generation": lease_generation,
            }
        )
        return self.search_admission

    async def release_web_search(self, *, call_id: str) -> bool:
        self.search_released.append(call_id)
        return True


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
        self.read: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
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

    async def read_resource(self, session: Session, *, uri: str) -> dict[str, Any]:
        self.read.append({"uri": uri})
        return {"contents": [{"uri": uri, "text": "resource body"}]}

    async def get_prompt(self, session: Session, **kwargs: Any) -> dict[str, Any]:
        self.prompts.append(kwargs)
        return {
            "description": "a prompt",
            "messages": [{"role": "user", "content": {"type": "text", "text": "hello"}}],
        }


@pytest.fixture()
def host(monkeypatch: pytest.MonkeyPatch) -> StubHost:
    """Replace the process-wide host and restore it afterwards."""
    stub = StubHost()
    monkeypatch.setattr(mcp_runtime, "_HOST_INSTANCE", stub)
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


def _catalogue(*connections: str) -> list[str]:
    """The names the merged catalogue advertises, in listing order.

    Every tool is a *connection's* tool now. The platform used to merge two of
    its own goal tools into this surface; the report protocol moved into the
    turn's final message instead, so a catalogue is exactly the connections'
    namespaced tools and nothing else.
    """
    return sorted(connections)


@pytest.fixture()
async def client(store: FakeStore) -> AsyncClient:
    """Mount the relay router against the fake store."""
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_storage] = lambda: OneFake(store)
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
        assert _tool_names(response) == _catalogue("docs__search", "github__get_issue")

    async def test_a_capability_the_session_did_not_enable_is_not_advertised(
        self, client: AsyncClient
    ) -> None:
        """The merged surface is not a way around a session's own selection."""
        response = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert "github__delete_everything" not in _tool_names(response)

    @pytest.mark.parametrize(
        "unavailable",
        [
            {"deleted_at": "2026-01-01T00:00:00Z"},
            {"enabled": False},
            {"status": "error", "error_kind": "unreachable"},
        ],
    )
    async def test_a_broken_connection_is_skipped_and_the_rest_survive(
        self, client: AsyncClient, unavailable
    ) -> None:
        """Losing every tool is a worse outcome than the failure that caused it."""
        store = FakeStore(
            connections={
                7: _connection(7, "GitHub"),
                8: _connection(8, "Docs", **unavailable),
            }
        )
        app = FastAPI()
        app.include_router(agent_mcp_relay_app_router())
        app.dependency_overrides[get_storage] = lambda: OneFake(store)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert _tool_names(response) == _catalogue("github__get_issue")

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
        # Two connections, each advertising one tool under the same presented
        # name: the catalogue deduplicates by name, so two is the whole surface.
        assert len(set(_tool_names(listing))) == 2
        name = next(
            item["name"] for item in _result(listing)["tools"] if item["description"] == "c on 7"
        )

        call = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name=name, arguments={}),
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
        app.dependency_overrides[get_storage] = lambda: OneFake(store)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(
                _HOST_URL,
                json=_rpc("tools/call", name="github__get_issue", arguments={"number": 7}),
                headers=_auth(),
            )
        assert response.status_code == 200
        assert "expired" in response.json()["error"]["message"]
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
        app.dependency_overrides[get_storage] = lambda: OneFake(store)
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

    async def test_every_surface_this_endpoint_answers_is_advertised(
        self, client: AsyncClient
    ) -> None:
        """A well-behaved client asks for what the handshake claimed, and no more.

        This surface answers ``tools/*``, ``resources/*`` and ``prompts/*``. A
        handshake that named only ``tools`` left the other two never asked for,
        and a capability the platform serves but does not claim is, from the
        client's side, indistinguishable from one it does not have.
        """
        response = await client.post(
            _HOST_URL,
            json=_rpc("initialize", protocolVersion="2025-11-25"),
            headers=_auth(),
        )
        capabilities = _result(response)["capabilities"]
        assert set(capabilities) == {"tools", "resources", "prompts"}
        # The sub-flags are the honest half: this surface is request/response,
        # so it can neither push a change nor hold a subscription open.
        assert capabilities["resources"] == {"subscribe": False, "listChanged": False}
        assert capabilities["prompts"] == {"listChanged": False}

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
        response = await client.post(_HOST_URL, json=_rpc("completion/complete"), headers=_auth())
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
        assert _tool_names(merged) == _catalogue("host__get_issue")

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


class TestTheRequestBounds:
    """One request cannot make the relay every tenant shares hold or run without limit."""

    async def test_a_declared_length_over_the_bound_is_refused_unread(
        self, client: AsyncClient, host: StubHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body announced as too large is refused before any of it is read."""
        monkeypatch.setattr(agent_mcp_router, "MAX_RELAY_BODY_BYTES", 64)
        body = json.dumps(
            _rpc("tools/call", name="github__get_issue", arguments={"pad": "x" * 64})
        ).encode()

        response = await client.post(
            _HOST_URL, content=body, headers={**_auth(), "content-type": "application/json"}
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == -32600
        assert host.called == []

    async def test_a_chunked_body_stops_being_read_at_the_bound(
        self, client: AsyncClient, host: StubHost, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body with no declared length is measured as it arrives, not afterwards."""
        monkeypatch.setattr(agent_mcp_router, "MAX_RELAY_BODY_BYTES", 64)
        produced: list[int] = []

        async def endless() -> Any:
            for _ in range(1_000):
                produced.append(32)
                yield b" " * 32

        response = await client.post(_HOST_URL, content=endless(), headers=_auth())

        assert response.status_code == 413
        assert sum(produced) < 1_000 * 32
        assert host.called == []

    async def test_a_batch_over_the_cap_runs_nothing(
        self, client: AsyncClient, host: StubHost
    ) -> None:
        """An oversized batch is refused whole, before its first message runs."""
        batch = [
            _rpc("tools/call", index, name="github__get_issue")
            for index in range(agent_mcp_router.MAX_RELAY_BATCH_MESSAGES + 1)
        ]

        response = await client.post(_HOST_URL, json=batch, headers=_auth())

        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32600
        assert host.called == []

    async def test_a_batch_at_the_cap_is_answered(self, client: AsyncClient) -> None:
        """The cap is a ceiling, not a ban on batching."""
        batch = [_rpc("ping", index) for index in range(agent_mcp_router.MAX_RELAY_BATCH_MESSAGES)]

        response = await client.post(_HOST_URL, json=batch, headers=_auth())

        assert response.status_code == 200
        assert len(response.json()) == agent_mcp_router.MAX_RELAY_BATCH_MESSAGES


def agent_mcp_relay_app_router() -> Any:
    """The relay router, for a case that needs its own store overrides."""
    return agent_mcp_router.router


class FakeUpstream:
    """A stand-in MCP server, at the JSON-RPC level.

    The merged endpoint is a *client* of the servers behind it, so the only way
    to know the host path works is to let it run against something that speaks
    the protocol back. The transport is mocked rather than the host, which is
    what keeps the protocol, the era detection and the result rendering inside
    the test's blast radius.
    """

    def __init__(self, tools: dict[str, str] | None = None) -> None:
        """Serve ``tools`` (name to reply text) from one server."""
        self.tools = tools or {"get_issue": "issue 7: a bug"}
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC POST the way a Streamable HTTP server would."""
        body = json.loads(request.content or b"{}")
        self.calls.append({"method": body.get("method"), "params": body.get("params")})
        method = body.get("method")
        if method == "initialize":
            result: Any = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": f"{name} from the fake",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                    for name in self.tools
                ]
            }
        elif method == "tools/call":
            name = str((body.get("params") or {}).get("name") or "")
            result = {"content": [{"type": "text", "text": self.tools.get(name, "?")}]}
        else:
            # A server that predates the stateless revision says "no such
            # method" with the ordinary JSON-RPC code, and that is what tells
            # the era detection to fall back to the handshake rather than
            # conclude it is talking to a modern server that refuses this.
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32601, "message": "method not found"},
                },
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": result},
            headers={"content-type": "application/json"},
        )


@pytest.fixture()
def real_host(monkeypatch: pytest.MonkeyPatch) -> FakeUpstream:
    """Drive the merged endpoint through the real host against a fake server.

    Two things are arranged rather than mocked away. The connection's URL is an
    https loopback literal, so the egress check keeps its default scheme rule
    and needs no resolver; only the private-address rule is widened, the way a
    deployment with a private connector bridge widens it. The transport is then
    mocked, so everything above it -- version negotiation, ``tools/list``, the
    call itself, the result rendering -- is the code that runs in production.
    """
    upstream = FakeUpstream()
    monkeypatch.setenv("AGENT_MCP_ALLOW_PRIVATE_TARGETS", "1")
    monkeypatch.setattr(
        "cloud_agent.mcp.transports.build_client",
        lambda config: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    # ``_host()`` caches its instance; None makes it build one under the patched
    # policy, and monkeypatch restores the global afterwards.
    monkeypatch.setattr(mcp_runtime, "_HOST_INSTANCE", None)
    return upstream


class TestAgainstARealProtocolHost:
    """The merged endpoint driving the host for real, not a stub of it."""

    def _store_with_loopback(self) -> FakeStore:
        """One connection whose address the widened policy allows."""
        return FakeStore(
            connections={7: _connection(7, "GitHub", url="https://127.0.0.1:9/mcp")},
            bindings=[
                _binding(
                    7,
                    "github",
                    rules={"get_issue": {"policy": "allow", "fingerprint": ""}},
                )
            ],
            snapshots={7: _snapshot(7, "get_issue")},
        )

    async def test_a_call_travels_through_the_host_to_the_upstream(
        self, real_host: FakeUpstream
    ) -> None:
        """The namespaced name reaches the upstream as its own, and back."""
        store = self._store_with_loopback()
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_storage] = lambda: OneFake(store)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(
                _HOST_URL,
                json=_rpc("tools/call", name="github__get_issue", arguments={"number": 7}),
                headers=_auth(),
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "result" in body, body
        assert body["result"]["content"][0]["text"] == "issue 7: a bug"
        # The upstream was spoken to by the host, in its own vocabulary.
        methods = [call["method"] for call in real_host.calls]
        assert "initialize" in methods
        called = next(call for call in real_host.calls if call["method"] == "tools/call")
        assert called["params"]["name"] == "get_issue"
        assert called["params"]["arguments"] == {"number": 7}

    async def test_the_catalogue_the_host_reports_is_namespaced(
        self, real_host: FakeUpstream
    ) -> None:
        """Discovery goes through the host too, and the alias is the prefix."""
        store = self._store_with_loopback()
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_storage] = lambda: OneFake(store)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert [tool["name"] for tool in _result(response)["tools"]] == _catalogue(
            "github__get_issue"
        )


class TestResourcesAndPrompts:
    """The other two surfaces, under their own scopes.

    The design gives resources and prompts separate reading and selection scopes
    so that they are not a way round tool authorization. These pin that they are
    resolved through the same catalogue and decided by the same function, and
    that what a session did not enable is neither listed nor readable.
    """

    def _store(self, **overrides: Any) -> FakeStore:
        """One connection offering a resource and a prompt, both enabled."""
        return FakeStore(
            snapshots={
                7: _snapshot(
                    7,
                    "get_issue",
                    resources=("repo://notes", "repo://secrets"),
                    prompts=("summarize",),
                )
            },
            bindings=[
                _binding(
                    7,
                    "github",
                    rules={"get_issue": {"policy": "allow", "fingerprint": ""}},
                    resource_rules={"repo://notes": "allow", "repo://secrets": "deny"},
                    prompt_rules={"summarize": "allow"},
                )
            ],
            **overrides,
        )

    async def test_only_enabled_resources_are_listed(self, host: StubHost) -> None:
        """Only enabled resources receive connection-scoped public URIs."""
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_storage] = lambda: self._store()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(_HOST_URL, json=_rpc("resources/list"), headers=_auth())
        assert [entry["uri"] for entry in _result(response)["resources"]] == [
            "mcp-resource://7/repo://notes"
        ]

    async def test_a_resource_outside_the_scope_is_not_readable(self, host: StubHost) -> None:
        """Refused without dialling: the scope is checked before the call."""
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_storage] = lambda: self._store()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(
                _HOST_URL,
                json=_rpc("resources/read", uri="mcp-resource://7/repo://secrets"),
                headers=_auth(),
            )
        assert "error" in response.json(), response.json()
        assert host.read == []
        assert host.opened == []

    async def test_an_enabled_resource_is_read_through_the_host(self, host: StubHost) -> None:
        """The upstream URI is what travels; the scope is what allowed it."""
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_storage] = lambda: self._store()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(
                _HOST_URL,
                json=_rpc("resources/read", uri="mcp-resource://7/repo://notes"),
                headers=_auth(),
            )
        assert _result(response)["contents"][0]["text"] == "resource body"
        assert host.read == [{"uri": "repo://notes"}]
        assert host.opened[-1]["connection"].connection_id == "7"

    async def test_a_prompt_is_listed_namespaced_and_fetched_by_its_own_name(
        self, host: StubHost
    ) -> None:
        """Two connections may both offer `summarize`; only one name is callable.

        A prompt has no URI to disambiguate it, so it is namespaced like a tool
        and mapped back on the way out -- the same reason the tool catalogue is
        keyed by the presented name rather than the server's.
        """
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_storage] = lambda: self._store()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            listed = await api.post(_HOST_URL, json=_rpc("prompts/list"), headers=_auth())
            fetched = await api.post(
                _HOST_URL,
                json=_rpc("prompts/get", name="github__summarize", arguments={"style": "tldr"}),
                headers=_auth(),
            )
        assert [entry["name"] for entry in _result(listed)["prompts"]] == ["github__summarize"]
        assert _result(fetched)["messages"][0]["content"] == {"type": "text", "text": "hello"}
        assert host.prompts[-1]["name"] == "summarize"
        assert host.prompts[-1]["arguments"] == {"style": "tldr"}

    async def test_an_unknown_prompt_name_is_refused(self, host: StubHost) -> None:
        """A name that is not in the catalogue is not a server to guess at."""
        app = FastAPI()
        app.include_router(agent_mcp_router.router)
        app.dependency_overrides[get_storage] = lambda: self._store()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(
                _HOST_URL, json=_rpc("prompts/get", name="summarize"), headers=_auth()
            )
        assert "prompts/list" in response.json()["error"]["message"]
        assert host.prompts == []


@pytest.mark.parametrize("kind", ["tool", "prompt"])
@pytest.mark.parametrize("change", ["reorder", "disable"])
async def test_a_cached_name_cannot_move_to_another_connection(client, store, host, kind, change):
    """A separator collision cannot redirect a cached call after catalogue changes."""
    tools = kind == "tool"
    store.bindings = [
        _binding(7, "a__b", rules={"c": {"policy": "allow"}}, prompt_rules={"c": "allow"}),
        _binding(8, "a", rules={"b__c": {"policy": "allow"}}, prompt_rules={"b__c": "allow"}),
    ]
    store.snapshots = {
        7: _snapshot(7, "c", prompts=("c",)),
        8: _snapshot(8, "b__c", prompts=("b__c",)),
    }
    listing = "tools/list" if tools else "prompts/list"
    method = "tools/call" if tools else "prompts/get"
    first = await client.post(_HOST_URL, json=_rpc(listing), headers=_auth())
    entries = _result(first)["tools" if tools else "prompts"]
    cached_name = next(item["name"] for item in entries if item["description"] == "c on 7")
    if change == "reorder":
        store.bindings.reverse()
    else:
        store.bindings[0]["enabled"] = False
    response = await client.post(
        _HOST_URL, json=_rpc(method, name=cached_name, arguments={}), headers=_auth()
    )
    if change == "disable":
        assert "error" in response.json()
        assert host.opened == []
    else:
        assert "result" in response.json()
        assert host.opened[-1]["connection"].connection_id == "7"
        assert (host.called if tools else host.prompts)[-1]["name"] == "c"


@pytest.mark.parametrize("names", [("a.b", "a/b"), ("x" * 70 + "a", "x" * 70 + "b")])
async def test_disabling_a_tool_does_not_reassign_its_cached_name(client, store, host, names):
    """Another tool cannot inherit a name that collided after sanitizing or shortening."""
    first, second = names
    store.bindings = [_binding(7, "github", rules={name: {"policy": "allow"} for name in names})]
    store.snapshots = {7: _snapshot(7, *names)}
    listing = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    cached_name = next(
        item["name"]
        for item in _result(listing)["tools"]
        if item["description"].endswith(f"{first} on 7")
    )
    store.bindings[0]["rules"][first]["policy"] = "deny"
    response = await client.post(
        _HOST_URL, json=_rpc("tools/call", name=cached_name, arguments={}), headers=_auth()
    )
    assert "error" in response.json()
    assert host.called == []
    listing = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    # By description, not by position: the catalogue also carries the
    # platform's own tools, and which of them sorts first is not what this
    # case is about.
    allowed_name = next(
        item["name"]
        for item in _result(listing)["tools"]
        if item["description"].endswith(f"{second} on 7")
    )
    response = await client.post(
        _HOST_URL, json=_rpc("tools/call", name=allowed_name, arguments={}), headers=_auth()
    )
    assert "result" in response.json()
    assert host.called[-1]["name"] == second


@pytest.mark.parametrize("state", ["binding-disabled", "connection-disabled", "disconnected"])
async def test_inactive_connection_never_refreshes_or_delays_healthy_tools(
    client, store, host, monkeypatch, state
):
    if state == "binding-disabled":
        store.bindings[0]["enabled"] = False
    elif state == "connection-disabled":
        store.connections[7]["status"] = "disabled"
    else:
        store.connections[7]["deleted_at"] = "disconnected"
    refreshed = []

    async def refresh(repository, *, row, policy):
        refreshed.append(row["id"])
        assert row["id"] == 8, "inactive connection must not enter refresh"
        return row

    monkeypatch.setattr(agent_mcp_router, "ensure_fresh", refresh)
    listed = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    assert _tool_names(listed) == _catalogue("docs__search")
    assert refreshed == []
    for _ in range(2):
        response = await client.post(
            _HOST_URL, json=_rpc("tools/call", name="docs__search", arguments={}), headers=_auth()
        )
        assert "result" in response.json()
    assert refreshed == [8, 8]
    assert host.called[-1]["name"] == "search"


@pytest.mark.parametrize("surface", ["tool", "resource", "prompt", "direct"])
async def test_legacy_duplicate_aliases_cannot_route_to_the_first_connection(
    client, store, host, surface
):
    store.bindings.append(_binding(9, "github"))
    request = {
        "tool": _rpc("tools/call", name="github__get_issue"),
        "resource": _rpc("resources/read", uri="mcp-resource://7/repo://notes"),
        "prompt": _rpc("prompts/get", name="github__summary"),
        "direct": _rpc("tools/call", name="get_issue"),
    }[surface]
    listed = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
    assert _tool_names(listed) == _catalogue("docs__search")
    response = await client.post(
        f"{_HOST_URL}/github" if surface == "direct" else _HOST_URL,
        json=request,
        headers=_auth(),
    )
    assert response.status_code == 403 or "error" in response.json()
    assert host.opened == []
    assert host.called == []


class TestBuiltInWebSearch:
    """The platform tool shares the fenced host without becoming a fake connection."""

    async def test_unconfigured_search_is_neither_listed_nor_callable(
        self, client: AsyncClient, host: StubHost
    ) -> None:
        listing = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert "web_search" not in _tool_names(listing)

        call = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="web_search", arguments={"query": "latest release"}),
            headers=_auth(),
        )
        assert "error" in call.json()
        assert host.opened == []
        assert host.called == []

    async def test_configured_search_has_a_bounded_public_schema(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: True)

        listing = await client.post(_HOST_URL, json=_rpc("tools/list"), headers=_auth())
        assert _tool_names(listing).count("web_search") == 1
        tool = next(item for item in _result(listing)["tools"] if item["name"] == "web_search")

        assert tool["inputSchema"]["required"] == ["query"]
        assert tool["inputSchema"]["properties"]["num_results"]["maximum"] == 10
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "Serper.dev" in tool["description"]
        assert "Brave Search" in tool["description"]
        assert "Parallel Search" in tool["description"]

    async def test_search_call_uses_the_platform_dispatch_not_a_user_connection(
        self,
        client: AsyncClient,
        store: FakeStore,
        host: StubHost,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: True)

        async def search(arguments):
            calls.append(arguments)
            return {
                "query": arguments["query"],
                "organic": [{"title": "Result", "link": "https://example.com"}],
            }

        monkeypatch.setattr(agent_mcp_router.web_search, "search", search)
        response = await client.post(
            _HOST_URL,
            json=_rpc(
                "tools/call",
                name="web_search",
                arguments={"query": "current package version", "num_results": 3},
            ),
            headers=_auth(),
        )

        assert response.status_code == 200
        assert calls == [{"query": "current package version", "num_results": 3}]
        assert json.loads(_result(response)["content"][0]["text"]) == {
            "query": "current package version",
            "organic": [{"title": "Result", "link": "https://example.com"}],
        }
        assert store.search_acquired == [
            {
                "job_id": _JOB,
                "attempt_id": _ATTEMPT,
                "lease_generation": _LEASE,
            }
        ]
        assert store.search_released == ["search-call-1"]
        assert host.opened == []
        assert host.called == []

    async def test_provider_failure_is_a_tool_error_with_no_provider_body(
        self,
        client: AsyncClient,
        store: FakeStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: True)

        async def failed(arguments):
            del arguments
            raise agent_mcp_router.web_search.WebSearchError(
                "Web search is rate limited. Try again later.",
                kind="rate_limited",
            )

        monkeypatch.setattr(agent_mcp_router.web_search, "search", failed)
        response = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="web_search", arguments={"query": "anything"}),
            headers=_auth(),
        )

        assert _result(response)["isError"] is True
        assert _result(response)["content"] == [
            {"type": "text", "text": "Web search is rate limited. Try again later."}
        ]
        assert store.search_released == ["search-call-1"]

    async def test_invalid_arguments_consume_no_shared_key_admission(
        self,
        client: AsyncClient,
        store: FakeStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: True)

        response = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="web_search", arguments={"unexpected": True}),
            headers=_auth(),
        )

        assert response.json()["error"]["code"] == agent_mcp_router.INVALID_PARAMS
        assert store.search_acquired == []
        assert store.search_released == []

    @pytest.mark.parametrize(
        ("provider_name", "arguments"),
        [
            (
                "brave",
                {"query": "current package version", "language": "en-us"},
            ),
            (
                "parallel",
                {"query": "current package version", "language": "en"},
            ),
            (
                "parallel",
                {"query": "current package version", "country": "sg"},
            ),
            ("parallel", {"query": "x" * 201}),
        ],
    )
    async def test_unsupported_provider_options_consume_no_shared_key_admission(
        self,
        client: AsyncClient,
        store: FakeStore,
        monkeypatch: pytest.MonkeyPatch,
        provider_name: str,
        arguments: dict[str, Any],
    ) -> None:
        monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: True)
        monkeypatch.setattr(
            agent_mcp_router.web_search,
            "selected_provider",
            lambda: provider_name,
        )

        response = await client.post(
            _HOST_URL,
            json=_rpc(
                "tools/call",
                name="web_search",
                arguments=arguments,
            ),
            headers=_auth(),
        )

        assert response.json()["error"]["code"] == agent_mcp_router.INVALID_PARAMS
        assert store.search_acquired == []
        assert store.search_released == []

    async def test_usage_refusal_does_not_call_a_provider_or_user_connection(
        self,
        client: AsyncClient,
        store: FakeStore,
        host: StubHost,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: True)
        store.search_admission = WebSearchAdmission(call_id=None, reason="user_rate_limited")

        async def unexpected(arguments):
            raise AssertionError(f"rate-limited search reached a provider: {arguments!r}")

        monkeypatch.setattr(agent_mcp_router.web_search, "search", unexpected)
        response = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="web_search", arguments={"query": "anything"}),
            headers=_auth(),
        )

        assert _result(response)["isError"] is True
        assert "usage limit" in _result(response)["content"][0]["text"]
        assert store.search_released == []
        assert host.opened == []
        assert host.called == []

    async def test_admission_storage_failure_fails_closed(
        self,
        client: AsyncClient,
        store: FakeStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(agent_mcp_router.web_search, "configured", lambda: True)
        store.search_admission_error = True

        async def unexpected(arguments):
            raise AssertionError(f"unadmitted search reached a provider: {arguments!r}")

        monkeypatch.setattr(agent_mcp_router.web_search, "search", unexpected)
        response = await client.post(
            _HOST_URL,
            json=_rpc("tools/call", name="web_search", arguments={"query": "anything"}),
            headers=_auth(),
        )

        assert _result(response)["isError"] is True
        assert _result(response)["content"][0]["text"] == (
            "Web search is temporarily unavailable. Try again."
        )
        assert store.search_released == []

    async def test_cancellation_releases_the_admission(
        self, store: FakeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = asyncio.Event()

        async def blocked(arguments):
            del arguments
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(agent_mcp_router.web_search, "search", blocked)
        identity = await store.resolve_model_credential(
            job_id=_JOB, attempt_id=_ATTEMPT, lease_generation=_LEASE
        )
        assert identity is not None
        task = asyncio.create_task(
            agent_mcp_router._host_web_search(
                {"query": "anything"},
                request_id=1,
                identity=identity,
                store=store,
                authorization=_auth()["Authorization"],
                stateless=False,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.search_released == ["search-call-1"]
