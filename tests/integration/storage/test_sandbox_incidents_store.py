"""Real Postgres checks for sandbox incidents and the told-through cursor.

Each test is a way an incident could be lost, misplaced or told twice:

- a row and its notices commit together, so a failed notice is retried with
  its row, and a stored row is never notified twice;
- a chat's cursor starts when it joins, so its first incident is told, and a
  Fork is not told what happened before it joined;
- the cursor moves when the prompt was dispatched (``turn_started``), not
  when the container started, so a turn that failed before its prompt went
  out tells the same incidents again.
"""

from __future__ import annotations

import json

import pytest
from tests.integration.storage.test_admin_sandbox_queries import member, seed
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db, store as store

from cloud_agent.storage._sql.event_batches import append_attempt_events
from cloud_agent.storage.repositories import sandbox_incidents as repository
from cloud_agent.storage.repositories.sandbox_incidents import PostgresSandboxIncidentRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

SANDBOX = "asbx_incidents"
KINDS = ["out_of_memory", "browser_crashed", "mcp_server_failed"]
OOM_NOTICE = {"phase": "sandbox_incident", "incident": "out_of_memory"}


def _row(incident_id: str, kind: str, *, thread_id: str | None = None, notice=None, **details):
    row = {
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
    if notice is not None:
        row["notice"] = {"payload": notice, "thread_id": thread_id}
    return row


async def _group(db, store):
    """A sandbox with one member whose turn is running."""
    origin, attempt_id, _ = await _seed_attempt(db, store, job=await seed(db, store, SANDBOX))
    await member(db, SANDBOX, origin, 0, attempt=attempt_id)
    return origin, attempt_id


async def _join(db, store, slot: int):
    """Another chat joins the sandbox with a running turn of its own."""
    job, attempt_id, _ = await _seed_attempt(db, store, job=await _create_job(store))
    await member(db, SANDBOX, job, slot, attempt=attempt_id)
    return job, attempt_id


async def _lifecycle(db, attempt_id: int, phase: str, lease_generation: int = 1) -> None:
    async with db.acquire() as conn, conn.transaction():
        stored = await append_attempt_events(
            conn,
            attempt_id=attempt_id,
            lease_generation=lease_generation,
            events=(("lifecycle", {"phase": phase}, None),),
        )
    assert stored is not None


async def _notices(db, attempt_id: int) -> list[dict]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM agent_job_events WHERE attempt_id = $1 AND event_type = 'lifecycle'",
            attempt_id,
        )
    return [
        payload
        for payload in (json.loads(row["payload"]) for row in rows)
        if payload.get("phase") == "sandbox_incident"
    ]


async def _record(incidents, *rows):
    return await incidents.record_sandbox_incidents(
        host="runner-one", sandbox_id=SANDBOX, generation=1, incidents=list(rows)
    )


async def _untold(incidents, thread: str) -> list[str]:
    untold = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    return [] if untold is None else [row["details"]["incident_id"] for row in untold["rows"]]


async def _tell(db, incidents, thread: str, attempt_id: int, *, lease_generation: int = 1):
    """A claim builds its prompt and the prompt goes out."""
    untold = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    if untold is not None:
        await incidents.mark_attempt_incidents(
            attempt_id=attempt_id, thread_id=thread, through_id=untold["through_id"]
        )
    await _lifecycle(db, attempt_id, "started", lease_generation)
    await _lifecycle(db, attempt_id, "turn_started", lease_generation)


# -- storing and notifying ------------------------------------------------------------


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
    stored = await _record(incidents, *rows)
    assert [row["event_type"] for row in stored] == [
        "incident.out_of_memory",
        "incident.browser_crashed",
    ]
    # A beat sent again stores nothing new: the host had not heard the answer.
    assert await _record(incidents, *rows) == []
    async with db.acquire() as conn:
        source = await conn.fetch(
            "SELECT source FROM agent_sandbox_events WHERE starts_with(event_type, 'incident.')"
        )
    assert [row["source"] for row in source] == ["host", "host"]


async def test_a_notice_lands_in_the_running_turns_it_concerns_once(db, store):
    _, attempt_id = await _group(db, store)
    sibling, sibling_attempt = await _join(db, store, 1)
    mcp = {"phase": "sandbox_incident", "incident": "mcp_server_failed", "alias": "docs"}
    incidents = PostgresSandboxIncidentRepository(db)

    await _record(
        incidents,
        _row("oom", "out_of_memory", notice=OOM_NOTICE),
        _row("mcp", "mcp_server_failed", thread_id=sibling["thread_id"], notice=mcp),
        _row("peak", "memory_high", peak=9, limit=10),
    )
    await _record(incidents, _row("oom", "out_of_memory", notice=OOM_NOTICE))  # a replay

    assert await _notices(db, attempt_id) == [OOM_NOTICE]
    assert await _notices(db, sibling_attempt) == [OOM_NOTICE, mcp]


