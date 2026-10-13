"""API-surface tests for this service's MCP relay endpoint.

The upstream is a ``MockTransport``, so these pin what actually crosses each
boundary: which credential reaches the MCP server, which never reaches the
sandbox, and which calls are refused or held before they leave the relay at all.

Authorization is per binding now. The URL path segment is the *alias* a
conversation chose, the relay reads that job's bindings out of the same fence
lookup that decides whether the token is still live, and it decides a call from
the binding's own policy map. There is no catalogue to consult and no name that
resolves anything on its own.

Two credentials still meet here and must not interchange: the sandbox carries an
``mcp``-scoped worker token, and the upstream gets the connection owner's
credential in its place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.storage import OneFake

from cloud_agent.api import mcp_relay as agent_mcp_router
from cloud_agent.api.deps import get_storage
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent.mcp_proxy import canonical_request
from cloud_agent.tokens import SCOPE_FULL, SCOPE_MCP, SCOPE_MODEL, mint_worker_token

pytestmark = pytest.mark.asyncio

_OWNER = "owner-1"
_OTHER = "someone-else"
_THREAD = "athr_abc"
_JOB = "ajob_abc"
_FENCE = {"job_id": _JOB, "attempt_id": 5, "lease_generation": 2}
# A loopback literal, because the relay now validates the target it dials and a
# name that does not resolve would be refused before the mocked client sees it.
# The policy that admits it is set in the fixture below.
_UPSTREAM = "http://127.0.0.1:9/mcp"
_UPSTREAM_HOST = "127.0.0.1:9"
_UPSTREAM_SECRET = "ghs_deployment_secret"
_ENCRYPTION_KEY = "mcp-relay-test-encryption-key"
_CONNECTION_ID = 7
_ALIAS = "github"

_CIPHER = mcp_cipher(_ENCRYPTION_KEY)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    """Provide the signing secret and the key stored credentials are wrapped under."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "mcp-relay-secret-value-32-bytes-x")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _ENCRYPTION_KEY)
    yield


