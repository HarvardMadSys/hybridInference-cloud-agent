"""Bulk inventory parity, snapshot isolation, and round trips on real PostgreSQL."""

from contextlib import asynccontextmanager
from time import perf_counter

import pytest
from tests.integration.storage.test_host_pool_store import db as db
from tests.support.db import assert_pool_idle

from cloud_agent.storage._sql.compute_admission import host_compute_admission
from cloud_agent.storage.repositories.compute_resources import PostgresComputeResourceRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


class CountedPool:
    def __init__(self, pool, after_first_fetch=None):
        self.pool = pool
        self.calls = 0
        self.acquisitions = 0
        self.held_seconds = 0.0
        self.after_first_fetch = after_first_fetch

    @asynccontextmanager
    async def acquire(self):
        self.acquisitions += 1
        async with self.pool.acquire() as conn:
            start = perf_counter()
            yield CountedConnection(conn, self)
            self.held_seconds += perf_counter() - start


class CountedConnection:
    def __init__(self, conn, owner):
        self.conn, self.owner = conn, owner

    def transaction(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        return self.conn.transaction(**kwargs)

    async def fetch(self, query, *args):
        self.owner.calls += 1
        result = await self.conn.fetch(query, *args)
        if self.owner.calls == 1 and self.owner.after_first_fetch:
            await self.owner.after_first_fetch()
        return result

    async def fetchrow(self, query, *args):
        self.owner.calls += 1
        return await self.conn.fetchrow(query, *args)

    async def fetchval(self, query, *args):
        self.owner.calls += 1
        return await self.conn.fetchval(query, *args)


async def seed_inventory(db, count):
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_compute_allocations
                (id,name,provider_id,mode,observed_state,scope_complete,observed_at,created_at)
            SELECT 'allocation-'||n,'machine-'||n,'cloudlab','attach','ready',true,NOW(),
                   NOW()+n*interval '1 millisecond'
            FROM generate_series(1,$1::integer) n
        """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_compute_nodes (id,allocation_id,provider_node_id,instance_identity,name)
            SELECT 'node-'||n,'allocation-'||n,'provider-'||n,'instance-'||n,'node-'||n
            FROM generate_series(1,$1::integer) n
        """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_runner_hosts(host,credential_hash,compute_node_id)
            SELECT 'host-'||n,'test-credential','node-'||n FROM generate_series(1,$1::integer) n
        """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_host_admission_holds(host,allocation_id,purpose,source_id,scope)
            SELECT 'host-'||n,'allocation-'||n,'retirement','source-'||n,'new_groups'
            FROM generate_series(1,$1::integer) n WHERE n%2=0
        """,
            count,
        )
        await conn.execute(
            """
            INSERT INTO agent_cloudlab_autoscaling_members(allocation_id,deployment_profile,state)
            SELECT 'allocation-'||n,'profile-'||n,'active'
            FROM generate_series(1,$1::integer) n WHERE n%3=0
        """,
            count,
        )


