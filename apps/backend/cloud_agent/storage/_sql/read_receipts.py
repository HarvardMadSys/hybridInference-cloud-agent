"""Owner-scoped read receipts that only advance through completed attempts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


async def merge_read_receipts(
    conn: asyncpg.Connection, *, user_id: str, receipts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge a bounded import, deriving thread and turn identities from owned jobs.

    Missing, deleted, foreign and unfinished completions are ignored so one
    obsolete browser receipt cannot prevent migration of the remaining history.
    The conflict condition is evaluated under the row lock: an old device or
    a retried request cannot move a newer receipt backwards. No thread or job
    is updated, so reading never reorders the sidebar or produces activity.
    """
    rows = await conn.fetch(
        """
        WITH incoming AS (
            SELECT DISTINCT ON (j.thread_id)
                   j.thread_id, j.id AS job_id, j.turn_no, input.attempt_id
            FROM jsonb_to_recordset($2::jsonb) AS input(job_id TEXT, attempt_id BIGINT)
            JOIN agent_jobs j ON j.id = input.job_id AND j.user_id = $1
            JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = $1
            LEFT JOIN agent_attempts a ON a.job_id = j.id AND a.id = input.attempt_id
            WHERE t.deleted_at IS NULL
              AND (
                  (input.attempt_id IS NULL AND j.current_attempt_id IS NULL
                   AND j.state IN ('succeeded', 'failed', 'cancelled'))
                  OR (a.status <> 'running'
                      AND (a.id IS DISTINCT FROM j.current_attempt_id
                           OR j.state IN ('succeeded', 'failed', 'cancelled')))
              )
            ORDER BY j.thread_id, j.turn_no DESC, input.attempt_id DESC NULLS LAST
        )
        INSERT INTO agent_conversation_read_receipts AS saved
            (user_id, thread_id, job_id, turn_no, attempt_id)
        SELECT $1, thread_id, job_id, turn_no, attempt_id FROM incoming ORDER BY thread_id
        ON CONFLICT (user_id, thread_id) DO UPDATE
        SET job_id = EXCLUDED.job_id, turn_no = EXCLUDED.turn_no,
            attempt_id = EXCLUDED.attempt_id, revision = EXCLUDED.revision
        WHERE (saved.turn_no, COALESCE(saved.attempt_id, 0))
            < (EXCLUDED.turn_no, COALESCE(EXCLUDED.attempt_id, 0))
        RETURNING thread_id, job_id, turn_no, attempt_id
        """,
        user_id,
        json.dumps(receipts),
    )
    return [dict(row) for row in rows]
