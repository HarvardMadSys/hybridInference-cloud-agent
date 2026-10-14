"""The MCP connector route and reverse authorization stay singular and aligned."""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.integration.storage.test_agent_job_store import pool as pool
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import REPO, apply_migrations, downgrade_to

from cloud_agent.storage.assembly import Storage

pytestmark = pytest.mark.dbtest

_BEFORE = "0057_admin_sandbox_trajectory"
_THIS = "0058_mcp_connector_invariant"
_NEXT = "0059_counter_constraints"
_HEAD = "0072_drop_old_memory_tables"
_OWNER = "user-owner"
_OTHER = "user-other"
_PRESERVED_AUTHORIZED_AT = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)


def test_mcp_connector_invariant_revision_follows_trajectory_merge() -> None:
    """The repair extends the one post-merge migration line."""
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_revision(_THIS).down_revision == _BEFORE
    assert script.get_revision(_NEXT).down_revision == _THIS
    assert script.get_revision("0060_admin_sandbox_discard").down_revision == _NEXT
    assert script.get_current_head() == _HEAD
    assert script.get_revision("0061_mcp_catalog_sync_fence").down_revision == (
        "0060_admin_sandbox_discard"
    )
    assert script.get_revision("0062_native_session_locator").down_revision == (
        "0061_mcp_catalog_sync_fence"
    )
    assert script.get_revision("0063_user_profiles").down_revision == "0062_native_session_locator"
    assert (
        script.get_revision("0064_mcp_remembered_approvals").down_revision == "0063_user_profiles"
    )
    assert script.get_revision("0065_environments").down_revision == "0064_mcp_remembered_approvals"
    assert script.get_revision("0066_admin_job_listing_index").down_revision == "0065_environments"
    assert (
        script.get_revision("0067_failed_snapshot_wake").down_revision
        == "0066_admin_job_listing_index"
    )
    assert (
        script.get_revision("0068_sandbox_checkpoints").down_revision == "0067_failed_snapshot_wake"
    )
    assert script.get_revision("0069_agent_memories").down_revision == "0068_sandbox_checkpoints"
    assert script.get_revision("0070_memory_cutover").down_revision == "0069_agent_memories"
    assert script.get_revision("0071_temporary_workspaces").down_revision == "0070_memory_cutover"
    assert script.get_revision(_HEAD).down_revision == "0071_temporary_workspaces"


async def _local_connection(
    store: Storage,
    *,
    label: str,
    connector_id: int | None = None,
) -> dict:
    """Create one local connection through the normal repository."""
    return await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label=label,
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "mcp-server", "args": [], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
        connector_id=connector_id,
    )


async def _remote_connection(store: Storage, *, label: str) -> dict:
    """Create one remote connection before corrupting its connector route."""
    return await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label=label,
        transport="streamable-http",
        location="remote",
        url=f"https://{label.lower().replace(' ', '-')}.example.com/mcp",
        launch_spec=None,
        auth_kind="none",
        auth_config={},
    )