async def test_a_notice_that_fails_rolls_its_row_back_and_the_retry_delivers_it(
    db, store, monkeypatch
):
    """Review P2: the row committed, its notice did not, and the retry was a no-op."""
    _, attempt_id = await _group(db, store)
    _, sibling_attempt = await _join(db, store, 1)
    incidents = PostgresSandboxIncidentRepository(db)
    real = repository.append_attempt_events
    calls: list[int] = []

    async def second_turn_fails(conn, *, attempt_id, **kwargs):
        calls.append(attempt_id)
        if len(calls) == 2:
            raise RuntimeError("the second turn's append failed")
        return await real(conn, attempt_id=attempt_id, **kwargs)

    monkeypatch.setattr(repository, "append_attempt_events", second_turn_fails)
    with pytest.raises(RuntimeError):
        await _record(incidents, _row("oom", "out_of_memory", notice=OOM_NOTICE))
    # Nothing committed: neither the row nor the first turn's notice.
    assert await _notices(db, attempt_id) == [] and await _notices(db, sibling_attempt) == []
    assert await _untold(incidents, "no-such-thread") == []

    monkeypatch.setattr(repository, "append_attempt_events", real)
    assert [
        row["event_type"]
        for row in await _record(incidents, _row("oom", "out_of_memory", notice=OOM_NOTICE))
    ] == ["incident.out_of_memory"]
    await _record(incidents, _row("oom", "out_of_memory", notice=OOM_NOTICE))
    assert await _notices(db, attempt_id) == [OOM_NOTICE]
    assert await _notices(db, sibling_attempt) == [OOM_NOTICE]


async def test_a_turn_whose_lease_expired_is_not_notified(db, store):
    _, attempt_id = await _group(db, store)
    _, sibling_attempt = await _join(db, store, 1)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET lease_expires_at = NOW() - INTERVAL '1 minute' WHERE id = $1",
            sibling_attempt,
        )
    await _record(
        PostgresSandboxIncidentRepository(db), _row("oom", "out_of_memory", notice=OOM_NOTICE)
    )
    assert await _notices(db, attempt_id) == [OOM_NOTICE]
    assert await _notices(db, sibling_attempt) == []


# -- the cursor -----------------------------------------------------------------------


async def test_a_chats_first_incident_is_told_even_when_its_first_turn_had_none(db, store):
    """Review P1: no incident at the first claim left no cursor, and the first one was skipped."""
    origin, attempt_id = await _group(db, store)
    thread = origin["thread_id"]
    incidents = PostgresSandboxIncidentRepository(db)
    await _tell(db, incidents, thread, attempt_id)  # the first turn: nothing to tell

    await _record(incidents, _row("crash", "browser_crashed"))
    assert await _untold(incidents, thread) == ["crash"]


async def test_a_fork_is_told_only_what_happened_after_it_joined(db, store):
    origin, _ = await _group(db, store)
    incidents = PostgresSandboxIncidentRepository(db)
    await _record(incidents, _row("before", "out_of_memory"))

    fork, _ = await _join(db, store, 1)
    assert await _untold(incidents, fork["thread_id"]) == []
    await _record(incidents, _row("after", "browser_crashed"))
    assert await _untold(incidents, fork["thread_id"]) == ["after"]
    # The chat that was there all along hears both.
    assert await _untold(incidents, origin["thread_id"]) == ["after", "before"]


async def test_the_cursor_moves_when_the_prompt_goes_out_not_when_the_container_starts(db, store):
    """Review P2: ``started`` comes before the workspace, uploads and runtime checks."""
    origin, attempt_id = await _group(db, store)
    thread = origin["thread_id"]
    incidents = PostgresSandboxIncidentRepository(db)
    await _record(incidents, _row("a", "out_of_memory"))

    untold = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    assert untold is not None and [row["details"]["incident_id"] for row in untold["rows"]] == ["a"]
    await incidents.mark_attempt_incidents(
        attempt_id=attempt_id, thread_id=thread, through_id=untold["through_id"]
    )
    await _lifecycle(db, attempt_id, "started")
    # The container started; the turn then failed before its prompt went out.
    assert await _untold(incidents, thread) == ["a"]

    _, retry_id, _ = await _seed_attempt(db, store, job=origin, attempt_no=2)
    await _tell(db, incidents, thread, retry_id, lease_generation=2)
    assert await _untold(incidents, thread) == []


async def test_only_the_chats_own_and_the_sandboxs_incidents_are_told(db, store):
    origin, attempt_id = await _group(db, store)
    thread = origin["thread_id"]
    sibling, _ = await _join(db, store, 1)
    incidents = PostgresSandboxIncidentRepository(db)
    await _record(
        incidents,
        _row("b", "browser_crashed"),
        _row("c", "mcp_server_failed", thread_id=thread, alias="docs", phase="start"),
        _row("d", "mcp_server_failed", thread_id=sibling["thread_id"], alias="x", phase="start"),
        _row("e", "memory_high", peak=9, limit=10),
    )
    untold = await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10)
    assert untold is not None
    # Its own failure and the sandbox's; not a sibling's, not a peak.
    assert [row["details"]["incident_id"] for row in untold["rows"]] == ["c", "b"]
    assert untold["total"] == 2
    await _tell(db, incidents, thread, attempt_id)
    assert await _untold(incidents, thread) == []
    assert await _untold(incidents, sibling["thread_id"]) == ["d", "b"]


async def test_a_membership_without_a_cursor_starts_one_explicitly(db, store):
    origin, _ = await _group(db, store)
    thread = origin["thread_id"]
    incidents = PostgresSandboxIncidentRepository(db)
    await _record(incidents, _row("old", "out_of_memory"))
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM agent_thread_incident_cursors WHERE thread_id = $1", thread)

    assert await incidents.untold_incidents(thread_id=thread, kinds=KINDS, limit=10) is None
    await _record(incidents, _row("new", "browser_crashed"))
    assert await _untold(incidents, thread) == ["new"]
