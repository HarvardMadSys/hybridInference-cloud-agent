"""The Usage page reads only its owner's containers and runs, and is never cached."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.admin_observability import encode_cursor
from cloud_agent.api import owner_usage
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.owner_usage import (
    CONTAINER_CURSOR_SCOPE,
    OwnerContainerCursor,
    OwnerContainerList,
    OwnerCurrentUse,
    OwnerResourceHistory,
    OwnerResourceTotals,
)
from cloud_agent.storage.admin_usage_queries import UsageQueryTooLarge, UsageQueryUnavailable

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
ZERO = OwnerResourceTotals(
    running_seconds=0, measured_seconds=0, cpu_core_seconds=0, memory_byte_seconds=0
)


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "usage-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(owner_usage.router)
    queries = AsyncMock()
    queries.containers.return_value = OwnerContainerList(
        observed_at=NOW,
        counts={"running": 0},
        total_count=0,
        current=OwnerCurrentUse(cpu_cores=0, memory_bytes=0, measured=0, unmeasured=0),
        containers=[],
    )
    queries.resource_history.return_value = OwnerResourceHistory(
        since=NOW, until=NOW, generated_at=NOW, totals=ZERO, days=[]
    )
    usage = AsyncMock()
    usage.selection.return_value = ([], [])
    gateway = AsyncMock()
    gateway.agent_access.side_effect = lambda owner: {
        "user_id": owner,
        "allowed": True,
        "permissions": ["agent.use"],
    }
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        owner_usage=queries, admin_usage=usage
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


@pytest.mark.parametrize("path", ["containers", "resources", "tokens"])
async def test_every_read_needs_a_signed_in_owner_and_is_never_cached(app, path):
    anonymous = await _get(app, f"/v1/agent/usage/{path}", owner=None)
    read = await _get(app, f"/v1/agent/usage/{path}")
    assert anonymous.status_code == 401
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"


async def test_containers_are_the_owners_and_continue_from_a_cursor(app):
    first = await _get(app, "/v1/agent/usage/containers")
    assert first.status_code == 200
    app.state.queries.containers.assert_awaited_once_with("owner-1", cursor=None, limit=50)
    cursor = OwnerContainerCursor(
        scope=CONTAINER_CURSOR_SCOPE, rank=1, last_activity_at=NOW, id="asbx_last"
    )
    later = await _get(app, "/v1/agent/usage/containers", cursor=encode_cursor(cursor), limit=10)
    assert later.status_code == 200
    assert app.state.queries.containers.await_args.args == ("owner-1",)
    assert app.state.queries.containers.await_args.kwargs == {"cursor": cursor, "limit": 10}


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "not-a-cursor"},
        {
            "cursor": encode_cursor(
                OwnerContainerCursor(scope="another-list", rank=0, last_activity_at=NOW, id="x")
            )
        },
        {"limit": 0},
        {"limit": 101},
    ],
)
async def test_a_bad_container_page_request_is_refused_before_any_read(app, params):
    response = await _get(app, "/v1/agent/usage/containers", **params)
    assert response.status_code == 422
    app.state.queries.containers.assert_not_awaited()


async def test_resources_read_one_bucket_per_utc_day(app):
    response = await _get(app, "/v1/agent/usage/resources", days=30)
    assert response.status_code == 200
    call = app.state.queries.resource_history.await_args
    assert call.args == ("owner-1",)
    buckets = call.kwargs["buckets"]
    assert len(buckets) == 30
    assert buckets[0][0].hour == 0 and buckets[0][0].tzinfo is not None
    assert all(earlier[1] == later[0] for earlier, later in pairwise(buckets))


@pytest.mark.parametrize("path", ["resources", "tokens"])
@pytest.mark.parametrize("days", [0, 91])
async def test_history_periods_are_bounded(app, path, days):
    assert (await _get(app, f"/v1/agent/usage/{path}", days=days)).status_code == 422


async def test_tokens_select_only_the_owners_runs(app):
    response = await _get(app, "/v1/agent/usage/tokens", days=7)
    assert response.status_code == 200
    call = app.state.usage.selection.await_args.kwargs
    assert call["user_id"] == "owner-1"
    assert call["model"] is None and call["runtime"] is None and call["repo"] is None
    body = response.json()
    assert body["summary"]["run_count"] == 0
    assert len(body["days"]) == 7
    assert body["by_model"]["groups"] == []
    assert body["day_timezone"] == "UTC" and body["time_basis"] == "job_created_at"
    assert "by_user" not in body


@pytest.mark.parametrize(
    ("failure", "status"), [(UsageQueryTooLarge(), 422), (UsageQueryUnavailable(), 503)]
)
async def test_tokens_refuse_rather_than_return_a_partial_sum(app, failure, status):
    app.state.usage.selection.side_effect = failure
    response = await _get(app, "/v1/agent/usage/tokens")
    assert response.status_code == status
    assert "summary" not in response.text
