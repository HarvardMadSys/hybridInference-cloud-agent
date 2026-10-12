"""Merge the trajectory branch and protect its ledger from truncation.

Revision ID: 0057_admin_sandbox_trajectory
Revises: 0046_admin_sandbox_trajectory, 0056_cloudlab_autoscaling
"""

from alembic import op

revision = "0057_admin_sandbox_trajectory"
down_revision = ("0046_admin_sandbox_trajectory", "0056_cloudlab_autoscaling")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the committed branches and reject destructive truncation."""
    op.execute(
        """
        CREATE TRIGGER trg_agent_sandbox_events_no_truncate
        BEFORE TRUNCATE ON agent_sandbox_events
        FOR EACH STATEMENT EXECUTE FUNCTION agent_reject_sandbox_event_mutation()
        """
    )


def downgrade() -> None:
    """Remove only the post-merge truncate guard."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_agent_sandbox_events_no_truncate ON agent_sandbox_events"
    )