def _connection(**overrides: Any) -> dict[str, Any]:
    """One stored connection row, as ``resolve_connection`` reads it.

    The full column set rather than the handful a case happens to need: a fake
    that omits a column makes the real reader's ``row.get`` look optional when
    the store always supplies it.
    """
    row: dict[str, Any] = {
        "id": _CONNECTION_ID,
        "user_id": _OWNER,
        "label": "GitHub",
        "template": "github",
        "transport": "streamable-http",
        "location": "remote",
        "url": _UPSTREAM,
        "launch_spec": None,
        "auth_kind": "static",
        "auth_config": {"header_name": "Authorization", "scheme": "Bearer"},
        "connector_id": None,
        "status": "active",
        "error_kind": "none",
        "error_detail": None,
        "access_token_ciphertext": _CIPHER.encrypt(_UPSTREAM_SECRET),
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
        "last_login_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    row.update(overrides)
    return row


def _binding(**overrides: Any) -> dict[str, Any]:
    """One conversation's use of a connection, as the fence lookup returns it."""
    binding: dict[str, Any] = {
        "thread_id": _THREAD,
        "connection_id": _CONNECTION_ID,
        "alias": _ALIAS,
        "enabled": True,
        "selection_pending": False,
        "config_revision": 1,
        "authorization_epoch": 1,
        "capability_revision": 1,
        "policy_revision": 1,
        "rules": {"get_issue": {"policy": "allow", "fingerprint": ""}},
        "resource_rules": {},
        "prompt_rules": {},
        "owner_id": _OWNER,
        "connection_status": "active",
        "deleted_at": None,
        "label": "GitHub",
        "transport": "streamable-http",
        "location": "remote",
    }
    binding.update(overrides)
    return binding


def _snapshot(*names: str, connection_id: int = _CONNECTION_ID) -> dict[str, Any]:
    """One discovery result advertising ``names`` as tools."""
    return {
        "connection_id": connection_id,
        "capability_revision": 1,
        "config_revision": 1,
        "protocol_version": "2026-07-28",
        "server_name": "github",
        "server_version": "1.0",
        "tools": [
            {"name": name, "description": name, "input_schema": {"type": "object"}}
            for name in names
        ],
        "resources": [],
        "resource_templates": [],
        "prompts": [],
        "support": {},
        "discovered_at": None,
    }


def _catalogue(*names: str) -> bytes:
    """An upstream ``tools/list`` result advertising ``names``."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": name, "description": name} for name in names]},
        }
    ).encode()


class FakeJobStore:
    """The store surface the relay reads: a fence, a row, and held calls."""

    def __init__(
        self,
        *,
        live: bool = True,
        connection: dict[str, Any] | None = None,
        bindings: list[dict[str, Any]] | None = None,
        snapshot: dict[str, Any] | None = None,
        approved: dict[str, Any] | None = None,
        claim_race_won: bool = True,
    ) -> None:
        self.live = live
        self.connection = _connection() if connection is None else connection
        self.bindings = [_binding()] if bindings is None else bindings
        self.snapshot = _snapshot("get_issue") if snapshot is None else snapshot
        self.approved = approved
        self.claim_race_won = claim_race_won
        self.created_invocations: list[dict[str, Any]] = []
        self.looked_up: list[dict[str, Any]] = []
        self.claimed: list[int] = []
        self.wait_action = "expired"
        self.finished = []
        self.invocation_created = asyncio.Event()
        self.fenced: list[dict[str, Any]] = []

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
            "user_id": _OWNER,
            "job_id": _JOB,
            "thread_id": _THREAD,
            "mcp_bindings": [dict(binding) for binding in self.bindings],
            "model": "glm-5.1",
        }

    async def get_mcp_connection(
        self, *, connection_id: int, user_id: str | None = None
    ) -> dict[str, Any] | None:
        row = self.connection
        if row is None or int(row["id"]) != int(connection_id):
            return None
        if user_id is not None and row["user_id"] != user_id:
            return None
        return dict(row)

    async def get_mcp_capabilities(self, *, connection_id: int) -> dict[str, Any] | None:
        if self.snapshot is None or int(self.snapshot["connection_id"]) != int(connection_id):
            return None
        return dict(self.snapshot)

    async def find_approved_mcp_invocation(self, **kwargs: Any) -> dict[str, Any] | None:
        self.looked_up.append(kwargs)
        return self.approved

    async def claim_mcp_invocation(
        self, *, invocation_id: int, **fence: Any
    ) -> dict[str, Any] | None:
        # The fence is recorded so a test can assert it travelled; the fake has
        # one approval and no versions to disagree with.
        self.fenced.append(fence)
        if (
            not self.claim_race_won
            or self.approved is None
            or int(self.approved["id"]) != int(invocation_id)
        ):
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


class Upstream:
    """A stand-in MCP server that records what the relay sent it."""

    def __init__(
        self,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        status_code: int = 200,
    ) -> None:
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


@contextlib.asynccontextmanager
async def _relay(monkeypatch, store: FakeJobStore, upstream: Upstream):
    """Mount the relay router with a mocked upstream and the fake store.

    The target is loopback, so the deployment has to admit private plain-HTTP
    targets for the guard to let it through -- which is what the guard is *for*,
    and the two tests that assert a target is refused set them back.
    """
    monkeypatch.setenv("AGENT_MCP_ALLOW_PRIVATE_TARGETS", "1")
    monkeypatch.setenv("AGENT_MCP_ALLOW_PLAIN_HTTP", "1")
    monkeypatch.setattr(
        agent_mcp_router,
        "build_upstream_client",
        # The timeout argument is what a per-connection bound travels in, so the
        # stub takes it even though the mock ignores it.
        lambda request_timeout_s=None: httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler)
        ),
    )
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_storage] = lambda: OneFake(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api


@pytest.fixture()
def upstream() -> Upstream:
    """The stand-in MCP server."""
    return Upstream()


@pytest.fixture()
def store() -> FakeJobStore:
    """A store resolving the test fence to one allowed binding."""
    return FakeJobStore()


@pytest.fixture()
async def client(monkeypatch, store, upstream):
    """A relay client over the default store and upstream."""
    async with _relay(monkeypatch, store, upstream) as api:
        yield api


def _token(scope: str = SCOPE_MCP) -> str:
    """Mint the capability token the sandbox actually carries."""
    return mint_worker_token(**_FENCE, scope=scope)


def _auth(scope: str = SCOPE_MCP) -> dict[str, str]:
    """Authorization header for the sandbox's token."""
    return {"Authorization": f"Bearer {_token(scope)}"}


def _rpc(method: str, **params: Any) -> dict[str, Any]:
    """One JSON-RPC request body."""
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


# -- the credential boundary -------------------------------------------


async def test_the_upstream_credential_replaces_the_sandbox_token(client, upstream):
    """The MCP server sees the owner's credential; the job token stops at the relay."""
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
    )
    assert response.status_code == 200, response.text

    sent = upstream.requests[0]
    assert str(sent.url) == _UPSTREAM
    assert sent.headers["authorization"] == f"Bearer {_UPSTREAM_SECRET}"
    assert _token() not in str(sent.headers)


