"""Database browser authorization, cache protection and metadata-only audit."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from cloud_agent.admin_database import (
    DatabaseQueryInvalid,
    DatabaseQueryResult,
    DatabaseQueryTimeout,
    DatabaseQueryUnavailable,
    DatabaseTables,
)
from cloud_agent.api import admin_database_browser
from cloud_agent.api.deps import verify_admin_access
from cloud_agent.app import install_admin_no_store

ADMIN = {"Authorization": "Bearer test-browser-admin"}
ROOT = "/admin/agent/database"


@pytest.fixture
async def browser_api(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-browser-admin")
    queries, audit = AsyncMock(), AsyncMock()
    queries.list_tables.return_value = DatabaseTables(tables=[])
    queries.query.return_value = DatabaseQueryResult(
        table="agent_jobs",
        columns=["id", "state"],
        rows=[["job-1", "running"]],
        limit=50,
        offset=0,
        has_more=False,
        elapsed_ms=1.2,
        text_limit=2048,
    )
    app = FastAPI()
    app.state.storage = SimpleNamespace(admin_database=queries, audit=audit)
    app.include_router(admin_database_browser.router)
    install_admin_no_store(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client, queries, audit, app


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/tables", None),
        ("POST", "/query", {"table": "agent_jobs"}),
    ],
)
async def test_every_read_is_admin_only_and_refusals_are_not_cached(
    browser_api, method, path, body
):
    client, queries, audit, _ = browser_api
    response = await client.request(method, ROOT + path, json=body)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    queries.list_tables.assert_not_awaited()
    queries.query.assert_not_awaited()
    audit.record_admin_action.assert_not_awaited()


async def test_queries_audit_identifiers_and_outcomes_without_values_or_row_data(browser_api):
    client, queries, audit, _ = browser_api
    body = {
        "table": "agent_jobs",
        "filters": [{"column": "id", "operator": "eq", "value": "PRIVATE-VALUE"}],
    }
    response = await client.post(ROOT + "/query", headers=ADMIN, json=body)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert queries.query.call_args.args[0].filters[0].value == "PRIVATE-VALUE"
    recorded = audit.record_admin_action.call_args.kwargs
    assert recorded["action"] == "agent.database.query" and recorded["actor_id"] == "admin-token"
    assert recorded["success"] is True
    assert recorded["details"]["columns"] == ["id", "state"]
    assert recorded["details"]["count"] == 1
    assert "PRIVATE-VALUE" not in str(recorded) and "job-1" not in str(recorded)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (DatabaseQueryInvalid("Filter column is not available."), 422),
        (DatabaseQueryTimeout(), 408),
        (DatabaseQueryUnavailable(), 503),
    ],
)
async def test_failed_queries_are_audited_and_not_cached(browser_api, error, status):
    client, queries, audit, _ = browser_api
    queries.query.side_effect = error
    response = await client.post(ROOT + "/query", headers=ADMIN, json={"table": "agent_jobs"})
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert audit.record_admin_action.call_args.kwargs["success"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"table": "agent_jobs", "limit": 201},
        {"table": "agent_jobs", "offset": 100001},
        {"table": "agent_jobs", "limit": "50"},
        {"table": "agent_jobs", "sql": "PRIVATE-SQL"},
        {"table": "agent_jobs", "filters": [{"column": "id", "operator": "execute PRIVATE-SQL"}]},
        {
            "table": "agent_jobs",
            "filters": [{"column": "id", "operator": "eq", "value": "PRIVATE-VALUE" * 300}],
        },
    ],
)
async def test_validation_is_bounded_and_never_echoes_filter_values(browser_api, body):
    client, queries, _, _ = browser_api
    response = await client.post(ROOT + "/query", headers=ADMIN, json=body)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert "PRIVATE" not in response.text
    queries.query.assert_not_awaited()


async def test_permission_is_rechecked_after_metadata_read(browser_api):
    client, queries, _, app = browser_api
    assert (await client.get(ROOT + "/tables", headers=ADMIN)).status_code == 200

    async def revoked():
        raise HTTPException(status_code=403, detail="Admin permission revoked")

    app.dependency_overrides[verify_admin_access] = revoked
    response = await client.post(ROOT + "/query", headers=ADMIN, json={"table": "agent_jobs"})
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    queries.query.assert_not_awaited()


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/tables", None),
        ("POST", "/query", {"table": "agent_jobs"}),
    ],
)
async def test_missing_database_is_a_non_cached_503(browser_api, method, path, body):
    client, _, _, app = browser_api
    app.state.storage = None
    response = await client.request(method, ROOT + path, headers=ADMIN, json=body)
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
