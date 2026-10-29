"""Malformed upstream streams are failed checks, never owner-facing server errors."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from cloud_agent.application.model_connection_check import check_connection

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _public_address(_host: str, _port: int) -> list[str]:
    return ["8.8.8.8"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "payload"),
    [
        ("chat_completions", {"choices": 1}),
        ("chat_completions", {"choices": [{"delta": {"tool_calls": 1}}]}),
        ("chat_completions", {"choices": [{"finish_reason": []}]}),
        ("responses", {"type": []}),
    ],
)
async def test_malformed_stream_is_a_failed_check(protocol: str, payload: dict[str, Any]) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
        )

    result = await check_connection(
        base_url="https://provider.example/custom",
        api_key="fake-test-key-not-a-credential",
        protocol=protocol,
        model="test-model",
        resolve=_public_address,
        transport=httpx.MockTransport(respond),
    )
    assert result.status != "supported"


@pytest.mark.asyncio
async def test_invalid_upstream_encoding_is_a_failed_check() -> None:
    chunks_read: list[bytes] = []

    class EncodedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            chunks_read.append(b"invalid-gzip-stream")
            yield chunks_read[-1]

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
            stream=EncodedStream(),
        )

    result = await check_connection(
        base_url="https://provider.example/custom",
        api_key="fake-test-key-not-a-credential",
        protocol="chat_completions",
        model="test-model",
        resolve=_public_address,
        transport=httpx.MockTransport(respond),
    )
    assert result.status != "supported"
    assert not chunks_read, "compressed responses must be refused before decoding allocates memory"


@pytest.mark.asyncio
async def test_invalid_protected_network_setting_refuses_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_EGRESS_PROTECTED_CIDRS", "8.8.8.0/invalid-mask")

    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("a malformed protected-network policy must not allow an upstream call")

    result = await check_connection(
        base_url="https://provider.example/custom",
        api_key="fake-test-key-not-a-credential",
        protocol="chat_completions",
        model="test-model",
        resolve=_public_address,
        transport=httpx.MockTransport(respond),
    )
    assert result.status == "failed"
    assert result.category == "blocked_address"