async def test_the_upstream_address_never_reaches_the_sandbox(client):
    """The agent talks to the relay and learns nothing about who answers."""
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
    )
    assert _UPSTREAM_HOST not in json.dumps(dict(response.headers))
    assert _UPSTREAM_HOST not in response.text
    # The session id has to survive or streamable HTTP does not work at all.
    assert response.headers["mcp-session-id"] == "sess-1"


async def test_a_connection_header_replaces_the_sandbox_header_rather_than_joining_it(
    monkeypatch, upstream
):
    """Case must not smuggle a second value through under the same name.

    Starlette lowercases inbound header names and a connection row writes them
    the way a human does, so a case-sensitive merge keeps both and httpx sends
    both. The dangerous instance of this is the credential header, where the
    sandbox's own token would ride upstream beside the one meant to replace it.
    """
    store = FakeJobStore(
        connection=_connection(
            auth_config={"header_name": "Mcp-Session-Id", "scheme": ""},
            access_token_ciphertext=_CIPHER.encrypt("registry-value"),
        )
    )
    async with _relay(monkeypatch, store, upstream) as api:
        await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("initialize"),
            headers={**_auth(), "mcp-session-id": "sandbox-value"},
        )

    assert upstream.requests[0].headers.get_list("mcp-session-id") == ["registry-value"]


@pytest.mark.parametrize("status", [401, 403])
async def test_an_upstream_refusing_the_credential_is_attributed_to_the_server(
    client, upstream, caplog, status
):
    """The third way a connection dies, and the only one visible from here.

    Expiry and an unreadable credential are caught while resolving; an upstream
    that has stopped accepting a live-looking token can only be found by asking
    it. Unattributed, that reads as the sandbox misbehaving -- which is how an
    hour of silent 401s went unnoticed before the relay's route learned to name
    the server (hybridInference#1199).

    The status still reaches the agent unchanged: it is the answer to the
    agent's own request, and rewriting it would hide a real reply from the CLI.
    """
    upstream.status_code = status

    with caplog.at_level("WARNING"):
        response = await client.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
        )

    assert response.status_code == status
    rejected = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "agent_mcp_upstream_rejected_credential"
    ]
    assert [(record.server, record.job_id, record.status) for record in rejected] == [
        (_ALIAS, _JOB, status)
    ]


async def test_an_upstream_answering_normally_logs_no_credential_warning(client, upstream, caplog):
    """A warning on every reply would be noise nobody reads."""
    with caplog.at_level("WARNING"):
        await client.post(f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth())

    assert not [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "agent_mcp_upstream_rejected_credential"
    ]


async def test_a_connection_that_no_longer_exists_is_reported_as_unavailable(
    monkeypatch, store, upstream
):
    """The binding survived; the row it names did not: 503, not a crash."""
    store.connection = None
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
        )

    assert response.status_code == 503
    assert upstream.requests == []


# -- the fence and the token -------------------------------------------


async def test_a_binding_the_job_does_not_have_is_refused(monkeypatch, store, upstream):
    """A token reaches its conversation's bindings, not every connection here."""
    store.bindings = []
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "permission_error"
    assert upstream.requests == []


async def test_a_binding_owned_by_somebody_else_is_not_found(monkeypatch, store, upstream):
    """A binding row that names another owner is not this job's to use.

    The relay is the one place the owner is read from the fence rather than from
    a session, so a binding and the connection it names must agree on whose they
    are before anything is dialled.
    """
    store.bindings = [_binding(owner_id=_OTHER)]
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
        )

    assert response.status_code == 404
    assert upstream.requests == []


