"""Real-PostgreSQL snapshot/delta consistency and fleet-independent idle reads."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from tests.integration.storage.test_host_pool_store import db as db
from tests.support.db import assert_pool_idle

from cloud_agent.admin_observability import decode_cursor
from cloud_agent.admin_sandbox_inventory import decode_inventory_cursor, inventory_scope
from cloud_agent.admin_sandboxes import SandboxCursor, sandbox_cursor_scope
from cloud_agent.storage.admin_sandbox_inventory import AdminSandboxInventory, _prune
from cloud_agent.storage.admin_sandbox_queries import AdminSandboxQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


class CountedPool:
    def __init__(self, pool):
        self.pool, self.reads = pool, []

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            yield CountedConnection(conn, self.reads)


class CountedConnection:
    def __init__(self, conn, reads):
        self.conn, self.reads = conn, reads

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def fetch(self, sql, *args):
        self.reads.append((sql, args))
        return await self.conn.fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        self.reads.append((sql, args))
        return await self.conn.fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        self.reads.append((sql, args))
        return await self.conn.fetchval(sql, *args)


async def seed(pool, count):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_threads(id,user_id,repo,title,execution_profile)
            SELECT 'thread-'||lpad(n::text,6,'0'),'owner','org/repo','Public title','trusted-v1'
            FROM generate_series(1,$1::integer) n
        """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_sandboxes(id,user_id,origin_thread_id,host,container_name,
                runtime_id,execution_profile,state,last_error)
            SELECT 'sandbox-'||lpad(n::text,6,'0'),'owner','thread-'||lpad(n::text,6,'0'),
                'host-one','container-'||n,'codex','trusted-v1','running','PRIVATE error'
            FROM generate_series(1,$1::integer) n
        """,
            count,
        )
        await conn.execute("ANALYZE agent_admin_sandbox_changes")


async def read(pool, cursor=None, *, limit=200, state="running", include_archived=False):
    return await AdminSandboxInventory(pool).read(
        cursor=decode_inventory_cursor(cursor, inventory_scope(state, include_archived, limit)),
        state=state,
        include_archived=include_archived,
        limit=limit,
    )


async def complete(pool, *, limit=200):
    page = await read(pool, limit=limit)
    rows = list(page.sandboxes)
    while page.next_cursor:
        page = await read(pool, page.next_cursor, limit=limit)
        rows.extend(page.sandboxes)
    return rows, page.change_cursor


@pytest.mark.parametrize("count", [0, 1, 1000])
async def test_unchanged_reads_and_bytes_do_not_scale_with_fleet(db, count):
    await seed(db, count)
    rows, cursor = await complete(db)
    assert len(rows) == count
    assert "PRIVATE" not in json.dumps([row.model_dump(mode="json") for row in rows])
    baseline = CountedPool(db)
    legacy_rows, legacy_cursor, legacy_bytes = [], None, 0
    while True:
        legacy = await AdminSandboxQueries(baseline).list_sandboxes(
            cursor=decode_cursor(legacy_cursor, SandboxCursor, sandbox_cursor_scope("running")),
            limit=200,
            state="running",
        )
        legacy_rows.extend(legacy.sandboxes)
        legacy_bytes += len(legacy.model_dump_json())
        legacy_cursor = legacy.next_cursor
        if not legacy_cursor:
            break
    assert rows == legacy_rows
    assert len(baseline.reads) == 3 * max(1, (count + 199) // 200)
    counted = CountedPool(db)
    result = await read(counted, cursor)
    assert result.mode == "delta"
    assert result.sandboxes == result.deleted_ids == result.hosts == []
    assert result.next_cursor is None
    assert len(counted.reads) == 2
    assert len(result.model_dump_json()) < 1000
    assert not any("COUNT(*)" in sql or "FROM agent_sandboxes" in sql for sql, _ in counted.reads)
    async with db.acquire() as conn:
        sql, args = counted.reads[-1]
        plan = json.loads(
            await conn.fetchval("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, *args)
        )[0]
    blocks = plan["Plan"].get("Shared Hit Blocks", 0) + plan["Plan"].get("Shared Read Blocks", 0)
    assert blocks < 40
    await assert_pool_idle(db)
    print(
        f"N={count}: legacy_queries={len(baseline.reads)}, legacy_bytes={legacy_bytes}, "
        f"idle_queries={len(counted.reads)}, idle_bytes={len(result.model_dump_json())}, shared_blocks={blocks}"
    )


async def test_large_fleet_initial_pages_are_one_snapshot_with_bounded_source_reads(db):
    await seed(db, 1000)
    counted = CountedPool(db)
    page = await read(counted)
    rows, observed = list(page.sandboxes), page.observed_at
    source_reads = [(sql, args) for sql, args in counted.reads if "WITH page AS" in sql]
    assert len(source_reads) == 5
    assert all(args[1] == 201 for _, args in source_reads)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_threads SET title='New title' WHERE id='thread-000500'")
        await conn.execute("UPDATE agent_sandboxes SET archived_at=NOW() WHERE id='sandbox-000800'")
    while page.next_cursor:
        page = await read(db, page.next_cursor)
        assert page.observed_at == observed
        assert page.total_count == 1000
        rows.extend(page.sandboxes)
    assert len(rows) == 1000
    assert rows[499].title == "Public title"
    assert rows[799].archived_at is None
    changes = await read(db, page.change_cursor)
    assert [(row.id, row.title) for row in changes.sandboxes] == [("sandbox-000500", "New title")]
    assert changes.deleted_ids == ["sandbox-000800"]


async def test_snapshot_pages_stay_consistent_and_deltas_repair_changes_between_pages(db):
    await seed(db, 3)
    first = await read(db, limit=1)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET title='Changed title' WHERE id='thread-000002'"
        )
        # Production deletion retains the immutable trajectory and archives the row.
        await conn.execute("UPDATE agent_sandboxes SET archived_at=NOW() WHERE id='sandbox-000003'")
    second = await read(db, first.next_cursor, limit=1)
    third = await read(db, second.next_cursor, limit=1)
    assert second.sandboxes[0].title == "Public title"
    assert third.sandboxes[0].id == "sandbox-000003"
    assert first.observed_at == second.observed_at == third.observed_at
    assert first.total_count == second.total_count == third.total_count == 3
    updated = await read(db, third.change_cursor, limit=1)
    assert updated.sandboxes[0].title == "Changed title"
    removed = await read(db, updated.next_cursor, limit=1)
    assert removed.deleted_ids == ["sandbox-000003"]
    assert removed.change_cursor


async def test_delayed_commit_is_not_skipped_after_a_newer_transaction_commits(db):
    await seed(db, 2)
    _, initial = await complete(db)
    async with db.acquire() as slow:
        transaction = slow.transaction()
        await transaction.start()
        try:
            await slow.execute(
                "UPDATE agent_sandboxes SET memory_limit='9g' WHERE id='sandbox-000001'"
            )
            async with db.acquire() as fast:
                await fast.execute(
                    "UPDATE agent_sandboxes SET memory_limit='10g' WHERE id='sandbox-000002'"
                )
            first = await read(db, initial)
            assert [row.id for row in first.sandboxes] == ["sandbox-000002"]
            await transaction.commit()
        except BaseException:
            await transaction.rollback()
            raise
    later = await read(db, first.change_cursor)
    assert [(row.id, row.memory_limit) for row in later.sandboxes] == [("sandbox-000001", "9g")]
    assert not (await read(db, later.change_cursor)).sandboxes


async def test_membership_filter_transitions_and_host_deletion_are_delivered(db):
    await seed(db, 3)
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_runner_hosts(host,credential_hash) VALUES ('host-one','test')"
        )
    _, cursor = await complete(db)
    async with db.acquire() as conn:
        await conn.execute("""INSERT INTO agent_thread_sandbox_memberships
            (thread_id,user_id,sandbox_id,resident_slot_id) VALUES
            ('thread-000001','owner','sandbox-000001',0)""")
        await conn.execute("UPDATE agent_sandboxes SET state='stopped' WHERE id='sandbox-000002'")
        await conn.execute("UPDATE agent_sandboxes SET archived_at=NOW() WHERE id='sandbox-000003'")
        await conn.execute("DELETE FROM agent_runner_hosts WHERE host='host-one'")
    page = await read(db, cursor)
    assert [(row.id, row.member_count) for row in page.sandboxes] == [("sandbox-000001", 1)]
    assert page.deleted_ids == ["sandbox-000002", "sandbox-000003"]
    assert [(row.host, row.last_heartbeat_at) for row in page.hosts] == [("host-one", None)]


async def test_changes_behind_a_delta_page_are_caught_on_the_next_round(db):
    await seed(db, 2)
    _, cursor = await complete(db, limit=1)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET memory_limit='8g'")
    first = await read(db, cursor, limit=1)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_sandboxes SET memory_limit='12g' WHERE id='sandbox-000001'"
        )
    last = await read(db, first.next_cursor, limit=1)
    next_round = await read(db, last.change_cursor, limit=1)
    assert [(row.id, row.memory_limit) for row in next_round.sandboxes] == [
        ("sandbox-000001", "12g")
    ]


async def test_expired_snapshot_and_pruned_receipts_require_explicit_resync(db):
    await seed(db, 2)
    first = await read(db, limit=1)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_admin_sandbox_inventory_snapshots SET expires_at=NOW()-interval '1 second'"
        )
    assert (await read(db, first.next_cursor, limit=1)).reset_required
    _, cursor = await complete(db)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET memory_limit='20g'")
        await conn.execute(
            "UPDATE agent_admin_sandbox_changes SET changed_at=NOW()-interval '2 days'"
        )
        await _prune(conn)
    assert (await read(db, cursor)).reset_required
    rows, fresh = await complete(db)
    assert all(row.memory_limit == "20g" for row in rows)
    assert not (await read(db, fresh)).reset_required


async def test_rolled_back_writes_create_no_delta(db):
    await seed(db, 1)
    _, cursor = await complete(db)
    async with db.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        await conn.execute("UPDATE agent_sandboxes SET memory_limit='99g'")
        await transaction.rollback()
    assert not (await read(db, cursor)).sandboxes


async def test_older_transaction_updating_same_entity_after_newer_commit_is_not_lost(db):
    await seed(db, 1)
    async with db.acquire() as slow:
        transaction = slow.transaction()
        await transaction.start()
        try:
            await slow.fetchval("SELECT pg_current_xact_id()")
            async with db.acquire() as fast:
                await fast.execute("UPDATE agent_sandboxes SET memory_limit='10g'")
            _, cursor = await complete(db)
            await slow.execute("UPDATE agent_sandboxes SET memory_limit='20g'")
            await transaction.commit()
        except BaseException:
            await transaction.rollback()
            raise
    page = await read(db, cursor)
    assert [(row.id, row.memory_limit) for row in page.sandboxes] == [("sandbox-000001", "20g")]


async def test_repeated_heartbeats_coalesce_without_per_sandbox_fanout(db):
    await seed(db, 1000)
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_runner_hosts(host,credential_hash) VALUES ('host-one','test')"
        )
    _, cursor = await complete(db)
    async with db.acquire() as conn:
        for _ in range(100):
            await conn.execute(
                "UPDATE agent_runner_hosts SET last_heartbeat_at=clock_timestamp() WHERE host='host-one'"
            )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_admin_sandbox_changes WHERE kind='host'"
            )
            == 1
        )
    counted = CountedPool(db)
    page = await read(counted, cursor)
    assert len(page.hosts) == 1
    assert page.sandboxes == page.deleted_ids == []
    assert len(counted.reads) == 3
    assert len(page.model_dump_json()) < 1000


async def test_concurrent_resyncs_prune_without_serialization_failures(db):
    await seed(db, 1000)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_admin_sandbox_changes SET changed_at=NOW()-interval '2 days'"
        )
    snapshots = await asyncio.gather(*(complete(db) for _ in range(4)))
    assert all(len(rows) == 1000 for rows, _ in snapshots)
