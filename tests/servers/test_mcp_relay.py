"""API-surface tests for this service's MCP relay endpoint.

The upstream is a ``MockTransport``, so these pin what actually crosses each
boundary: which credential reaches the MCP server, which never reaches the
sandbox, and which requests are refused before they leave the relay at all.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import mcp_relay as agent_mcp_router
from cloud_agent.api.deps import get_agent_job_store
from cloud_agent.mcp_registry import McpRegistry, McpServer
from cloud_agent.tokens import SCOPE_FULL, SCOPE_MCP, SCOPE_MODEL, mint_worker_token

pytestmark = pytest.mark.asyncio

_FENCE = {"job_id": "ajob_abc", "attempt_id": 5, "lease_generation": 2}
_UPSTREAM = "https://mcp.example.com/mcp"
_UPSTREAM_SECRET = "Bearer ghs_deployment_secret"


@pytest.fixture(autouse=True)
def _api_key_secret(monkeypatch):
    """Provide the signing secret the capability tokens are derived from."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "mcp-relay-secret-value-32-bytes-x")
    yield


class FakeJobStore:
    """Resolves one live fence, mirroring the real SQL predicate."""

    def __init__(self, *, live: bool = True, granted: list[str] | None = None) -> None:
        self.live = live
        self.granted = ["github"] if granted is None else granted

    async def resolve_model_credential(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> dict[str, Any] | None:
        matches = (job_id, attempt_id, lease_generation) == (
            _FENCE["job_id"],
            _FENCE["attempt_id"],
            _FENCE["lease_generation"],
        )
        if not (self.live and matches):
            return None
        return {
            "user_id": "owner-1",
            "role": "internal",
            "job_id": job_id,
            "mcp_servers": list(self.granted),
            "model": "glm-5.1",
        }

    async def list_mcp_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """The owner's stored connections, for cases that use real resolution."""
        return [
            {
                "server": "github",
                "account_label": "octocat",
                "access_token_ciphertext": "ciphertext-that-needs-a-key",
                "refresh_token_ciphertext": None,
                "expires_at": None,
            }
        ]


class Upstream:
    """A stand-in MCP server that records what the gateway sent it."""

    def __init__(
        self,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        status_code: int = 200,
    ):
        self.requests: list[httpx.Request] = []
        self.body = body if body is not None else b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.content_type = content_type
        self.status_code = status_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer with the canned body."""
        self.requests.append(request)
        return httpx.Response(
            self.status_code,
            content=self.body,
            headers={"content-type": self.content_type, "mcp-session-id": "sess-1"},
        )


def _registry(tools: frozenset[str] = frozenset({"get_issue"})) -> McpRegistry:
    """A registry with one credentialed, allowlisted server."""
    return McpRegistry(
        servers={
            "github": McpServer(
                name="github",
                url=_UPSTREAM,
                headers={"Authorization": _UPSTREAM_SECRET},
                tools=tools,
            )
        }
    )


def _patch_registry(monkeypatch, registry: McpRegistry, resolved_for: list[str] | None = None):
    """Resolve every user to ``registry``, recording who was asked about.

    Servers resolve per user now, so the relay has to name one — and these
    tests care *which*: the caller is a sandbox, and the credential it spends
    must be the job owner's. ``resolved_for`` is how that is observed.
    """

    async def _resolve(store, *, user_id, cipher=None):
        if resolved_for is not None:
            resolved_for.append(user_id)
        return registry

    monkeypatch.setattr(agent_mcp_router, "registry_for_user", _resolve)


@pytest.fixture()
def upstream():
    """The stand-in MCP server."""
    return Upstream()


@pytest.fixture()
def store():
    """A store resolving the test fence."""
    return FakeJobStore()


@pytest_asyncio.fixture()
async def client(monkeypatch, store, upstream):
    """Mount the proxy router with a mocked upstream and a fixed registry."""
    _patch_registry(monkeypatch, _registry())
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The proxy also accepts inference grants, which resolve through the
    # operational store. These legacy cases present ajt tokens and never
    # reach it, but the dependency still has to resolve.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def _token() -> str:
    """Mint the model-scoped token the sandbox actually carries."""
    return mint_worker_token(**_FENCE, scope=SCOPE_MCP)


def _auth() -> dict[str, str]:
    """Authorization header for the sandbox's token."""
    return {"Authorization": f"Bearer {_token()}"}


def _rpc(method: str, **params: Any) -> dict[str, Any]:
    """One JSON-RPC request body."""
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


async def test_the_upstream_credential_replaces_the_sandbox_token(client, upstream):
    """The MCP server sees our credential; the job token stops at the gateway."""
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 200

    sent = upstream.requests[0]
    assert sent.headers["authorization"] == _UPSTREAM_SECRET
    assert _token() not in str(sent.headers)


async def test_the_upstream_address_never_reaches_the_sandbox(client):
    """The agent talks to the gateway and learns nothing about who answers."""
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert "mcp.example.com" not in json.dumps(dict(response.headers))
    assert "mcp.example.com" not in response.text
    # The session id has to survive or streamable HTTP does not work at all.
    assert response.headers["mcp-session-id"] == "sess-1"


async def test_a_registry_header_replaces_the_sandbox_header_rather_than_joining_it(
    monkeypatch, store, upstream
):
    """Case must not smuggle a second value through under the same name.

    Starlette lowercases inbound header names and a registry writes them the
    way a human does, so a case-sensitive merge keeps both and httpx sends
    both. The dangerous instance of this is Authorization, where the sandbox's
    own token would ride upstream beside the credential meant to replace it.
    """
    registry = McpRegistry(
        servers={
            "github": McpServer(
                name="github",
                url=_UPSTREAM,
                headers={"Mcp-Session-Id": "registry-value"},
                tools=frozenset({"get_issue"}),
            )
        }
    )
    _patch_registry(monkeypatch, registry)
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The proxy also accepts inference grants, which resolve through the
    # operational store. These legacy cases present ajt tokens and never
    # reach it, but the dependency still has to resolve.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        await api.post(
            "/v1/agent/mcp/github",
            json=_rpc("initialize"),
            headers={**_auth(), "mcp-session-id": "sandbox-value"},
        )

    assert upstream.requests[0].headers.get_list("mcp-session-id") == ["registry-value"]


