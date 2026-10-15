"""The residency half of a captured MCP attempt fence."""

from __future__ import annotations

from typing import Any


def residency_fence_sql(generation: str) -> str:
    """Fence aliases ``a`` and ``j`` using a trusted SQL generation expression."""
    return f"""({generation}::bigint IS NULL OR EXISTS (
        SELECT 1 FROM agent_thread_sandbox_memberships m
        JOIN agent_sandboxes g ON g.id = m.sandbox_id
        JOIN agent_threads t ON t.id = m.thread_id
        WHERE m.thread_id = j.thread_id AND t.deleted_at IS NULL
          AND m.residency_generation = {generation}
          AND m.residency_status = 'hot'
          AND m.residency_lease_expires_at > clock_timestamp()
          AND m.active_attempt_id = a.id AND a.host_id = g.host
    ))"""


async def lock_mcp_residency(
    conn: Any, *, job_id: str, attempt_id: int, generation: int | None
) -> bool:
    """Hold the residency through a write after its attempt and job are locked.

    Rotation and release both update the membership, so they either precede
    this check or wait for the transaction's write. Read the wall clock again
    after the lock, including when another membership writer made us wait.
    """
    if generation is None:
        return True
    locked = await conn.fetchval(
        """
        SELECT m.thread_id FROM agent_thread_sandbox_memberships m
        JOIN agent_jobs j ON j.thread_id = m.thread_id
        WHERE j.id = $1 FOR SHARE OF m
        """,
        job_id,
    )
    if locked is None:
        return False
    return bool(
        await conn.fetchval(
            f"""
            SELECT EXISTS (
                SELECT 1 FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE j.id = $1 AND a.id = $2
                  AND a.lease_expires_at > clock_timestamp()
                  AND {residency_fence_sql("$3")}
            )
            """,
            job_id,
            attempt_id,
            generation,
        )
    )
