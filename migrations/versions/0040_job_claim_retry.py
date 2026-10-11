"""Delay transient credential retries without blocking later queued jobs.

Revision ID: 0040_job_claim_retry
Revises: 0039_compute_defaults
"""

from alembic import op

revision = "0040_job_claim_retry"
down_revision = "0039_compute_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Track the earliest time a released claim can be retried."""
    op.execute("ALTER TABLE agent_jobs ADD COLUMN claim_retry_at TIMESTAMPTZ")


def downgrade() -> None:
    """Restore immediate eligibility for released claims."""
    op.execute("ALTER TABLE agent_jobs DROP COLUMN claim_retry_at")
