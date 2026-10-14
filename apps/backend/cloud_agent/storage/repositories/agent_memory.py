"""Postgres storage for durable memories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.agent_memory import (
    ACTIVE_LIMIT,
    ARCHIVE_RETENTION_DAYS,
    AgentMemoryConflict,
    AgentMemoryDisabled,
    AgentMemoryFull,
    AgentMemoryNotFound,
    AgentMemoryScopeView,
    AgentMemoryTaskEnded,
    AgentMemoryView,
    describe,
    memory_key,
    normalize_memory,
)
from cloud_agent.storage._sql.hosts import LIVE_ATTEMPT_HOST

if TYPE_CHECKING:
    from collections.abc import Mapping

    import asyncpg

    from cloud_agent.agent_memory import MemoryTask

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
    """Read the scope's switch; memory is on until its owner turns it off."""
    enabled = await conn.fetchval(
        "SELECT enabled FROM agent_memory_settings WHERE user_id = $1 AND repo = $2",
        user_id,
        repo or "",
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


async def _live_task_repo(conn: Any, task: MemoryTask) -> str | None:
    """The repository of a task whose attempt is still the running one.

    Checked inside the write's transaction, with the job and attempt rows
    share-locked, so a task stopped or superseded after its token was accepted
    cannot write: a stop that commits first is seen here, and one that comes
    later waits for this write. ``None`` is a chat without a repository.
    """
    row = await conn.fetchrow(
        f"""
        SELECT NULLIF(t.repo, '') AS repo
        FROM agent_attempts a
        JOIN agent_jobs j ON j.id = a.job_id
        JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
        WHERE a.id = $3
          AND a.lease_generation = $4
          AND a.status = 'running'
          AND a.lease_expires_at > clock_timestamp()
          AND j.id = $2
          AND j.user_id = $1
          AND j.current_attempt_id = a.id
          AND j.state = 'running'
          AND NOT j.cancel_requested
          AND t.deleted_at IS NULL
          AND {LIVE_ATTEMPT_HOST}
        FOR SHARE OF a, j
        """,
        task.user_id,
        task.job_id,
        task.attempt_id,
        task.lease_generation,
    )
    if row is None:
        raise AgentMemoryTaskEnded("This task is no longer running.")
    return row["repo"]


async def _add_source(conn: Any, memory_id: str, job_id: str) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        UPDATE agent_memories
        SET source_job_ids = CASE WHEN $2::text = ANY(source_job_ids) THEN source_job_ids
                                  ELSE array_append(source_job_ids, $2::text) END
        WHERE id = $1
        RETURNING {_COLUMNS}
        """,
        memory_id,
        job_id,
    )


async def _archive(conn: Any, memory_id: str, *, superseded_by: str | None) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        UPDATE agent_memories
        SET status = 'archived', archived_at = clock_timestamp(), superseded_by = $2,
            updated_at = clock_timestamp(), version = version + 1
        WHERE id = $1
        RETURNING {_COLUMNS}
        """,
        memory_id,
        superseded_by,
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
        """Save a memory the owner wrote; an identical active memory is reused.

        Reusing an agent's memory makes it the owner's, as an edit does, so it
        is never archived to make room.
        """
        content = normalize_memory(content)
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            active = await _active(conn, user_id, repo)
            for row in active:
                if memory_key(row["content"]) == memory_key(content):
                    if row["origin"] != "manual":
                        row = await conn.fetchrow(
                            f"""
                            UPDATE agent_memories
                            SET origin = 'manual', version = version + 1,
                                updated_at = clock_timestamp()
                            WHERE id = $1
                            RETURNING {_COLUMNS}
                            """,
                            row["id"],
                        )
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

    async def set_memory_enabled(
        self, user_id: str, repo: str | None, enabled: bool
    ) -> dict[str, Any]:
        """Turn one scope's memory on or off; its memories are kept either way."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            await conn.execute(
                """
                INSERT INTO agent_memory_settings (user_id, repo, enabled)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, repo)
                DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = clock_timestamp()
                """,
                user_id,
                repo or "",
                enabled,
            )
        return await self.list_memories(user_id, repo)

    async def memory_projects(self, user_id: str) -> list[str]:
        """Repositories the owner worked in or keeps memories or a switch for."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT repo FROM agent_threads
                WHERE user_id = $1 AND repo IS NOT NULL AND repo <> '' AND deleted_at IS NULL
                UNION
                SELECT repo FROM agent_memories WHERE user_id = $1 AND repo IS NOT NULL
                UNION
                SELECT repo FROM agent_memory_settings WHERE user_id = $1 AND repo <> ''
                ORDER BY repo
                """,
                user_id,
            )
        return [str(row["repo"]) for row in rows]

    async def clear_memories(self, user_id: str, repo: str | None) -> None:
        """Delete every memory in one scope, archived ones included."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, user_id)
            await conn.execute(
                "DELETE FROM agent_memories WHERE user_id = $1 AND repo IS NOT DISTINCT FROM $2",
                user_id,
                repo,
            )

    async def task_memories(self, user_id: str, repo: str | None) -> dict[str, Any]:
        """Return the active memories a task in ``repo`` follows.

        Project memories come only from that repository; a scope whose memory
        is turned off contributes nothing. Within a scope the most recently
        written or edited come first, so a prompt that cannot fit them all
        leaves out the oldest.
        """
        scopes: dict[str, list[dict[str, Any]]] = {"project": [], "user": []}
        async with self._pool.acquire() as conn:
            for scope, scope_repo in (("project", repo), ("user", None)):
                if scope == "project" and repo is None:
                    continue
                if not await _enabled(conn, user_id, scope_repo):
                    continue
                rows = await conn.fetch(
                    f"SELECT {_COLUMNS} FROM agent_memories "
                    "WHERE user_id = $1 AND repo IS NOT DISTINCT FROM $2 AND status = 'active' "
                    "ORDER BY updated_at DESC, created_at DESC, id",
                    user_id,
                    scope_repo,
                )
                scopes[scope] = [_view(row) for row in rows]
        return {"repo": repo, **scopes}

    async def mark_memories_used(self, user_id: str, memory_ids: list[str]) -> None:
        """Record that these memories were given to a task."""
        if not memory_ids:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_memories SET last_used_at = clock_timestamp() "
                "WHERE user_id = $1 AND id = ANY($2::text[])",
                user_id,
                memory_ids,
            )

    async def save_agent_memory(
        self, task: MemoryTask, content: str, *, all_projects: bool
    ) -> dict[str, Any]:
        """Save a memory an agent wrote during ``task``.

        It belongs to the task's repository unless ``all_projects`` is set or
        the task has none. An identical active memory gains this task as a
        source instead of a copy.
        """
        content = normalize_memory(content)
        async with self._pool.acquire() as conn, conn.transaction():
            user_id, job_id = task.user_id, task.job_id
            await _lock(conn, user_id)
            task_repo = await _live_task_repo(conn, task)
            repo = None if all_projects else task_repo
            if not await _enabled(conn, user_id, repo):
                raise AgentMemoryDisabled("Memory is turned off for this scope.")
            active = await _active(conn, user_id, repo)
            match = next(
                (row for row in active if memory_key(row["content"]) == memory_key(content)), None
            )
            if match is not None:
                row = await _add_source(conn, match["id"], job_id)
            else:
                if not await _make_room(conn, user_id, repo, len(active)):
                    raise AgentMemoryFull("This scope already holds its limit of saved memories.")
                row = await _insert(
                    conn, user_id, repo, content, origin="agent", source_job_ids=[job_id]
                )
        return _view(row)

    async def replace_agent_memory(
        self, task: MemoryTask, memory_id: str, content: str
    ) -> dict[str, Any]:
        """Replace a memory the task can see with new text, archiving the old one.

        The replacement keeps the old memory's scope, and the archived memory
        names it in ``superseded_by`` so the owner can undo the change.
        """
        content = normalize_memory(content)
        async with self._pool.acquire() as conn, conn.transaction():
            user_id, job_id = task.user_id, task.job_id
            await _lock(conn, user_id)
            old = await self._visible_memory(conn, task, memory_id)
            if memory_key(old["content"]) == memory_key(content):
                return _view(old)
            active = await _active(conn, user_id, old["repo"])
            match = next(
                (
                    row
                    for row in active
                    if row["id"] != memory_id and memory_key(row["content"]) == memory_key(content)
                ),
                None,
            )
            if match is not None:
                new = await _add_source(conn, match["id"], job_id)
            else:
                new = await _insert(
                    conn, user_id, old["repo"], content, origin="agent", source_job_ids=[job_id]
                )
            await _archive(conn, memory_id, superseded_by=new["id"])
        return _view(new)

    async def archive_agent_memory(self, task: MemoryTask, memory_id: str) -> dict[str, Any]:
        """Archive a memory the task can see; the owner can restore it."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock(conn, task.user_id)
            await self._visible_memory(conn, task, memory_id)
            row = await _archive(conn, memory_id, superseded_by=None)
        return _view(row)

    async def _visible_memory(self, conn: Any, task: MemoryTask, memory_id: str) -> asyncpg.Record:
        """An active memory of the task's repository or the user, in a scope that is on."""
        user_id = task.user_id
        task_repo = await _live_task_repo(conn, task)
        row = await self._owned_memory(conn, user_id, memory_id)
        if row["status"] != "active" or row["repo"] not in (None, task_repo):
            raise AgentMemoryNotFound("Memory not found.")
        if not await _enabled(conn, user_id, row["repo"]):
            raise AgentMemoryDisabled("Memory is turned off for this scope.")
        return row

    async def purge_memories(self) -> None:
        """Forget archives older than the retention period."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_memories WHERE status = 'archived' "
                "AND archived_at < clock_timestamp() - make_interval(days => $1)",
                ARCHIVE_RETENTION_DAYS,
            )
