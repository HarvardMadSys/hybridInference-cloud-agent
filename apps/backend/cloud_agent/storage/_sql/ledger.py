"""The ledger's window, the refresh a model call asks for, and a job's usage reads.

The reads take the caller's connection, so that an owner's usage can be read
from one snapshot and given that snapshot's version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.attempts import NEXT_ATTEMPT_START_SQL
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.thread_stats import SUMMARY_PAYLOAD_KEYS

if TYPE_CHECKING:
    import asyncpg

_SUMMARY_PAYLOAD_KEYS_SQL = ", ".join(f"'{key}'" for key in SUMMARY_PAYLOAD_KEYS)

#: The payload of event ``e`` as a usage read needs it: the keys the summaries
#: read (``SUMMARY_PAYLOAD_KEYS``) and whether its text has anything to show. A
#: tool result also carries what the tool returned, such as a whole edited
#: file, and taking every key but a few moved all of it to add up token counts.
USAGE_EVENT_PAYLOAD_SQL = f"""
COALESCE((
    SELECT jsonb_object_agg(field.key, field.value)
    FROM jsonb_each(CASE WHEN jsonb_typeof(e.payload) = 'object'
                         THEN e.payload ELSE '{{}}'::jsonb END) AS field
    WHERE field.key IN ({_SUMMARY_PAYLOAD_KEYS_SQL})
), '{{}}'::jsonb)
|| jsonb_build_object('has_text', COALESCE(e.payload->>'text', '') ~ '[^[:space:]]')
"""

LEDGER_WINDOW_UNTIL_SQL = f"""
LEAST(
    COALESCE(a.finished_at, NOW()),
    {NEXT_ATTEMPT_START_SQL}
)
"""

#: The two columns :func:`cloud_agent.ledger.summarize_ledger` needs to tell
#: what an attempt ``a`` with no ledger reference ``l`` is: still starting,
#: ended before its agent called a model, or unattributed. A read that hands the
#: reducer attempt rows without them makes every such attempt unattributed, so
#: each one selects them from here. Only a finished attempt without a reference
#: is asked whether it reported model usage, the way thread stats reads it.
UNREFERENCED_ATTEMPT_SQL = """
a.status AS attempt_status,
CASE WHEN l.id IS NULL AND a.status <> 'running' THEN EXISTS (
    SELECT 1 FROM agent_job_events e
    WHERE e.attempt_id = a.id
      AND (e.event_type = 'usage'
           OR jsonb_typeof(e.payload->'usage') = 'object')
) END AS model_usage_reported
"""


#: Ask for this attempt's usage to be read again, at once or as soon as the last
#: read is five seconds old, and never later than a read already scheduled.
#:
#: Unlike an owner's refresh request, this one is recorded while a read is under
#: way: that read may have started before the call it is asked for finished.
#: The claim cleared ``next_refresh_at``, so a value there when the read
#: completes can only be such a request, and completing keeps it.
REQUEST_ATTEMPT_LEDGER_REFRESH_SQL = """
UPDATE agent_attempt_ledger l
SET next_refresh_at = GREATEST(clock_timestamp(), l.last_refresh_attempt_at + INTERVAL '5 seconds')
WHERE l.attempt_id = $1
  AND (l.next_refresh_at IS NULL OR l.next_refresh_at > GREATEST(
      clock_timestamp(), l.last_refresh_attempt_at + INTERVAL '5 seconds'
  ))
"""


def carries_model_usage(event_type: str, payload: dict[str, object] | None) -> bool:
    """Whether a runtime event reports a model call's usage (as thread stats reads it)."""
    return event_type == "usage" or isinstance((payload or {}).get("usage"), dict)


