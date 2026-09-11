"""One event row, written inside the caller's transaction.

Lifted verbatim from the single storage class this package replaced. Two
capabilities write events -- an attempt reporting its progress, and a snapshot
publish recording its outcome -- so the statement belongs to neither and lives
here, taking the caller's connection as its first argument exactly as it did
as a method.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


async def insert_event(
    conn: Any,
    *,
    job_id: str,
    attempt_id: int,
    event_type: str,
    payload: dict[str, Any] | None,
    client_seq: int | None = None,
) -> int:
    """Insert one event row inside the caller's transaction.

    A ``client_seq`` the attempt has already stored is a retry of a
    response that never arrived: answer with the stored row's id and do
    not run the thread-message mirror again — it ran when the row landed.
    """
    if client_seq is not None:
        existing = await conn.fetchval(
            "SELECT id FROM agent_job_events WHERE attempt_id = $1 AND client_seq = $2",
            attempt_id,
            client_seq,
        )
        if existing is not None:
            return int(existing)
    event_id = await conn.fetchval(
        """
            INSERT INTO agent_job_events (job_id, attempt_id, seq, event_type, payload, client_seq)
            VALUES (
                $1, $2,
                (SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_job_events WHERE attempt_id = $2),
                $3, $4::jsonb, $5
            )
            RETURNING id
            """,
        job_id,
        attempt_id,
        event_type,
        json.dumps(payload) if payload is not None else None,
        client_seq,
    )
    text = (payload or {}).get("text")
    if event_type == "message" and isinstance(text, str) and text.strip():
        await conn.execute(
            """
                INSERT INTO agent_thread_messages
                    (thread_id, job_id, role, content, source_event_id)
                SELECT thread_id, id, 'assistant', $2, $3
                FROM agent_jobs
                WHERE id = $1 AND thread_id IS NOT NULL
                ON CONFLICT (source_event_id) DO NOTHING
                """,
            job_id,
            text,
            event_id,
        )
        await conn.execute(
            """
                UPDATE agent_threads t
                SET updated_at = NOW()
                FROM agent_jobs j
                WHERE j.id = $1 AND t.id = j.thread_id
                """,
            job_id,
        )
    return event_id
