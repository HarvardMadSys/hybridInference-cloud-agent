"""PostgreSQL incremental browser notification state."""

from __future__ import annotations

import json

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811
from tests.integration.storage.test_agent_job_store import (  # noqa: F401 - pytest fixtures
    Storage,
    _create_job,
    pool,
    pytest,
    store,
)

from cloud_agent.notification_state import decode_notification_cursor
from cloud_agent.storage import notification_state as notification_state_storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


# Incremental browser notification state


async def _finish_for_notification(pool, job: dict) -> int:
    """Create the terminal attempt projection the notification feed returns."""
    async with pool.acquire() as conn, conn.transaction():
        attempt_id = await conn.fetchval(
            """
            INSERT INTO agent_attempts (
                job_id,thread_id,attempt_no,lease_owner,lease_generation,
                lease_expires_at,status,finished_at
            ) VALUES ($1,$2,1,'notification-test',1,NOW(),'finished',clock_timestamp())
            RETURNING id
            """,
            job["id"],
            job["thread_id"],
        )
        await conn.execute(
            """
            UPDATE agent_jobs
            SET state='failed',current_attempt_id=$2,updated_at=clock_timestamp()
            WHERE id=$1
            """,
            job["id"],
            attempt_id,
        )
    return attempt_id


async def test_notification_state_ignores_lifetime_history_but_bootstraps_recent_work(
    pool, store: Storage
):
    """A thousand archived chats do not enter the active/recent bootstrap."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO agent_threads (
                id,user_id,repo,title,execution_profile,archived_at,created_at,updated_at
            )
            SELECT 'athr_notification_history_' || n,'user-1','owner/history',
                   'Old task ' || n,'trusted-v1',NOW()-interval '25 hours',
                   NOW()-interval '25 hours',NOW()-interval '25 hours'
            FROM generate_series(1,1001) AS n
            """
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs (
                id,thread_id,user_id,turn_no,task_prompt,runtime,model,state,created_at,updated_at
            )
            SELECT 'ajob_notification_history_' || n,
                   'athr_notification_history_' || n,'user-1',1,
                   'old task','claude-code','glm-5.1','cancelled',
                   NOW()-interval '25 hours',NOW()-interval '25 hours'
            FROM generate_series(1,1001) AS n
            """
        )
        await conn.execute(
            """
            UPDATE agent_notification_changes
            SET changed_at=NOW()-interval '25 hours'
            WHERE entity_id LIKE 'a%_notification_history_%'
            """
        )

    recent = await _create_job(store, task_prompt="recent terminal")
    recent_attempt = await _finish_for_notification(pool, recent)
    unclaimed = await _create_job(store, task_prompt="recent unclaimed cancellation")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state='cancelled',updated_at=clock_timestamp() WHERE id=$1",
            unclaimed["id"],
        )
    active = [await _create_job(store, task_prompt=f"active {index}") for index in range(3)]
    await _create_job(store, user_id="user-2", task_prompt="foreign active")

    first = await store.conversation_queries.notification_state(user_id="user-1", limit=200)
    assert first["mode"] == "snapshot"
    assert first["next_cursor"] is None
    assert first["change_cursor"]
    assert {row["id"] for row in first["jobs"]} == {
        recent["id"],
        unclaimed["id"],
        *(job["id"] for job in active),
    }
    terminal = next(row for row in first["jobs"] if row["id"] == recent["id"])
    assert terminal["state"] == "failed"
    assert terminal["current_attempt_id"] == recent_attempt
    assert terminal["finished_at"] is not None
    cancellation = next(row for row in first["jobs"] if row["id"] == unclaimed["id"])
    assert cancellation["state"] == "cancelled"
    assert cancellation["current_attempt_id"] is None
    assert cancellation["finished_at"] is None

    unchanged = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=first["change_cursor"], limit=200
    )
    assert unchanged["jobs"] == []
    assert unchanged["threads"] == []
    assert unchanged["deleted_job_ids"] == []
    assert unchanged["deleted_thread_ids"] == []
    assert unchanged["next_cursor"] is None
    assert unchanged["change_cursor"]


async def test_notification_bootstrap_pages_100k_recent_terminals_with_bounded_index_reads(
    pool, store: Storage, monkeypatch
):
    """Every bootstrap page touches one index-sized slice, not the 24-hour population."""
    count = 100_000
    async with pool.acquire() as conn, conn.transaction():
        # These are synthetic scale rows, not a trigger test. Avoid 200,000
        # irrelevant NOTIFY calls and write the exact derived markers directly.
        await conn.execute("SET LOCAL session_replication_role = 'replica'")
        await conn.execute(
            """
            INSERT INTO agent_threads (id,user_id,repo,title,execution_profile)
            SELECT 'athr_notification_scale_' || lpad(n::text,6,'0'),
                   'notification-scale','owner/scale','Scale ' || n,'trusted-v1'
            FROM generate_series(1,$1) AS n
            """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs (
                id,thread_id,user_id,turn_no,task_prompt,runtime,model,state
            )
            SELECT 'ajob_notification_scale_' || lpad(n::text,6,'0'),
                   'athr_notification_scale_' || lpad(n::text,6,'0'),
                   'notification-scale',1,'scale','claude-code','glm-5.1','cancelled'
            FROM generate_series(1,$1) AS n
            """,
            count,
        )
        await conn.execute("SET LOCAL session_replication_role = 'origin'")
        await conn.execute(
            """
            INSERT INTO agent_notification_changes (
                kind,entity_id,user_id,change_xid,changed_at,terminal
            )
            SELECT 'job','ajob_notification_scale_' || lpad(n::text,6,'0'),
                   'notification-scale',pg_current_xact_id(),
                   NOW() - n * interval '1 microsecond',TRUE
            FROM generate_series(1,$1) AS n
            """,
            count,
        )
    async with pool.acquire() as conn:
        await conn.execute("ANALYZE agent_jobs")
        await conn.execute("ANALYZE agent_notification_changes")

    scans: list[int] = []
    original = notification_state_storage._recent_candidates

    async def counted(*args, **kwargs):
        scans.append(kwargs["scan_limit"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(notification_state_storage, "_recent_candidates", counted)
    first = await store.conversation_queries.notification_state(
        user_id="notification-scale", limit=200
    )
    assert len(first["jobs"]) == 200
    assert first["next_cursor"] and first["change_cursor"] is None
    cursor = decode_notification_cursor(
        first["next_cursor"], user_id="notification-scale", limit=200
    )
    assert cursor is not None
    assert cursor.bootstrap_phase == "recent"
    assert cursor.after_changed_at is not None

    second = await store.conversation_queries.notification_state(
        user_id="notification-scale", cursor=first["next_cursor"], limit=200
    )
    assert len(second["jobs"]) == 200
    assert {row["id"] for row in first["jobs"]}.isdisjoint(row["id"] for row in second["jobs"])
    assert scans == [201, 201]

    async with pool.acquire() as conn:
        raw_plan = await conn.fetchval(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
            + notification_state_storage._RECENT_CANDIDATES_SQL,
            "notification-scale",
            cursor.bootstrap_at,
            None,
            None,
            201,
        )
    plan = json.loads(raw_plan)[0]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    marker_nodes = [
        node for node in nodes(plan) if node.get("Relation Name") == "agent_notification_changes"
    ]
    assert any(
        node.get("Index Name") == "idx_agent_notification_changes_owner_recent"
        for node in marker_nodes
    )
    examined = sum(
        (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0))
        * node.get("Actual Loops", 1)
        for node in marker_nodes
    )
    assert examined <= 201, f"examined {examined} markers for a 200-row page"


async def test_notification_state_recovers_delayed_commit_once_and_reports_deletion(
    pool, store: Storage
):
    """A transaction in the bootstrap snapshot survives a stream gap exactly once."""
    job = await _create_job(store)
    writer = await pool.acquire()
    transaction = writer.transaction()
    await transaction.start()
    try:
        await writer.execute(
            """
            UPDATE agent_jobs
            SET state='cancelled',updated_at=clock_timestamp()
            WHERE id=$1
            """,
            job["id"],
        )
        first = await store.conversation_queries.notification_state(user_id="user-1", limit=200)
        assert [(row["id"], row["state"]) for row in first["jobs"]] == [(job["id"], "queued")]
        await transaction.commit()
    except BaseException:
        await transaction.rollback()
        raise
    finally:
        await pool.release(writer)

    terminal = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=first["change_cursor"], limit=200
    )
    assert [(row["id"], row["state"]) for row in terminal["jobs"]] == [(job["id"], "cancelled")]
    after_terminal = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=terminal["change_cursor"], limit=200
    )
    assert after_terminal["jobs"] == []

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at=clock_timestamp() WHERE id=$1",
            job["thread_id"],
        )
    deleted = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=after_terminal["change_cursor"], limit=200
    )
    assert deleted["deleted_thread_ids"] == [job["thread_id"]]
    assert deleted["jobs"] == []

    await _create_job(store, user_id="user-2")
    isolated = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=deleted["change_cursor"], limit=200
    )
    assert isolated["jobs"] == []
    assert isolated["threads"] == []


