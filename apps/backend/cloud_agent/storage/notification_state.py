"""Bounded active-job bootstrap and durable owner notification deltas.

A PostgreSQL snapshot is the cursor. Per-row markers coalesce repeated writes
and make delayed commits visible without scanning an owner's historical jobs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.notification_state import (
    NotificationStateCursor,
    decode_notification_cursor,
    encode_cursor,
    notification_scope,
)

if TYPE_CHECKING:
    import asyncpg


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
        # Define enablement in the database clock domain before maintenance or
        # bootstrap work. Every terminal timestamp is compared to this same
        # request-receipt baseline, so response latency can neither replay old
        # history nor move the baseline past a completion found by this read.
        bootstrap_at = await conn.fetchval("SELECT clock_timestamp()") if cursor is None else None
        async with conn.transaction(isolation="repeatable_read"):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            if cursor is None:
                snapshot = await conn.fetchval("SELECT pg_current_snapshot()::text")
                cursor = NotificationStateCursor(
                    scope=notification_scope(user_id, limit),
                    database_snapshot=snapshot,
                    bootstrap=True,
                    bootstrap_at=bootstrap_at,
                    bootstrap_phase="active",
                )
                return await _bootstrap(conn, cursor, user_id, limit)
            if cursor.bootstrap:
                return await _bootstrap(conn, cursor, user_id, limit)
            return await _changes(conn, cursor, user_id, limit)


def _job(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "title": row["title"],
        "thread_revision": row["thread_revision"],
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
        SELECT j.id,j.thread_id,t.title,t.revision AS thread_revision,t.repo,
               j.state,j.current_attempt_id,j.turn_no,
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


_ACTIVE_CANDIDATES_SQL = """
    SELECT id FROM agent_jobs
    WHERE user_id=$1 AND state IN ('queued','running')
      AND fork_source_job_id IS NULL
      AND ($2::text IS NULL OR id>$2)
    ORDER BY id LIMIT $3
"""

_RECENT_CANDIDATES_SQL = """
    SELECT changed_at,entity_id
    FROM agent_notification_changes
    WHERE user_id=$1 AND kind='job' AND terminal
      AND changed_at >= $2::timestamptz - interval '24 hours'
      AND (
          $3::timestamptz IS NULL
          OR (changed_at,entity_id)<($3::timestamptz,$4::text)
      )
    ORDER BY changed_at DESC,entity_id DESC LIMIT $5