async def test_a_disabled_binding_refuses_every_call(monkeypatch, store, upstream):
    """Turned off in the session's selection is off, whatever the row allows."""
    store.bindings = [_binding(enabled=False)]
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="get_issue"),
            headers=_auth(),
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601
    assert upstream.requests == []


@pytest.mark.parametrize(
    "method", ["initialize", "ping", "tools/list", "notifications/initialized"]
)
async def test_disabled_binding_never_refreshes_or_forwards_housekeeping(
    monkeypatch, store, upstream, method
):
    from unittest.mock import AsyncMock

    store.bindings = [_binding(enabled=False)]
    refresh = AsyncMock(side_effect=AssertionError("disabled binding refreshed"))
    monkeypatch.setattr(agent_mcp_router, "ensure_fresh", refresh)
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(f"/v1/agent/mcp/{_ALIAS}", json=_rpc(method), headers=_auth())
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601
    refresh.assert_not_awaited()
    assert upstream.requests == []


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_disabled_binding_never_opens_or_closes_an_upstream_stream(
    monkeypatch, store, upstream, method
):
    from unittest.mock import AsyncMock

    store.bindings = [_binding(enabled=False)]
    refresh = AsyncMock(side_effect=AssertionError("disabled binding refreshed"))
    monkeypatch.setattr(agent_mcp_router, "ensure_fresh", refresh)
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.request(method, f"/v1/agent/mcp/{_ALIAS}", headers=_auth())
    assert response.status_code == 403
    refresh.assert_not_awaited()
    assert upstream.requests == []


async def test_a_dead_fence_stops_reaching_tools(store, client, upstream):
    """Cancelling a job revokes its tools exactly as it revokes inference."""
    store.live = False
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
    )
    assert response.status_code == 401
    assert upstream.requests == []


async def test_an_ordinary_api_key_is_not_a_job_token(client, upstream):
    """This endpoint is for sandboxes; a user key is refused outright."""
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}",
        json=_rpc("initialize"),
        headers={"Authorization": "Bearer hyi-not-an-agent-token"},
    )
    assert response.status_code == 401
    assert upstream.requests == []


@pytest.mark.parametrize("scope", [SCOPE_FULL, SCOPE_MODEL])
async def test_only_an_mcp_scoped_token_reaches_the_relay(client, scope):
    """**Positive, not "not full".**

    ``parse_worker_token`` reads a token minted before scopes existed as
    ``full``, which is the conservative reading when deciding whether a
    signature can be trusted and exactly the wrong one here -- a check written
    as "not full" would admit every such token to the tool surface.

    The runner's own ``full`` token is refused too. Reporting events and driving
    terminal transitions is not a reason to be able to call somebody's tools.
    """
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/list"), headers=_auth(scope)
    )

    assert response.status_code == 403
    # `{"detail": {"error": ...}}`, not `{"error": ...}`: this service installs
    # no handler that unwraps FastAPI's envelope, unlike the gateway. Asserted
    # as it actually is rather than as the other service does it, so a reader
    # is not told a shape the wire does not carry.
    assert response.json()["detail"]["error"]["type"] == "insufficient_scope"


async def test_a_gateway_inference_grant_is_not_accepted_here(client):
    """An ``agr`` grant authorizes models at the gateway and nothing here.

    This service cannot verify one -- it holds none of the gateway's signing
    material -- so the refusal is structural rather than a policy. Asserted
    anyway: "it happens not to parse" and "we refuse it" fail differently when
    somebody later adds a gateway client to this module.
    """
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}",
        json=_rpc("tools/list"),
        headers={"Authorization": "Bearer agr.cGF5bG9hZA.c2ln"},
    )

    assert response.status_code in (401, 403)


async def test_a_stale_lease_generation_stops_buying_tools(client, store):
    """The fence is read fresh on every call, so revocation is automatic.

    The moment the reaper supersedes the attempt or the owner cancels, the same
    token stops working -- there is no key to remember to revoke, which is the
    whole reason this service holds the fence rather than the gateway.
    """
    store.live = False

    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/list"), headers=_auth()
    )

    assert response.status_code == 401


# -- the binding's policy decides --------------------------------------


