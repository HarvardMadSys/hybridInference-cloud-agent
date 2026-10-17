"""Index the turns whose settled workspace still waits for its terminals to resume.

Revision ID: 0085_terminal_resume_index
Revises: 0084_host_removal_move_out

The control plane replays these recovery markers on every maintenance pass,
every thirty seconds, oldest first. Nothing indexed ``terminal_resume_pending``,
so each pass scanned and sorted the whole job history to find the few turns
that carry it. The partial index holds only those turns, in the order the pass
reads them.

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

revision = "0085_terminal_resume_index"
down_revision = "0084_host_removal_move_out"
branch_labels = None
depends_on = None

_DEFINITION = (
    "idx_agent_jobs_terminal_resume ON agent_jobs (updated_at) WHERE terminal_resume_pending"
)


def upgrade() -> None:
    """Build the recovery-marker index without blocking job writes."""
    if context.is_offline_mode():
        op.execute("DROP INDEX IF EXISTS idx_agent_jobs_terminal_resume")
        op.execute(f"CREATE INDEX {_DEFINITION}")
        return
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_agent_jobs_terminal_resume")
        op.execute(f"CREATE INDEX CONCURRENTLY {_DEFINITION}")


def downgrade() -> None:
    """Drop the recovery-marker index without blocking job writes."""
    if context.is_offline_mode():
        op.execute("DROP INDEX idx_agent_jobs_terminal_resume")
        return
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY idx_agent_jobs_terminal_resume")