@pytest.mark.asyncio
async def test_migration_repairs_routes_and_preserves_only_the_canonical_pair(pool) -> None:
    """0058 backfills valid authority and removes every stale alternative."""
    config = _db_config()
    downgrade_to(config, _BEFORE)
    store = Storage(pool)

    canonical = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Canonical",
        registration_hash="migration-canonical",
    )
    extra = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Extra",
        registration_hash="migration-extra",
    )
    wrong_owner = await store.mcp.create_mcp_connector(
        user_id=_OTHER,
        name="Other owner's",
        registration_hash="migration-other-owner",
    )
    revoked = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Revoked",
        registration_hash="migration-revoked",
    )
    assert await store.mcp.revoke_mcp_connector(connector_id=int(revoked["id"]), user_id=_OWNER)

    valid = await _local_connection(
        store, label="Valid canonical", connector_id=int(canonical["id"])
    )
    missing = await _local_connection(store, label="Valid missing mapping")
    remote = await _remote_connection(store, label="Invalid remote")
    foreign = await _local_connection(store, label="Invalid owner")
    revoked_route = await _local_connection(store, label="Invalid revoked")
    deleted = await _local_connection(store, label="Invalid deleted")
    no_route = await _local_connection(store, label="No route")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_mcp_connector_connections
            SET authorized_at = $3
            WHERE connector_id = $1 AND connection_id = $2
            """,
            canonical["id"],
            valid["id"],
            _PRESERVED_AUTHORIZED_AT,
        )
        # The historical composite primary key permits two connectors to
        # authorize one singly-routed connection.
        await conn.execute(
            """
            INSERT INTO agent_mcp_connector_connections (connector_id, connection_id)
            VALUES ($1, $2)
            """,
            extra["id"],
            valid["id"],
        )
        await conn.execute(
            """
            UPDATE agent_mcp_connections
            SET connector_id = CASE id
                WHEN $1 THEN $7
                WHEN $2 THEN $7
                WHEN $3 THEN $8
                WHEN $4 THEN $9
                WHEN $5 THEN $7
                ELSE connector_id
            END,
            deleted_at = CASE WHEN id = $5 THEN clock_timestamp() ELSE deleted_at END
            WHERE id = ANY($6::bigint[])
            """,
            missing["id"],
            remote["id"],
            foreign["id"],
            revoked_route["id"],
            deleted["id"],
            [
                missing["id"],
                remote["id"],
                foreign["id"],
                revoked_route["id"],
                deleted["id"],
            ],
            canonical["id"],
            wrong_owner["id"],
            revoked["id"],
        )
        await conn.executemany(
            """
            INSERT INTO agent_mcp_connector_connections (connector_id, connection_id)
            VALUES ($1, $2)
            """,
            [
                (canonical["id"], remote["id"]),
                (wrong_owner["id"], foreign["id"]),
                (revoked["id"], revoked_route["id"]),
                (canonical["id"], deleted["id"]),
                (extra["id"], no_route["id"]),
            ],
        )
        before_invalid = {
            int(row["id"]): (int(row["config_revision"]), int(row["authorization_epoch"]))
            for row in await conn.fetch(
                """
                SELECT id, config_revision, authorization_epoch
                FROM agent_mcp_connections
                WHERE id = ANY($1::bigint[])
                """,
                [remote["id"], foreign["id"], revoked_route["id"], deleted["id"]],
            )
        }

    try:
        apply_migrations(config, _THIS)

        async with pool.acquire() as conn:
            rows = {
                int(row["id"]): row
                for row in await conn.fetch(
                    """
                    SELECT id, connector_id, config_revision, authorization_epoch
                    FROM agent_mcp_connections
                    WHERE id = ANY($1::bigint[])
                    """,
                    [
                        valid["id"],
                        missing["id"],
                        remote["id"],
                        foreign["id"],
                        revoked_route["id"],
                        deleted["id"],
                        no_route["id"],
                    ],
                )
            }
            mappings = await conn.fetch(
                """
                SELECT connector_id, connection_id, authorized_at
                FROM agent_mcp_connector_connections
                ORDER BY connection_id, connector_id
                """
            )
            constraint = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_constraint
                WHERE conname = 'uq_agent_mcp_connector_connections_connection'
                """
            )

            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO agent_mcp_connector_connections (connector_id, connection_id)
                    VALUES ($1, $2)
                    """,
                    extra["id"],
                    valid["id"],
                )

        assert rows[int(valid["id"])]["connector_id"] == canonical["id"]
        assert rows[int(missing["id"])]["connector_id"] == canonical["id"]
        assert rows[int(no_route["id"])]["connector_id"] is None
        for invalid in (remote, foreign, revoked_route, deleted):
            row = rows[int(invalid["id"])]
            assert row["connector_id"] is None
            old_revision, old_epoch = before_invalid[int(invalid["id"])]
            assert row["config_revision"] == old_revision + 1
            assert row["authorization_epoch"] == old_epoch + 1

        assert [
            (row["connector_id"], row["connection_id"], row["authorized_at"]) for row in mappings
        ] == [
            (canonical["id"], valid["id"], _PRESERVED_AUTHORIZED_AT),
            (canonical["id"], missing["id"], mappings[1]["authorized_at"]),
        ]
        assert constraint == 1

        downgrade_to(config, _BEFORE)
        async with pool.acquire() as conn:
            constraint_after_downgrade = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_constraint
                WHERE conname = 'uq_agent_mcp_connector_connections_connection'
                """
            )
            still_repaired = await conn.fetchval(
                "SELECT connector_id FROM agent_mcp_connections WHERE id = $1",
                remote["id"],
            )
        assert constraint_after_downgrade == 0
        assert still_repaired is None
    finally:
        apply_migrations(config)
