"""Admin authorization, cursor scoping and conversation content boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.admin_conversations import ConversationCursor, ConversationTurnsCursor
from cloud_agent.admin_observability import RunFilters, cursor_scope, encode_cursor
from cloud_agent.api import admin_conversations
from cloud_agent.app import create_app, create_mcp_app
from cloud_agent.storage.admin_conversation_queries import turns_scope
from tests.servers.test_admin_runs import ADMIN, _admin_run_row, setup as run_setup  # noqa: F401
from tests.unit.test_ledger_snapshots import END, START

PREFIX = "/admin/agent/conversations"


def conversation():
    """Safe conversation metadata plus canaries a response must exclude."""
    return {
        "thread_id": "athr_other",
        "title": "Example conversation",
        "user_id": "other-owner",
        "repo": "example/repository",
        "created_at": START,
        "last_activity_at": END,
        "state": "idle",
        "turn_count": 2,
        "latest_job_id": "ajob_other",
        "latest_turn_no": 2,
        "latest_turn_state": "succeeded",
        "latest_runtime": "claude-code",
        "latest_model": "model",
        "last_matching_turn_at": START,
        "task_prompt": "PRIVATE prompt",
        "metadata": {"token": "PRIVATE credential"},
    }


@pytest.fixture
def setup(run_setup):  # noqa: F811 — imported pytest fixture
    """Use real admin authentication and a purpose-built query fake."""
    app, _, _, gateway = run_setup
    queries = SimpleNamespace(
        list_conversations=AsyncMock(
            return_value={
                "conversations": [conversation()],
                "since": START,
                "until": END,
            }
        ),
        detail=AsyncMock(
            return_value={
                "conversation": conversation(),
                "turns": [{**_admin_run_row(), "matches_filters": True}],
            }
        ),
    )
    app.include_router(admin_conversations.router)
    app.dependency_overrides[admin_conversations.get_admin_conversation_queries] = lambda: queries
    return app, queries, gateway


@pytest.mark.parametrize("path", [PREFIX, PREFIX + "/athr_other"])
@pytest.mark.parametrize("auth", ["missing", "invalid", "revoked"])
async def test_conversation_reads_require_current_admin_access(setup, path, auth):
    app, queries, gateway = setup
    gateway.permissions = ["agent.use"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer wrong"} if auth == "invalid" else {}
        if auth == "revoked":
            client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id="admin"))
        response = await client.get(path, headers=headers)
    assert response.status_code == (403 if auth == "revoked" else 401)
    queries.list_conversations.assert_not_awaited()
    queries.detail.assert_not_awaited()


@pytest.mark.parametrize("path", [PREFIX, PREFIX + "/athr_other"])
async def test_metadata_reads_exclude_content_and_are_not_cached(setup, path):
    app, _, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path, headers=ADMIN)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "other-owner" in response.text
    assert "private" not in response.text.lower()
    assert "task_prompt" not in response.text
    assert "metadata" not in response.text


async def test_list_cursor_keeps_window_and_rejects_a_different_cohort(setup):
    app, queries, _ = setup
    filters = RunFilters(model="model-a")
    cursor = encode_cursor(
        ConversationCursor(
            scope=cursor_scope(["conversations", filters.model_dump()]),
            since=START,
            until=END,
            created_at=START,
            thread_id="athr_other",
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            PREFIX, headers=ADMIN, params={"model": "model-a", "cursor": cursor}
        )
        invalid = await client.get(
            PREFIX, headers=ADMIN, params={"model": "model-b", "cursor": cursor}
        )
    assert response.status_code == 200
    assert invalid.status_code == 422
    assert queries.list_conversations.await_count == 1
    args = queries.list_conversations.call_args.kwargs
    assert (args["since"], args["until"]) == (START, END)
    assert args["filters"] == filters


@pytest.mark.parametrize(
    "path,params",
    [
        (PREFIX, {"limit": 201}),
        (PREFIX, {"cursor": "bad"}),
        (PREFIX, {"model": "x" * 257}),
        (PREFIX, {"since": END.isoformat(), "until": START.isoformat()}),
        (PREFIX + "/athr_other", {"since": START.isoformat()}),
        (PREFIX + "/athr_other", {"limit": 0}),
    ],
)
async def test_bad_windows_filters_and_cursors_fail_before_queries(setup, path, params):
    app, queries, _ = setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path, headers=ADMIN, params=params)
    assert response.status_code == 422
    queries.list_conversations.assert_not_awaited()
    queries.detail.assert_not_awaited()


async def test_turn_pagination_cannot_switch_conversations_or_filter_context(setup):
    app, queries, _ = setup
    cursor = encode_cursor(
        ConversationTurnsCursor(
            scope=turns_scope("athr_other", RunFilters(), None, None),
            before_turn_no=2,
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        good = await client.get(PREFIX + "/athr_other", headers=ADMIN, params={"cursor": cursor})
        other = await client.get(PREFIX + "/athr_else", headers=ADMIN, params={"cursor": cursor})
        changed = await client.get(
            PREFIX + "/athr_other", headers=ADMIN, params={"cursor": cursor, "model": "different"}
        )
    assert good.status_code == 200
    assert other.status_code == changed.status_code == 422
    assert queries.detail.await_count == 1
    assert queries.detail.call_args.kwargs["since"] is None


async def test_missing_conversation_is_404(setup):
    app, queries, _ = setup
    queries.detail.return_value = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PREFIX + "/missing", headers=ADMIN)
    assert response.status_code == 404


def test_conversations_are_only_on_the_main_listener():
    """Never expose cross-user navigation on the sandbox MCP listener."""
    assert PREFIX in create_app().openapi()["paths"]
    assert PREFIX not in create_mcp_app().openapi()["paths"]
