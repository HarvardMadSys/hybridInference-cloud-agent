"""Persist the bounded context used to title a snapshot publication.

Revision ID: 0094_snapshot_title_context
Revises: 0093_thread_message_pages

The publisher used to rebuild every captured chat transcript while claiming a
snapshot.  Keeping the already-bounded title input on the immutable snapshot
means a claim's database result and Python decode no longer grow with chat
history.
"""

from alembic import op

revision = "0094_snapshot_title_context"
down_revision = "0093_thread_message_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an empty context for old snapshots; new captures fill it."""
    op.execute(
        """
        ALTER TABLE agent_sandbox_snapshots
        ADD COLUMN title_context JSONB NOT NULL
            DEFAULT '{"messages": [], "summaries": [], "summary_count": 0}'::jsonb,
        ADD CONSTRAINT ck_agent_sandbox_snapshots_title_context_object
            CHECK (jsonb_typeof(title_context) = 'object')
        """
    )


def downgrade() -> None:
    """Remove the derived publication-title context."""
    op.execute(
        """
        ALTER TABLE agent_sandbox_snapshots
        DROP CONSTRAINT ck_agent_sandbox_snapshots_title_context_object,
        DROP COLUMN title_context
        """
    )
