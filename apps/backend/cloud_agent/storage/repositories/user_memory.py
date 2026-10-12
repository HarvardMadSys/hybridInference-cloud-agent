"""Postgres materialization of user and project memory from owned messages."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.user_memory import (
    MEMORY_TOTAL_LIMIT,
    MemoryRevisionConflict,
    UserMemory,
    UserMemoryConflict,
    UserMemoryNotFound,
    bounded_memories,
    build_project_memory,
    build_user_memory,
    normalize_memory_edit,
    prune_memory_overrides,
    validate_memory_preference_edit,
)

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
        "memories": [
            memory.model_dump(mode="json")
            for memory in bounded_memories(
                [
                    UserMemory.model_validate(
                        {
                            **memory,
                            "scope": memory.get(
                                "scope", "repo" if memory.get("source_repo") else "user"
                            ),
                        }
                    )
                    for memory in _memory_json(row["memories"])
                ]
            )
        ],
        "suggestions": _memory_json(row["suggestions"]),
        "revision": int(row["revision"]),
        "updated_at": row["updated_at"],
    }


def _public_project_memory(row: Mapping[str, Any]) -> dict[str, Any]:
    """Include the repository in a project's otherwise shared public profile."""
    return {**_public_memory(row), "repo": str(row["repo"])}


