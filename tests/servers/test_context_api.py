"""The Context view and Context insights: the owner's own, never cached."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


@pytest.mark.parametrize(
    "path", ["jobs/j1/thread/context/requests/5", "jobs/j1/thread/context/content/5"]
)
async def test_request_reads_need_an_owner_and_answer_404_for_anything_not_theirs(app, path):
    app.state.queries.request_composition.return_value = None
    app.state.queries.event_content.return_value = None
    assert (await _get(app, f"/v1/agent/{path}", owner=None)).status_code == 401
    response = await _get(app, f"/v1/agent/{path}")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path", ["jobs/j1/thread/context/requests/0", "jobs/j1/thread/context/content/x"]
)
async def test_request_reads_refuse_a_bad_event_id(app, path):
    assert (await _get(app, f"/v1/agent/{path}")).status_code == 422


async def test_content_is_read_in_windows_for_its_owner(app):
    app.state.queries.event_content.return_value = {
        "id": 5,
        "event_type": "tool_result",
        "phase": None,
        "total_chars": 50_000,
        "text": "x" * 20_000,
        "truncated": False,
    }
    response = await _get(app, "/v1/agent/jobs/j1/thread/context/content/5", offset=20_000)
    assert response.status_code == 200
    call = app.state.queries.event_content.await_args.kwargs
    assert call == {
        "job_id": "j1",
        "user_id": "owner-1",
        "event_id": 5,
        "offset": 20_000,
        "limit": 20_000,
    }
    assert response.json()["next_offset"] == 40_000
    assert response.headers["cache-control"] == "no-store"


async def test_filters_reach_the_query_and_bind_the_cursor(app):
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    app.state.queries.insights_page.return_value = {
        **app.state.queries.insights_page.return_value,
        "activity": [
            {
                "day": datetime(2026, 10, 14, tzinfo=UTC).date(),
                "turns": 3,
                "conversations": 2,
                "main_requests": 9,
                "compactions": 1,
                "peak_prompt_tokens": 1000,
            }
        ],
        "projects": [
            {
                "repo": None,
                "conversations": 1,
                "turns": 1,
                "main_requests": 2,
                "compactions": 0,
                "peak_prompt_tokens": None,
                "last_turn_at": NOW,
            }
        ],
        "composition": [
            {
                "thread_id": "athr_1",
                "turns": 1,
                "turns_profiled": 1,
                "turns_truncated": 0,
                "prompt_tokens": 12,
                "tool_results": 300,
                "main_requests": 2,
                "main_prompt_tokens": 2_000,
            },
            {"thread_id": "athr_9", "turns": 4, "turns_profiled": 4, "prompt_tokens": 8},
        ],
    }
    response = await _get(
        app,
        "/v1/agent/usage/context",
        range="7d",
        q="  Login ",
        temporary="true",
        day=today.date().isoformat(),
        limit=1,
    )
    assert response.status_code == 200
    filters = app.state.queries.insights_page.await_args.kwargs["filters"]
    assert (filters.q, filters.repo, filters.temporary) == ("Login", None, True)
    assert filters.day_since == today
    assert filters.day_until == today + timedelta(days=1)
    body = response.json()
    assert body["filters"] == {
        "q": "Login",
        "repo": None,
        "temporary": True,
        "day": today.date().isoformat(),
    }
    assert body["activity"][0]["day"] == "2026-10-14"
    assert body["projects"][0]["repo"] is None
    # The chart sums every matching conversation; each row carries its own.
    assert body["composition"]["recorded"]["total_tokens"] == 12 + 300 + 8
    assert body["composition"]["provider"]["prompt_tokens"] == 2_000
    first = body["conversations"][0]["composition"]
    assert first["recorded"]["total_tokens"] == 312
    # The cursor belongs to this search, project and day, and no other.
    cursor = body["next_cursor"]
    assert cursor
    again = await _get(
        app,
        "/v1/agent/usage/context",
        range="7d",
        q="Login",
        temporary="true",
        day=today.date().isoformat(),
        limit=1,
        cursor=cursor,
    )
    assert again.status_code == 200, again.text
    assert again.json()["activity"] is None and again.json()["composition"] is None
    other = await _get(app, "/v1/agent/usage/context", range="7d", q="Login", cursor=cursor)
    assert other.status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"repo": "acme/web", "temporary": "true"},
        {"repo": " "},
        {"day": "2020-01-01"},
        {"day": "yesterday"},
        {"q": "x" * 201},
    ],
)
async def test_a_bad_filter_is_refused_before_the_list_is_read(app, params):
    response = await _get(app, "/v1/agent/usage/context", range="7d", **params)
    assert response.status_code == 422
    app.state.queries.insights_page.assert_not_awaited()
