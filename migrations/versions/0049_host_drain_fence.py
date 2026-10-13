"""Fence automated host drains against stale release requests.

Revision ID: 0049_host_drain_fence
Revises: 0048_sandbox_plugins
"""

from alembic import op

revision = "0049_host_drain_fence"
down_revision = "0048_sandbox_plugins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist one active drain owner or its released idempotency receipt."""
    op.execute("""
        ALTER TABLE agent_runner_hosts
        ADD COLUMN drain_fence_token UUID,
        ADD COLUMN drain_fence_status_before TEXT,
        ADD COLUMN drain_fence_active BOOLEAN NOT NULL DEFAULT FALSE,
        ADD CONSTRAINT ck_agent_runner_host_drain_fence CHECK (
            (
                drain_fence_token IS NULL
                AND drain_fence_status_before IS NULL
                AND NOT drain_fence_active
            )
            OR (
                drain_fence_token IS NOT NULL
                AND drain_fence_status_before IS NOT NULL
                AND drain_fence_status_before IN ('online', 'draining')
                AND (
                    (drain_fence_active AND status = 'draining')
                    OR (NOT drain_fence_active AND status = 'online')
                )
            )
        )
    """)


def downgrade() -> None:
    """Remove automated-drain ownership and receipts."""
    op.execute("""
        ALTER TABLE agent_runner_hosts
        DROP CONSTRAINT ck_agent_runner_host_drain_fence,
        DROP COLUMN drain_fence_active,
        DROP COLUMN drain_fence_status_before,
        DROP COLUMN drain_fence_token
    """)
