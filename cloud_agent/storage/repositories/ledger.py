"""Postgres implementation of the The usage ledger port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cloud_agent.storage._sql.ledger import LEDGER_WINDOW_UNTIL_SQL
from cloud_agent.storage._sql.rows import load_json

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
            rows = await conn.fetch(
                f"""
                SELECT a.id AS attempt_id, a.job_id, a.attempt_no,
                       a.status AS attempt_status, a.started_at, a.finished_at,
                       l.id, l.grant_id, l.residency_generation, l.binding_id,
                       l.window_since, {LEDGER_WINDOW_UNTIL_SQL} AS window_until,
                       l.snapshot, l.queried_at, l.last_error_code, l.next_refresh_at,
                       l.last_refresh_attempt_at, l.refresh_generation,
                       l.refresh_lease_expires_at, l.terminal_refresh_count,
                       (l.id IS NOT NULL AND (
                           l.refresh_lease_expires_at IS NOT NULL
                           OR l.next_refresh_at <= NOW()
                           OR (a.status <> 'running' AND l.terminal_refresh_count = 0
                               AND l.next_refresh_at IS NULL)
                       )) IS TRUE AS refreshing
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                LEFT JOIN agent_attempt_ledger l ON l.attempt_id = a.id
                WHERE a.job_id = $1
                ORDER BY a.attempt_no, l.id
                """,
                job_id,
            )
        return [{**dict(row), "snapshot": load_json(row["snapshot"])} for row in rows]

    async def ledger_rows_for_conversation(
        self, *, job_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        """Read only this owner's conversation snapshots, including unstarted turns.

        Forks share a workspace but have distinct thread IDs and usage. Read
        cached totals in one query; a drawer must not fan out to the Gateway
        or load historical event payloads to calculate a conversation total.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.id AS job_id, j.state AS job_state, a.id AS attempt_id, a.attempt_no,
                       l.id, l.grant_id, l.snapshot, l.queried_at,
                       l.last_error_code, l.next_refresh_at,
                       (l.id IS NOT NULL AND (
                           l.refresh_lease_expires_at IS NOT NULL
                           OR l.next_refresh_at <= NOW()
                           OR (a.status <> 'running' AND l.terminal_refresh_count = 0
                               AND l.next_refresh_at IS NULL)
                       )) IS TRUE AS refreshing
                FROM agent_jobs anchor
                JOIN agent_jobs j ON (j.thread_id = anchor.thread_id OR j.id = anchor.id)
                    AND j.user_id = $2
                LEFT JOIN agent_attempts a ON a.job_id = j.id
                LEFT JOIN agent_attempt_ledger l ON l.attempt_id = a.id
                WHERE anchor.id = $1 AND anchor.user_id = $2
                ORDER BY j.turn_no, a.attempt_no, l.id
                """,
                job_id,
                user_id,
            )
        return [{**dict(row), "snapshot": load_json(row["snapshot"])} for row in rows]

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
                            WHEN NOT $3 THEN NULL
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
