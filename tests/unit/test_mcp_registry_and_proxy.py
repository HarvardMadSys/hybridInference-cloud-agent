"""Unit tests for MCP resolution, the proxy's filters, and runtime wiring.

The properties pinned here are the ones the feature's safety rests on: a
credential never reaching the sandbox, a repository never supplying its own MCP
servers, and a tool outside the allowlist being neither advertised nor callable.

Since the deployment-configured registry was retired, one more property joins
them: **no configuration can put a server back on offer.** That claim outlives
#48 — servers now come from per-user connections, and the tests below check
that a deployment's file and variables are still inert against the *new*
source, not merely against the deleted parser.

The per-user properties are here too, and they are what #49 turns on: a server
is reachable only for the person who connected it, and "unknown" means unknown
to *that* user.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cloud_agent.mcp_proxy import SseFilter, filter_json_body, plan_request
from cloud_agent.mcp_registry import (
    McpRegistry,
    McpRegistryError,
    McpServer,
    connection_states,
    mcp_cipher,
    registry_for_user,
)
from tests.support.storage import AllCapabilities

_GATEWAY = "https://gateway.example.com"
_TOKEN = "ajt.job.attempt.signature"
_CIPHER = mcp_cipher("unit-test-encryption-key")


def _server(**overrides) -> McpServer:
    """Build a server with a read-only allowlist unless told otherwise."""
    return McpServer(
        name=overrides.pop("name", "github"),
        url=overrides.pop("url", "https://mcp.example.com/mcp"),
        tools=overrides.pop("tools", frozenset({"get_issue", "list_issues"})),
        **overrides,
    )


class FakeConnectionStore(AllCapabilities):
    """Connections by user, as the store returns them."""

    def __init__(self, by_user: dict[str, list[dict]] | None = None) -> None:
        self.by_user = by_user or {}

    async def list_mcp_connections(self, *, user_id: str) -> list[dict]:
        """Return one user's rows, and never another's."""
        return list(self.by_user.get(user_id, []))


def _connection(server: str = "github", token: str = "ghu_owner_secret") -> dict:
    """One stored connection, encrypted the way the store holds it."""
    return {
        "server": server,
        "account_label": "octocat",
        "access_token_ciphertext": _CIPHER.encrypt(token),
        "refresh_token_ciphertext": None,
        "expires_at": None,
    }


# ── a server is one person's, or it is nobody's ───────────────────────


@pytest.mark.asyncio
async def test_no_deployment_configuration_can_offer_servers(tmp_path, monkeypatch):
    """A registry file and the variables that used to name it change nothing.

    This is the test that distinguishes retiring the feature from unwiring it,
    and it has to keep holding now that servers come from somewhere else: a
    deployment upgrading from the version that had the registry keeps its file
    and its variables, and neither may put a server in front of a user who
    connected nothing.
    """
    config = tmp_path / "mcp.yaml"
    config.write_text(
        """
servers:
  - name: github
    url: https://api.githubcopilot.com/mcp/
    default: true
    headers:
      Authorization: "Bearer ${GITHUB_MCP_TOKEN}"
"""
    )
    monkeypatch.setenv("AGENT_MCP_CONFIG", str(config))
    monkeypatch.setenv("AGENT_MCP_CONFIG_FILE", str(config))
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "ghs_secret")

    registry = await registry_for_user(FakeConnectionStore(), user_id="nobody", cipher=_CIPHER)
    assert registry.servers == {}
    assert registry.names == []
    assert registry.defaults == []


@pytest.mark.asyncio
async def test_a_user_who_connected_nothing_cannot_name_a_server():
    """Naming one fails at creation, and says so in the owner's language."""
    registry = await registry_for_user(FakeConnectionStore(), user_id="nobody", cipher=_CIPHER)
    with pytest.raises(McpRegistryError, match="have not connected"):
        registry.resolve(["github"])


