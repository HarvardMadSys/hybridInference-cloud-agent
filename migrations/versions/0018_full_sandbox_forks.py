"""Crash-recoverable full Docker sandbox forks.

A retained chat's conversation and Docker writable layer have to cross the
fork boundary together.  The operation row is the durable hand-off between
the short database transactions and the host's Docker commit/create calls;
``fork_operation_id`` fences new claims while that hand-off is in progress.

Revision ID: 0018_full_sandbox_forks
Revises: 0017_restartable_chat_sandboxes
"""

from __future__ import annotations

from alembic import op

revision = "0018_full_sandbox_forks"
down_revision = "0017_restartable_chat_sandboxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the retained-fork saga and its source-chat claim fence."""
    op.execute(
        """
        CREATE TABLE agent_sandbox_forks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            source_thread_id TEXT NOT NULL
                REFERENCES agent_threads(id) ON DELETE CASCADE,
            source_job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
            source_sandbox_id TEXT NOT NULL,
            source_sandbox_generation BIGINT NOT NULL CHECK (source_sandbox_generation > 0),
            source_activation_generation BIGINT NOT NULL
                CHECK (source_activation_generation >= 0),
            source_container_id TEXT NOT NULL,
            source_native_session_id TEXT NOT NULL,
            target_thread_id TEXT NOT NULL UNIQUE,
            target_sandbox_id TEXT NOT NULL UNIQUE,
            target_container_id TEXT UNIQUE,
            target_anchor_job_id TEXT REFERENCES agent_jobs(id),
            snapshot_image_digest TEXT,
            source_fingerprint TEXT,
            target_fingerprint TEXT,
            state TEXT NOT NULL DEFAULT 'preparing'
                CHECK (state IN ('preparing', 'ready', 'failed')),
            cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_sandbox_forks_source_preparing "
        "ON agent_sandbox_forks(source_thread_id) WHERE state = 'preparing'"
    )
    op.execute(
        "ALTER TABLE agent_thread_sandboxes ADD COLUMN fork_operation_id TEXT "
        "REFERENCES agent_sandbox_forks(id)"
    )
    op.execute(
        "CREATE INDEX idx_agent_thread_sandboxes_fork_operation "
        "ON agent_thread_sandboxes(fork_operation_id) "
        "WHERE fork_operation_id IS NOT NULL"
    )


def downgrade() -> None:
    """Remove fork sagas without changing retained-container ownership."""
    op.execute("DROP INDEX IF EXISTS idx_agent_thread_sandboxes_fork_operation")
    op.execute("ALTER TABLE agent_thread_sandboxes DROP COLUMN IF EXISTS fork_operation_id")
    op.execute("DROP INDEX IF EXISTS idx_agent_sandbox_forks_source_preparing")
    op.execute("DROP TABLE IF EXISTS agent_sandbox_forks")
