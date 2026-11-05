"""Bounded Postgres reads for cross-task usage aggregation.

One selection reads a lightweight job cohort and then its attempts' usage
inside a single repeatable-read transaction, so the totals and every grouping
come from one view of the database. An attempt's usage is its thread-stats
rollup: the agent's own report, cached against the attempt's newest event. A
missing or stale rollup is reduced from the attempt's events in the same read
and stored afterwards, so the next read and the thread statistics reuse it.

The statements below are module constants so that a plan review or the
measurement script exercises exactly what the route runs.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.ports.admin_queries import UsageQueryTooLarge, UsageQueryUnavailable
from cloud_agent.storage._sql.attempt_rollups import (
    partition_attempt_rollups,
    refresh_attempt_rollups,
    store_attempt_rollups,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    import asyncpg

    from cloud_agent.domain.thread_stats import AttemptStatsRollup

logger = get_logger(__name__)

#: How long one statement of a selection may run, in milliseconds.
STATEMENT_TIMEOUT_MS = 5_000

#: Metadata needed to select and group a cohort. A repository belongs to the
#: thread a run belongs to. Identities are returned whole: two models that share
#: their first 256 characters are still two groups, and a truncated key would
#: also produce a filter link the database cannot match. Rows are only as wide
#: as the stored identity, and the response budget bounds the total.
_JOB_COLUMNS = """
    j.id, j.user_id, j.thread_id, j.model, j.runtime, t.repo, j.state, j.created_at
"""

_FILTERS_SQL = """
      AND ($3::text IS NULL OR j.user_id = $3)
      AND ($4::text IS NULL OR j.model = $4)
      AND ($5::text IS NULL OR j.runtime = $5)
      AND ($6::text IS NULL OR t.repo = $6)
"""

#: The runs a selection counts, created in ``[$1, $2)`` and matching filters
#: ``$3`` to ``$6``. A Fork's copies of its source's turns are left out. They
#: are history copied for display, with no attempt of their own: counted, each
#: was one more run, its usage unavailable, beside the run it copies.
_SELECTED_RUNS_SQL = f"""
    FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id
    WHERE j.created_at >= $1 AND j.created_at < $2
      AND j.fork_source_job_id IS NULL
    {_FILTERS_SQL}
"""

_COHORT_SQL = f"""
    SELECT {_JOB_COLUMNS}
    {_SELECTED_RUNS_SQL}
    ORDER BY j.created_at, j.id
    LIMIT $7
"""

#: The same cohort, read in ``(created_at, id)`` order after a keyset and
#: optionally narrowed to some of the owner's conversations. A batched read
#: walks a history too large for one selection with it, every batch under the
#: same predicate as the single read, so the two cannot select different runs.
_COHORT_BATCH_SQL = f"""
    SELECT {_JOB_COLUMNS}
    {_SELECTED_RUNS_SQL}
      AND ($8::text[] IS NULL OR j.thread_id = ANY($8::text[]))
      AND ($9::timestamptz IS NULL OR (j.created_at, j.id) > ($9::timestamptz, $10::text))
    ORDER BY j.created_at, j.id
    LIMIT $7
"""

#: The earliest run the cohort predicate selects for one owner.
_FIRST_RUN_SQL = """
    SELECT MIN(j.created_at)
    FROM agent_jobs j
    WHERE j.user_id = $1 AND j.fork_source_job_id IS NULL
"""

#: How many attempts the selection holds, counted before any is read.
_ATTEMPT_COUNT_SQL = """
    SELECT COUNT(*)
    FROM agent_attempts a
    WHERE a.job_id = ANY($1::text[])
"""

#: Each attempt of the cohort, its newest event and its cached rollup.
_ATTEMPT_ROLLUPS_SQL = """
    SELECT a.job_id,
           a.id AS attempt_id,
           a.attempt_no,
           latest.event_seq,
           rollup.event_seq AS rollup_event_seq,
           rollup.reducer_version,
           rollup.rollup
    FROM agent_attempts a
    LEFT JOIN LATERAL (
        SELECT e.seq AS event_seq
        FROM agent_job_events e
        WHERE e.attempt_id = a.id
        ORDER BY e.seq DESC
        LIMIT 1
    ) latest ON TRUE
    LEFT JOIN agent_attempt_thread_stats rollup ON rollup.attempt_id = a.id
    WHERE a.job_id = ANY($1::text[])
    ORDER BY a.job_id, a.attempt_no