async def test_a_denied_tool_is_refused_before_it_leaves_the_relay(monkeypatch, store, upstream):
    """A name the owner turned off is denied, not confirmed and not forwarded."""
    store.bindings = [
        _binding(rules={"get_issue": {"policy": "deny", "fingerprint": ""}}),
    ]
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="get_issue"),
            headers=_auth(),
        )

    # A JSON-RPC error, not an HTTP one: the hop worked and the call failed, so
    # the CLI reports a failed tool instead of dropping the whole server.
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601
    assert upstream.requests == []
    assert store.created_invocations == []


async def test_a_tool_the_owner_never_chose_is_denied_not_confirmed(monkeypatch, store, upstream):
    """An absent name means denied once a selection exists.

    Confirming it instead would let a server that invents forty tools produce
    forty prompts in a session that asked for none of them.
    """
    store.snapshot = _snapshot("get_issue", "list_issues")
    store.bindings = [_binding(rules={"get_issue": {"policy": "allow", "fingerprint": ""}})]
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="list_issues"),
            headers=_auth(),
        )

    assert response.json()["error"]["code"] == -32601
    assert upstream.requests == []
    assert store.created_invocations == []


async def test_a_tool_discovery_never_saw_is_refused(monkeypatch, store, upstream):
    """A stale client naming a tool the server does not offer is a refusal."""
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="create_issue"),
            headers=_auth(),
        )

    assert response.json()["error"]["code"] == -32601
    assert upstream.requests == []


async def test_an_allowed_tool_is_forwarded(client, upstream):
    """The policy map refuses what it names and nothing else."""
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/call", name="get_issue"), headers=_auth()
    )
    assert response.status_code == 200
    assert len(upstream.requests) == 1


# -- the body is decided once, and the decided body is what travels ----


@pytest.mark.parametrize(
    "body",
    [
        # A byte that is not UTF-8, inside a string. This relay cannot decode
        # it; a decoder that substitutes U+FFFD reads a call to create_issue.
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"create_issue","arguments":{"title":"\xff"}}}',
        # No ``method`` here, but a case-insensitive decoder reads one.
        b'{"jsonrpc":"2.0","id":1,"Method":"tools/call","params":{"name":"create_issue"}}',
        # Here ``name`` is get_issue; a case-insensitive, last-wins decoder
        # keeps create_issue.
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"get_issue","Name":"create_issue"}}',
        # Python keeps the last of two members; a decoder that keeps the first
        # runs the other tool.
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"create_issue","name":"get_issue"}}',
        b"",
    ],
    ids=["invalid-utf8", "method-case", "name-case", "repeated-name", "empty"],
)
async def test_a_body_the_relay_cannot_decide_is_never_forwarded(
    monkeypatch, store, upstream, body
):
    """An unparseable body is a call nothing decided, so it never leaves.

    It used to be forwarded as it came, on the theory that a body this relay
    cannot parse cannot be a ``tools/call``. That holds only if the upstream
    parses the same way, and Node and Go do not: each of these reaches a
    denied tool on an upstream that decodes it more leniently.
    """
    store.snapshot = _snapshot("get_issue", "create_issue")
    store.bindings = [
        _binding(
            rules={
                "get_issue": {"policy": "allow", "fingerprint": ""},
                "create_issue": {"policy": "deny", "fingerprint": ""},
            }
        )
    ]
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            content=body,
            headers={**_auth(), "content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] in (-32700, -32600)
    assert response.json()["id"] is None
    assert upstream.requests == []
    assert store.created_invocations == []


async def test_the_upstream_receives_the_request_as_it_was_decided(client, upstream):
    """What travels is this relay's encoding of the parsed call, not the bytes.

    The two mean the same thing to a strict decoder. Sending the decided form
    means they also mean the same thing to every other decoder.
    """
    raw = (
        b'{ "jsonrpc" : "2.0", "id" : 1, "method" : "tools/call", "params" : '
        b'{ "name" : "get_issue", "arguments" : { "title" : "caf\xc3\xa9", "n" : 1E2 } } }'
    )
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}",
        content=raw,
        headers={**_auth(), "content-type": "application/json; charset=utf-16"},
    )

    assert response.status_code == 200, response.text
    sent = upstream.requests[0]
    assert sent.content == canonical_request(json.loads(raw))
    assert sent.content.isascii()
    assert json.loads(sent.content)["params"]["arguments"] == {"title": "café", "n": 100.0}
    assert sent.headers["content-type"] == "application/json"