async def test_terminal_job_carries_title_revision_before_the_thread_delta_page(
    pool, store: Storage
):
    """A terminal notice has the fresh title even when its thread marker is page two."""
    job = await _create_job(store)
    initial = await store.conversation_queries.notification_state(user_id="user-1", limit=1)
    while initial["next_cursor"]:
        initial = await store.conversation_queries.notification_state(
            user_id="user-1", cursor=initial["next_cursor"], limit=1
        )
    assert initial["change_cursor"]

    async with pool.acquire() as conn:
        thread_revision = await conn.fetchval(
            """
            UPDATE agent_threads SET title='Fresh title' WHERE id=$1
            RETURNING revision
            """,
            job["thread_id"],
        )
    await _finish_for_notification(pool, job)

    first = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=initial["change_cursor"], limit=1
    )
    assert [
        (row["id"], row["title"], row["thread_revision"], row["state"]) for row in first["jobs"]
    ] == [(job["id"], "Fresh title", thread_revision, "failed")]
    assert first["threads"] == []
    assert first["next_cursor"] and first["change_cursor"] is None

    second = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=first["next_cursor"], limit=1
    )
    assert second["jobs"] == []
    assert [(row["id"], row["title"], row["revision"]) for row in second["threads"]] == [
        (job["thread_id"], "Fresh title", thread_revision)
    ]
    assert second["next_cursor"] is None
    assert second["change_cursor"]


