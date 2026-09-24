"""The pending-message queue's revision: additive on the way up, careful on the way down."""

from __future__ import annotations

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_schema_upgrade import _db_config, store as store
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_BEFORE = "0022_goal_open_ended"
_THIS = "0023_pending_messages"


async def _rows(conn, table: str) -> list[dict]:
    return [dict(row) for row in await conn.fetch(f"SELECT * FROM {table} ORDER BY id")]


async def test_upgrade_adds_the_queue_without_touching_existing_rows(store):
    job = await _create_job(store)
    await store.attachments.create_attachment(
        user_id=job["user_id"], path="notes.txt", content=b"notes", content_type="text/plain"
    )
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        threads = await _rows(conn, "agent_threads")
        uploads = await _rows(conn, "agent_job_attachments")
        transcript = await _rows(conn, "agent_thread_messages")
    finally:
        await conn.close()

    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        # Every thread starts with a queue that is neither paused nor has acted
        # on any ending, and every upload is held by no message.
        assert await _rows(conn, "agent_threads") == [
            {**row, "queue_paused_reason": None, "queue_settled_job_id": None} for row in threads
        ]
        assert await _rows(conn, "agent_job_attachments") == [
            {**row, "pending_message_id": None} for row in uploads
        ]
        # Every existing transcript row is what it was: a prompt or a reply.
        assert transcript
        assert await _rows(conn, "agent_thread_messages") == [
            {**row, "mid_turn": False} for row in transcript
        ]
        # Positions come from one sequence, in insertion order.
        first = await conn.fetchval(
            "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
            "VALUES ('apmsg_one', $1, $2, 'queued', 'first') RETURNING position",
            job["thread_id"],
            job["user_id"],
        )
        second = await conn.fetchval(
            "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
            "VALUES ('apmsg_two', $1, $2, 'held', 'second') RETURNING position",
            job["thread_id"],
            job["user_id"],
        )
        assert second > first
    finally:
        await conn.close()


async def test_the_schema_refuses_what_the_queue_cannot_represent(store):
    job = await _create_job(store)
    config = _db_config()
    conn = await asyncpg.connect(**config)
    try:
        refused = [
            # A state the queue does not have.
            (
                "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
                "VALUES ('apmsg_bad', $1, $2, 'delivered', 'x')"
            ),
            # An empty message.
            (
                "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
                "VALUES ('apmsg_bad', $1, $2, 'queued', '')"
            ),
            # A key without the fingerprint that says what it named.
            (
                "INSERT INTO agent_pending_messages "
                "(id, thread_id, user_id, state, prompt, client_request_id) "
                "VALUES ('apmsg_bad', $1, $2, 'queued', 'x', 'key-1')"
            ),
            # A pause reason nothing writes.
            (
                "UPDATE agent_threads SET queue_paused_reason = 'unknown' "
                "WHERE id = $1 AND user_id = $2"
            ),
        ]
        for statement in refused:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(statement, job["thread_id"], job["user_id"])
        # A reply is never a message the owner sent into the turn.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, mid_turn) "
                "VALUES ($1, $2, 'assistant', 'x', TRUE)",
                job["thread_id"],
                job["id"],
            )
        # One prompt per turn, as before; a message sent into the running turn
        # is another user row of the same job, and there may be several.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO agent_thread_messages (thread_id, job_id, role, content) "
                "VALUES ($1, $2, 'user', 'a second prompt')",
                job["thread_id"],
                job["id"],
            )
        for text in ("also check the docs", "and the changelog"):
            await conn.execute(
                "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, mid_turn) "
                "VALUES ($1, $2, 'user', $3, TRUE)",
                job["thread_id"],
                job["id"],
                text,
            )
        # One key per owner, across every conversation they have.
        await conn.execute(
            "INSERT INTO agent_pending_messages "
            "(id, thread_id, user_id, state, prompt, client_request_id, "
            " client_request_fingerprint) "
            "VALUES ('apmsg_key', $1, $2, 'queued', 'x', 'key-1', 'fp')",
            job["thread_id"],
            job["user_id"],
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO agent_pending_messages "
                "(id, thread_id, user_id, state, prompt, client_request_id, "
                " client_request_fingerprint) "
                "VALUES ('apmsg_key2', $1, $2, 'queued', 'y', 'key-1', 'fp2')",
                job["thread_id"],
                job["user_id"],
            )
    finally:
        await conn.close()


async def test_downgrade_refuses_to_drop_a_waiting_message_then_restores_the_schema(store):
    job = await _create_job(store)
    upload = await store.attachments.create_attachment(
        user_id=job["user_id"], path="notes.txt", content=b"notes", content_type="text/plain"
    )
    config = _db_config()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        threads = await _rows(conn, "agent_threads")
        uploads = await _rows(conn, "agent_job_attachments")
    finally:
        await conn.close()
    apply_migrations(config, _THIS)
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute(
            "INSERT INTO agent_pending_messages (id, thread_id, user_id, state, prompt) "
            "VALUES ('apmsg_waiting', $1, $2, 'queued', 'still to send')",
            job["thread_id"],
            job["user_id"],
        )
        await conn.execute(
            "UPDATE agent_job_attachments SET pending_message_id = 'apmsg_waiting' WHERE id = $1",
            upload["id"],
        )
    finally:
        await conn.close()

    # A waiting message exists nowhere else: dropping it would lose what the
    # owner wrote without a word.
    with pytest.raises(Exception, match="Send or delete pending messages before downgrading"):
        downgrade_to(config, _BEFORE)

    conn = await asyncpg.connect(**config)
    try:
        assert await conn.fetchval("SELECT count(*) FROM agent_pending_messages") == 1
        # Deleting the message releases its upload back to staged.
        await conn.execute("DELETE FROM agent_pending_messages WHERE id = 'apmsg_waiting'")
        assert (
            await conn.fetchval(
                "SELECT pending_message_id FROM agent_job_attachments WHERE id = $1",
                upload["id"],
            )
            is None
        )
    finally:
        await conn.close()

    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert await _rows(conn, "agent_threads") == threads
        assert await _rows(conn, "agent_job_attachments") == uploads
        assert await conn.fetchval("SELECT to_regclass('agent_pending_messages')") is None
        assert (
            await conn.fetchval("SELECT to_regclass('agent_pending_message_position_seq')") is None
        )
    finally:
        await conn.close()
    apply_migrations(config)


async def test_downgrade_refuses_while_a_transcript_holds_a_message_sent_into_a_turn(store):
    job = await _create_job(store)
    config = _db_config()
    conn = await asyncpg.connect(**config)
    try:
        await conn.execute(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, mid_turn) "
            "VALUES ($1, $2, 'user', 'also check the docs', TRUE)",
            job["thread_id"],
            job["id"],
        )
    finally:
        await conn.close()

    # The older schema allows one user row per job; this job has two, and
    # dropping one would rewrite a conversation the owner already had.
    with pytest.raises(Exception, match="messages sent into running turns"):
        downgrade_to(config, _BEFORE)

    conn = await asyncpg.connect(**config)
    try:
        await conn.execute("DELETE FROM agent_thread_messages WHERE mid_turn")
    finally:
        await conn.close()
    downgrade_to(config, _BEFORE)
    conn = await asyncpg.connect(**config)
    try:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'agent_thread_messages' AND column_name = 'mid_turn'"
            )
            == 0
        )
        # The index is the one-user-row-per-job index again.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO agent_thread_messages (thread_id, job_id, role, content) "
                "VALUES ($1, $2, 'user', 'a second user row')",
                job["thread_id"],
                job["id"],
            )
    finally:
        await conn.close()
    apply_migrations(config)