@pytest.mark.asyncio
async def test_a_job_that_names_nothing_still_runs():
    """No MCP is a supported shape, not an error: the job just gets no tools."""
    registry = await registry_for_user(FakeConnectionStore(), user_id="nobody", cipher=_CIPHER)
    assert registry.resolve(None) == []
    assert registry.resolve([]) == []


@pytest.mark.asyncio
async def test_one_users_connection_is_not_another_users():
    """The property the deployment registry could not have.

    Same server name, same code path, two users: one connected it and one did
    not, and the second is refused in exactly the words someone who connected
    nothing gets. No credential of the first user's is reachable through the
    second's resolution.
    """
    store = FakeConnectionStore({"owner": [_connection()]})

    owner_registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert owner_registry.names == ["github"]
    assert owner_registry.resolve(["github"]) == ["github"]
    assert owner_registry.servers["github"].headers == {"Authorization": "Bearer ghu_owner_secret"}

    other_registry = await registry_for_user(store, user_id="someone-else", cipher=_CIPHER)
    assert other_registry.names == []
    with pytest.raises(McpRegistryError, match="have not connected"):
        other_registry.resolve(["github"])


@pytest.mark.asyncio
async def test_connecting_is_the_opt_in_so_a_job_that_chose_nothing_gets_them():
    """``None`` takes what the owner connected; ``[]`` still takes nothing."""
    store = FakeConnectionStore({"owner": [_connection()]})
    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert registry.resolve(None) == ["github"]
    assert registry.resolve([]) == []


@pytest.mark.asyncio
async def test_a_connection_to_a_retired_provider_is_inert_not_fatal():
    """A row outliving its catalogue entry drops out, taking nothing with it.

    Withdrawing a provider is our decision, not the user's mistake, so the row
    must not break resolution for the servers they can still reach.
    """
    store = FakeConnectionStore(
        {"owner": [_connection(server="withdrawn"), _connection(server="github")]}
    )
    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert registry.names == ["github"]


@pytest.mark.asyncio
async def test_an_undecryptable_credential_drops_only_its_own_server():
    """One corrupt row cannot take out the servers beside it."""
    broken = _connection(server="github")
    broken["access_token_ciphertext"] = "not-fernet-ciphertext"
    store = FakeConnectionStore({"owner": [broken]})
    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert registry.names == []


