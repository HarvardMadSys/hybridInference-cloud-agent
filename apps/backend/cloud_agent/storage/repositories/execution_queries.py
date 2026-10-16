"""Postgres implementation of the Execution reads port.

Lifted verbatim from the single storage class this package replaced. The
statements, their bound parameters and their transaction ownership are what
they were; only the class holding them changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.storage._sql.ledger import USAGE_EVENT_PAYLOAD_SQL, fetch_job_usage_inputs
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE

if TYPE_CHECKING:
    from typing import Any

    import asyncpg


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
                SELECT e.id,
                       e.job_id,
                       j.turn_no,
                       e.attempt_id,
                       e.seq,
                       e.event_type,
                       e.payload,
                       e.created_at
                FROM agent_job_events e
                JOIN agent_jobs j ON j.id = e.job_id
                WHERE j.thread_id = $1 AND e.id > $2
                ORDER BY e.id
                LIMIT $3
                """,
                thread_id,
                after_id,
                limit,
            )
        return [{**dict(row), "payload": load_json(row["payload"])} for row in rows]

    async def thread_stats_inputs(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return everything needed to summarize the thread containing a job.

        The event rows come back with their bulky text fields stripped in
        SQL: the summary reads identity, pairing and usage keys only, and a
        long thread's transcript is megabytes we would otherwise pull across
        the wire to throw away. Nothing is filtered by event type — wall
        time is measured from a turn's first to last event, so a Tier-2
        thread whose only events are ``raw`` still gets its duration.

        :returns: ``None`` when the job does not exist or is not the
            caller's, matching :meth:`get_thread_for_job`.
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
            jobs = await conn.fetch(
                "SELECT id FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no",
                thread_id,
            )
            attempts = await conn.fetch(
                """
                SELECT a.job_id, a.started_at
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE j.thread_id = $1
                """,
                thread_id,
            )
            events = await conn.fetch(
                f"""
                SELECT e.job_id,
                       e.attempt_id,
                       e.seq,
                       e.event_type,
                       e.created_at,
                       {USAGE_EVENT_PAYLOAD_SQL} AS payload
                FROM agent_job_events e
                JOIN agent_jobs j ON j.id = e.job_id
                WHERE j.thread_id = $1
                ORDER BY e.attempt_id, e.seq
                """,
                thread_id,
            )
        return {
            "thread_id": thread_id,
            "job_ids": [row["id"] for row in jobs],
            "attempts": [dict(row) for row in attempts],
            "events": [
                {
                    "job_id": row["job_id"],
                    "attempt_id": row["attempt_id"],
                    "seq": row["seq"],
                    "event_type": row["event_type"],
                    "created_at": row["created_at"],
                    "payload": load_json(row["payload"]),
                }
                for row in events
            ],
        }

    async def job_usage_inputs(self, *, job_id: str) -> dict[str, Any]:
        """Read runtime usage for this job's attempts, excluding sibling turns.

        Keep the same compact payload projection as thread_stats_inputs so
        output-presence detection and retry deduplication have identical
        semantics when compared with this job's Gateway snapshots.
        """
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            return await fetch_job_usage_inputs(conn, job_id)

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
        return [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "attempt_id": row["attempt_id"],
                "seq": row["seq"],
                "event_type": row["event_type"],
                "payload": load_json(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