@pytest.mark.parametrize("number", ["9007199254740993.0", "0.12345678901234567890", "1e-400"])
@pytest.mark.parametrize("location", ["argument", "id", "batch"])
async def test_a_number_that_would_change_is_never_forwarded(
    client, upstream, store, number: str, location: str
):
    """A lossy number refuses the whole request before any call is held or sent."""
    body = (
        b'{"jsonrpc":"2.0","id":REQUEST_ID,"method":"tools/call",'
        b'"params":{"name":"get_issue","arguments":{"n":ARGUMENT}}}'
    )
    body = body.replace(b"REQUEST_ID", number.encode() if location == "id" else b"2")
    body = body.replace(b"ARGUMENT", b"0" if location == "id" else number.encode())
    if location == "batch":
        body = b"[" + json.dumps(_rpc("tools/call", name="get_issue")).encode() + b"," + body + b"]"

    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}",
        content=body,
        headers={**_auth(), "content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700
    assert response.json()["id"] is None
    assert "without changing its value" in response.json()["error"]["message"]
    assert upstream.requests == []
    assert store.created_invocations == []


async def test_notifications_and_batches_still_travel(client, upstream):
    """A batch is decided message by message and forwarded whole."""
    batch = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_issue"}},
    ]
    response = await client.post(f"/v1/agent/mcp/{_ALIAS}", json=batch, headers=_auth())

    assert response.status_code == 200, response.text
    assert json.loads(upstream.requests[0].content) == batch


async def test_an_oversized_body_is_refused_before_it_is_decided(monkeypatch, store, upstream):
    """The per-connection route reads under the same bound as the merged one."""
    monkeypatch.setattr(agent_mcp_router, "MAX_RELAY_BODY_BYTES", 64)
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="get_issue", arguments={"pad": "x" * 64}),
            headers=_auth(),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32600
    assert upstream.requests == []


@pytest.mark.parametrize("policy", ["allow", "confirm"])
async def test_a_body_that_outgrows_the_bound_when_encoded_is_refused(
    monkeypatch, store, upstream, policy
):
    """A small UTF-8 body cannot become an oversized forwarded or held call."""
    store.bindings = [_binding(rules={"get_issue": {"policy": policy, "fingerprint": ""}})]
    document = _rpc("tools/call", name="get_issue", arguments={"pad": "é" * 50})
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    limit = len(raw) + 1
    assert len(raw) < limit < len(canonical_request(document))
    monkeypatch.setattr(agent_mcp_router, "MAX_RELAY_BODY_BYTES", limit)

    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            content=raw,
            headers={**_auth(), "content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == -32600
    assert response.json()["id"] is None
    assert (
        response.json()["error"]["message"] == f"An MCP request body is limited to {limit} bytes."
    )
    assert upstream.requests == []
    assert store.created_invocations == []
    assert store.looked_up == []


async def test_a_canonical_body_exactly_at_the_bound_is_forwarded(monkeypatch, store, upstream):
    """The canonical byte bound includes its ceiling, even when escaping grows it."""
    document = _rpc("tools/call", name="get_issue", arguments={"pad": "é" * 50})
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    canonical = canonical_request(document)
    assert len(raw) < len(canonical)
    monkeypatch.setattr(agent_mcp_router, "MAX_RELAY_BODY_BYTES", len(canonical))

    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            content=raw,
            headers={**_auth(), "content-type": "application/json"},
        )

    assert response.status_code == 200
    assert upstream.requests[0].content == canonical
    assert store.created_invocations == []


async def test_a_batch_over_the_cap_is_never_forwarded(client, upstream, store):
    """An oversized batch is refused whole rather than decided message by message."""
    batch = [
        {**_rpc("tools/call", name="get_issue"), "id": index}
        for index in range(agent_mcp_router.MAX_RELAY_BATCH_MESSAGES + 1)
    ]
    response = await client.post(f"/v1/agent/mcp/{_ALIAS}", json=batch, headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert upstream.requests == []
    assert store.looked_up == []


async def test_a_denied_tool_is_never_advertised(monkeypatch, store, upstream):
    """A model cannot be talked into a tool it was never offered."""
    upstream.body = _catalogue("get_issue", "create_issue")
    store.snapshot = _snapshot("get_issue", "create_issue")
    store.bindings = [
        _binding(
            rules={
                "get_issue": {"policy": "allow", "fingerprint": ""},
                "create_issue": {"policy": "deny", "fingerprint": ""},
            }
        )
    ]
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/list"), headers=_auth()
        )

    assert [tool["name"] for tool in response.json()["result"]["tools"]] == ["get_issue"]


