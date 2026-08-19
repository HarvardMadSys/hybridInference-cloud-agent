"""Record which continuity rung each attempt actually ran on.

``hot`` (live process), ``cold_native`` (checkpoint resume) or ``replay``
(transcript) — design §9's "automatic and observable": the user never
configures the ladder, but every turn says which rung it ran on, the UI can
show it, and admin can aggregate HOT hit-rate and degradation reasons.
Nullable: attempts from before this column simply have no answer.

Revision ID: 0010_continuity_mode
Revises: 0009_thread_residency
"""

from __future__ import annotations

from alembic import op

revision = "0010_continuity_mode"
down_revision = "0009_thread_residency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the per-attempt continuity mode."""
    op.execute(
        "ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS continuity_mode TEXT "
        "CHECK (continuity_mode IN ('hot', 'cold_native', 'replay'))"
    )


def downgrade() -> None:
    """Drop the continuity mode."""
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS continuity_mode")
