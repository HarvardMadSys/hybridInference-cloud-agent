"""Per-attempt thread-stats rollups: reuse a cached one, refresh a stale one.

A rollup is one attempt's tool, timing and runtime-reported token facts,
cached against the attempt's newest event sequence. Thread statistics and the
usage pages read through these, so a turn's numbers are one reduction
wherever they are shown.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cloud_agent.storage._sql.ledger import USAGE_EVENT_PAYLOAD_SQL
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.thread_stats import (
    ATTEMPT_ROLLUP_VERSION,
    AttemptStatsAccumulator,
    AttemptStatsRollup,
    attempt_rollup_from_payload,
    attempt_rollup_payload,
)

THREAD_STATS_EVENT_PAGE_SIZE = 1_000


def partition_attempt_rollups(
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


async def refresh_attempt_rollups(
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


async def store_attempt_rollups(
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
