"""PostgreSQL thread-event and session query behavior."""

from __future__ import annotations

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.integration.storage.test_agent_job_store import (  # noqa: F401 - pytest fixtures
    Storage,
    _claim,
    _create_job,
    _request_publication,
    _reserve_shared_chat,
    append_follow_up,
    asyncio,
    pool,
    pytest,
    store,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_thread_events_span_every_turn_and_page(pool, store: Storage):
    """A thread's complete log stays ordered and pageable across follow-ups."""
    first = await _create_job(store)
    claim = await _claim(store, worker_id="w1", lease_ttl_seconds=60)
    assert claim is not None
    for kind in ("tool_use", "tool_result", "message"):
        await store.attempt_commands.append_event(
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
            event_type=kind,
            payload={"marker": kind},
        )
    await store.attempt_commands.transition(
        job_id=first["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="failed",
        detail="synthetic first-turn failure",
    )
    second = await append_follow_up(
        pool, parent_job_id=first["id"], user_id="user-1", prompt="and the docs"
    )
    assert second is not None

    events = await store.execution_queries.list_thread_events_after(
        job_id=second["id"], user_id="user-1"
    )
    assert events is not None
    assert [event["event_type"] for event in events] == ["tool_use", "tool_result", "message"]
    assert all(event["turn_no"] == 1 for event in events)
    assert events[0]["payload"] == {"marker": "tool_use"}

    page = await store.execution_queries.list_thread_events_after(
        job_id=second["id"], user_id="user-1", limit=2
    )
    assert page is not None and len(page) == 2
    rest = await store.execution_queries.list_thread_events_after(
        job_id=second["id"], user_id="user-1", after_id=page[-1]["id"]
    )
    assert rest is not None and [event["event_type"] for event in rest] == ["message"]


async def test_thread_events_stop_at_the_owner(store: Storage):
    mine = await _create_job(store)

    assert (
        await store.execution_queries.list_thread_events_after(job_id=mine["id"], user_id="user-2")
        is None
    )
    assert (
        await store.execution_queries.list_thread_events_after(job_id=mine["id"], user_id="user-1")
        == []
    )


async def test_session_pages_count_threads_and_keep_long_conversation_titles(pool, store: Storage):
    """A long thread cannot consume the history window or replace its title."""
    jobs = [await _create_job(store, task_prompt=f"History task {index}") for index in range(55)]
    long_thread = jobs[0]
    for index in range(60):
        child = await append_follow_up(
            pool, parent_job_id=long_thread["id"], user_id="user-1", prompt=f"Follow-up {index}"
        )
        assert child is not None and child["turn_no"] == index + 2
    await store.conversation_commands.set_sessions_archived(
        thread_ids=[job["thread_id"] for job in jobs], user_id="user-1", archived=True
    )
    first = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, limit=50
    )
    assert first["total"] == 55 and len(first["sessions"]) == 50
    assert first["next_cursor"] is not None
    # Removing a preceding row must not skip the next conversation, as an
    # offset cursor would. Counts reflect the current owner history.
    await store.conversation_commands.set_sessions_archived(
        thread_ids=[first["sessions"][0]["thread_id"]], user_id="user-1", archived=False
    )
    second = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, limit=50, cursor=first["next_cursor"]
    )
    assert second["total"] == 54 and len(second["sessions"]) == 5
    assert second["next_cursor"] is None
    all_sessions = first["sessions"] + second["sessions"]
    assert {session["thread_id"] for session in all_sessions} == {job["thread_id"] for job in jobs}
    long_session = next(row for row in all_sessions if row["thread_id"] == long_thread["thread_id"])
    assert long_session["title"] == "History task 0"
    assert long_session["job"]["turn_no"] == 61
    assert long_session["job"]["task_prompt"] == "Follow-up 59"
    assert long_session["job"]["archived_at"] == long_session["archived_at"]