async def test_a_server_the_job_was_not_granted_is_refused(monkeypatch, store, client, upstream):
    """A token reaches its job's servers, not every server configured here."""
    store.granted = []
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 403
    assert upstream.requests == []


async def test_a_dead_fence_stops_reaching_tools(store, client, upstream):
    """Cancelling a job revokes its tools exactly as it revokes inference."""
    store.live = False
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 401
    assert upstream.requests == []


async def test_an_ordinary_api_key_is_not_a_job_token(client, upstream):
    """This endpoint is for sandboxes; a user key is refused outright."""
    response = await client.post(
        "/v1/agent/mcp/github",
        json=_rpc("initialize"),
        headers={"Authorization": "Bearer hyi-not-an-agent-token"},
    )
    assert response.status_code == 401
    assert upstream.requests == []


async def test_a_blocked_tool_never_leaves_the_gateway(client, upstream):
    """The allowlist is enforced before the request is made, not after."""
    response = await client.post(
        "/v1/agent/mcp/github",
        json=_rpc("tools/call", name="create_issue"),
        headers=_auth(),
    )
    # A JSON-RPC error, not an HTTP one: the hop worked and the call failed, so
    # the CLI reports a failed tool instead of dropping the whole server.
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601
    assert upstream.requests == []


async def test_an_allowed_tool_is_forwarded(client, upstream):
    """The allowlist refuses what it names and nothing else."""
    response = await client.post(
        "/v1/agent/mcp/github", json=_rpc("tools/call", name="get_issue"), headers=_auth()
    )
    assert response.status_code == 200
    assert len(upstream.requests) == 1


async def test_the_catalogue_is_filtered_on_the_way_back(monkeypatch, store, upstream):
    """A tool outside the allowlist is never advertised to the model."""
    upstream.body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "get_issue"}, {"name": "create_issue"}]},
        }
    ).encode()
    _patch_registry(monkeypatch, _registry())
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    # The proxy also accepts inference grants, which resolve through the
    # operational store. These legacy cases present ajt tokens and never
    # reach it, but the dependency still has to resolve.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        response = await api.post("/v1/agent/mcp/github", json=_rpc("tools/list"), headers=_auth())
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == ["get_issue"]


async def test_tools_resolve_against_the_job_owner_not_the_caller(monkeypatch, store, upstream):
    """The sandbox does not get to say whose tools it is spending.

    The caller here is a sandbox holding a job token; the owner is whoever the
    fence says the job belongs to. Resolution must ask about the owner, because
    a per-user registry asked about anyone else is one user's credential
    spending on another user's job.
    """
    resolved_for: list[str] = []
    _patch_registry(monkeypatch, _registry(), resolved_for)
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        response = await api.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 200
    assert resolved_for == ["owner-1"]


