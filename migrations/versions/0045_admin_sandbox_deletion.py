"""Fence archive-before-delete operations while retaining the native ledger."""

from alembic import op

revision = "0045_admin_sandbox_deletion"
down_revision = "0044_admin_sandbox_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist requests, worker leases and verified immutable S3 receipts."""
    op.execute("""
        ALTER TABLE agent_sandboxes
        ADD COLUMN deletion_state TEXT NOT NULL DEFAULT 'none'
            CHECK (deletion_state IN ('none', 'pending', 'archived', 'deleted', 'failed')),
        ADD COLUMN deletion_request_id TEXT UNIQUE,
        ADD COLUMN deletion_requested_at TIMESTAMPTZ,
        ADD COLUMN deletion_generation BIGINT,
        ADD COLUMN deletion_container_id TEXT,
        ADD COLUMN deletion_last_activity_at TIMESTAMPTZ,
        ADD COLUMN deletion_claim_token TEXT,
        ADD COLUMN deletion_claim_expires_at TIMESTAMPTZ,
        ADD COLUMN deletion_retry_at TIMESTAMPTZ,
        ADD COLUMN deletion_cleanup_required BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN deletion_cleanup_archive_key TEXT,
        ADD COLUMN deletion_archive_key TEXT,
        ADD COLUMN deletion_archive_version_id TEXT,
        ADD COLUMN deletion_sha256 TEXT CHECK (deletion_sha256 ~ '^[0-9a-f]{64}$'),
        ADD COLUMN deletion_size_bytes BIGINT CHECK (deletion_size_bytes > 0),
        ADD COLUMN deletion_error TEXT,
        ADD COLUMN deleted_at TIMESTAMPTZ,
        ADD CONSTRAINT ck_agent_sandbox_deletion_receipt CHECK (
            deletion_state NOT IN ('archived', 'deleted') OR (
                deletion_archive_key IS NOT NULL AND deletion_sha256 IS NOT NULL
                AND deletion_size_bytes IS NOT NULL
            )
        )
    """)
    op.execute("""
        CREATE INDEX idx_agent_sandboxes_deletion_due
        ON agent_sandboxes (deletion_retry_at, deletion_requested_at)
        WHERE deletion_state IN ('pending', 'archived')
    """)


def downgrade() -> None:
    """Remove deletion bookkeeping without touching retained execution rows."""
    op.execute("DROP INDEX idx_agent_sandboxes_deletion_due")
    op.execute("""
        ALTER TABLE agent_sandboxes
        DROP CONSTRAINT ck_agent_sandbox_deletion_receipt,
        DROP COLUMN deletion_state,
        DROP COLUMN deletion_request_id,
        DROP COLUMN deletion_requested_at,
        DROP COLUMN deletion_generation,
        DROP COLUMN deletion_container_id,
        DROP COLUMN deletion_last_activity_at,
        DROP COLUMN deletion_claim_token,
        DROP COLUMN deletion_claim_expires_at,
        DROP COLUMN deletion_retry_at,
        DROP COLUMN deletion_cleanup_required,
        DROP COLUMN deletion_cleanup_archive_key,
        DROP COLUMN deletion_archive_key,
        DROP COLUMN deletion_archive_version_id,
        DROP COLUMN deletion_sha256,
        DROP COLUMN deletion_size_bytes,
        DROP COLUMN deletion_error,
        DROP COLUMN deleted_at
    """)
