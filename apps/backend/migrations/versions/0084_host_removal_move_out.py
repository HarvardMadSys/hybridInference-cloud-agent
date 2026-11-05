"""Let an administrator's removal move a Host's groups out before revoking it.

Revision ID: 0084_host_removal_move_out
Revises: 0083_host_move_out

``removal_moves_out`` marks a pending removal that waits for the Host's groups
to move to other Hosts through their checkpoints, as a lease-driven move-out
does. A removal without it keeps the earlier meaning: revoke once the running
turns end. Existing pending removals keep that meaning.
``removal_leaves_unmoved`` records the administrator's word that groups which
can never move (a stopped group whose checkpoint failed, say) may stay bound to
the removed Host; without it such a group holds the removal. See
``docs/agents/specs/2026-10-16-host-move-out-design.zh-CN.md``.
"""

from alembic import op

revision = "0084_host_removal_move_out"
down_revision = "0083_host_move_out"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the flags, false for every existing row."""
    op.execute(
        "ALTER TABLE agent_runner_hosts "
        "ADD COLUMN removal_moves_out BOOLEAN NOT NULL DEFAULT FALSE, "
        "ADD COLUMN removal_leaves_unmoved BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    """Drop the flags; a pending moving removal then finishes as a plain one."""
    op.execute(
        "ALTER TABLE agent_runner_hosts "
        "DROP COLUMN removal_leaves_unmoved, DROP COLUMN removal_moves_out"
    )
