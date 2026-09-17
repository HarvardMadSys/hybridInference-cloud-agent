"""Real Postgres checks for distinct conversations and complete turn history."""

from datetime import timedelta

import pytest

from cloud_agent.admin_conversations import ConversationCursor, ConversationTurnsCursor
from cloud_agent.admin_observability import RunFilters, cursor_scope, decode_cursor
from cloud_agent.storage.admin_conversation_queries import turns_scope
from tests.integration.storage.test_admin_observability import _safe_title
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_attempt_ledger import _seed_attempt, db as db, store as store
from tests.unit.test_ledger_snapshots import END, START

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def turn(db, job, number, *, model="model-a", state="succeeded", fork_source=None):
    """Add one historical turn without running a sandbox."""
    async with db.acquire() as conn:
        parent = await conn.fetchval(
            "SELECT id FROM agent_jobs WHERE thread_id = $1 ORDER BY turn_no DESC LIMIT 1",
            job["thread_id"],
        )
        return await conn.fetchval(
            """INSERT INTO agent_jobs
               (id, thread_id, user_id, parent_job_id, turn_no, native_turn_no,
                task_prompt, runtime, model, state, created_at, updated_at, fork_source_job_id)
               VALUES ($1, $2, $3, $4, $5, $5, 'PRIVATE follow-up', 'claude-code', $6, $7, $8, $9, $10)
               RETURNING id""",
            f"{job['id']}-turn-{number}",
            job["thread_id"],
            job["user_id"],
            parent,
            number,
            model,
            state,
            START + timedelta(seconds=number * 10),
            END,
            fork_source,
        )


async def seed(db, store, **kwargs):
    job = await _create_job(store, task_prompt="PRIVATE prompt", **kwargs)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET created_at = $2, updated_at = $2 WHERE id = $1", job["id"], START
        )
    return job


async def listing(store, filters=RunFilters(), cursor=None, limit=50):
    return await store.admin_conversations.list_conversations(
        filters=filters, since=START, until=END, cursor=cursor, limit=limit
    )


async def details(store, thread_id, filters=RunFilters(), cursor=None, limit=50):
    return await store.admin_conversations.detail(
        thread_id, filters=filters, since=START, until=END, cursor=cursor, limit=limit
    )


async def test_multiple_turns_form_one_conversation_with_global_state_and_safe_title(db, store):
    first = await seed(db, store, user_id="owner-one")
    second = await seed(db, store, user_id="owner-two")
    await _safe_title(db, store, first, "Generated title " + "x" * 200)
    await turn(db, first, 2, state="failed")
    latest = await turn(db, first, 3, model="model-b")
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'running' WHERE id = $1", first["id"])
    page = await listing(store)
    assert len(page.conversations) == 2
    row = next(x for x in page.conversations if x.thread_id == first["thread_id"])
    assert row.turn_count == row.matching_turn_count == 3
    assert row.latest_job_id == latest
    assert row.latest_model == "model-b"
    assert row.latest_turn_state == "succeeded"
    assert row.state == "running"  # An earlier active turn is not hidden by a settled follow-up.
    assert len(row.title) == 160
    assert next(x for x in page.conversations if x.thread_id == second["thread_id"]).title is None
    assert "PRIVATE" not in page.model_dump_json()


async def test_matching_turn_filters_preserve_full_history_and_latest_model(db, store):
    job = await seed(db, store, model="model-a")
    matched = await turn(db, job, 2, model="model-a", state="failed")
    latest = await turn(db, job, 3, model="model-b")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET created_at = $2 WHERE id = $1",
            job["id"],
            START - timedelta(days=30),
        )
        await conn.execute(
            "UPDATE agent_threads SET created_at = $2 WHERE id = $1",
            job["thread_id"],
            START - timedelta(days=30),
        )
    filters = RunFilters(model="model-a", state="failed", user_id=job["user_id"], repo=job["repo"])
    page = await listing(store, filters)
    assert len(page.conversations) == 1
    assert page.conversations[0].matching_turn_count == 1
    assert page.conversations[0].turn_count == 3
    assert page.conversations[0].latest_model == "model-b"
    detail = await details(store, job["thread_id"], filters)
    assert [x.id for x in detail.turns] == [latest, matched, job["id"]]
    assert [x.matches_filters for x in detail.turns] == [False, True, False]
    assert "PRIVATE" not in detail.model_dump_json()


