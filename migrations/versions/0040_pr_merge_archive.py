"""Persist fenced PR merge checks and receipts for automatic session archival."""

from __future__ import annotations

from alembic import op

revision = "0040_pr_merge_archive"
down_revision = "0039_compute_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a durable obligation per session and published PR."""
    op.execute(
        """
        CREATE TABLE agent_pr_archive_checks (
            thread_id TEXT NOT NULL,
            pr_url TEXT NOT NULL CHECK (length(pr_url) > 0),
            job_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'archived', 'skipped')),
            claim_token TEXT,
            lease_expires_at TIMESTAMPTZ,
            next_check_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
            last_error TEXT CHECK (last_error ~ '^[a-z][a-z0-9_]{0,63}$'),
            merged_at TIMESTAMPTZ,
            merge_sha TEXT,
            archived_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, pr_url),
            FOREIGN KEY (thread_id, job_id)
                REFERENCES agent_jobs (thread_id, id) ON DELETE CASCADE,
            CHECK ((claim_token IS NULL) = (lease_expires_at IS NULL)),
            CHECK ((state = 'pending') = (completed_at IS NULL)),
            CHECK (state = 'pending' OR claim_token IS NULL),
            CHECK (state <> 'archived' OR
                (merged_at IS NOT NULL AND archived_at IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_pr_archive_checks_due "
        "ON agent_pr_archive_checks (next_check_at, thread_id) WHERE state = 'pending'"
    )


def downgrade() -> None:
    """Remove merge-check receipts without changing archived sessions."""
    op.execute("DROP TABLE agent_pr_archive_checks")