async def test_session_search_state_and_project_facets_apply_before_pagination(
    pool,
    store: Storage,
):
    target = await _create_job(store, task_prompt="Repair 100%_ Coverage", repo="acme/api")
    other_project = await _create_job(store, task_prompt="Repair Dashboard", repo="acme/web")
    needs_review = await _create_job(store, task_prompt="Repair renderer", repo="acme/api")
    foreign = await _create_job(store, user_id="user-2", task_prompt="Repair 100%_ Coverage")
    for job in (target, other_project, needs_review, foreign):
        await store.conversation_commands.set_thread_archived(
            job_id=job["id"], user_id=job["user_id"], archived=True
        )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', "
            "native_turn_no = COALESCE(native_turn_no, turn_no)"
        )
        await conn.execute(
            "UPDATE agent_jobs SET published_pr_url = 'https://example.test/pr/1' "
            "WHERE id = ANY($1::text[])",
            [target["id"], other_project["id"]],
        )
    literal = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, search="100%_"
    )
    assert [row["thread_id"] for row in literal["sessions"]] == [target["thread_id"]]
    found = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, search="rEPAir", state="done", repo="acme/api", limit=1
    )
    assert found["total"] == 1
    assert found["projects"] == [{"repo": "acme/api", "count": 1}, {"repo": "acme/web", "count": 1}]
    assert found["sessions"][0]["thread_id"] == target["thread_id"]
    review = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, state="needs_review"
    )
    assert review["total"] == 1 and review["sessions"][0]["thread_id"] == needs_review["thread_id"]
    missing = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, repo="acme/missing"
    )
    assert missing["total"] == 0 and missing["sessions"] == []
    assert missing["projects"] == [
        {"repo": "acme/api", "count": 2},
        {"repo": "acme/web", "count": 1},
    ]


async def test_idle_sessions_filter_latest_turn_before_pagination_and_facets(pool, store: Storage):
    completed = await _create_job(store, task_prompt="Completed", repo="acme/api")
    published = await _create_job(store, task_prompt="Published", repo="acme/web")
    stopped = await _create_job(store, task_prompt="Stopped", repo="acme/api")
    older_completed = await _create_job(store, task_prompt="Continued", repo="acme/api")
    follow_up = await append_follow_up(
        pool, parent_job_id=older_completed["id"], user_id="user-1", prompt="Continue working"
    )
    assert follow_up is not None and follow_up["state"] == "queued"
    foreign = await _create_job(store, user_id="user-2", repo="acme/api")
    unarchived = await _create_job(store, repo="acme/api")
    excluded = [
        await _create_job(store, task_prompt=state) for state in ("failed", "queued", "running")
    ]
    states = [
        (completed, "succeeded"),
        (published, "succeeded"),
        (stopped, "cancelled"),
        (older_completed, "succeeded"),
        (foreign, "succeeded"),
        (unarchived, "succeeded"),
        *zip(excluded, ("failed", "queued", "running"), strict=True),
    ]
    for job, state in states:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_jobs SET state = $2, "
                "native_turn_no = CASE WHEN $2 = 'succeeded' THEN turn_no ELSE NULL END "
                "WHERE id = $1",
                job["id"],
                state,
            )
        if job is not unarchived:
            await store.conversation_commands.set_thread_archived(
                job_id=job["id"], user_id=job["user_id"], archived=True
            )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET published_pr_url = 'https://example.test/pr/1' WHERE id = $1",
            published["id"],
        )
    cursor = None
    found = []
    for _ in range(3):
        page = await store.conversation_queries.list_sessions(
            user_id="user-1", archived=True, state="idle", limit=1, cursor=cursor
        )
        assert page["total"] == 3
        assert page["projects"] == [
            {"repo": "acme/api", "count": 2},
            {"repo": "acme/web", "count": 1},
        ]
        found.extend(page["sessions"])
        cursor = page["next_cursor"]
    assert cursor is None
    assert {row["thread_id"] for row in found} == {
        completed["thread_id"],
        published["thread_id"],
        stopped["thread_id"],
    }
    assert {row["job"]["state"] for row in found} == {"succeeded", "cancelled"}

    project = await store.conversation_queries.list_sessions(
        user_id="user-1", archived=True, state="idle", repo="acme/api"
    )
    assert project["total"] == 2
    assert {row["thread_id"] for row in project["sessions"]} == {
        completed["thread_id"],
        stopped["thread_id"],
    }
    assert project["projects"] == page["projects"]


