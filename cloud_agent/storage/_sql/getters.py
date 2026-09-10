"""Projections more than one repository needs, each taking a connection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import cloud_agent.storage._sql.rows as _rows

if TYPE_CHECKING:
    import asyncpg


async def get_job_projection(conn: asyncpg.Connection, job_id: str) -> dict[str, Any] | None:
    """Fetch one job by id.

    Uses the connection the caller passes, like every helper in this
    package: none of them acquires one, so which transaction the read
    joins is the caller's decision and never a surprise.
    """
    row = await conn.fetchrow(
        f"\n                SELECT {_rows._QUALIFIED_JOB_COLUMNS}, t.title, t.title_source, t.pinned_at, t.archived_at, t.deleted_at,\n                       to_jsonb(s) AS thread_sandbox\n                FROM agent_jobs j\n                LEFT JOIN agent_threads t ON t.id = j.thread_id\n                LEFT JOIN {_rows._THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id\n                WHERE j.id = $1\n                ",
        job_id,
    )
    return _rows.job_row_to_dict(row) if row else None
