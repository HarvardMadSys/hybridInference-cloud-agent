"""Owner-scoped context reads: one conversation, and every conversation in a range.

Attempt profiles (``agent_attempt_context_profiles``, migration 0107) are a
derived cache of the event ledger. A read compares each attempt's newest
ledger sequence and the reducer version with its stored profile and derives
the stale ones from bounded, ordered event pages of the projection in
:mod:`cloud_agent.storage._sql.context`, writing them back without ever
regressing a newer row. Every statement names the owner: a conversation that
is not the caller's, or is deleted, reads as absent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.context_profile import CONTEXT_PROFILE_VERSION, ContextProfileAccumulator
from cloud_agent.storage._sql.context import CONTEXT_EVENT_PAYLOAD_SQL
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg

    from cloud_agent.context_insights import ContextInsightsCursor, ContextSort

#: Events one page of a profile refresh reads.
CONTEXT_EVENT_PAGE_SIZE = 1_000

_THREAD_ROWS_SQL = """
    /* thread_context_rows */
    WITH owned AS MATERIALIZED (
        SELECT j.thread_id
        FROM agent_jobs j
        JOIN agent_threads t ON t.id = j.thread_id
        WHERE j.id = $1 AND j.user_id = $2 AND t.deleted_at IS NULL
    )
    SELECT owned.thread_id,
           j.id AS job_id, j.turn_no, j.created_at, j.runtime, j.model, j.state,
           (j.fork_source_job_id IS NOT NULL) AS copied,
           length(j.task_prompt) AS prompt_chars,
           a.id AS attempt_id, a.attempt_no,
           latest.event_seq,
           p.event_seq AS profile_seq, p.reducer_version, p.profile
    FROM owned
    JOIN agent_jobs j ON j.thread_id = owned.thread_id AND j.user_id = $2
    LEFT JOIN agent_attempts a ON a.job_id = j.id
    LEFT JOIN LATERAL (
        SELECT e.seq AS event_seq
        FROM agent_job_events e
        WHERE e.attempt_id = a.id
        ORDER BY e.seq DESC
        LIMIT 1
    ) latest ON TRUE
    LEFT JOIN agent_attempt_context_profiles p ON p.attempt_id = a.id
    ORDER BY j.turn_no, a.attempt_no, a.id
"""

_EVENTS_SQL = f"""
    /* context_profile_events */
    SELECT e.id, e.seq, e.event_type, e.created_at,
           {CONTEXT_EVENT_PAYLOAD_SQL.strip()} AS payload
    FROM agent_job_events e
    WHERE e.attempt_id = $1
      AND ($2::integer IS NULL OR e.seq > $2)
      AND e.seq <= $3
    ORDER BY e.seq
    LIMIT $4
"""

_STORE_SQL = """
    /* context_profile_store */
    INSERT INTO agent_attempt_context_profiles
        (attempt_id, event_seq, reducer_version, main_requests, subagent_requests,
         peak_prompt_tokens, latest_prompt_tokens, latest_observed_at, context_window,
         compactions, profile)
    SELECT attempt_id, event_seq, reducer_version, main_requests, subagent_requests,
           peak_prompt_tokens, latest_prompt_tokens, latest_observed_at, context_window,
           compactions, profile
    FROM jsonb_to_recordset($1::jsonb) AS refreshed(
        attempt_id bigint,
        event_seq integer,
        reducer_version integer,
        main_requests integer,
        subagent_requests integer,
        peak_prompt_tokens bigint,
        latest_prompt_tokens bigint,
        latest_observed_at timestamptz,
        context_window bigint,
        compactions integer,
        profile jsonb
    )
    ON CONFLICT (attempt_id) DO UPDATE
    SET event_seq = EXCLUDED.event_seq,
        reducer_version = EXCLUDED.reducer_version,
        main_requests = EXCLUDED.main_requests,
        subagent_requests = EXCLUDED.subagent_requests,
        peak_prompt_tokens = EXCLUDED.peak_prompt_tokens,
        latest_prompt_tokens = EXCLUDED.latest_prompt_tokens,
        latest_observed_at = EXCLUDED.latest_observed_at,
        context_window = EXCLUDED.context_window,
        compactions = EXCLUDED.compactions,
        profile = EXCLUDED.profile,
        updated_at = NOW()
    WHERE agent_attempt_context_profiles.event_seq <= EXCLUDED.event_seq