async def test_conversation_state_keeps_active_work_visible_after_cancelling_queued_turn(
    pool,
    store: Storage,
):
    first = await _create_job(store)
    follow_up = await append_follow_up(
        pool, parent_job_id=first["id"], user_id="user-1", prompt="Queued follow-up"
    )
    assert follow_up is not None and follow_up["state"] == "queued"
    assert (await store.conversation_queries.get_job(first["id"]))["conversation_state"] == "queued"
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'running' WHERE id = $1", first["id"])
    assert (
        await store.attempt_commands.request_cancel(job_id=follow_up["id"], user_id="user-1")
        == "cancelled"
    )

    for job_id in (first["id"], follow_up["id"]):
        assert (await store.conversation_queries.get_job(job_id))["conversation_state"] == "running"
    conversation = (await store.conversation_queries.list_conversations(user_id="user-1"))[
        "conversations"
    ][0]["job"]
    assert conversation["id"] == follow_up["id"]
    assert conversation["state"] == "cancelled"
    assert conversation["conversation_state"] == "running"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="running"))[
        "total"
    ] == 1
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="idle"))[
        "total"
    ] == 0

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', native_turn_no = 1 WHERE id = $1",
            first["id"],
        )
    assert (await store.conversation_queries.get_job(first["id"]))["conversation_state"] == "idle"
    assert (await store.conversation_queries.get_job(follow_up["id"]))[
        "conversation_state"
    ] == "idle"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="idle"))[
        "total"
    ] == 1

    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'failed' WHERE id = $1", follow_up["id"])
    historical = await store.conversation_queries.get_job(first["id"])
    assert historical["state"] == "succeeded"
    assert historical["conversation_state"] == "failed"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="failed"))[
        "total"
    ] == 1
    assert (await store.conversation_queries.list_sessions(user_id="user-1", state="idle"))[
        "total"
    ] == 0


async def test_session_cursor_is_scoped_to_owner_and_filters(store: Storage):
    for _ in range(2):
        job = await _create_job(store)
        await store.conversation_commands.set_thread_archived(
            job_id=job["id"], user_id="user-1", archived=True
        )
    page = await store.conversation_queries.list_sessions(user_id="user-1", archived=True, limit=1)
    assert page["next_cursor"] is not None
    for overrides in (
        {"user_id": "user-2"},
        {"archived": False},
        {"search": "another title"},
        {"repo": "different/repo"},
        {"state": "queued"},
        {"cursor": "bad cursor"},
    ):
        args = {"user_id": "user-1", "archived": True, "cursor": page["next_cursor"], **overrides}
        with pytest.raises(ValueError, match="Invalid session cursor"):
            await store.conversation_queries.list_sessions(**args)


async def test_session_pages_can_skip_summary_without_changing_rows_or_cursor(store: Storage):
    for repo in ("acme/api", "acme/api", "acme/web"):
        job = await _create_job(store, repo=repo)
        await store.conversation_commands.set_thread_archived(
            job_id=job["id"], user_id="user-1", archived=True
        )
    options = {"user_id": "user-1", "archived": True, "repo": "acme/api", "limit": 1}
    summary = await store.conversation_queries.list_sessions(**options)
    rows = await store.conversation_queries.list_sessions(**options, include_summary=False)
    assert rows == {**summary, "total": None, "projects": None}
    assert summary["projects"] == [
        {"repo": "acme/api", "count": 2},
        {"repo": "acme/web", "count": 1},
    ]
    assert rows["next_cursor"] is not None
    next_rows = await store.conversation_queries.list_sessions(
        **options, cursor=rows["next_cursor"], include_summary=False
    )
    assert next_rows["total"] is None and next_rows["projects"] is None
    assert next_rows["sessions"][0]["thread_id"] != rows["sessions"][0]["thread_id"]
    assert next_rows["sessions"][0]["job"]["repo"] == "acme/api"
    assert next_rows["next_cursor"] is None


