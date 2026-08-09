"""Unit tests for MCP resolution, the proxy's filters, and runtime wiring.

The properties pinned here are the ones the feature's safety rests on: a
credential never reaching the sandbox, a repository never supplying its own MCP
servers, and a tool outside the allowlist being neither advertised nor callable.

Since the deployment-configured registry was retired, one more property joins
them: **no configuration can put a server back on offer.** The tests that used
to exercise the YAML loader are gone with it; what replaces them asserts the
absence, because "we deleted the parser" and "a deployment cannot supply
servers" are different claims and only the second one is the product decision.
"""

from __future__ import annotations

import json

import pytest

from cloud_agent.mcp_proxy import SseFilter, filter_json_body, plan_request
from cloud_agent.mcp_registry import (
    McpRegistry,
    McpRegistryError,
    McpServer,
    get_registry,
)

_GATEWAY = "https://gateway.example.com"
_TOKEN = "ajt.job.attempt.signature"


def _server(**overrides) -> McpServer:
    """Build a server with a read-only allowlist unless told otherwise."""
    return McpServer(
        name=overrides.pop("name", "github"),
        url=overrides.pop("url", "https://mcp.example.com/mcp"),
        tools=overrides.pop("tools", frozenset({"get_issue", "list_issues"})),
        **overrides,
    )


# ── nothing is on offer ───────────────────────────────────────────────


def test_no_deployment_configuration_can_offer_servers(tmp_path, monkeypatch):
    """A registry file and the variable that used to name it change nothing.

    This is the test that distinguishes retiring the feature from unwiring it.
    A deployment upgrading from a version that had the registry keeps its file
    and its variable; both must now be inert rather than quietly honoured.
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

    assert get_registry().servers == {}
    assert get_registry().names == []
    assert get_registry().defaults == []


def test_a_job_cannot_name_a_server_because_none_are_on_offer():
    """Naming one fails at creation, and says so in the owner's language."""
    with pytest.raises(McpRegistryError, match="not available yet"):
        get_registry().resolve(["github"])


def test_a_job_that_names_nothing_still_runs():
    """No MCP is a supported shape, not an error: the job just gets no tools."""
    assert get_registry().resolve(None) == []
    assert get_registry().resolve([]) == []


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