async def test_a_confirmed_tool_is_advertised_but_held_for_its_owner(monkeypatch, store, upstream):
    """A confirmation is a question, not a refusal, so the tool stays visible.

    The CLI-driven relay cannot block until a person answers, so the honest
    contract is a JSON-RPC error the model can read -- "this needs approval,
    ask again once it is granted" -- and a pending row the owner can decide.
    """
    upstream.body = _catalogue("get_issue", "create_issue")
    store.snapshot = _snapshot("get_issue", "create_issue")
    store.bindings = [_binding(rules={"create_issue": {"policy": "confirm", "fingerprint": ""}})]
    async with _relay(monkeypatch, store, upstream) as api:
        held = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="create_issue", arguments={"title": "x"}),
            headers=_auth(),
        )
        upstream.body = _catalogue("get_issue", "create_issue")
        advertised = await api.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/list"), headers=_auth()
        )

    assert held.status_code == 200
    assert held.json()["error"]["code"] == -32601
    assert "expired" in held.json()["error"]["message"]
    # Visible, because hiding it would make the change invisible to the person
    # who has to approve it.
    assert [tool["name"] for tool in advertised.json()["result"]["tools"]] == ["create_issue"]

    (invocation,) = store.created_invocations
    assert invocation["capability_name"] == "create_issue"
    assert invocation["connection_id"] == _CONNECTION_ID
    assert invocation["thread_id"] == _THREAD
    assert invocation["job_id"] == _JOB
    assert invocation["attempt_id"] == _FENCE["attempt_id"]
    assert invocation["lease_generation"] == _FENCE["lease_generation"]
    assert invocation["authorization_epoch"] == 1
    assert invocation["arguments_digest"]


async def test_a_call_too_large_to_review_is_refused_rather_than_held(monkeypatch, store, upstream):
    """A held call's arguments are stored for its owner, so they are bounded.

    The body is well inside the relay's request bound, and still far more than
    a person reviews before approving; storing it would only grow a table every
    tenant shares.
    """
    store.snapshot = _snapshot("create_issue")
    store.bindings = [_binding(rules={"create_issue": {"policy": "confirm", "fingerprint": ""}})]
    arguments = {"body": "x" * agent_mcp_router.MAX_HELD_ARGUMENTS_BYTES}
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="create_issue", arguments=arguments),
            headers=_auth(),
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602
    assert "not held or sent" in response.json()["error"]["message"]
    assert store.created_invocations == []
    assert upstream.requests == []


async def test_a_retry_matching_an_approval_is_dispatched(monkeypatch, store, upstream):
    """**The retry is the consumption.**

    An approval is one decision about one call, so the next matching request
    claims the pending row and dispatches; the claim is what makes exactly one
    dispatcher win, which is why a retry storm cannot send a write twice.
    """
    store.snapshot = _snapshot("get_issue", "create_issue")
    store.bindings = [_binding(rules={"create_issue": {"policy": "confirm", "fingerprint": ""}})]
    async with _relay(monkeypatch, store, upstream) as api:
        first = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="create_issue", arguments={"title": "x"}),
            headers=_auth(),
        )
        (invocation,) = store.created_invocations
        store.approved = {
            "id": 9,
            "status": "approved",
            "arguments_digest": invocation["arguments_digest"],
        }
        retried = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="create_issue", arguments={"title": "x"}),
            headers=_auth(),
        )

    assert first.json()["error"]["code"] == -32601
    assert retried.status_code == 200
    assert "error" not in retried.json()
    assert len(upstream.requests) == 1
    assert store.claimed == [9]
    # The approval matched the same call it was granted for, digest included.
    assert store.looked_up[-1]["capability_name"] == "create_issue"
    assert store.looked_up[-1]["arguments_digest"] == invocation["arguments_digest"]