async def fetch_ledger_rows_for_job(conn: asyncpg.Connection, job_id: str) -> list[dict[str, Any]]:
    """A job's attempts with their stored grant snapshots, in the caller's transaction."""
    rows = await conn.fetch(
        f"""
        SELECT a.id AS attempt_id, a.job_id, a.attempt_no,
               a.started_at, a.finished_at,
               l.id, l.grant_id, l.residency_generation, l.binding_id,
               l.window_since, {LEDGER_WINDOW_UNTIL_SQL} AS window_until,
               l.snapshot, l.queried_at, l.last_error_code, l.next_refresh_at,
               l.last_refresh_attempt_at, l.refresh_generation,
               l.refresh_lease_expires_at, l.terminal_refresh_count,
               (l.id IS NOT NULL AND (
                   l.refresh_lease_expires_at IS NOT NULL
                   OR l.next_refresh_at <= NOW()
                   OR (a.status <> 'running' AND l.terminal_refresh_count = 0
                       AND l.next_refresh_at IS NULL)
               )) IS TRUE AS refreshing,
               {UNREFERENCED_ATTEMPT_SQL}
        FROM agent_attempts a
        JOIN agent_jobs j ON j.id = a.job_id
        LEFT JOIN agent_attempt_ledger l ON l.attempt_id = a.id
        WHERE a.job_id = $1
        ORDER BY a.attempt_no, l.id
        """,
        job_id,
    )
    return [{**dict(row), "snapshot": load_json(row["snapshot"])} for row in rows]


async def fetch_ledger_rows_for_conversation(
    conn: asyncpg.Connection, job_id: str, user_id: str
) -> list[dict[str, Any]]:
    """The owner's conversation snapshots around a job, in the caller's transaction."""
    rows = await conn.fetch(
        f"""
        SELECT j.id AS job_id, j.state AS job_state, a.id AS attempt_id, a.attempt_no,
               l.id, l.grant_id, l.snapshot, l.queried_at,
               l.last_error_code, l.next_refresh_at,
               (l.id IS NOT NULL AND (
                   l.refresh_lease_expires_at IS NOT NULL
                   OR l.next_refresh_at <= NOW()
                   OR (a.status <> 'running' AND l.terminal_refresh_count = 0
                       AND l.next_refresh_at IS NULL)
               )) IS TRUE AS refreshing,
               {UNREFERENCED_ATTEMPT_SQL}
        FROM agent_jobs anchor
        JOIN agent_jobs j ON (j.thread_id = anchor.thread_id OR j.id = anchor.id)
            AND j.user_id = $2
        LEFT JOIN agent_attempts a ON a.job_id = j.id
        LEFT JOIN agent_attempt_ledger l ON l.attempt_id = a.id
        WHERE anchor.id = $1 AND anchor.user_id = $2
        ORDER BY j.turn_no, a.attempt_no, l.id
        """,
        job_id,
        user_id,
    )
    return [{**dict(row), "snapshot": load_json(row["snapshot"])} for row in rows]


async def fetch_job_usage_inputs(conn: asyncpg.Connection, job_id: str) -> dict[str, Any]:
    """A job's runtime usage events, in the caller's one-snapshot transaction.

    Attempts and events are read apart, so the transaction must be
    ``REPEATABLE READ`` for the two to agree.
    """
    attempts = await conn.fetch(
        "SELECT job_id, started_at FROM agent_attempts WHERE job_id = $1",
        job_id,
    )
    events = await conn.fetch(
        f"""
        SELECT e.job_id, e.attempt_id, e.seq, e.event_type, e.created_at,
               {USAGE_EVENT_PAYLOAD_SQL} AS payload
        FROM agent_job_events e
        WHERE e.job_id = $1
        ORDER BY e.attempt_id, e.seq
        """,
        job_id,
    )
    return {
        "job_ids": [job_id],
        "attempts": [dict(row) for row in attempts],
        "events": [{**dict(row), "payload": load_json(row["payload"])} for row in events],
    }


#: Fewer transactions still running is a later snapshot at the same ``xmax``;
#: the count is subtracted from this so that later also sorts greater.
_RUNNING_CEILING = 999_999


async def fetch_snapshot_version(conn: asyncpg.Connection) -> str:
    """The caller's snapshot, as a string that sorts with the time it was taken.

    ``xmax`` (the next transaction id) only grows, and while it stays put the
    transactions still running only finish. So of two snapshots, the one whose
    string is greater was taken later and sees every commit the other sees. The
    caller's transaction must be ``REPEATABLE READ``, so that this is the
    snapshot its other reads saw.
    """
    row = await conn.fetchrow(
        "SELECT pg_snapshot_xmax(pg_current_snapshot())::text AS xmax, "
        "(SELECT count(*) FROM pg_snapshot_xip(pg_current_snapshot())) AS running"
    )
    assert row is not None
    return f"{int(row['xmax']):020d}.{_RUNNING_CEILING - int(row['running']):06d}"
