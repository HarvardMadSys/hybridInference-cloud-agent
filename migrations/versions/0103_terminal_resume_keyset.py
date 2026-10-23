"""Complete the terminal-resume replay keyset index.

Revision ID: 0103_terminal_resume_keyset
Revises: 0102_admin_sandbox_marker_order

The replay orders and continues by (updated_at, id). The original partial
index stopped at updated_at, so a bulk transition sharing one NOW() still
rescanned and sorted the preceding ids on every continuation page.
"""

from alembic import context, op

revision = "0103_terminal_resume_keyset"
down_revision = "0102_admin_sandbox_marker_order"
branch_labels = None
depends_on = None

_INDEX = "idx_agent_jobs_terminal_resume"
_KEYSET = f"{_INDEX} ON agent_jobs (updated_at, id) WHERE terminal_resume_pending"
_PREVIOUS = f"{_INDEX} ON agent_jobs (updated_at) WHERE terminal_resume_pending"


def _replace(definition: str) -> None:
    """Install one retry-safe online definition."""
    if context.is_offline_mode():
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        op.execute(f"CREATE INDEX {definition}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY {definition}")


def upgrade() -> None:
    """Align the partial index with the full replay keyset."""
    _replace(_KEYSET)


def downgrade() -> None:
    """Restore the preceding one-column replay index."""
    _replace(_PREVIOUS)
