"""Unit tests for the MCP proxy's two directions of enforcement.

The proxy is what the relay calls once a binding has been authorized, and it
answers the two questions that must not depend on the relay getting them right
twice: may this request leave the platform, and what may the model be told the
server offers?

Both directions are needed. Refusing only the call would leave a denied tool
advertised, and an agent that keeps trying it spends the job's budget learning
the policy one error at a time; filtering only the catalogue would leave the
tool callable by name. The negative cases carry the weight here: a blocked tool
in a batch, a catalogue split across chunks, and a reply that is not a
catalogue at all.

The filter is driven by :class:`McpServer`, which a generic connection builds
from its binding: the reachable set is the owner's decision, so there is no
wildcard to fall back to. A server with an empty set is the proxy's own
fail-closed witness, not a shape a connection produces -- for a connection,
empty means nothing is reachable.
"""

from __future__ import annotations

import json

from cloud_agent.mcp.models import McpServer, Transport
from cloud_agent.mcp_proxy import SseFilter, filter_json_body, plan_request

_GATEWAY = "https://gateway.example.com"
_TOKEN = "ajt.job.attempt.signature"


def _server(**overrides) -> McpServer:
    """Build a server with a read-only allowlist unless told otherwise."""
    return McpServer(
        name=overrides.pop("name", "github"),
        url=overrides.pop("url", "https://mcp.example.com/mcp"),
        connection_id=overrides.pop("connection_id", "7"),
        transport=overrides.pop("transport", Transport.STREAMABLE_HTTP),
        tools=overrides.pop("tools", frozenset({"get_issue", "list_issues"})),
        **overrides,
    )


# -- what the picker may see -------------------------------------------


def test_public_view_never_carries_the_url_or_the_credential():
    """What the composer sees has nowhere to put the two secrets."""
    server = _server(headers={"Authorization": "Bearer ghs_secret"})
    rendered = json.dumps(server.public())
    assert "ghs_secret" not in rendered
    assert "mcp.example.com" not in rendered
    assert "get_issue" in rendered


# -- request guard -----------------------------------------------------


def _call(tool: str, *, request_id: int = 1) -> bytes:
    """Encode a ``tools/call`` request."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool}}
    ).encode()


def test_allowed_tool_is_forwarded():
    """A tool in the reachable set produces no rejection."""
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


def test_a_malformed_body_is_passed_through_rather_than_guessed_at():
    """The upstream owns the protocol and answers a bad request better."""
    assert not plan_request(b"{not json", _server()).rejected


def test_an_empty_reachable_set_is_nothing_not_everything():
    """The model's answer to "what if no tool is allowed" is: nothing is.

    It is the opposite of the provider-catalogue model this replaced, where an
    absent allowlist meant the whole tool surface. A connection whose owner
    turned every tool off must not become an unfiltered passthrough.
    """
    server = _server(tools=frozenset())
    assert not server.unfiltered
    assert not server.allows("get_issue")
    assert plan_request(_call("get_issue"), server).rejected
    assert _listed(filter_json_body(_catalogue("get_issue", "create_issue"), server)) == []


# -- response filtering ------------------------------------------------


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
