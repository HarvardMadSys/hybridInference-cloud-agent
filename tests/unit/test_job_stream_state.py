"""The SSE page and state share one owner-scoped database statement."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_agent.domain.execution import JobStreamState
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries


async def test_page_and_state_use_one_owner_scoped_query():
    connection = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[
                {
                    "state": "running",
                    "published_pr_url": None,
                    "id": 1,
                    "job_id": "ajob_1",
                    "attempt_id": 7,
                    "seq": 1,
                    "event_type": "message",
                    "payload": '{"text":"hello"}',
                    "created_at": None,
                }
            ]
        )
    )

    @asynccontextmanager
    async def acquire():
        yield connection

    repository = PostgresExecutionQueries(SimpleNamespace(acquire=acquire))
    page = await repository.job_stream_page(job_id="ajob_1", user_id="owner")
    assert page is not None
    events, state = page
    assert state == JobStreamState("running", None)
    assert events[0]["payload"] == {"text": "hello"}
    connection.fetch.assert_awaited_once()
    sql, job_id, owner, after_id, limit = connection.fetch.await_args.args
    assert (job_id, owner, after_id, limit) == ("ajob_1", "owner", 0, 500)
    # Ownership, state, and the page are read in one bounded query. The query
    # never loads the prompt, sandbox, or conversation-state subqueries.
    projection, source = sql.split("FROM", 1)
    assert "j.state, j.published_pr_url" in projection
    assert "LEFT JOIN LATERAL" in source
    assert "j.id = $1 AND j.user_id = $2" in source


async def test_missing_or_foreign_state_is_absent():
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[]))

    @asynccontextmanager
    async def acquire():
        yield connection

    repository = PostgresExecutionQueries(SimpleNamespace(acquire=acquire))
    assert await repository.job_stream_page(job_id="ajob_missing", user_id="owner") is None