"""


async def _active_candidates(
    conn: Any, *, user_id: str, after_id: str | None, scan_limit: int
) -> list[Any]:
    """Read at most one index-ordered page, before any visibility joins."""
    return await conn.fetch(
        _ACTIVE_CANDIDATES_SQL,
        user_id,
        after_id,
        scan_limit,
    )


async def _recent_candidates(
    conn: Any,
    *,
    user_id: str,
    bootstrap_at: Any,
    after_changed_at: Any,
    after_id: str | None,
    scan_limit: int,
) -> list[Any]:
    """Read one bounded page from the terminal-marker covering index."""
    return await conn.fetch(
        _RECENT_CANDIDATES_SQL,
        user_id,
        bootstrap_at,
        after_changed_at,
        after_id,
        scan_limit,
    )


async def _recent_job_rows(
    conn: Any, *, user_id: str, ids: list[str], bootstrap_at: Any
) -> list[Any]:
    """Hydrate only terminal rows whose actual completion remains recent."""
    return await conn.fetch(
        """
        SELECT j.id,j.thread_id,t.title,t.revision AS thread_revision,t.repo,
               j.state,j.current_attempt_id,j.turn_no,
               j.revision,j.cancel_requested,a.finished_at,j.updated_at
        FROM agent_jobs j
        JOIN agent_threads t ON t.id=j.thread_id AND t.user_id=j.user_id
        LEFT JOIN agent_attempts a ON a.id=j.current_attempt_id
        WHERE j.user_id=$1 AND t.deleted_at IS NULL
          AND j.fork_source_job_id IS NULL
          AND j.id=ANY($2::text[])
          AND j.state IN ('succeeded','failed','cancelled')
          AND COALESCE(a.finished_at,j.updated_at)
              >= $3::timestamptz - interval '24 hours'
        ORDER BY j.id
        """,
        user_id,
        ids,
        bootstrap_at,
    )


def _bootstrap_result(
    cursor: NotificationStateCursor,
    rows: list[Any],
    continuation: NotificationStateCursor | None,
) -> dict[str, Any]:
    changes = NotificationStateCursor(
        scope=cursor.scope,
        database_snapshot=cursor.database_snapshot,
    )
    return {
        "mode": "snapshot",
        "jobs": [_job(row) for row in rows],
        "threads": [],
        "deleted_job_ids": [],
        "deleted_thread_ids": [],
        "observed_at": cursor.bootstrap_at,
        "next_cursor": encode_cursor(continuation) if continuation is not None else None,
        "change_cursor": encode_cursor(changes) if continuation is None else None,
        "reset_required": False,
    }


async def _bootstrap(
    conn: Any,
    cursor: NotificationStateCursor,
    user_id: str,
    limit: int,
) -> dict[str, Any]:
    """Page active jobs, then recent terminals, with O(limit) candidate reads."""
    phase = cursor.bootstrap_phase or "active"
    page: list[Any] = []

    if phase == "active":
        candidates = await _active_candidates(
            conn,
            user_id=user_id,
            after_id=cursor.after_id,
            scan_limit=limit + 1,
        )
        selected = candidates[:limit]
        if selected:
            page.extend(
                await _job_rows(
                    conn,
                    user_id=user_id,
                    ids=[row["id"] for row in selected],
                )
            )
        if len(candidates) > limit:
            continuation = cursor.model_copy(
                update={
                    "bootstrap_phase": "active",
                    "after_changed_at": None,
                    "after_id": selected[-1]["id"],
                }
            )
            return _bootstrap_result(cursor, page, continuation)

        remaining = limit - len(page)
        if remaining == 0:
            continuation = cursor.model_copy(
                update={
                    "bootstrap_phase": "recent",
                    "after_changed_at": None,
                    "after_id": None,
                }
            )
            return _bootstrap_result(cursor, page, continuation)
        after_changed_at = None
        after_id = None
    else:
        remaining = limit
        after_changed_at = cursor.after_changed_at
        after_id = cursor.after_id

    recent = await _recent_candidates(
        conn,
        user_id=user_id,
        bootstrap_at=cursor.bootstrap_at,
        after_changed_at=after_changed_at,
        after_id=after_id,
        scan_limit=remaining + 1,
    )
    selected_recent = recent[:remaining]
    if selected_recent:
        page.extend(
            await _recent_job_rows(
                conn,
                user_id=user_id,
                ids=[row["entity_id"] for row in selected_recent],
                bootstrap_at=cursor.bootstrap_at,
            )
        )
    recent_continuation: NotificationStateCursor | None = None
    if len(recent) > remaining:
        boundary = selected_recent[-1]
        recent_continuation = cursor.model_copy(
            update={
                "bootstrap_phase": "recent",
                "after_changed_at": boundary["changed_at"],
                "after_id": boundary["entity_id"],
            }
        )
    return _bootstrap_result(cursor, page, recent_continuation)


_CHANGE_KEYSET_CLAUSE = "AND (c.change_xid,c.entity_id)>($5::text::xid8,$6::text)"

_LEGACY_CHANGE_PAGE_SQL = """
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
    SELECT NULL::text AS change_xid,kind,entity_id FROM changed
    WHERE (kind,entity_id)>($3,$4)
    ORDER BY kind,entity_id LIMIT $6
