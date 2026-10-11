"""The frequent SSE state read stays independent of heavyweight job details."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_agent.domain.execution import JobStreamState
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries


async def test_state_read_uses_one_narrow_owner_scoped_query():
    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value={"state": "running", "published_pr_url": None})
    )

    @asynccontextmanager
    async def acquire():
        yield connection

    repository = PostgresConversationQueries(SimpleNamespace(acquire=acquire))
    assert await repository.get_job_stream_state(
        job_id="ajob_1", user_id="owner"
    ) == JobStreamState("running", None)
    connection.fetchrow.assert_awaited_once()
    sql, job_id, owner = connection.fetchrow.await_args.args
    assert (job_id, owner) == ("ajob_1", "owner")
    # These are the performance bounds: two columns, one table lookup, and
    # no prompt, sandbox peer aggregation, or conversation-state subqueries.
    projection, source = sql.split("FROM", 1)
    assert projection.strip() == "SELECT state, published_pr_url"
    assert source.strip() == "agent_jobs WHERE id = $1 AND user_id = $2"


async def test_missing_or_foreign_state_is_absent():
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value=None))

    @asynccontextmanager
    async def acquire():
        yield connection

    repository = PostgresConversationQueries(SimpleNamespace(acquire=acquire))
    assert await repository.get_job_stream_state(job_id="ajob_missing", user_id="owner") is None