async def test_notification_markers_skip_churn_and_expire_explicitly(
    pool, store: Storage, monkeypatch
):
    """Delta traffic prunes stale durable markers while ordinary churn stays push-only."""
    job = await _create_job(store)
    initial = await store.conversation_queries.notification_state(user_id="user-1", limit=200)

    async with pool.acquire() as conn:
        before_thread = await conn.fetchrow(
            """
            SELECT change_xid::text,changed_at
            FROM agent_notification_changes WHERE kind='thread' AND entity_id=$1
            """,
            job["thread_id"],
        )
        before_job = await conn.fetchrow(
            """
            SELECT change_xid::text,changed_at
            FROM agent_notification_changes WHERE kind='job' AND entity_id=$1
            """,
            job["id"],
        )
        await conn.execute(
            "UPDATE agent_threads SET updated_at=clock_timestamp() WHERE id=$1",
            job["thread_id"],
        )
        await conn.execute(
            "UPDATE agent_jobs SET published_pr_url='https://example.test/pr/1' WHERE id=$1",
            job["id"],
        )
        assert (
            await conn.fetchrow(
                """
                SELECT change_xid::text,changed_at
                FROM agent_notification_changes WHERE kind='thread' AND entity_id=$1
                """,
                job["thread_id"],
            )
            == before_thread
        )
        assert (
            await conn.fetchrow(
                """
                SELECT change_xid::text,changed_at
                FROM agent_notification_changes WHERE kind='job' AND entity_id=$1
                """,
                job["id"],
            )
            == before_job
        )

        await conn.execute(
            "UPDATE agent_threads SET title='A durable title' WHERE id=$1",
            job["thread_id"],
        )
        durable_thread = await conn.fetchrow(
            """
            SELECT change_xid::text,changed_at
            FROM agent_notification_changes WHERE kind='thread' AND entity_id=$1
            """,
            job["thread_id"],
        )
        assert durable_thread != before_thread
        removed_through = await conn.fetchval(
            """
            WITH aged AS (
                UPDATE agent_notification_changes
                SET changed_at=NOW()-interval '25 hours'
                WHERE user_id='user-1'
                RETURNING change_xid
            )
            SELECT max(change_xid::text::numeric) FROM aged
            """
        )

    monkeypatch.setattr(notification_state_storage, "_next_prune_at", 0.0)
    expired = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=initial["change_cursor"], limit=200
    )
    assert expired["reset_required"] is True
    assert expired["change_cursor"] is None

    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_notification_changes WHERE user_id='user-1'"
            )
            == 0
        )
        floor = await conn.fetchval(
            """
            SELECT floor_xid::text
            FROM agent_notification_change_floors WHERE user_id='user-1'
            """
        )
    assert int(floor) == int(removed_through) + 1


