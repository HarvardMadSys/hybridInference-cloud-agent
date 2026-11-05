"""A job stream's read on each wake-up stays one narrow, owner-scoped statement."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud_agent.domain.execution import JobStreamState
from cloud_agent.storage.repositories.execution_queries import PostgresExecutionQueries

_NO_EVENT = dict.fromkeys(
    ("id", "job_id", "attempt_id", "seq", "event_type", "payload", "created_at")
)


def _repository(rows: list[dict]) -> tuple[PostgresExecutionQueries, SimpleNamespace]:
    connection = SimpleNamespace(fetch=AsyncMock(return_value=rows))

    @asynccontextmanager
    async def acquire():
        yield connection

    return PostgresExecutionQueries(SimpleNamespace(acquire=acquire)), connection


async def test_a_wake_up_reads_events_and_state_in_one_narrow_owner_scoped_statement():
    stored = {
        "id": 9,
        "job_id": "ajob_1",
        "attempt_id": 7,
        "seq": 1,
        "event_type": "message",
        "payload": '{"text": "hi"}',
        "created_at": datetime(2026, 10, 21, tzinfo=UTC),
    }
    repository, connection = _repository([{"state": "running", "published_pr_url": None, **stored}])

    page = await repository.job_stream_page(job_id="ajob_1", user_id="owner", after_id=3, limit=500)

    assert page == ([{**stored, "payload": {"text": "hi"}}], JobStreamState("running", None))
    connection.fetch.assert_awaited_once()
    sql, job_id, owner, after_id, limit = connection.fetch.await_args.args
    assert (job_id, owner, after_id, limit) == ("ajob_1", "owner", 3, 500)
    # These are the performance bounds of the state half: two columns of the
    # owned job, and no prompt, sandbox peer aggregation, or conversation-state
    # subqueries, on every wake-up.
    assert "SELECT j.state, j.published_pr_url," in sql
    assert "WHERE j.id = $1 AND j.user_id = $2" in sql
    for heavy in ("task_prompt", "agent_threads", "agent_sandboxes", "memberships"):
        assert heavy not in sql


async def test_nothing_new_still_answers_how_the_job_stands():
    repository, _ = _repository(
        [{"state": "succeeded", "published_pr_url": "https://example.test/pr/1", **_NO_EVENT}]
    )

    assert await repository.job_stream_page(
        job_id="ajob_1", user_id="owner", after_id=9, limit=500
    ) == ([], JobStreamState("succeeded", "https://example.test/pr/1"))


async def test_a_missing_or_foreign_job_answers_nothing():
    repository, _ = _repository([])

    assert (
        await repository.job_stream_page(
            job_id="ajob_missing", user_id="owner", after_id=0, limit=500
        )
        is None
    )
