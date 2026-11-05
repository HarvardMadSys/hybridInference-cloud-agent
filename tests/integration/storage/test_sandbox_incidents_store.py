"""Real Postgres checks for sandbox incidents and the told-through cursor."""

from __future__ import annotations

import json

import pytest
from tests.integration.storage.test_admin_sandbox_queries import member, seed
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db, store as store

from cloud_agent.storage._sql.event_batches import append_attempt_events
from cloud_agent.storage.repositories.sandbox_incidents import PostgresSandboxIncidentRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

SANDBOX = "asbx_incidents"
KINDS = ["out_of_memory", "browser_crashed", "mcp_server_failed"]


def _row(incident_id: str, kind: str, *, thread_id: str | None = None, **details) -> dict:
    return {
        "event_type": f"incident.{kind}",
        "details": {
            "incident_id": incident_id,
            "scope": "member" if thread_id else "sandbox",
            "observed_at": "2026-11-05T10:00:00+00:00",
            "count": 1,
            **({"thread_id": thread_id} if thread_id else {}),
            **details,
        },
    }


async def _group(db, store):
    """A sandbox with one member whose turn is running."""
    job = await _seed_attempt(db, store, job=await seed(db, store, SANDBOX))
    origin, attempt_id, _ = job
    await member(db, SANDBOX, origin, 0, attempt=attempt_id)
    return origin, attempt_id


async def _started(db, attempt_id: int) -> None:
    async with db.acquire() as conn, conn.transaction():
        stored = await append_attempt_events(
            conn,
            attempt_id=attempt_id,
            lease_generation=1,
            events=(("lifecycle", {"phase": "started"}, None),),
        )
    assert stored is not None


async def test_a_row_is_stored_once_and_only_for_the_host_holding_the_sandbox(db, store):
    await _group(db, store)
    incidents = PostgresSandboxIncidentRepository(db)
    rows = [_row("docker:a:1:oom", "out_of_memory"), _row("journal:a:j:1", "browser_crashed")]

    assert (
        await incidents.record_sandbox_incidents(
            host="someone-else", sandbox_id=SANDBOX, generation=1, incidents=rows
        )
        == []
    )
    stored = await incidents.record_sandbox_incidents(
        host="runner-one", sandbox_id=SANDBOX, generation=1, incidents=rows
    )
    assert [row["event_type"] for row in stored] == [
        "incident.out_of_memory",
        "incident.browser_crashed",
    ]
    # A beat sent again stores nothing new: the host had not heard the answer.
    assert (
        await incidents.record_sandbox_incidents(
            host="runner-one", sandbox_id=SANDBOX, generation=1, incidents=rows
        )
        == []
    )
    async with db.acquire() as conn:
        source = await conn.fetch(
            "SELECT source FROM agent_sandbox_events WHERE starts_with(event_type, 'incident.')"
        )
    assert [row["source"] for row in source] == ["host", "host"]


async def test_a_notice_lands_in_the_running_turn_it_concerns(db, store):
    _, attempt_id = await _group(db, store)
    incidents = PostgresSandboxIncidentRepository(db)
    payload = {"phase": "sandbox_incident", "incident": "out_of_memory"}

    assert (
        await incidents.notify_running_turns(sandbox_id=SANDBOX, thread_id=None, payload=payload)
        == 1
    )
    assert (
        await incidents.notify_running_turns(
            sandbox_id=SANDBOX, thread_id="th-other", payload=payload
        )
        == 0
    )
    async with db.acquire() as conn:
        events = await conn.fetch(
            "SELECT event_type, payload FROM agent_job_events WHERE attempt_id = $1", attempt_id
        )
    assert [(row["event_type"], json.loads(row["payload"])) for row in events] == [
        ("lifecycle", payload)
    ]


async def test_the_cursor_moves_only_when_the_attempt_that_told_starts(db, store):
    origin, attempt_id = await _group(db, store)
    thread = origin["thread_id"]
    incidents = PostgresSandboxIncidentRepository(db)
    assert await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10) is None

    await incidents.record_sandbox_incidents(
        host="runner-one", sandbox_id=SANDBOX, generation=1, incidents=[_row("a", "out_of_memory")]
    )
    # Never told anything: told nothing, and starts at the newest.
    first = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    assert first is not None and first["rows"] == [] and first["total"] == 0
    await incidents.mark_attempt_incidents(
        attempt_id=attempt_id, thread_id=thread, through_id=first["through_id"]
    )
    await _started(db, attempt_id)

    sibling = await _create_job(store)
    await incidents.record_sandbox_incidents(
        host="runner-one",
        sandbox_id=SANDBOX,
        generation=1,
        incidents=[
            _row("b", "browser_crashed"),
            _row("c", "mcp_server_failed", thread_id=thread, alias="docs", phase="start"),
            _row(
                "d", "mcp_server_failed", thread_id=sibling["thread_id"], alias="x", phase="start"
            ),
            _row("e", "memory_high", peak=9, limit=10),
        ],
    )
    untold = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    assert untold is not None
    # Its own failure and the sandbox's; not a sibling's, not a peak.
    assert [row["details"]["incident_id"] for row in untold["rows"]] == ["c", "b"]
    assert untold["total"] == 2

    # A claim that marks but never starts tells the same incidents again.
    _, retry_id, _ = await _seed_attempt(db, store, job=origin, attempt_no=2)
    await incidents.mark_attempt_incidents(
        attempt_id=retry_id, thread_id=thread, through_id=untold["through_id"]
    )
    again = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    assert again is not None and again["total"] == 2

    async with db.acquire() as conn, conn.transaction():
        await append_attempt_events(
            conn,
            attempt_id=retry_id,
            lease_generation=2,
            events=(("lifecycle", {"phase": "started"}, None),),
        )
    told = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    assert told is not None and told["rows"] == [] and told["through_id"] == untold["through_id"]