async def test_an_approval_that_cannot_be_claimed_holds_the_call_again(
    monkeypatch, store, upstream
):
    """Losing the claim race means somebody else is sending it, not that we are.

    A second dispatcher that finds the row already claimed must not send the
    call again -- an external side effect may already have landed. It records a
    fresh pending call instead, so the owner is asked about the retry.
    """
    store.snapshot = _snapshot("create_issue")
    store.bindings = [_binding(rules={"create_issue": {"policy": "confirm", "fingerprint": ""}})]
    store.approved = {"id": 9, "status": "approved"}
    store.claim_race_won = False

    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="create_issue", arguments={"title": "x"}),
            headers=_auth(),
        )

    assert response.json()["error"]["code"] == -32601
    assert upstream.requests == []
    assert [row["capability_name"] for row in store.created_invocations] == ["create_issue"]


async def test_a_migrated_session_advertises_and_confirms_everything(monkeypatch, store, upstream):
    """A binding from the old provider list has no rules and asks per call.

    A session that predates this model keeps working without inheriting an
    approval nobody ever gave: what the server offers is visible, and every call
    is held for its owner once.
    """
    upstream.body = _catalogue("get_issue", "create_issue")
    store.snapshot = _snapshot("get_issue", "create_issue")
    store.bindings = [_binding(selection_pending=True, rules={})]
    async with _relay(monkeypatch, store, upstream) as api:
        advertised = await api.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/list"), headers=_auth()
        )
        held = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name="create_issue", arguments={}),
            headers=_auth(),
        )

    assert [tool["name"] for tool in advertised.json()["result"]["tools"]] == [
        "get_issue",
        "create_issue",
    ]
    assert held.json()["error"]["code"] == -32601
    assert len(store.created_invocations) == 1


# -- the deployment switch ---------------------------------------------


async def test_mcp_is_available_without_a_rollout_flag(client, monkeypatch):
    """Legacy environment values cannot disable the shipped MCP surface."""
    monkeypatch.setenv("AGENT_MCP_ENABLED", "0")
    response = await client.post(
        f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/list"), headers=_auth()
    )
    assert response.status_code == 200
    assert "result" in response.json()


async def test_mcp_still_requires_its_own_token(client):
    """Always available still requires an authenticated, live attempt."""
    response = await client.post(f"/v1/agent/mcp/{_ALIAS}", json=_rpc("tools/list"))
    assert response.status_code == 401


@pytest.mark.parametrize("scope", ["denied", "unselected", "unknown"])
async def test_a_changed_connection_cannot_prompt_for_an_out_of_scope_tool(
    monkeypatch, store, upstream, scope
):
    """The direct route must reject before creating or consuming any approval."""
    store.connection["authorization_epoch"] += 1
    if scope == "unknown":
        name = "not_discovered"
    else:
        name = "get_issue"
        store.bindings[0]["rules"] = {name: {"policy": "deny"}} if scope == "denied" else {}
    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}",
            json=_rpc("tools/call", name=name),
            headers=_auth(),
        )
    assert response.status_code == 200
    assert "error" in response.json()
    assert upstream.requests == []
    assert store.created_invocations == []
    assert store.looked_up == []


class _StallingBody(httpx.AsyncByteStream):
    """An upstream reply that dies after its first chunk."""

    async def __aiter__(self) -> Any:
        yield b'{"jsonrpc":"2.0","id":1,'
        raise httpx.ReadTimeout("the upstream stopped answering")

    async def aclose(self) -> None:
        """Nothing to release."""


async def test_an_upstream_that_stops_answering_is_a_bad_gateway(monkeypatch, store) -> None:
    """A mid-body failure is the same event as no answer, and leaves as one.

    Unconverted it becomes a 500 whose body says nothing, which the caller
    cannot tell from this service breaking -- and the handler that records an
    approved call's outcome never runs.
    """
    upstream = Upstream()
    upstream.handler = lambda request: httpx.Response(  # type: ignore[method-assign]
        200, headers={"content-type": "application/json"}, stream=_StallingBody()
    )

    async with _relay(monkeypatch, store, upstream) as api:
        response = await api.post(
            f"/v1/agent/mcp/{_ALIAS}", json=_rpc("initialize"), headers=_auth()
        )

    assert response.status_code == 502
    assert "MCP server stopped answering" in response.text
