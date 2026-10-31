"""The File Activity routes: ownership, cursors, filters and cache policy.

The store is a fake here; what these tests settle is the wire contract a
browser and a cursor depend on, and that a conversation that is not the
caller's is a 404 rather than an empty page.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import file_activity as file_activity_api
from cloud_agent.api.deps import get_storage
from cloud_agent.file_activity.models import (
    FileActivityCoverage,
    FileActivitySemantics,
    FileActivityTotals,
    FileActivityView,
    FileOperationsPage,
)

USER = {"user_id": "owner-a", "email": "a@example.com"}


def _view(**overrides: Any) -> FileActivityView:
    defaults: dict[str, Any] = {
        "thread_id": "t1",
        "generated_at": datetime(2026, 10, 31, 9, tzinfo=UTC),
        "sort": "active",
        "filter": "all",
        "q": "",
        "totals": FileActivityTotals(
            files=1,
            operations=1,
            reads=1,
            writes=0,
            searches=0,
            images=0,
            added=0,
            removed=0,
            added_unknown_files=0,
            removed_unknown_files=0,
            failed=0,
            pending=0,
            not_recorded=0,
            unknown_path_operations=0,
            search_unknown_scope=0,
            subagent_operations=0,
        ),
        "filter_counts": {"all": 1, "read": 1, "written": 0, "searched": 0, "images": 0},
        "coverage": FileActivityCoverage(turns=1, attempts=1, events_scanned=2),
        "semantics": FileActivitySemantics(),
        "files": [],
    }
    defaults.update(overrides)
    return FileActivityView(**defaults)


class _Queries:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.missing = False

    async def file_activity(self, **kwargs: Any) -> FileActivityView | None:
        self.calls.append(kwargs)
        return None if self.missing else _view(sort=kwargs["sort"], filter=kwargs["filter_key"])

    async def file_operations(self, **kwargs: Any) -> FileOperationsPage | None:
        self.calls.append(kwargs)
        if self.missing:
            return None
        return FileOperationsPage(thread_id="t1", path=kwargs["path"], total=0)


class _Store:
    def __init__(self, queries: _Queries) -> None:
        self.file_activity_queries = queries


def _client(queries: _Queries) -> AsyncClient:
    app = FastAPI()
    app.include_router(file_activity_api.router)
    app.dependency_overrides[get_storage] = lambda: _Store(queries)
    app.dependency_overrides[file_activity_api.require_agent_owner] = lambda: USER
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_a_page_echoes_its_filters_and_is_never_cached():
    queries = _Queries()
    async with _client(queries) as http:
        response = await http.get(
            "/v1/agent/jobs/job-1/thread/files",
            params={"sort": "lines", "filter": "written", "q": " src ", "limit": 10},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["filter_counts"]["written"] == 0
    assert queries.calls[0]["q"] == "src"
    assert queries.calls[0]["sort"] == "lines"
    assert queries.calls[0]["limit"] == 10


@pytest.mark.asyncio
async def test_a_conversation_that_is_not_the_callers_is_404():
    queries = _Queries()
    queries.missing = True
    async with _client(queries) as http:
        response = await http.get("/v1/agent/jobs/job-1/thread/files")
        operations = await http.get(
            "/v1/agent/jobs/job-1/thread/files/operations", params={"path": "/w/a.py"}
        )
    assert response.status_code == 404
    assert operations.status_code == 404


@pytest.mark.asyncio
async def test_a_cursor_from_another_list_is_422():
    class Refusing(_Queries):
        async def file_activity(self, **kwargs: Any) -> FileActivityView | None:
            raise ValueError("Invalid pagination cursor for these filters.")

    async with _client(Refusing()) as http:
        response = await http.get("/v1/agent/jobs/job-1/thread/files", params={"cursor": "stale"})
    assert response.status_code == 422
    assert "cursor" in response.json()["detail"]["error"]["message"]


@pytest.mark.asyncio
async def test_an_unknown_sort_filter_or_oversized_search_is_refused():
    async with _client(_Queries()) as http:
        bad_sort = await http.get("/v1/agent/jobs/job-1/thread/files", params={"sort": "fastest"})
        bad_filter = await http.get(
            "/v1/agent/jobs/job-1/thread/files", params={"filter": "deleted"}
        )
        long_search = await http.get("/v1/agent/jobs/job-1/thread/files", params={"q": "x" * 500})
        bad_limit = await http.get("/v1/agent/jobs/job-1/thread/files", params={"limit": 0})
    assert bad_sort.status_code == 422
    assert bad_filter.status_code == 422
    assert long_search.status_code == 422
    assert bad_limit.status_code == 422


@pytest.mark.asyncio
async def test_the_operations_route_passes_the_path_as_data():
    queries = _Queries()
    async with _client(queries) as http:
        response = await http.get(
            "/v1/agent/jobs/job-1/thread/files/operations",
            params={"path": "/w/a.py", "limit": 5},
        )
    assert response.status_code == 200
    assert queries.calls[0]["path"] == "/w/a.py"
    assert queries.calls[0]["limit"] == 5
