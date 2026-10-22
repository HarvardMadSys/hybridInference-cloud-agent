"""The event append combines its row lock, post-wait clock, and job lookup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.storage._sql.attempts import live_attempt_job


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"job_id": "ajob_1", "live": True}, "ajob_1"),
        ({"job_id": "ajob_1", "live": False}, None),
        (None, None),
    ],
)
async def test_attempt_lock_clock_and_job_are_one_statement(row, expected):
    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value=row),
        fetchval=AsyncMock(),
    )

    assert await live_attempt_job(connection, 7, 3) == expected

    connection.fetchrow.assert_awaited_once()
    connection.fetchval.assert_not_awaited()
    sql, attempt_id, generation = connection.fetchrow.await_args.args
    assert (attempt_id, generation) == (7, 3)
    assert "WITH locked AS MATERIALIZED" in sql
    assert "FOR UPDATE" in sql
    assert "lease_expires_at > clock_timestamp()" in sql
