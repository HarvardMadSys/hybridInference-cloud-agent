"""``0113_mcp_retire_dead_locations`` against a real database.

What only Postgres settles: every row stored at a location nothing runs
(``managed-worker``, ``user-connector``) becomes a ``sandbox`` program with its
launch as stored and its decision versions moved, nothing is deleted, and the
narrowed check refuses the two values afterwards. A downgrade admits them again
and leaves the moved rows where they are.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.mcp_connectors import insert_connector
from tests.support.migrations import REPO, apply_migrations, downgrade_to

pytestmark = pytest.mark.dbtest

_BEFORE = "0112_thread_pull_requests"
_THIS = "0113_mcp_retire_dead_locations"
_OWNER = "user-owner"

#: A launch a cloud worker would have been given: its mount and working
#: directory are what the sandbox refuses, and what the move must keep.
_WORKER_LAUNCH = {
    "command": "npx",
    "args": ["-y", "@scope/files@1.2.3", "/srv/data"],
    "env": {},
    "cwd": "/srv",
    "mounts": [{"source": "/srv/data", "target": "/workspace/mcp/0", "read_only": True}],
}
_CONNECTOR_LAUNCH = {
    "command": "npx",
    "args": ["-y", "@scope/notes@latest"],
    "env": {"TOKEN": "secret://TOKEN"},
    "env_secrets": {"TOKEN": "sealed-ciphertext"},
}
_SANDBOX_LAUNCH = {"command": "uvx", "args": ["mcp-server-fetch==2025.4.7"], "env": {}}

_COLUMNS = (
    "location, launch_spec, status, error_kind, error_detail, connector_id, "
    "config_revision, authorization_epoch, deleted_at"
)


async def _insert(conn: asyncpg.Connection, **values: Any) -> int:
    """Insert one connection as the previous release stored it."""
    remote = values["location"] == "remote"
    return int(
        await conn.fetchval(
            """
            INSERT INTO agent_mcp_connections
                (user_id, label, transport, location, url, launch_spec, auth_kind,
                 auth_config, status, error_kind, error_detail, connector_id,
                 config_revision, authorization_epoch, deleted_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'none', '{}'::jsonb, $7, $8, $9, $10,
                    $11, $12, CASE WHEN $13::boolean THEN clock_timestamp() END)
            RETURNING id
            """,
            _OWNER,
            values["label"],
            "streamable-http" if remote else "stdio",
            values["location"],
            "https://docs.example.com/mcp" if remote else None,
            None if remote else json.dumps(values["launch"]),
            values.get("status", "draft"),
            values.get("error_kind", "none"),
            values.get("error_detail"),
            values.get("connector_id"),
            values.get("config_revision", 1),
            values.get("authorization_epoch", 1),
            values.get("deleted", False),
        )
    )


async def _rows(pool: asyncpg.Pool, ids: list[int]) -> dict[int, dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, {_COLUMNS} FROM agent_mcp_connections WHERE id = ANY($1::bigint[])",
            ids,
        )
    return {int(row["id"]): {**dict(row), "launch_spec": _json(row["launch_spec"])} for row in rows}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def test_the_revision_follows_the_thread_pull_requests() -> None:
    """It extends the one migration line."""
    script = ScriptDirectory.from_config(Config(toml_file=str(REPO / "pyproject.toml")))
    assert script.get_revision(_THIS).down_revision == _BEFORE


@pytest.mark.asyncio
async def test_dead_locations_become_programs_and_nothing_is_deleted(pool) -> None:
    config = _db_config()
    downgrade_to(config, _BEFORE)
    try:
        connector = await insert_connector(pool, user_id=_OWNER, name="Laptop")
        async with pool.acquire() as conn:
            worker = await _insert(
                conn,
                label="Files",
                location="managed-worker",
                launch=_WORKER_LAUNCH,
                status="error",
                error_kind="launch_failed",
                error_detail="needs a managed worker that this deployment has not been given",
                config_revision=3,
                authorization_epoch=5,
            )
            routed = await _insert(
                conn,
                label="Notes",
                location="user-connector",
                launch=_CONNECTOR_LAUNCH,
                connector_id=int(connector["id"]),
            )
            await conn.execute(
                "INSERT INTO agent_mcp_connector_connections (connector_id, connection_id) "
                "VALUES ($1, $2)",
                connector["id"],
                routed,
            )
            tombstone = await _insert(
                conn,
                label="Gone",
                location="user-connector",
                launch=_SANDBOX_LAUNCH,
                status="disabled",
                deleted=True,
            )
            remote = await _insert(conn, label="Docs", location="remote", status="active")
            program = await _insert(conn, label="Fetch", location="sandbox", launch=_SANDBOX_LAUNCH)
        ids = [worker, routed, tombstone, remote, program]
        before = await _rows(pool, ids)

        apply_migrations(config, _THIS)
        after = await _rows(pool, ids)

        assert {row_id: after[row_id]["location"] for row_id in ids} == {
            worker: "sandbox",
            routed: "sandbox",
            tombstone: "sandbox",
            remote: "remote",
            program: "sandbox",
        }
        for moved in (worker, routed, tombstone):
            # Kept exactly: the mounts, cwd, unpinned version and sealed keys
            # are the owner's to fix, and the sandbox refuses what it cannot run.
            assert after[moved]["launch_spec"] == before[moved]["launch_spec"]
            assert after[moved]["config_revision"] == before[moved]["config_revision"] + 1
            assert after[moved]["authorization_epoch"] == before[moved]["authorization_epoch"] + 1
            assert (after[moved]["error_kind"], after[moved]["error_detail"]) == ("none", None)
        assert after[worker]["status"] == "draft"
        assert after[routed]["status"] == "draft"
        assert after[tombstone]["status"] == "disabled"
        assert after[tombstone]["deleted_at"] == before[tombstone]["deleted_at"]
        for untouched in (remote, program):
            assert after[untouched] == before[untouched]

        async with pool.acquire() as conn:
            # The connector and its link stay, unread, for a later revision.
            assert after[routed]["connector_id"] == connector["id"]
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM agent_mcp_connector_connections WHERE connection_id = $1",
                    routed,
                )
                == 1
            )
            for location in ("managed-worker", "user-connector"):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        "UPDATE agent_mcp_connections SET location = $1 WHERE id = $2",
                        location,
                        program,
                    )

        downgrade_to(config, _BEFORE)
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT location FROM agent_mcp_connections WHERE id = $1", worker
                )
                == "sandbox"
            )
            await conn.execute(
                "UPDATE agent_mcp_connections SET location = 'user-connector' WHERE id = $1",
                program,
            )
            names = await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = "
                "'agent_mcp_connections'::regclass AND contype = 'c' "
                "AND position('location' in pg_get_constraintdef(oid)) > 0"
            )
        assert [row["conname"] for row in names] == ["agent_mcp_connections_location_check"]
    finally:
        apply_migrations(config)
