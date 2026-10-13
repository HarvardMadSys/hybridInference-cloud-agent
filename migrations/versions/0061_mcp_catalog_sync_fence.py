"""Fence MCP catalog scans with authoritative settings and DB generations.

Revision ID: 0061_mcp_catalog_sync_fence
Revises: 0060_admin_sandbox_discard
"""

from alembic import op

revision = "0061_mcp_catalog_sync_fence"
down_revision = "0060_admin_sandbox_discard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create a fenced cache alongside the rolling-compatible legacy cache.

    A process from the previous release can continue writing the old table
    during a rolling deployment, but cannot overwrite a snapshot published by
    a process that understands Registry source revisions. The copied legacy
    snapshot remains available while deliberately lacking a trusted source, so
    the first new process serves it but refreshes it immediately.
    """
    op.execute("CREATE SEQUENCE agent_mcp_catalog_generation_seq AS BIGINT")
    op.execute(
        """
        CREATE TABLE agent_mcp_catalog_snapshots (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            entries JSONB NOT NULL CHECK (jsonb_typeof(entries) = 'array'),
            synced_at TIMESTAMPTZ NOT NULL,
            registry_url TEXT CHECK (registry_url IS NULL OR registry_url <> ''),
            settings_revision BIGINT CHECK (
                settings_revision IS NULL OR settings_revision >= 1
            ),
            generation BIGINT NOT NULL CHECK (generation >= 0),
            CHECK ((registry_url IS NULL) = (settings_revision IS NULL))
        )
        """
    )
    op.execute(
        """
        INSERT INTO agent_mcp_catalog_snapshots
            (singleton, entries, synced_at, registry_url, settings_revision, generation)
        SELECT singleton, entries, synced_at, NULL, NULL, 0
        FROM agent_mcp_catalog_cache
        """
    )


def downgrade() -> None:
    """Return the latest complete snapshot to the legacy cache."""
    op.execute(
        """
        INSERT INTO agent_mcp_catalog_cache (singleton, entries, synced_at)
        SELECT singleton, entries, synced_at
        FROM agent_mcp_catalog_snapshots
        ON CONFLICT (singleton) DO UPDATE
        SET entries = EXCLUDED.entries, synced_at = EXCLUDED.synced_at
        """
    )
    op.execute("DROP TABLE agent_mcp_catalog_snapshots")
    op.execute("DROP SEQUENCE agent_mcp_catalog_generation_seq")
