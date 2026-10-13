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

from cloud_agent_shared.event_payloads import subagent_owned
from cloud_agent_shared.shared_protocol import USER_MESSAGE_EVENT

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
    is_subagent = subagent_owned(payload)
    if event_type == "message" and isinstance(text, str) and text.strip() and not is_subagent:
        # A child agent's reply stays in the recorded trajectory. Mirroring it
        # into the assistant transcript would expose it as the main answer in
        # fallback reads and let goal reporting consume the child's response.
        # ``truncated`` travels with the text because it is a fact about the
        # text: the runtime clipped this message, so it is not the model's whole
        # answer and the part that was cut off may be the part that matters. A
        # reader that needs the *final* message — the goal report is one — has
        # to be able to tell that from a message that simply ended.
        await conn.execute(
            """
                INSERT INTO agent_thread_messages
                    (thread_id, job_id, role, content, source_event_id, truncated)
                SELECT thread_id, id, 'assistant', $2, $3, $4
                FROM agent_jobs
                WHERE id = $1 AND thread_id IS NOT NULL
                ON CONFLICT (source_event_id) DO NOTHING
                """,
            job_id,
            text,
            event_id,
            (payload or {}).get("truncated") is True,
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
    elif event_type == USER_MESSAGE_EVENT:
        await _take_message_sent_into_turn(conn, job_id=job_id, event_id=event_id, payload=payload)
    return event_id


async def _take_message_sent_into_turn(
    conn: Any, *, job_id: str, event_id: int, payload: dict[str, Any] | None
) -> None:
    """Move a message the owner sent into this turn from the queue to the transcript.

    The turn's host reports the point where the turn took the message; this is
    where the queue lets go of it, whatever state it is in, because the model
    has it now and sending it again would repeat it. The text is the queue's
    own copy while it still has the message. The thread is locked before the
    queue row, the order a settlement takes them in.
    """
    thread_id = await conn.fetchval("SELECT thread_id FROM agent_jobs WHERE id = $1", job_id)
    if thread_id is None:
        return
    await conn.execute("SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", thread_id)
    message_id = (payload or {}).get("pending_message_id")
    waiting = None
    if isinstance(message_id, str) and message_id:
        waiting = await conn.fetchrow(
            "SELECT prompt FROM agent_pending_messages WHERE id = $1 AND thread_id = $2 FOR UPDATE",
            message_id,
            thread_id,
        )
    text = str(waiting["prompt"]) if waiting is not None else str((payload or {}).get("text") or "")
    if not text.strip():
        return
    await conn.execute(
        """
        INSERT INTO agent_thread_messages
            (thread_id, job_id, role, content, source_event_id, mid_turn)
        VALUES ($1, $2, 'user', $3, $4, TRUE)
        ON CONFLICT (source_event_id) DO NOTHING
        """,
        thread_id,
        job_id,
        text,
        event_id,
    )
    if waiting is not None:
        await conn.execute("DELETE FROM agent_pending_messages WHERE id = $1", message_id)
    await conn.execute("UPDATE agent_threads SET updated_at = NOW() WHERE id = $1", thread_id)