async def test_notification_delta_pages_recover_an_older_xid_behind_the_page(pool, store: Storage):
    """The target snapshot catches a late lower-XID overwrite exactly once."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads (id,user_id,repo,title,execution_profile)
            VALUES
                ('athr_notification_page_a','user-1','owner/repo','original-a','trusted-v1'),
                ('athr_notification_page_b','user-1','owner/repo','original-b','trusted-v1')
            """
        )
    initial = await store.conversation_queries.notification_state(user_id="user-1", limit=1)
    assert initial["jobs"] == []
    assert initial["change_cursor"]

    older = await pool.acquire()
    transaction = older.transaction()
    await transaction.start()
    committed = False
    try:
        older_xid = int(await older.fetchval("SELECT pg_current_xact_id()::text"))
        async with pool.acquire() as newer:
            await newer.execute(
                "UPDATE agent_threads SET title='visible-a' WHERE id='athr_notification_page_a'"
            )
            await newer.execute(
                "UPDATE agent_threads SET title='visible-b' WHERE id='athr_notification_page_b'"
            )
            newer_xid = int(
                await newer.fetchval(
                    """
                    SELECT change_xid::text FROM agent_notification_changes
                    WHERE kind='thread' AND entity_id='athr_notification_page_b'
                    """
                )
            )
        assert older_xid < newer_xid

        first = await store.conversation_queries.notification_state(
            user_id="user-1", cursor=initial["change_cursor"], limit=1
        )
        assert [(row["id"], row["title"]) for row in first["threads"]] == [
            ("athr_notification_page_a", "visible-a")
        ]
        assert first["next_cursor"] and first["change_cursor"] is None

        await older.execute(
            "UPDATE agent_threads SET title='late-a' WHERE id='athr_notification_page_a'"
        )
        await transaction.commit()
        committed = True
    except BaseException:
        if not committed:
            await transaction.rollback()
        raise
    finally:
        await pool.release(older)

    second = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=first["next_cursor"], limit=1
    )
    assert [(row["id"], row["title"]) for row in second["threads"]] == [
        ("athr_notification_page_b", "visible-b")
    ]
    assert second["next_cursor"] is None
    assert second["change_cursor"]

    behind = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=second["change_cursor"], limit=1
    )
    assert [(row["id"], row["title"]) for row in behind["threads"]] == [
        ("athr_notification_page_a", "late-a")
    ]
    final = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=behind["change_cursor"], limit=1
    )
    assert final["jobs"] == []
    assert final["threads"] == []


