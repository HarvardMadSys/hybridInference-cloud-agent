"""What a running attempt reports: events, continuity, resources and turn summaries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent.storage._sql.event_batches import append_attempt_events
from cloud_agent.storage._sql.fences import leased_attempt_sql
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.deadlocks import retry_deadlock_victim
from cloud_agent.storage.repositories.attempt_commands._common import AttemptCommandsBase

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cloud_agent.ports.attempt_commands import TurnSummaryWrite


#: What the ordering rules decide, before the caller writes anything.
#: ``accept`` is this function's own answer and never leaves the repository:
#: the write either succeeds (``stored``) or the row moved under it
#: (``conflict``), and the caller only ever sees the port's four outcomes.
TurnSummaryDecision = Literal["accept", "duplicate", "conflict"]


class ReportsMixin(AttemptCommandsBase):
    """Record what a running attempt reports."""

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_seq: int | None = None,
    ) -> int | None:
        """Append one event, fenced by the live lease.

        Returns the global event id (the SSE ``Last-Event-ID`` cursor), or
        ``None`` when the caller lost the lease. The row lock on the attempt
        serializes ``seq`` assignment for the single legitimate writer.

        An append the database aborted as a deadlock's victim is tried again:
        its runner takes a refused append as a lost turn, and marks the chat's
        retained context lost.
        """
        stored = await retry_deadlock_victim(
            "append_event",
            lambda: self._append_events(
                attempt_id=attempt_id,
                lease_generation=lease_generation,
                events=((event_type, payload, event_seq),),
            ),
        )
        return None if stored is None else stored[0]

    async def append_events(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        events: Sequence[tuple[str, dict[str, Any] | None, int | None]],
    ) -> list[int] | None:
        """Append a runner's batch of events in order, in one transaction.

        One fence check and one commit, so one notification, for the whole
        batch, where each event used to cost all three. Each event is stored as
        :meth:`append_event` stores it, replays and side effects included, so a
        batch is retried whole: the events it already stored answer with their
        ids. Returns the ids in the order given, or ``None`` when the caller
        lost the lease. A deadlock's victim is tried again, as a single append is.
        """
        return await retry_deadlock_victim(
            "append_events",
            lambda: self._append_events(
                attempt_id=attempt_id, lease_generation=lease_generation, events=events
            ),
        )

    async def _append_events(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        events: Sequence[tuple[str, dict[str, Any] | None, int | None]],
    ) -> list[int] | None:
        """Append events in one transaction of their own."""
        async with self._pool.acquire() as conn, conn.transaction():
            return await append_attempt_events(
                conn, attempt_id=attempt_id, lease_generation=lease_generation, events=events
            )

    async def record_continuity_mode(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        mode: str | None,
        context_status: str | None = None,
    ) -> bool:
        """Stamp the diagnostic rung and optional product context semantic."""
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_live_attempt(conn, attempt_id, lease_generation):
                return False
            return bool(
                await conn.fetchval(
                    f"""
                    UPDATE agent_attempts
                    SET continuity_mode = COALESCE($3, continuity_mode),
                        context_status = COALESCE($4, context_status)
                    WHERE {leased_attempt_sql("$1", "$2", alias="")}
                    RETURNING id
                    """,
                    attempt_id,
                    lease_generation,
                    mode,
                    context_status,
                )
            )

    async def record_attempt_resources(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        rollup: dict[str, Any],
    ) -> bool:
        """Store one attempt's resource rollup, lease-fenced and idempotent.

        Fenced like every other worker write: a superseded attempt must not be
        able to overwrite what the live one consumed. Idempotent because the
        runner may retry the post — a rollup describes a closed window, so a
        repeat is the same window said twice, never a second window to add.

        Absent counters stay NULL. Current shared-member writers intentionally
        cannot assign whole-container totals to one member; historical sampler
        failures likewise must not be reported as a machine that used nothing.

        The per-call rows ride the same fence and the same transaction: they
        describe spans inside this window, so a caller that could not write
        the window must not be able to write its parts. They are replaced
        rather than merged, because the list is one statement about the whole
        attempt — a retry reporting fewer calls than a previous post means
        fewer calls, not a set to union with.
        """
        tool_calls = rollup.get("tool_calls") or []
        processes = rollup.get("processes") or []
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_live_attempt(conn, attempt_id, lease_generation):
                return False
            recorded = bool(
                await conn.fetchval(
                    f"""
                    WITH live AS (
                        SELECT id, job_id
                        FROM agent_attempts
                        WHERE {leased_attempt_sql("$1", "$2", alias="")}
                    )
                    INSERT INTO agent_attempt_resources (
                        attempt_id, job_id, cpu_usec, mem_peak_bytes, mem_avg_bytes,
                        io_read_bytes, io_write_bytes, net_rx_bytes, net_tx_bytes,
                        sample_count, coverage, sampler_version, tool_calls_truncated,
                        tool_time_ms, process_observer, process_observation_complete,
                        process_events_lost, processes_truncated
                    )
                    SELECT live.id, live.job_id, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                           $13, $14, $15, $16, $17, $18
                    FROM live
                    ON CONFLICT (attempt_id) DO UPDATE SET
                        cpu_usec = EXCLUDED.cpu_usec,
                        mem_peak_bytes = EXCLUDED.mem_peak_bytes,
                        mem_avg_bytes = EXCLUDED.mem_avg_bytes,
                        io_read_bytes = EXCLUDED.io_read_bytes,
                        io_write_bytes = EXCLUDED.io_write_bytes,
                        net_rx_bytes = EXCLUDED.net_rx_bytes,
                        net_tx_bytes = EXCLUDED.net_tx_bytes,
                        sample_count = EXCLUDED.sample_count,
                        coverage = EXCLUDED.coverage,
                        sampler_version = EXCLUDED.sampler_version,
                        tool_calls_truncated = EXCLUDED.tool_calls_truncated,
                        tool_time_ms = EXCLUDED.tool_time_ms,
                        process_observer = EXCLUDED.process_observer,
                        process_observation_complete = EXCLUDED.process_observation_complete,
                        process_events_lost = EXCLUDED.process_events_lost,
                        processes_truncated = EXCLUDED.processes_truncated
                    RETURNING attempt_id
                    """,
                    attempt_id,
                    lease_generation,
                    rollup.get("cpu_usec"),
                    rollup.get("mem_peak_bytes"),
                    rollup.get("mem_avg_bytes"),
                    rollup.get("io_read_bytes"),
                    rollup.get("io_write_bytes"),
                    rollup.get("net_rx_bytes"),
                    rollup.get("net_tx_bytes"),
                    int(rollup.get("sample_count") or 0),
                    float(rollup.get("coverage") or 0.0),
                    str(rollup.get("sampler_version") or "unknown"),
                    bool(rollup.get("tool_calls_truncated")),
                    rollup.get("tool_time_ms"),
                    rollup.get("process_observer"),
                    bool(rollup.get("process_observation_complete")),
                    int(rollup.get("process_events_lost") or 0),
                    bool(rollup.get("processes_truncated")),
                )
            )
            if not recorded:
                return False
            await conn.execute(
                "DELETE FROM agent_tool_call_resources WHERE attempt_id = $1",
                attempt_id,
            )
            if tool_calls:
                await conn.executemany(
                    """
                    INSERT INTO agent_tool_call_resources (
                        attempt_id, call_seq, tool_use_id, tool_name, timing_source,
                        duration_ms, sample_count, overlap_n, measured_span_ms,
                        cpu_usec_exclusive, cpu_usec_shared,
                        mem_peak_exclusive_bytes, mem_peak_shared_bytes, process_count,
                        process_cpu_usec, process_mem_peak_bytes,
                        process_io_read_bytes, process_io_write_bytes,
                        process_resources_partial
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                            $15, $16, $17, $18, $19)
                    """,
                    [
                        (
                            attempt_id,
                            int(call["call_seq"]),
                            str(call["tool_use_id"]),
                            str(call["tool_name"]),
                            str(call.get("timing_source") or "message"),
                            call.get("duration_ms"),
                            int(call.get("sample_count") or 0),
                            int(call.get("overlap_n") or 1),
                            call.get("measured_span_ms"),
                            call.get("cpu_usec_exclusive"),
                            call.get("cpu_usec_shared"),
                            call.get("mem_peak_exclusive_bytes"),
                            call.get("mem_peak_shared_bytes"),
                            int(call.get("process_count") or 0),
                            call.get("process_cpu_usec"),
                            call.get("process_mem_peak_bytes"),
                            call.get("process_io_read_bytes"),
                            call.get("process_io_write_bytes"),
                            bool(call.get("process_resources_partial")),
                        )
                        for call in tool_calls
                    ],
                )
            await conn.execute(
                "DELETE FROM agent_process_observations WHERE attempt_id = $1",
                attempt_id,
            )
            if processes:
                await conn.executemany(
                    """
                    INSERT INTO agent_process_observations (
                        attempt_id, process_seq, uid, pid, tgid, ppid, comm, executable,
                        started_offset_ms, duration_ms, exit_code,
                        observed_fork, observed_exec, observed_exit, tool_call_seqs,
                        cpu_usec, mem_peak_bytes, io_read_bytes, io_write_bytes,
                        io_rchar_bytes, io_wchar_bytes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            $14, $15, $16, $17, $18, $19, $20, $21)
                    """,
                    [
                        (
                            attempt_id,
                            int(process["process_seq"]),
                            int(process["uid"]),
                            int(process["pid"]),
                            int(process["tgid"]),
                            process.get("ppid"),
                            str(process["comm"]),
                            process.get("executable"),
                            int(process.get("started_offset_ms") or 0),
                            process.get("duration_ms"),
                            process.get("exit_code"),
                            bool(process.get("observed_fork")),
                            bool(process.get("observed_exec")),
                            bool(process.get("observed_exit")),
                            [int(seq) for seq in process.get("tool_call_seqs") or []],
                            process.get("cpu_usec"),
                            process.get("mem_peak_bytes"),
                            process.get("io_read_bytes"),
                            process.get("io_write_bytes"),
                            process.get("io_rchar_bytes"),
                            process.get("io_wchar_bytes"),
                        )
                        for process in processes
                    ],
                )
            return True

    async def record_attempt_turn_summary(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        measurement_id: str,
        report_seq: int,
        state: str,
        summary: dict[str, Any],
    ) -> TurnSummaryWrite:
        """Store one cumulative ``member_execution_window`` snapshot per attempt.

        Fenced like every other worker write: a superseded attempt must not be
        able to relabel what the live one consumed. The fence predicate is
        evaluated against the locked attempt row and the database clock, so a
        lease that expired a microsecond ago cannot write even if the reaper
        has not noticed it yet.

        Unlike :meth:`record_attempt_resources`, this body is a cumulative
        snapshot rather than a closed window. The runner posts checkpoints
        while the turn runs and a final one when it ends, and a delivery can be
        duplicated, reordered, or replayed after a retry. ``report_seq`` is
        therefore the ordering key and ``turn_summary_seq`` is what it was
        accepted at: a lower or equal-and-different sequence must never win,
        because that would let a stale checkpoint roll a measured window
        backwards. Equal sequences with a byte-identical payload are the same
        snapshot arriving twice, which is what a retry of an acknowledged post
        looks like.

        The first accepted snapshot fixes the attempt's ``measurement_id``.
        A later report naming a different measurement is not an update to the
        same window — it is a different window claiming the same attempt, and
        accepting it would silently splice two measurements together.

        This never touches the legacy aggregate columns or the tool-call and
        process child rows. Those describe what the container sampler and the
        process observer saw, which is independent evidence; this snapshot
        describes the member's own window. Neither replaces the other, so a
        turn may have either, both, or neither.

        When this runs first it inserts a legacy-compatible base row — the
        same "no container totals were attributed to this member" statement
        ``shared-member-v1`` already makes, never a fabricated measurement.

        Args:
            attempt_id: The attempt the snapshot describes.
            lease_generation: The caller's fencing generation for that attempt.
            measurement_id: The window identity fixed at the first accepted report.
            report_seq: Strictly monotonic per ``measurement_id``.
            state: ``collecting`` or ``final`` as the guest reported it.
            summary: The canonical snake_case snapshot to store.

        Returns:
            ``stored`` when this call accepted the snapshot, ``duplicate`` when
            the identical snapshot was already stored, ``conflict`` when the
            ordering rules refuse it, and ``lease_lost`` when the attempt is
            not running under this lease. Only ``stored`` and ``duplicate`` are
            success for the caller; the other two are not retryable.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Lock the attempt before reading its resource row. The lock is
            # what makes the read-decide-write below atomic: two concurrent
            # checkpoints of the same attempt serialize here, so the second
            # one sees what the first wrote instead of racing it.
            if not await self._lock_live_attempt(conn, attempt_id, lease_generation):
                return "lease_lost"
            fenced = await conn.fetchrow(
                "SELECT job_id FROM agent_attempts WHERE id = $1", attempt_id
            )
            if fenced is None:
                return "lease_lost"
            inserted = await conn.fetchval(
                """
                INSERT INTO agent_attempt_resources (
                    attempt_id, job_id, sample_count, coverage, sampler_version,
                    turn_summary, turn_summary_seq, turn_summary_received_at
                )
                VALUES ($1, $2, 0, 0, 'shared-member-v1', $3::jsonb, $4, NOW())
                ON CONFLICT (attempt_id) DO NOTHING
                RETURNING attempt_id
                """,
                attempt_id,
                fenced["job_id"],
                json.dumps(summary),
                report_seq,
            )
            if inserted is not None:
                return "stored"
            stored = await conn.fetchrow(
                """
                SELECT turn_summary, turn_summary_seq
                FROM agent_attempt_resources
                WHERE attempt_id = $1
                """,
                attempt_id,
            )
            if stored is None:
                # The row must exist after the INSERT above found a conflict.
                # Reaching here means it was deleted between the two
                # statements, and the safe answer is that nothing was written.
                return "conflict"
            stored_summary = load_json(stored["turn_summary"])
            stored_object = stored_summary if isinstance(stored_summary, dict) else {}
            decision = self._turn_summary_decision(
                stored_seq=stored["turn_summary_seq"],
                stored_summary=stored_summary,
                stored_measurement=stored_object.get("measurement_id"),
                stored_state=stored_object.get("state"),
                measurement_id=measurement_id,
                report_seq=report_seq,
                state=state,
                summary=summary,
            )
            if decision != "accept":
                return decision
            updated = await conn.fetchval(
                """
                UPDATE agent_attempt_resources
                SET turn_summary = $2::jsonb,
                    turn_summary_seq = $3,
                    turn_summary_received_at = NOW()
                WHERE attempt_id = $1
                RETURNING attempt_id
                """,
                attempt_id,
                json.dumps(summary),
                report_seq,
            )
            if updated is None:
                return "conflict"
            return "stored"

    @staticmethod
    def _turn_summary_decision(
        *,
        stored_seq: int | None,
        stored_summary: Any,
        stored_measurement: Any,
        stored_state: Any,
        measurement_id: str,
        report_seq: int,
        state: str,
        summary: dict[str, Any],
    ) -> TurnSummaryDecision:
        """Whether an incoming snapshot may replace the stored one, and how.

        Separated from the statement so the ordering rules read as rules
        rather than as one long SQL condition, and so each refusal is a named
        decision: the caller gets ``conflict`` for every one of them, because
        none of them is a transient failure worth retrying.
        """
        if stored_seq is None:
            # The row exists for the legacy accounting only. The first
            # snapshot may take it over, and nothing can conflict with a
            # sequence that was never recorded.
            return "accept"
        if report_seq < stored_seq:
            return "conflict"
        if report_seq == stored_seq:
            # Same sequence means the same snapshot. A different payload at
            # the same sequence is two states claiming one position, and
            # last-write-wins would let whichever arrived second decide.
            # An identical payload is the retry this whole path exists for.
            return "duplicate" if stored_summary == summary else "conflict"
        # From here the incoming snapshot is newer, which still does not make
        # it authoritative: these two rules outrank recency.
        if stored_state == "final" and state == "collecting":
            return "conflict"
        if stored_measurement is not None and stored_measurement != measurement_id:
            return "conflict"
        return "accept"
