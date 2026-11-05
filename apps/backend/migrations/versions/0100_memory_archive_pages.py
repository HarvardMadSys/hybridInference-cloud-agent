"""Index bounded active-memory and archive-page reads.

Revision ID: 0100_memory_archive_pages
Revises: 0099_thread_stats_rollups
"""

from alembic import context, op

revision = "0100_memory_archive_pages"
down_revision = "0099_thread_stats_rollups"
branch_labels = None
depends_on = None

_INDEXES = {
    "idx_agent_memories_active_page": "ON agent_memories (user_id,COALESCE(repo,''),created_at DESC,id) WHERE status='active'",
    "idx_agent_memories_archive_page": "ON agent_memories (user_id,COALESCE(repo,''),archived_at DESC,id DESC) WHERE status='archived'",
}


def upgrade() -> None:
    """Build owner/scope access paths without blocking memory writes."""
    if context.is_offline_mode():
        for name, definition in _INDEXES.items():
            op.execute(f"DROP INDEX IF EXISTS {name}")
            op.execute(f"CREATE INDEX {name} {definition}")
        return
    with op.get_context().autocommit_block():
        for name, definition in _INDEXES.items():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY {name} {definition}")


def downgrade() -> None:
    """Remove page indexes without changing any memory."""
    if context.is_offline_mode():
        for name in _INDEXES:
            op.execute(f"DROP INDEX {name}")
        return
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