async def test_notification_delta_pages_defer_post_target_overwrites(pool, store: Storage):
    """A continuation cannot emit a post-target overwrite and replay it next cycle."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads (id,user_id,repo,title,execution_profile)
            VALUES
                ('athr_notification_target_a','user-1','owner/repo','original-a','trusted-v1'),
                ('athr_notification_target_b','user-1','owner/repo','original-b','trusted-v1')
            """
        )
    initial = await store.conversation_queries.notification_state(user_id="user-1", limit=1)
    assert initial["change_cursor"]

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET title='visible-a' WHERE id='athr_notification_target_a'"
        )
        await conn.execute(
            "UPDATE agent_threads SET title='visible-b' WHERE id='athr_notification_target_b'"
        )

    first = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=initial["change_cursor"], limit=1
    )
    assert [(row["id"], row["title"]) for row in first["threads"]] == [
        ("athr_notification_target_a", "visible-a")
    ]
    assert first["next_cursor"] and first["change_cursor"] is None

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET title='after-target-b' WHERE id='athr_notification_target_b'"
        )

    completed = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=first["next_cursor"], limit=1
    )
    assert completed["threads"] == []
    assert completed["next_cursor"] is None
    assert completed["change_cursor"]

    after_target = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=completed["change_cursor"], limit=1
    )
    assert [(row["id"], row["title"]) for row in after_target["threads"]] == [
        ("athr_notification_target_b", "after-target-b")
    ]
    final = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=after_target["change_cursor"], limit=1
    )
    assert final["threads"] == []


async def test_notification_state_never_hydrates_fork_history_jobs(pool, store: Storage):
    """Copied turns neither write durable markers nor replay through legacy markers."""
    source = await _create_job(store, task_prompt="fork source")
    copy_thread_id = "athr_notification_fork_copy"
    copy_job_id = "ajob_notification_fork_copy"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO agent_threads (id,user_id,repo,title,execution_profile)
            VALUES ($1,$2,$3,$4,$5)
            """,
            copy_thread_id,
            "user-1",
            "owner/repo",
            "Copied history",
            "trusted-v1",
        )
        await conn.execute(
            """
            INSERT INTO agent_jobs (
                id,thread_id,user_id,turn_no,task_prompt,runtime,model,state,
                fork_source_job_id
            ) VALUES ($1,$2,$3,1,$4,$5,$6,$7,$8)
            """,
            copy_job_id,
            copy_thread_id,
            "user-1",
            "copied prompt",
            "claude-code",
            "glm-5.1",
            "cancelled",
            source["id"],
        )
        assert (
            await conn.fetchval(
                """
                SELECT count(*) FROM agent_notification_changes
                WHERE kind=$1 AND entity_id=$2
                """,
                "job",
                copy_job_id,
            )
            == 0
        )

    initial = await store.conversation_queries.notification_state(user_id="user-1", limit=200)
    assert copy_job_id not in {row["id"] for row in initial["jobs"]}

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_notification_changes (
                kind,entity_id,user_id,change_xid,changed_at
            ) VALUES ($1,$2,$3,pg_current_xact_id(),clock_timestamp())
            """,
            "job",
            copy_job_id,
            "user-1",
        )
    delta = await store.conversation_queries.notification_state(
        user_id="user-1", cursor=initial["change_cursor"], limit=200
    )
    assert copy_job_id not in {row["id"] for row in delta["jobs"]}
    assert delta["deleted_job_ids"] == [copy_job_id]
