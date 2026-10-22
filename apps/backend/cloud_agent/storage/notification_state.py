"""Bounded active-job bootstrap and durable owner notification deltas.

A PostgreSQL snapshot is the cursor. Per-row markers coalesce repeated writes
and make delayed commits visible without scanning an owner's historical jobs.
"""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Any

from cloud_agent.notification_state import (
    NotificationStateCursor,
    decode_notification_cursor,
    encode_cursor,
    notification_scope,
)

if TYPE_CHECKING:
    import asyncpg

_PRUNE_INTERVAL_SECONDS = 60.0
_next_prune_at = 0.0


async def read_notification_state(
    pool: asyncpg.Pool,
    *,
    user_id: str,
    cursor_value: str | None,
    limit: int,
) -> dict[str, Any]:
    """Return active jobs once, then only identities changed since the cursor."""
    cursor = decode_notification_cursor(cursor_value, user_id=user_id, limit=limit)
    async with pool.acquire() as conn:
        await _prune_if_due(conn, force=cursor is None)
        async with conn.transaction(isolation="repeatable_read"):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            if cursor is None:
                observed_at, snapshot = await conn.fetchrow(
                    "SELECT NOW(),pg_current_snapshot()::text"
                )
                cursor = NotificationStateCursor(
                    scope=notification_scope(user_id, limit),
                    database_snapshot=snapshot,
                    bootstrap=True,
                    bootstrap_at=observed_at,
                )
                return await _bootstrap(conn, cursor, user_id, limit, observed_at)
            if cursor.bootstrap:
                observed_at = await conn.fetchval("SELECT NOW()")
                return await _bootstrap(conn, cursor, user_id, limit, observed_at)
            return await _changes(conn, cursor, user_id, limit)


