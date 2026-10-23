"""Notification delta pages seek through bounded, index-aligned branches."""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent.notification_state import NotificationStateCursor
from cloud_agent.storage.notification_state import _change_rows


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.calls.append((query, args))
        return []


@pytest.mark.asyncio
async def test_change_pages_bound_each_branch_and_seek_without_nullable_predicates():
    connection = _Connection()
    first = NotificationStateCursor(scope="scope", database_snapshot="100:104:101")
    after_job = NotificationStateCursor(
        scope="scope",
        database_snapshot="100:104:101",
        target_snapshot="104:110:108",
        after_kind="job",
        after_xid=106,
        after_id="job-4",
    )
    after_thread = after_job.model_copy(
        update={"after_kind": "thread", "after_xid": 107, "after_id": "thread-2"}
    )

    for cursor in (first, after_job, after_thread):
        await _change_rows(
            connection,
            cursor=cursor,
            user_id="owner",
            target_snapshot="104:110:108",
            scan_limit=201,
        )

    first_sql, first_args = connection.calls[0]
    job_sql, job_args = connection.calls[1]
    thread_sql, thread_args = connection.calls[2]
    for query, _args in connection.calls:
        assert "MATERIALIZED" not in query
        assert "IS NULL OR" not in query
        assert query.count("CROSS JOIN LATERAL") == 2
        assert query.count("c.change_xid=eligible.base_xid") == 2
        assert query.count("SELECT base_xid FROM eligible_base_xids ORDER BY base_xid") == 2
        assert query.count("ORDER BY c.entity_id LIMIT $4") == 2
        assert query.count("ORDER BY c.change_xid,c.entity_id LIMIT $4") == 2
        assert query.count("ORDER BY eligible.base_xid,page.entity_id LIMIT $4") == 2
        assert "ORDER BY changed.kind,changed.change_xid,changed.entity_id LIMIT $4" in query
    assert _CHANGE_KEYSET not in first_sql
    assert "AND FALSE" not in first_sql
    assert first_args == ("owner", "100:104:101", "104:110:108", 201)
    assert job_sql.count(_CHANGE_KEYSET) == 2
    assert "AND FALSE" not in job_sql
    assert job_args[-2:] == ("106", "job-4")
    assert thread_sql.count(_CHANGE_KEYSET) == 2
    assert thread_sql.count("AND FALSE") == 2
    assert thread_args[-2:] == ("107", "thread-2")


@pytest.mark.asyncio
async def test_v1_continuation_keeps_its_legacy_ordering():
    connection = _Connection()
    cursor = NotificationStateCursor(
        version=1,
        scope="scope",
        database_snapshot="100:104:101",
        target_snapshot="104:110:108",
        after_kind="thread",
        after_id="thread-2",
    )

    await _change_rows(
        connection,
        cursor=cursor,
        user_id="owner",
        target_snapshot="104:110:108",
        scan_limit=201,
    )

    query, args = connection.calls[0]
    assert "WITH changed AS MATERIALIZED" in query
    assert "ORDER BY kind,entity_id LIMIT $6" in query
    assert _CHANGE_KEYSET not in query
    assert args == (
        "owner",
        "100:104:101",
        "thread",
        "thread-2",
        "104:110:108",
        201,
    )


_CHANGE_KEYSET = "(c.change_xid,c.entity_id)>($5::text::xid8,$6::text)"
