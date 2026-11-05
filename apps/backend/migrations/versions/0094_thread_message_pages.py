"""Index bounded transcript pages in their existing prompt-first turn order.

Revision ID: 0094_thread_message_pages
Revises: 0093_recent_requests

Online builds do not block message inserts. Retrying an interrupted build drops
the invalid index first. Offline conversion runs inside a transaction and uses
a plain build. No stored message or ordering is changed.
"""

from alembic import context, op

revision = "0094_thread_message_pages"
down_revision = "0093_recent_requests"
branch_labels = None
depends_on = None

_INDEX = "idx_agent_thread_messages_job_order"
_DEFINITION = (
    f"{_INDEX} ON agent_thread_messages "
    "(job_id, (CASE WHEN role = 'user' AND NOT mid_turn THEN 0 ELSE 1 END), id)"
)


def upgrade() -> None:
    """Build an ordered seek index without blocking the serving API."""
    if context.is_offline_mode():
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        op.execute(f"CREATE INDEX {_DEFINITION}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY {_DEFINITION}")


def downgrade() -> None:
    """Remove the read optimization without changing history."""
    if context.is_offline_mode():
        op.execute(f"DROP INDEX {_INDEX}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
