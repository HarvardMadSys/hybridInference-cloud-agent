"""PR summaries consider the whole captured conversation and contain no credentials."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent import pr_titles
from cloud_agent.pr_titles import generate_pr_title

_JOB = {
    "job_id": "ajob_2",
    "snapshot_id": 9,
    "user_id": "owner",
    "model": "session-model",
    "session_messages": [
        {"role": "user", "content": "Fix retry timeouts."},
        {"role": "assistant", "content": "I found the retry loop."},
        {"role": "user", "content": "Keep the existing timeout; add backoff instead."},
        {"role": "assistant", "content": "Added exponential backoff and regression coverage."},
        {"role": "user", "content": "Also document retries."},
        {"role": "assistant", "content": "Done.\nUpdated the retry documentation."},
    ],
}


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://gateway.example/")


def _grants():
    grants = AsyncMock()
    grants.mint.return_value = {
        "grant_id": "publication-grant",
        "token": "scoped-secret",
        "allowed_models": ["session-model"],
    }
    return grants


async def test_title_summarizes_all_user_and_assistant_messages():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Add retry backoff, regression tests, and documentation"
                        }
                    }
                ]
            },
        )

    grants = _grants()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        title = await generate_pr_title(
            _JOB, ["retry.py", "README.md"], grants=grants, client=client
        )

    assert title == "[agent] Add retry backoff, regression tests, and documentation"
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer scoped-secret"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "session-model"
    source = json.loads(payload["messages"][1]["content"])
    assert source["conversation"] == _JOB["session_messages"]
    assert source["changed_files"] == ["retry.py", "README.md"]
    assert "tools" not in payload and payload["stream"] is False
    assert "scoped-secret" not in title
    grants.mint.assert_awaited_once_with(
        user_id="owner",
        job_id="ajob_2",
        attempt_id="pr-title:9",
        models=["session-model"],
        ttl_seconds=120,
    )
    grants.revoke.assert_awaited_once_with("publication-grant")


async def test_long_transcripts_include_every_message_through_chunk_summaries():
    requests = []
    messages = [
        {"role": "assistant", "content": "Early change. " + "details " * 4000},
        *_JOB["session_messages"],
        {"role": "assistant", "content": "Final change."},
    ]
    source = json.dumps(
        {"conversation": messages, "changed_files": ["retry.py"]}, ensure_ascii=False
    )

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) <= 2:
            result = f"Summary of fragment {len(requests)}"
        else:
            result = "Fix retries and document backoff"
        return httpx.Response(200, json={"choices": [{"message": {"content": result}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        title = await generate_pr_title(
            {**_JOB, "session_messages": messages}, ["retry.py"], grants=_grants(), client=client
        )

    assert title == "[agent] Fix retries and document backoff"
    assert len(requests) == 3
    assert "".join(request["messages"][1]["content"] for request in requests[:2]) == source
    final = requests[-1]["messages"][1]["content"]
    assert "Summary of fragment 1" in final and "Summary of fragment 2" in final


@pytest.mark.parametrize(
    "content", [None, "", "Title\nexplanation", "<think>title</think>", "Fix\u202e title"]
)
async def test_malformed_response_uses_fallback_and_revokes_grant(content):
    grants = _grants()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]}
            )
        )
    ) as client:
        assert await generate_pr_title(_JOB, ["x"], grants=grants, client=client) is None
    grants.revoke.assert_awaited_once_with("publication-grant")


@pytest.mark.parametrize(
    "grant",
    [
        {"grant_id": "publication-grant", "token": "scoped-secret", "allowed_models": ["other"]},
        {"grant_id": "publication-grant", "token": "scoped-secret"},
        {"grant_id": "publication-grant", "allowed_models": ["session-model"]},
    ],
)
async def test_insufficient_grant_does_not_call_inference(grant):
    grants = _grants()
    grants.mint.return_value = grant
    client = AsyncMock()
    assert await generate_pr_title(_JOB, ["x"], grants=grants, client=client) is None
    client.post.assert_not_awaited()
    grants.revoke.assert_awaited_once_with("publication-grant")


async def test_warmup_response_is_retried_and_does_not_become_a_title():
    replies = iter(["⏳ The model is starting up — please wait", "修复重试并完善文档"])
    grants = _grants()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": next(replies)}}]}
            )
        )
    ) as client:
        title = await generate_pr_title(_JOB, ["x"], grants=grants, client=client)
    assert title == "[agent] 修复重试并完善文档"
    grants.revoke.assert_awaited_once()


async def test_timeout_cannot_block_publication(monkeypatch, caplog):
    monkeypatch.setattr(pr_titles, "_TOTAL_TIMEOUT_S", 0.01)
    grants = _grants()

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    client = AsyncMock()
    client.post.side_effect = blocked
    assert await generate_pr_title(_JOB, ["x"], grants=grants, client=client) is None
    grants.revoke.assert_awaited_once_with("publication-grant")
    assert "scoped-secret" not in caplog.text


async def test_missing_gateway_or_grants_preserves_publication(monkeypatch):
    assert await generate_pr_title(_JOB, ["x"], grants=None) is None
    monkeypatch.delenv("GATEWAY_BASE_URL")
    grants = _grants()
    assert await generate_pr_title(_JOB, ["x"], grants=grants) is None
    grants.mint.assert_not_awaited()
