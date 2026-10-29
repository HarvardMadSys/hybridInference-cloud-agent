"""The completeness guard: only a Chat answer that finished becomes a client success."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cloud_agent_shared.protocol_compat.gate import prepare
from cloud_agent_shared.protocol_compat.stream import (
    ClientStream,
    client_error,
    client_response,
    parse_frames,
)

CLIENTS = ("responses", "anthropic_messages")


def _chunk(delta: dict[str, Any] | None = None, finish: str | None = None, **choice: Any):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish, **choice}]}


def _run(client: str, payloads: list[Any], *, fail: bool = False, names=None) -> list[dict]:
    stream = ClientStream(client, model="m", request_body={"model": "m"}, names=names)
    frames: list[str] = []
    for payload in payloads:
        frames += stream.feed(payload)
    if fail:
        stream.fail()
    frames += stream.finish()
    return parse_frames(frames)


def _outcome(client: str, events: list[dict]) -> str:
    kinds = [event.get("type") for event in events]
    if client == "responses":
        if "response.completed" in kinds:
            return "success"
        assert "response.failed" in kinds and "error" in kinds, kinds
        return "failure"
    if "message_stop" in kinds:
        assert "error" not in kinds
        return "success"
    assert kinds[-1] == "error", kinds
    return "failure"


@pytest.mark.parametrize("client", CLIENTS)
def test_a_finished_answer_succeeds_with_its_usage(client):
    events = _run(
        client,
        [
            _chunk({"role": "assistant", "content": "hel"}),
            _chunk({"content": "lo"}),
            _chunk({}, "stop"),
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        ],
    )
    assert _outcome(client, events) == "success"
    text = "".join(
        e.get("delta", {}).get("text", "") if client == "anthropic_messages" else e.get("delta", "")
        for e in events
        if e.get("type") in ("content_block_delta", "response.output_text.delta")
    )
    assert text == "hello"


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize(
    "payloads",
    [
        pytest.param([_chunk({"content": "hello"}, "made_up")], id="unknown-finish-reason"),
        pytest.param(
            [{"choices": [{"index": 1, "delta": {"content": "x"}, "finish_reason": "stop"}]}],
            id="only-choice-one",
        ),
        pytest.param([{"choices": ["bad"]}], id="non-object-choice"),
        pytest.param([{"choices": {"index": 0}}], id="choices-not-a-list"),
        pytest.param(
            [{"choices": [{"index": 0, "delta": [], "finish_reason": None}]}],
            id="non-object-delta",
        ),
        pytest.param([_chunk({"content": "partial"})], id="no-finish-reason"),
        pytest.param(
            [_chunk({"content": "partial"}), {"error": {"message": "leak sk-secret"}}],
            id="error-after-output",
        ),
        pytest.param(
            [_chunk({"content": "x"}, "stop"), {"error": {"message": "late"}}],
            id="error-after-finish",
        ),
        pytest.param([], id="empty-stream"),
        pytest.param(
            [
                _chunk({"content": "x"}, finish=None)
                | {"choices": [{"index": 0, "delta": {}, "finish_reason": {"r": 1}}]}
            ],
            id="object-finish-reason",
        ),
        pytest.param(
            [{"choices": [{"index": 0, "delta": {}, "finish_reason": ["stop"]}]}],
            id="list-finish-reason",
        ),
        pytest.param(
            [_chunk({"tool_calls": "not-a-list"}), _chunk({}, "tool_calls")],
            id="tool-calls-not-a-list",
        ),
        pytest.param(
            [_chunk({"tool_calls": [1]}), _chunk({}, "tool_calls")], id="tool-call-not-an-object"
        ),
        pytest.param(
            [_chunk({"content": {"nested": True}}), _chunk({}, "stop")], id="content-not-text"
        ),
    ],
)
def test_anything_short_of_a_finished_answer_fails(client, payloads):
    events = _run(client, payloads)
    assert _outcome(client, events) == "failure"
    # The upstream's own words never reach the client.
    assert "sk-secret" not in json.dumps(events)


@pytest.mark.parametrize("client", CLIENTS)
def test_a_stream_that_breaks_after_its_finish_reason_still_fails(client):
    events = _run(client, [_chunk({"content": "x"}, "stop")], fail=True)
    assert _outcome(client, events) == "failure"


def test_a_length_finish_is_incomplete_not_completed():
    events = _run("responses", [_chunk({"content": "cut"}, "length")])
    kinds = [event["type"] for event in events]
    assert "response.incomplete" in kinds and "response.completed" not in kinds


def test_a_tool_call_streams_as_a_function_call_and_messages_tool_use():
    payloads = [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": '{"a":'},
                    }
                ]
            }
        ),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
        _chunk({}, "tool_calls"),
    ]
    responses = _run("responses", payloads)
    done = [e["item"] for e in responses if e["type"] == "response.output_item.done"]
    assert done == [
        {
            "type": "function_call",
            "id": done[0]["id"],
            "call_id": "call_1",
            "name": "f",
            "arguments": '{"a":1}',
            "status": "completed",
        }
    ]
    messages = _run("anthropic_messages", payloads)
    starts = [e for e in messages if e["type"] == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "tool_use"
    assert starts[0]["content_block"]["id"] == "call_1"
    stop = next(e for e in messages if e["type"] == "message_delta")
    assert stop["delta"]["stop_reason"] == "tool_use"


def test_namespaced_calls_come_back_under_their_namespace():
    request = {
        "model": "m",
        "input": "go",
        "tools": [
            {
                "type": "namespace",
                "name": "mcp__session",
                "tools": [{"type": "function", "name": "lookup", "parameters": {}}],
            }
        ],
    }
    prepared = prepare("responses", request, "m")
    flat = prepared.chat["tools"][0]["function"]["name"]
    assert flat == "mcp__session__lookup"
    events = _run(
        "responses",
        [
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": flat, "arguments": "{}"},
                        }
                    ]
                }
            ),
            _chunk({}, "tool_calls"),
        ],
        names=prepared.names,
    )
    items = [e["item"] for e in events if e["type"] in ("response.output_item.done",)]
    final = next(e["response"] for e in events if e["type"] == "response.completed")
    for item in [*items, *final["output"]]:
        assert (item["name"], item["namespace"]) == ("lookup", "mcp__session")


@pytest.mark.parametrize("client", CLIENTS)
def test_a_non_streamed_answer_must_be_finished_and_choice_zero(client):
    finished = {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ]
    }
    assert client_response(client, finished, model="m", request_body={}) is not None
    for broken in (
        {"choices": [{"index": 0, "message": {"content": "hi"}, "finish_reason": {"x": 1}}]},
        {"choices": [{"index": 0, "message": {"tool_calls": "bad"}, "finish_reason": "stop"}]},
        {"choices": [{"index": 0, "message": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [{"index": 1, "message": {"content": "hi"}, "finish_reason": "stop"}]},
        {"choices": [{"index": 0, "message": {"content": "hi"}, "finish_reason": "made_up"}]},
        {"choices": []},
        {"choices": "bad"},
    ):
        assert client_response(client, broken, model="m", request_body={}) is None


def test_errors_take_each_client_shape_with_only_the_given_message():
    assert client_error("anthropic_messages", 429, "slow down") == {
        "type": "error",
        "error": {"type": "rate_limit_error", "message": "slow down"},
    }
    body = client_error("responses", 502, "unreachable")
    assert body["error"]["type"] == "server_error" and body["error"]["message"] == "unreachable"