async def test_session_mutations_are_atomic_owner_scoped_and_irreversible(pool, store: Storage):
    mine = await _create_job(store)
    busy = await _create_job(store)
    foreign = await _create_job(store, user_id="user-2")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'failed' WHERE id = $1", mine["id"])
    assert await store.conversation_commands.set_sessions_archived(
        thread_ids=[mine["thread_id"], foreign["thread_id"]], user_id="user-1", archived=True
    ) == {"error": "not_found"}
    assert (await store.conversation_queries.get_job(mine["id"]))["archived_at"] is None
    selection = [mine["thread_id"], busy["thread_id"]]
    archived = await store.conversation_commands.set_sessions_archived(
        thread_ids=selection, user_id="user-1", archived=True
    )
    assert len(archived["sessions"]) == 2
    assert len({row["archived_at"] for row in archived["sessions"]}) == 1
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[mine["thread_id"], foreign["thread_id"]], user_id="user-1"
    ) == {"error": "not_found"}
    assert (await store.conversation_queries.get_job(mine["id"]))["deleted_at"] is None
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[mine["thread_id"]], user_id="user-1"
    ) == {"thread_ids": [mine["thread_id"]], "released_residencies": []}
    retained = await store.conversation_queries.get_job(mine["id"])
    assert retained is not None and retained["deleted_at"] is not None
    assert retained["task_prompt"] == mine["task_prompt"]
    assert (
        await store.conversation_queries.get_thread_for_job(job_id=mine["id"], user_id="user-1")
        is None
    )
    assert (
        await store.execution_queries.list_thread_events_after(job_id=mine["id"], user_id="user-1")
        is None
    )
    assert await store.execution_queries.thread_stats(job_id=mine["id"], user_id="user-1") is None
    assert (
        await store.conversation_commands.set_thread_archived(
            job_id=mine["id"], user_id="user-1", archived=False
        )
        is None
    )
    assert (
        await store.conversation_commands.set_thread_pinned(
            job_id=mine["id"], user_id="user-1", pinned=True
        )
        is None
    )
    assert await store.conversation_commands.set_sessions_archived(
        thread_ids=selection, user_id="user-1", archived=False
    ) == {"error": "not_found"}
    assert (await store.conversation_queries.get_job(busy["id"]))["archived_at"] is not None
    assert (
        await append_follow_up(
            pool, parent_job_id=mine["id"], user_id="user-1", prompt="must not resurrect"
        )
        is None
    )
    assert (
        await store.session_fork.prepare_session_fork(
            source_job_id=mine["id"], user_id="user-1", idempotency_key="deleted-history"
        )
        is None
    )
    assert {
        job["id"]
        for job in await store.conversation_queries.list_jobs(user_id="user-1", archived=True)
    } == {busy["id"]}
    assert (await store.conversation_queries.list_sessions(user_id="user-1", archived=True))[
        "total"
    ] == 1
    assert {
        item["job"]["id"]
        for item in (
            await store.conversation_queries.list_conversations(
                user_id="user-1", include_archived=True
            )
        )["conversations"]
    } == {busy["id"]}
    assert (await store.conversation_queries.list_projects(user_id="user-1", archived=True))[0][
        "task_count"
    ] == 1


async def test_archiving_serializes_with_follow_up_creation(pool, store: Storage):
    job = await _create_job(store)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE agent_threads SET archived_at = NOW() WHERE id = $1", job["thread_id"]
        )
        follow_up = asyncio.create_task(
            append_follow_up(
                pool, parent_job_id=job["id"], user_id="user-1", prompt="racing message"
            )
        )
        await asyncio.sleep(0.03)
        assert not follow_up.done()
    assert await asyncio.wait_for(follow_up, timeout=3) == {"error": "session_archived"}
    assert (
        len(
            (
                await store.conversation_queries.get_thread_for_job(
                    job_id=job["id"], user_id="user-1"
                )
            )["jobs"]
        )
        == 1
    )
    await store.conversation_commands.set_sessions_archived(
        thread_ids=[job["thread_id"]], user_id="user-1", archived=False
    )
    resumed = await append_follow_up(
        pool, parent_job_id=job["id"], user_id="user-1", prompt="resume"
    )
    assert resumed is not None and resumed["turn_no"] == 2


