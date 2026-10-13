"""Admin usage aggregation: authorization, bounds and content-free responses."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI  # noqa: TC002 — used in a runtime annotation.
from httpx import ASGITransport, AsyncClient
from tests.unit.test_ledger_snapshots import START, ledger_row, report_payload

from cloud_agent import identity
from cloud_agent.admin_usage import MAX_USAGE_RESPONSE_BYTES, MAX_USAGE_RUNS
from cloud_agent.api import admin_usage
from cloud_agent.api.deps import get_storage
from cloud_agent.app import create_app, create_mcp_app
from cloud_agent.storage.admin_usage_queries import (
    UsageQueryTooLarge,
    UsageQueryUnavailable,
)

ADMIN = {"Authorization": "Bearer admin-usage-test"}
PATH = "/admin/agent/usage"
PERSONAL = "private prompt"


def row(
    *,
    job_id: str = "ajob_one",
    user_id: str = "owner-one",
    model: str = "model-a",
    state: str = "succeeded",
    created_at: datetime = START,
    ledger: list[dict] | None = None,
) -> dict:
    """One projected job row, including fields the response must never echo."""
    return {
        "id": job_id,
        "user_id": user_id,
        "model": model,
        "runtime": "claude-code",
        "repo": "example/repository",
        "state": state,
        "created_at": created_at,
        "ledger": [ledger_row()] if ledger is None else ledger,
        "task_prompt": PERSONAL,
        "metadata": {"token": "private credential"},
    }


def ledger_row_for(job_id: str, *, amount: str = "0.00012345") -> dict:
    return ledger_row(
        job_id=job_id,
        snapshot=report_payload(totals={**report_payload()["totals"], "spent_usd": amount}),
    )


@pytest.fixture
def queries():
    """A stub query layer that records exactly what the route asked for."""
    stub = SimpleNamespace(
        # One call, one read view: the cohort and its ledger rows come back
        # together so the route cannot count a run and sum a different snapshot.
        selection=AsyncMock(return_value=([row()], [ledger_row_for("ajob_one")])),
    )
    return stub


@pytest.fixture
def setup(monkeypatch, queries):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-usage-test")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-usage-session-test-long-secret")
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    app = create_app()
    app.dependency_overrides[admin_usage.get_admin_usage_queries] = lambda: queries
    app.dependency_overrides[get_storage] = lambda: None
    return app, queries


async def get(app: FastAPI, path: str = PATH, headers: dict | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=ADMIN if headers is None else headers)


async def test_an_administrator_reads_totals_days_and_both_breakdowns(setup):
    app, _ = setup
    response = await get(app, f"{PATH}?since=2026-09-09T00:00:00Z&until=2026-09-11T00:00:00Z")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["source"] == "gateway_api_logs"
    assert body["attribution"] == "grant_time_window"
    assert body["finalized"] is False
    assert body["selection"]["time_basis"] == "job_created_at"
    assert body["selection"]["day_timezone"] == "UTC"
    assert body["selection"]["model_basis"] == "job_configured_model"
    assert body["summary"]["run_count"] == 1
    assert body["summary"]["totals"]["spent_usd"] == "0.00012345"
    assert body["summary"]["runs_with_snapshot"] == 1
    assert [day["date"] for day in body["days"]] == ["2026-09-09", "2026-09-10"]
    assert body["by_user"]["groups"][0]["key"] == "owner-one"
    assert body["by_model"]["groups"][0]["key"] == "model-a"
    assert response.headers["cache-control"] == "no-store"


async def test_defaults_are_the_last_seven_days_and_filters_pass_through(setup):
    app, queries = setup
    await get(app, f"{PATH}?user_id=owner-two&model=model-b&runtime=codex&repo=org/repo")
    kwargs = queries.selection.await_args.kwargs
    assert kwargs["user_id"] == "owner-two"
    assert kwargs["model"] == "model-b"
    assert kwargs["runtime"] == "codex"
    assert kwargs["repo"] == "org/repo"
    assert kwargs["max_runs"] == MAX_USAGE_RUNS
    assert kwargs["until"] - kwargs["since"] == timedelta(days=7)
    assert kwargs["since"].tzinfo is not None


async def test_an_equivalent_offset_selects_the_same_instant(setup):
    app, queries = setup
    await get(app, f"{PATH}?since=2026-09-09T09:00:00%2B09:00&until=2026-09-11T09:00:00%2B09:00")
    kwargs = queries.selection.await_args.kwargs
    assert kwargs["since"] == datetime(2026, 9, 9, tzinfo=UTC)
    assert kwargs["until"] == datetime(2026, 9, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        "since=2026-09-11T00:00:00Z&until=2026-09-09T00:00:00Z",
        "since=2026-09-09T00:00:00Z&until=2026-09-09T00:00:00Z",
        "since=2026-01-01T00:00:00Z&until=2026-09-09T00:00:00Z",
        "user_id=",
        "model=",
        "runtime=",
        "repo=",
    ],
)
async def test_an_unusable_filter_is_rejected_before_any_query(setup, query):
    app, queries = setup
    response = await get(app, f"{PATH}?{query}")
    assert response.status_code == 422
    queries.selection.assert_not_awaited()


async def test_a_naive_or_unknown_bound_is_rejected_before_any_query(setup):
    app, queries = setup
    for query in (
        "since=2026-09-09T00:00:00",
        "until=2026-09-11T00:00:00",
        "since=yesterday",
        "host=runner-one",
        "group_by=model",
    ):
        response = await get(app, f"{PATH}?{query}")
        assert response.status_code == 422, query
    queries.selection.assert_not_awaited()


async def test_too_many_runs_refuses_the_whole_selection_without_totals(setup):
    app, queries = setup
    # The store refuses from inside its own read view, before it projects a
    # single snapshot, so the route can only answer with that refusal.
    queries.selection.side_effect = UsageQueryTooLarge
    response = await get(app, PATH)
    assert response.status_code == 422
    detail = response.json()["detail"]["error"]
    assert detail["type"] == "usage_query_too_large"
    assert detail["code"] == 422
    assert "totals" not in response.json()
    assert MAX_USAGE_RUNS == 10_000


async def test_a_response_over_its_byte_budget_is_refused_whole(setup, monkeypatch):
    """The bound is checked on the bytes that would actually leave.

    Wide counters are protocol-legal, cheap to store and bounded by nothing in
    the row or snapshot budgets, so the guard has to look at the serialized
    response. The bound is lowered here because reaching 1 MiB needs counters
    no real gateway report carries; the message that would be returned is the
    same one a real oversized selection gets.
    """
    app, _ = setup
    monkeypatch.setattr(admin_usage, "MAX_USAGE_RESPONSE_BYTES", 2_000)
    response = await get(app, PATH)
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "usage_query_too_large"
    assert "totals" not in response.json()
    assert MAX_USAGE_RESPONSE_BYTES == 1024 * 1024


async def test_the_reduction_runs_without_holding_the_event_loop(setup, monkeypatch):
    """Host heartbeats, claims and streams share this loop with a wide selection."""
    app, _ = setup
    reducing, served = threading.Event(), threading.Event()
    answered: list[bool] = []
    original = admin_usage.prepare_jobs

    def prepare_jobs(*args, **kwargs):
        reducing.set()
        # Only the event loop can answer; reducing on it would wait in vain.
        answered.append(served.wait(timeout=5))
        return original(*args, **kwargs)

    async def serve() -> None:
        while not reducing.is_set():
            await asyncio.sleep(0.001)
        served.set()

    monkeypatch.setattr(admin_usage, "prepare_jobs", prepare_jobs)
    server = asyncio.create_task(serve())
    response = await get(app, PATH)
    await asyncio.wait_for(server, timeout=5)
    assert answered == [True]
    assert response.status_code == 200
    assert response.json()["summary"]["totals"]["spent_usd"] == "0.00012345"


async def test_an_unreadable_selection_returns_503_not_a_partial_total(setup):
    app, queries = setup
    queries.selection.side_effect = UsageQueryUnavailable
    response = await get(app, PATH)
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "usage_temporarily_unavailable"


async def test_an_empty_selection_reports_zero_runs_and_no_totals(setup):
    app, queries = setup
    queries.selection.return_value = ([], [])
    response = await get(app, PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["run_count"] == 0
    assert body["summary"]["totals"] is None
    assert body["summary"]["unknown_rows"] is None
    assert body["by_user"] == {"group_count": 0, "groups": [], "other": None}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-an-admin"}])
async def test_a_non_administrator_is_refused_without_running_the_query(setup, headers):
    app, queries = setup
    response = await get(app, PATH, headers=headers)
    assert response.status_code in (401, 403)
    queries.selection.assert_not_awaited()


async def test_responses_never_carry_content_or_identity_canaries(setup):
    app, _ = setup
    response = await get(app, PATH)
    body = response.text
    assert PERSONAL not in body
    assert "private credential" not in body
    for private in ("agr_actual", "gw_one", "usage_id", "binding"):
        assert private not in body


async def test_a_revoked_administrator_is_refused(setup, monkeypatch):
    app, queries = setup
    monkeypatch.setenv("ADMIN_TOKEN", "rotated-token")
    response = await get(app)
    assert response.status_code in (401, 403)
    queries.selection.assert_not_awaited()


def test_the_mcp_listener_does_not_mount_the_admin_usage_route():
    assert not any(
        getattr(getattr(route, "original_router", None), "prefix", "") == "/admin/agent/usage"
        for route in create_mcp_app().routes
    )
    assert any(
        getattr(getattr(route, "original_router", None), "prefix", "") == "/admin/agent/usage"
        for route in create_app().routes
    )


async def test_jobs_are_paired_with_their_own_ledger_rows(setup):
    """A job's snapshot must never be attributed to a neighbouring run."""
    app, queries = setup
    queries.selection.return_value = (
        [
            row(job_id="ajob_one", user_id="owner-one", model="model-a"),
            row(job_id="ajob_two", user_id="owner-two", model="model-b"),
        ],
        [
            ledger_row_for("ajob_two", amount="2.00"),
            ledger_row_for("ajob_one", amount="1.00"),
        ],
    )
    response = await get(app, PATH)
    body = response.json()
    groups = {group["key"]: group for group in body["by_user"]["groups"]}
    assert groups["owner-one"]["usage"]["totals"]["spent_usd"] == "1.00"
    assert groups["owner-two"]["usage"]["totals"]["spent_usd"] == "2.00"
    assert body["summary"]["totals"]["spent_usd"] == "3.00"


