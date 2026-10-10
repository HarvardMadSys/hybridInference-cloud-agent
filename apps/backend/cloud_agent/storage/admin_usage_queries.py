"""Bounded Postgres reads for cross-task usage aggregation.

One selection reads a lightweight job cohort and then its attempt and ledger
rows inside a single repeatable-read transaction, so the totals and every
grouping come from one view of the database. Snapshots are projected through an
explicit protocol allowlist: optional keys keep their absence, and a damaged
report stays damaged instead of being repaired into a valid one.

The three statements below are module constants so that a plan review or the
measurement script exercises exactly what the route runs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    import asyncpg


#: Metadata needed to select and group a cohort. A repository belongs to the
#: thread a run belongs to. Identities are returned whole: two models that share
#: their first 256 characters are still two groups, and a truncated key would
#: also produce a filter link the database cannot match. Rows are only as wide
#: as the stored identity, and the response budget bounds the total.
_JOB_COLUMNS = """
    j.id, j.user_id, j.model, j.runtime, t.repo, j.state, j.created_at
"""

#: The protocol keys of a stored report. A key outside this list is never read,
#: whatever a future producer decides to add to the snapshot.
_SNAPSHOT_KEYS = (
    "schema_version",
    "source",
    "grant_id",
    "external_job_id",
    "attribution",
    "window",
    "as_of",
    "finalized",
    "totals",
    "unknown_rows",
    "estimated_calls",
)


def _projection(source: str, keys: tuple[str, ...]) -> str:
    """Build a jsonb object of allowlisted keys, preserving which ones exist.

    A key the report never carried must stay absent rather than arrive as an
    explicit null: the reader treats a missing required key as a damaged report
    and an explicit null as a complete report that had nothing to report.
    ``jsonb_build_object`` turns a SQL null into a JSON null, so the pairs are
    aggregated and the ones the report does not carry are filtered out.
    """
    pairs = ", ".join(f"'{key}', {source}->'{key}'" for key in keys)
    return (
        f"(SELECT jsonb_object_agg(pair.key, pair.value)"
        f" FROM jsonb_each(jsonb_build_object({pairs})) AS pair(key, value)"
        f" WHERE {source} ? pair.key)"
    )


_SNAPSHOT_SQL = f"""
    CASE WHEN jsonb_typeof(l.snapshot) = 'object'
         THEN {_projection("l.snapshot", _SNAPSHOT_KEYS)} END AS snapshot
"""

#: Refreshing uses the same rule as the owner read, so the admin page never
#: invents its own refresh phases.
_LEDGER_COLUMNS = f"""
    -- ``id`` is the name the shared ledger reducer reads to tell an existing
    -- reference from a missing one; aliasing it here keeps one accounting path.
    l.id, l.grant_id, l.queried_at, l.last_error_code, l.next_refresh_at,
    {_SNAPSHOT_SQL.strip()},
    (l.id IS NOT NULL AND (
        l.refresh_lease_expires_at IS NOT NULL
        OR l.next_refresh_at <= NOW()
        OR (a.status <> 'running' AND l.terminal_refresh_count = 0
            AND l.next_refresh_at IS NULL)
    )) IS TRUE AS refreshing
"""

_FILTERS_SQL = """
      AND ($3::text IS NULL OR j.user_id = $3)
      AND ($4::text IS NULL OR j.model = $4)
      AND ($5::text IS NULL OR j.runtime = $5)
      AND ($6::text IS NULL OR t.repo = $6)
"""

_COHORT_SQL = f"""
    SELECT {_JOB_COLUMNS}
    FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id
    WHERE j.created_at >= $1 AND j.created_at < $2
    {_FILTERS_SQL}
    ORDER BY j.created_at, j.id
    LIMIT $7
"""

#: What the selection would cost, measured in Postgres so an oversized one is
#: refused before a single payload crosses the wire.
_BUDGET_SQL = """
    SELECT COUNT(*) AS row_count,
           COALESCE(SUM(OCTET_LENGTH(l.snapshot::text)), 0) AS snapshot_bytes
    FROM agent_jobs j
    JOIN agent_attempts a ON a.job_id = j.id
    LEFT JOIN agent_attempt_ledger l ON l.attempt_id = a.id
    WHERE j.id = ANY($1::text[])
      AND j.created_at >= $2 AND j.created_at < $3
"""

_PROJECTION_SQL = f"""
    SELECT j.id AS job_id, a.id AS attempt_id, a.attempt_no,
           COALESCE(OCTET_LENGTH(l.snapshot::text), 0) AS snapshot_bytes,
           {_LEDGER_COLUMNS}
    FROM agent_jobs j
    JOIN agent_attempts a ON a.job_id = j.id
    LEFT JOIN agent_attempt_ledger l ON l.attempt_id = a.id
    WHERE j.id = ANY($1::text[])
      AND j.created_at >= $2 AND j.created_at < $3
    ORDER BY j.created_at, j.id, a.attempt_no, l.id
"""


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
        max_rows: int,
        max_snapshot_bytes: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read one selection's jobs and their stored reports from one view.

        All three statements share one read-only repeatable-read transaction, so
        the job metadata the aggregate counts and the snapshots it sums can only
        describe the same instant. Reading them in two transactions produced
        combinations the database never held: a run still ``running`` beside the
        cost its finished attempt had already written.

        The cohort is read first, then its rows and snapshots are measured in
        Postgres before any payload is projected. An oversized selection is
        refused rather than trimmed, so no total is quietly computed from a
        subset of the runs that matched.

        Returns:
            The cohort rows, and the projected attempt and ledger rows.

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
                rows = await self._reports(
                    conn,
                    cohort=cohort,
                    since=since,
                    until=until,
                    max_rows=max_rows,
                    max_snapshot_bytes=max_snapshot_bytes,
                )
        except UsageQueryTooLarge:
            raise
        except Exception as error:
            raise UsageQueryUnavailable from error
        return (
            cohort,
            [
                {
                    **dict(row),
                    "snapshot": load_json(row["snapshot"]) if row["id"] is not None else None,
                }
                for row in rows
            ],
        )

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

    async def _reports(
        self,
        conn: asyncpg.Connection,
        *,
        cohort: list[dict[str, Any]],
        since: datetime,
        until: datetime,
        max_rows: int,
        max_snapshot_bytes: int,
    ) -> list[dict[str, Any]]:
        """Measure, then project, this cohort's attempts and stored reports.

        The measurement runs in Postgres so an oversized selection is refused
        before a single payload is transferred.

        Raises:
            UsageQueryTooLarge: If the selection exceeds either budget.
        """
        job_ids = [row["id"] for row in cohort]
        measured = (await conn.fetch(_BUDGET_SQL, job_ids, since, until))[0]
        if (
            int(measured["row_count"]) > max_rows
            or int(measured["snapshot_bytes"]) > max_snapshot_bytes
        ):
            raise UsageQueryTooLarge
        return list(await conn.fetch(_PROJECTION_SQL, job_ids, since, until))