"""

#: The owner's runs in a range, as the usage history selects them, on
#: conversations they can still open.
_RANGE_RUNS_SQL = """
    SELECT j.id, j.thread_id, j.created_at, j.runtime, j.model
    FROM agent_jobs j
    JOIN agent_threads t ON t.id = j.thread_id
    WHERE j.user_id = $1 AND j.created_at >= $2 AND j.created_at < $3
      AND j.fork_source_job_id IS NULL AND t.deleted_at IS NULL
"""

#: Attempts in the range whose profile is missing or stale, newest first.
_STALE_SQL = f"""
    /* context_insights_stale */
    WITH runs AS ({_RANGE_RUNS_SQL})
    SELECT a.id AS attempt_id, a.job_id, latest.event_seq
    FROM runs r
    JOIN agent_attempts a ON a.job_id = r.id
    CROSS JOIN LATERAL (
        SELECT e.seq AS event_seq
        FROM agent_job_events e
        WHERE e.attempt_id = a.id
        ORDER BY e.seq DESC
        LIMIT 1
    ) latest
    LEFT JOIN agent_attempt_context_profiles p ON p.attempt_id = a.id
    WHERE p.attempt_id IS NULL OR p.event_seq <> latest.event_seq
       OR p.reducer_version <> $4
    ORDER BY r.created_at DESC, a.id DESC
    LIMIT $5
"""

_PER_THREAD_SQL = f"""
    WITH runs AS ({_RANGE_RUNS_SQL}),
    per_thread AS (
        SELECT r.thread_id,
               COUNT(DISTINCT r.id) AS turns,
               MIN(r.created_at) AS first_turn_at,
               MAX(r.created_at) AS last_turn_at,
               ARRAY_AGG(DISTINCT r.runtime) AS runtimes,
               ARRAY_AGG(DISTINCT r.model) AS models,
               COUNT(a.id) AS attempts,
               -- An attempt without events has nothing to read: it is as read
               -- as one whose profile is stored.
               COUNT(a.id) FILTER (
                   WHERE p.attempt_id IS NOT NULL
                      OR NOT EXISTS (SELECT 1 FROM agent_job_events e WHERE e.attempt_id = a.id)
               ) AS attempts_profiled,
               COALESCE(SUM(p.main_requests), 0) AS main_requests,
               COALESCE(SUM(p.subagent_requests), 0) AS subagent_requests,
               MAX(p.peak_prompt_tokens) AS peak_prompt_tokens,
               (ARRAY_AGG(p.latest_prompt_tokens ORDER BY p.latest_observed_at DESC)
                    FILTER (WHERE p.latest_prompt_tokens IS NOT NULL))[1]
                   AS latest_prompt_tokens,
               MAX(p.latest_observed_at) AS latest_observed_at,
               MAX(p.context_window) AS context_window,
               COALESCE(SUM(p.compactions), 0) AS compactions
        FROM runs r
        LEFT JOIN agent_attempts a ON a.job_id = r.id
        LEFT JOIN agent_attempt_context_profiles p
            ON p.attempt_id = a.id AND p.reducer_version = $4
        GROUP BY r.thread_id
    ),
    ranked AS (
        SELECT pt.*,
               CASE $5::text
                   WHEN 'peak' THEN COALESCE(pt.peak_prompt_tokens, -1)
                   WHEN 'compactions' THEN pt.compactions
                   WHEN 'requests' THEN pt.main_requests
                   ELSE 0
               END AS sort_number
        FROM per_thread pt
    )
