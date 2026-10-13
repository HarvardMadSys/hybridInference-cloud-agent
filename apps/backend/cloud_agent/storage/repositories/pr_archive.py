"""Postgres merge-check leases and transactional session archival receipts."""

from __future__ import annotations

import math
import re
import secrets
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.pending_messages import QUEUE_PAUSED_ARCHIVED
from cloud_agent.storage._sql.events import insert_event
from cloud_agent.storage._sql.goals import CANCEL_QUEUED_GOAL_ROUNDS_SQL, PAUSE_GOALS_SQL
from cloud_agent.storage._sql.pending_messages import PAUSE_QUEUE_SQL

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


# A fork copies its source's display publication, but it did not contribute
# that job to the published snapshot. Original jobs retain legacy publisher
# mappings too; copied jobs must have their own snapshot association.
_PUBLISHED_JOB_SQL = """
j.state = 'succeeded'
AND NULLIF(j.published_pr_url, '') IS NOT NULL
AND j.current_attempt_id IS NOT NULL
AND (
    j.fork_source_job_id IS NULL OR EXISTS (
        SELECT 1 FROM agent_snapshot_jobs included
        JOIN agent_sandbox_snapshots snapshot ON snapshot.id = included.snapshot_id
        WHERE included.job_id = j.id AND snapshot.state = 'published'
          AND snapshot.published_pr_url = j.published_pr_url
    )
)
"""


