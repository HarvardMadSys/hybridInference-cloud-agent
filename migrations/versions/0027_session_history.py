"""Owner-visible session deletion with retained runtime and billing history.

Revision ID: 0027_session_history
Revises: 0026_attempt_ledger
"""

from __future__ import annotations

from alembic import op

revision = "0027_session_history"
down_revision = "0026_attempt_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a tombstone without breaking native-fork or accounting foreign keys."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    with op.get_context().autocommit_block():
        # A cancelled concurrent build can leave an INVALID index. Drop it
        # before retrying so IF NOT EXISTS cannot silently preserve that index.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_threads_archive_history")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_threads_archive_history "
            "ON agent_threads(user_id, archived_at DESC, id DESC) WHERE deleted_at IS NULL"
        )


def downgrade() -> None:
    """Drop the index while preserving irreversible owner-history tombstones."""
    # Removing deleted_at would expose deleted conversations on a later
    # upgrade. Keep the additive column rather than resurrecting history.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_threads_archive_history")
