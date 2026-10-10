"""Postgres implementation of the The usage ledger port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.storage._sql.ledger import (
    LEDGER_WINDOW_UNTIL_SQL,
    fetch_job_usage_inputs,
    fetch_ledger_rows_for_conversation,
    fetch_ledger_rows_for_job,
    fetch_snapshot_version,
)

if TYPE_CHECKING:
    from datetime import datetime
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

    async def ledger_rows_for_conversation(
        self, *, job_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Read only this owner's conversation snapshots, including unstarted turns.

        Forks share a workspace but have distinct thread IDs and usage. Read
        cached totals in one query; a drawer must not fan out to the Gateway
        or load historical event payloads to calculate a conversation total.
        """
        async with self._pool.acquire() as conn:
            return await fetch_ledger_rows_for_conversation(conn, job_id, user_id)

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
                SELECT j.id, j.user_id, j.state, j.runtime
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

    async def queue_job_ledger_refresh(self, *, job_id: str) -> int:
        """Queue known references, sharing in-flight work and throttling retries."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE agent_attempt_ledger l
                SET next_refresh_at = GREATEST(
                    NOW(), l.last_refresh_attempt_at + INTERVAL '5 seconds'
                )
                FROM agent_attempts a
                WHERE a.id = l.attempt_id AND a.job_id = $1
                  AND (l.refresh_lease_expires_at IS NULL
                       OR l.refresh_lease_expires_at <= NOW())
                  AND (l.next_refresh_at IS NULL OR l.next_refresh_at > GREATEST(
                      NOW(), l.last_refresh_attempt_at + INTERVAL '5 seconds'
                  ))
                RETURNING l.id
                """,
                job_id,
            )
        return len(rows)

    async def next_ledger_refresh_due(self) -> datetime | None:
        """When the earliest scheduled refresh or expiring claim becomes due.

        A refresh scheduled while its row is claimed counts from the claim's
        expiry: the claiming worker reads it when that claim completes, so no
        other worker has anything to do before then. ``None`` when nothing is
        scheduled; an attempt that stops is announced by a notification instead.
        """
        async with self._pool.acquire() as conn:
            due: datetime | None = await conn.fetchval(
                """
                SELECT LEAST(
                    (SELECT MIN(next_refresh_at) FROM agent_attempt_ledger
                     WHERE next_refresh_at IS NOT NULL
                       AND refresh_lease_expires_at IS NULL),
                    (SELECT MIN(refresh_lease_expires_at) FROM agent_attempt_ledger
                     WHERE refresh_lease_expires_at IS NOT NULL)
                )
                """
            )
        return due

    async def claim_due_ledger_refresh(self, *, lease_seconds: float = 60) -> dict[str, Any] | None:
        """Fence one due snapshot query across control-plane workers.

        A crashed worker's expired lease is independently due, including
        running jobs for which there is no automatic periodic refresh.
        """
        if lease_seconds <= 0:
            raise ValueError("ledger refresh lease must be positive")
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                WITH due AS (
                    SELECT l.id, a.job_id, a.status <> 'running' AS terminal,
                           {LEDGER_WINDOW_UNTIL_SQL} AS window_until
                    FROM agent_attempt_ledger l
                    JOIN agent_attempts a ON a.id = l.attempt_id
                    JOIN agent_jobs j ON j.id = a.job_id
                    WHERE (l.refresh_lease_expires_at IS NULL
                           OR l.refresh_lease_expires_at <= NOW())
                      AND (l.next_refresh_at <= NOW()
                           OR l.refresh_lease_expires_at <= NOW()
                           OR (a.status <> 'running' AND l.terminal_refresh_count = 0
                               AND l.next_refresh_at IS NULL))
                    ORDER BY COALESCE(l.next_refresh_at, l.refresh_lease_expires_at,
                                      a.finished_at, a.started_at), l.id
                    LIMIT 1
                    FOR UPDATE OF l SKIP LOCKED
                )
                UPDATE agent_attempt_ledger l
                SET refresh_generation = l.refresh_generation + 1,
                    refresh_lease_expires_at = NOW() + make_interval(secs => $1),
                    last_refresh_attempt_at = NOW(), next_refresh_at = NULL
                FROM due
                WHERE l.id = due.id
                RETURNING l.id, l.attempt_id, l.grant_id, l.binding_id,
                          l.residency_generation, l.window_since, due.window_until,
                          due.terminal, l.terminal_refresh_count, l.refresh_generation,
                          due.job_id
                """,
                lease_seconds,
            )
        return None if row is None else dict(row)

    async def complete_ledger_refresh(
        self,
        *,
        ledger_id: int,
        refresh_generation: int,
        terminal: bool,
        report: dict[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        """Store one fenced result without discarding the last usable snapshot.

        Terminal queries get two delayed retries for late Gateway writes.
        Failures consume retries too; an unavailable Gateway must not create
        an unbounded loop. Early manual queries update the cache but cannot
        consume a later reconciliation: stages advance by the saved query
        start time, never the response completion time. A running query which
        finishes after the attempt ends does not count as a closed-window query.
        A read requested while a running query was in flight is kept for after
        it: that query may have started before the call it was asked for.
        """
        if (report is None) == (error_code is None):
            raise ValueError("a ledger refresh must have either a report or an error code")
        encoded = None if report is None else json.dumps(report)
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    WITH refresh AS (
                        SELECT l.id, a.finished_at,
                               CASE
                                   WHEN NOT $3 THEN l.terminal_refresh_count
                                   WHEN l.terminal_refresh_count = 0 THEN 1
                                   WHEN l.terminal_refresh_count = 1 AND (
                                       a.finished_at IS NULL
                                       OR l.last_refresh_attempt_at >=
                                           a.finished_at + INTERVAL '30 seconds'
                                   ) THEN 2
                                   WHEN l.terminal_refresh_count = 2 AND (
                                       a.finished_at IS NULL
                                       OR l.last_refresh_attempt_at >=
                                           a.finished_at + INTERVAL '300 seconds'
                                   ) THEN 3
                                   ELSE l.terminal_refresh_count
                               END AS next_stage
                        FROM agent_attempt_ledger l
                        JOIN agent_attempts a ON a.id = l.attempt_id
                        WHERE l.id = $1 AND l.refresh_generation = $2
                          AND l.refresh_lease_expires_at > NOW()
                    )
                    UPDATE agent_attempt_ledger l
                    SET snapshot = CASE WHEN $4::jsonb IS NULL THEN l.snapshot ELSE $4 END,
                        queried_at = CASE WHEN $4::jsonb IS NULL THEN l.queried_at ELSE NOW() END,
                        last_error_code = $5,
                        refresh_lease_expires_at = NULL,
                        terminal_refresh_count = refresh.next_stage,
                        next_refresh_at = CASE
                            -- The claim cleared it, so anything here was asked
                            -- for while this read ran: keep it for the next.
                            WHEN NOT $3 THEN l.next_refresh_at
                            WHEN refresh.next_stage = 1
                                THEN GREATEST(NOW(), refresh.finished_at + INTERVAL '30 seconds')
                            WHEN refresh.next_stage = 2
                                THEN GREATEST(NOW(), refresh.finished_at + INTERVAL '300 seconds')
                            ELSE NULL END
                    FROM refresh
                    WHERE l.id = refresh.id AND l.refresh_generation = $2
                      AND l.refresh_lease_expires_at > NOW()
                    RETURNING l.id
                    """,
                    ledger_id,
                    refresh_generation,
                    terminal,
                    encoded,
                    error_code,
                )
            )
