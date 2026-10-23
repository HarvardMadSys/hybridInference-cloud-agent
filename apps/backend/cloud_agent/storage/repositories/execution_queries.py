"""Postgres implementation of the Execution reads port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from cloud_agent.domain.execution import JobStreamState
from cloud_agent.storage._sql.ledger import USAGE_EVENT_PAYLOAD_SQL
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE
from cloud_agent.thread_stats import (
    ATTEMPT_ROLLUP_VERSION,
    AttemptStatsAccumulator,
    AttemptStatsRollup,
    attempt_rollup_from_payload,
    attempt_rollup_payload,
    summarize_thread_rollups,
)

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


THREAD_STATS_EVENT_PAGE_SIZE = 1_000


class PostgresExecutionQueries:
    """Reading events, attempts and telemetry for one thread or job."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def list_thread_events_after(
        self,
        *,
        job_id: str,
        user_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]] | None:
        """Page a whole thread's events, payloads intact.

        The per-job read beside this one is scoped to the turn being watched.
        Without this thread read, history keeps only durable messages, so
        everything an earlier turn *did* becomes invisible even though every
        row remains stored.

        Payloads are intentionally preserved: the caller needs tool inputs and
        outputs to reconstruct the historical activity. The result is paged so
        a long thread is not pulled into memory in one response.

        The page's ids are picked first (issue #810): the events of a thread
        are interleaved with every other thread's in `agent_job_events`, so
        ordering the full remaining scan fetched nearly every heap row to
        keep the first page's worth. The materialized inner query stops at
        the page boundary on the `(job_id, id)` index and only those rows'
        payloads are fetched, making each page cost its own size rather than
        the thread's remaining length.

        :returns: ``None`` when the job does not exist or is not the caller's,
            matching :meth:`get_thread_for_job`.
        """
        async with self._pool.acquire() as conn:
            thread_id = await conn.fetchval(
                "SELECT j.thread_id FROM agent_jobs j "
                "JOIN agent_threads t ON t.id = j.thread_id "
                "WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL",
                job_id,
                user_id,
            )
            if thread_id is None:
                return None
            rows = await conn.fetch(
                """
                WITH page AS MATERIALIZED (
                    SELECT e.id
                    FROM agent_job_events e
                    JOIN agent_jobs j ON j.id = e.job_id
                    WHERE j.thread_id = $1 AND e.id > $2
                    ORDER BY e.id
                    LIMIT $3
                )
                SELECT e.id,
                       e.job_id,
                       j.turn_no,
                       e.attempt_id,
                       e.seq,
                       e.event_type,
                       e.payload,
                       e.created_at
                FROM page
                JOIN agent_job_events e USING (id)
                JOIN agent_jobs j ON j.id = e.job_id
                ORDER BY e.id
                """,
                thread_id,
                after_id,
                limit,
            )
        return [{**dict(row), "payload": load_json(row["payload"])} for row in rows]

    async def thread_stats(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return a thread summary, refreshing only attempts whose ledger grew.

        The existing ``(attempt_id, seq)`` index gives each attempt's newest
        sequence without transferring an event row. A matching versioned
        rollup is reusable; only a missing or stale attempt loads its compact
        event projection. Late snapshot events invalidate the same way as
        streamed events, without adding work to either insert path.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                /* thread_stats_rollups */
                WITH owned AS MATERIALIZED (
                    SELECT j.thread_id
                    FROM agent_jobs j
                    JOIN agent_threads t ON t.id = j.thread_id
                    WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
                )
                SELECT owned.thread_id,
                       j.id AS job_id,
                       a.id AS attempt_id,
                       a.started_at,
                       latest.event_seq,
                       rollup.event_seq AS rollup_event_seq,
                       rollup.reducer_version,
                       rollup.rollup
                FROM owned
                JOIN agent_jobs j ON j.thread_id = owned.thread_id
                LEFT JOIN agent_attempts a ON a.job_id = j.id
                LEFT JOIN LATERAL (
                    SELECT e.seq AS event_seq
                    FROM agent_job_events e
                    WHERE e.attempt_id = a.id
                    ORDER BY e.seq DESC
                    LIMIT 1
                ) latest ON TRUE
                LEFT JOIN agent_attempt_thread_stats rollup ON rollup.attempt_id = a.id
                ORDER BY j.turn_no, a.id
                """,
                job_id,
                user_id,
            )
            if not rows:
                return None
            job_ids = list(dict.fromkeys(str(row["job_id"]) for row in rows))
            attempts = [
                {
                    "attempt_id": int(row["attempt_id"]),
                    "job_id": str(row["job_id"]),
                    "started_at": row["started_at"],
                }
                for row in rows
                if row["attempt_id"] is not None
            ]
            rollups, stale = _partition_attempt_rollups(rows)
            if stale:
                refreshed = await _refresh_attempt_rollups(conn, stale)
                rollups.extend(refreshed)
                await _store_attempt_rollups(conn, stale, refreshed)
        return {
            "thread_id": str(rows[0]["thread_id"]),
            "stats": summarize_thread_rollups(
                job_ids=job_ids,
                attempts=attempts,
                rollups=rollups,
            ),
        }

    async def job_attempts_telemetry(self, *, job_id: str) -> list[dict[str, Any]]:
        """Read lightweight attempt summaries without tool or process rows."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id AS attempt_id, attempt_no, continuity_mode, context_status,
                       started_at, finished_at
                FROM agent_attempts
                WHERE job_id = $1
                ORDER BY attempt_no
                """,
                job_id,
            )
            return [dict(row) for row in rows]

    async def trajectory_timings(
        self, *, job_ids: list[str], user_id: str, max_attempts: int
    ) -> dict[str, Any]:
        """Read aggregate timing for a bounded job batch in one statement.

        The metadata row survives an empty attempt set, an authorization
        mismatch and an oversized result. Callers can therefore distinguish
        all three without another ownership/count query. Tool and process
        detail stays on :meth:`job_turn_telemetry`; only exactness of the tool
        timing aggregate is reduced here.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH requested AS MATERIALIZED (
                    SELECT job_id, position
                    FROM unnest($1::text[]) WITH ORDINALITY AS request(job_id, position)
                ),
                owned AS MATERIALIZED (
                    SELECT request.job_id, request.position
                    FROM requested request
                    JOIN agent_jobs job ON job.id = request.job_id
                    JOIN agent_threads thread ON thread.id = job.thread_id
                    WHERE job.user_id = $2 AND thread.deleted_at IS NULL
                ),
                attempts AS MATERIALIZED (
                    SELECT owned.position, attempt.job_id,
                           attempt.id AS attempt_id, attempt.attempt_no,
                           attempt.continuity_mode, attempt.context_status,
                           attempt.started_at, attempt.finished_at
                    FROM owned
                    JOIN agent_attempts attempt ON attempt.job_id = owned.job_id
                ),
                tools AS MATERIALIZED (
                    SELECT call.attempt_id, COUNT(*) AS call_count,
                           BOOL_AND(call.timing_source = 'exact') AS all_exact
                    FROM attempts attempt
                    JOIN agent_tool_call_resources call
                        ON call.attempt_id = attempt.attempt_id
                    GROUP BY call.attempt_id
                ),
                counts AS (
                    SELECT (SELECT COUNT(*) FROM owned) AS owned_count,
                           (SELECT COUNT(*) FROM attempts) AS attempt_count
                )
                SELECT counts.owned_count, counts.attempt_count,
                       timing.position, timing.job_id,
                       timing.attempt_id, timing.attempt_no,
                       timing.continuity_mode, timing.context_status,
                       timing.started_at, timing.finished_at,
                       timing.tool_time_ms, timing.tool_calls_truncated,
                       timing.tool_timing_exact
                FROM counts
                LEFT JOIN LATERAL (
                    SELECT attempt.position, attempt.job_id,
                           attempt.attempt_id, attempt.attempt_no,
                           attempt.continuity_mode, attempt.context_status,
                           attempt.started_at, attempt.finished_at,
                           resources.tool_time_ms,
                           COALESCE(resources.tool_calls_truncated, FALSE)
                               AS tool_calls_truncated,
                           COALESCE(tools.call_count > 0 AND tools.all_exact, FALSE)
                               AS tool_timing_exact
                    FROM attempts attempt
                    LEFT JOIN agent_attempt_resources resources
                        ON resources.attempt_id = attempt.attempt_id
                    LEFT JOIN tools ON tools.attempt_id = attempt.attempt_id
                    ORDER BY attempt.position, attempt.attempt_no, attempt.attempt_id
                    LIMIT $3
                ) timing ON counts.owned_count = cardinality($1)
                    AND counts.attempt_count <= $3
                ORDER BY timing.position NULLS LAST,
                         timing.attempt_no NULLS LAST,
                         timing.attempt_id NULLS LAST
                """,
                job_ids,
                user_id,
                max_attempts,
            )
        first = rows[0]
        return {
            "owned_count": int(first["owned_count"]),
            "attempt_count": int(first["attempt_count"]),
            "attempts": [dict(row) for row in rows if row["attempt_id"] is not None],
        }

    async def get_attempt(self, *, job_id: str, attempt_id: int) -> dict[str, Any] | None:
        """Return one attempt of one job, by its exact id.

        Bounded to the job on purpose: an attempt id is unique on its own, but
        a caller that authenticated as this job has no business reading any
        other job's attempt, and putting the constraint in the query means the
        check cannot be forgotten by a caller.

        The generation is included because a credential names one, and a
        record that names it must be reporting on the attempt it belongs to.

        ``thread_sandbox`` is decoded here rather than left as the JSONB text
        asyncpg hands back for a ``to_jsonb`` projection. Callers read it as a
        mapping -- the ingest needs the sandbox and host that served *this*
        attempt -- and text fails at ``.get()`` with an ``AttributeError`` that
        only a real database produces.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT a.id AS attempt_id, a.job_id, a.attempt_no, a.lease_generation,
                       a.host_id, a.status, a.started_at, a.finished_at,
                       to_jsonb(s) AS thread_sandbox
                FROM agent_attempts a
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = a.thread_id
                WHERE a.id = $1 AND a.job_id = $2
                """,
                attempt_id,
                job_id,
            )
        if row is None:
            return None
        attempt = dict(row)
        attempt["thread_sandbox"] = load_json(attempt.get("thread_sandbox"))
        return attempt

    async def job_turn_telemetry(
        self, *, job_id: str, attempt_id: int | None = None
    ) -> dict[str, Any] | None:
        """Read one job-scoped attempt, defaulting to the newest attempt.

        Newest attempt rather than the current one: a retried turn's earlier
        attempts each have their own window, and the one a reader wants is the
        one that produced the result in front of them. One repeatable-read
        snapshot keeps the aggregate, per-call rows, and process rows from
        different rollup retries out of the same response. The child rows stay
        separate statements so counters arrive as numbers rather than as JSON
        this pool would hand back as text.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ),
        ):
            row = await conn.fetchrow(
                """
                SELECT a.id AS attempt_id, a.attempt_no,
                       a.continuity_mode, a.context_status,
                       a.started_at, a.finished_at,
                       r.cpu_usec, r.mem_peak_bytes, r.mem_avg_bytes,
                       r.io_read_bytes, r.io_write_bytes,
                       r.net_rx_bytes, r.net_tx_bytes,
                       r.sample_count, r.coverage, r.sampler_version, r.tool_time_ms,
                       r.process_observer,
                       COALESCE(r.process_observation_complete, FALSE)
                           AS process_observation_complete,
                       COALESCE(r.process_events_lost, 0) AS process_events_lost,
                       COALESCE(r.processes_truncated, FALSE) AS processes_truncated,
                       COALESCE(r.tool_calls_truncated, FALSE) AS tool_calls_truncated,
                       r.turn_summary, r.turn_summary_received_at,
                       (a.status <> 'running') AS attempt_terminal
                FROM agent_attempts a
                LEFT JOIN agent_attempt_resources r ON r.attempt_id = a.id
                WHERE a.job_id = $1
                  AND ($2::bigint IS NULL OR a.id = $2)
                ORDER BY a.attempt_no DESC
                LIMIT 1
                """,
                job_id,
                attempt_id,
            )
            if row is None:
                return None
            telemetry = dict(row)
            # The stored snapshot is JSONB, which this pool may hand back as
            # text; the reader needs the object, and its absence stays None
            # rather than becoming an empty one.
            telemetry["turn_summary"] = load_json(telemetry.get("turn_summary"))
            calls = await conn.fetch(
                """
                SELECT call_seq, tool_use_id, tool_name,
                       timing_source, duration_ms,
                       sample_count, overlap_n, measured_span_ms,
                       cpu_usec_exclusive, cpu_usec_shared,
                       mem_peak_exclusive_bytes, mem_peak_shared_bytes, process_count,
                       process_cpu_usec, process_mem_peak_bytes,
                       process_io_read_bytes, process_io_write_bytes,
                       process_resources_partial
                FROM agent_tool_call_resources
                WHERE attempt_id = $1
                ORDER BY call_seq
                """,
                telemetry["attempt_id"],
            )
            telemetry["tool_calls"] = [dict(call) for call in calls]
            processes = await conn.fetch(
                """
                SELECT process_seq, uid, pid, tgid, ppid, comm, executable,
                       started_offset_ms, duration_ms, exit_code,
                       observed_fork, observed_exec, observed_exit, tool_call_seqs,
                       cpu_usec, mem_peak_bytes, io_read_bytes, io_write_bytes,
                       io_rchar_bytes, io_wchar_bytes
                FROM agent_process_observations
                WHERE attempt_id = $1
                ORDER BY process_seq
                """,
                telemetry["attempt_id"],
            )
            telemetry["processes"] = [dict(process) for process in processes]
            return telemetry

    async def list_events_after(
        self,
        *,
        job_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return events for a job with a global id strictly greater than the cursor."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, job_id, attempt_id, seq, event_type, payload, created_at
                FROM agent_job_events
                WHERE job_id = $1 AND id > $2
                ORDER BY id
                LIMIT $3
                """,
                job_id,
                after_id,
                limit,
            )
        return [_stream_event(row) for row in rows]

    async def job_stream_page(
        self, *, job_id: str, user_id: str, after_id: int, limit: int
    ) -> tuple[list[dict[str, Any]], JobStreamState] | None:
        """One wake-up of a job's event stream: its next events and its state.

        A stream reads on every event its job stores, and reading the page and
        the state apart cost two pool checkouts and two statements a wake-up.
        One statement also gives both from the same snapshot. ``None`` when the
        job is not this owner's.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT j.state, j.published_pr_url,
                       e.id, e.job_id, e.attempt_id, e.seq, e.event_type, e.payload, e.created_at
                FROM agent_jobs j
                LEFT JOIN LATERAL (
                    SELECT id, job_id, attempt_id, seq, event_type, payload, created_at
                    FROM agent_job_events
                    WHERE job_id = j.id AND id > $3
                    ORDER BY id
                    LIMIT $4
                ) e ON TRUE
                WHERE j.id = $1 AND j.user_id = $2
                ORDER BY e.id
                """,
                job_id,
                user_id,
                after_id,
                limit,
            )
        if not rows:
            return None
        state = JobStreamState(rows[0]["state"], rows[0]["published_pr_url"])
        return [_stream_event(row) for row in rows if row["id"] is not None], state


def _stream_event(row: Any) -> dict[str, Any]:
    """One stored event as the job's event stream sends it."""
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "attempt_id": row["attempt_id"],
        "seq": row["seq"],
        "event_type": row["event_type"],
        "payload": load_json(row["payload"]),
        "created_at": row["created_at"],
    }


