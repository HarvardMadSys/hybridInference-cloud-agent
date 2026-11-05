"""Resetting the agent schema between database-backed tests.

One ``TRUNCATE ... CASCADE`` rather than a hand-ordered list of ``DELETE``s:
the schema now has circular references (a job names its current attempt, an
attempt names its job; a snapshot names its anchor through the membership
table that names the snapshot), so no linear order deletes cleanly, and a
list that has to be kept in step with every new foreign key is a list that
will be wrong. ``CASCADE`` empties whatever references the named tables.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def backdate_measurement_arrival(pool: Any, *, moment: Any) -> None:
    """Set every stored measurement's arrival to one instant.

    The page key is ``(received_at, measurement_id)``, so a test of the
    tiebreaker has to make ``received_at`` collide. It lives here rather than
    in the test because touching a pool past its owner is what the storage
    boundary forbids, and this module is the exception the boundary names.
    """
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_operation_measurements SET received_at = $1", moment)


async def assert_pool_idle(pool: Any, message: str = "", *, timeout: float = 3) -> None:
    """Assert that every connection returns to the pool, allowing for asyncpg's shield.

    asyncpg returns a connection under ``asyncio.shield``, so a task cancelled
    while it releases one finishes before the release does, and the connection
    reaches the idle queue a moment later. Checking the pool the instant the
    task is awaited races that release and fails as ``1 == 2``. A connection
    that never comes back still fails, once ``timeout`` seconds have passed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while pool.get_idle_size() != pool.get_size():
        if loop.time() >= deadline:
            missing = pool.get_size() - pool.get_idle_size()
            raise AssertionError(message or f"{missing} pool connection(s) never returned")
        await asyncio.sleep(0.005)


async def reset_agent_tables(conn: Any) -> None:
    """Empty every ``agent_*`` table and re-seed the policy singleton."""
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'agent\\_%'"
    )
    names = ", ".join(sorted(row["tablename"] for row in rows))
    if names:
        protects_sandbox_events = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_trigger AS trigger
                JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'agent_sandbox_events'
                  AND trigger.tgname = 'trg_agent_sandbox_events_no_truncate'
                  AND NOT trigger.tgisinternal
            )
            """
        )
        if protects_sandbox_events:
            # Production has no bypass for the append-only trigger. Test reset
            # owns the migrated schema, so disable only this named guard inside
            # one transaction; a failed TRUNCATE rolls the ALTER TABLE back.
            async with conn.transaction():
                await conn.execute(
                    "ALTER TABLE agent_sandbox_events "
                    "DISABLE TRIGGER trg_agent_sandbox_events_no_truncate"
                )
                await conn.execute(f"TRUNCATE TABLE {names} CASCADE")
                await conn.execute(
                    "ALTER TABLE agent_sandbox_events "
                    "ENABLE TRIGGER trg_agent_sandbox_events_no_truncate"
                )
        else:
            await conn.execute(f"TRUNCATE TABLE {names} CASCADE")
    # The baseline seeds this row; every claim reads it, and a test that left
    # a host pinned would otherwise gate every later claim.
    await conn.execute(
        "INSERT INTO agent_runner_policy (id, active_host, runtime_versions) "
        "VALUES (TRUE, NULL, NULL) ON CONFLICT (id) DO UPDATE "
        "SET active_host = NULL, runtime_versions = NULL, "
        "sandbox_archive_settings = "
        '\'{"enabled":false,"region":"","kms_key_arn":"",'
        '"expected_bucket_owner":"","max_archive_bytes":2147483648,'
        '"multipart_threshold_bytes":8388608,"multipart_part_bytes":8388608}\'::jsonb, '
        "sandbox_archive_revision = 0, sandbox_archive_updated_at = NOW()"
    )
    # Autoscaling uses the same singleton/CAS shape. Keep older migration
    # targets usable by checking for the additive tables before reseeding.
    if await conn.fetchval("SELECT to_regclass('agent_cloudlab_autoscaling_policy')") is not None:
        await conn.execute("INSERT INTO agent_cloudlab_autoscaling_policy (id) VALUES (TRUE)")
        await conn.execute("INSERT INTO agent_cloudlab_autoscaling_runtime (id) VALUES (TRUE)")
    # The profiling control row is seeded by its migration for the same reason:
    # every Start, Stop and policy read takes it, so a truncate that left it out
    # would make a correct deployment look like one that was never configured.
    # Re-inserted here rather than created on demand, because "there is no row"
    # is a real answer that must stay available to the code that reports it.
    if await conn.fetchval("SELECT to_regclass('agent_operation_profiling_control')") is not None:
        await conn.execute(
            "INSERT INTO agent_operation_profiling_control "
            "(deployment, revision, desired_state, current_run_id, updated_at, "
            " updated_by, last_stop_revision, last_stopped_at, last_stopped_run_id) "
            "VALUES ('default', 0, 'off', NULL, NOW(), NULL, NULL, NULL, NULL), "
            "       ('', 0, 'off', NULL, NOW(), NULL, NULL, NULL, NULL) "
            "ON CONFLICT (deployment) DO UPDATE SET "
            "revision = 0, desired_state = 'off', current_run_id = NULL, "
            "updated_at = NOW(), updated_by = NULL, last_stop_revision = NULL, "
            "last_stopped_at = NULL, last_stopped_run_id = NULL"
        )
