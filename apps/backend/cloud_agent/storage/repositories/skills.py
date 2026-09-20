"""Postgres persistence for personal skills and new-conversation defaults."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.skills.models import (
    MAX_LIBRARY_SKILLS,
    MAX_SELECTED_SKILLS,
    MAX_SELECTION_BYTES,
    SkillConflict,
    SkillError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class PostgresSkillRepository:
    """Serialize one owner's library writes and fence every edit by revision."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the control plane's connection pool."""
        self._pool = pool

    async def list_skills(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return lightweight summaries of the owner's live skills."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, description, source, source_url, source_revision, default_enabled, "
                "revision, content_revision, created_at, updated_at, "
                "octet_length(instructions) AS instruction_bytes "
                "FROM agent_skills WHERE user_id = $1 AND deleted_at IS NULL "
                "ORDER BY lower(name), id",
                user_id,
            )
        return [dict(row) for row in rows]

    async def select_skills(
        self, *, user_id: str, skill_ids: list[int] | None
    ) -> list[dict[str, Any]]:
        """Read bodies only for owned defaults or explicitly selected IDs."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_skills WHERE user_id = $1 AND deleted_at IS NULL "
                "AND (($2::bigint[] IS NULL AND default_enabled) OR id = ANY($2::bigint[])) "
                "ORDER BY lower(name), id",
                user_id,
                skill_ids,
            )
        return [dict(row) for row in rows]

    async def get_skill(self, *, user_id: str, skill_id: int) -> dict[str, Any] | None:
        """Read a live skill only when it belongs to this owner."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_skills WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                skill_id,
                user_id,
            )
        return dict(row) if row else None

    @staticmethod
    async def _lock(conn: Any, user_id: str) -> None:
        """Serialize count/default checks, including a user's first insertion."""
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-skills:' || $1, 0))",
            user_id,
        )

    @staticmethod
    async def _check_defaults(
        conn: Any, user_id: str, candidate: Mapping[str, Any], skill_id: int | None = None
    ) -> None:
        """Keep new-chat defaults inside the same limits as explicit selections."""
        if not candidate.get("default_enabled", False):
            return
        totals = await conn.fetchrow(
            "SELECT count(*) AS count, COALESCE(sum(octet_length(instructions)), 0) AS bytes "
            "FROM agent_skills WHERE user_id = $1 "
            "AND default_enabled AND deleted_at IS NULL "
            "AND ($2::bigint IS NULL OR id <> $2)",
            user_id,
            skill_id,
        )
        if totals["count"] + 1 > MAX_SELECTED_SKILLS:
            raise SkillError("Enable at most 16 default skills for new chats.")
        if (
            totals["bytes"] + len(str(candidate["instructions"]).encode("utf-8"))
            > MAX_SELECTION_BYTES
        ):
            raise SkillError("Default skill instructions must total no more than 128 KB.")

    async def create_skill(self, *, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a skill, enforcing names, library size, and default limits."""
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await self._lock(conn, user_id)
                count = await conn.fetchval(
                    "SELECT count(*) FROM agent_skills WHERE user_id = $1 AND deleted_at IS NULL",
                    user_id,
                )
                if count >= MAX_LIBRARY_SKILLS:
                    raise SkillError("Your library can contain at most 100 skills.")
                await self._check_defaults(conn, user_id, values)
                row = await conn.fetchrow(
                    """
                    INSERT INTO agent_skills
                        (user_id, name, description, instructions, source,
                         source_url, source_revision, default_enabled)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING *
                    """,
                    user_id,
                    values["name"],
                    values["description"],
                    values["instructions"],
                    values["source"],
                    values.get("source_url"),
                    values.get("source_revision"),
                    values.get("default_enabled", False),
                )
        except asyncpg.UniqueViolationError as exc:
            raise SkillConflict("A skill with this name already exists.") from exc
        return dict(row)

    async def update_skill(
        self, *, user_id: str, skill_id: int, revision: int, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a live skill at the revision read, or return None if absent."""
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await self._lock(conn, user_id)
                current = await conn.fetchrow(
                    "SELECT * FROM agent_skills WHERE id = $1 AND user_id = $2 "
                    "AND deleted_at IS NULL FOR UPDATE",
                    skill_id,
                    user_id,
                )
                if current is None:
                    return None
                if current["revision"] != revision:
                    raise SkillConflict("This skill changed. Refresh it before saving.")
                if current["source"] == "github" and any(
                    key != "default_enabled" for key in values
                ):
                    raise SkillError("Imported GitHub skills are read-only.")
                merged = {**dict(current), **values}
                await self._check_defaults(conn, user_id, merged, skill_id)
                row = await conn.fetchrow(
                    """
                    UPDATE agent_skills SET name = $3, description = $4, instructions = $5,
                        default_enabled = $6, revision = revision + 1,
                        content_revision = content_revision +
                            CASE WHEN (name, description, instructions) IS DISTINCT FROM ($3, $4, $5)
                                 THEN 1 ELSE 0 END,
                        updated_at = NOW()
                    WHERE id = $1 AND user_id = $2 RETURNING *
                    """,
                    skill_id,
                    user_id,
                    merged["name"],
                    merged["description"],
                    merged["instructions"],
                    merged["default_enabled"],
                )
        except asyncpg.UniqueViolationError as exc:
            raise SkillConflict("A skill with this name already exists.") from exc
        return dict(row)

    async def delete_skill(self, *, user_id: str, skill_id: int, revision: int) -> bool:
        """Soft-delete at the expected revision, leaving turn snapshots intact."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock(conn, user_id)
            row = await conn.fetchrow(
                "SELECT revision FROM agent_skills WHERE id = $1 AND user_id = $2 "
                "AND deleted_at IS NULL FOR UPDATE",
                skill_id,
                user_id,
            )
            if row is None:
                return False
            if row["revision"] != revision:
                raise SkillConflict("This skill changed. Refresh it before removing.")
            await conn.execute(
                "UPDATE agent_skills SET deleted_at = NOW(), default_enabled = FALSE, "
                "revision = revision + 1, updated_at = NOW() WHERE id = $1 AND user_id = $2",
                skill_id,
                user_id,
            )
        return True
