"""Postgres implementation of the The usage ledger port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.ledger import (
    fetch_job_usage_inputs,
    fetch_ledger_rows_for_conversation,
    fetch_ledger_rows_for_job,
    fetch_snapshot_version,
)

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


class PostgresLedgerRepository:
    """The usage ledger and the refresh queue behind it."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def ledger_rows_for_job(self, *, job_id: str) -> list[dict[str, Any]]:
        """Read stored grant snapshots and retain attempts without attribution."""
        async with self._pool.acquire() as conn:
            return await fetch_ledger_rows_for_job(conn, job_id)

    async def job_usage_reads(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Everything an owner's read of a job's usage is made of, from one snapshot.

        ``job``, ``ledger_rows``, ``conversation_rows`` and ``inputs``, and the
        snapshot's ``version``: of two reads of a job's usage, the one whose
        version is greater saw everything the other did. ``None`` when the job
        is not this owner's, or its conversation was deleted.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            job = await conn.fetchrow(
                """
                SELECT j.id, j.user_id, j.state, j.runtime, j.model_connection_id
                FROM agent_jobs j
                LEFT JOIN agent_threads t ON t.id = j.thread_id
                WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
                """,
                job_id,
                user_id,
            )
            if job is None:
                return None
            return {
                "job": dict(job),
                "ledger_rows": await fetch_ledger_rows_for_job(conn, job_id),
                "conversation_rows": await fetch_ledger_rows_for_conversation(
                    conn, job_id, user_id
                ),
                "inputs": await fetch_job_usage_inputs(conn, job_id),
                "version": await fetch_snapshot_version(conn),
            }