async def test_equal_timestamp_pages_do_not_split_or_repeat_conversations(db, store):
    jobs = [await seed(db, store, user_id=f"owner-{i}") for i in range(4)]
    for job in jobs:
        await turn(db, job, 2)
    seen = []
    cursor = None
    while True:
        page = await listing(store, cursor=cursor, limit=1)
        seen.extend(x.thread_id for x in page.conversations)
        if not page.next_cursor:
            break
        cursor = decode_cursor(
            page.next_cursor,
            ConversationCursor,
            cursor_scope(["conversations", RunFilters().model_dump()]),
        )
    assert seen == sorted([job["thread_id"] for job in jobs], reverse=True)


async def test_turn_pages_include_inherited_history_and_ignore_newer_appends(db, store):
    parent = await seed(db, store, user_id="parent-owner")
    job = await seed(db, store)
    inherited = await turn(db, job, 2, fork_source=parent["id"])
    latest = await turn(db, job, 3)
    first = await details(store, job["thread_id"], limit=1)
    assert [x.id for x in first.turns] == [latest]
    await turn(db, job, 4)
    cursor = decode_cursor(
        first.next_cursor,
        ConversationTurnsCursor,
        turns_scope(job["thread_id"], RunFilters(), START, END),
    )
    second = await details(store, job["thread_id"], cursor=cursor)
    assert [x.id for x in second.turns] == [inherited, job["id"]]
    assert second.turns[0].fork_source_job_id == parent["id"]
    assert second.next_cursor is None
    assert second.conversation.turn_count == 4


async def test_missing_empty_and_title_provenance(db, store):
    assert await details(store, "missing") is None
    assert (await listing(store)).conversations == []
    job = await seed(db, store)
    for source in ["fallback", "inherited"]:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_threads SET title_source = $2, title = 'PRIVATE title' WHERE id = $1",
                job["thread_id"],
                source,
            )
        assert (await listing(store)).conversations[0].title is None
        assert (await details(store, job["thread_id"])).conversation.title is None


async def test_host_and_model_must_match_the_same_turn_without_attempt_duplicates(db, store):
    job = await seed(db, store, model="model-a")
    sibling = await turn(db, job, 2, model="model-b")
    _, first, _ = await _seed_attempt(db, store, job=job, grant_id=None)
    _, retry, _ = await _seed_attempt(db, store, job=job, attempt_no=2, grant_id=None)
    _, other, _ = await _seed_attempt(db, store, job={**job, "id": sibling}, grant_id=None)
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_runner_hosts (host, first_seen_at, last_seen_at)
               VALUES ('host-a', NOW(), NOW()), ('host-b', NOW(), NOW())"""
        )
        await conn.execute(
            "UPDATE agent_attempts SET host_id = 'host-a' WHERE id = ANY($1::bigint[])",
            [first, retry],
        )
        await conn.execute("UPDATE agent_attempts SET host_id = 'host-b' WHERE id = $1", other)
    assert (await listing(store, RunFilters(model="model-a", host="host-b"))).conversations == []
    matching = await listing(store, RunFilters(model="model-a", host="host-a"))
    assert len(matching.conversations) == 1
    assert matching.conversations[0].matching_turn_count == 1
    detail = await details(store, job["thread_id"], RunFilters(host="host-a"))
    assert [x.matches_filters for x in detail.turns] == [False, True]


async def test_window_end_is_exclusive_and_idle_does_not_mean_conversation_completed(db, store):
    job = await seed(db, store)
    outside = await turn(db, job, 2)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            job["id"],
        )
        await conn.execute("UPDATE agent_jobs SET created_at = $2 WHERE id = $1", outside, END)
    row = (await listing(store)).conversations[0]
    assert row.turn_count == 2
    assert row.matching_turn_count == 1
    assert row.state == "idle"
    assert row.latest_turn_state == "succeeded"