"""


def _legacy_change_page(cursor: NotificationStateCursor) -> bool:
    """Finish a v1 continuation in the ordering that created its boundary."""
    return cursor.version == 1 and cursor.target_snapshot is not None


def _change_after_clauses(cursor: NotificationStateCursor) -> tuple[str, str]:
    """Build kind-local keysets without nullable predicates in either index scan."""
    if cursor.after_xid is None:
        return "", ""
    if cursor.after_kind == "job":
        return _CHANGE_KEYSET_CLAUSE, ""
    return "AND FALSE", _CHANGE_KEYSET_CLAUSE


def _change_page_statement(cursor: NotificationStateCursor) -> str:
    """Merge four independently bounded index scans into one owner page."""
    job_after, thread_after = _change_after_clauses(cursor)
    return f"""
        WITH eligible_base_xids AS (
            SELECT base_xid
            FROM pg_snapshot_xip($2::text::pg_snapshot) AS base_xids(base_xid)
            WHERE base_xid<pg_snapshot_xmax($3::text::pg_snapshot)
            EXCEPT
            SELECT target_xid
            FROM pg_snapshot_xip($3::text::pg_snapshot) AS target_xids(target_xid)
        )
        SELECT changed.change_xid::text AS change_xid,changed.kind,changed.entity_id
        FROM (
            (
                SELECT page.change_xid,page.kind,page.entity_id
                FROM (
                    SELECT base_xid FROM eligible_base_xids ORDER BY base_xid
                ) AS eligible
                CROSS JOIN LATERAL (
                    SELECT c.change_xid,c.kind,c.entity_id
                    FROM agent_notification_changes AS c
                    WHERE c.user_id=$1 AND c.kind='job'
                      AND c.change_xid=eligible.base_xid
                      {job_after}
                    ORDER BY c.entity_id LIMIT $4
                ) AS page
                ORDER BY eligible.base_xid,page.entity_id LIMIT $4
            )
            UNION ALL
            (
                SELECT c.change_xid,c.kind,c.entity_id
                FROM agent_notification_changes AS c
                WHERE c.user_id=$1 AND c.kind='job'
                  AND c.change_xid>=pg_snapshot_xmax($2::text::pg_snapshot)
                  AND c.change_xid<pg_snapshot_xmax($3::text::pg_snapshot)
                  AND NOT c.change_xid=ANY(
                      ARRAY(SELECT pg_snapshot_xip($3::text::pg_snapshot))
                  )
                  {job_after}
                ORDER BY c.change_xid,c.entity_id LIMIT $4
            )
            UNION ALL
            (
                SELECT page.change_xid,page.kind,page.entity_id
                FROM (
                    SELECT base_xid FROM eligible_base_xids ORDER BY base_xid
                ) AS eligible
                CROSS JOIN LATERAL (
                    SELECT c.change_xid,c.kind,c.entity_id
                    FROM agent_notification_changes AS c
                    WHERE c.user_id=$1 AND c.kind='thread'
                      AND c.change_xid=eligible.base_xid
                      {thread_after}
                    ORDER BY c.entity_id LIMIT $4
                ) AS page
                ORDER BY eligible.base_xid,page.entity_id LIMIT $4
            )
            UNION ALL
            (
                SELECT c.change_xid,c.kind,c.entity_id
                FROM agent_notification_changes AS c
                WHERE c.user_id=$1 AND c.kind='thread'
                  AND c.change_xid>=pg_snapshot_xmax($2::text::pg_snapshot)
                  AND c.change_xid<pg_snapshot_xmax($3::text::pg_snapshot)
                  AND NOT c.change_xid=ANY(
                      ARRAY(SELECT pg_snapshot_xip($3::text::pg_snapshot))
                  )
                  {thread_after}
                ORDER BY c.change_xid,c.entity_id LIMIT $4
            )
        ) AS changed
        ORDER BY changed.kind,changed.change_xid,changed.entity_id LIMIT $4
    """


async def _change_rows(
    conn: Any,
    *,
    cursor: NotificationStateCursor,
    user_id: str,
    target_snapshot: str,
    scan_limit: int,
) -> list[Any]:
    if _legacy_change_page(cursor):
        return await conn.fetch(
            _LEGACY_CHANGE_PAGE_SQL,
            user_id,
            cursor.database_snapshot,
            cursor.after_kind,
            cursor.after_id,
            target_snapshot,
            scan_limit,
        )
    arguments: list[Any] = [
        user_id,
        cursor.database_snapshot,
        target_snapshot,
        scan_limit,
    ]
    if cursor.after_xid is not None:
        arguments.extend((str(cursor.after_xid), cursor.after_id))
    return await conn.fetch(_change_page_statement(cursor), *arguments)


async def _changes(
    conn: Any,
    cursor: NotificationStateCursor,
    user_id: str,
    limit: int,
) -> dict[str, Any]:
    metadata = await conn.fetchrow(
        """
        SELECT NOW() AS observed_at,pg_current_snapshot()::text AS snapshot,
               COALESCE(NOT pg_visible_in_snapshot((
                   SELECT floor_xid FROM agent_notification_change_floors WHERE user_id=$2
               ),$1::text::pg_snapshot),FALSE) AS expired
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
    changes = await _change_rows(
        conn,
        cursor=cursor,
        user_id=user_id,
        target_snapshot=target,
        scan_limit=limit + 1,
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
    legacy_page = _legacy_change_page(cursor)
    continuation = cursor.model_copy(
        update={
            "version": 1 if legacy_page else 2,
            "target_snapshot": target,
            "after_kind": selected[-1]["kind"] if selected else None,
            "after_xid": (None if legacy_page or not selected else int(selected[-1]["change_xid"])),
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
            encode_cursor(
                NotificationStateCursor(version=2, scope=cursor.scope, database_snapshot=target)
            )
            if len(changes) <= limit
            else None
        ),
        "reset_required": False,
    }
