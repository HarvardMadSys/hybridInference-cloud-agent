"""Postgres storage for durable memories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.agent_memory import (
    ACTIVE_LIMIT,
    ARCHIVE_RETENTION_DAYS,
    AgentMemoryConflict,
    AgentMemoryFull,
    AgentMemoryNotFound,
    AgentMemoryScopeView,
    AgentMemoryView,
    describe,
    memory_key,
    normalize_memory,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import asyncpg

_COLUMNS = (
    "id, repo, content, description, status, origin, source_job_ids, version, "
    "created_at, updated_at, last_used_at, archived_at"
)


def _memory(row: Mapping[str, Any]) -> AgentMemoryView:
    return AgentMemoryView.model_validate(
        {**dict(row), "source_job_ids": list(row["source_job_ids"])}
    )


def _view(row: Mapping[str, Any]) -> dict[str, Any]:
    return _memory(row).model_dump(mode="json")


async def _lock(conn: Any, user_id: str) -> None:
    """Serialize one owner's memory writes, including limit checks."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('agent-memory:' || $1, 0))", user_id
    )


async def _enabled(conn: Any, user_id: str, repo: str | None) -> bool:
    """Read the scope's existing switch; memory is on until its owner turns it off."""
    if repo is None:
        enabled = await conn.fetchval(
            "SELECT enabled FROM agent_user_memories WHERE user_id = $1", user_id
        )
    else:
        enabled = await conn.fetchval(
            "SELECT enabled FROM agent_project_memories WHERE user_id = $1 AND repo = $2",
            user_id,
            repo,
        )
    return enabled is not False


async def _active(conn: Any, user_id: str, repo: str | None) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"SELECT {_COLUMNS} FROM agent_memories "
        "WHERE user_id = $1 AND repo IS NOT DISTINCT FROM $2 AND status = 'active' "
        "ORDER BY created_at DESC, id",
        user_id,
        repo,
    )


async def _make_room(conn: Any, user_id: str, repo: str | None, active: int) -> bool:
    """Archive the least recently used automatic memory when the scope is full.

    Memories the owner wrote or edited are never archived to make room.
    """
    if active < ACTIVE_LIMIT:
        return True
    archived = await conn.fetchval(
        """
        UPDATE agent_memories
        SET status = 'archived', archived_at = clock_timestamp(),
            updated_at = clock_timestamp(), version = version + 1
        WHERE id = (
            SELECT id FROM agent_memories
            WHERE user_id = $1 AND repo IS NOT DISTINCT FROM $2
              AND status = 'active' AND origin <> 'manual'
            ORDER BY COALESCE(last_used_at, updated_at), created_at, id
            LIMIT 1
        )
        RETURNING id
        """,
        user_id,
        repo,
    )
    return archived is not None