@pytest.mark.parametrize("status", [401, 403])
async def test_an_upstream_refusing_the_credential_is_attributed_to_the_server(
    client, upstream, caplog, status
):
    """The third way a connection dies, and the only one visible from here.

    Expiry and an unreadable credential are caught during resolution; an
    upstream that has stopped accepting a live-looking token can only be found
    by asking it. Unattributed, that reads as the sandbox misbehaving — which
    is how an hour of silent 401s went unnoticed before the gateway's route
    learned to name the server (hybridInference#1199).

    The status still reaches the agent unchanged: it is the answer to the
    agent's own request, and rewriting it would hide a real reply from the CLI.
    """
    upstream.status_code = status

    with caplog.at_level("WARNING"):
        response = await client.post(
            "/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth()
        )

    assert response.status_code == status
    rejected = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "agent_mcp_upstream_rejected_credential"
    ]
    assert [(record.server, record.job_id, record.status) for record in rejected] == [
        ("github", _FENCE["job_id"], status)
    ]


async def test_an_upstream_answering_normally_logs_no_credential_warning(client, upstream, caplog):
    """A warning on every reply would be noise nobody reads."""
    with caplog.at_level("WARNING"):
        await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())

    assert not [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "agent_mcp_upstream_rejected_credential"
    ]


async def test_a_lost_encryption_key_is_a_503_and_not_an_unhandled_error(
    monkeypatch, store, upstream
):
    """Real resolution, no key: the documented 503, not a 500 with a stack trace.

    Deliberately does **not** patch ``registry_for_user`` — the bug this pins
    was in the real one, which raised on a missing key while only the composer
    caught it. A deployment that had the key, has connections, and then loses it
    (a rotation gone wrong, a partial rollout) would turn every tool call of
    every running job into a 500. Fail-closed either way; the difference is
    whether the alerts say what happened.
    """
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
    )
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_agent_job_store] = lambda: store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        response = await api.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())

    assert response.status_code == 503
    assert upstream.requests == []


async def test_a_server_the_owner_no_longer_has_reports_it_as_unavailable(
    monkeypatch, client, upstream
):
    """Granted at creation, disconnected since: a 503, not a permission error."""
    _patch_registry(monkeypatch, McpRegistry())
    response = await client.post("/v1/agent/mcp/github", json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 503
    assert upstream.requests == []


# ── The credential boundary ────────────────────────────────────────────
#
# Two credentials reach a sandbox and they buy different things: the gateway's
# `agr` grant buys model calls, an `mcp`-scoped worker token buys tools here.
# Neither service accepts the other's, so a leak is bounded by what that one
# credential was for.


@pytest.mark.parametrize("scope", [SCOPE_FULL, SCOPE_MODEL])
async def test_only_an_mcp_scoped_token_reaches_the_relay(client, scope):
    """**Positive, not "not full".**

    `parse_worker_token` reads a token minted before scopes existed as `full`,
    which is the conservative reading when deciding whether a signature can be
    trusted and exactly the wrong one here — a check written as "not full"
    would admit every such token to the tool surface.

    The runner's own `full` token is refused too. Reporting events and driving
    terminal transitions is not a reason to be able to call somebody's tools.
    """
    token = mint_worker_token(**_FENCE, scope=scope)

    response = await client.post(
        "/v1/agent/mcp/github",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    # `{"detail": {"error": ...}}`, not `{"error": ...}`: this service installs
    # no handler that unwraps FastAPI's envelope, unlike the gateway. Asserted
    # as it actually is rather than as the other service does it, so a reader
    # is not told a shape the wire does not carry.
    assert response.json()["detail"]["error"]["type"] == "insufficient_scope"


async def test_a_gateway_inference_grant_is_not_accepted_here(client):
    """An `agr` grant authorizes models at the gateway and nothing here.

    This service cannot verify one — it holds none of the gateway's signing
    material — so the refusal is structural rather than a policy. Asserted
    anyway: "it happens not to parse" and "we refuse it" fail differently when
    somebody later adds a gateway client to this module.
    """
    response = await client.post(
        "/v1/agent/mcp/github",
        json=_rpc("tools/list"),
        headers={"Authorization": "Bearer agr.cGF5bG9hZA.c2ln"},
    )

    assert response.status_code in (401, 403)


async def test_a_stale_lease_generation_stops_buying_tools(client, store):
    """The fence is read fresh on every call, so revocation is automatic.

    The moment the reaper supersedes the attempt or the owner cancels, the same
    token stops working — there is no key to remember to revoke, which is the
    whole reason this service holds the fence rather than the gateway.
    """
    store.live = False

    response = await client.post("/v1/agent/mcp/github", json=_rpc("tools/list"), headers=_auth())

    assert response.status_code == 401