"""

_PAGE_SQL = f"""
    /* context_insights_page */
    {_PER_THREAD_SQL}
    SELECT ranked.*, t.title, t.repo, (t.archived_at IS NOT NULL) AS archived,
           (SELECT j.id FROM agent_jobs j
            WHERE j.thread_id = ranked.thread_id ORDER BY j.turn_no DESC LIMIT 1)
               AS latest_job_id
    FROM ranked
    JOIN agent_threads t ON t.id = ranked.thread_id
    WHERE $6::bigint IS NULL
       OR (ranked.sort_number, ranked.last_turn_at, ranked.thread_id)
          < ($6::bigint, $7::timestamptz, $8::text)
    ORDER BY ranked.sort_number DESC, ranked.last_turn_at DESC, ranked.thread_id DESC
    LIMIT $9
"""

_SUMMARY_SQL = f"""
    /* context_insights_summary */
    {_PER_THREAD_SQL}
    SELECT COUNT(*) AS conversations,
           COUNT(*) FILTER (WHERE peak_prompt_tokens IS NOT NULL OR compactions > 0)
               AS conversations_observed,
           COALESCE(SUM(main_requests), 0) AS main_requests,
           COALESCE(SUM(subagent_requests), 0) AS subagent_requests,
           COALESCE(SUM(compactions), 0) AS compactions,
           MAX(peak_prompt_tokens) AS peak_prompt_tokens,
           (ARRAY_AGG(thread_id ORDER BY peak_prompt_tokens DESC, thread_id)
                FILTER (WHERE peak_prompt_tokens IS NOT NULL))[1] AS peak_thread_id,
           (SELECT COUNT(*) FROM agent_jobs j JOIN agent_threads t ON t.id = j.thread_id
            WHERE j.user_id = $1 AND j.created_at >= $2 AND j.created_at < $3
              AND j.fork_source_job_id IS NULL AND t.deleted_at IS NOT NULL)
               AS deleted_conversation_runs
    FROM ranked
"""

_PENDING_SQL = f"""
    /* context_insights_pending */
    SELECT COUNT(*) FROM ({_STALE_SQL}) pending
"""


def _summary_record(target: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    summary = profile["summary"]
    return {
        "attempt_id": target["attempt_id"],
        "event_seq": target["event_seq"],
        "reducer_version": CONTEXT_PROFILE_VERSION,
        "main_requests": summary["main_requests"],
        "subagent_requests": summary["subagent_requests"],
        "peak_prompt_tokens": summary["peak_prompt_tokens"],
        "latest_prompt_tokens": summary["latest_prompt_tokens"],
        "latest_observed_at": summary["latest_observed_at"],
        "context_window": summary["context_window"],
        "compactions": summary["compactions"],
        "profile": profile,
    }


async def _profile_attempt(conn: Any, target: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Derive one attempt's profile from ordered pages; also the events read."""
    accumulator = ContextProfileAccumulator(
        attempt_id=target["attempt_id"], job_id=target["job_id"]
    )
    after: int | None = None
    read = 0
    while True:
        rows = await conn.fetch(
            _EVENTS_SQL, target["attempt_id"], after, target["event_seq"], CONTEXT_EVENT_PAGE_SIZE
        )
        if not rows:
            break
        read += len(rows)
        accumulator.extend({**dict(row), "payload": load_json(row["payload"])} for row in rows)
        page_end = int(rows[-1]["seq"])
        if after is not None and page_end <= after:
            raise RuntimeError("context profile event cursor did not advance")
        after = page_end
        if len(rows) < CONTEXT_EVENT_PAGE_SIZE or page_end >= target["event_seq"]:
            break
    return accumulator.finish(), read


async def refresh_profiles(
    conn: Any, targets: list[dict[str, Any]], *, event_budget: int | None = None
) -> dict[int, dict[str, Any]]:
    """Derive and store profiles for stale attempts; the new ones by attempt id.

    With ``event_budget``, no attempt is started once that many events were
    read; an attempt that was started is always finished, so no stored
    profile describes part of an attempt.
    """
    refreshed: dict[int, dict[str, Any]] = {}
    records = []
    read = 0
    for target in targets:
        if event_budget is not None and read >= event_budget:
            break
        profile, count = await _profile_attempt(conn, target)
        read += count
        refreshed[target["attempt_id"]] = profile
        records.append(_summary_record(target, profile))
    if records:
        await conn.execute(_STORE_SQL, json.dumps(records, default=str))
    return refreshed


