"""Record every control-plane process and what it has adopted.

Each API and MCP listener process writes one row every few seconds: the build
it runs, the capabilities it has, the settings revision it last adopted, the
saved settings it uses only from a restart, and the root key it decrypts with
(as a one-way identifier). The Admin pages compare a saved revision with these
rows to say whether it is adopted, and refuse a mixed-version switch while a
participant cannot be shown to support it.

A row is evidence about a process that reports; an older release reports
nothing, and the absence of rows is never read as "no other process runs".

Revision ID: 0108_control_plane_instances
Revises: 0107_host_late_settings
"""

from __future__ import annotations

from alembic import op

revision = "0108_control_plane_instances"
down_revision = "0107_host_late_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the instance table."""
    op.execute(
        """
        CREATE TABLE agent_control_plane_instances (
            instance_id TEXT PRIMARY KEY CHECK (instance_id ~ '^[0-9a-f]{32}$'),
            role TEXT NOT NULL CHECK (role IN ('api', 'mcp')),
            machine TEXT NOT NULL CHECK (char_length(machine) BETWEEN 1 AND 255),
            build_sha TEXT NOT NULL DEFAULT '' CHECK (char_length(build_sha) <= 64),
            capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
            adopted_settings_revision BIGINT NOT NULL DEFAULT -1,
            restart_required JSONB NOT NULL DEFAULT '[]'::jsonb,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            stopped_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_control_plane_instances_seen "
        "ON agent_control_plane_instances (last_seen_at DESC)"
    )


def downgrade() -> None:
    """Drop the table; an older release neither writes nor reads it."""
    op.execute("DROP TABLE IF EXISTS agent_control_plane_instances")