class PostgresPrArchiveRepository:
    """Merge obligations over the same pool as session and publication commands."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def claim_pr_archive_checks(
        self, *, limit: int = 50, lease_seconds: float = 120.0
    ) -> list[dict[str, Any]]:
        """Claim due checks for live sessions whose latest turn was published.

        Original publication or the job's own published snapshot membership
        counts; copied fork history does not. Each result contains thread_id,
        job_id, user_id, repo, pr_url,
        claim_token and failures. Expired claims can be claimed again, and a
        completed session/PR pair is never reseeded after a manual restore.
        """
        if limit < 1:
            return []
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        claimed = []
        async with self._pool.acquire() as conn, conn.transaction():
            # Bound discovery as well as claiming. A receipt is durable even
            # when the session is restored or later turns use the same PR.
            await conn.execute(
                f"""
                INSERT INTO agent_pr_archive_checks (thread_id, job_id, pr_url)
                SELECT t.id, j.id, j.published_pr_url
                FROM agent_threads t
                JOIN LATERAL (
                    SELECT * FROM agent_jobs latest
                    WHERE latest.thread_id = t.id AND latest.user_id = t.user_id
                    ORDER BY latest.turn_no DESC LIMIT 1
                ) j ON TRUE
                WHERE t.archived_at IS NULL AND t.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_pending_messages waiting WHERE waiting.thread_id = t.id
                  )
                  AND {_PUBLISHED_JOB_SQL}
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_pr_archive_checks receipt
                      WHERE receipt.thread_id = t.id
                        AND receipt.pr_url = j.published_pr_url
                  )
                ORDER BY j.updated_at, t.id
                LIMIT $1
                ON CONFLICT (thread_id, pr_url) DO NOTHING
                """,
                limit,
            )
            rows = await conn.fetch(
                f"""
                SELECT checks.thread_id, j.id AS job_id, t.user_id, t.repo,
                       checks.pr_url, checks.failures
                FROM agent_pr_archive_checks checks
                JOIN agent_threads t ON t.id = checks.thread_id
                JOIN LATERAL (
                    SELECT * FROM agent_jobs latest
                    WHERE latest.thread_id = t.id AND latest.user_id = t.user_id
                    ORDER BY latest.turn_no DESC LIMIT 1
                ) j ON TRUE
                WHERE checks.state = 'pending'
                  AND checks.next_check_at <= NOW()
                  AND (checks.lease_expires_at IS NULL
                       OR checks.lease_expires_at <= clock_timestamp())
                  AND t.archived_at IS NULL AND t.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_pending_messages waiting WHERE waiting.thread_id = t.id
                  )
                  AND j.published_pr_url = checks.pr_url
                  AND {_PUBLISHED_JOB_SQL}
                ORDER BY checks.next_check_at, checks.thread_id
                LIMIT $1
                FOR UPDATE OF checks SKIP LOCKED
                """,
                limit,
            )
            for row in rows:
                token = secrets.token_urlsafe(24)
                await conn.execute(
                    """
                    UPDATE agent_pr_archive_checks
                    SET job_id = $3, claim_token = $4,
                        lease_expires_at = clock_timestamp() + make_interval(secs => $5),
                        updated_at = NOW()
                    WHERE thread_id = $1 AND pr_url = $2
                    """,
                    row["thread_id"],
                    row["pr_url"],
                    row["job_id"],
                    token,
                    lease_seconds,
                )
                claimed.append({**dict(row), "claim_token": token})
        return claimed

    async def reschedule_pr_archive_check(
        self,
        *,
        thread_id: str,
        pr_url: str,
        claim_token: str,
        delay_s: float,
        error_code: str | None = None,
        terminal: bool = False,
    ) -> bool:
        """Release a live claim for retry, or record a terminal skipped check.

        Errors are safe lowercase identifiers, never provider response text.
        A successful check resets consecutive failures. Expired or superseded
        claim tokens cannot change the durable obligation.
        """
        if not math.isfinite(delay_s) or delay_s < 0:
            raise ValueError("delay_s must be finite and nonnegative")
        if error_code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code) is None:
            raise ValueError("error_code must be a safe lowercase identifier")
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await conn.fetchval(
                "SELECT thread_id FROM agent_pr_archive_checks "
                "WHERE thread_id = $1 AND pr_url = $2 AND claim_token = $3 "
                "AND state = 'pending' FOR UPDATE",
                thread_id,
                pr_url,
                claim_token,
            )
            if locked is None:
                return False
            result = await conn.fetchval(
                """
                UPDATE agent_pr_archive_checks
                SET state = CASE WHEN $6 THEN 'skipped' ELSE 'pending' END,
                    claim_token = NULL, lease_expires_at = NULL,
                    next_check_at = clock_timestamp() + make_interval(secs => $4),
                    failures = CASE WHEN $5::text IS NULL THEN 0 ELSE failures + 1 END,
                    last_error = $5,
                    completed_at = CASE WHEN $6 THEN NOW() ELSE NULL END,
                    updated_at = NOW()
                WHERE thread_id = $1 AND pr_url = $2 AND claim_token = $3
                  AND state = 'pending' AND lease_expires_at > clock_timestamp()
                RETURNING thread_id
                """,
                thread_id,
                pr_url,
                claim_token,
                delay_s,
                error_code,
                terminal,
            )
        return result is not None

    async def complete_pr_merge_archive(
        self,
        *,
        thread_id: str,
        job_id: str,
        user_id: str,
        pr_url: str,
        claim_token: str,
        merged_at: datetime,
        merge_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Archive the still-current publication and its receipt atomically.

        Recheck ownership, latest turn, publication mapping and the live claim
        under the session lock. Pause goals and pending messages while keeping
        history and shared workspaces. Return the archive receipt, or None for
        a stale claim, deleted session, waiting owner work or changed publication.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Follow-up submission and native-fork materialization take this
            # lock too: no new tip can be appended after the latest-turn check.
            thread = await conn.fetchrow(
                "SELECT id FROM agent_threads "
                "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
                thread_id,
                user_id,
            )
            if thread is None:
                return None
            # Read after acquiring the lock. A message enqueued while this
            # transaction waited must be visible in this statement's snapshot.
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agent_pending_messages WHERE thread_id = $1)",
                thread_id,
            ):
                return None
            check = await conn.fetchval(
                "SELECT job_id FROM agent_pr_archive_checks "
                "WHERE thread_id = $1 AND pr_url = $2 AND claim_token = $3 "
                "AND state = 'pending' FOR UPDATE",
                thread_id,
                pr_url,
                claim_token,
            )
            if check != job_id or not await conn.fetchval(
                "SELECT lease_expires_at > clock_timestamp() "
                "FROM agent_pr_archive_checks WHERE thread_id = $1 AND pr_url = $2",
                thread_id,
                pr_url,
            ):
                return None
            # Lock the job as well: a publisher cannot replace its recorded
            # association while this transaction commits an archive receipt.
            job = await conn.fetchrow(
                f"""
                SELECT j.id, j.current_attempt_id FROM agent_jobs j
                WHERE j.id = $1 AND j.thread_id = $2 AND j.user_id = $3
                  AND j.published_pr_url = $4 AND {_PUBLISHED_JOB_SQL}
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_jobs newer
                      WHERE newer.thread_id = j.thread_id AND newer.turn_no > j.turn_no
                  )
                FOR UPDATE OF j
                """,
                job_id,
                thread_id,
                user_id,
                pr_url,
            )
            if job is None:
                return None
            archived_at = await conn.fetchval(
                "UPDATE agent_threads SET archived_at = COALESCE(archived_at, NOW()) "
                "WHERE id = $1 RETURNING archived_at",
                thread_id,
            )
            await conn.execute(
                PAUSE_GOALS_SQL,
                [thread_id],
                "archived",
                "the conversation was archived; resume the goal to continue it",
            )
            await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
            await conn.execute(PAUSE_QUEUE_SQL, [thread_id], QUEUE_PAUSED_ARCHIVED)
            await conn.execute(
                """
                UPDATE agent_pr_archive_checks
                SET state = 'archived', claim_token = NULL, lease_expires_at = NULL,
                    merged_at = $3, merge_sha = $4, archived_at = $5,
                    completed_at = NOW(), last_error = NULL, failures = 0,
                    updated_at = NOW()
                WHERE thread_id = $1 AND pr_url = $2
                """,
                thread_id,
                pr_url,
                merged_at,
                merge_sha,
                archived_at,
            )
            await insert_event(
                conn,
                job_id=job_id,
                attempt_id=job["current_attempt_id"],
                event_type="lifecycle",
                payload={
                    "phase": "session_archived_pr_merged",
                    "thread_id": thread_id,
                    "pr_url": pr_url,
                    "merged_at": merged_at.isoformat(),
                    "merge_sha": merge_sha,
                    "archived_at": archived_at.isoformat(),
                },
            )
        return {
            "thread_id": thread_id,
            "job_id": job_id,
            "pr_url": pr_url,
            "archived_at": archived_at,
            "merged_at": merged_at,
        }
