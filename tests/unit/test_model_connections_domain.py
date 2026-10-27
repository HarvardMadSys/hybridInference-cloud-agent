"""What an owner may store as a model connection, when a turn may use it, and how it is checked.

The check dials an address the owner typed, so it is exercised against a fake
resolver and an in-process upstream: the address rules, the absence of
redirects, which answers count as supported, unsupported or failed, and that
nothing the upstream sends -- the key echoed in an error body included -- comes
back in the result.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from cloud_agent.application.model_connection_check import (
    CHECK_MESSAGES,
    CheckStop,
    check_connection,
    discover_models,
    guard_base_url,
)
from cloud_agent.domain.model_connections import (
    ModelConnectionInvalid,
    ModelConnectionUnusable,
    clean_base_url,
    clean_models,
    clean_name,
    clean_protocols,
    require_usable,
)

_KEY = "sk-check-secret-9876543210"
_BASE = "https://models.example/v1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("  https://API.Example.com/v1beta/openai/ ", "https://api.example.com/v1beta/openai"),
        ("https://openrouter.ai:443/api/v1", "https://openrouter.ai/api/v1"),
        ("https://api.deepseek.com", "https://api.deepseek.com"),
    ],
)
def test_a_base_url_is_kept_as_its_provider_spells_it(raw, expected):
    assert clean_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://api.example.com/v1",
        "https://api.example.com:8443/v1",
        "https://user:key@api.example.com/v1",
        "https://api.example.com/v1?key=abc",
        "https://api.example.com/v1#frag",
        "https://[2001:db8::1]/v1",
        "https://api.example.com/v 1",
        "ftp://api.example.com",
        "",
    ],
)
def test_a_base_url_a_sandbox_could_not_reach_safely_is_refused(raw):
    with pytest.raises(ModelConnectionInvalid):
        clean_base_url(raw)


def test_names_models_and_protocols_are_normalized_and_bounded():
    assert clean_name("  My   OpenAI ") == "My OpenAI"
    assert clean_models([" gpt-4.1 ", "", "openai/gpt-4.1", "gpt-4.1"]) == [
        "gpt-4.1",
        "openai/gpt-4.1",
    ]
    assert clean_protocols(["responses", "chat_completions"]) == ["chat_completions", "responses"]
    for bad in ([], ["bad model"], ["x" * 201], [f"m{i}" for i in range(65)]):
        with pytest.raises(ModelConnectionInvalid):
            clean_models(bad)
    for bad in ([], ["anthropic_messages"]):
        with pytest.raises(ModelConnectionInvalid):
            clean_protocols(bad)


def _row(**changes: Any) -> dict[str, Any]:
    row = {
        "enabled": True,
        "deleted_at": None,
        "protocols": ["chat_completions"],
        "models": ["gpt-4.1"],
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    ("row", "runtime", "model", "effort", "reason"),
    [
        (None, "opencode", "gpt-4.1", None, "not_found"),
        (_row(deleted_at="now", enabled=False), "opencode", "gpt-4.1", None, "deleted"),
        (_row(enabled=False), "opencode", "gpt-4.1", None, "disabled"),
        (_row(), "claude-code", "gpt-4.1", None, "runtime_unsupported"),
        (_row(), "codex", "gpt-4.1", None, "protocol_unsupported"),
        (_row(), "opencode", "gpt-4o", None, "model_not_listed"),
        (_row(), "opencode", "gpt-4.1", "high", "effort_unsupported"),
    ],
)
def test_every_unusable_pairing_is_refused_with_its_reason(row, runtime, model, effort, reason):
    with pytest.raises(ModelConnectionUnusable) as refused:
        require_usable(row, runtime=runtime, model=model, reasoning_effort=effort)
    assert refused.value.reason == reason


def test_a_usable_pairing_names_the_protocol_the_runtime_speaks():
    both = _row(protocols=["chat_completions", "responses"])
    assert require_usable(both, runtime="pi", model="gpt-4.1") == "chat_completions"
    assert require_usable(both, runtime="codex", model="gpt-4.1") == "responses"


def _resolver(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        del host, port
        return list(addresses)

    return resolve


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.1.2.3",),
        ("169.254.169.254",),
        ("100.64.0.1",),
        ("93.184.216.34", "192.168.1.10"),
        ("::ffff:127.0.0.1",),
        ("2606:4700::1111",),
    ],
)
async def test_an_address_that_is_not_only_public_ipv4_is_never_dialled(addresses):
    with pytest.raises(CheckStop) as stop:
        await guard_base_url(_BASE, resolve=_resolver(*addresses))
    assert stop.value.category == "blocked_address"


async def test_a_deployments_protected_ranges_are_refused_too(monkeypatch):
    monkeypatch.setenv("AGENT_EGRESS_PROTECTED_CIDRS", "93.184.216.0/24")
    with pytest.raises(CheckStop):
        await guard_base_url(_BASE, resolve=_resolver("93.184.216.34"))


def _sse(*events: dict[str, Any], done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(event)}\n\n" for event in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _stream(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})


class _Upstream:
    """Answers each request in turn and remembers what it was sent.

    An answer is a response, or a function of the check's nonce -- read from
    the first request's prompt, as a real model would read it -- that builds
    one, so a correlated round trip can be answered correctly or not.
    """

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def nonce(self) -> str:
        body = json.loads(self.requests[0].content)
        prompt = (body.get("messages") or body.get("input"))[0]["content"]
        match = re.search(r"check-[0-9a-f]+", prompt)
        assert match is not None
        return match.group()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        return answer(self.nonce()) if callable(answer) else answer

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


async def _check(upstream: _Upstream, protocol: str = "chat_completions") -> dict[str, Any]:
    result = await check_connection(
        base_url=_BASE,
        api_key=_KEY,
        protocol=protocol,
        model="gpt-4.1",
        resolve=_resolver("93.184.216.34"),
        transport=upstream.transport(),
    )
    return result.as_dict()


def _chat_tool_call(nonce: str) -> httpx.Response:
    arguments = json.dumps({"value": nonce})
    return _stream(
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "cloud_"}}
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"name": "agent_check", "arguments": arguments},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
    )


def _chat_answer(nonce: str) -> httpx.Response:
    return _stream(
        _sse(
            {"choices": [{"delta": {"content": f"The result is {nonce}."}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
    )


async def test_a_streamed_tool_call_round_trip_is_supported_and_carries_the_result_back():
    upstream = _Upstream(_chat_tool_call, _chat_answer)
    result = await _check(upstream)

    assert result["status"] == "supported"
    assert result["streaming"] and result["tool_call"] and result["final_answer"]
    first, second = (json.loads(request.content) for request in upstream.requests)
    assert all(str(r.url) == f"{_BASE}/chat/completions" for r in upstream.requests)
    assert all(r.headers["authorization"] == f"Bearer {_KEY}" for r in upstream.requests)
    assert first["stream"] is True and first["tools"][0]["function"]["name"] == "cloud_agent_check"
    tool_message = second["messages"][2]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"
    assert tool_message["content"] == upstream.nonce()
    assert second["messages"][1]["tool_calls"][0]["function"]["name"] == "cloud_agent_check"


async def test_the_responses_protocol_is_checked_with_its_own_events():
    def call(nonce: str) -> httpx.Response:
        item = {
            "type": "function_call",
            "id": "fc_item",
            "call_id": "fc_1",
            "name": "cloud_agent_check",
            "arguments": json.dumps({"value": nonce}),
        }
        return _stream(
            _sse(
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed", "response": {"status": "completed"}},
                done=False,
            )
        )

    def answer(nonce: str) -> httpx.Response:
        return _stream(
            _sse(
                {"type": "response.output_text.delta", "delta": nonce},
                {"type": "response.completed", "response": {"status": "completed"}},
                done=False,
            )
        )

    upstream = _Upstream(call, answer)
    result = await _check(upstream, protocol="responses")

    assert result["status"] == "supported"
    assert str(upstream.requests[0].url) == f"{_BASE}/responses"
    second = json.loads(upstream.requests[1].content)
    assert second["input"][1]["id"] == "fc_item", "the call item goes back as it came"
    output = second["input"][2]
    assert output == {"type": "function_call_output", "call_id": "fc_1", "output": upstream.nonce()}


@pytest.mark.parametrize(
    ("answers", "status", "category"),
    [
        ([httpx.Response(401, json={"error": f"bad key {_KEY}"})], "failed", "auth"),
        ([httpx.Response(429, text="slow down")], "failed", "rate_limited"),
        ([httpx.Response(503, text=f"upstream said {_KEY}")], "failed", "upstream_error"),
        ([httpx.Response(404, text="no such model")], "unsupported", "endpoint_missing"),
        ([httpx.Response(400, text="tools unsupported")], "unsupported", "request_rejected"),
        ([httpx.Response(200, json={"choices": []})], "unsupported", "not_streaming"),
        (
            [
                _stream(
                    _sse(
                        {"choices": [{"delta": {"content": "hi"}}]},
                        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                    )
                )
            ],
            "unsupported",
            "no_tool_call",
        ),
        (
            [
                _chat_tool_call,
                _stream(
                    _sse(
                        {"choices": [{"delta": {"content": "done"}}]},
                        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                    )
                ),
            ],
            "unsupported",
            "no_final_answer",
        ),
        (
            [_stream(_sse({"choices": [{"delta": {"content": "hi"}}]}, done=False))],
            "failed",
            "incomplete_stream",
        ),
        (
            [_stream(_sse({"error": {"message": f"key {_KEY} is over quota"}}))],
            "failed",
            "stream_error",
        ),
        (
            [
                _stream(
                    _sse(
                        {"choices": [{"delta": {"content": "hi"}}]},
                        {"choices": [{"delta": {}, "finish_reason": "length"}]},
                    )
                )
            ],
            "failed",
            "stream_error",
        ),
    ],
)
async def test_failures_and_unsupported_providers_are_told_apart_and_never_echoed(
    answers, status, category
):
    result = await _check(_Upstream(*answers))

    assert (result["status"], result["category"]) == (status, category)
    assert result["message"] == CHECK_MESSAGES[category]
    assert _KEY not in json.dumps(result)


async def test_a_redirect_is_reported_and_never_followed_with_the_key():
    upstream = _Upstream(
        httpx.Response(307, headers={"location": "https://collector.example/steal"}),
        _chat_answer,
    )
    result = await _check(upstream)

    assert (result["status"], result["category"]) == ("failed", "redirected")
    assert [str(r.url.host) for r in upstream.requests] == ["models.example"]


async def test_an_unreachable_provider_is_a_failure_not_an_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await check_connection(
        base_url=_BASE,
        api_key=_KEY,
        protocol="chat_completions",
        model="gpt-4.1",
        resolve=_resolver("93.184.216.34"),
        transport=httpx.MockTransport(refuse),
    )
    assert (result.status, result.category) == ("failed", "unreachable")


async def test_model_discovery_lists_ids_and_is_only_a_convenience():
    listed = httpx.Response(
        200, json={"data": [{"id": "gpt-4.1"}, {"id": "bad id"}, {"id": "gpt-4.1"}, {"x": 1}]}
    )
    upstream = _Upstream(listed)
    models = await discover_models(
        base_url=_BASE,
        api_key=_KEY,
        resolve=_resolver("93.184.216.34"),
        transport=upstream.transport(),
    )
    assert models == ["gpt-4.1"]
    assert str(upstream.requests[0].url) == f"{_BASE}/models"

    with pytest.raises(CheckStop) as stop:
        await discover_models(
            base_url=_BASE,
            api_key=_KEY,
            resolve=_resolver("93.184.216.34"),
            transport=_Upstream(httpx.Response(404)).transport(),
        )
    assert stop.value.category == "endpoint_missing"
