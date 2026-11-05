"""A job's usage reads: its reported usage events, and the push a model call sends.

The reads take the caller's connection, so that an owner's usage can be read
from one snapshot and given that snapshot's version.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.rows import load_json
from cloud_agent.thread_stats import SUMMARY_PAYLOAD_KEYS

if TYPE_CHECKING:
    import asyncpg

_SUMMARY_PAYLOAD_KEYS_SQL = ", ".join(f"'{key}'" for key in SUMMARY_PAYLOAD_KEYS)


def _usage_projection(payload: str) -> str:
    """The jsonb ``payload`` as a usage read needs it.

    That is the keys the summaries read (``SUMMARY_PAYLOAD_KEYS``) and whether
    its text has anything to show. A tool result also carries what the tool
    returned, such as a whole edited file, and taking every key but a few moved
    all of it to add up token counts.
    """
    return f"""
COALESCE((
    SELECT jsonb_object_agg(field.key, field.value)
    FROM jsonb_each(CASE WHEN jsonb_typeof({payload}) = 'object'
                         THEN {payload} ELSE '{{}}'::jsonb END) AS field
    WHERE field.key IN ({_SUMMARY_PAYLOAD_KEYS_SQL})
), '{{}}'::jsonb)
|| jsonb_build_object('has_text', COALESCE({payload}->>'text', '') ~ '[^[:space:]]')
"""


#: Which projection a stored ``usage_summary`` was made with. The keys and the
#: expression are both in its SQL, so changing either makes a new version, and
#: a summary stored under an older one is ignored rather than trusted.
USAGE_SUMMARY_VERSION = hashlib.sha256(_usage_projection("payload").encode()).hexdigest()[:16]
_USAGE_SUMMARY_VERSION_KEY = "_usage_projection"


def usage_summary_sql(payload: str) -> str:
    """The projection of ``payload`` as an event row stores it (migration ``0105``).

    It is the read's own SQL, run once when the event is inserted, so the
    stored value cannot drift from what a read would compute; the version
    beside it says which projection that was.
    """
    return (
        f"(({_usage_projection(payload)}) || jsonb_build_object("
        f"'{_USAGE_SUMMARY_VERSION_KEY}', '{USAGE_SUMMARY_VERSION}'))"
    )


#: The payload of event ``e`` as a usage read needs it. Events record that
#: projection when they are stored, and a read takes it from there without
#: opening the payload: a long turn's usage is read again each time a model
#: call is counted, by every tab the owner has open. Rows stored before
#: migration ``0105``, or under a projection since changed, are projected here.
USAGE_EVENT_PAYLOAD_SQL = f"""
CASE WHEN e.usage_summary ->> '{_USAGE_SUMMARY_VERSION_KEY}' = '{USAGE_SUMMARY_VERSION}'
     THEN e.usage_summary - '{_USAGE_SUMMARY_VERSION_KEY}'
     ELSE {_usage_projection("e.payload")}
END
"""

#: Tell the owner's change streams that job ``$1``'s usage changed. Sent
#: (like every notification) when the transaction commits; the stream reads the
#: usage itself, so nothing about it is in the notification.
NOTIFY_JOB_USAGE_SQL = """
SELECT agent_notify_owner(j.user_id, 'usage', j.id) FROM agent_jobs j WHERE j.id = $1
"""


def carries_model_usage(event_type: str, payload: dict[str, object] | None) -> bool:
    """Whether a runtime event reports a model call's usage (as thread stats reads it)."""
    return event_type == "usage" or isinstance((payload or {}).get("usage"), dict)


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
