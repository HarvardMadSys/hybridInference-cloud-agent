"""Automatic names are compact, isolated, scoped, and safe to lose."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent import task_titles
from cloud_agent.task_titles import fallback_title, generate_thread_title, normalize_generated_title


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("\n # \n 修复  登录问题 \n More details", "修复 登录问题"),
        ("a" * 160, "a" * 35 + "…"),
        ("问" * 160, "问" * 35 + "…"),
        ("\n\n", "New task"),
        ("修复\u202e 登录", "修复 登录"),
        ("Fix\x1b\x00\tlogin\ud800 error", "Fix login error"),
    ],
)
def test_fallback_is_short_and_keeps_first_meaningful_line(prompt, expected):
    assert fallback_title(prompt) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (' "SPRUCE_8462 导出函数命名" ', "SPRUCE_8462 导出函数命名"),
        ("Fix   login error", "Fix login error"),
        ("Title\nExplanation", None),
        ("Fix\u202e title", None),
        ("<think>work</think>", None),
        ("```title```", None),
        ("", None),
        (None, None),
        ({"title": "Fix"}, None),
    ],
)
def test_generated_title_rejects_control_text(value, expected):
    assert normalize_generated_title(value) == expected


async def _generate(store, client):
    await generate_thread_title(
        store,
        job_id="ajob_title",
        attempt_id=7,
        lease_generation=2,
        prompt="SPRUCE_8462 导出函数命名 " + "secret prompt " * 1000,
        model="granted-model",
        sandbox_token="scoped-grant-secret",
        client=client,
    )


async def test_generation_uses_existing_scoped_grant_and_fenced_store(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example/")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "SPRUCE_8462 导出函数命名"}}]}
        )

    store = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await _generate(store, client)

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://gateway.example/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer scoped-grant-secret"
    body = json.loads(request.content)
    assert body["model"] == "granted-model"
    assert len(body["messages"][1]["content"]) == 4000
    assert body["max_tokens"] == 256
    assert body["stream"] is False
    assert "tools" not in body
    store.set_generated_thread_title.assert_awaited_once_with(
        job_id="ajob_title", attempt_id=7, lease_generation=2, title="SPRUCE_8462 导出函数命名"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="scoped-grant-secret"),
        httpx.Response(503, text="secret prompt"),
        httpx.Response(302, headers={"location": "https://other.example"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None, "reasoning_content": "secret prompt"},
                    }
                ]
            },
        ),
        httpx.Response(200, json={"choices": [{"message": {"content": "title\nexplanation"}}]}),
    ],
)
async def test_failed_generation_preserves_fallback_without_retry_or_sensitive_logs(
    monkeypatch, caplog, response
):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    requests = []

    def handle(request):
        requests.append(request)
        return response

    store = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await _generate(store, client)
    assert len(requests) == 1
    store.set_generated_thread_title.assert_not_awaited()
    assert "scoped-grant-secret" not in caplog.text
    assert "secret prompt" not in caplog.text


async def test_generation_has_total_deadline(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    monkeypatch.setattr(task_titles, "_TIMEOUT_S", 0.01)
    cancelled = asyncio.Event()

    async def handle(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    store = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await asyncio.wait_for(_generate(store, client), timeout=1)
    assert cancelled.is_set()
    store.set_generated_thread_title.assert_not_awaited()


async def test_missing_gateway_does_not_make_request(monkeypatch):
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    store, client = AsyncMock(), AsyncMock()
    await _generate(store, client)
    client.post.assert_not_awaited()
    store.set_generated_thread_title.assert_not_awaited()


async def test_store_failure_does_not_escape_background_task(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    store = AsyncMock()
    store.set_generated_thread_title.side_effect = RuntimeError("sensitive database details")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "Fix"}}]})
        )
    ) as client:
        await _generate(store, client)
    store.set_generated_thread_title.assert_awaited_once()
