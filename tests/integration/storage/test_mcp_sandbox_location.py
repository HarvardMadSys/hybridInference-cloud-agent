"""MCP connections that run in the sandbox, against a real database.

What only Postgres settles: the location check constraint admits ``sandbox``
(``0111_mcp_sandbox_location``) and refuses a downgrade that would orphan one,
and the relay's fenced identity read never names a sandbox binding, which the
relay could not dial.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest
from tests.integration.storage.test_credential_queries_store import (
    _OWNER,
    _live,
    credentials as credentials,
    db as db,
    store as store,
)
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage
    from cloud_agent.storage.repositories.credential_queries import PostgresCredentialQueries

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_LAUNCH = {
    "command": "npx",
    "args": ["-y", "@playwright/mcp@0.0.83"],
    "env": {},
    "cwd": None,
    "mounts": [],
}


async def _connection(store: Storage, location: str, label: str) -> dict:
    remote = location == "remote"
    return await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label=label,
        transport="streamable-http" if remote else "stdio",
        location=location,
        url=f"https://{label}.example.com/mcp" if remote else None,
        launch_spec=None if remote else dict(_LAUNCH),
        auth_kind="none",
        auth_config={},
    )


async def test_the_location_constraint_admits_the_sandbox_only(store: Storage, db) -> None:
    created = await _connection(store, "sandbox", "browser")
    assert created["location"] == "sandbox"

    async with db.acquire() as conn:
        names = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'agent_mcp_connections'::regclass "
            "AND contype = 'c' AND position('location' in pg_get_constraintdef(oid)) > 0"
        )
        assert [row["conname"] for row in names] == ["agent_mcp_connections_location_check"]
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_mcp_connections SET location = 'elsewhere' WHERE id = $1",
                created["id"],
            )


async def test_the_relay_identity_names_no_sandbox_binding(
    store: Storage, credentials: PostgresCredentialQueries
) -> None:
    job, claim = await _live(store)
    await _connection(store, "remote", "docs")
    await _connection(store, "sandbox", "browser")
    bound = await store.mcp.list_mcp_bindings(thread_id=job["thread_id"])
    assert sorted(row["location"] for row in bound if row["enabled"]) == ["remote", "sandbox"]

    resolved = await credentials.resolve_model_credential(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
    )

    assert resolved is not None
    assert [row["location"] for row in resolved["mcp_bindings"]] == ["remote"]


async def test_a_downgrade_refuses_to_orphan_a_sandbox_connection(store: Storage, db) -> None:
    config = _db_config()
    created = await _connection(store, "sandbox", "browser")
    try:
        with pytest.raises(Exception, match="sandbox MCP connections exist"):
            downgrade_to(config, "0110_host_late_settings")
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM agent_mcp_connections WHERE id = $1", created["id"])
        downgrade_to(config, "0110_host_late_settings")
        with pytest.raises(asyncpg.CheckViolationError):
            await _connection(store, "sandbox", "after-downgrade")
    finally:
        apply_migrations(config)
    assert (await _connection(store, "sandbox", "again"))["location"] == "sandbox"
