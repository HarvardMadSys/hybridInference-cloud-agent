"""The native Messages check and model list: the CLI's URL and headers, and honest failures.

Complements ``test_model_connection_anthropic_review`` (framing and
correlation faults) with the HTTP-level outcomes, the authentication the CLI
itself uses, and where the model list is asked for.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from cloud_agent.application.model_connection_check import (
    CheckStop,
    check_connection,
    discover_models,
)

_KEY = "fixture-anthropic-key-0123456789"
pytestmark = pytest.mark.asyncio


async def _public(_host: str, _port: int) -> list[str]:
    return ["8.8.8.8"]


def _sse(events: list[dict[str, Any]]) -> httpx.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def _answer(blocks: list[dict[str, Any]], deltas: list[dict[str, Any]], stop: str):
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"id": "msg_1", "content": [], "role": "assistant"}},
        {"type": "ping"},
    ]
    for index, block in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.append({"type": "content_block_delta", "index": index, "delta": deltas[index]})
        events.append({"type": "content_block_stop", "index": index})
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]
    return _sse(events)


def _tool_answer(nonce: str, call_id: str = "toolu_1") -> httpx.Response:
    return _answer(
        [{"type": "tool_use", "id": call_id, "name": "cloud_agent_check", "input": {}}],
        [{"type": "input_json_delta", "partial_json": json.dumps({"value": nonce})}],
        "tool_use",
    )


def _text_answer(text: str, stop: str = "end_turn") -> httpx.Response:
    return _answer([{"type": "text", "text": ""}], [{"type": "text_delta", "text": text}], stop)


async def _check(respond, base_url: str = "https://api.example.com/v1"):
    return await check_connection(
        base_url=base_url,
        api_key=_KEY,
        protocol="anthropic_messages",
        model="claude-sonnet-4-5",
        resolve=_public,
        transport=httpx.MockTransport(respond),
    )


def _nonce(first: httpx.Request) -> str:
    match = re.search(r"check-[0-9a-f]+", first.content.decode())
    assert match
    return match.group()


async def test_a_round_trip_uses_the_cli_url_and_headers_and_no_bearer_token():
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        nonce = _nonce(seen[0])
        return _tool_answer(nonce) if len(seen) == 1 else _text_answer(nonce)

    result = await _check(respond)
    assert result.status == "supported", result.as_dict()
    assert (result.streaming, result.tool_call, result.final_answer) == (True, True, True)
    for request in seen:
        assert str(request.url) == "https://api.example.com/v1/messages?beta=true"
        assert request.headers["x-api-key"] == _KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
    first, second = (json.loads(request.content) for request in seen)
    assert first["tools"][0]["name"] == "cloud_agent_check"
    assert first["tools"][0]["input_schema"]["required"] == ["value"]
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "cloud_agent_check",
            "input": {"value": _nonce(seen[0])},
        }
    ]
    assert second["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": _nonce(seen[0]),
    }


@pytest.mark.parametrize(
    ("status", "category", "outcome"),
    [
        (401, "auth", "failed"),
        (403, "auth", "failed"),
        # No Messages endpoint at that URL: the provider does not offer the protocol.
        (404, "endpoint_missing", "unsupported"),
        (429, "rate_limited", "failed"),
        (529, "upstream_error", "failed"),
    ],
)
async def test_an_http_refusal_is_its_own_category_and_never_echoes_the_body(
    status, category, outcome
):
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"type": "error", "error": {"message": f"bad {_KEY}"}})

    result = await _check(respond)
    assert (result.status, result.category, result.http_status) == (outcome, category, status)
    assert _KEY not in json.dumps(result.as_dict())


async def test_a_json_answer_is_not_a_stream():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "message", "content": []})

    result = await _check(respond)
    assert result.category == "not_streaming" and result.status == "unsupported"


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "pause_turn"])
async def test_a_cut_short_or_refused_answer_is_never_a_success(stop):
    def respond(request: httpx.Request) -> httpx.Response:
        return _text_answer("nothing", stop=stop)

    result = await _check(respond)
    assert result.category == "stream_error" and result.status == "failed"


async def test_a_final_answer_without_the_tool_result_is_not_certified():
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _tool_answer(_nonce(seen[0])) if len(seen) == 1 else _text_answer("done")

    result = await _check(respond)
    assert result.category == "no_final_answer" and result.tool_call


async def test_a_truncated_byte_stream_is_incomplete():
    def respond(request: httpx.Request) -> httpx.Response:
        start = {"type": "message_start", "message": {"id": "m", "content": []}}
        body = f"event: message_start\ndata: {json.dumps(start)}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    result = await _check(respond)
    assert result.category == "incomplete_stream"


def _models(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5"}, {"id": "bad id"}]})


async def test_a_messages_only_connection_lists_models_the_way_the_cli_would():
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _models(request)

    models = await discover_models(
        base_url="https://gateway.example/anthropic/v1",
        api_key=_KEY,
        protocols=["anthropic_messages"],
        resolve=_public,
        transport=httpx.MockTransport(respond),
    )
    assert models == ["claude-sonnet-4-5"]
    assert str(seen[0].url) == "https://gateway.example/anthropic/v1/models"
    assert seen[0].headers["x-api-key"] == _KEY
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen[0].headers


async def test_a_mixed_connection_lists_models_with_the_openai_request():
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _models(request)

    await discover_models(
        base_url="https://api.example.com/v1",
        api_key=_KEY,
        protocols=["chat_completions", "anthropic_messages"],
        resolve=_public,
        transport=httpx.MockTransport(respond),
    )
    assert str(seen[0].url) == "https://api.example.com/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {_KEY}"
    assert "x-api-key" not in seen[0].headers


async def test_a_messages_model_list_refusal_is_a_category_not_a_body():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {_KEY}")

    with pytest.raises(CheckStop) as stop:
        await discover_models(
            base_url="https://api.example.com",
            api_key=_KEY,
            protocols=["anthropic_messages"],
            resolve=_public,
            transport=httpx.MockTransport(respond),
        )
    assert stop.value.category == "auth" and _KEY not in str(stop.value)
