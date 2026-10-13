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
from decimal import Decimal

import pytest

from cloud_agent.mcp.models import McpServer, Transport
from cloud_agent.mcp_proxy import (
    SseFilter,
    UnreadableRequest,
    canonical_request,
    filter_json_body,
    parse_request,
    plan_request,
)

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


def test_a_malformed_body_is_refused_rather_than_forwarded():
    """A body the proxy cannot parse is a ``tools/call`` it cannot see.

    This used to be passed through for the upstream to answer. An upstream that
    parses more leniently can find a call in it, and that call would have
    skipped the allowlist entirely.
    """
    plan = plan_request(b"{not json", _server())
    assert plan.rejected
    assert plan.rejection == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": plan.rejection["error"]["message"]},
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b'{"method":"tools/call","params":{"name":"get_issue\xff"}}', -32700),
        (b'{"method":"tools/call","method":"tools/list"}', -32700),
        (b'{"method":"tools/call","params":{"arguments":{"a":1,"a":2}}}', -32700),
        (b'{"id":1,"n":NaN}', -32700),
        (b'{"id":1,"n":-Infinity}', -32700),
        (b'{"id":1,"n":1e400}', -32700),
        (b"[" * 100_000 + b"]" * 100_000, -32700),
        (b"\xef\xbb\xbf{}", -32700),
        (b'{"Method":"tools/call"}', -32600),
        (b'{"method":"tools/call","PARAMS":{"name":"x"}}', -32600),
        # Go's case folding also matches U+017F, the long s, to "s".
        (b'{"method":"tools/call","param\xc5\xbf":{}}', -32600),
        (b'{"method":"tools/call","params":{"name":"x","Name":"y"}}', -32600),
        (b'[{"method":"ping"},{"method":"tools/call","params":{"URI":"x"}}]', -32600),
        # Java's equalsIgnoreCase also matches U+0131 and U+0130 to "i", and
        # casefold matches neither.
        (b'{"method":"resources/read","params":{"uri":"a","ur\xc4\xb1":"b"}}', -32600),
        (b'{"\xc4\xb0d":1,"method":"ping"}', -32600),
    ],
    ids=[
        "invalid-utf8",
        "repeated-method",
        "repeated-argument",
        "nan",
        "infinity",
        "overflow",
        "too-deep",
        "byte-order-mark",
        "method-case",
        "params-case",
        "long-s",
        "name-case",
        "batch-member-case",
        "dotless-i",
        "dotted-capital-i",
    ],
)
def test_an_ambiguous_body_is_refused_by_both_decisions(body: bytes, code: int):
    """Every body two decoders could read differently is refused, not guessed at."""
    with pytest.raises(UnreadableRequest) as caught:
        parse_request(body)
    assert caught.value.code == code
    assert plan_request(body, _server()).rejection["error"]["code"] == code


@pytest.mark.parametrize(
    "number",
    [
        "9007199254740993.0",
        "9007199254740993e0",
        "0.12345678901234567890",
        "1.0000000000000001",
        "1e-400",
        "-1e-400",
        "0e999999999999999999999999999999",
    ],
)
def test_a_number_changed_by_reencoding_is_refused(number: str):
    """An exact-number upstream must never receive rounded arguments."""
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"get_issue","arguments":{"n":' + number + "}}}"
    ).encode()

    with pytest.raises(UnreadableRequest, match="without changing its value") as caught:
        parse_request(body)

    assert caught.value.code == -32700
    assert plan_request(body, _server()).rejection["error"]["code"] == -32700


@pytest.mark.parametrize(
    "number", ["0.1", "-0.1", "1E2", "1.2300000", "9007199254740992.0", "1e-20", "5e-324"]
)
def test_a_number_with_an_unchanged_decimal_value_still_travels(number: str):
    """Compare exact decimal values, since float comparisons would hide rounding."""
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"get_issue","arguments":{"n":' + number + "}}}"
    ).encode()

    canonical = canonical_request(parse_request(body))

    assert json.loads(canonical, parse_float=Decimal) == json.loads(body, parse_float=Decimal)
    assert not plan_request(canonical, _server()).rejected


def test_a_member_this_relay_does_not_read_keeps_its_own_spelling():
    """Only the members a decision reads are pinned; arguments are the tool's."""
    document = parse_request(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_issue",'
        b'"arguments":{"Name":"a","name":"b"},"_meta":{"progressToken":1}}}'
    )
    assert document["params"]["arguments"] == {"Name": "a", "name": "b"}


def test_the_canonical_form_reads_back_as_what_was_decided():
    """Re-encoding loses nothing a decision read, and stays ASCII for every decoder."""
    body = json.dumps(
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": "a",
                "method": "tools/call",
                "params": {"name": "get_issue", "arguments": {"t": "café 😀", "n": 2**70}},
            },
        ]
    ).encode()
    document = parse_request(body)
    canonical = canonical_request(document)
    assert canonical.isascii()
    assert parse_request(canonical) == document == json.loads(body)
    assert not plan_request(canonical, _server()).rejected


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


@pytest.mark.parametrize("ending", [b"\r\n", b"\r"], ids=["crlf", "cr"])
def test_sse_frames_end_at_any_blank_line_not_only_lf_lf(ending):
    """A CRLF server's events are framed, streamed and filtered one by one.

    ``sse-starlette``, which the MCP Python SDK serves with, ends lines with
    CRLF. Framed on LF-LF alone, two such events streamed nothing before the
    end and left as one frame too garbled to parse -- and so unfiltered.
    """
    first = b"event: message" + ending + b"data: " + _catalogue("get_issue", "create_issue")
    second = b"data: " + _catalogue("create_issue", "list_issues")
    sse = SseFilter(_server())
    out = sse.feed(first + ending * 2 + second + ending * 2)
    assert sse.flush() == b""
    events = out.split(b"\n\n")
    assert events[2:] == [b""]
    assert events[0].startswith(b"event: message\n")
    assert _listed(events[0].split(b"data: ", 1)[1]) == ["get_issue"]
    assert _listed(events[1].split(b"data: ", 1)[1]) == ["list_issues"]


def test_a_crlf_event_is_released_once_its_blank_line_arrives():
    """The long-lived stream cannot wait for the size cap to release an event.

    Fed byte by byte, so every CRLF splits across chunks: its LF finishes the
    line the CR already ended rather than starting a blank one of its own.
    """
    event = b"data: " + _catalogue("get_issue", "create_issue") + b"\r\n\r\n"
    sse = SseFilter(_server())
    released = [sse.feed(event[i : i + 1]) for i in range(len(event))]
    # Out on the second CR, not one byte later and not at the end of the stream.
    assert [bool(chunk) for chunk in released][-3:] == [False, True, False]
    frame = b"".join(released)
    assert frame.endswith(b"\n\n") and frame.count(b"\n") == 2
    assert _listed(frame.split(b"data: ", 1)[1]) == ["get_issue"]
    assert sse.feed(b": still open\r") == b""
    assert sse.flush() == b": still open\n"
