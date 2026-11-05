"""PostgreSQL implementation of the sandbox incident port.

Incidents are ``incident.<kind>`` rows of ``agent_sandbox_events``, stored with
``source = 'host'``. The unique index on ``(sandbox_id, details->>'incident_id')``
(migration ``0113_sandbox_incidents``) makes a row a host sends again a no-op.

A row and the notices it puts in running turns commit together. A notice that
cannot be written rolls its rows back, so the heartbeat is not confirmed and
the host sends them again, and the retry stores and notifies them afresh; a
row that committed was notified, so its replay notifies nobody twice. The
transaction inserts its rows first and then locks the running attempts in id
order: no runner append, which locks an attempt and writes job events, waits
on anything this holds.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.event_batches import append_attempt_events

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import asyncpg

_INSERT_INCIDENT_SQL = """
INSERT INTO agent_sandbox_events (sandbox_id, generation, event_type, source, details)
VALUES ($1, $2, $3, 'host', $4::jsonb)
ON CONFLICT (sandbox_id, (details->>'incident_id'))
    WHERE starts_with(event_type, 'incident.') DO NOTHING
RETURNING id
"""

_RUNNING_TURNS_SQL = """
SELECT a.id AS attempt_id, a.lease_generation, m.thread_id
FROM agent_thread_sandbox_memberships m
JOIN agent_attempts a ON a.id = m.active_attempt_id
WHERE m.sandbox_id = $1
  AND a.status = 'running'
  AND a.lease_expires_at > clock_timestamp()
ORDER BY a.id
"""

_CURSOR_SQL = """
SELECT m.sandbox_id, c.told_through,
       (SELECT max(e.id) FROM agent_sandbox_events e
        WHERE e.sandbox_id = m.sandbox_id AND starts_with(e.event_type, 'incident.')) AS newest
FROM agent_thread_sandbox_memberships m
LEFT JOIN agent_thread_incident_cursors c ON c.thread_id = m.thread_id
WHERE m.thread_id = $1
"""

_UNTOLD_SQL = """
SELECT id, event_type, details
FROM agent_sandbox_events
WHERE sandbox_id = $1 AND id > $2 AND id <= $3
  AND event_type = ANY($4::text[])
  AND (details->>'scope' = 'sandbox' OR details->>'thread_id' = $5)
ORDER BY id DESC
LIMIT $6
"""

_UNTOLD_COUNT_SQL = """
SELECT count(*)
FROM agent_sandbox_events
WHERE sandbox_id = $1 AND id > $2 AND id <= $3
  AND event_type = ANY($4::text[])
  AND (details->>'scope' = 'sandbox' OR details->>'thread_id' = $5)
"""

#: A chat whose cursor the membership trigger did not start starts here.
_START_SQL = """
INSERT INTO agent_thread_incident_cursors (thread_id, told_through)
VALUES ($1, $2)
ON CONFLICT (thread_id) DO NOTHING
"""

_MARK_SQL = """
INSERT INTO agent_attempt_incident_marks (attempt_id, thread_id, through_id)
VALUES ($1, $2, $3)
ON CONFLICT (attempt_id) DO UPDATE SET through_id = EXCLUDED.through_id
"""


def _details(raw: Any) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    return dict(value) if isinstance(value, dict) else {}


class PostgresSandboxIncidentRepository:
    """Incidents, the turns they are shown in, and each chat's told-through cursor."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the caller's pool."""
        self._pool = pool

    async def record_sandbox_incidents(
        self,
        *,
        host: str,
        sandbox_id: str,
        generation: int,
        incidents: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Store a host's rows for a sandbox it holds, with their notices; the rows newly stored.

        Each row is ``{event_type, details, notice?}``. A ``notice`` is
        ``{payload, thread_id}``: the ``lifecycle`` payload to append to the
        running turns it concerns, every member's or only ``thread_id``'s. It
        is appended only for a row stored now, in the same transaction, to
        each turn whose lease is still live.
        """
        if not incidents:
            return []
        stored: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            held = await conn.fetchval(
                "SELECT 1 FROM agent_sandboxes WHERE id = $1 AND host = $2", sandbox_id, host
            )
            if held is None:
                return []
            for incident in incidents:
                event_id = await conn.fetchval(
                    _INSERT_INCIDENT_SQL,
                    sandbox_id,
                    generation,
                    incident["event_type"],
                    json.dumps(incident["details"]),
                )
                if event_id is not None:
                    stored.append({"id": int(event_id), **incident})
            notices = [row["notice"] for row in stored if row.get("notice")]
            if notices:
                await _notify_running_turns(conn, sandbox_id, notices)
        return stored

    async def untold_incidents(
        self, *, thread_id: str, kinds: Sequence[str], limit: int
    ) -> dict[str, Any] | None:
        """The incidents ``thread_id`` has not been told, of ``kinds``."""
        async with self._pool.acquire() as conn:
            cursor = await conn.fetchrow(_CURSOR_SQL, thread_id)
            if cursor is None:
                return None  # in no sandbox
            if cursor["told_through"] is None:
                # The membership's trigger starts every cursor; one it did not
                # start begins after everything already recorded, explicitly,
                # so the next incident is the first it is told.
                await conn.execute(_START_SQL, thread_id, int(cursor["newest"] or 0))
                return None
            if cursor["newest"] is None:
                return None
            newest = int(cursor["newest"])
            told = int(cursor["told_through"])
            if told >= newest:
                return {"through_id": told, "rows": [], "total": 0}
            event_types = [f"incident.{kind}" for kind in kinds]
            args = (cursor["sandbox_id"], told, newest, event_types, thread_id)
            rows = await conn.fetch(_UNTOLD_SQL, *args, limit)
            total = int(await conn.fetchval(_UNTOLD_COUNT_SQL, *args))
        return {
            "through_id": newest,
            "rows": [
                {
                    "id": int(row["id"]),
                    "event_type": row["event_type"],
                    "details": _details(row["details"]),
                }
                for row in rows
            ],
            "total": total,
        }

    async def mark_attempt_incidents(
        self, *, attempt_id: int, thread_id: str, through_id: int
    ) -> None:
        """Record what an attempt's prompt told; its ``turn_started`` moves the cursor there."""
        async with self._pool.acquire() as conn:
            await conn.execute(_MARK_SQL, attempt_id, thread_id, through_id)


async def _notify_running_turns(
    conn: Any, sandbox_id: str, notices: Sequence[Mapping[str, Any]]
) -> None:
    """Append each notice to the running turns it concerns, one locked append per turn."""
    for turn in await conn.fetch(_RUNNING_TURNS_SQL, sandbox_id):
        events = tuple(
            ("lifecycle", dict(notice["payload"]), None)
            for notice in notices
            if notice.get("thread_id") in (None, turn["thread_id"])
        )
        if events:
            # ``None``: the turn lost its lease meanwhile, and is not told.
            await append_attempt_events(
                conn,
                attempt_id=int(turn["attempt_id"]),
                lease_generation=int(turn["lease_generation"]),
                events=events,
            )


__all__ = ["PostgresSandboxIncidentRepository"]
