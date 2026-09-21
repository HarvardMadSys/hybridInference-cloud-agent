"""Automatic names are compact, isolated, scoped, and safe to lose."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent import task_titles
from cloud_agent.task_titles import (
    fallback_title,
    generate_thread_title,
    normalize_generated_title,
    thread_title_needs_generation,
)

WARMUP = "⏳ The model is starting up — this takes about 14 minutes. Please wait…"


@pytest.fixture(autouse=True)
def immediate_title_retries(monkeypatch):
    monkeypatch.setattr(task_titles, "_RETRY_DELAYS_S", (0.0,) * 5)


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
        (WARMUP, None),
        ('"' + WARMUP + '"', None),
        ("⏳ The model is starting up — first load on Spark can take several minutes.", None),
        ("⏳ The model is starting up — this t…", None),
        ("Fix model startup", "Fix model startup"),
    ],
)
def test_generated_title_rejects_control_text(value, expected):
    assert normalize_generated_title(value) == expected


async def _generate(store, client):
    if not store.get_thread_title_prompt.side_effect:
        store.get_thread_title_prompt.return_value = (
            "SPRUCE_8462 导出函数命名 " + "secret prompt " * 1000
        )
    await generate_thread_title(
        store,
        job_id="ajob_title",
        attempt_id=7,
        lease_generation=2,
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
    monkeypatch.setattr(task_titles, "_TOTAL_TIMEOUT_S", 0.01)
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


@pytest.mark.parametrize("source", ["fallback", "generated", "inherited", "manual", None])
def test_warmup_recovery_preserves_owner_titles(source):
    assert thread_title_needs_generation(source, WARMUP) is (
        source in {"fallback", "generated", "inherited"}
    )
    assert thread_title_needs_generation(source, "Investigate model startup") is (
        source == "fallback"
    )


async def test_warmup_then_ready_recovers_without_saving_banner(monkeypatch):
    """Reproduce the 200/stop proxy response and recover from the next model call."""
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    monkeypatch.setattr(task_titles, "_RETRY_DELAYS_S", (0, 2, 5))
    sleep = AsyncMock()
    monkeypatch.setattr(task_titles.asyncio, "sleep", sleep)
    contents = iter([WARMUP, WARMUP, "SPRUCE_8462 CSV export"])
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(contents)}, "finish_reason": "stop"}]},
        )

    store = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await _generate(store, client)
    assert len(requests) == 3
    assert all(request == requests[0] for request in requests)
    assert [call.args[0] for call in sleep.await_args_list] == [2, 5]
    store.set_generated_thread_title.assert_awaited_once_with(
        job_id="ajob_title", attempt_id=7, lease_generation=2, title="SPRUCE_8462 CSV export"
    )


@pytest.mark.parametrize("status", [200, 408, 429, 500, 502, 503, 504])
async def test_transient_failures_are_bounded_and_never_saved(monkeypatch, status):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"choices": [{"message": {"content": WARMUP}}]})

    store = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await _generate(store, client)
    assert calls == 5
    store.set_generated_thread_title.assert_not_awaited()


async def test_each_retry_rechecks_title_ownership_and_attempt_fence(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    store = AsyncMock()
    # The next read may be ineligible due to a manual rename, cancellation,
    # deletion, another completed title request, or supersession of the lease.
    store.get_thread_title_prompt.side_effect = ["Original task", None]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": WARMUP}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await _generate(store, client)
    assert len(requests) == 1
    assert store.get_thread_title_prompt.await_count == 2
    for call in store.get_thread_title_prompt.await_args_list:
        assert call.kwargs == {"job_id": "ajob_title", "attempt_id": 7, "lease_generation": 2}
    store.set_generated_thread_title.assert_not_awaited()


async def test_transport_failure_then_ready_retries(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example")
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("private upstream details", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Export CSV"}}]})

    store = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await _generate(store, client)
    assert calls == 2
    store.set_generated_thread_title.assert_awaited_once()
