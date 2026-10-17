"""PR summaries consider the whole captured conversation and contain no credentials."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent import pr_titles
from cloud_agent.pr_titles import generate_pr_title

_JOB = {
    "job_id": "ajob_2",
    "snapshot_id": 9,
    "publish_attempt_count": 0,
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
        attempt_id="pr-title:9:0",
        models=["session-model"],
        ttl_seconds=120,
    )
    grants.revoke.assert_awaited_once_with("publication-grant")


class _IdempotentGrants:
    """The gateway's mint: one grant per job and attempt, kept after its revoke."""

    def __init__(self) -> None:
        self.issued: dict[tuple[str, str], dict[str, Any]] = {}
        self.revoked: set[str] = set()

    async def mint(
        self, *, job_id: str, attempt_id: str, models: list[str], **_kwargs: Any
    ) -> dict[str, Any]:
        number = len(self.issued) + 1
        return self.issued.setdefault(
            (job_id, attempt_id),
            {"grant_id": f"grant-{number}", "token": f"token-{number}", "allowed_models": models},
        )

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.add(grant_id)
        return True

    def live(self, token: str) -> bool:
        return any(
            grant["token"] == token and grant["grant_id"] not in self.revoked
            for grant in self.issued.values()
        )


async def test_a_retried_publication_titles_its_pull_request_again():
    """A publication tried again after its title was written is titled again.

    The title's grant is revoked once the title is written, and the gateway's
    mint returns the grant a job and attempt already have, revoked or not.
    Every try of one snapshot's publication asked for the same attempt, so a
    try after a failed pull-request call was handed the revoked grant, every
    completion was refused, and the retry renamed the pull request to the
    fallback title.
    """
    grants = _IdempotentGrants()

    def handle(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if not grants.live(token):
            return httpx.Response(401, json={"error": {"type": "invalid_grant"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Add retry backoff"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        titles = [
            await generate_pr_title(
                {**_JOB, "publish_attempt_count": count}, ["retry.py"], grants=grants, client=client
            )
            for count in (0, 1)
        ]

    assert titles == ["[agent] Add retry backoff"] * 2
    assert len(grants.issued) == len(grants.revoked) == 2


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


def _session_of(fragments: int) -> list[dict[str, str]]:
    """A conversation whose title source splits into exactly ``fragments`` fragments."""
    return [{"role": "assistant", "content": "x" * (pr_titles._INPUT_CHARS * fragments - 1000)}]


async def test_fragments_are_summarized_together_so_the_title_fits_the_budget(monkeypatch):
    """The longest session that is titled costs one round of summaries and the title.

    Summarized one after another, each fragment waited for the one before it,
    and a session of a few fragments could spend the whole budget before its
    title was asked for. Here no summary is answered until every fragment has
    been asked about, which only a single round can satisfy.
    """
    monkeypatch.setattr(pr_titles, "_TOTAL_TIMEOUT_S", 2.0)
    fragments = pr_titles._MAX_FRAGMENTS
    asked: list[str] = []
    everyone_asked = asyncio.Event()

    async def handle(request):
        payload = json.loads(request.content)
        if payload["messages"][0]["content"] == pr_titles._TITLE_PROMPT:
            reply = "Fix retries and document backoff"
        else:
            asked.append(payload["messages"][1]["content"])
            if len(asked) == fragments:
                everyone_asked.set()
            await everyone_asked.wait()
            reply = f"Summary of fragment {len(asked)}"
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    job = {**_JOB, "session_messages": _session_of(fragments)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        title = await generate_pr_title(job, ["retry.py"], grants=_grants(), client=client)

    assert title == "[agent] Fix retries and document backoff"
    assert len(asked) == fragments


async def test_a_session_too_long_to_summarize_in_the_budget_asks_the_model_nothing(caplog):
    """Past the fragments one round can summarize, the publisher's own title is used.

    A long session used to be summarized fragment after fragment until the
    budget ran out, on every publication attempt: the owner paid for each call,
    and the title came from the fallback anyway. Now no grant is minted and no
    call is made.
    """
    caplog.set_level(logging.INFO, logger="cloud_agent.pr_titles")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Fix retries"}}]})

    grants = _grants()
    job = {**_JOB, "session_messages": _session_of(pr_titles._MAX_FRAGMENTS + 1)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        title = await generate_pr_title(job, ["retry.py"], grants=grants, client=client)

    assert title is None
    assert requests == []
    grants.mint.assert_not_awaited()
    failures = [
        record
        for record in caplog.records
        if record.getMessage() == "agent_pr_title_generation_failed"
    ]
    assert [failure.reason for failure in failures] == ["conversation_too_long"]


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


async def test_generic_completion_response_is_retried_for_a_substantive_title():
    requests = []
    replies = iter(
        [
            "I've implemented the feature. Here's a summary of what I did.",
            "Add a button to restart failed runs in a new session",
        ]
    )

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    grants = _grants()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        title = await generate_pr_title(_JOB, ["x"], grants=grants, client=client)

    assert title == "[agent] Add a button to restart failed runs in a new session"
    assert len(requests) == 2
    grants.revoke.assert_awaited_once_with("publication-grant")


@pytest.mark.parametrize(
    "content", ["Changes.ts: add restart navigation", "summary.py now preserves earlier turns"]
)
async def test_generated_filename_title_is_preserved(content):
    grants = _grants()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]}
            )
        )
    ) as client:
        title = await generate_pr_title(_JOB, ["x"], grants=grants, client=client)

    assert title == f"[agent] {content}"
    grants.revoke.assert_awaited_once_with("publication-grant")