@pytest.mark.parametrize("count", [0, 1, 100])
async def test_inventory_query_count_is_constant_and_payload_matches_single_reads(db, count):
    await seed_inventory(db, count)
    legacy_pool = CountedPool(db)
    legacy_repo = PostgresComputeResourceRepository(legacy_pool)
    async with (
        legacy_pool.acquire() as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        ids = await conn.fetch("SELECT id FROM agent_compute_allocations ORDER BY created_at")
        expected = [await legacy_repo._allocation(conn, row["id"]) for row in ids]
    assert legacy_pool.calls == 1 + 6 * count
    for method, queries in (
        ("list_allocations", 5),
        ("server_inventory", 7),
        ("scheduling_inventory", 6),
    ):
        pool = CountedPool(db)
        result = await getattr(PostgresComputeResourceRepository(pool), method)()
        allocations = (
            result
            if method == "list_allocations"
            else (result["allocations"] if method == "server_inventory" else result.allocations)
        )
        assert allocations == expected
        assert pool.calls == queries
        assert pool.acquisitions == 1
        await assert_pool_idle(db)
        print(
            f"{method} N={count}: queries={pool.calls}, pool_hold_ms={pool.held_seconds * 1000:.3f}; "
            f"legacy_queries={legacy_pool.calls}, legacy_pool_hold_ms={legacy_pool.held_seconds * 1000:.3f}"
        )


async def test_bulk_operation_windows_match_per_allocation_queries(db):
    await seed_inventory(db, 2)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO agent_compute_operations(id,request_id,fingerprint,allocation_id,kind,state,created_at)
            SELECT 'op-'||a||'-'||n,'request-'||a||'-'||n,'fingerprint','allocation-'||a,
                   CASE WHEN n<=220 THEN 'inspect' WHEN n<=440 THEN 'extend' ELSE 'rotate' END,
                   'succeeded',NOW()-n*interval '1 minute'
            FROM generate_series(1,2) a CROSS JOIN generate_series(1,450) n
        """)
        await conn.execute("""
            INSERT INTO agent_compute_operations(id,request_id,fingerprint,allocation_id,kind,state,created_at)
            VALUES ('old-acquire','old-acquire','f','allocation-1','acquire','blocked',NOW()-interval '2 days'),
                   ('old-renew','old-renew','f','allocation-1','extend','unknown',NOW()-interval '3 days'),
                   ('old-inspect','old-inspect','f','allocation-2','inspect','waiting',NOW()-interval '4 days')
        """)
    repository = PostgresComputeResourceRepository(db)
    snapshot = await repository.scheduling_inventory()
    for allocation in snapshot.allocations:
        allocation_id = allocation["id"]
        assert snapshot.operations[allocation_id] == await repository.list_operations(
            allocation_id=allocation_id
        )
        assert snapshot.lifecycle_operations[allocation_id] == await repository.list_operations(
            allocation_id=allocation_id, kinds=["extend", "rotate"]
        )
    assert "old-acquire" in {row["id"] for row in snapshot.operations["allocation-1"]}
    assert "old-renew" in {row["id"] for row in snapshot.lifecycle_operations["allocation-1"]}


async def test_bulk_children_and_operations_use_the_allocation_snapshot(db):
    await seed_inventory(db, 1)

    async def change_after_allocation_read():
        async with db.acquire() as conn:
            await conn.execute("UPDATE agent_compute_nodes SET name='changed'")
            await conn.execute("""
                INSERT INTO agent_compute_operations(id,request_id,fingerprint,allocation_id,kind)
                VALUES ('later','later','f','allocation-1','extend')
            """)

    pool = CountedPool(db, after_first_fetch=change_after_allocation_read)
    snapshot = await PostgresComputeResourceRepository(pool).scheduling_inventory()
    assert snapshot.allocations[0]["nodes"][0]["name"] == "node-1"
    assert snapshot.operations == {}
    later = await PostgresComputeResourceRepository(db).scheduling_inventory()
    assert later.allocations[0]["nodes"][0]["name"] == "changed"
    assert later.operations["allocation-1"][0]["id"] == "later"


async def test_bulk_host_admission_matches_single_host_deadlines_and_holds(db):
    await seed_inventory(db, 8)
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_runner_hosts(host,credential_hash) VALUES ('legacy','test')"
        )
        await conn.execute(
            "UPDATE agent_compute_allocations SET observed_state='failed' WHERE id='allocation-3'"
        )
        await conn.execute(
            "UPDATE agent_compute_allocations SET observed_at=NOW()-interval '1 hour' WHERE id='allocation-4'"
        )
        await conn.execute(
            "UPDATE agent_compute_allocations SET expires_at=NOW()-interval '1 hour' WHERE id='allocation-5'"
        )
        await conn.execute(
            "UPDATE agent_compute_allocations SET release_committed_at=NOW() WHERE id='allocation-6'"
        )
        await conn.execute(
            "UPDATE agent_compute_allocations SET lease_managed=true,expiration_known=false WHERE id='allocation-7'"
        )
        await conn.execute(
            "UPDATE agent_host_admission_holds SET scope='all_claims' WHERE host='host-8'"
        )
    inventory = await PostgresComputeResourceRepository(db).server_inventory()
    async with db.acquire() as conn:
        for host in inventory["hosts"]:
            assert (
                host["allows_new_groups"],
                host["allows_claims"],
            ) == await host_compute_admission(conn, host["host"])
