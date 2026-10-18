"""Conversation reads, against a real database.

The unit tier drives these through fakes, so it pins what the *code* does with
rows it is handed. What only Postgres can settle is the thing every one of
these six methods turns on: **the scope is in the query, not in the caller**.

- `get_thread_for_job` and `get_job` answer for their owner and nobody else. A
  missing `WHERE user_id` reads exactly like a correct one until two people
  have jobs, at which point one is reading the other's task prompt.
- `list_projects` groups by repo and filters by archive state; the scoping is
  in the join, so a user's project list cannot contain a repo only somebody
  else has run in.
- `list_conversations` and `list_sessions` page, and a page that is scoped
  wrongly is still a page -- which is why the filtering is asserted here rather
  than only the shape of the result.

Setup goes through the store, because a job is ConversationCommands' to create
and that capability has not moved yet. Every assertion goes through this
repository.

`latest_assistant_message_state` is here for what only Postgres can settle
about it: which rows it reads. Every automatic round calls it as it finishes.
"""

from __future__ import annotations

import json
import os
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from tests.integration.storage.test_agent_job_store import _create_job
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations

from cloud_agent.domain.execution import JobStreamState
from cloud_agent.storage.assembly import Storage
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-1"
_OTHER = "user-2"


@pytest_asyncio.fixture
async def db():
    """A clean database over the shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")

    config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(config)

    async def _reset() -> None:
        # The shared helper owns the order; a hand-written list of tables
        # misses whichever child was added last, and the failure is a foreign
        # key violation in a fixture rather than in the test.
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)

    await _reset()
    try:
        yield pool
    finally:
        await _reset()
        await pool.close()


@pytest_asyncio.fixture
async def store(db):
    """Everything this test needs, to create a job."""
    return Storage(db)


@pytest_asyncio.fixture
async def queries(db):
    """The capability under test, on the repository that owns it."""
    return PostgresConversationQueries(db)


# ── Ownership ─────────────────────────────────────────────────────────


async def test_a_job_is_only_read_by_its_owner(store, queries: PostgresConversationQueries) -> None:
    """Another user's id answers nothing, and answers it as absence."""
    job = await _create_job(store)

    assert await queries.get_job(job["id"]) is not None
    assert await queries.get_thread_for_job(job_id=job["id"], user_id=_OWNER) is not None
    assert await queries.get_thread_for_job(job_id=job["id"], user_id=_OTHER) is None


async def test_an_unknown_job_answers_nothing(queries: PostgresConversationQueries) -> None:
    """Absence and refusal are the same answer, which callers rely on."""
    assert await queries.get_job("ajob_does_not_exist") is None
    assert await queries.get_thread_for_job(job_id="ajob_does_not_exist", user_id=_OWNER) is None


async def test_stream_state_is_current_and_scoped_to_the_owner(
    store, queries: PostgresConversationQueries, db
) -> None:
    job = await _create_job(store, user_id=_OWNER)
    assert await queries.get_job_stream_state(job_id=job["id"], user_id=_OWNER) == JobStreamState(
        "queued", None
    )
    assert await queries.get_job_stream_state(job_id=job["id"], user_id=_OTHER) is None
    assert await queries.get_job_stream_state(job_id="ajob_missing", user_id=_OWNER) is None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1, "
            "published_pr_url = $2 WHERE id = $1",
            job["id"],
            "https://example.test/pr/42",
        )
    assert await queries.get_job_stream_state(job_id=job["id"], user_id=_OWNER) == JobStreamState(
        "succeeded", "https://example.test/pr/42"
    )


async def test_the_job_list_is_scoped_to_its_owner(
    store, queries: PostgresConversationQueries
) -> None:
    """Two users, one page each: neither sees the other's job."""
    mine = await _create_job(store, user_id=_OWNER, task_prompt="mine")
    theirs = await _create_job(store, user_id=_OTHER, task_prompt="theirs")

    mine_page = [row["id"] for row in await queries.list_jobs(user_id=_OWNER)]
    theirs_page = [row["id"] for row in await queries.list_jobs(user_id=_OTHER)]

    assert mine["id"] in mine_page and theirs["id"] not in mine_page
    assert theirs["id"] in theirs_page and mine["id"] not in theirs_page


async def test_the_job_list_narrows_to_one_repo(
    store, queries: PostgresConversationQueries
) -> None:
    """``repo`` is how the sidebar pages a single project past the global window."""
    here = await _create_job(store, repo="org/here")
    there = await _create_job(store, repo="org/there")

    page = [row["id"] for row in await queries.list_jobs(user_id=_OWNER, repo="org/here")]

    assert here["id"] in page
    assert there["id"] not in page


# ── Projects ──────────────────────────────────────────────────────────


async def test_projects_group_by_repo_and_stay_scoped(
    store, queries: PostgresConversationQueries
) -> None:
    """A repo only somebody else has run in is not this user's project."""
    await _create_job(store, user_id=_OWNER, repo="org/mine")
    await _create_job(store, user_id=_OWNER, repo="org/mine")
    await _create_job(store, user_id=_OTHER, repo="org/theirs")

    projects = await queries.list_projects(user_id=_OWNER)

    assert [row["repo"] for row in projects] == ["org/mine"]
    assert projects[0]["task_count"] == 2


async def test_an_archived_project_leaves_the_default_list(
    store, queries: PostgresConversationQueries
) -> None:
    """The archive filter is applied to the thread, not to the jobs."""
    job = await _create_job(store, repo="org/archived")
    await store.conversation_commands.set_thread_archived(
        job_id=job["id"], user_id=_OWNER, archived=True
    )

    assert await queries.list_projects(user_id=_OWNER) == []
    assert [row["repo"] for row in await queries.list_projects(user_id=_OWNER, archived=True)] == [
        "org/archived"
    ]


