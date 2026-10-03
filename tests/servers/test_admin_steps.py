"""Admin steps: the model requests of a page of turns, read across the gateway.

A steps page is the runs list's page of turns plus each turn's requests, so
these tests hold both halves: the turns are exactly the runs list's for the
same selection, and a turn whose requests could not all be read says so
instead of reading as a turn that made fewer calls.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.servers.test_admin_llm_requests import (
    ADMIN,
    BINDING,
    SESSION_SECRET,
    gateway_page,
    source_row,
)
from tests.servers.test_owner_revalidation import _AccessGateway
from tests.unit.test_grant_usage_requests import SINCE, UNTIL, request_row

from cloud_agent import identity
from cloud_agent.admin_observability import (
    AdminRun,
    AdminRunList,
    RunCursor,
    RunFilters,
    cursor_scope,
    encode_cursor,
)
from cloud_agent.api import admin_steps
from cloud_agent.api.deps import get_gateway_client, get_grant_client
from cloud_agent.app import create_app, create_mcp_app, install_admin_no_store
from cloud_agent.gateway import GatewayError

PATH = "/admin/agent/steps"


def turn(job_id: str, *, thread_id: str = "athr_one", turn_no: int = 1, **overrides: Any):
    """One run row as the runs list returns it."""
    values: dict[str, Any] = {
        "id": job_id,
        "thread_id": thread_id,
        "title": "Never listed on a step",
        "turn_no": turn_no,
        "user_id": "other-owner",
        "repo": "example/repository",
        "runtime": "claude-code",
        "model": "model-a",
        "execution_profile": "trusted-v1",
        "state": "succeeded",
        "created_at": SINCE,
        "updated_at": UNTIL,
    }
    values.update(overrides)
    return AdminRun.model_validate(values)


class _FakeGrants:
    """The gateway client: one answer per grant, recorded."""

    def __init__(self) -> None:
        self.binding_id = BINDING
        self.usage_requests = AsyncMock(return_value=gateway_page())


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-llm-requests-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, SESSION_SECRET)
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    queries = SimpleNamespace(
        list_runs=AsyncMock(
            return_value=AdminRunList(runs=[turn("ajob_one")], since=SINCE, until=UNTIL)
        ),
        llm_request_sources=AsyncMock(return_value=([source_row()], False)),
    )
    grants = _FakeGrants()
    gateway = _AccessGateway(["agent.use", "agent.admin"])
    app = FastAPI()
    app.include_router(admin_steps.router)
    install_admin_no_store(app)
    app.dependency_overrides[admin_steps.get_admin_run_queries] = lambda: queries
    app.dependency_overrides[get_grant_client] = lambda: grants
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app, queries, grants, gateway


async def _get(app: FastAPI, path: str = PATH, **kwargs: Any):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=ADMIN, **kwargs)


@pytest.mark.parametrize("auth", ["missing", "invalid", "revoked"])
async def test_steps_require_a_current_admin(setup, auth):
    app, queries, grants, gateway = setup
    gateway.permissions = ["agent.use"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {}
        if auth == "invalid":
            headers = {"Authorization": "Bearer not-an-admin-token"}
        elif auth == "revoked":
            client.cookies.set(
                identity.SESSION_COOKIE, identity.mint_session(user_id="admin-person")
            )
        response = await client.get(PATH, headers=headers)
    assert response.status_code == (403 if auth == "revoked" else 401)
    assert response.headers["cache-control"] == "no-store"
    queries.list_runs.assert_not_awaited()
    queries.llm_request_sources.assert_not_awaited()
    grants.usage_requests.assert_not_awaited()


async def test_steps_read_the_runs_list_selection_and_merge_newest_first(setup):
    app, queries, grants, _ = setup
    queries.list_runs.return_value = AdminRunList(
        runs=[
            turn("ajob_new", thread_id="athr_new", turn_no=2),
            turn("ajob_old", thread_id="athr_old"),
        ],
        since=SINCE,
        until=UNTIL,
        next_cursor="next-turns",
    )
    queries.llm_request_sources.side_effect = lambda job_id, **_: (
        [
            source_row(
                thread_id="athr_new" if job_id == "ajob_new" else "athr_old",
                grant_id=f"agr_{job_id}",
                ledger_id=1 if job_id == "ajob_new" else 2,
            )
        ],
        False,
    )

    def answer(grant_id: str, **_: Any):
        second = SINCE + timedelta(seconds=10 if grant_id == "agr_ajob_old" else 20)
        return gateway_page(
            [
                request_row(request_id=f"{grant_id}_1", request_started_at=SINCE.isoformat()),
                request_row(request_id=f"{grant_id}_2", request_started_at=second.isoformat()),
            ]
        )

    grants.usage_requests.side_effect = answer
    response = await _get(
        app, PATH + "?since=2026-09-11T03:20:00Z&until=2026-09-11T03:21:00Z&model=model-a"
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    # The turns are the runs list's own: same filters, same window, a page of five.
    kwargs = queries.list_runs.await_args.kwargs
    assert kwargs["filters"] == RunFilters(model="model-a")
    assert (kwargs["since"], kwargs["until"], kwargs["limit"]) == (SINCE, UNTIL, 5)
    assert body["next_cursor"] == "next-turns"
    assert [item["job_id"] for item in body["turns"]] == ["ajob_new", "ajob_old"]
    assert {item["coverage"] for item in body["turns"]} == {"complete"}
    # Newest request first across both turns, each carrying the turn it belongs to.
    # Requests that started at the same instant fall back to the request ID.
    assert [(step["request_id"], step["job_id"]) for step in body["steps"]] == [
        ("agr_ajob_new_2", "ajob_new"),
        ("agr_ajob_old_2", "ajob_old"),
        ("agr_ajob_old_1", "ajob_old"),
        ("agr_ajob_new_1", "ajob_new"),
    ]
    # A turn's title is session content the steps list has no use for.
    assert "Never listed on a step" not in response.text
    for call in grants.usage_requests.await_args_list:
        assert call.kwargs["limit"] == admin_steps.STEP_REQUESTS_PER_RUN
        assert call.kwargs["external_job_id"].startswith("thread:athr_")


async def test_a_turn_says_how_completely_its_requests_were_read(setup):
    app, queries, grants, _ = setup
    queries.list_runs.return_value = AdminRunList(
        runs=[
            turn("ajob_full"),
            turn("ajob_more"),
            turn("ajob_foreign"),
            turn("ajob_down"),
            turn("ajob_queued"),
            turn("ajob_restarted"),
        ],
        since=SINCE,
        until=UNTIL,
    )
    sources = {
        "ajob_full": ([source_row(grant_id="agr_full")], False),
        "ajob_more": ([source_row(grant_id="agr_more")], False),
        "ajob_foreign": ([source_row(binding_id="gw_other")], False),
        "ajob_down": ([source_row(grant_id="agr_down")], False),
        "ajob_queued": ([], False),
        # The first run could not start: it has no reference and made no requests.
        "ajob_restarted": (
            [
                source_row(
                    attempt_id=9,
                    attempt_status="aborted",
                    ledger_id=None,
                    grant_id=None,
                    binding_id=None,
                    window_since=None,
                    window_until=None,
                ),
                source_row(attempt_id=10, attempt_no=2, grant_id="agr_restarted"),
            ],
            False,
        ),
    }
    queries.llm_request_sources.side_effect = lambda job_id, **_: sources[job_id]

    def answer(grant_id: str, **_: Any):
        if grant_id == "agr_down":
            raise GatewayError(
                "gateway said something with request data",
                status_code=503,
                error_type="service_unavailable",
            )
        page = gateway_page([request_row(request_id=f"req_{grant_id}")])
        if grant_id == "agr_more":
            return page.model_copy(update={"next_cursor": "more"})
        return page

    grants.usage_requests.side_effect = answer
    response = await _get(app)
    assert response.status_code == 200
    body = response.json()
    coverage = {item["job_id"]: (item["coverage"], item["reason"]) for item in body["turns"]}
    assert coverage == {
        "ajob_full": ("complete", None),
        "ajob_more": ("partial", None),
        "ajob_foreign": ("unavailable", "binding_changed"),
        "ajob_down": ("unavailable", "gateway_unavailable"),
        "ajob_queued": ("not_started", None),
        "ajob_restarted": ("complete", None),
    }
    assert "request data" not in response.text
    assert {step["request_id"] for step in body["steps"]} == {
        "req_agr_full",
        "req_agr_more",
        "req_agr_restarted",
    }
    restarted = next(step for step in body["steps"] if step["job_id"] == "ajob_restarted")
    assert restarted["attempt_no"] == 2


async def test_a_turn_restarted_more_often_than_a_page_reads_is_partial(setup):
    app, queries, _, _ = setup
    queries.llm_request_sources.return_value = ([source_row()], True)
    body = (await _get(app)).json()
    assert body["turns"][0]["coverage"] == "partial"
    kwargs = queries.llm_request_sources.await_args.kwargs
    assert kwargs["limit"] == admin_steps.STEP_SOURCES_PER_TURN


async def test_steps_continue_the_runs_list_cursor(setup):
    app, queries, _, _ = setup
    filters = RunFilters(state="failed")
    token = encode_cursor(
        RunCursor(
            scope=cursor_scope(filters.model_dump()),
            since=SINCE,
            until=UNTIL,
            created_at=SINCE,
            job_id="ajob_one",
        )
    )
    response = await _get(app, PATH, params={"state": "failed", "cursor": token, "limit": 10})
    assert response.status_code == 200
    kwargs = queries.list_runs.await_args.kwargs
    assert kwargs["cursor"].job_id == "ajob_one"
    assert (kwargs["since"], kwargs["until"], kwargs["limit"]) == (SINCE, UNTIL, 10)


@pytest.mark.parametrize(
    "query",
    [
        "?since=2026-09-11T03:21:00Z&until=2026-09-11T03:20:00Z",
        "?cursor=not-a-cursor",
        "?limit=11",
    ],
)
async def test_an_unusable_selection_is_refused_before_any_read(setup, query):
    app, queries, grants, _ = setup
    response = await _get(app, PATH + query)
    assert response.status_code == 422
    queries.list_runs.assert_not_awaited()
    grants.usage_requests.assert_not_awaited()


async def test_a_page_too_large_to_carry_is_refused_whole(setup, monkeypatch):
    app, _, _, _ = setup
    monkeypatch.setattr(admin_steps, "MAX_ADMIN_RESPONSE_BYTES", 256)
    response = await _get(app)
    assert response.status_code == 502
    assert response.json()["detail"]["error"]["type"] == "steps_page_too_large"


def test_steps_are_only_on_the_main_listener():
    """Never expose cross-user request detail on the sandbox MCP listener."""
    assert PATH in create_app().openapi()["paths"]
    assert PATH not in create_mcp_app().openapi()["paths"]
