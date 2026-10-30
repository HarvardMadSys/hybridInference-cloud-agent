"""Import host settings added after a host's first import, and keep config reports.

``settings_imported`` records that a host's legacy environment was imported
once. A host setting registered later (the runner lease) would never be
imported for such a host, and its environment value would silently give way
to the built-in default. ``settings_late_imported`` lists the later keys whose
observation this host has already been offered, so each is imported at most
once and an administrator's reset is never undone.

``config_reports`` holds what a host says it actually uses for non-numeric
configuration delivered on the heartbeat, such as the npm registry it
downloads agent versions from.

Revision ID: 0107_host_late_settings
Revises: 0106_user_model_connections
"""

from __future__ import annotations

from alembic import op

revision = "0107_host_late_settings"
down_revision = "0106_user_model_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add both columns; existing hosts have offered no late key yet."""
    op.execute(
        "ALTER TABLE agent_runner_hosts "
        "ADD COLUMN settings_late_imported TEXT[] NOT NULL DEFAULT '{}'::text[], "
        "ADD COLUMN config_reports JSONB"
    )


def downgrade() -> None:
    """Drop both columns; an older release neither imports late keys nor reads reports."""
    op.execute(
        "ALTER TABLE agent_runner_hosts "
        "DROP COLUMN IF EXISTS config_reports, "
        "DROP COLUMN IF EXISTS settings_late_imported"
    )