class PostgresContextQueries:
    """The context reads behind the Context view and the Usage page's insights."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Share the existing pool."""
        self._pool = pool

    async def thread_context(self, *, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Jobs and current attempt profiles of the caller's conversation holding ``job_id``.

        None when the job is not the caller's or its conversation is deleted.
        Every stale attempt is derived before answering, so the view is whole.
        """
        async with self._pool.acquire() as conn:
            rows = [dict(row) for row in await conn.fetch(_THREAD_ROWS_SQL, job_id, user_id)]
            if not rows:
                return None
            stale = [
                {
                    "attempt_id": int(row["attempt_id"]),
                    "job_id": str(row["job_id"]),
                    "event_seq": int(row["event_seq"]),
                }
                for row in rows
                if row["attempt_id"] is not None
                and row["event_seq"] is not None
                and (
                    row["profile_seq"] != row["event_seq"]
                    or row["reducer_version"] != CONTEXT_PROFILE_VERSION
                )
            ]
            refreshed = await refresh_profiles(conn, stale) if stale else {}
        return _thread_result(rows, refreshed)

    async def insights_page(
        self,
        *,
        user_id: str,
        since: datetime,
        until: datetime,
        sort: ContextSort,
        cursor: ContextInsightsCursor | None,
        limit: int,
        refresh_attempts: int,
        refresh_events: int,
        pending_cap: int,
    ) -> dict[str, Any]:
        """Profile a bounded number of stale attempts, then read one ranked page.

        The page, the totals and the remaining count come from one read-only
        snapshot taken after the refresh, so they agree with each other.
        """
        async with self._pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '5s'")
            try:
                stale = [
                    dict(row)
                    for row in await conn.fetch(
                        _STALE_SQL, user_id, since, until, CONTEXT_PROFILE_VERSION, refresh_attempts
                    )
                ]
                refreshed = await refresh_profiles(conn, stale, event_budget=refresh_events)
            finally:
                await conn.execute("RESET statement_timeout")
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                await conn.execute("SET LOCAL statement_timeout = '5s'")
                page = [
                    dict(row)
                    for row in await conn.fetch(
                        _PAGE_SQL,
                        user_id,
                        since,
                        until,
                        CONTEXT_PROFILE_VERSION,
                        sort,
                        cursor.number if cursor else None,
                        cursor.at if cursor else None,
                        cursor.thread_id if cursor else None,
                        limit + 1,
                    )
                ]
                summary = dict(
                    await conn.fetchrow(
                        _SUMMARY_SQL, user_id, since, until, CONTEXT_PROFILE_VERSION, sort
                    )
                )
                pending = int(
                    await conn.fetchval(
                        _PENDING_SQL,
                        user_id,
                        since,
                        until,
                        CONTEXT_PROFILE_VERSION,
                        pending_cap + 1,
                    )
                )
        return {
            "page": page,
            "summary": summary,
            "pending": pending,
            "refreshed": len(refreshed),
        }


def _thread_result(
    rows: list[dict[str, Any]], refreshed: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    jobs: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    for row in rows:
        jobs.setdefault(
            str(row["job_id"]),
            {
                "job_id": str(row["job_id"]),
                "turn_no": row["turn_no"],
                "created_at": row["created_at"],
                "runtime": row["runtime"],
                "model": row["model"],
                "state": row["state"],
                "copied": row["copied"],
                "prompt_chars": row["prompt_chars"],
            },
        )
        if row["attempt_id"] is None:
            continue
        attempt_id = int(row["attempt_id"])
        if attempt_id in refreshed:
            profile: Any = refreshed[attempt_id]
        elif row["event_seq"] is None:
            profile = None
        else:
            profile = load_json(row["profile"])
        attempts.append(
            {"attempt_id": attempt_id, "job_id": str(row["job_id"]), "profile": profile}
        )
    return {
        "thread_id": str(rows[0]["thread_id"]),
        "jobs": list(jobs.values()),
        "attempts": attempts,
    }
