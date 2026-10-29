"""The Context view and Context insights: the owner's own, never cached."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.admin_observability import encode_cursor
from cloud_agent.api import context
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.context_insights import ContextInsightsCursor, context_cursor_scope

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
_SUMMARY = {
    "conversations": 0,
    "conversations_observed": 0,
    "main_requests": 0,
    "subagent_requests": 0,
    "compactions": 0,
    "peak_prompt_tokens": None,
    "peak_thread_id": None,
    "deleted_conversation_runs": 0,
}


class _NoUsage:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def batched_selection(self, **kwargs):
        self.calls.append(kwargs)
        for batch in ():
            yield batch


def _row(thread_id: str) -> dict:
    return {
        "thread_id": thread_id,
        "title": "T",
        "repo": "r",
        "archived": False,
        "latest_job_id": f"{thread_id}-j1",
        "turns": 1,
        "first_turn_at": NOW,
        "last_turn_at": NOW,
        "runtimes": ["codex"],
        "models": ["m"],
        "attempts": 1,
        "attempts_profiled": 1,
        "main_requests": 2,
        "subagent_requests": 0,
        "peak_prompt_tokens": 1000,
        "latest_prompt_tokens": 900,
        "latest_observed_at": NOW,
        "context_window": None,
        "compactions": 0,
        "sort_number": 1000,
    }


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "context-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(context.router)
    queries = AsyncMock()
    queries.thread_context.return_value = None
    queries.insights_page.return_value = {
        "page": [_row("athr_1"), _row("athr_2")],
        "summary": _SUMMARY,
        "pending": 3,
        "refreshed": 2,
    }
    owner_usage = AsyncMock()
    owner_usage.first_record_at.return_value = None
    usage = _NoUsage()
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        context_queries=queries, owner_usage=owner_usage, admin_usage=usage
    )
    application.state.queries = queries
    application.state.usage = usage
    return application


async def _get(app, path, *, owner="owner-1", **params):
    headers = {"Authorization": f"Bearer {identity.mint_session(user_id=owner)}"} if owner else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as http:
        return await http.get(path, headers=headers, params=params)


async def test_a_conversation_that_is_not_the_callers_is_absent(app):
    response = await _get(app, "/v1/agent/jobs/ajob_x/thread/context")
    assert response.status_code == 404
    app.state.queries.thread_context.assert_awaited_once_with(job_id="ajob_x", user_id="owner-1")


async def test_the_view_is_assembled_for_its_owner_and_not_cached(app):
    app.state.queries.thread_context.return_value = {
        "thread_id": "athr_1",
        "jobs": [{"job_id": "j1", "turn_no": 1, "runtime": "pi", "state": "succeeded"}],
        "attempts": [],
    }
    response = await _get(app, "/v1/agent/jobs/j1/thread/context")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["thread_id"] == "athr_1" and body["latest"] is None
    assert body["capabilities"][0]["live_occupancy"] == "not_collected"


@pytest.mark.parametrize("path", ["jobs/j1/thread/context", "usage/context"])
async def test_both_reads_need_a_signed_in_owner(app, path):
    assert (await _get(app, f"/v1/agent/{path}", owner=None)).status_code == 401


async def test_insights_page_ranks_the_owners_runs_and_says_what_is_pending(app):
    response = await _get(app, "/v1/agent/usage/context", range="30d", sort="peak", limit=1)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    call = app.state.queries.insights_page.await_args.kwargs
    assert call["user_id"] == "owner-1" and call["sort"] == "peak" and call["limit"] == 1
    body = response.json()
    assert body["period"]["preset"] == "30d"
    assert body["profiling"] == {
        "pending_attempts": 3,
        "pending_capped": False,
        "refreshed": 2,
        "complete": False,
    }
    assert [c["thread_id"] for c in body["conversations"]] == ["athr_1"]
    # Cost is summed by the server for exactly the listed conversations.
    [usage_call] = app.state.usage.calls
    assert usage_call["thread_ids"] == ["athr_1"] and usage_call["user_id"] == "owner-1"
    assert body["conversations"][0]["usage"] is None  # no runs had usage: not zero
    assert body["next_cursor"]


async def test_a_later_page_keeps_the_first_pages_interval(app):
    since, until = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    cursor = ContextInsightsCursor(
        scope=context_cursor_scope("recent", "all"),
        since=since,
        until=until,
        number=0,
        at=NOW,
        thread_id="athr_1",
    )
    response = await _get(app, "/v1/agent/usage/context", range="all", cursor=encode_cursor(cursor))
    assert response.status_code == 200
    call = app.state.queries.insights_page.await_args.kwargs
    assert (call["since"], call["until"]) == (since, until)
    assert call["cursor"] == cursor


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "cost"},
        {"range": "90d"},
        {"limit": 0},
        {"limit": 51},
        {"cursor": "nope"},
        {
            "range": "7d",
            "cursor": encode_cursor(
                ContextInsightsCursor(
                    scope=context_cursor_scope("peak", "7d"),
                    since=NOW,
                    until=NOW,
                    number=0,
                    at=NOW,
                    thread_id="x",
                )
            ),
        },
    ],
)
async def test_a_bad_insights_request_is_refused_before_any_read(app, params):
    response = await _get(app, "/v1/agent/usage/context", **params)
    assert response.status_code == 422
    app.state.queries.insights_page.assert_not_awaited()
