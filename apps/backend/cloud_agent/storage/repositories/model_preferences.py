"""Postgres read of the model an owner's newest turn ran on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.model_preference import ModelPreferenceView

if TYPE_CHECKING:
    import asyncpg


#: The owner's newest turn's model and the connection it ran on.
_LAST_TURN_SQL = (
    "SELECT model, model_connection_id FROM agent_jobs WHERE user_id = $1 "
    "ORDER BY created_at DESC LIMIT 1"
)


class PostgresModelPreferenceRepository:
    """Read where an owner's newest turn ran, which a new task starts on."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the control plane's connection pool."""
        self._pool = pool

    async def get_model_preference(self, user_id: str) -> ModelPreferenceView:
        """Read the owner's newest turn's model and connection.

        Scoped to ``user_id``; a job belonging to somebody else can never
        supply the remembered model. The newest turn is the right source
        rather than the newest *finished* one: a task in flight is still the
        model the owner chose most recently.
        """
        async with self._pool.acquire() as conn:
            last = await conn.fetchrow(_LAST_TURN_SQL, user_id)
        if last is None:
            return ModelPreferenceView()
        return ModelPreferenceView(
            last_model=last["model"], last_model_connection_id=last["model_connection_id"]
        )