# ── Paged reads ───────────────────────────────────────────────────────


async def test_a_conversation_page_carries_its_own_thread(
    store, queries: PostgresConversationQueries
) -> None:
    """The page is complete conversations, keyed by the thread they belong to."""
    job = await _create_job(store, task_prompt="a conversation")

    page = await queries.list_conversations(user_id=_OWNER)

    # A conversation carries its thread's identity and its latest turn.
    assert "conversations" in page and "next_cursor" in page
    assert [item["job"]["thread_id"] for item in page["conversations"]] == [job["thread_id"]]


async def test_a_conversation_page_stays_scoped(
    store, queries: PostgresConversationQueries
) -> None:
    """Another user's conversation is not in this user's page."""
    await _create_job(store, user_id=_OTHER, task_prompt="not mine")

    page = await queries.list_conversations(user_id=_OWNER)

    assert page["conversations"] == []


async def test_a_session_page_stays_scoped(store, queries: PostgresConversationQueries) -> None:
    """The session list is the same scoping question, answered in another shape."""
    job = await _create_job(store, task_prompt="a session")
    await _create_job(store, user_id=_OTHER, task_prompt="not mine")

    page = await queries.list_sessions(user_id=_OWNER)

    assert [row["thread_id"] for row in page["sessions"]] == [job["thread_id"]]


async def test_a_row_only_session_page_skips_the_history_aggregate(
    store, queries: PostgresConversationQueries
) -> None:
    """``include_summary=False`` returns null counts rather than wrong ones."""
    await _create_job(store)

    page = await queries.list_sessions(user_id=_OWNER, include_summary=False)

    assert page["sessions"], "the page should still list the row"
    assert page["total"] is None and page["projects"] is None


# ── The final message of a turn ───────────────────────────────────────

#: Conversations whose transcripts share the table with the one under test.
_TRANSCRIPT_CHATS = 1000

#: The messages the conversation under test holds: two turns, one reply twice.
_OWN_MESSAGES = 5


def _plan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node of one ``EXPLAIN (FORMAT JSON)`` plan, subplans included."""
    return [node, *(item for child in node.get("Plans", []) for item in _plan_nodes(child))]


async def _seed_transcripts(conn: asyncpg.Connection, turns: list[int]) -> None:
    """Every conversation's prompt and reply for ``turns``, oldest first."""
    await conn.execute(
        """
        INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
        SELECT j.thread_id, j.id, role.name, role.name || ' of ' || j.id
        FROM agent_jobs j CROSS JOIN (VALUES ('user'), ('assistant')) AS role(name)
        WHERE j.turn_no = ANY($1::int[])
        ORDER BY j.turn_no, j.id, role.name DESC
        """,
        turns,
    )


async def test_a_turns_final_message_is_read_from_its_own_conversation(db) -> None:
    """Every automatic round reads its final message as it finishes: one transcript, not all.

    The message table holds every conversation's transcript, and nothing
    indexes a reply by its job. Read by job alone, each finishing round read
    the table until it reached that reply -- all of it, in the plan a large
    table gets -- so the read grew with everything anyone had been answered
    since. The plan check runs the statement the read actually sends.
    """
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads
                (id, user_id, repo, title, title_source, setup_script, execution_profile)
            SELECT 'athr_tx_' || g, $2, 'example-org/example-repo', 'history', 'fallback',
                   NULL, 'trusted-v1'
            FROM generate_series(1, $1) g
            """,
            _TRANSCRIPT_CHATS,
            _OWNER,
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs
                (id, thread_id, user_id, parent_job_id, turn_no, native_turn_no,
                 task_prompt, runtime, model, state)
            SELECT 'ajob_tx_' || g || '_' || turn, 'athr_tx_' || g, $2,
                   CASE WHEN turn = 2 THEN 'ajob_tx_' || g || '_1' END, turn, turn,
                   'p', 'claude-code', 'glm-5.1', 'succeeded'
            FROM generate_series(1, $1) g CROSS JOIN generate_series(1, 2) turn
            """,
            _TRANSCRIPT_CHATS,
            _OWNER,
        )
        await _seed_transcripts(conn, [1])
        # The turn under test answered twice; its last reply is its report.
        await conn.execute(
            "INSERT INTO agent_thread_messages (thread_id, job_id, role, content, truncated) "
            "VALUES ('athr_tx_7', 'ajob_tx_7_1', 'assistant', 'the final answer', TRUE)"
        )
        # Every conversation went on talking after it.
        await _seed_transcripts(conn, [2])
        await conn.execute("ANALYZE agent_jobs")
        await conn.execute("ANALYZE agent_thread_messages")

    captured: list[Any] = []

    async def capture_queries(conn: asyncpg.Connection) -> None:
        conn.add_query_logger(captured.append)

    reading_pool = await asyncpg.create_pool(
        **_db_config(), min_size=1, max_size=1, init=capture_queries
    )
    try:
        state = await PostgresConversationQueries(reading_pool).latest_assistant_message_state(
            job_id="ajob_tx_7_1"
        )
    finally:
        await reading_pool.close()
    assert state == {"content": "the final answer", "truncated": True}

    (statement,) = [item for item in captured if "agent_thread_messages" in item.query]
    async with db.acquire() as conn:
        plan = await conn.fetchval(
            f"EXPLAIN (ANALYZE, FORMAT JSON) {statement.query}", *statement.args
        )
    read = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in _plan_nodes(json.loads(plan)[0]["Plan"])
        if node.get("Relation Name") == "agent_thread_messages"
    )
    assert read <= _OWN_MESSAGES, f"read {read} messages to find one of {_OWN_MESSAGES}"
