"""Postgres storage for owner-authored profile names and sanitized pictures."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.user_profile import ProfileConflict, ProfileView

if TYPE_CHECKING:
    import asyncpg


class PostgresUserProfileRepository:
    """Keep display data separate from identity, permissions, and inferred memory."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_user_profile(self, user_id: str) -> ProfileView:
        """Read only the supplied owner's display fields."""
        row = await self._pool.fetchrow(
            "SELECT first_name, last_name, image_data_url, revision "
            "FROM agent_user_profiles WHERE user_id = $1",
            user_id,
        )
        return ProfileView.model_validate(dict(row)) if row else ProfileView()

    async def save_user_profile(
        self,
        user_id: str,
        *,
        first_name: str,
        last_name: str,
        expected_revision: int,
        replace_image: bool,
        image_data_url: str | None,
    ) -> ProfileView:
        """Atomically save a profile without overwriting changes from another tab."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO agent_user_profiles (user_id) VALUES ($1) "
                "ON CONFLICT (user_id) DO NOTHING",
                user_id,
            )
            row = await conn.fetchrow(
                "UPDATE agent_user_profiles SET first_name = $2, last_name = $3, "
                "image_data_url = CASE WHEN $5 THEN $6 ELSE image_data_url END, "
                "revision = revision + 1, updated_at = now() "
                "WHERE user_id = $1 AND revision = $4 "
                "RETURNING first_name, last_name, image_data_url, revision",
                user_id,
                first_name,
                last_name,
                expected_revision,
                replace_image,
                image_data_url,
            )
            if row is None:
                raise ProfileConflict("This profile changed in another tab.")
            return ProfileView.model_validate(dict(row))