async def test_days_cover_the_selection_and_empty_days_stay_visible(setup):
    app, queries = setup
    queries.selection.return_value = (
        [row(created_at=datetime(2026, 9, 8, 12, tzinfo=UTC))],
        [ledger_row_for("ajob_one")],
    )
    response = await get(app, f"{PATH}?since=2026-09-08T06:00:00Z&until=2026-09-10T06:00:00Z")
    body = response.json()
    assert [day["date"] for day in body["days"]] == ["2026-09-08", "2026-09-09", "2026-09-10"]
    assert body["days"][0]["since"] == "2026-09-08T06:00:00Z"
    assert body["days"][-1]["until"] == "2026-09-10T06:00:00Z"
    assert [day["usage"]["run_count"] for day in body["days"]] == [1, 0, 0]
    assert body["days"][1]["usage"]["totals"] is None


async def test_unstarted_terminal_runs_count_as_unavailable_not_as_zero(setup):
    app, queries = setup
    queries.selection.return_value = ([row(state="cancelled", ledger=[])], [])
    response = await get(app, PATH)
    body = response.json()
    assert body["summary"]["run_count"] == 1
    assert body["summary"]["runs_with_snapshot"] == 0
    assert body["summary"]["status_counts"]["unavailable"] == 1
    assert body["summary"]["totals"] is None


async def test_the_route_keeps_its_own_path_without_a_redirect(setup):
    app, _ = setup
    response = await get(app, PATH, headers=ADMIN)
    assert response.status_code == 200
    assert response.history == []


def test_the_route_is_registered_once_for_the_control_plane(setup):
    _, queries = setup
    assert admin_usage.router.prefix == "/admin/agent/usage"
    assert queries.selection.await_count == 0
