"""Binding staged uploads to the turn that accepted them."""

from __future__ import annotations

from typing import Any

from cloud_agent.attachments.models import (
    AttachmentError,
    check_paths,
    check_thread_total,
    current_uploads,
)

#: A conversation's uploads in the order its turns attached them, without the
#: bytes: the order :func:`current_uploads` reads as "later replaces earlier".
#: Upload ids alone are not that order — a file staged long ago can be attached
#: by the newest turn.
THREAD_UPLOADS_SQL = """
    SELECT u.id, u.user_id, u.job_id, u.thread_id, u.path, u.content_type,
           octet_length(u.content) AS size_bytes, u.created_at
    FROM agent_job_attachments u
    JOIN agent_jobs turn ON turn.id = u.job_id
    WHERE u.thread_id = $1
    ORDER BY turn.turn_no, u.id
"""


async def claim_attachments(
    conn: Any,
    *,
    user_id: str,
    job_id: str,
    thread_id: str,
    attachments: list[dict[str, Any]] | None,
    pending_message_id: str | None = None,
) -> None:
    """Bind a turn's uploads inside the transaction that created it.

    In the same transaction on purpose. Resolving the ids and creating the job
    are two steps, and between them a second submission by the same owner can
    take the same staged files. Claiming here means either the job and its
    files are both committed or neither is — the alternative leaves a job whose
    prompt names files the sandbox will never contain.

    An upload a waiting message holds is that message's alone: only the turn
    it becomes (``pending_message_id``) may claim it, the same rule pinning
    applies. Anything else — a new task, an idle-only turn — would leave the
    message naming a file its own turn can no longer have.

    The host rebuilds ``/uploads`` from the whole conversation, so the paths
    and the total are checked against it here, under the lock that orders the
    conversation's turns: a path an earlier turn attached is replaced, but one
    that would be a file and a folder at once is refused (see
    :func:`check_paths`), and so is a turn that would take the conversation
    past what the host imports (see :func:`check_thread_total`).
    """
    ids = [int(item["id"]) for item in attachments or []]
    if not ids:
        return
    claimed = await conn.fetchval(
        """
        WITH bound AS (
            UPDATE agent_job_attachments
               SET job_id = $3, thread_id = $4, claimed_at = NOW()
             WHERE user_id = $1 AND job_id IS NULL AND id = ANY($2::bigint[])
               AND (pending_message_id IS NULL OR pending_message_id = $5)
         RETURNING id
        )
        SELECT count(*) FROM bound
        """,
        user_id,
        ids,
        job_id,
        thread_id,
        pending_message_id,
    )
    if int(claimed or 0) != len(ids):
        raise AttachmentError("One or more uploads are no longer available. Attach them again.")
    thread = [dict(row) for row in await conn.fetch(THREAD_UPLOADS_SQL, thread_id)]
    adding = set(ids)
    check_paths(
        [str(row["path"]) for row in thread if row["id"] in adding],
        held=[
            str(row["path"])
            for row in current_uploads([row for row in thread if row["id"] not in adding])
        ],
    )
    check_thread_total(current_uploads(thread))
