"""Real SQL for the stored attribution references behind the request panel.

The gateway holds the request rows; what is checked here is the read that says
which of them belong to a turn — bounded, ordered, scoped to one job, and
sharing the ledger's own window definition rather than a second one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db

from cloud_agent.admin_observability import LlmRequestSourceCursor, cursor_scope
from cloud_agent.storage.assembly import Storage


@pytest_asyncio.fixture
async def store(db):
    """Everything these tests need, to set a claim up."""
    return Storage(db)


pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

START = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)


async def _ledger_state(db, store) -> list[dict]:
    """Everything a detail read must leave exactly as it found it."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, attempt_id, grant_id, binding_id, residency_generation,
                   window_since, snapshot, queried_at, last_error_code,
                   refresh_generation, next_refresh_at, terminal_refresh_count
            FROM agent_attempt_ledger ORDER BY id
            """
        )
    return [dict(row) for row in rows]


async def test_references_keep_execution_order_and_keep_unattributed_ones(db, store):
    job, _, first_ledger = await _seed_attempt(
        db,
        store,
        attempt_no=1,
        grant_id="grant-one",
        started_at=START,
        finished_at=START + timedelta(seconds=5),
    )
    _, _, _ = await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=2,
        grant_id=None,
        started_at=START + timedelta(seconds=10),
        finished_at=START + timedelta(seconds=15),
    )
    _, _, third_ledger = await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=3,
        grant_id="grant-three",
        started_at=START + timedelta(seconds=20),
        finished_at=START + timedelta(seconds=25),
    )
    queries = store.admin_runs
    rows, more = await queries.llm_request_sources(
        job["id"], attempt_id=None, cursor=None, limit=100
    )
    assert more is False
    assert [row["attempt_no"] for row in rows] == [1, 2, 3]
    assert [row["ledger_id"] for row in rows] == [first_ledger, None, third_ledger]
    assert rows[1]["grant_id"] is None
    assert rows[1]["window_since"] is None


async def test_paging_never_skips_the_unattributed_row(db, store):
    job, _, first = await _seed_attempt(
        db,
        store,
        attempt_no=1,
        grant_id="grant-one",
        started_at=START,
        finished_at=START + timedelta(seconds=1),
    )
    await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=2,
        grant_id=None,
        started_at=START + timedelta(seconds=2),
        finished_at=START + timedelta(seconds=3),
    )
    _, _, third = await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=3,
        grant_id="grant-three",
        started_at=START + timedelta(seconds=4),
        finished_at=START + timedelta(seconds=5),
    )
    queries = store.admin_runs
    seen: list[int | None] = []
    cursor = None
    while True:
        rows, more = await queries.llm_request_sources(
            job["id"], attempt_id=None, cursor=cursor, limit=1
        )
        assert len(rows) <= 1
        seen.extend(row["ledger_id"] for row in rows)
        if not more:
            break
        last = rows[-1]
        cursor = LlmRequestSourceCursor(
            scope=cursor_scope([job["id"], None]),
            attempt_no=last["attempt_no"],
            attempt_id=last["attempt_id"],
            ledger_key=last["ledger_key"],
        )
    assert seen == [first, None, third]


async def test_a_reference_from_another_run_is_not_found(db, store):
    job, _, ledger_id = await _seed_attempt(db, store, grant_id="grant-one", started_at=START)
    other = await _create_job(store, user_id="other-owner")
    queries = store.admin_runs
    assert (await queries.llm_request_source(other["id"], ledger_id)) is None
    row = await queries.llm_request_source(job["id"], ledger_id)
    assert row is not None
    assert row["attempt_no"] == 1


async def test_the_source_window_is_the_ledger_window(db, store):
    """One definition of the effective end, shared with the accounting read."""
    job, _, _ = await _seed_attempt(
        db,
        store,
        attempt_no=1,
        grant_id="grant-one",
        started_at=START,
        # Deliberately unfinished: only the successor bounds this window, which
        # is exactly the case a second, approximate algorithm would get wrong.
        finished_at=None,
    )
    successor_start = START + timedelta(seconds=30)
    await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=2,
        grant_id="grant-two",
        started_at=successor_start,
        finished_at=None,
    )
    queries = store.admin_runs
    sources, _ = await queries.llm_request_sources(
        job["id"], attempt_id=None, cursor=None, limit=100
    )
    ledger_rows = await store.ledger.ledger_rows_for_job(job_id=job["id"])
    assert sources[0]["window_until"] == successor_start
    assert sources[0]["window_until"] == ledger_rows[0]["window_until"]
    assert sources[0]["window_open"] is False
    # The last running attempt is bounded by the clock, so its window is open.
    assert sources[1]["window_open"] is True
    assert sources[1]["window_until"] > successor_start


async def test_reading_references_changes_no_accounting_state(db, store):
    job, _, _ = await _seed_attempt(db, store, grant_id="grant-one", started_at=START)
    await _seed_attempt(
        db,
        store,
        job=job,
        attempt_no=2,
        grant_id=None,
        started_at=START + timedelta(seconds=10),
        finished_at=START + timedelta(seconds=20),
    )
    queries = store.admin_runs
    before = await _ledger_state(db, store)
    await queries.llm_request_sources(job["id"], attempt_id=None, cursor=None, limit=100)
    await queries.llm_request_source(job["id"], before[0]["id"])
    assert await _ledger_state(db, store) == before


async def test_one_execution_can_hold_more_than_one_reference(db, store):
    """A rotated grant is a second source, not a merged or lost one."""
    job, attempt_id, first = await _seed_attempt(
        db, store, attempt_no=1, grant_id="grant-first", started_at=START, finished_at=None
    )
    async with db.acquire() as conn:
        second = await conn.fetchval(
            """
            INSERT INTO agent_attempt_ledger (
                attempt_id, grant_id, residency_generation, binding_id, window_since
            ) VALUES ($1, 'grant-rotated', 2, 'gateway-binding-one', $2) RETURNING id
            """,
            attempt_id,
            START,
        )
    sources, more = await store.admin_runs.llm_request_sources(
        job["id"], attempt_id=None, cursor=None, limit=100
    )
    assert more is False
    assert [row["ledger_id"] for row in sources] == [first, second]
    assert {row["residency_generation"] for row in sources} == {1, 2}
    # One window boundary for both references: a rotation does not create a
    # second, drifting definition of when this execution ended.
    assert {row["window_until"] for row in sources} == {sources[0]["window_until"]}
