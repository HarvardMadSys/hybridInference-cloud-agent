"""Postgres implementation of the Host capacity observations and their history port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.capacity_protocol import (
    CAPACITY_HISTORY_MAX_AGE_S,
    CAPACITY_HISTORY_RETENTION_S,
    history_window,
)

if TYPE_CHECKING:
    from typing import Any

    import asyncpg

    from cloud_agent.capacity_protocol import CapacityHistoryPointWrite


class PostgresHostCapacityRepository:
    """Host capacity: what each machine is actually doing."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def record_capacity_history_point(
        self,
        *,
        host: str,
        capacity_epoch: Any,
        point: CapacityHistoryPointWrite,
    ) -> bool:
        """Keep one observation for a host, epoch and minute.

        ``ON CONFLICT DO NOTHING`` against both the primary key and the sample
        uniqueness constraint, and that is the whole correctness argument: the
        first fresh observation selected for a minute is the one that is kept.
        Later ones in the same minute are not averaged, not summed and not
        allowed to replace it — three runner replicas report independently, and
        a chart built from whichever of them wrote last would move for reasons
        that have nothing to do with the machine.

        Args:
            host: The host the observation belongs to.
            capacity_epoch: The generation it was observed under. Passed
                through from the row that was read, never re-derived, so a
                delayed write cannot stamp an old sample with a new epoch.
            point: The scalar observation.

        Returns:
            ``True`` when this call inserted the minute's point.
        """
        async with self._pool.acquire() as conn:
            inserted = await conn.fetchval(
                """
                INSERT INTO agent_host_capacity_history (
                    host, capacity_epoch, bucket_at, sampled_at, sample_id, linux_boot_id,
                    cpu_percent, cpu_logical_count, cpu_window_seconds, cpu_missing_reason,
                    memory_total_bytes, memory_available_bytes, memory_missing_reason
                )
                VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT DO NOTHING
                RETURNING bucket_at
                """,
                host,
                capacity_epoch,
                point.bucket_at,
                point.sampled_at,
                point.sample_id,
                point.linux_boot_id,
                point.cpu_percent,
                point.cpu_logical_count,
                point.cpu_window_seconds,
                point.cpu_missing_reason,
                point.memory_total_bytes,
                point.memory_available_bytes,
                point.memory_missing_reason,
            )
        return inserted is not None

    async def list_capacity_history_candidates(
        self, *, after_host: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return one page of hosts whose stored observation is still fresh.

        Keyset pagination on the host name, not ``OFFSET``: the history task
        walks the pool every minute, and an offset it restarts from would
        re-read the same prefix forever while the hosts behind it are never
        sampled.

        The snapshot's scalars are projected as text rather than cast in SQL.
        A cast would make one corrupt document fail the whole batch, and a
        batch that fails at the same row never gets past it; parsing happens
        where a bad value costs a single point.

        Args:
            after_host: Last host of the previous page, or ``None`` to start.
            limit: Page size ceiling.

        Returns:
            Rows ordered by host, each carrying its epoch, sample identity and
            projected scalars.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT host,
                       capacity_epoch,
                       capacity_sample_id,
                       capacity_sampled_at,
                       capacity_snapshot #>> '{linux_boot_id}' AS linux_boot_id,
                       capacity_snapshot #>> '{cpu_percent}' AS cpu_percent,
                       capacity_snapshot #>> '{cpu_logical_count}' AS cpu_logical_count,
                       capacity_snapshot #>> '{cpu_window_seconds}' AS cpu_window_seconds,
                       capacity_snapshot #>> '{missing,cpu}' AS cpu_missing_reason,
                       capacity_snapshot #>> '{memory_total_bytes}' AS memory_total_bytes,
                       capacity_snapshot #>> '{memory_available_bytes}' AS memory_available_bytes,
                       capacity_snapshot #>> '{missing,memory}' AS memory_missing_reason
                FROM agent_runner_hosts
                WHERE capacity_snapshot IS NOT NULL
                  AND capacity_sample_id IS NOT NULL
                  AND capacity_sampled_at IS NOT NULL
                  AND capacity_sampled_at
                      >= NOW() - make_interval(secs => $2::double precision)
                  AND host > COALESCE($1, '')
                ORDER BY host
                LIMIT $3
                """,
                after_host,
                CAPACITY_HISTORY_MAX_AGE_S,
                limit,
            )
        return [dict(row) for row in rows]

    async def prune_capacity_history(
        self, *, older_than_seconds: float = CAPACITY_HISTORY_RETENTION_S, limit: int = 2000
    ) -> int:
        """Delete one bounded batch of history older than the retention window.

        Bounded because this runs beside the reaper and the publisher on the
        same database: an unbounded ``DELETE`` after a control-plane outage
        would hold locks for as long as it took to remove the backlog, and the
        work it delayed is recovering jobs.

        Args:
            older_than_seconds: Retention window.
            limit: Rows this call may remove.

        Returns:
            How many rows were removed.
        """
        async with self._pool.acquire() as conn:
            # The delete is a data-modifying CTE and the count comes from the
            # outer SELECT. Returning the deleted rows and calling ``fetchval``
            # would yield the first row — that is, ``1`` for every batch — and
            # a retention pass that always reports one row looks like it is
            # keeping up while the table grows without bound.
            removed = await conn.fetchval(
                """
                WITH expired AS (
                    SELECT ctid FROM agent_host_capacity_history
                    WHERE bucket_at < NOW() - make_interval(secs => $1::double precision)
                    LIMIT $2
                ), removed AS (
                    DELETE FROM agent_host_capacity_history h
                    USING expired
                    WHERE h.ctid = expired.ctid
                    RETURNING 1
                )
                SELECT count(*) FROM removed
                """,
                older_than_seconds,
                limit,
            )
        return 0 if removed is None else int(removed)

    async def host_capacity_history(
        self, *, host: str, range_seconds: int
    ) -> dict[str, Any] | None:
        """Read one host's stored capacity history for the current epoch.

        The epoch and the points come from a single read-only repeatable-read
        view, which is what stops a re-enrollment landing between them from
        returning a window labelled with one generation and filled from
        another.

        Args:
            host: Enrollment name.
            range_seconds: Window length ending at the read's own minute.

        Returns:
            ``{"capacity_epoch", "since", "until", "observed_at", "rows"}``,
            or ``None`` when no such host exists. A host that has never
            reported capacity answers with an empty point list rather than
            ``None``: it exists, and "not measured" is a different answer from
            "not here".
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            observed_at = await conn.fetchval("SELECT NOW()")
            epoch = await conn.fetchval(
                "SELECT capacity_epoch FROM agent_runner_hosts WHERE host = $1", host
            )
            if epoch is None:
                return None
            since, until = history_window(now=observed_at, seconds=range_seconds)
            rows = await conn.fetch(
                """
                SELECT bucket_at, sampled_at, linux_boot_id, cpu_percent,
                       cpu_logical_count, cpu_window_seconds, cpu_missing_reason,
                       memory_total_bytes, memory_available_bytes, memory_missing_reason
                FROM agent_host_capacity_history
                WHERE host = $1 AND capacity_epoch = $2
                  AND bucket_at >= $3 AND bucket_at <= $4
                ORDER BY bucket_at
                """,
                host,
                epoch,
                since,
                until,
            )
        return {
            "capacity_epoch": epoch,
            "since": since,
            "until": until,
            "observed_at": observed_at,
            "rows": [dict(row) for row in rows],
        }
