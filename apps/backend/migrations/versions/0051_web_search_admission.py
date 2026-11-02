"""Add short-lived, content-free receipts for Web Search admission.

Revision ID: 0051_web_search_admission
Revises: 0050_user_memory_edits
"""

from alembic import op

revision = "0051_web_search_admission"
down_revision = "0050_user_memory_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the cross-replica concurrency and rate-accounting ledger."""
    op.execute(
        """
        CREATE TABLE agent_web_search_admissions (
            call_id TEXT PRIMARY KEY
                CHECK (call_id ~ '^[0-9a-f]{32}$'),
            user_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_id BIGINT NOT NULL,
            lease_generation BIGINT NOT NULL CHECK (lease_generation > 0),
            admitted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            active_until TIMESTAMPTZ NOT NULL,
            CONSTRAINT ck_agent_web_search_active_window
                CHECK (active_until >= admitted_at),
            CONSTRAINT fk_agent_web_search_attempt
                FOREIGN KEY (job_id, attempt_id)
                REFERENCES agent_attempts (job_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_web_search_admitted ON agent_web_search_admissions (admitted_at)"
    )
    op.execute(
        "CREATE INDEX idx_agent_web_search_user_admitted "
        "ON agent_web_search_admissions (user_id, admitted_at)"
    )
    op.execute(
        "CREATE INDEX idx_agent_web_search_active ON agent_web_search_admissions (active_until)"
    )


def downgrade() -> None:
    """Remove Web Search admission state without touching jobs or attempts."""
    op.execute("DROP TABLE agent_web_search_admissions")