async def test_generic_completion_responses_use_fallback_and_revoke_grant(caplog):
    caplog.set_level(logging.INFO, logger="cloud_agent.pr_titles")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "I've implemented the feature. Here's a summary of what I did."
                        }
                    }
                ]
            },
        )

    grants = _grants()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        title = await generate_pr_title(_JOB, ["x"], grants=grants, client=client)

    assert title is None
    assert len(requests) == 2
    grants.revoke.assert_awaited_once_with("publication-grant")
    failures = [
        record
        for record in caplog.records
        if record.getMessage() == "agent_pr_title_generation_failed"
    ]
    assert len(failures) == 1
    assert failures[0].snapshot_id == _JOB["snapshot_id"]
    assert failures[0].reason == "invalid_title"


async def test_missing_grants_logs_the_fallback_reason(caplog):
    caplog.set_level(logging.INFO, logger="cloud_agent.pr_titles")

    assert await generate_pr_title(_JOB, ["x"], grants=None) is None

    failures = [
        record
        for record in caplog.records
        if record.getMessage() == "agent_pr_title_generation_failed"
    ]
    assert len(failures) == 1
    assert failures[0].snapshot_id == _JOB["snapshot_id"]
    assert failures[0].reason == "missing_grants"


async def test_inference_failure_logs_status_without_sensitive_context(caplog):
    caplog.set_level(logging.INFO, logger="cloud_agent.pr_titles")
    private_conversation = "private-conversation-that-must-not-be-logged"
    private_error_body = "private-error-body-that-must-not-be-logged"
    job = {
        **_JOB,
        "session_messages": [{"role": "user", "content": private_conversation}],
    }
    grants = _grants()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503, text=f"{private_error_body}: scoped-secret; {private_conversation}"
            )
        )
    ) as client:
        assert await generate_pr_title(job, ["x"], grants=grants, client=client) is None

    grants.revoke.assert_awaited_once_with("publication-grant")
    failures = [
        record
        for record in caplog.records
        if record.getMessage() == "agent_pr_title_generation_failed"
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.snapshot_id == job["snapshot_id"]
    assert failure.reason == "inference_failed"
    assert failure.error_type == "HTTPStatusError"
    assert failure.status_code == 503
    assert failure.exc_info is None
    logged = caplog.text + json.dumps([record.__dict__ for record in caplog.records], default=str)
    for private_value in ("scoped-secret", private_conversation, private_error_body):
        assert private_value not in logged


async def test_a_fragment_that_fails_is_logged_as_that_call_failed(caplog):
    """Fragments are asked about at once, and the log still names the failed call's error."""
    caplog.set_level(logging.INFO, logger="cloud_agent.pr_titles")
    grants = _grants()
    job = {**_JOB, "session_messages": _session_of(2)}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    ) as client:
        assert await generate_pr_title(job, ["x"], grants=grants, client=client) is None

    grants.revoke.assert_awaited_once_with("publication-grant")
    (failure,) = [
        record
        for record in caplog.records
        if record.getMessage() == "agent_pr_title_generation_failed"
    ]
    assert (failure.reason, failure.error_type, failure.status_code) == (
        "inference_failed",
        "HTTPStatusError",
        503,
    )


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
