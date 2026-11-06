"""What the sandbox repository's mixins share: the pool, shared SQL and helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    import asyncpg

#: The logger the single module had, so records keep their logger name.
logger = get_logger(__package__)

#: Work a stop or deletion must wait for: a member turn or Fork, or a running
#: job. ``$1`` is the sandbox.
_GROUP_WORK_PENDING_SQL = """
SELECT EXISTS (
    SELECT 1 FROM agent_thread_sandbox_memberships m
    WHERE m.sandbox_id = $1 AND (
        m.active_attempt_id IS NOT NULL OR m.fork_operation_id IS NOT NULL
        OR EXISTS (
            SELECT 1 FROM agent_jobs j
            WHERE j.thread_id = m.thread_id AND j.state = 'running'
        )
    )
)
"""


class SandboxRepositoryBase:
    """The pool every mixin uses, and the helpers more than one of them calls."""

    _pool: asyncpg.Pool

    @staticmethod
    async def _record_trajectory_event(
        conn: asyncpg.Connection,
        *,
        group: Any,
        event_type: str,
        source: str,
        actor_id: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Commit content-free lifecycle context beside the mutation it explains."""
        await conn.execute(
            """
            INSERT INTO agent_sandbox_events (
                sandbox_id, generation, event_type, from_state, to_state,
                source, actor_id, details
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            group["id"],
            group["generation"],
            event_type,
            from_state,
            to_state,
            source,
            actor_id,
            json.dumps(details or {}),
        )
