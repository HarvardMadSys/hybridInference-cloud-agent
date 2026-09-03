"""Fence historical disposable failures from retained follow-up execution.

Revision ID: 0025_retained_followup_gate
Revises: 0024_native_turn_rollout_guard
"""

from __future__ import annotations

from alembic import op

revision = "0025_retained_followup_gate"
down_revision = "0024_native_turn_rollout_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep pre-cutover rows distinguishable from new retained jobs."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS retained_followup_eligible BOOLEAN")
    # NULL is deliberately also the default. The previous API keeps serving
    # while this migration runs and does not name this column in INSERTs; a
    # database TRUE default would therefore misclassify disposable-era work
    # created during that compatibility window. Only the new API explicitly
    # writes TRUE. A real retained membership independently proves continuity
    # for an older row, and keeping the column nullable avoids a table-wide
    # UPDATE/NOT NULL validation on the live jobs table.


def downgrade() -> None:
    """Remove the cutover disposition marker."""
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS retained_followup_eligible")
