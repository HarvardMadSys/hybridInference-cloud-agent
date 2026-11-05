"""Independent native Messages checks: correlated tools and complete SSE frames."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from cloud_agent.application.model_connection_check import check_connection

_KEY = "fixture-anthropic-key-not-a-credential"


async def _public(_host: str, _port: int) -> list[str]:
    return ["8.8.8.8"]


def _events(nonce: str, *, tool: bool) -> list[dict[str, Any]]:
    block = (
        {"type": "tool_use", "id": "toolu_fixture", "name": "cloud_agent_check", "input": {}}
        if tool
        else {"type": "text", "text": ""}
    )
    delta = (
        {"type": "input_json_delta", "partial_json": json.dumps({"value": nonce})}
        if tool
        else {"type": "text_delta", "text": nonce}
    )
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "fixture-claude",
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


def _response(events: list[dict[str, Any]]) -> httpx.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def _corrupt(events: list[dict[str, Any]], fault: str) -> list[dict[str, Any]]:
    omitted = {
        "missing_start": "message_start",
        "missing_delta": "message_delta",
        "missing_stop": "message_stop",
        "open_block": "content_block_stop",
    }
    if fault in omitted:
        return [event for event in events if event["type"] != omitted[fault]]
    if fault == "bad_index":
        events[2]["index"] = 7
    elif fault == "non_object_delta":
        events[2]["delta"] = []
    elif fault == "max_tokens":
        events[4]["delta"]["stop_reason"] = "max_tokens"
    elif fault == "error_after_text":
        events.insert(4, {"type": "error", "error": {"type": "overloaded_error"}})
    elif fault == "text_delta_on_thinking_block":
        events[1]["content_block"] = {"type": "thinking", "thinking": "", "signature": ""}
    elif fault == "message_delta_before_content":
        events.insert(1, events.pop(4))
    elif fault == "final_still_requests_a_tool":
        events[4]["delta"]["stop_reason"] = "tool_use"
        events[4:4] = _extra_tool_events()
    return events


def _extra_tool_events() -> list[dict[str, Any]]:
    return [
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_unexpected",
                "name": "unoffered_tool",
                "input": {},
            },
        },
        {"type": "content_block_stop", "index": 1},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "valid",
        "missing_start",
        "missing_delta",
        "missing_stop",
        "open_block",
        "bad_index",
        "non_object_delta",
        "max_tokens",
        "error_after_text",
        "text_delta_on_thinking_block",
        "message_delta_before_content",
        "unexpected_extra_tool_call",
        "final_still_requests_a_tool",
        "missing_call_id",
        "wrong_arguments",
    ],
)
async def test_anthropic_check_cannot_certify_an_incomplete_or_uncorrelated_answer(fault):
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        match = re.search(r"check-[0-9a-f]+", json.dumps(calls[0]))
        assert match
        nonce = match.group()
        assert request.url.path == "/anthropic/v1/messages"
        assert request.headers["x-api-key"] == _KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert body["stream"] is True and body["max_tokens"] > 0
        events = _events(nonce, tool=len(calls) == 1)
        if len(calls) == 1:
            if fault == "missing_call_id":
                events[1]["content_block"]["id"] = ""
            elif fault == "wrong_arguments":
                events[2]["delta"]["partial_json"] = '{"value":"wrong"}'
            elif fault == "unexpected_extra_tool_call":
                events[4:4] = _extra_tool_events()
        else:
            events = _corrupt(events, fault)
        return _response(events)

    result = await check_connection(
        base_url="https://provider.example/anthropic/v1",
        api_key=_KEY,
        protocol="anthropic_messages",
        model="fixture-claude",
        resolve=_public,
        transport=httpx.MockTransport(respond),
    )
    assert (result.status == "supported") is (fault == "valid")
    assert _KEY not in json.dumps(result.as_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://provider.example", "https://provider.example/v1"])
async def test_anthropic_probe_keeps_signed_thinking_and_correlates_the_tool_result(base):
    calls = []
    thinking = {"type": "thinking", "thinking": "Fixture thought", "signature": "opaque-signature"}

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        match = re.search(r"check-[0-9a-f]+", json.dumps(calls[0]))
        assert match
        nonce = match.group()
        assert request.url.path == "/v1/messages"
        events = _events(nonce, tool=len(calls) == 1)
        if len(calls) == 1:
            for event in events:
                if "index" in event:
                    event["index"] += 1
            events[1:1] = [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": thinking["thinking"]},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": thinking["signature"]},
                },
                {"type": "content_block_stop", "index": 0},
            ]
        else:
            assistant = next(
                message for message in body["messages"] if message["role"] == "assistant"
            )
            assert thinking in assistant["content"]
            results = [
                block
                for message in body["messages"]
                if message["role"] == "user" and isinstance(message["content"], list)
                for block in message["content"]
                if block.get("type") == "tool_result"
            ]
            assert len(results) == 1
            assert results[0]["tool_use_id"] == "toolu_fixture"
            assert nonce in json.dumps(results[0]["content"])
        return _response(events)

    result = await check_connection(
        base_url=base,
        api_key=_KEY,
        protocol="anthropic_messages",
        model="fixture-claude",
        resolve=_public,
        transport=httpx.MockTransport(respond),
    )
    assert result.status == "supported"
    assert len(calls) == 2
