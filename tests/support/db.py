"""Resetting the agent schema between database-backed tests.

One ``TRUNCATE ... CASCADE`` rather than a hand-ordered list of ``DELETE``s:
the schema now has circular references (a job names its current attempt, an
attempt names its job; a snapshot names its anchor through the membership
table that names the snapshot), so no linear order deletes cleanly, and a
list that has to be kept in step with every new foreign key is a list that
will be wrong. ``CASCADE`` empties whatever references the named tables.
"""

from __future__ import annotations

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


async def reset_agent_tables(conn: Any) -> None:
    """Empty every ``agent_*`` table and re-seed the policy singleton."""
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'agent\\_%'"
    )
    names = ", ".join(sorted(row["tablename"] for row in rows))
    if names:
        await conn.execute(f"TRUNCATE TABLE {names} CASCADE")
    # The baseline seeds this row; every claim reads it, and a test that left
    # a host pinned would otherwise gate every later claim.
    await conn.execute(
        "INSERT INTO agent_runner_policy (id, active_host, runtime_versions) "
        "VALUES (TRUE, NULL, NULL) ON CONFLICT (id) DO UPDATE "
        "SET active_host = NULL, runtime_versions = NULL"
    )
