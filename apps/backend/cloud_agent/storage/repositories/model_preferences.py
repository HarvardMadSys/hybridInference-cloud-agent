"""Postgres storage for each owner's pinned default model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.model_preference import ModelPreferenceView

if TYPE_CHECKING:
    import asyncpg


class PostgresModelPreferenceRepository:
    """Keep an owner's pin beside the newest turn that shows what they used.

    A row exists only once somebody pins a model. Reading an owner who never
    pinned one is therefore a missing row, not a missing answer: the last-used
    half still comes from their newest turn.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the control plane's connection pool."""
        self._pool = pool

    async def get_model_preference(self, user_id: str) -> ModelPreferenceView:
        """Read the pin and the owner's newest turn's model together.

        Both halves are scoped to ``user_id``; a job belonging to somebody else
        can never supply the remembered model. The newest turn is the right
        source rather than the newest *finished* one: a task in flight is still
        the model the owner chose most recently.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT default_model FROM agent_user_model_preferences WHERE user_id = $1",
                user_id,
            )
            last_model = await conn.fetchval(
                "SELECT model FROM agent_jobs WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user_id,
            )
        pinned = row["default_model"] if row is not None else None
        return ModelPreferenceView.of(pinned, last_model)

    async def set_default_model(self, user_id: str, model: str | None) -> ModelPreferenceView:
        """Pin a model, or clear the pin, and answer with the resolved choice.

        One statement, so two tabs cannot leave the pin half-written. Clearing
        keeps the row (with ``default_model`` null) rather than deleting it:
        ``updated_at`` is then the honest time of the last decision.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_user_model_preferences (user_id, default_model)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE
                SET default_model = EXCLUDED.default_model, updated_at = now()
                """,
                user_id,
                model,
            )
            last_model = await conn.fetchval(
                "SELECT model FROM agent_jobs WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user_id,
            )
        return ModelPreferenceView.of(model, last_model)