def _partition_attempt_rollups(
    rows: list[Any],
) -> tuple[list[AttemptStatsRollup], list[dict[str, Any]]]:
    """Separate reusable cache rows from attempts whose sequence changed."""
    cached: list[AttemptStatsRollup] = []
    stale: list[dict[str, Any]] = []
    for row in rows:
        attempt_id = row["attempt_id"]
        event_seq = row["event_seq"]
        if attempt_id is None or event_seq is None:
            continue
        target: dict[str, Any] = {
            "attempt_id": int(attempt_id),
            "job_id": str(row["job_id"]),
            "event_seq": int(event_seq),
        }
        if row["rollup_event_seq"] != event_seq or row["reducer_version"] != ATTEMPT_ROLLUP_VERSION:
            stale.append(target)
            continue
        payload = load_json(row["rollup"])
        if not isinstance(payload, Mapping):
            stale.append(target)
            continue
        try:
            cached.append(
                attempt_rollup_from_payload(
                    attempt_id=target["attempt_id"],
                    job_id=target["job_id"],
                    payload=payload,
                )
            )
        except (TypeError, ValueError):
            stale.append(target)
    return cached, stale


async def _refresh_attempt_rollups(
    conn: Any, stale: list[dict[str, Any]]
) -> list[AttemptStatsRollup]:
    """Reduce stale attempts from fixed-sequence, bounded ordered pages."""
    rollups: list[AttemptStatsRollup] = []
    for target in stale:
        accumulator = AttemptStatsAccumulator(
            attempt_id=target["attempt_id"],
            job_id=target["job_id"],
        )
        after_seq: int | None = None
        while True:
            events = await conn.fetch(
                f"""
                /* thread_stats_stale_events */
                SELECT e.job_id,
                       e.attempt_id,
                       e.seq,
                       e.event_type,
                       e.created_at,
                       {USAGE_EVENT_PAYLOAD_SQL} AS payload
                FROM agent_job_events e
                WHERE e.attempt_id = $1
                  AND e.seq >= COALESCE($2::integer, -2147483648)
                  AND ($2::integer IS NULL OR e.seq > $2)
                  AND e.seq <= $3
                ORDER BY e.seq
                LIMIT $4
                """,
                target["attempt_id"],
                after_seq,
                target["event_seq"],
                THREAD_STATS_EVENT_PAGE_SIZE,
            )
            if not events:
                break
            accumulator.extend(
                {
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                    "seq": row["seq"],
                    "event_type": row["event_type"],
                    "created_at": row["created_at"],
                    "payload": load_json(row["payload"]),
                }
                for row in events
            )
            page_end = int(events[-1]["seq"])
            if after_seq is not None and page_end <= after_seq:
                raise RuntimeError("thread stats event cursor did not advance")
            after_seq = page_end
            if len(events) < THREAD_STATS_EVENT_PAGE_SIZE or page_end >= target["event_seq"]:
                break
        rollups.append(accumulator.finish())
    return rollups


