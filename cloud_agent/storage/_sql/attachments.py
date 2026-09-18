"""Binding staged uploads to the turn that accepted them."""

from __future__ import annotations

from typing import Any

from cloud_agent.attachments.models import AttachmentError


async def claim_attachments(
    conn: Any,
    *,
    user_id: str,
    job_id: str,
    thread_id: str,
    attachments: list[dict[str, Any]] | None,
) -> None:
    """Bind a turn's uploads inside the transaction that created it.

    In the same transaction on purpose. Resolving the ids and creating the job
    are two steps, and between them a second submission by the same owner can
    take the same staged files. Claiming here means either the job and its
    files are both committed or neither is — the alternative leaves a job whose
    prompt names files the sandbox will never contain.
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
         RETURNING id
        )
        SELECT count(*) FROM bound
        """,
        user_id,
        ids,
        job_id,
        thread_id,
    )
    if int(claimed or 0) != len(ids):
        raise AttachmentError("One or more uploads are no longer available. Attach them again.")
