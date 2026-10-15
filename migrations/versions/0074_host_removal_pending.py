"""Remember that an operator asked to remove a host that was still busy.

Revision ID: 0074_host_removal_pending
Revises: 0073_sandbox_restore

Remove used to refuse a host that still held slots, so an operator had to
drain it, wait, and come back. Now Remove drains a busy host itself and this
timestamp records the request; the reaper revokes the host once its last
running turn ends. The constraint keeps the request meaningful: only a
draining host can be waiting to be removed, so every path that moves a host
out of ``draining`` has to clear it.
"""

from alembic import op

revision = "0074_host_removal_pending"
down_revision = "0073_sandbox_restore"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable request time and tie it to ``draining``."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
        ADD COLUMN removal_requested_at TIMESTAMPTZ,
        ADD CONSTRAINT ck_agent_runner_host_removal_draining CHECK (
            removal_requested_at IS NULL OR status = 'draining'
        )
        """
    )


def downgrade() -> None:
    """Drop the request; a pending host stays draining, as a manual drain did."""
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
        DROP CONSTRAINT ck_agent_runner_host_removal_draining,
        DROP COLUMN removal_requested_at
        """
    )