async def _store_attempt_rollups(
    conn: Any,
    stale: list[dict[str, Any]],
    rollups: list[AttemptStatsRollup],
) -> None:
    """Persist refreshed attempts in one statement without regressing a newer cache."""
    sequences = {target["attempt_id"]: target["event_seq"] for target in stale}
    records = [
        {
            "attempt_id": rollup.attempt_id,
            "event_seq": sequences[rollup.attempt_id],
            "reducer_version": ATTEMPT_ROLLUP_VERSION,
            "rollup": attempt_rollup_payload(rollup),
        }
        for rollup in rollups
    ]
    await conn.execute(
        """
        /* thread_stats_store_rollups */
        INSERT INTO agent_attempt_thread_stats
            (attempt_id, event_seq, reducer_version, rollup)
        SELECT attempt_id, event_seq, reducer_version, rollup
        FROM jsonb_to_recordset($1::jsonb)
             AS refreshed(
                 attempt_id bigint,
                 event_seq integer,
                 reducer_version integer,
                 rollup jsonb
             )
        ON CONFLICT (attempt_id) DO UPDATE
        SET event_seq = EXCLUDED.event_seq,
            reducer_version = EXCLUDED.reducer_version,
            rollup = EXCLUDED.rollup,
            updated_at = NOW()
        WHERE agent_attempt_thread_stats.event_seq <= EXCLUDED.event_seq
        """,
        json.dumps(records),
    )
