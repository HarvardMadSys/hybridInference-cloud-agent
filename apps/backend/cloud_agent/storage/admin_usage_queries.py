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

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.attempt_rollups import (
    partition_attempt_rollups,
    refresh_attempt_rollups,
    store_attempt_rollups,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from cloud_agent.thread_stats import AttemptStatsRollup

logger = get_logger(__name__)

#: Metadata needed to select and group a cohort. A repository belongs to the
#: thread a run belongs to. Identities are returned whole: two models that share
#: their first 256 characters are still two groups, and a truncated key would
#: also produce a filter link the database cannot match. Rows are only as wide
#: as the stored identity, and the response budget bounds the total.
_JOB_COLUMNS = """
    j.id, j.user_id, j.model, j.runtime, t.repo, j.state, j.created_at
"""

_FILTERS_SQL = """
      AND ($3::text IS NULL OR j.user_id = $3)
      AND ($4::text IS NULL OR j.model = $4)
      AND ($5::text IS NULL OR j.runtime = $5)
      AND ($6::text IS NULL OR t.repo = $6)
"""

#: The runs a selection counts, created in ``[$1, $2)`` and matching filters
#: ``$3`` to ``$6``. A Fork's copies of its source's turns are left out. They
#: are history copied for display, with no attempt and no ledger reference:
#: counted, each was one more run, its usage unavailable, beside the run it
#: copies. The cohort and the run bounds share this text, so a period the
#: bounds narrow to the run budget is one the cohort then fits.
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

#: When the first selected run was created.
_EARLIEST_RUN_SQL = f"""
    SELECT MIN(j.created_at)
    {_SELECTED_RUNS_SQL}
"""

#: When the newest selected run past the first ``$7`` was created, counting
#: back from ``$2``: every run created after it fits a budget of ``$7`` runs.
_OVERFLOW_RUN_SQL = f"""
    SELECT j.created_at
    {_SELECTED_RUNS_SQL}
    ORDER BY j.created_at DESC
    OFFSET $7
    LIMIT 1
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


#: The lower bound of a selection that has none: every stored run is after it.
_BEGINNING = datetime.min.replace(tzinfo=UTC)


class UsageQueryTooLarge(Exception):
    """The selection exceeds a published bound and is refused as a whole."""


class UsageQueryUnavailable(Exception):
    """The selection could not be read within its bounds."""


class AdminUsageQueries:
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
            await conn.execute("SET LOCAL statement_timeout = '5s'")
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

    async def owner_run_bounds(
        self, user_id: str, *, until: datetime, max_runs: int
    ) -> tuple[datetime | None, datetime | None]:
        """Where an owner's all-time period can start, from the runs a selection counts.

        Both statements read the same runs the cohort selects, through the same
        predicate, inside one read-only repeatable-read transaction.

        Returns:
            When the owner's first run before ``until`` was created, and when
            the newest run past the newest ``max_runs`` was created. Each is
            None when there is no such run: no runs at all, or all of them fit.

        Raises:
            UsageQueryUnavailable: If a statement fails or exceeds its timeout.
        """
        filters = (user_id, None, None, None)
        try:
            async with self._read() as conn:
                earliest = await conn.fetchval(_EARLIEST_RUN_SQL, _BEGINNING, until, *filters)
                overflow = await conn.fetchval(
                    _OVERFLOW_RUN_SQL, _BEGINNING, until, *filters, max_runs
                )
        except Exception as error:
            raise UsageQueryUnavailable from error
        return earliest, overflow

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
            }
        )
    return rows
