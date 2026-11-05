"""Record each authorization to remove a container a moved group left behind."""

from alembic import op

revision = "0089_superseded_removals"
down_revision = "0088_sandbox_retry_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the empty ledger a release writes before it removes such a container."""
    op.execute("""
        CREATE TABLE agent_superseded_container_removals (
            request_id TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id) ON DELETE CASCADE,
            origin_thread_id TEXT NOT NULL,
            generation BIGINT NOT NULL,
            container_id TEXT NOT NULL,
            operation_id TEXT REFERENCES agent_compute_operations(id) ON DELETE SET NULL,
            authorized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            removed_at TIMESTAMPTZ,
            UNIQUE (host, sandbox_id, generation, container_id)
        )
    """)


def downgrade() -> None:
    """Drop the ledger; removals already made on Hosts are not undone."""
    op.execute("DROP TABLE agent_superseded_container_removals")