async def _insert(
    conn: Any,
    user_id: str,
    repo: str | None,
    content: str,
    *,
    origin: str,
    source_job_ids: list[str],
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO agent_memories
            (id, user_id, repo, content, description, origin, source_job_ids)
        VALUES ($1, $2, $3, $4, $5, $6, $7::text[])
        RETURNING {_COLUMNS}
        """,
        uuid4().hex,
        user_id,
        repo,
        content,
        describe(content),
        origin,
        source_job_ids,
    )


class PostgresAgentMemoryRepository:
    """One row per memory; every write holds its owner's lock."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the existing pool."""
        self._pool = pool

    async def list_memories(self, user_id: str, repo: str | None) -> dict[str, Any]:
        """Read one scope's active and archived memories and its enabled setting."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM agent_memories "
                "WHERE user_id = $1 AND repo IS NOT DISTINCT FROM $2 "
                "ORDER BY archived_at DESC NULLS FIRST, created_at DESC, id",
                user_id,
                repo,
            )
            enabled = await _enabled(conn, user_id, repo)
        memories = [_memory(row) for row in rows]
        return AgentMemoryScopeView(
            repo=repo,
            enabled=enabled,
            memories=[memory for memory in memories if memory.status == "active"],
            archived=[memory for memory in memories if memory.status == "archived"],
        ).model_dump(mode="json")

    async def add_memory(self, user_id: str, repo: str | None, content: str) -> dict[str, Any]:
        """Save a memory the owner wrote; an identical active memory is reused."""
        content = normalize_memory(content)
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            active = await _active(conn, user_id, repo)
            for row in active:
                if memory_key(row["content"]) == memory_key(content):
                    return _view(row)
            if not await _make_room(conn, user_id, repo, len(active)):
                raise AgentMemoryFull("This scope already holds its limit of saved memories.")
            row = await _insert(conn, user_id, repo, content, origin="manual", source_job_ids=[])
        return _view(row)

    async def _owned_memory(self, conn: Any, user_id: str, memory_id: str) -> asyncpg.Record:
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM agent_memories WHERE id = $1 AND user_id = $2 FOR UPDATE",
            memory_id,
            user_id,
        )
        if row is None:
            raise AgentMemoryNotFound("Memory not found.")
        return row

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str,
        repo: str | None,
        expected_version: int,
    ) -> dict[str, Any]:
        """Rewrite or move an active memory; the owner's edit makes it manual."""
        content = normalize_memory(content)
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            row = await self._owned_memory(conn, user_id, memory_id)
            if row["status"] != "active":
                raise AgentMemoryNotFound("Memory not found.")
            if row["version"] != expected_version:
                raise AgentMemoryConflict("Memory changed. Refresh before saving.")
            if repo != row["repo"]:
                active = await _active(conn, user_id, repo)
                if not await _make_room(conn, user_id, repo, len(active)):
                    raise AgentMemoryFull("This scope already holds its limit of saved memories.")
            row = await conn.fetchrow(
                f"""
                UPDATE agent_memories
                SET content = $2, description = $3, repo = $4, origin = 'manual',
                    version = version + 1, updated_at = clock_timestamp()
                WHERE id = $1
                RETURNING {_COLUMNS}
                """,
                memory_id,
                content,
                describe(content),
                repo,
            )
        return _view(row)

    async def delete_memory(self, user_id: str, memory_id: str, *, expected_version: int) -> None:
        """Delete one memory permanently at the version the owner examined."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            row = await self._owned_memory(conn, user_id, memory_id)
            if row["version"] != expected_version:
                raise AgentMemoryConflict("Memory changed. Refresh before removing it.")
            await conn.execute("DELETE FROM agent_memories WHERE id = $1", memory_id)

    async def restore_memory(
        self, user_id: str, memory_id: str, *, expected_version: int
    ) -> dict[str, Any]:
        """Make an archived memory active again, within its scope's limit."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            row = await self._owned_memory(conn, user_id, memory_id)
            if row["status"] != "archived":
                raise AgentMemoryNotFound("Memory not found.")
            if row["version"] != expected_version:
                raise AgentMemoryConflict("Memory changed. Refresh before restoring it.")
            active = await _active(conn, user_id, row["repo"])
            if not await _make_room(conn, user_id, row["repo"], len(active)):
                raise AgentMemoryFull("This scope already holds its limit of saved memories.")
            row = await conn.fetchrow(
                f"""
                UPDATE agent_memories
                SET status = 'active', archived_at = NULL, superseded_by = NULL,
                    version = version + 1, updated_at = clock_timestamp()
                WHERE id = $1
                RETURNING {_COLUMNS}
                """,
                memory_id,
            )
        return _view(row)

    async def clear_memories(self, user_id: str, repo: str | None) -> None:
        """Delete every memory in one scope, archived ones included."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            await conn.execute(
                "DELETE FROM agent_memories WHERE user_id = $1 AND repo IS NOT DISTINCT FROM $2",
                user_id,
                repo,
            )

    async def purge_memories(self) -> None:
        """Forget archives older than the retention period."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_memories WHERE status = 'archived' "
                "AND archived_at < clock_timestamp() - make_interval(days => $1)",
                ARCHIVE_RETENTION_DAYS,
            )
