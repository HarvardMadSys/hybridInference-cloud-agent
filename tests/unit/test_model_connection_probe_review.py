"""A provider check must prove a complete, correlated tool round trip."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from cloud_agent.application.model_connection_check import check_connection


async def _public_address(_host: str, _port: int) -> list[str]:
    return ["8.8.8.8"]


def _sse(events: list[dict[str, Any]], *, done: bool = True) -> httpx.Response:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def _nonce(body: dict[str, Any], protocol: str) -> str:
    prompt = body["messages" if protocol == "chat_completions" else "input"][0]["content"]
    match = re.search(r"check-[0-9a-f]+", prompt)
    assert match is not None
    return match.group()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["valid", "valid_fragmented", "arguments", "call_id", "answer", "truncated", "error"]
)
async def test_chat_check_requires_complete_tool_round_trip(fault: str) -> None:
    """Text or a tool-shaped object alone cannot certify a usable connection."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        nonce = _nonce(requests[0], "chat_completions")
        if len(requests) == 1:
            call: dict[str, Any] = {
                "index": 0,
                "id": "call_probe",
                "type": "function",
                "function": {
                    "name": "cloud_agent_check",
                    "arguments": json.dumps({"value": "wrong" if fault == "arguments" else nonce}),
                },
            }
            if fault == "call_id":
                del call["id"]
            events = [{"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}]
            if fault == "valid_fragmented":
                arguments = call["function"]["arguments"]
                call["function"]["arguments"] = arguments[:10]
                events.append(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": arguments[10:]}}
                                    ]
                                },
                            }
                        ]
                    }
                )
            events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            return _sse(events)
        events: list[dict[str, Any]] = [
            {
                "choices": [
                    {"index": 0, "delta": {"content": "unrelated" if fault == "answer" else nonce}}
                ]
            }
        ]
        if fault == "error":
            events.append({"error": {"type": "server_error", "message": "synthetic failure"}})
        elif fault != "truncated":
            events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        return _sse(events, done=fault not in {"truncated", "error"})

    result = await check_connection(
        base_url="https://provider.example/custom",
        api_key="fake-test-key-not-a-credential",
        protocol="chat_completions",
        model="test-model",
        resolve=_public_address,
        transport=httpx.MockTransport(respond),
    )
    if fault.startswith("valid"):
        assert result.status == "supported"
    else:
        assert result.status != "supported", f"check certified a {fault} failure"


@pytest.mark.asyncio
async def test_responses_check_refuses_failed_response_after_text_delta() -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        nonce = _nonce(requests[0], "responses")
        if len(requests) == 1:
            return _sse(
                [
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "id": "fc_probe",
                            "call_id": "call_probe",
                            "name": "cloud_agent_check",
                            "arguments": json.dumps({"value": nonce}),
                        },
                    },
                    {"type": "response.completed", "response": {"status": "completed"}},
                ],
                done=False,
            )
        return _sse(
            [
                {"type": "response.output_text.delta", "delta": nonce},
                {
                    "type": "response.failed",
                    "response": {"status": "failed", "error": {"message": "synthetic failure"}},
                },
            ],
            done=False,
        )

    result = await check_connection(
        base_url="https://provider.example/custom",
        api_key="fake-test-key-not-a-credential",
        protocol="responses",
        model="test-model",
        resolve=_public_address,
        transport=httpx.MockTransport(respond),
    )
    assert result.status != "supported"


@pytest.mark.asyncio
async def test_responses_check_keeps_reasoning_output_for_tool_continuation() -> None:
    requests: list[dict[str, Any]] = []
    reasoning = {
        "type": "reasoning",
        "id": "rs_probe",
        "summary": [],
        "encrypted_content": "opaque-fixture-context",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        nonce = _nonce(requests[0], "responses")
        if len(requests) == 1:
            call = {
                "type": "function_call",
                "id": "fc_probe",
                "call_id": "call_probe",
                "name": "cloud_agent_check",
                "arguments": json.dumps({"value": nonce}),
            }
            return _sse(
                [
                    {"type": "response.output_item.done", "item": reasoning},
                    {"type": "response.output_item.done", "item": call},
                    {
                        "type": "response.completed",
                        "response": {"status": "completed", "output": [reasoning, call]},
                    },
                ],
                done=False,
            )
        if reasoning not in body["input"]:
            return httpx.Response(400, json={"error": {"message": "Missing reasoning context"}})
        return _sse(
            [
                {"type": "response.output_text.delta", "delta": nonce},
                {"type": "response.completed", "response": {"status": "completed"}},
            ],
            done=False,
        )

    result = await check_connection(
        base_url="https://provider.example/custom",
        api_key="fake-test-key-not-a-credential",
        protocol="responses",
        model="test-model",
        resolve=_public_address,
        transport=httpx.MockTransport(respond),
    )
    assert result.status == "supported"
