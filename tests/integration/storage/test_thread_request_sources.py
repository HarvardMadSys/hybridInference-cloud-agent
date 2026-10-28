"""Real SQL for the owner's thread references behind the model TTFT read.

The gateway holds the request rows; what is checked here is which stored
references an owner's thread hands to it: newest first, bounded, scoped to the
owner's own undeleted thread, without aborted attempts, and with the same
window definition the ledger and the admin request pages use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db
from tests.support.follow_ups import append_follow_up

from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def store(db):
    """Everything these tests need, to seed attempts."""
    return Storage(db)


async def _thread(db, store):
    """Two turns of one thread, an aborted retry, and an unrelated thread."""
    start = datetime.now(UTC) - timedelta(minutes=10)
    first, first_attempt, first_ledger = await _seed_attempt(
        db, store, started_at=start, finished_at=start + timedelta(minutes=1)
    )
    _, aborted_attempt, _ = await _seed_attempt(
        db,
        store,
        job=first,
        attempt_no=2,
        grant_id=None,
        started_at=start + timedelta(minutes=2),
        finished_at=start + timedelta(minutes=2, seconds=1),
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_attempts SET status = 'aborted' WHERE id = $1", aborted_attempt
        )
    second = await append_follow_up(
        db, parent_job_id=first["id"], user_id="user-1", prompt="second turn"
    )
    assert second is not None and second.get("error") is None
    _, second_attempt, second_ledger = await _seed_attempt(
        db, store, job=second, started_at=start + timedelta(minutes=5)
    )
    other, _, _ = await _seed_attempt(db, store, started_at=start)
    return {
        "start": start,
        "first": first,
        "second": second,
        "other": other,
        "attempts": (first_attempt, second_attempt),
        "ledgers": (first_ledger, second_ledger),
    }


async def test_newest_started_executions_of_the_owners_thread_only(db, store):
    seeded = await _thread(db, store)

    read = await store.ledger.thread_request_sources(
        job_id=seeded["first"]["id"], user_id="user-1", limit=8
    )

    assert read is not None
    rows, more = read
    assert more is False
    first_attempt, second_attempt = seeded["attempts"]
    first_ledger, second_ledger = seeded["ledgers"]
    assert [row["attempt_id"] for row in rows] == [second_attempt, first_attempt]
    assert [row["ledger_id"] for row in rows] == [second_ledger, first_ledger]
    assert {row["thread_id"] for row in rows} == {seeded["first"]["thread_id"]}
    assert all(row["binding_id"] == "gateway-binding-one" for row in rows)
    newest, oldest = rows
    assert newest["window_open"] is True
    assert oldest["window_open"] is False
    assert oldest["window_since"] == seeded["start"]
    assert oldest["window_until"] == seeded["start"] + timedelta(minutes=1)


async def test_the_read_is_bounded_and_says_when_it_left_rows_out(db, store):
    seeded = await _thread(db, store)

    rows, more = await store.ledger.thread_request_sources(
        job_id=seeded["second"]["id"], user_id="user-1", limit=1
    )

    assert more is True
    assert [row["attempt_id"] for row in rows] == [seeded["attempts"][1]]


async def test_another_owner_missing_or_deleted_thread_reads_nothing(db, store):
    seeded = await _thread(db, store)
    job_id = seeded["first"]["id"]

    assert (
        await store.ledger.thread_request_sources(job_id=job_id, user_id="another-user", limit=8)
        is None
    )
    assert (
        await store.ledger.thread_request_sources(job_id="ajob_missing", user_id="user-1", limit=8)
        is None
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1",
            seeded["first"]["thread_id"],
        )
    assert (
        await store.ledger.thread_request_sources(job_id=job_id, user_id="user-1", limit=8) is None
    )
