"""Bounded transcript projections; seek IDs before reading message bodies."""

from __future__ import annotations

from typing import Any

from cloud_agent.domain.thread_history import (
    THREAD_DETAIL_CHARS,
    THREAD_PAGE_MAX,
    THREAD_PENDING_LIMIT,
    THREAD_PREVIEW_CHARS,
    parse_history_cursor,
)
from cloud_agent.storage._sql.jobs import JOB_SUMMARY_SELECT
from cloud_agent.storage._sql.pending_messages import (
    PENDING_MESSAGE_COLUMNS,
    pending_attachments,
    pending_message_row,
)
from cloud_agent.storage._sql.rows import job_row_to_dict, sandbox_row_to_dict
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE

_PHASE = "CASE WHEN m.role = 'user' AND NOT m.mid_turn THEN 0 ELSE 1 END"
_OWNER_THREAD = """
SELECT t.id, t.repo, t.title, t.created_at, t.updated_at,
       t.pinned_at, t.archived_at, t.queue_paused_reason,
       latest.id AS current_job_id, latest.turn_no AS current_turn_no, j.turn_no AS anchor_turn_no
FROM agent_threads t
JOIN agent_jobs j ON j.thread_id = t.id
CROSS JOIN LATERAL (
    SELECT id, turn_no FROM agent_jobs WHERE thread_id = t.id
    ORDER BY turn_no DESC LIMIT 1
) latest
WHERE j.id = $1 AND j.user_id = $2 AND t.user_id = $2 AND t.deleted_at IS NULL
"""


def _job_projection() -> str:
    """Bound repeated wide fields before asyncpg allocates their Python values."""
    query = JOB_SUMMARY_SELECT
    truncated = []
    for field in (
        "j.task_prompt",
        "j.detail",
        "j.goal_report_reason",
        "j.goal_report_detail",
        "t.goal_objective",
        "t.goal_pause_detail",
    ):
        name = field.split(".")[1]
        query = query.replace(field, f"left({field}, {THREAD_PREVIEW_CHARS}) AS {name}")
        truncated.append(f"length({field}) > {THREAD_PREVIEW_CHARS}")
    query = query.replace("t.setup_script", "''::text AS setup_script")
    for field, empty in (("j.metadata", "{}"), ("j.attachments", "[]"), ("t.plugins", "[]")):
        name = field.split(".")[1]
        query = query.replace(
            field,
            f"CASE WHEN octet_length({field}::text) <= 16384 THEN {field} "
            f"ELSE '{empty}'::jsonb END AS {name}",
        )
        truncated.append(f"octet_length({field}::text) > 16384")
    return query.replace("SELECT ", f"SELECT ({' OR '.join(truncated)}) AS thread_preview, ", 1)


_JOBS = _job_projection()


def _entries_query(forward: bool) -> str:
    """Only bounded turn IDs and per-turn index seeks reach the final page sort."""
    direction, comparison = ("ASC", ">") if forward else ("DESC", "<")
    turn_edge, phase_edge = (1, -2) if forward else (2147483647, 2)
    return f"""
    WITH turns AS MATERIALIZED (
        SELECT id, turn_no FROM agent_jobs
        WHERE thread_id = $1 AND turn_no {comparison}= COALESCE($2::bigint, {turn_edge})
        ORDER BY turn_no {direction} LIMIT ($5 + 1)
    ), entries AS (
        SELECT j.id AS job_id, j.turn_no, -1 AS phase, 0::bigint AS id
        FROM turns j
        WHERE $2::bigint IS NULL OR (j.turn_no, -1, 0::bigint) {comparison} ($2, $3, $4)
        UNION ALL
        SELECT j.id, j.turn_no, m.phase, m.id
        FROM turns j CROSS JOIN LATERAL (
            SELECT m.id, {_PHASE} AS phase
            FROM agent_thread_messages m
            WHERE m.job_id = j.id AND
              (({_PHASE}), m.id) {comparison}
              (CASE WHEN j.turn_no = $2 THEN $3::int ELSE {phase_edge} END, COALESCE($4::bigint, 0))
            ORDER BY ({_PHASE}) {direction}, m.id {direction} LIMIT $5
        ) m
    ), chosen AS MATERIALIZED (
        SELECT * FROM entries
        ORDER BY turn_no {direction}, phase {direction}, id {direction} LIMIT $5
    )
    SELECT c.*, m.role, left(m.content, {THREAD_PREVIEW_CHARS}) AS content,
           m.created_at, m.mid_turn, length(m.content) AS content_length
    FROM chosen c LEFT JOIN agent_thread_messages m ON m.id = c.id
    ORDER BY c.turn_no {direction}, c.phase {direction}, c.id {direction}
    """


