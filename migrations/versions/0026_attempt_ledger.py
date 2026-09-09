"""Keep the actual inference grant and refreshable usage snapshot per attempt.

Revision ID: 0026_attempt_ledger
Revises: 0025_retained_followup_gate
"""

from __future__ import annotations

from alembic import op

revision = "0026_attempt_ledger"
down_revision = "0025_retained_followup_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add durable attribution without guessing historical grant bindings."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_thread_residencies ADD COLUMN IF NOT EXISTS ledger_binding_id TEXT"
    )
    # A binding identifies the configured Gateway, never its credential. NULL
    # marks grants minted before this writer was deployed; do not backfill it
    # from today's configuration or from the mutable per-attempt grant link.
    op.execute(
        """
        CREATE TABLE agent_attempt_ledger (
            id BIGSERIAL PRIMARY KEY,
            attempt_id BIGINT NOT NULL REFERENCES agent_attempts(id) ON DELETE CASCADE,
            grant_id TEXT NOT NULL,
            residency_generation BIGINT NOT NULL,
            binding_id TEXT NOT NULL,
            window_since TIMESTAMPTZ NOT NULL,
            snapshot JSONB,
            queried_at TIMESTAMPTZ,
            last_error_code TEXT,
            next_refresh_at TIMESTAMPTZ,
            last_refresh_attempt_at TIMESTAMPTZ,
            refresh_generation BIGINT NOT NULL DEFAULT 0,
            refresh_lease_expires_at TIMESTAMPTZ,
            terminal_refresh_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE (attempt_id, grant_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_attempt_ledger_due ON agent_attempt_ledger(next_refresh_at) "
        "WHERE next_refresh_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_agent_attempt_ledger_initial ON agent_attempt_ledger(attempt_id) "
        "WHERE terminal_refresh_count = 0"
    )
    op.execute(
        "CREATE INDEX idx_agent_attempt_ledger_leased "
        "ON agent_attempt_ledger(refresh_lease_expires_at) "
        "WHERE refresh_lease_expires_at IS NOT NULL"
    )


def downgrade() -> None:
    """Remove snapshots and the non-secret residency binding marker."""
    op.execute("DROP TABLE IF EXISTS agent_attempt_ledger")
    op.execute("ALTER TABLE agent_thread_residencies DROP COLUMN IF EXISTS ledger_binding_id")