def _job(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "title": row["title"],
        "repo": row["repo"],
        "state": row["state"],
        "current_attempt_id": row["current_attempt_id"],
        "turn_no": row["turn_no"],
        "revision": row["revision"],
        "cancel_requested": row["cancel_requested"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
    }


async def _job_rows(conn: Any, *, user_id: str, ids: list[str]) -> list[Any]:
    return await conn.fetch(
        """
        SELECT j.id,j.thread_id,t.title,t.repo,j.state,j.current_attempt_id,j.turn_no,
               j.revision,j.cancel_requested,a.finished_at,j.updated_at
        FROM agent_jobs j
        JOIN agent_threads t ON t.id=j.thread_id AND t.user_id=j.user_id
        LEFT JOIN agent_attempts a ON a.id=j.current_attempt_id
        WHERE j.user_id=$1 AND t.deleted_at IS NULL
          AND j.fork_source_job_id IS NULL
          AND j.id=ANY($2::text[])
        ORDER BY j.id
        """,
        user_id,
        ids,
    )


async def _bootstrap(
    conn: Any,
    cursor: NotificationStateCursor,
    user_id: str,
    limit: int,
    observed_at: Any,
) -> dict[str, Any]:
    rows = await conn.fetch(
        """
        WITH candidates AS MATERIALIZED (
            SELECT id
            FROM agent_jobs
            WHERE user_id=$1 AND state IN ('queued','running')
              AND fork_source_job_id IS NULL
              AND ($2::text IS NULL OR id>$2)
            UNION ALL
            SELECT c.entity_id
            FROM agent_notification_changes c
            JOIN agent_jobs recent ON recent.id=c.entity_id AND recent.user_id=c.user_id
            LEFT JOIN agent_attempts completed ON completed.id=recent.current_attempt_id
            WHERE c.user_id=$1 AND c.kind='job'
              AND recent.fork_source_job_id IS NULL
              AND c.changed_at >= $4::timestamptz - interval '24 hours'
              AND COALESCE(completed.finished_at,recent.updated_at)
                  >= $4::timestamptz - interval '24 hours'
              AND recent.state IN ('succeeded','failed','cancelled')
              AND ($2::text IS NULL OR c.entity_id>$2)
        )
        SELECT j.id,j.thread_id,t.title,t.repo,j.state,j.current_attempt_id,j.turn_no,
               j.revision,j.cancel_requested,a.finished_at,j.updated_at
        FROM candidates c
        JOIN agent_jobs j ON j.id=c.id AND j.user_id=$1
        JOIN agent_threads t ON t.id=j.thread_id AND t.user_id=j.user_id
        LEFT JOIN agent_attempts a ON a.id=j.current_attempt_id
        WHERE t.deleted_at IS NULL
        ORDER BY j.id LIMIT $3
        """,
        user_id,
        cursor.after_id,
        limit + 1,
        cursor.bootstrap_at,
    )
    page = rows[:limit]
    more = len(rows) > limit
    continuation = cursor.model_copy(
        update={"after_id": page[-1]["id"] if page else cursor.after_id}
    )
    changes = NotificationStateCursor(
        scope=cursor.scope,
        database_snapshot=cursor.database_snapshot,
    )
    return {
        "mode": "snapshot",
        "jobs": [_job(row) for row in page],
        "threads": [],
        "deleted_job_ids": [],
        "deleted_thread_ids": [],
        "observed_at": observed_at,
        "next_cursor": encode_cursor(continuation) if more else None,
        "change_cursor": encode_cursor(changes) if not more else None,
        "reset_required": False,
    }


async def _changes(
    conn: Any,
    cursor: NotificationStateCursor,
    user_id: str,
    limit: int,
) -> dict[str, Any]:
    metadata = await conn.fetchrow(
        """
        SELECT NOW() AS observed_at,pg_current_snapshot()::text AS snapshot,
               COALESCE((
                   SELECT floor_xid FROM agent_notification_change_floors WHERE user_id=$2
               ),'0') > pg_snapshot_xmin($1::text::pg_snapshot) AS expired
        """,
        cursor.database_snapshot,
        user_id,
    )
    if metadata["expired"]:
        return {
            "mode": "delta",
            "jobs": [],
            "threads": [],
            "deleted_job_ids": [],
            "deleted_thread_ids": [],
            "observed_at": metadata["observed_at"],
            "next_cursor": None,
            "change_cursor": None,
            "reset_required": True,
        }

    target = cursor.target_snapshot or metadata["snapshot"]
    changes = await conn.fetch(
        """
        WITH changed AS MATERIALIZED (
            SELECT kind,entity_id FROM agent_notification_changes
            WHERE user_id=$1
              AND change_xid>=pg_snapshot_xmax($2::text::pg_snapshot)
              AND change_xid<pg_snapshot_xmax($5::text::pg_snapshot)
              AND NOT change_xid=ANY(
                  ARRAY(SELECT pg_snapshot_xip($5::text::pg_snapshot))
              )
            UNION ALL
            SELECT kind,entity_id FROM agent_notification_changes
            WHERE user_id=$1
              AND change_xid=ANY(ARRAY(SELECT pg_snapshot_xip($2::text::pg_snapshot)))
              AND change_xid<pg_snapshot_xmax($5::text::pg_snapshot)
              AND NOT change_xid=ANY(
                  ARRAY(SELECT pg_snapshot_xip($5::text::pg_snapshot))
              )
        )
        SELECT kind,entity_id FROM changed
        WHERE $3::text IS NULL OR (kind,entity_id)>($3,$4)
        ORDER BY kind,entity_id LIMIT $6
        """,
        user_id,
        cursor.database_snapshot,
        cursor.after_kind,
        cursor.after_id,
        target,
        limit + 1,
    )
    selected = changes[:limit]
    job_ids = [row["entity_id"] for row in selected if row["kind"] == "job"]
    thread_ids = [row["entity_id"] for row in selected if row["kind"] == "thread"]
    jobs = await _job_rows(conn, user_id=user_id, ids=job_ids) if job_ids else []
    threads = (
        await conn.fetch(
            """
            SELECT id,title,revision FROM agent_threads
            WHERE user_id=$1 AND deleted_at IS NULL AND id=ANY($2::text[])
            ORDER BY id
            """,
            user_id,
            thread_ids,
        )
        if thread_ids
        else []
    )
    present_jobs = {row["id"] for row in jobs}
    present_threads = {row["id"] for row in threads}
    continuation = cursor.model_copy(
        update={
            "target_snapshot": target,
            "after_kind": selected[-1]["kind"] if selected else None,
            "after_id": selected[-1]["entity_id"] if selected else None,
        }
    )
    return {
        "mode": "delta",
        "jobs": [_job(row) for row in jobs],
        "threads": [dict(row) for row in threads],
        "deleted_job_ids": [identity for identity in job_ids if identity not in present_jobs],
        "deleted_thread_ids": [
            identity for identity in thread_ids if identity not in present_threads
        ],
        "observed_at": metadata["observed_at"],
        "next_cursor": encode_cursor(continuation) if len(changes) > limit else None,
        "change_cursor": (
            encode_cursor(NotificationStateCursor(scope=cursor.scope, database_snapshot=target))
            if len(changes) <= limit
            else None
        ),
        "reset_required": False,
    }


async def _prune_if_due(conn: Any, *, force: bool = False) -> None:
    """Bound retention work across bootstrap and long-lived delta clients."""
    global _next_prune_at

    now = monotonic()
    if not force and now < _next_prune_at:
        return
    _next_prune_at = now + _PRUNE_INTERVAL_SECONDS
    try:
        await _prune(conn)
    except BaseException:
        _next_prune_at = 0.0
        raise


async def _prune(conn: Any) -> None:
    """Bound cleanup; a cursor behind its owner's floor explicitly rebuilds."""
    async with conn.transaction(isolation="read_committed"):
        await conn.execute("SET LOCAL statement_timeout = '5s'")
        await conn.execute(
            """
            WITH expired AS (
                SELECT kind,entity_id FROM agent_notification_changes
                WHERE changed_at<NOW()-interval '24 hours'
                  AND change_xid<pg_snapshot_xmin(pg_current_snapshot())
                ORDER BY changed_at LIMIT 500 FOR UPDATE SKIP LOCKED
            ), removed AS (
                DELETE FROM agent_notification_changes c USING expired e
                WHERE c.kind=e.kind AND c.entity_id=e.entity_id
                RETURNING c.user_id,c.change_xid
            )
            INSERT INTO agent_notification_change_floors (user_id,floor_xid)
            SELECT user_id,(max(change_xid::text::numeric)+1)::text::xid8
            FROM removed GROUP BY user_id
            ON CONFLICT (user_id) DO UPDATE SET floor_xid=GREATEST(
                agent_notification_change_floors.floor_xid,EXCLUDED.floor_xid
            )
            """
        )
