"""Narrow, keyset-paged terminal replay routing without loading job payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.storage._sql.rows import load_json, sandbox_row_to_dict
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE

if TYPE_CHECKING:
    from datetime import datetime


async def pending_ids(conn: Any, *, limit: int) -> list[str]:
    """Read pending ids for callers that do not need a routing manifest."""
    rows = await conn.fetch(
        "SELECT id FROM agent_jobs WHERE terminal_resume_pending = TRUE "
        "AND state = ANY($1::text[]) ORDER BY updated_at, id LIMIT $2",
        list(TERMINAL_STATES),
        limit,
    )
    return [row["id"] for row in rows]


async def page(
    conn: Any, *, after: tuple[datetime, str] | None, limit: int
) -> list[dict[str, Any]]:
    """Select candidates first, then only their current host and shared manifest.

    Ownership matches host_for_job: a running attempt, else the current group,
    else the last attempt. Missing host rows keep the durable owner but no URL,
    so the normal routing validator continues to fail closed.
    """
    owner = "COALESCE(CASE WHEN a.status = 'running' THEN a.host_id END, s.host, a.host_id)"
    after_clause = ""
    arguments: list[Any] = [list(TERMINAL_STATES), limit]
    if after is not None:
        after_clause = "AND (updated_at, id) > ($3::timestamptz, $4::text)"
        arguments.extend(after)
    rows = await conn.fetch(
        f"""
        WITH candidates AS MATERIALIZED (
            SELECT id, thread_id, current_attempt_id, updated_at
            FROM agent_jobs
            WHERE terminal_resume_pending = TRUE AND state = ANY($1::text[])
              {after_clause}
            ORDER BY updated_at, id LIMIT $2
        )
        SELECT j.id, j.thread_id, j.updated_at, to_jsonb(s) AS thread_sandbox,
               {owner} AS host, h.broker_url AS sandbox_manager_url
        FROM candidates j
        LEFT JOIN agent_attempts a ON a.id = j.current_attempt_id
        LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
        LEFT JOIN agent_runner_hosts h ON h.host = {owner}
        ORDER BY j.updated_at, j.id
        """,
        *arguments,
    )
    result = []
    for row in rows:
        job = dict(row)
        sandbox = load_json(job["thread_sandbox"])
        job["thread_sandbox"] = sandbox_row_to_dict(sandbox) if sandbox is not None else None
        result.append(job)
    return result