def _memory_activity_digest(activity: list[dict[str, Any]]) -> str:
    """Detect edits, deletions and relevant context changes, including long text."""
    encoded = json.dumps(activity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _apply_memory_edits(
    memories: list[dict[str, Any]], edits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply corrections only to their original generated source, in source order."""
    by_id = {edit["memory_id"]: edit for edit in edits}
    result = []
    for memory in memories:
        edit = by_id.get(memory["id"])
        if edit is not None and edit["content"] is None:
            continue
        if edit is not None and edit["source_job_id"] == memory["source_job_id"]:
            memory = {**memory, "content": edit["content"], "user_edited": True}
        result.append(memory)
    return result


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
        UNION
        SELECT user_id FROM agent_project_memories WHERE enabled
    ) candidates
    LEFT JOIN agent_user_memories p ON p.user_id = candidates.user_id
    WHERE (COALESCE(p.enabled, TRUE)
       OR EXISTS (
           SELECT 1 FROM agent_project_memories project
           WHERE project.user_id = candidates.user_id AND project.enabled
       )
       OR EXISTS (
           SELECT 1 FROM agent_threads t
           JOIN agent_jobs j ON j.thread_id = t.id AND j.user_id = t.user_id
           JOIN agent_thread_messages m ON m.job_id = j.id AND m.thread_id = t.id
           LEFT JOIN agent_project_memories project
             ON project.user_id = t.user_id AND project.repo = t.repo
           WHERE t.user_id = candidates.user_id AND t.deleted_at IS NULL
             AND t.repo IS NOT NULL AND t.repo <> ''
             AND m.role = 'user' AND j.fork_source_job_id IS NULL
             AND (j.turn_source <> 'goal' OR m.mid_turn)
             AND COALESCE(project.enabled, TRUE)
       ))
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
        # Corrections belong to their original source. Forget their saved
        # content when that source is deleted; removal tombstones remain valid
        # if the same generated ID occurs in another source later.
        await conn.execute(
            """
            DELETE FROM agent_user_memory_edits e
            WHERE e.user_id = $1 AND e.content IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
                WHERE j.id = e.source_job_id AND j.user_id = $1 AND t.user_id = $1
                  AND t.deleted_at IS NULL
            )
            """,
            user_id,
        )
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
        overrides = prune_memory_overrides(activity, _memory_json(current.get("overrides", [])))
        profile = build_user_memory(activity, _public_memory(current), overrides)
        memory_ids = [memory["id"] for memory in profile["memories"]]
        if memory_ids:
            edits = await conn.fetch(
                "SELECT memory_id, source_job_id, content FROM agent_user_memory_edits "
                "WHERE user_id = $1 AND memory_id = ANY($2::text[])",
                user_id,
                memory_ids,
            )
            profile["memories"] = _apply_memory_edits(
                profile["memories"], [dict(edit) for edit in edits]
            )
        updated = await conn.fetchrow(
            """
            UPDATE agent_user_memories
            SET portrait = $2::jsonb, memories = $3::jsonb, suggestions = $4::jsonb,
                activity_digest = $5, overrides = $6::jsonb, revision = revision + 1, updated_at = clock_timestamp()
            WHERE user_id = $1 RETURNING *
            """,
            user_id,
            json.dumps(profile["portrait"]),
            json.dumps(profile["memories"]),
            json.dumps(profile["suggestions"]),
            digest,
            json.dumps(overrides),
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
            await conn.execute("DELETE FROM agent_user_memory_edits WHERE user_id = $1", user_id)
            await conn.execute(
                """
                UPDATE agent_project_memories p
                SET overrides = COALESCE((
                    SELECT jsonb_agg(
                        CASE WHEN item.entry->'memory'->>'scope' = 'user'
                             THEN (item.entry - 'memory') || jsonb_build_object('memory', NULL)
                             ELSE item.entry END ORDER BY item.ordinality
                    )
                    FROM jsonb_array_elements(p.overrides)
                        WITH ORDINALITY AS item(entry, ordinality)
                ), '[]'::jsonb)
                WHERE p.user_id = $1
                """,
                user_id,
            )
            row = await conn.fetchrow(
                """
                UPDATE agent_user_memories
                SET portrait = $2::jsonb, memories = $3::jsonb, suggestions = $4::jsonb,
                    overrides = '[]'::jsonb, activity_digest = $5,
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

    async def _change_user_memory_entry(
        self, user_id: str, memory_id: str, *, content: str | None, expected_revision: int
    ) -> dict[str, Any]:
        """Fence an owned entry mutation and its overlay against all profile writes."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_user_memory(conn, user_id)
            profile = await self._refresh_user_memory_locked(conn, user_id, current)
            if profile["revision"] != expected_revision:
                raise UserMemoryConflict("Memory changed. Refresh before saving.")
            entry = next(
                (memory for memory in profile["memories"] if memory["id"] == memory_id), None
            )
            if entry is None:
                raise UserMemoryNotFound("Memory entry was not found.")
            if content is not None:
                if entry["kind"] == "preference":
                    validate_memory_preference_edit(content)
                source_live = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM agent_jobs j
                        JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
                        WHERE j.id = $2 AND j.user_id = $1 AND t.user_id = $1
                          AND t.deleted_at IS NULL
                    )
                    """,
                    user_id,
                    entry["source_job_id"],
                )
                if not source_live:
                    raise UserMemoryNotFound("Memory entry was not found.")
                if entry["content"] == content and entry.get("user_edited") is True:
                    return profile
            if (
                content is not None
                and sum(
                    len(memory["content"])
                    for memory in profile["memories"]
                    if memory["id"] != memory_id
                )
                + len(content)
                > MEMORY_TOTAL_LIMIT
            ):
                raise ValueError("Memory exceeds the total text limit")
            await conn.execute(
                """
                INSERT INTO agent_user_memory_edits (user_id, memory_id, source_job_id, content)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, memory_id) DO UPDATE
                SET source_job_id = EXCLUDED.source_job_id, content = EXCLUDED.content,
                    updated_at = clock_timestamp()
                """,
                user_id,
                memory_id,
                entry["source_job_id"],
                content,
            )
            memories = [
                {**memory, "content": content, "user_edited": True}
                if memory["id"] == memory_id
                else memory
                for memory in profile["memories"]
                if memory["id"] != memory_id or content is not None
            ]
            row = await conn.fetchrow(
                """
                UPDATE agent_user_memories
                SET memories = $2::jsonb, revision = revision + 1, updated_at = clock_timestamp()
                WHERE user_id = $1 RETURNING *
                """,
                user_id,
                json.dumps(memories),
            )
        return _public_memory(row)

    async def update_entry(
        self, user_id: str, memory_id: str, *, content: str, expected_revision: int
    ) -> dict[str, Any]:
        """Correct one existing entry without changing its identity or provenance."""
        return await self._change_user_memory_entry(
            user_id,
            memory_id,
            content=normalize_memory_edit(content),
            expected_revision=expected_revision,
        )

    async def delete_entry(
        self, user_id: str, memory_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        """Save a removal tombstone so automatic refresh cannot restore the entry."""
        return await self._change_user_memory_entry(
            user_id, memory_id, content=None, expected_revision=expected_revision
        )

    async def replace_memories(
        self, user_id: str, revision: int, memories: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Persist edits and tombstones under the same lock as refresh and clear."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_user_memory(conn, user_id)
            fresh = await self._refresh_user_memory_locked(conn, user_id, current)
            if fresh["revision"] != revision:
                raise MemoryRevisionConflict()
            current = await self._ensure_user_memory(conn, user_id)
            existing = _memory_json(current["overrides"])
            # Bulk corrections supersede older single-entry corrections. Keep
            # hidden removal markers so refresh cannot revive omitted entries.
            await conn.execute(
                "DELETE FROM agent_user_memory_edits WHERE user_id = $1 "
                "AND memory_id = ANY($2::text[])",
                user_id,
                [memory["id"] for memory in fresh["memories"]],
            )
            original = fresh["memories"]
            source_ids = {
                source_id
                for memory in original
                for source_id in (memory.get("source_ids") or [memory["id"]])
            }
            overrides = [
                item for item in existing if not source_ids.intersection(item["source_ids"])
            ]
            retained: set[str] = set()
            for memory in memories:
                ids = memory["source_ids"]
                if not ids or not set(ids).issubset(source_ids):
                    raise ValueError("Memory sources must belong to the current view")
                retained.update(ids)
                overrides.append({"source_ids": ids, "memory": memory})
            for source_id in source_ids - retained:
                overrides.append({"source_ids": [source_id], "memory": None})
            await self._transfer_memory_scopes_locked(conn, user_id, None, memories)
            memories = [memory for memory in memories if memory["scope"] == "user"]
            row = await conn.fetchrow(
                """
                UPDATE agent_user_memories
                SET memories = $2::jsonb, overrides = $3::jsonb,
                    revision = revision + 1, updated_at = clock_timestamp()
                WHERE user_id = $1 RETURNING *
                """,
                user_id,
                json.dumps(memories),
                json.dumps(overrides),
            )
        return _public_memory(row)

    async def _change_project_memory_entry(
        self,
        user_id: str,
        repo: str,
        memory_id: str,
        *,
        content: str | None,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Fence an owned entry mutation and its overlay against all profile writes."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_project_memory(conn, user_id, repo)
            profile = await self._refresh_project_memory_locked(conn, user_id, repo, current)
            if profile["revision"] != expected_revision:
                raise UserMemoryConflict("Memory changed. Refresh before saving.")
            entry = next(
                (memory for memory in profile["memories"] if memory["id"] == memory_id), None
            )
            if entry is None:
                raise UserMemoryNotFound("Memory entry was not found.")
            if content is not None:
                if entry["kind"] == "preference":
                    validate_memory_preference_edit(content)
                source_live = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM agent_jobs j
                        JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
                        WHERE j.id = $3 AND j.user_id = $1 AND t.user_id = $1
                          AND t.repo = $2 AND t.deleted_at IS NULL
                    )
                    """,
                    user_id,
                    repo,
                    entry["source_job_id"],
                )
                if not source_live:
                    raise UserMemoryNotFound("Memory entry was not found.")
                if entry["content"] == content and entry.get("user_edited") is True:
                    return profile
            if (
                content is not None
                and sum(
                    len(memory["content"])
                    for memory in profile["memories"]
                    if memory["id"] != memory_id
                )
                + len(content)
                > MEMORY_TOTAL_LIMIT
            ):
                raise ValueError("Memory exceeds the total text limit")
            await conn.execute(
                """
                INSERT INTO agent_project_memory_edits (user_id, repo, memory_id, source_job_id, content)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, repo, memory_id) DO UPDATE
                SET source_job_id = EXCLUDED.source_job_id, content = EXCLUDED.content,
                    updated_at = clock_timestamp()
                """,
                user_id,
                repo,
                memory_id,
                entry["source_job_id"],
                content,
            )
            memories = [
                {**memory, "content": content, "user_edited": True}
                if memory["id"] == memory_id
                else memory
                for memory in profile["memories"]
                if memory["id"] != memory_id or content is not None
            ]
            row = await conn.fetchrow(
                """
                UPDATE agent_project_memories
                SET memories = $3::jsonb, revision = revision + 1, updated_at = clock_timestamp()
                WHERE user_id = $1 AND repo = $2 RETURNING *
                """,
                user_id,
                repo,
                json.dumps(memories),
            )
        return _public_project_memory(row)

    async def update_project_entry(
        self, user_id: str, repo: str, memory_id: str, *, content: str, expected_revision: int
    ) -> dict[str, Any]:
        """Correct one existing entry without changing its identity or provenance."""
        return await self._change_project_memory_entry(
            user_id,
            repo,
            memory_id,
            content=normalize_memory_edit(content),
            expected_revision=expected_revision,
        )

    async def delete_project_entry(
        self, user_id: str, repo: str, memory_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        """Save a removal tombstone so automatic refresh cannot restore the entry."""
        return await self._change_project_memory_entry(
            user_id, repo, memory_id, content=None, expected_revision=expected_revision
        )

    async def replace_project_memories(
        self, user_id: str, repo: str, revision: int, memories: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Persist edits and tombstones under the same lock as refresh and clear."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_project_memory(conn, user_id, repo)
            fresh = await self._refresh_project_memory_locked(conn, user_id, repo, current)
            if fresh["revision"] != revision:
                raise MemoryRevisionConflict()
            current = await self._ensure_project_memory(conn, user_id, repo)
            existing = _memory_json(current["overrides"])
            # Bulk corrections supersede older single-entry corrections. Keep
            # hidden removal markers so refresh cannot revive omitted entries.
            await conn.execute(
                "DELETE FROM agent_project_memory_edits WHERE user_id = $1 AND repo = $2 "
                "AND memory_id = ANY($3::text[])",
                user_id,
                repo,
                [memory["id"] for memory in fresh["memories"]],
            )
            original = fresh["memories"]
            source_ids = {
                source_id
                for memory in original
                for source_id in (memory.get("source_ids") or [memory["id"]])
            }
            overrides = [
                item for item in existing if not source_ids.intersection(item["source_ids"])
            ]
            retained: set[str] = set()
            for memory in memories:
                ids = memory["source_ids"]
                if not ids or not set(ids).issubset(source_ids):
                    raise ValueError("Memory sources must belong to the current view")
                retained.update(ids)
                overrides.append({"source_ids": ids, "memory": memory})
            for source_id in source_ids - retained:
                overrides.append({"source_ids": [source_id], "memory": None})
            if any(
                memory["scope"] == "repo" and memory.get("source_repo") != repo
                for memory in memories
            ):
                raise ValueError("Project memory must retain its source repository")
            await self._transfer_memory_scopes_locked(conn, user_id, repo, memories)
            memories = [memory for memory in memories if memory["scope"] == "repo"]
            row = await conn.fetchrow(
                """
                UPDATE agent_project_memories
                SET memories = $3::jsonb, overrides = $4::jsonb,
                    revision = revision + 1, updated_at = clock_timestamp()
                WHERE user_id = $1 AND repo = $2 RETURNING *
                """,
                user_id,
                repo,
                json.dumps(memories),
                json.dumps(overrides),
            )
        return _public_project_memory(row)

    async def _transfer_memory_scopes_locked(
        self, conn: Any, user_id: str, source_repo: str | None, memories: list[dict[str, Any]]
    ) -> None:
        """Copy scope overrides atomically; source overrides suppress the old scope."""
        targets: dict[str | None, list[dict[str, Any]]] = {}
        for memory in memories:
            target_repo = memory.get("source_repo") if memory["scope"] == "repo" else None
            if memory["scope"] == "repo" and not target_repo:
                raise ValueError("Project memory requires a source repository")
            if target_repo != source_repo:
                targets.setdefault(target_repo, []).append(memory)
        for repo, entries in targets.items():
            current = (
                await self._ensure_user_memory(conn, user_id)
                if repo is None
                else await self._ensure_project_memory(conn, user_id, repo)
            )
            ids = {source_id for memory in entries for source_id in memory["source_ids"]}
            overrides = [
                item
                for item in _memory_json(current["overrides"])
                if not ids.intersection(item["source_ids"])
            ] + [{"source_ids": memory["source_ids"], "memory": memory} for memory in entries]
            previous = [
                memory
                for memory in _memory_json(current["memories"])
                if not ids.intersection(memory.get("source_ids") or [memory["id"]])
            ]
            combined = [UserMemory.model_validate(memory) for memory in entries + previous]
            bounded = bounded_memories(combined)
            # Never silently lose existing manual knowledge during a scope change.
            if len(bounded) != len(combined):
                raise ValueError("The destination memory exceeds its saved-memory limits")
            saved = [memory.model_dump(mode="json") for memory in bounded]
            if repo is None:
                await conn.execute(
                    "DELETE FROM agent_user_memory_edits WHERE user_id = $1 "
                    "AND memory_id = ANY($2::text[])",
                    user_id,
                    list(ids),
                )
                await conn.fetchrow(
                    """
                    UPDATE agent_user_memories
                    SET memories = $2::jsonb, overrides = $3::jsonb, activity_digest = '',
                        revision = revision + 1, updated_at = clock_timestamp()
                    WHERE user_id = $1 RETURNING *
                    """,
                    user_id,
                    json.dumps(saved),
                    json.dumps(overrides),
                )
            else:
                await conn.execute(
                    "DELETE FROM agent_project_memory_edits WHERE user_id = $1 AND repo = $2 "
                    "AND memory_id = ANY($3::text[])",
                    user_id,
                    repo,
                    list(ids),
                )
                await conn.fetchrow(
                    """
                    UPDATE agent_project_memories
                    SET memories = $3::jsonb, overrides = $4::jsonb, activity_digest = '',
                        revision = revision + 1, updated_at = clock_timestamp()
                    WHERE user_id = $1 AND repo = $2 RETURNING *
                    """,
                    user_id,
                    repo,
                    json.dumps(saved),
                    json.dumps(overrides),
                )

    @staticmethod
    async def _ensure_project_memory(conn: Any, user_id: str, repo: str) -> dict[str, Any]:
        """Initialize this owner's project with independent generation and reset defaults."""
        row = await conn.fetchrow(
            "SELECT * FROM agent_project_memories WHERE user_id = $1 AND repo = $2",
            user_id,
            repo,
        )
        if row is not None:
            return dict(row)
        empty = build_project_memory([], None, repo)
        row = await conn.fetchrow(
            """
            INSERT INTO agent_project_memories (user_id, repo, portrait, memories, suggestions)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb) RETURNING *
            """,
            user_id,
            repo,
            json.dumps(empty["portrait"]),
            json.dumps(empty["memories"]),
            json.dumps(empty["suggestions"]),
        )
        return dict(row)

    @staticmethod
    async def _refresh_project_memory_locked(
        conn: Any, user_id: str, repo: str, current: dict[str, Any]
    ) -> dict[str, Any]:
        """Read this project's bounded input while holding the owner's privacy lock."""
        if not current["enabled"]:
            return _public_project_memory(current)
        await conn.execute(
            """
            DELETE FROM agent_project_memory_edits e
            WHERE e.user_id = $1 AND e.repo = $2 AND e.content IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id AND t.user_id = j.user_id
                WHERE j.id = e.source_job_id AND j.user_id = $1 AND t.user_id = $1
                  AND t.repo = $2 AND t.deleted_at IS NULL
            )
            """,
            user_id,
            repo,
        )
        rows = await conn.fetch(
            """
            SELECT m.id, m.id AS message_id, m.job_id, left(m.content, 8000) AS content,
                   md5(m.content) AS content_digest, t.repo, j.runtime, j.model,
                   t.title, m.created_at
            FROM agent_thread_messages m
            JOIN agent_jobs j ON j.id = m.job_id AND j.thread_id = m.thread_id
            JOIN agent_threads t ON t.id = m.thread_id AND t.user_id = j.user_id
            WHERE j.user_id = $1 AND t.user_id = $1 AND t.repo = $4
              AND t.deleted_at IS NULL AND m.role = 'user' AND j.fork_source_job_id IS NULL
              AND (j.turn_source <> 'goal' OR m.mid_turn)
              AND m.id > $2
              AND ($3::timestamptz IS NULL OR m.created_at > $3)
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 50
            """,
            user_id,
            current["cleared_message_id"],
            current["cleared_at"],
            repo,
        )
        activity = [dict(row) for row in rows]
        digest = _memory_activity_digest(activity)
        if digest == current["activity_digest"]:
            return _public_project_memory(current)
        for item in activity:
            item.pop("content_digest")
        overrides = prune_memory_overrides(activity, _memory_json(current.get("overrides", [])))
        profile = build_project_memory(activity, _public_project_memory(current), repo, overrides)
        memory_ids = [memory["id"] for memory in profile["memories"]]
        if memory_ids:
            edits = await conn.fetch(
                "SELECT memory_id, source_job_id, content FROM agent_project_memory_edits "
                "WHERE user_id = $1 AND repo = $2 AND memory_id = ANY($3::text[])",
                user_id,
                repo,
                memory_ids,
            )
            profile["memories"] = _apply_memory_edits(
                profile["memories"], [dict(edit) for edit in edits]
            )
        row = await conn.fetchrow(
            """
            UPDATE agent_project_memories
            SET portrait = $3::jsonb, memories = $4::jsonb, suggestions = $5::jsonb,
                activity_digest = $6, overrides = $7::jsonb, revision = revision + 1, updated_at = clock_timestamp()
            WHERE user_id = $1 AND repo = $2 RETURNING *
            """,
            user_id,
            repo,
            json.dumps(profile["portrait"]),
            json.dumps(profile["memories"]),
            json.dumps(profile["suggestions"]),
            digest,
            json.dumps(overrides),
        )
        return _public_project_memory(row)

    async def refresh_project(self, user_id: str, repo: str) -> dict[str, Any]:
        """Materialize only the selected repository's eligible owner activity."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_project_memory(conn, user_id, repo)
            return await self._refresh_project_memory_locked(conn, user_id, repo, current)

    async def get_project(self, user_id: str, repo: str) -> dict[str, Any] | None:
        """Read a saved project without sharing another owner's knowledge."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_project_memories WHERE user_id = $1 AND repo = $2",
                user_id,
                repo,
            )
        return _public_project_memory(row) if row is not None else None

    async def set_project_enabled(self, user_id: str, repo: str, enabled: bool) -> dict[str, Any]:
        """Change only this project's generation and use, serialized with refresh."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_project_memory(conn, user_id, repo)
            if current["enabled"] == enabled:
                return (
                    await self._refresh_project_memory_locked(conn, user_id, repo, current)
                    if enabled
                    else _public_project_memory(current)
                )
            row = await conn.fetchrow(
                """
                UPDATE agent_project_memories
                SET enabled = $3, revision = revision + 1, updated_at = clock_timestamp()
                WHERE user_id = $1 AND repo = $2 RETURNING *
                """,
                user_id,
                repo,
                enabled,
            )
            if enabled:
                return await self._refresh_project_memory_locked(conn, user_id, repo, dict(row))
        return _public_project_memory(row)

    async def clear_project(self, user_id: str, repo: str) -> dict[str, Any]:
        """Forget one project's derived knowledge and fence its hidden history too."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._user_memory_lock(conn, user_id)
            current = await self._ensure_project_memory(conn, user_id, repo)
            empty = build_project_memory([], _public_project_memory(current), repo)
            await conn.execute(
                "DELETE FROM agent_project_memory_edits WHERE user_id = $1 AND repo = $2",
                user_id,
                repo,
            )
            await conn.execute(
                """
                UPDATE agent_user_memories u
                SET overrides = COALESCE((
                    SELECT jsonb_agg(
                        CASE WHEN item.entry->'memory'->>'scope' = 'repo'
                                   AND item.entry->'memory'->>'source_repo' = $2
                             THEN (item.entry - 'memory') || jsonb_build_object('memory', NULL)
                             ELSE item.entry END ORDER BY item.ordinality
                    )
                    FROM jsonb_array_elements(u.overrides)
                        WITH ORDINALITY AS item(entry, ordinality)
                ), '[]'::jsonb)
                WHERE u.user_id = $1
                """,
                user_id,
                repo,
            )
            row = await conn.fetchrow(
                """
                UPDATE agent_project_memories
                SET portrait = $3::jsonb, memories = $4::jsonb, suggestions = $5::jsonb,
                    overrides = '[]'::jsonb, activity_digest = $6,
                    cleared_message_id = (
                        SELECT COALESCE(max(m.id), 0)
                        FROM agent_thread_messages m
                        JOIN agent_jobs j ON j.id = m.job_id AND j.thread_id = m.thread_id
                        JOIN agent_threads t ON t.id = m.thread_id AND t.user_id = j.user_id
                        WHERE j.user_id = $1 AND t.user_id = $1 AND t.repo = $2
                          AND m.role = 'user'
                    ),
                    cleared_at = clock_timestamp(), revision = revision + 1,
                    updated_at = clock_timestamp()
                WHERE user_id = $1 AND repo = $2 RETURNING *
                """,
                user_id,
                repo,
                json.dumps(empty["portrait"]),
                json.dumps(empty["memories"]),
                json.dumps(empty["suggestions"]),
                _memory_activity_digest([]),
            )
        return _public_project_memory(row)

    async def list_memory_projects(self, user_id: str) -> list[str]:
        """List owned repositories, including saved projects whose sources disappeared."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT repo FROM agent_threads WHERE user_id = $1 AND repo IS NOT NULL AND repo <> ''
                UNION
                SELECT repo FROM agent_project_memories WHERE user_id = $1
                ORDER BY repo
                """,
                user_id,
            )
        return [str(row["repo"]) for row in rows]

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
                    try:
                        await self.refresh(user_id)
                    except Exception:
                        # Independent projects must still refresh if the user
                        # document is malformed or its update fails.
                        logger.warning("agent_user_memory_owner_refresh_failed")
                    for repo in await self.list_memory_projects(user_id):
                        try:
                            await self.refresh_project(user_id, repo)
                        except Exception:
                            logger.warning("agent_project_memory_refresh_failed")
                except Exception:
                    # One corrupt profile or transient owner failure must not
                    # starve later owners. Never log content or exception data.
                    logger.warning("agent_user_memory_owner_refresh_failed")
                finally:
                    self._active_memory_cursor = user_id
            return len(rows)
