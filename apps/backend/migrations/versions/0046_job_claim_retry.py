"""Delay transient credential retries without blocking later queued jobs.

Revision ID: 0046_job_claim_retry
Revises: 0045_admin_sandbox_deletion
"""

from alembic import op

revision = "0046_job_claim_retry"
down_revision = "0045_admin_sandbox_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Track the earliest time a released claim can be retried."""
    op.execute("ALTER TABLE agent_jobs ADD COLUMN claim_retry_at TIMESTAMPTZ")


def downgrade() -> None:
    """Restore immediate eligibility for released claims."""
    op.execute("ALTER TABLE agent_jobs DROP COLUMN claim_retry_at")
