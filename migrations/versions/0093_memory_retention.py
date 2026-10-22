"""Index global archived-memory retention and self-reference cleanup.

Revision ID: 0093_memory_retention
Revises: 0092_targeted_work_wakes
"""

from alembic import context, op

revision = "0093_memory_retention"
down_revision = "0092_targeted_work_wakes"
branch_labels = None
depends_on = None

_INDEXES = {
    "idx_agent_memories_retention": "ON agent_memories (archived_at, id) WHERE status = 'archived'",
    "idx_agent_memories_superseded": "ON agent_memories (superseded_by) WHERE superseded_by IS NOT NULL",
}


def upgrade() -> None:
    """Build both access paths without blocking foreground memory writes."""
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
    """Remove retention indexes, preserving all memory rows."""
    if context.is_offline_mode():
        for name in _INDEXES:
            op.execute(f"DROP INDEX {name}")
        return
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY {name}")
