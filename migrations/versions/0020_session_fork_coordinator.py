"""Lease native-session fork materialization across control-plane replicas.

The first shared-live migration was exercised locally before the coordinator
fence was added.  Keep the fence in its own forward revision so databases that
already recorded 0019 receive the two columns without dropping any sandbox or
chat state, while fresh databases still build the same final schema.

Revision ID: 0020_session_fork_coordinator
Revises: 0019_shared_live_sandboxes
"""

from __future__ import annotations

from alembic import op

revision = "0020_session_fork_coordinator"
down_revision = "0019_shared_live_sandboxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the renewable ownership fence for one external native mutation."""
    # ``IF NOT EXISTS`` also heals a developer database that rebuilt 0019 from
    # an intermediate worktree after these columns were first drafted there.
    op.execute("ALTER TABLE agent_session_forks ADD COLUMN IF NOT EXISTS coordinator_token TEXT")
    op.execute(
        "ALTER TABLE agent_session_forks "
        "ADD COLUMN IF NOT EXISTS coordinator_lease_expires_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    """Return to the pre-coordinator shared-live schema."""
    op.execute("ALTER TABLE agent_session_forks DROP COLUMN IF EXISTS coordinator_lease_expires_at")
    op.execute("ALTER TABLE agent_session_forks DROP COLUMN IF EXISTS coordinator_token")
