"""A translated check: the client's probe, through the helper's converter, on a Chat upstream.

The control plane checks Codex or Claude Code on a Chat Completions
connection with the same ``protocol_compat`` gate and stream converter the
member's helper runs: the provider sees Chat Completions at
``/chat/completions`` with the bearer key, and the client protocol's own
parser reads the converted answer.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from cloud_agent.application.model_connection_check import check_connection

_KEY = "fixture-chat-key-0123456789"
pytestmark = pytest.mark.asyncio


async def _public(_host: str, _port: int) -> list[str]:
    return ["8.8.8.8"]


def _sse(*payloads: dict[str, Any], done: bool = True) -> httpx.Response:
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads) + (
        "data: [DONE]\n\n" if done else ""
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class _ChatUpstream:
    """Calls the offered tool with the nonce, then answers with the tool's result."""

    def __init__(self, *, truncate_final: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.truncate_final = truncate_final

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        nonce = re.search(r"check-[0-9a-f]+", json.dumps(body["messages"][0])).group()
        tool = body["tools"][0]["function"]["name"]
        if len(self.requests) == 1:
            call = {
                "index": 0,
                "id": "call_check",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps({"value": nonce})},
            }
            return _sse(
                _chunk({"role": "assistant", "tool_calls": [call]}), _chunk({}, "tool_calls")
            )
        result = next(m for m in body["messages"] if m["role"] == "tool")
        assert result["tool_call_id"] == "call_check" and nonce in result["content"]
        if self.truncate_final:
            return _sse(_chunk({"content": nonce}), done=False)
        return _sse(_chunk({"content": nonce}), _chunk({}, "stop"))


@pytest.mark.parametrize("client", ["responses", "anthropic_messages"])
async def test_a_translated_check_runs_the_round_trip_as_chat_completions(client):
    upstream = _ChatUpstream()
    result = await check_connection(
        base_url="https://chat.example/prefix/v1",
        api_key=_KEY,
        protocol=client,
        model="chat-model",
        upstream_protocol="chat_completions",
        resolve=_public,
        transport=httpx.MockTransport(upstream),
    )
    assert result.status == "supported", result.as_dict()
    stored = result.as_dict()
    assert (stored["protocol"], stored["upstream_protocol"], stored["mode"]) == (
        client,
        "chat_completions",
        "translated",
    )
    assert len(upstream.requests) == 2
    for request in upstream.requests:
        assert str(request.url) == "https://chat.example/prefix/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {_KEY}"
        assert "x-api-key" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == "chat-model" and body["stream"] is True
        assert "messages" in body and "input" not in body
    assert _KEY not in json.dumps(stored)


@pytest.mark.parametrize("client", ["responses", "anthropic_messages"])
async def test_a_translated_check_never_certifies_an_unfinished_answer(client):
    result = await check_connection(
        base_url="https://chat.example/v1",
        api_key=_KEY,
        protocol=client,
        model="chat-model",
        upstream_protocol="chat_completions",
        resolve=_public,
        transport=httpx.MockTransport(_ChatUpstream(truncate_final=True)),
    )
    assert result.status != "supported"
    assert result.tool_call and not result.final_answer


async def test_a_direct_check_is_unchanged_and_no_other_pair_is_translated():
    seen: list[httpx.Request] = []

    def native(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    result = await check_connection(
        base_url="https://api.example/v1",
        api_key=_KEY,
        protocol="responses",
        model="m",
        resolve=_public,
        transport=httpx.MockTransport(native),
    )
    assert str(seen[0].url) == "https://api.example/v1/responses"
    assert result.as_dict()["mode"] == "direct"
    for client, upstream in (
        ("chat_completions", "responses"),
        ("responses", "anthropic_messages"),
    ):
        with pytest.raises(ValueError):
            await check_connection(
                base_url="https://api.example/v1",
                api_key=_KEY,
                protocol=client,
                model="m",
                upstream_protocol=upstream,
                resolve=_public,
            )