async def test_deleting_an_idle_shared_session_keeps_workspace_sibling_and_billing(
    pool,
    store: Storage,
):
    """Deleting a chat ends its residency but not the shared container's lifetime."""
    source, claim, sandbox = await _reserve_shared_chat(store)
    fence = {
        "thread_id": source["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-history-delete", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=source["id"]
    )
    assert await store.attempt_commands.append_event(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        event_type="message",
        payload={"text": "Retained answer for the source session"},
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=source["id"],
        context_status="live",
        native_session_id="native-source-history",
        idle_timeout_seconds=1800,
        base_sha=source["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=source["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )
    await _request_publication(store, source)
    await store.conversation_commands.set_thread_archived(
        job_id=source["id"], user_id="user-1", archived=True
    )
    pending = await store.conversation_queries.list_sessions(user_id="user-1", archived=True)
    assert pending["sessions"][0]["delete_block_reason"] == "publication_pending"
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[source["thread_id"]], user_id="user-1"
    ) == {"error": "publication_pending"}
    sibling = await _create_job(store, task_prompt="forked sibling")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandbox_snapshots SET state = 'published'")
        await conn.execute(
            "UPDATE agent_jobs SET state = 'succeeded', "
            "native_turn_no = COALESCE(native_turn_no, turn_no), fork_source_job_id = $2 WHERE id = $1",
            sibling["id"],
            source["id"],
        )
        await conn.execute(
            """
            INSERT INTO agent_thread_sandbox_memberships (
                thread_id, user_id, sandbox_id, native_session_id, native_state, context_status,
                resident_state, last_settled_job_id, forked_from_thread_id, fork_anchor_job_id
            ) VALUES ($1, (SELECT user_id FROM agent_threads WHERE id = $1), $2, 'native-sibling-history', 'ready', 'live', 'live', $3, $4, $5)
            """,
            sibling["thread_id"],
            sandbox["sandbox_id"],
            sibling["id"],
            source["thread_id"],
            source["id"],
        )
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET residency_generation = 1, residency_status = 'hot',
                residency_lease_expires_at = NOW() + INTERVAL '1 hour',
                residency_grant_id = 'grant-history'
            WHERE thread_id = $1 AND sandbox_id = $2
            """,
            source["thread_id"],
            sandbox["sandbox_id"],
        )
        await conn.execute(
            """
            INSERT INTO agent_attempt_ledger
                (attempt_id, grant_id, residency_generation, binding_id, window_since, snapshot)
            VALUES ($1, 'grant-history', 1, 'binding-test', NOW(), '{"spent_usd": 0.42}')
            """,
            claim["attempt_id"],
        )
    sibling_turn = await append_follow_up(
        pool, parent_job_id=sibling["id"], user_id="user-1", prompt="independent work"
    )
    assert sibling_turn is not None and sibling_turn["state"] == "queued"
    before = await store.sandbox.thread_sandbox(thread_id=source["thread_id"])
    assert before["resident_state"] == "live"
    assert (await store.conversation_queries.list_sessions(user_id="user-1", archived=True))[
        "sessions"
    ][0]["can_delete"]
    assert await store.conversation_commands.delete_sessions(
        thread_ids=[source["thread_id"]], user_id="user-1"
    ) == {
        "thread_ids": [source["thread_id"]],
        "released_residencies": [
            {
                "thread_id": source["thread_id"],
                "generation": 1,
                "host": "shared-runner",
                "grant_id": "grant-history",
            }
        ],
    }
    assert await store.sandbox.thread_sandbox(thread_id=source["thread_id"]) == before
    assert (await store.conversation_queries.get_job(sibling_turn["id"]))["state"] == "queued"
    assert (
        await store.conversation_queries.get_thread_for_job(job_id=sibling["id"], user_id="user-1")
        is not None
    )
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT snapshot FROM agent_attempt_ledger WHERE attempt_id = $1",
                claim["attempt_id"],
            )
            == '{"spent_usd": 0.42}'
        )
        assert (
            await conn.fetchval(
                "SELECT residency_grant_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
                source["thread_id"],
            )
            == "grant-history"
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_job_events WHERE job_id = $1", source["id"]
            )
            > 0
        )
    # The deleted chat's process loses its credential with the chat: the
    # host's next beat for that generation extends nothing, and comes back in
    # residencies_lost so the host closes the process.
    assert (
        await store.residency.heartbeat_residency(
            thread_id=source["thread_id"], generation=1, host="shared-runner", ttl_seconds=120
        )
        is None
    )


async def test_deleting_a_working_session_cancels_its_turns_and_goal(pool, store: Storage):
    """Permanent deletion need not wait for archive or idleness; it ends the work."""
    from cloud_agent.goals.models import GoalInput

    queued = await _create_job(store)
    running = await _create_job(store)
    assert await store.goals.set_goal(
        thread_id=queued["thread_id"],
        user_id="user-1",
        goal=GoalInput(objective="ship it", max_rounds=5),
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'running' WHERE id = $1", running["id"])

    # Neither chat is archived, and one has a turn running.
    selection = [queued["thread_id"], running["thread_id"]]
    assert await store.conversation_commands.delete_sessions(
        thread_ids=selection, user_id="user-1"
    ) == {"thread_ids": selection, "released_residencies": []}

    async with pool.acquire() as conn:
        jobs = {
            row["id"]: row
            for row in await conn.fetch(
                "SELECT id, state, cancel_requested FROM agent_jobs WHERE id = ANY($1::text[])",
                [queued["id"], running["id"]],
            )
        }
    # A claim does not read the thread tombstone, so the queued turn must not
    # stay queued; the running one stops through the worker's own transition.
    assert (jobs[queued["id"]]["state"], jobs[queued["id"]]["cancel_requested"]) == (
        "cancelled",
        True,
    )
    assert (jobs[running["id"]]["state"], jobs[running["id"]]["cancel_requested"]) == (
        "running",
        True,
    )
    goal = await store.goals.get_goal(thread_id=queued["thread_id"])
    assert goal is not None
    assert (goal["state"], goal["pause_reason"]) == ("paused", "cancelled")
