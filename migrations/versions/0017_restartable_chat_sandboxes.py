"""Durable ownership and lifecycle for one-chat/one-container sandboxes.

The legacy ``agent_thread_residencies`` row is a short-lived HOT optimization:
its lease may expire and affinity may fall away.  A retained Docker container
is different.  Even while STOPPED it remains the only local copy of the chat's
workspace and native CLI state, so its host/container identity must remain a
durable scheduling fact rather than disappear with a process lease.

This table is additive so legacy one-shot threads continue unchanged during
rollout.  ``agent_attempts.context_status`` records the product-level semantic
(NEW/LIVE/RESTORED/LOST) beside, not instead of, ``continuity_mode``'s
diagnostic replay/hot/warm/cold rung.

Revision ID: 0017_restartable_chat_sandboxes
Revises: 0016_runtime_version_menu
"""

from __future__ import annotations

from alembic import op

revision = "0017_restartable_chat_sandboxes"
down_revision = "0016_runtime_version_menu"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the retained-sandbox ledger and per-attempt context semantic."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_thread_sandboxes (
            thread_id TEXT PRIMARY KEY REFERENCES agent_threads(id) ON DELETE CASCADE,
            sandbox_id TEXT NOT NULL UNIQUE,
            generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
            host TEXT NOT NULL,
            container_id TEXT UNIQUE,
            container_name TEXT NOT NULL UNIQUE,
            runtime_id TEXT NOT NULL,
            runtime_version TEXT,
            sandbox_image_digest TEXT,
            execution_profile TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'provisioning'
                CHECK (state IN (
                    'provisioning', 'starting', 'running', 'stopping',
                    'stopped', 'stopped_dirty', 'lost'
                )),
            context_status TEXT NOT NULL DEFAULT 'new'
                CHECK (context_status IN ('new', 'live', 'restored', 'lost')),
            activation_generation BIGINT NOT NULL DEFAULT 0
                CHECK (activation_generation >= 0),
            process_generation BIGINT NOT NULL DEFAULT 0
                CHECK (process_generation >= 0),
            active_attempt_id BIGINT REFERENCES agent_attempts(id),
            admitted_job_id TEXT REFERENCES agent_jobs(id),
            admitted_at TIMESTAMPTZ,
            last_settled_job_id TEXT REFERENCES agent_jobs(id),
            native_session_id TEXT,
            idle_deadline TIMESTAMPTZ,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_thread_sandboxes_host_state "
        "ON agent_thread_sandboxes(host, state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_thread_sandboxes_idle "
        "ON agent_thread_sandboxes(idle_deadline) "
        "WHERE state = 'running' AND active_attempt_id IS NULL"
    )
    # Keep local preview databases upgradeable while this unreleased migration
    # is still evolving: ``CREATE TABLE IF NOT EXISTS`` cannot add columns to
    # a table created by an earlier checkout of the same branch.
    op.execute(
        "ALTER TABLE agent_thread_sandboxes "
        "ADD COLUMN IF NOT EXISTS admitted_job_id TEXT REFERENCES agent_jobs(id)"
    )
    op.execute(
        "ALTER TABLE agent_thread_sandboxes ADD COLUMN IF NOT EXISTS admitted_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS context_status TEXT "
        "CHECK (context_status IN ('new', 'live', 'restored', 'lost'))"
    )


def downgrade() -> None:
    """Remove restartable-mode state without touching legacy continuity."""
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS context_status")
    op.execute("DROP INDEX IF EXISTS idx_agent_thread_sandboxes_idle")
    op.execute("DROP INDEX IF EXISTS idx_agent_thread_sandboxes_host_state")
    op.execute("DROP TABLE IF EXISTS agent_thread_sandboxes")
