"""Inference-ledger capture and the window a refresh reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

if TYPE_CHECKING:
    import asyncpg


_LEDGER_WINDOW_UNTIL_SQL = """
LEAST(
    COALESCE(a.finished_at, NOW()),
    (
        SELECT MIN(next_attempt.started_at)
        FROM agent_attempts next_attempt
        JOIN agent_jobs next_job ON next_job.id = next_attempt.job_id
        WHERE next_job.thread_id = j.thread_id
          AND (next_attempt.started_at, next_attempt.id) > (a.started_at, a.id)
    )
)
"""


async def capture_attempt_ledger(
    conn: asyncpg.Connection, *, thread_id: str, attempt_id: int
) -> None:
    """Copy the exact residency grant inside a fenced admission transaction.

    The per-attempt grant is a separate, disposable credential. A NULL
    residency binding means an older writer never recorded its Gateway,
    so it deliberately leaves this attempt untracked. Re-admission keeps
    the first reference; rotation preserves an additional distinct grant.
    """
    await conn.execute(
        """
        INSERT INTO agent_attempt_ledger (
            attempt_id, grant_id, residency_generation, binding_id, window_since
        )
        SELECT a.id, r.grant_id, r.generation, r.ledger_binding_id, a.started_at
        FROM agent_thread_sandbox_memberships m
        JOIN agent_thread_residencies r ON r.thread_id = m.thread_id
            AND r.generation = m.residency_generation
            AND r.sandbox_id = m.sandbox_id
        JOIN agent_attempts a ON a.id = m.active_attempt_id
        JOIN agent_jobs j ON j.id = a.job_id AND j.thread_id = m.thread_id
        WHERE m.thread_id = $1 AND a.id = $2
          AND r.grant_id IS NOT NULL AND r.grant_id <> ''
          AND r.ledger_binding_id IS NOT NULL AND r.ledger_binding_id <> ''
        ON CONFLICT (attempt_id, grant_id) DO NOTHING
        """,
        thread_id,
        attempt_id,
    )
