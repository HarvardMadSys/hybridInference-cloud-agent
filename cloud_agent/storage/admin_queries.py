"""Explicit Postgres projections for cross-user admin observation reads.

The default paths never fetch prompts, arbitrary event payloads or raw errors.
Content access is deliberately a separate, single-event operation. All SQL
identifiers are source constants; user selectors are bound parameters.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from cloud_agent.admin_observability import (
    ActivityCursor,
    AdminActivity,
    AdminActivityPage,
    AdminRun,
    AdminRunList,
    RunCursor,
    RunFilters,
    cursor_scope,
    encode_cursor,
)
from cloud_agent.schemas import AttemptResources, ProcessObservation, ToolCallResources
from cloud_agent.storage.store import _load_json
from cloud_agent.thread_stats import _TOKEN_KEY_GROUPS
from cloud_agent_host.event_payloads import MAX_EVENT_PAYLOAD_BYTES, bound_event_payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    import asyncpg


_RUN_COLUMNS = """
    j.id, j.thread_id,
    CASE WHEN t.title_source IN ('generated', 'manual')
         THEN LEFT(t.title, 160) END AS title,
    j.turn_no,
    LEFT(j.user_id, 256) AS user_id, LEFT(t.repo, 256) AS repo,
    LEFT(j.runtime, 128) AS runtime, LEFT(j.model, 256) AS model,
    LEFT(j.reasoning_effort, 64) AS reasoning_effort,
    t.execution_profile, j.state, j.created_at, j.updated_at,
    ARRAY(SELECT DISTINCT LEFT(a.host_id, 256) FROM agent_attempts a
          WHERE a.job_id = j.id AND a.host_id IS NOT NULL
          ORDER BY 1) AS hosts,
    (SELECT COUNT(*) FROM agent_job_events e
     WHERE e.job_id = j.id AND e.event_type = 'tool_use') AS tool_calls
"""

_EVENT_COLUMNS = """
    e.id, e.attempt_id, a.attempt_no, e.seq,
    LEFT(e.event_type, 64) AS event_type, e.created_at,
    CASE WHEN e.event_type = 'tool_use'
              AND jsonb_typeof(e.payload->'id') = 'string'
              AND LENGTH(e.payload->>'id') <= 256 THEN e.payload->>'id'
         WHEN e.event_type = 'tool_result'
              AND jsonb_typeof(e.payload->'tool_use_id') = 'string'
              AND LENGTH(e.payload->>'tool_use_id') <= 256 THEN e.payload->>'tool_use_id'
    END AS tool_use_id,
    CASE WHEN e.event_type IN ('tool_use', 'tool_result')
              AND jsonb_typeof(e.payload->'name') = 'string'
         THEN LEFT(e.payload->>'name', 160) END AS tool_name,
    CASE WHEN e.event_type = 'tool_result'
              AND jsonb_typeof(e.payload->'is_error') = 'boolean'
         THEN (e.payload->>'is_error')::boolean END AS is_error,
    -- Content means something a reveal would actually return. An empty object
    -- is not content, and neither is a payload reduced to the truncation
    -- markers alone: advertising a reveal there leads to an empty payload.
    (jsonb_typeof(e.payload) = 'object' AND EXISTS (
        SELECT 1 FROM jsonb_each(e.payload) AS field(key, value)
        WHERE value <> 'null'::jsonb
          AND key NOT IN ('truncated', 'original_payload_bytes', 'truncation_notice')
     )) AS has_content