async def _pending_snapshot(conn: Any, thread_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Keep small legacy queue snapshots; never return editable truncated prompts."""
    columns = PENDING_MESSAGE_COLUMNS.replace(
        "prompt,", f"left(prompt, {THREAD_PREVIEW_CHARS}) AS prompt,"
    )
    rows = await conn.fetch(
        f"SELECT {columns}, length(prompt) AS prompt_length FROM agent_pending_messages "
        "WHERE thread_id = $1 ORDER BY position LIMIT $2",
        thread_id,
        THREAD_PENDING_LIMIT + 1,
    )
    if len(rows) > THREAD_PENDING_LIMIT or any(
        row["prompt_length"] > THREAD_PREVIEW_CHARS for row in rows
    ):
        return [], False
    held = await pending_attachments(conn, [str(row["id"]) for row in rows])
    return [pending_message_row(row, held.get(str(row["id"]))) for row in rows], True


async def read_thread_page(
    conn: Any,
    *,
    job_id: str,
    user_id: str,
    cursor: str | None,
    after: str | None,
    limit: int,
    anchor: bool = False,
) -> dict[str, Any] | None:
    """Read one repeatable snapshot, including the queue and latest turn identity."""
    if not 1 <= limit <= THREAD_PAGE_MAX or (cursor is not None and after is not None):
        raise ValueError("Invalid thread history page request")
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        thread = await conn.fetchrow(_OWNER_THREAD, job_id, user_id)
        if thread is None:
            return None
        key = parse_history_cursor(after if after is not None else cursor, thread["id"])
        if key is None and anchor:
            key = (thread["anchor_turn_no"], 2, 0)
        entries = await conn.fetch(
            _entries_query(after is not None),
            thread["id"],
            *(key or (None, None, None)),
            limit + 1,
        )
        jobs = await conn.fetch(
            f"{_JOBS} WHERE j.id = ANY($1::text[]) ORDER BY j.turn_no",
            list(dict.fromkeys(row["job_id"] for row in entries[:limit])),
        )
        sandbox = await conn.fetchrow(
            f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
            thread["id"],
        )
        rows = [
            {**job_row_to_dict(job), "thread_preview": bool(job["thread_preview"])} for job in jobs
        ]
        for row in rows:
            for field in ("title", "pinned_at", "archived_at"):
                row[field] = thread[field]
            if sandbox is not None:
                row["thread_sandbox"] = sandbox_row_to_dict(sandbox)
        pending, complete = await _pending_snapshot(conn, thread["id"])
    return {
        **dict(thread),
        "jobs": rows,
        "messages": [
            dict(row)
            for row in sorted(
                entries[:limit], key=lambda row: (row["turn_no"], row["phase"], row["id"])
            )
            if row["phase"] >= 0
        ],
        "pending_messages": pending,
        "pending_messages_complete": complete,
        "_entries": [dict(row) for row in entries[:limit]],
        "_has_more": len(entries) > limit,
        "_forward": after is not None,
    }


async def read_message_chunk(
    conn: Any,
    *,
    job_id: str,
    user_id: str,
    message_id: int,
    offset: int,
    limit: int,
) -> dict[str, Any] | None:
    """Seek one owned message; allocate only the requested Unicode-character slice."""
    if not 0 <= offset <= 2**31 - 2 or not 1 <= limit <= THREAD_DETAIL_CHARS:
        raise ValueError("Invalid thread message chunk")
    row = await conn.fetchrow(
        """
        SELECT m.id, m.job_id, m.role, substring(m.content FROM $4 + 1 FOR $5) AS content,
               length(m.content) AS content_length, m.created_at, m.mid_turn
        FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id
        JOIN agent_thread_messages m ON m.thread_id = t.id AND m.id = $3
        WHERE j.id = $1 AND j.user_id = $2 AND t.user_id = $2 AND t.deleted_at IS NULL
        """,
        job_id,
        user_id,
        message_id,
        offset,
        limit,
    )
    return dict(row) if row is not None else None