"""


#: The smallest batch a batched read shrinks to before refusing: a hundred
#: runs over the attempt budget is a pathological history, not a large one.
MIN_BATCH_RUNS = 100


class PostgresAdminUsageQueries:
    """Read-only aggregates over the Agent database's existing pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the pool the rest of the control plane already holds."""
        self._pool = pool

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[asyncpg.Connection]:
        """Hold one statement-timeout-bounded, repeatable read for a selection.

        The whole aggregation runs inside this transaction so that a snapshot
        written by the collector mid-request cannot split the summary from the
        breakdowns that are supposed to add up to it.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            yield conn

    async def selection(
        self,
        *,
        since: datetime,
        until: datetime,
        user_id: str | None,
        model: str | None,
        runtime: str | None,
        repo: str | None,
        max_runs: int,
        max_attempts: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read one selection's jobs and their attempts' usage from one view.

        Every statement shares one read-only repeatable-read transaction, so
        the job metadata the aggregate counts and the usage it sums can only
        describe the same instant: never a run still ``running`` beside the
        usage its finished attempt had already reported.

        The cohort is read first, then its attempts are counted before any is
        read. An oversized selection is refused rather than trimmed, so no total
        is quietly computed from a subset of the runs that matched.

        Returns:
            The cohort rows, and one row per attempt with ``job_id``,
            ``attempt_id``, ``attempt_no``, ``saw_usage`` and its token counters.

        Raises:
            UsageQueryTooLarge: If the selection exceeds any budget.
            UsageQueryUnavailable: If a statement fails or exceeds its timeout.
        """
        try:
            async with self._read() as conn:
                cohort = await self._cohort(
                    conn,
                    since=since,
                    until=until,
                    user_id=user_id,
                    model=model,
                    runtime=runtime,
                    repo=repo,
                    max_runs=max_runs,
                )
                attempts, rollups, stale = await self._attempt_usage(
                    conn, cohort=cohort, max_attempts=max_attempts
                )
        except UsageQueryTooLarge:
            raise
        except Exception as error:
            raise UsageQueryUnavailable from error
        if stale:
            await self._keep_rollups(stale, rollups)
        return cohort, _attempt_rows(attempts, rollups)

    async def _cohort(
        self,
        conn: asyncpg.Connection,
        *,
        since: datetime,
        until: datetime,
        user_id: str | None,
        model: str | None,
        runtime: str | None,
        repo: str | None,
        max_runs: int,
    ) -> list[dict[str, Any]]:
        """Read the selected jobs, refusing one over the run budget.

        Raises:
            UsageQueryTooLarge: If more runs matched than the budget allows.
        """
        cohort = [
            dict(row)
            for row in await conn.fetch(
                _COHORT_SQL,
                since,
                until,
                user_id,
                model,
                runtime,
                repo,
                max_runs + 1,
            )
        ]
        if len(cohort) > max_runs:
            raise UsageQueryTooLarge
        return cohort

    async def _attempt_usage(
        self,
        conn: asyncpg.Connection,
        *,
        cohort: list[dict[str, Any]],
        max_attempts: int,
    ) -> tuple[list[dict[str, Any]], list[AttemptStatsRollup], list[dict[str, Any]]]:
        """Count, then read, this cohort's attempts and their rollups.

        Raises:
            UsageQueryTooLarge: If the selection holds more attempts than allowed.
        """
        job_ids = [row["id"] for row in cohort]
        if int(await conn.fetchval(_ATTEMPT_COUNT_SQL, job_ids)) > max_attempts:
            raise UsageQueryTooLarge
        rows = await conn.fetch(_ATTEMPT_ROLLUPS_SQL, job_ids)
        attempts = [
            {
                "job_id": str(row["job_id"]),
                "attempt_id": int(row["attempt_id"]),
                "attempt_no": int(row["attempt_no"]),
            }
            for row in rows
        ]
        rollups, stale = partition_attempt_rollups(rows)
        if stale:
            rollups.extend(await refresh_attempt_rollups(conn, stale))
        return attempts, rollups, stale

    async def first_run_at(self, user_id: str) -> datetime | None:
        """When the owner's first run the cohort can select was created."""
        try:
            async with self._read() as conn:
                return await conn.fetchval(_FIRST_RUN_SQL, user_id)
        except Exception as error:
            raise UsageQueryUnavailable from error

    async def batched_selection(
        self,
        *,
        since: datetime,
        until: datetime,
        user_id: str,
        thread_ids: list[str] | None,
        max_runs: int,
        max_attempts: int,
        deadline: float,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        """Yield one owner's selection as consecutive bounded batches.

        Every batch is read in the same read-only repeatable-read transaction,
        so the batches together describe one instant exactly as a single
        selection would. A batch holds at most ``max_runs`` runs and
        ``max_attempts`` attempts; one that would not is retried at half the
        size, down to :data:`MIN_BATCH_RUNS`. Nothing is skipped: the batches
        partition every run the predicate selects. Rollups reduced on the way
        are stored once the read is over, as :meth:`selection` stores them.

        Yields:
            Each batch's cohort rows and their attempt rows, shaped as
            :meth:`selection` returns them.

        Raises:
            UsageQueryTooLarge: If even the smallest batch exceeds a budget.
            UsageQueryUnavailable: If a statement fails, times out, or the
                whole read passes ``deadline`` (a ``time.monotonic`` instant).
        """
        stale: list[dict[str, Any]] = []
        refreshed: list[AttemptStatsRollup] = []
        try:
            async with self._read() as conn:
                after: tuple[datetime, str] | None = None
                size = max_runs
                while True:
                    if time.monotonic() > deadline:
                        raise UsageQueryUnavailable
                    cohort = [
                        dict(row)
                        for row in await conn.fetch(
                            _COHORT_BATCH_SQL,
                            since,
                            until,
                            user_id,
                            None,
                            None,
                            None,
                            size,
                            thread_ids,
                            after[0] if after else None,
                            after[1] if after else None,
                        )
                    ]
                    if not cohort:
                        break
                    try:
                        attempts, rollups, batch_stale = await self._attempt_usage(
                            conn, cohort=cohort, max_attempts=max_attempts
                        )
                    except UsageQueryTooLarge:
                        if size <= MIN_BATCH_RUNS:
                            raise
                        size = max(MIN_BATCH_RUNS, size // 2)
                        continue
                    if batch_stale:
                        ids = {target["attempt_id"] for target in batch_stale}
                        stale.extend(batch_stale)
                        refreshed.extend(rollup for rollup in rollups if rollup.attempt_id in ids)
                    yield cohort, _attempt_rows(attempts, rollups)
                    if len(cohort) < size:
                        break
                    after = (cohort[-1]["created_at"], cohort[-1]["id"])
        except (UsageQueryTooLarge, UsageQueryUnavailable):
            raise
        except Exception as error:
            raise UsageQueryUnavailable from error
        if stale:
            await self._keep_rollups(stale, refreshed)

    async def _keep_rollups(
        self, stale: list[dict[str, Any]], rollups: list[AttemptStatsRollup]
    ) -> None:
        """Store the rollups this read reduced, so the next read reuses them.

        Outside the read-only view the totals came from. A failure costs the
        next read the same reduction, never this read's answer.
        """
        refreshed = {target["attempt_id"] for target in stale}
        try:
            async with self._pool.acquire() as conn:
                await store_attempt_rollups(
                    conn, stale, [rollup for rollup in rollups if rollup.attempt_id in refreshed]
                )
        except Exception:
            logger.warning("agent_usage_rollup_store_failed", exc_info=True)


def _attempt_rows(
    attempts: list[dict[str, Any]], rollups: list[AttemptStatsRollup]
) -> list[dict[str, Any]]:
    """Each attempt with its reported counters; one without events reported nothing."""
    by_attempt = {rollup.attempt_id: rollup for rollup in rollups}
    rows = []
    for attempt in attempts:
        rollup = by_attempt.get(attempt["attempt_id"])
        rows.append(
            {
                **attempt,
                "saw_usage": bool(rollup and rollup.saw_usage),
                "input_tokens": rollup.input_tokens if rollup else None,
                "output_tokens": rollup.output_tokens if rollup else None,
                "cache_read_tokens": rollup.cache_read_tokens if rollup else None,
                "cache_write_tokens": rollup.cache_write_tokens if rollup else None,
                "reasoning_tokens": rollup.reasoning_tokens if rollup else None,
            }
        )
    return rows
