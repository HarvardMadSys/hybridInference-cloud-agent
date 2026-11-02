"""Persist bounded diagnostics for each sandbox deletion attempt."""

from alembic import op

revision = "0087_sandbox_deletion_attempts"
down_revision = "0086_conversation_read_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an admin-only, secret-free ledger without changing deletion state."""
    op.execute("""
        CREATE TABLE agent_sandbox_deletion_attempts (
            correlation_id UUID PRIMARY KEY,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
            stage TEXT NOT NULL CHECK (stage IN (
                'sandbox_manager_resolution', 'stop_verification', 'archive_prepare',
                'upload_authorization',
                'archive_upload', 'archive_verification', 'durable_receipt',
                'container_removal', 'database_finalization', 'discard_container_removal',
                'discard_receipt_validation', 'cleanup_claim_renewal', 'cleanup_abort'
            )),
            outcome TEXT NOT NULL CHECK (outcome IN (
                'running', 'retrying', 'succeeded', 'failed', 'lease_expired'
            )),
            failure_code TEXT CHECK (
                failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$'
            ),
            sandbox_manager_status SMALLINT CHECK (
                sandbox_manager_status IS NULL OR sandbox_manager_status BETWEEN 100 AND 599
            ),
            retry_class TEXT CHECK (retry_class IS NULL OR retry_class IN (
                'transient', 'permanent', 'operator_action'
            )),
            retry_scheduled BOOLEAN,
            cleanup_outcome TEXT NOT NULL DEFAULT 'not_required' CHECK (cleanup_outcome IN (
                'not_required', 'pending', 'acknowledged', 'unconfirmed'
            )),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            UNIQUE (request_id, attempt_number),
            CHECK ((outcome = 'running' AND finished_at IS NULL)
                OR (outcome <> 'running' AND finished_at IS NOT NULL)),
            CHECK ((failure_code IS NULL) = (retry_class IS NULL)),
            CHECK (outcome <> 'retrying' OR retry_scheduled IS TRUE),
            CHECK (outcome <> 'failed' OR retry_scheduled IS FALSE)
        )
    """)
    op.execute("""
        CREATE INDEX idx_agent_sandbox_deletion_attempts_recent
        ON agent_sandbox_deletion_attempts (sandbox_id, started_at DESC)
    """)


def downgrade() -> None:
    """Remove the additive attempt history table."""
    op.execute("DROP INDEX idx_agent_sandbox_deletion_attempts_recent")
    op.execute("DROP TABLE agent_sandbox_deletion_attempts")
