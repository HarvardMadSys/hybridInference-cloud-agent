"""Let a Host whose lease is ending hold its claims while its groups move out.

Revision ID: 0083_host_move_out
Revises: 0082_team_environments

The ``move_out`` admission hold closes every claim on a Host in its move-out
window, so each chat's next turn is claimed by another Host from the group's
checkpoint. See ``docs/agents/specs/2026-10-16-host-move-out-design.zh-CN.md``.
"""

from alembic import op

revision = "0083_host_move_out"
down_revision = "0082_team_environments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Accept the move-out hold purpose."""
    op.execute(
        """
        ALTER TABLE agent_host_admission_holds
            DROP CONSTRAINT agent_host_admission_holds_purpose_check,
            ADD CONSTRAINT agent_host_admission_holds_purpose_check
                CHECK (purpose IN ('bootstrap','lease_deadline','retirement','rotation',
                                   'autoscaling_slots','move_out'));
        """
    )


def downgrade() -> None:
    """Drop move-out holds and the purpose; moved groups keep their checkpoints."""
    op.execute(
        """
        DELETE FROM agent_host_admission_holds WHERE purpose='move_out';
        ALTER TABLE agent_host_admission_holds
            DROP CONSTRAINT agent_host_admission_holds_purpose_check,
            ADD CONSTRAINT agent_host_admission_holds_purpose_check
                CHECK (purpose IN ('bootstrap','lease_deadline','retirement','rotation',
                                   'autoscaling_slots'));
        """
    )
