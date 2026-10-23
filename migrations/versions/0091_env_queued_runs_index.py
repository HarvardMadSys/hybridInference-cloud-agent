"""Index the queued environment runs the host pool scans for every idle host.

Revision ID: 0091_env_queued_runs_index
Revises: 0090_user_model_preferences

Every idle environment-capable host claims at least every five seconds, and
the maintenance pass repeats the scan for abandoned prebuilds every thirty.
Nothing indexed ``state='queued'`` since migration ``0078`` left the partial
unique index bound to ``purpose='test'``, so each claim scanned the table's
whole test and prebuild history. The partial index holds only the queued runs
and is probed for emptiness, which is the common case.

Built ``CONCURRENTLY`` because the deploy migrates while the previous API is
still serving, and a plain build holds a lock that blocks every insert into
``agent_environment_runs`` until it finishes. A concurrent build cannot run in
a transaction, and one that is interrupted leaves an invalid index behind, so
the upgrade drops any index of this name first and builds it again.

Offline SQL builds it plainly instead. It is applied to a database nothing is
serving from yet, and ``scripts/convert_legacy_database_copy.py`` runs every
revision's SQL inside its one conversion transaction, where a concurrent build
is refused.
"""

from alembic import context, op

revision = "0091_env_queued_runs_index"
down_revision = "0090_user_model_preferences"
branch_labels = None
depends_on = None

_INDEX = "idx_agent_environment_runs_queued"
_DEFINITION = f"{_INDEX} ON agent_environment_runs (created_at) WHERE state = 'queued'"


def upgrade() -> None:
    """Build the queued-runs index without blocking run inserts."""
    if context.is_offline_mode():
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        op.execute(f"CREATE INDEX {_DEFINITION}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY {_DEFINITION}")


def downgrade() -> None:
    """Drop the queued-runs index without blocking run inserts."""
    if context.is_offline_mode():
        op.execute(f"DROP INDEX {_INDEX}")
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