@pytest.mark.asyncio
async def test_a_credential_we_know_has_expired_is_not_offered():
    """A stored expiry in the past is a token we know will be refused.

    Resolving it anyway puts the server in the composer, lets a job be created
    around it, and then sends a dead bearer token upstream — three places that
    all had the answer in the row they were reading.
    """
    expired = _connection()
    expired["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    store = FakeConnectionStore({"owner": [expired]})

    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert registry.names == []
    with pytest.raises(McpRegistryError, match="have not connected"):
        registry.resolve(["github"])


@pytest.mark.asyncio
async def test_an_expiry_still_ahead_resolves_normally():
    """The boundary is the expiry itself, with no margin taken off it."""
    live = _connection()
    live["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=30)
    store = FakeConnectionStore({"owner": [live]})

    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert registry.names == ["github"]


@pytest.mark.asyncio
async def test_no_stored_expiry_means_no_expiry_we_know_of():
    """``NULL`` is "the provider told us nothing", not "already dead"."""
    store = FakeConnectionStore({"owner": [_connection()]})
    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert registry.names == ["github"]


@pytest.mark.asyncio
async def test_a_naive_stored_expiry_is_read_as_utc():
    """A comparison against a naive datetime raises; it must not reach one."""
    expired = _connection()
    expired["expires_at"] = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    store = FakeConnectionStore({"owner": [expired]})

    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    assert registry.names == []


@pytest.mark.asyncio
async def test_a_missing_encryption_key_disables_mcp_rather_than_raising(monkeypatch):
    """The failure that used to reach the relay as a 500.

    A deployment that had a key, has connections, and then loses the key —
    a rotation gone wrong, or a partial rollout — must degrade to "no servers"
    at every call site. It used to propagate, and the relay had no catch: every
    tool call of every running job became a 500 with a stack trace instead of
    the 503 the relay documents.
    """
    store = FakeConnectionStore({"owner": [_connection()]})

    # With the key, the connection resolves — so the assertion below is about
    # the key and not about some other reason this row might not resolve.
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "unit-test-encryption-key")
    assert (await registry_for_user(store, user_id="owner")).names == ["github"]

    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
    assert (await registry_for_user(store, user_id="owner")).names == []

    states = await connection_states(store, user_id="owner")
    assert [state.error for state in states] == ["MCP is not configured on this deployment."]


@pytest.mark.asyncio
async def test_every_unusable_connection_says_why():
    """The page and the picker read one answer, so they cannot disagree."""
    expired = _connection(server="github")
    expired["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    unreadable = _connection(server="withdrawn")
    unreadable["access_token_ciphertext"] = "not-fernet-ciphertext"
    store = FakeConnectionStore({"owner": [expired, unreadable]})

    states = {
        state.server: state
        for state in await connection_states(store, user_id="owner", cipher=_CIPHER)
    }

    assert states["github"].resolved is None
    assert "expired" in states["github"].error
    # Withdrawn is checked before the credential: a row for a provider that is
    # gone should not be reported as a credential problem the owner can fix.
    assert states["withdrawn"].error == "This server is no longer offered."


@pytest.mark.asyncio
async def test_a_resolved_server_carries_the_catalogue_bound_not_the_rows():
    """The tool surface comes from the reviewed entry, never from stored data.

    A connection row supplies a credential and nothing else. If it could widen
    what the agent may call, the catalogue's scope decision would be advisory.
    """
    row = _connection()
    row["tools"] = ["delete_repository"]
    store = FakeConnectionStore({"owner": [row]})
    registry = await registry_for_user(store, user_id="owner", cipher=_CIPHER)
    server = registry.servers["github"]
    assert server.tools == frozenset()
    assert server.url == "https://api.githubcopilot.com/mcp/readonly"


# ── the shapes the relay and proxy resolve against ────────────────────


def test_public_view_never_carries_the_url_or_the_credential():
    """What the composer sees has nowhere to put the two secrets."""
    server = _server(headers={"Authorization": "Bearer ghs_secret"})
    rendered = json.dumps(McpRegistry(servers={"github": server}).public())
    assert "ghs_secret" not in rendered
    assert "mcp.example.com" not in rendered
    assert "get_issue" in rendered


def test_resolve_distinguishes_unset_from_empty():
    """Omitted takes the defaults; an explicit [] means no servers at all."""
    registry = McpRegistry(servers={"a": _server(name="a", default=True), "b": _server(name="b")})
    assert registry.resolve(None) == ["a"]
    assert registry.resolve([]) == []
    assert registry.resolve(["b"]) == ["b"]


def test_resolve_refuses_an_unknown_server():
    """A job asking for a tool surface it will not get fails at creation."""
    registry = McpRegistry(servers={"a": _server(name="a")})
    with pytest.raises(McpRegistryError, match="nope"):
        registry.resolve(["nope"])


# ── request guard ─────────────────────────────────────────────────────


def _call(tool: str, *, request_id: int = 1) -> bytes:
    """Encode a ``tools/call`` request."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool}}
    ).encode()


def test_allowed_tool_is_forwarded():
    """A tool on the allowlist produces no rejection."""
    assert not plan_request(_call("get_issue"), _server()).rejected


def test_disallowed_tool_is_refused_before_leaving_the_gateway():
    """The allowlist is enforced here, not by asking the model nicely."""
    plan = plan_request(_call("create_issue"), _server())
    assert plan.rejected
    assert plan.blocked_tools == ("create_issue",)
    assert plan.rejection["id"] == 1
    assert "create_issue" in plan.rejection["error"]["message"]


def test_a_batch_with_one_blocked_tool_is_refused_whole():
    """Partial forwarding would need replies re-associated across requests."""
    body = json.dumps(
        [
            json.loads(_call("get_issue", request_id=1)),
            json.loads(_call("create_issue", request_id=2)),
        ]
    ).encode()
    plan = plan_request(body, _server())
    assert plan.rejected
    assert [entry["id"] for entry in plan.rejection] == [2]


def test_an_unfiltered_server_allows_every_tool():
    """Omitting ``tools:`` means the server's own surface, unmodified."""
    assert not plan_request(_call("delete_everything"), _server(tools=frozenset())).rejected


def test_a_malformed_body_is_passed_through_rather_than_guessed_at():
    """The upstream owns the protocol and answers a bad request better."""
    assert not plan_request(b"{not json", _server()).rejected


# ── response filtering ────────────────────────────────────────────────


def _catalogue(*names: str) -> bytes:
    """Encode a ``tools/list`` result advertising ``names``."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"tools": [{"name": name, "description": name} for name in names]},
        }
    ).encode()


def _listed(body: bytes) -> list[str]:
    """Return the tool names a filtered catalogue still advertises."""
    return [tool["name"] for tool in json.loads(body)["result"]["tools"]]


def test_disallowed_tools_are_never_advertised():
    """A model cannot be talked into a tool it was never offered."""
    filtered = filter_json_body(_catalogue("get_issue", "create_issue", "list_issues"), _server())
    assert _listed(filtered) == ["get_issue", "list_issues"]


def test_an_unfiltered_server_advertises_everything():
    """No allowlist means the catalogue passes through untouched."""
    body = _catalogue("get_issue", "create_issue")
    assert filter_json_body(body, _server(tools=frozenset())) == body


def test_a_non_catalogue_reply_is_left_alone():
    """Only ``result.tools`` is ours; every other reply passes through."""
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": "hello"}}).encode()
    assert json.loads(filter_json_body(body, _server()))["result"]["content"] == "hello"


def test_sse_catalogues_are_filtered_frame_by_frame():
    """Streamable HTTP may deliver the catalogue as an event, not a body."""
    stream = b"event: message\ndata: " + _catalogue("get_issue", "create_issue") + b"\n\n"
    sse = SseFilter(_server())
    out = sse.feed(stream) + sse.flush()
    assert b"create_issue" not in out
    assert b"get_issue" in out
    # The transport is the upstream's business and is left exactly as written.
    assert out.startswith(b"event: message\n")


def test_sse_filtering_survives_a_frame_split_across_chunks():
    """A catalogue arriving in pieces is still filtered, not passed through."""
    stream = b"data: " + _catalogue("get_issue", "create_issue") + b"\n\n"
    sse = SseFilter(_server())
    out = b"".join(sse.feed(stream[i : i + 7]) for i in range(0, len(stream), 7)) + sse.flush()
    assert b"create_issue" not in out
    assert b"get_issue" in out


def test_sse_passes_through_frames_that_are_not_catalogues():
    """Progress notifications and comments must survive untouched."""
    frame = b': keep-alive\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    sse = SseFilter(_server())
    assert sse.feed(frame) + sse.flush() == frame


# The runtime-wiring half of this file did not come across. It tests
# `cloud_agent_host.runtimes` — how a CLI invocation is built — not the
# registry or the proxy filters, and it already lives in
# `test_agent_runtimes.py`, ported there when the gateway's copy was removed at
# H4. Six cases, all covered: the strict-config flag, the endpoints and
# credential a brokered runtime emits, base-url normalisation, which runtimes
# may claim MCP, and the two fail-closed paths.
#
# Keeping a second copy here would have been worse than duplication. Those
# cases assert the *relay* address and the *MCP* credential now, and the copy
# here still asserted the gateway's — two files disagreeing about which service
# serves tools, with the stale one passing until somebody changed the contract.