"""

# Share exactly the spellings understood by the accounting implementation.
# Values must also be numbers, so an arbitrary object hidden under a familiar
# usage key cannot reach the application on a default metadata request.
_USAGE_KEYS_SQL = ", ".join(f"'{key}'" for group in _TOKEN_KEY_GROUPS for key in group)
_RESOURCE_FIELDS = tuple(
    key for key in AttemptResources.model_fields if key not in {"tool_calls", "processes"}
)
_RESOURCE_COLUMNS = ", ".join(
    f"LEFT(r.{key}, 128) AS {key}" if key in {"sampler_version", "process_observer"} else f"r.{key}"
    for key in _RESOURCE_FIELDS
)
_CALL_COLUMNS = ", ".join(
    f"LEFT({key}, 256) AS {key}" if key in {"tool_use_id", "tool_name", "timing_source"} else key
    for key in ToolCallResources.model_fields
)
_PROCESS_COLUMNS = ", ".join(
    f"LEFT({key}, 256) AS {key}" if key in {"comm", "executable"} else key
    for key in ProcessObservation.model_fields
)
_MAX_CALLS = 512
_MAX_PROCESSES = 2048


class AdminRunQueries:
    """Admin-only queries, kept separate from ownership-enforcing store reads."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the Agent database's existing connection pool."""
        self._pool = pool

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[asyncpg.Connection]:
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            yield conn

    async def list_runs(
        self,
        *,
        filters: RunFilters,
        since: datetime,
        until: datetime,
        cursor: RunCursor | None,
        limit: int,
    ) -> AdminRunList:
        """Read a page of turns across owners, with a creation-time tie breaker."""
        async with self._read() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_RUN_COLUMNS}
                FROM agent_jobs j LEFT JOIN agent_threads t ON t.id = j.thread_id
                WHERE j.created_at >= $1 AND j.created_at < $2
                  AND ($3::text IS NULL OR j.user_id = $3)
                  AND ($4::text IS NULL OR t.repo = $4)
                  AND ($5::text IS NULL OR j.state = $5)
                  AND ($6::text IS NULL OR j.runtime = $6)
                  AND ($7::text IS NULL OR j.model = $7)
                  AND ($8::text IS NULL OR EXISTS (
                      SELECT 1 FROM agent_attempts a WHERE a.job_id = j.id AND a.host_id = $8
                  ))
                  AND ($9::timestamptz IS NULL OR (j.created_at, j.id) < ($9, $10::text))
                ORDER BY j.created_at DESC, j.id DESC
                LIMIT $11
                """,
                since,
                until,
                filters.user_id,
                filters.repo,
                filters.state,
                filters.runtime,
                filters.model,
                filters.host,
                cursor.created_at if cursor else None,
                cursor.job_id if cursor else None,
                limit + 1,
            )
        page = [AdminRun.model_validate(dict(row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = encode_cursor(
                RunCursor(
                    scope=cursor_scope(filters.model_dump()),
                    since=since,
                    until=until,
                    created_at=last.created_at,
                    job_id=last.id,
                )
            )
        return AdminRunList(runs=page, since=since, until=until, next_cursor=next_cursor)

    async def get_run(self, job_id: str) -> dict[str, Any] | None:
        """Fetch the same explicit metadata fields as the list, never SELECT *."""
        async with self._read() as conn:
            row = await conn.fetchrow(
                f"""SELECT {_RUN_COLUMNS}
                FROM agent_jobs j LEFT JOIN agent_threads t ON t.id = j.thread_id
                WHERE j.id = $1""",
                job_id,
            )
        return dict(row) if row is not None else None

    async def attempts(self, job_id: str) -> list[dict[str, Any]]:
        """Read execution identities without lease credentials or failure text."""
        async with self._read() as conn:
            rows = await conn.fetch(
                """
                SELECT id AS attempt_id, attempt_no, LEFT(host_id, 256) AS host,
                       status, started_at, finished_at
                FROM agent_attempts WHERE job_id = $1 ORDER BY attempt_no, id
                """,
                job_id,
            )
        return [dict(row) for row in rows]

    async def usage_inputs(self, job_id: str) -> dict[str, Any]:
        """Project only numeric usage and response identity for existing accounting.

        Hashing response IDs preserves their equality without fetching their
        arbitrary text. Attempt totals, duplicate blocks, zero-output detection
        and each runtime's cache convention still use the shared summarizer.
        """
        async with self._read() as conn:
            attempts = await conn.fetch(
                "SELECT job_id, started_at FROM agent_attempts WHERE job_id = $1", job_id
            )
            rows = await conn.fetch(
                f"""
                SELECT e.job_id, e.attempt_id, e.seq, e.event_type, e.created_at,
                       jsonb_build_object(
                           'usage', COALESCE((
                               SELECT jsonb_object_agg(key, value)
                               FROM jsonb_each(CASE
                                   WHEN jsonb_typeof(e.payload->'usage') = 'object'
                                       THEN e.payload->'usage'
                                   WHEN e.event_type = 'usage'
                                       AND jsonb_typeof(e.payload) = 'object' THEN e.payload
                                   ELSE '{{}}'::jsonb END)
                               WHERE key IN ({_USAGE_KEYS_SQL})
                                 AND jsonb_typeof(value) = 'number'
                                 AND LENGTH(value::text) <= 32
                           ), '{{}}'::jsonb),
                           'usage_id', CASE
                               WHEN jsonb_typeof(e.payload->'usage_id') = 'string'
                                 AND e.payload->>'usage_id' <> ''
                               THEN encode(sha256(convert_to(e.payload->>'usage_id', 'UTF8')), 'hex')
                           END,
                           'usage_scope', CASE WHEN e.payload->>'usage_scope' = 'attempt'
                               THEN 'attempt' END,
                           'has_text', COALESCE(e.payload->>'text', '') ~ '[^[:space:]]'
                       ) AS payload
                FROM agent_job_events e
                WHERE e.job_id = $1 ORDER BY e.attempt_id, e.seq
                """,
                job_id,
            )
        return {
            "job_ids": [job_id],
            "attempts": [dict(row) for row in attempts],
            "events": [{**dict(row), "payload": _load_json(row["payload"])} for row in rows],
        }

    async def activity(
        self,
        job_id: str,
        *,
        attempt_id: int | None,
        cursor: ActivityCursor | None,
        limit: int,
    ) -> AdminActivityPage:
        """Page across attempt-local sequences without reordering late arrivals.

        A cursor fixes the upper global event ID. A fresh first page sees new
        events; subsequent pages finish the same snapshot even if older attempts
        receive a late control event meanwhile.
        """
        async with self._read() as conn:
            snapshot_id = (
                cursor.snapshot_id
                if cursor
                else await conn.fetchval(
                    "SELECT COALESCE(MAX(id), 0) FROM agent_job_events WHERE job_id = $1", job_id
                )
            )
            rows = await conn.fetch(
                f"""
                SELECT {_EVENT_COLUMNS}
                FROM agent_job_events e JOIN agent_attempts a ON a.id = e.attempt_id
                WHERE e.job_id = $1 AND a.job_id = $1 AND e.id <= $2
                  AND ($3::bigint IS NULL OR e.attempt_id = $3)
                  AND ($4::integer IS NULL
                       OR (a.attempt_no, e.attempt_id, e.seq) > ($4, $5::bigint, $6::integer))
                ORDER BY a.attempt_no, e.attempt_id, e.seq
                LIMIT $7
                """,
                job_id,
                snapshot_id,
                attempt_id,
                cursor.attempt_no if cursor else None,
                cursor.attempt_id if cursor else None,
                cursor.seq if cursor else None,
                limit + 1,
            )
        page = [AdminActivity.model_validate(dict(row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = encode_cursor(
                ActivityCursor(
                    scope=cursor_scope([job_id, attempt_id]),
                    snapshot_id=snapshot_id,
                    attempt_no=last.attempt_no,
                    attempt_id=last.attempt_id,
                    seq=last.seq,
                )
            )
        return AdminActivityPage(events=page, next_cursor=next_cursor)

    async def resources(self, job_id: str, attempt_id: int) -> dict[str, Any] | None:
        """Read one attempt's resource rows with their original source and gaps."""
        async with self._read() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT a.id AS attempt_id, a.attempt_no, a.started_at, a.finished_at,
                       a.continuity_mode, a.context_status, {_RESOURCE_COLUMNS}
                FROM agent_attempts a
                LEFT JOIN agent_attempt_resources r ON r.attempt_id = a.id
                WHERE a.job_id = $1 AND a.id = $2
                """,
                job_id,
                attempt_id,
            )
            if row is None:
                return None
            telemetry = dict(row)
            calls = await conn.fetch(
                f"SELECT {_CALL_COLUMNS} FROM agent_tool_call_resources "
                "WHERE attempt_id = $1 ORDER BY call_seq LIMIT $2",
                attempt_id,
                _MAX_CALLS + 1,
            )
            processes = await conn.fetch(
                f"SELECT {_PROCESS_COLUMNS} FROM agent_process_observations "
                "WHERE attempt_id = $1 ORDER BY process_seq LIMIT $2",
                attempt_id,
                _MAX_PROCESSES + 1,
            )
        telemetry["tool_calls"] = [dict(row) for row in calls[:_MAX_CALLS]]
        telemetry["processes"] = [dict(row) for row in processes[:_MAX_PROCESSES]]
        telemetry["tool_calls_truncated"] = bool(telemetry["tool_calls_truncated"]) or (
            len(calls) > _MAX_CALLS
        )
        telemetry["processes_truncated"] = bool(telemetry["processes_truncated"]) or (
            len(processes) > _MAX_PROCESSES
        )
        if telemetry["processes_truncated"]:
            telemetry["process_observation_complete"] = False
        return telemetry

    async def event_content(self, job_id: str, event_id: int) -> dict[str, Any] | None:
        """Fetch one payload, including a bounded preview for oversized old rows.

        Postgres bounds the legacy preview before sending it over the wire.
        The shared final limiter also counts JSON escaping and UTF-8 bytes.
        This method must only be called by an authenticated, audited reveal.
        """
        async with self._read() as conn:
            row = await conn.fetchrow(
                """
                SELECT e.id, e.attempt_id, e.seq, LEFT(e.event_type, 64) AS event_type,
                       e.created_at,
                       CASE WHEN OCTET_LENGTH(e.payload::text) > $3 THEN
                           jsonb_build_object(
                               'preview', LEFT(e.payload::text, 32768),
                               'truncated', TRUE,
                               'original_payload_bytes', OCTET_LENGTH(e.payload::text),
                               'truncation_notice', 'Stored event exceeds 256 KiB; showing a preview.'
                           )
                       WHEN jsonb_typeof(e.payload) = 'object' THEN e.payload
                       ELSE '{}'::jsonb END AS payload
                FROM agent_job_events e JOIN agent_attempts a ON a.id = e.attempt_id
                WHERE e.job_id = $1 AND a.job_id = $1 AND e.id = $2
                """,
                job_id,
                event_id,
                MAX_EVENT_PAYLOAD_BYTES,
            )
        if row is None:
            return None
        result = dict(row)
        result["payload"] = bound_event_payload(_load_json(row["payload"]) or {})
        result["truncated"] = result["payload"].get("truncated") is True
        return result
