"""Record how hard each turn was asked to think.

Per turn rather than per thread: the same conversation legitimately wants a
deep turn for the refactor and a shallow one for the typo that follows it, and
a turn is already where the model lives.

Nullable, with no backfill and no default. NULL is Auto — no instruction, the
runtime and model's own defaults — which is exactly what every turn created
before this column did, so the absence of a value is the truth about them
rather than a gap to fill. No CHECK constraint either: the accepted levels are
the runtime's and the model's, negotiated per pair at create time, and pinning
today's vocabulary in the schema would make a new CLI level a migration.

Revision ID: 0014_reasoning_effort
Revises: 0013_attempt_resources
"""

from __future__ import annotations

from alembic import op

revision = "0014_reasoning_effort"
down_revision = "0013_attempt_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the per-turn reasoning effort."""
    op.execute("ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS reasoning_effort TEXT")


def downgrade() -> None:
    """Drop the reasoning effort."""
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS reasoning_effort")
