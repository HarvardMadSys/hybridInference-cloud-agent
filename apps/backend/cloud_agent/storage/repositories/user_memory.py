"""Postgres materialization of user memory from owned user-authored messages."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.user_memory import build_user_memory

if TYPE_CHECKING:
    from collections.abc import Mapping

    import asyncpg


logger = get_logger(__name__)


def _memory_json(value: Any) -> Any:
    """Decode asyncpg's default JSON representation."""
    return json.loads(value) if isinstance(value, str) else value


def _public_memory(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep ownership, input fingerprints, and reset fences internal."""
    return {
        "enabled": bool(row["enabled"]),
        "portrait": _memory_json(row["portrait"]),
        "memories": _memory_json(row["memories"]),
        "suggestions": _memory_json(row["suggestions"]),
        "revision": int(row["revision"]),
        "updated_at": row["updated_at"],
    }


def _memory_activity_digest(activity: list[dict[str, Any]]) -> str:
    """Detect edits, deletions and relevant context changes, including long text."""
    encoded = json.dumps(activity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_ACTIVE_MEMORY_USERS = """
    SELECT candidates.user_id
    FROM (
        SELECT DISTINCT t.user_id
        FROM agent_threads t
        JOIN agent_jobs j ON j.thread_id = t.id AND j.user_id = t.user_id
        JOIN agent_thread_messages m ON m.job_id = j.id AND m.thread_id = t.id
        WHERE t.deleted_at IS NULL AND m.role = 'user' AND j.fork_source_job_id IS NULL
          AND (j.turn_source <> 'goal' OR m.mid_turn)
        UNION
        SELECT user_id FROM agent_user_memories WHERE enabled
    ) candidates
    LEFT JOIN agent_user_memories p ON p.user_id = candidates.user_id
    WHERE COALESCE(p.enabled, TRUE)
"""


class PostgresUserMemoryRepository:
    """Serialize refresh, privacy controls and reset fences per owner."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the existing pool and keep a fair background-batch cursor."""
        self._pool = pool
        self._active_memory_cursor: str | None = None
        self._active_memory_batch_lock = asyncio.Lock()

    @staticmethod
    async def _user_memory_lock(conn: Any, user_id: str) -> None:
        """Lock even before this user's first profile row exists."""
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-user-memory:' || $1, 0))",
            user_id,
        )

    @staticmethod
    async def _ensure_user_memory(conn: Any, user_id: str) -> dict[str, Any]:
        """Initialize a schema-valid empty profile while holding the owner lock."""
        row = await conn.fetchrow("SELECT * FROM agent_user_memories WHERE user_id = $1", user_id)
        if row is not None:
            return dict(row)
        empty = build_user_memory([], None)
        row = await conn.fetchrow(
            """
            INSERT INTO agent_user_memories (user_id, portrait, memories, suggestions)
            VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb) RETURNING *
            """,
            user_id,
            json.dumps(empty["portrait"]),
            json.dumps(empty["memories"]),
            json.dumps(empty["suggestions"]),
        )
        return dict(row)

    @staticmethod
    async def _refresh_user_memory_locked(
        conn: Any, user_id: str, current: dict[str, Any]
    ) -> dict[str, Any]:
        """Rebuild once when visible input changes, inside the privacy-control lock."""
        if not current["enabled"]:
            return _public_memory(current)
        rows = await conn.fetch(
            """
            SELECT m.id, m.id AS message_id, m.job_id, left(m.content, 8000) AS content,
                   md5(m.content) AS content_digest, t.repo, j.runtime, j.model,
                   t.title, m.created_at
            FROM agent_thread_messages m
            JOIN agent_jobs j ON j.id = m.job_id AND j.thread_id = m.thread_id
            JOIN agent_threads t ON t.id = m.thread_id AND t.user_id = j.user_id
            WHERE j.user_id = $1 AND t.user_id = $1 AND t.deleted_at IS NULL
              AND m.role = 'user' AND j.fork_source_job_id IS NULL
              AND (j.turn_source <> 'goal' OR m.mid_turn)
              AND m.id > $2
              AND ($3::timestamptz IS NULL OR m.created_at > $3)
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 50
            """,
            user_id,
            current["cleared_message_id"],
            current["cleared_at"],
        )
        activity = [dict(row) for row in rows]
        digest = _memory_activity_digest(activity)
        if digest == current["activity_digest"]:
            return _public_memory(current)
        # The full-content hash tracks edits without handing unbounded text to
        # the builder or storing it in the profile document.
        for item in activity:
            item.pop("content_digest")
        profile = build_user_memory(activity, _public_memory(current))
        updated = await conn.fetchrow(
            """
            UPDATE agent_user_memories
            SET portrait = $2::jsonb, memories = $3::jsonb, suggestions = $4::jsonb,
                activity_digest = $5, revision = revision + 1, updated_at = clock_timestamp()
            WHERE user_id = $1 RETURNING *
            """,
            user_id,
            json.dumps(profile["portrait"]),
            json.dumps(profile["memories"]),
            json.dumps(profile["suggestions"]),
            digest,
        )
        return _public_memory(updated)

    async def refresh(self, user_id: str) -> dict[str, Any]:
        """Materialize only this owner's current, eligible activity."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_user_memory(conn, user_id)
            return await self._refresh_user_memory_locked(conn, user_id, current)

    async def get(self, user_id: str) -> dict[str, Any] | None:
        """Read the saved profile without generating or exposing internal fields."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_user_memories WHERE user_id = $1", user_id
            )
        return _public_memory(row) if row is not None else None

    async def set_enabled(self, user_id: str, enabled: bool) -> dict[str, Any]:
        """Change generation and use while serializing against in-flight refreshes."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_user_memory(conn, user_id)
            if current["enabled"] == enabled:
                return (
                    await self._refresh_user_memory_locked(conn, user_id, current)
                    if enabled
                    else _public_memory(current)
                )
            row = await conn.fetchrow(
                """
                UPDATE agent_user_memories
                SET enabled = $2, revision = revision + 1, updated_at = clock_timestamp()
                WHERE user_id = $1 RETURNING *
                """,
                user_id,
                enabled,
            )
            if enabled:
                return await self._refresh_user_memory_locked(conn, user_id, dict(row))
        return _public_memory(row)

    async def clear(self, user_id: str) -> dict[str, Any]:
        """Empty all derived knowledge and prevent prior messages from reappearing."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_user_memory(conn, user_id)
            empty = build_user_memory([], _public_memory(current))
            row = await conn.fetchrow(
                """
                UPDATE agent_user_memories
                SET portrait = $2::jsonb, memories = $3::jsonb, suggestions = $4::jsonb,
                    activity_digest = $5,
                    cleared_message_id = (
                        SELECT COALESCE(max(m.id), 0)
                        FROM agent_thread_messages m
                        JOIN agent_jobs j ON j.id = m.job_id AND j.thread_id = m.thread_id
                        JOIN agent_threads t ON t.id = m.thread_id AND t.user_id = j.user_id
                        WHERE j.user_id = $1 AND t.user_id = $1 AND m.role = 'user'
                    ),
                    cleared_at = clock_timestamp(), revision = revision + 1,
                    updated_at = clock_timestamp()
                WHERE user_id = $1 RETURNING *
                """,
                user_id,
                json.dumps(empty["portrait"]),
                json.dumps(empty["memories"]),
                json.dumps(empty["suggestions"]),
                _memory_activity_digest([]),
            )
        return _public_memory(row)

    async def refresh_active_users(self, limit: int = 100) -> int:
        """Visit enabled owners fairly, including profiles whose history was deleted."""
        limit = min(max(limit, 0), 1000)
        if not limit:
            return 0
        async with self._active_memory_batch_lock:
            cursor = self._active_memory_cursor
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    _ACTIVE_MEMORY_USERS
                    + " AND ($1::text IS NULL OR candidates.user_id > $1)"
                    + " ORDER BY candidates.user_id LIMIT $2",
                    cursor,
                    limit,
                )
                if cursor is not None and len(rows) < limit:
                    rows += await conn.fetch(
                        _ACTIVE_MEMORY_USERS
                        + " AND candidates.user_id <= $1"
                        + " ORDER BY candidates.user_id LIMIT $2",
                        cursor,
                        limit - len(rows),
                    )
            for row in rows:
                user_id = str(row["user_id"])
                try:
                    await self.refresh(user_id)
                except Exception:
                    # One corrupt profile or transient owner failure must not
                    # starve later owners. Never log content or exception data.
                    logger.warning("agent_user_memory_owner_refresh_failed")
                finally:
                    self._active_memory_cursor = user_id
            return len(rows)
