"""Track title ownership so background summaries cannot overwrite manual names.

Revision ID: 0028_task_titles
Revises: 0027_session_history
"""

from __future__ import annotations

from alembic import op

revision = "0028_task_titles"
down_revision = "0027_session_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve every existing title; opt new conversations into generation explicitly."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_threads ADD COLUMN title_source TEXT NOT NULL DEFAULT 'manual' "
        "CHECK (title_source IN ('fallback', 'generated', 'manual', 'inherited'))"
    )


def downgrade() -> None:
    """Remove provenance without changing conversation titles."""
    op.drop_column("agent_threads", "title_source")
