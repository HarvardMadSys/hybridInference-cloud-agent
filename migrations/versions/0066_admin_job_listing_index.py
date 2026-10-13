"""Index every owner's turns by creation time for the admin listings.

Revision ID: 0066_admin_job_listing_index
Revises: 0065_environments

The admin run list, conversation list and usage selection read turns across
owners in a ``created_at`` window, newest or oldest first with ``id`` as the
tie breaker. The only ``created_at`` indexes on ``agent_jobs`` lead with
``user_id`` or cover queued turns alone, so without an owner filter every page
scanned and sorted the whole job history under the 5 s admin statement timeout.

Built ``CONCURRENTLY`` because the deploy migrates while the previous API is
still serving, and a plain build holds a lock that blocks every write to
``agent_jobs`` until it finishes. A concurrent build cannot run in a
transaction, and one that is interrupted leaves an invalid index behind, so the
upgrade drops any index of this name first and builds it again.

Offline SQL builds it plainly instead. It is applied to a database nothing is
serving from yet, and ``scripts/convert_legacy_database_copy.py`` runs every
revision's SQL inside its one conversion transaction, where a concurrent build
is refused.
"""

from alembic import context, op

revision = "0066_admin_job_listing_index"
down_revision = "0065_environments"
branch_labels = None
depends_on = None

_DEFINITION = "idx_agent_jobs_created ON agent_jobs (created_at DESC, id DESC)"


def upgrade() -> None:
    """Build the creation-time index without blocking job writes."""
    if context.is_offline_mode():
        op.execute("DROP INDEX IF EXISTS idx_agent_jobs_created")
        op.execute(f"CREATE INDEX {_DEFINITION}")
        return
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_jobs_created")
        op.execute(f"CREATE INDEX CONCURRENTLY {_DEFINITION}")


def downgrade() -> None:
    """Drop the creation-time index without blocking job writes."""
    if context.is_offline_mode():
        op.execute("DROP INDEX idx_agent_jobs_created")
        return
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY idx_agent_jobs_created")
