"""Pending-message rows, and the statements every writer of a thread's queue shares.

A thread's queue is read by the owner's transcript and written by the queue's
own repository, by cancellation and by archiving. The projection and the pause
statement live here so that all of them read and stop the queue the same way.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.storage._sql.rows import load_json

#: A pending message as every reader sees it.
PENDING_MESSAGE_COLUMNS = (
    "id, thread_id, user_id, position, state, revision, prompt, skill_ids, "
    "attachment_ids, model, reasoning_effort, client_request_id, "
    "client_request_fingerprint, send_now_job_id, send_now_requested_at, "
    "last_error, created_at, updated_at"
)

#: A turn that has not ended. Anything that is not terminal counts, so a state
#: added later is waited for rather than raced.
LIVE_TURN_SQL = "state NOT IN ('succeeded', 'failed', 'cancelled')"

#: Stop a queue from moving by itself, if it holds anything, and say why.
#:
#: ``COALESCE`` keeps the first reason: a queue that stopped because the owner
#: pressed Stop still says so after that turn ends ``cancelled``, and after an
#: archive. Only the owner's continue clears it. A thread with an empty queue is
#: left alone — there is nothing to hold back, and a reason on it would make the
#: next message the owner sends wait for no visible cause.
PAUSE_QUEUE_SQL = """
UPDATE agent_threads t
SET queue_paused_reason = COALESCE(t.queue_paused_reason, $2)
WHERE t.id = ANY($1::text[])
  AND EXISTS (SELECT 1 FROM agent_pending_messages p WHERE p.thread_id = t.id)
"""


class PendingAttachmentsUnavailable(Exception):
    """Raised inside a transaction so a message whose uploads were taken is not written."""


def pending_message_row(
    row: Any, attachments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """One pending message as a plain dict, with its JSON columns decoded."""
    message = dict(row)
    message["skill_ids"] = load_json(message["skill_ids"])
    message["attachment_ids"] = load_json(message["attachment_ids"])
    message["attachments"] = list(attachments or [])
    return message


async def pending_attachments(conn: Any, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The uploads each message holds, without their bytes, in upload order."""
    if not message_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT pending_message_id, id, path, content_type,
               octet_length(content) AS size_bytes
        FROM agent_job_attachments
        WHERE pending_message_id = ANY($1::text[])
        ORDER BY id
        """,
        message_ids,
    )
    held: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        held.setdefault(str(row["pending_message_id"]), []).append(
            {
                "id": row["id"],
                "path": row["path"],
                "content_type": row["content_type"],
                "size_bytes": row["size_bytes"],
            }
        )
    return held


async def list_pending_messages(conn: Any, thread_id: str) -> list[dict[str, Any]]:
    """A thread's pending messages in the order they will be sent."""
    rows = await conn.fetch(
        f"SELECT {PENDING_MESSAGE_COLUMNS} FROM agent_pending_messages "
        "WHERE thread_id = $1 ORDER BY position",
        thread_id,
    )
    held = await pending_attachments(conn, [str(row["id"]) for row in rows])
    return [pending_message_row(row, held.get(str(row["id"]))) for row in rows]


async def read_pending_message(conn: Any, message_id: str) -> dict[str, Any] | None:
    """One pending message with its uploads, or ``None`` once it is gone."""
    row = await conn.fetchrow(
        f"SELECT {PENDING_MESSAGE_COLUMNS} FROM agent_pending_messages WHERE id = $1",
        message_id,
    )
    if row is None:
        return None
    held = await pending_attachments(conn, [message_id])
    return pending_message_row(row, held.get(message_id))
