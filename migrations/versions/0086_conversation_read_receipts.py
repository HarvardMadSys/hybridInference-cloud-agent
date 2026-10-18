"""Persist each owner's latest read completion per conversation.

Revision ID: 0086_conversation_read_receipts
Revises: 0085_terminal_resume_index
"""

from alembic import op

revision = "0086_conversation_read_receipts"
down_revision = "0085_terminal_resume_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add receipts without changing conversation activity or execution state."""
    op.execute(
        """
        CREATE TABLE agent_conversation_read_receipts (
            user_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            turn_no INTEGER NOT NULL CHECK (turn_no > 0),
            attempt_id BIGINT,
            revision BIGSERIAL NOT NULL,
            PRIMARY KEY (user_id, thread_id),
            FOREIGN KEY (thread_id, user_id)
                REFERENCES agent_threads (id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (thread_id, job_id)
                REFERENCES agent_jobs (thread_id, id) ON DELETE CASCADE,
            FOREIGN KEY (job_id, attempt_id)
                REFERENCES agent_attempts (job_id, id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_read_receipts_revision "
        "ON agent_conversation_read_receipts (user_id, revision)"
    )
    op.execute(
        "CREATE INDEX idx_agent_read_receipts_job_attempt "
        "ON agent_conversation_read_receipts (job_id, attempt_id)"
    )


def downgrade() -> None:
    """Remove cloud receipts; browser caches remain usable by older clients."""
    op.execute("DROP TABLE agent_conversation_read_receipts")
