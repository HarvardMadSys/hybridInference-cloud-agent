"""Postgres persistence for per-task uploaded files."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.attachments.models import (
    AttachmentError,
    AttachmentLeaseLost,
    attachment_max_file_bytes,
    attachment_max_files,
    attachment_max_total_bytes,
    attachment_size_label,
)

if TYPE_CHECKING:
    import asyncpg

#: Columns that describe an upload without carrying its bytes. Reads that only
#: need the manifest must never pull `content` — a listing of fifty files would
#: otherwise move 20 MB to answer a question about names and sizes.
_META = (
    "id, user_id, job_id, thread_id, path, content_type, "
    "octet_length(content) AS size_bytes, created_at"
)

# A LEFT JOIN below retains one authorization sentinel for a live attempt
# whose conversation has no uploads (or does not contain the requested id).
# Keeping the fence and the bytes in one statement prevents a stale worker
# from racing a separate authorization lookup and reading a later snapshot.
_WORKER_FENCE = """
    FROM agent_jobs j
    JOIN agent_attempts a ON a.id = j.current_attempt_id AND a.job_id = j.id
    JOIN agent_runner_hosts h ON h.host = a.host_id
    LEFT JOIN agent_job_attachments u ON u.thread_id = j.thread_id
"""
_WORKER_AUTHORIZATION = """
    WHERE j.id = $1 AND a.id = $2 AND a.lease_generation = $3
      AND j.state = 'running' AND a.status = 'running'
      AND a.lease_expires_at > clock_timestamp()
      AND h.credential_hash IS NOT NULL AND h.status <> 'disabled'
"""


class PostgresAttachmentRepository:
    """Serialize one owner's staging quota and bind uploads to their turn."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the control plane's connection pool."""
        self._pool = pool

    @staticmethod
    async def _lock_uploads(conn: Any, user_id: str) -> None:
        """Serialize quota checks, including a user's first upload."""
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-uploads:' || $1, 0))",
            user_id,
        )

    async def create_attachment(
        self, *, user_id: str, path: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """Stage one upload before any job exists, enforcing the owner's quota."""
        if not content:
            raise AttachmentError("An uploaded file cannot be empty.")
        max_file_bytes = attachment_max_file_bytes()
        max_files = attachment_max_files()
        max_total_bytes = attachment_max_total_bytes()
        if len(content) > max_file_bytes:
            raise AttachmentError(
                f"Each file must be {attachment_size_label(max_file_bytes)} or smaller."
            )
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_uploads(conn, user_id)
            # The quota counts what is staged, not what is attached: a claimed
            # upload belongs to a turn that already exists and is bounded by
            # that turn's own selection check.
            staged = await conn.fetchrow(
                "SELECT count(*) AS files, COALESCE(sum(octet_length(content)), 0) AS bytes "
                "FROM agent_job_attachments WHERE user_id = $1 AND job_id IS NULL",
                user_id,
            )
            if int(staged["files"]) >= max_files:
                raise AttachmentError(f"Attach at most {max_files} files.")
            if int(staged["bytes"]) + len(content) > max_total_bytes:
                raise AttachmentError(
                    "Attached files must total no more than "
                    f"{attachment_size_label(max_total_bytes)}."
                )
            row = await conn.fetchrow(
                f"""
                INSERT INTO agent_job_attachments (user_id, path, content, content_type)
                VALUES ($1, $2, $3, $4)
                RETURNING {_META}
                """,
                user_id,
                path,
                content,
                content_type,
            )
        return dict(row)

    async def select_attachments(
        self, *, user_id: str, attachment_ids: list[int]
    ) -> list[dict[str, Any]]:
        """Read metadata for owned uploads that no job has claimed yet."""
        if not attachment_ids:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_META} FROM agent_job_attachments "
                "WHERE user_id = $1 AND job_id IS NULL AND id = ANY($2::bigint[])",
                user_id,
                attachment_ids,
            )
        return [dict(row) for row in rows]

    async def delete_attachment(self, *, user_id: str, attachment_id: int) -> bool:
        """Drop a staged upload the owner removed before running.

        Not one a waiting message holds: that message names the file and will
        be sent with it, so removing the bytes here would send a turn that
        mentions a file its sandbox never receives. Editing or deleting the
        message is what lets go of it.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_job_attachments "
                "WHERE id = $1 AND user_id = $2 AND job_id IS NULL "
                "AND pending_message_id IS NULL",
                attachment_id,
                user_id,
            )
        return result.endswith(" 1")

    async def count_thread_attachments(self, *, thread_id: str) -> int:
        """How many uploads a conversation holds, for the claim's cheap signal."""
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(*) FROM agent_job_attachments WHERE thread_id = $1",
                thread_id,
            )
        return int(total or 0)

    async def list_thread_attachments(self, *, thread_id: str) -> list[dict[str, Any]]:
        """Every upload in a conversation, so a fresh sandbox can be rebuilt."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_META} FROM agent_job_attachments WHERE thread_id = $1 ORDER BY id",
                thread_id,
            )
        return [dict(row) for row in rows]

    async def read_attachment_content(
        self, *, thread_id: str, attachment_id: int
    ) -> tuple[bytes, str] | None:
        """Read one claimed upload's bytes for a trusted thread-scoped caller."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content, content_type FROM agent_job_attachments "
                "WHERE id = $1 AND thread_id = $2",
                attachment_id,
                thread_id,
            )
        return (bytes(row["content"]), str(row["content_type"])) if row else None

    async def list_worker_attachments(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> list[dict[str, Any]]:
        """Read metadata under the current live attempt and enrolled-host fence.

        Raise ``AttachmentLeaseLost`` if the fence is no longer authorized.
        Authorization and the upload read must share one database snapshot.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.id, u.user_id, u.job_id, u.thread_id, u.path, u.content_type, "
                "octet_length(u.content) AS size_bytes, u.created_at "
                + _WORKER_FENCE
                + _WORKER_AUTHORIZATION
                + " ORDER BY u.id",
                job_id,
                attempt_id,
                lease_generation,
            )
        if not rows:
            raise AttachmentLeaseLost
        return [dict(row) for row in rows if row["id"] is not None]

    async def read_worker_attachment(
        self, *, job_id: str, attempt_id: int, lease_generation: int, attachment_id: int
    ) -> tuple[str, bytes, str] | None:
        """Read path, bytes and type under the same fence as the worker listing.

        Raise ``AttachmentLeaseLost`` for a lost fence; return ``None`` for an
        upload outside the authorized conversation, including a missing id.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT u.id, u.path, u.content, u.content_type "
                + _WORKER_FENCE
                + " AND u.id = $4 "
                + _WORKER_AUTHORIZATION,
                job_id,
                attempt_id,
                lease_generation,
                attachment_id,
            )
        if row is None:
            raise AttachmentLeaseLost
        if row["id"] is None:
            return None
        return str(row["path"]), bytes(row["content"]), str(row["content_type"])

    async def purge_stale_staged(self, *, older_than_hours: int) -> int:
        """Collect uploads no job ever claimed and no waiting message holds.

        A message can wait behind a long turn for longer than a staged upload
        is kept. Its files are not abandoned while it waits; they are released
        back to this sweep when the message is edited to drop them or deleted.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_job_attachments WHERE job_id IS NULL "
                "AND pending_message_id IS NULL "
                f"AND created_at < NOW() - INTERVAL '{int(older_than_hours)} hours'",
            )
        return int(result.rsplit(" ", 1)[-1] or 0)
