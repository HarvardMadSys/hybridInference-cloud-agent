"""Let a stopped sandbox group move to another host through its checkpoint.

Revision ID: 0073_sandbox_restore
Revises: 0072_drop_old_memory_tables
"""

from alembic import op

revision = "0073_sandbox_restore"
down_revision = "0072_drop_old_memory_tables"
branch_labels = None
depends_on = None

_STATES = (
    "'provisioning', 'starting', 'running', 'stopping', 'stopped', 'stopped_dirty', 'lost'"
)


def upgrade() -> None:
    """Add the cold/restoring states and the checkpoint a group would restore from.

    Existing groups start with ``start_seq = 0`` and no restore point, and
    checkpoints already recorded carry no ``start_seq``: none of them can
    cover a group, so nothing moves until a host captures a new checkpoint.
    """
    op.execute(
        f"""
        ALTER TABLE agent_sandboxes
            DROP CONSTRAINT agent_sandboxes_state_check,
            ADD CONSTRAINT agent_sandboxes_state_check
                CHECK (state IN ({_STATES}, 'cold', 'restoring')),
            ADD COLUMN start_seq BIGINT NOT NULL DEFAULT 0 CHECK (start_seq >= 0),
            ADD COLUMN restore_checkpoint_id TEXT
                REFERENCES agent_sandbox_checkpoints (id) ON DELETE SET NULL,
            ADD COLUMN restore_attempts INTEGER NOT NULL DEFAULT 0
                CHECK (restore_attempts >= 0),
            ADD COLUMN restore_attempt_id BIGINT
                REFERENCES agent_attempts (id) ON DELETE SET NULL,
            ADD CONSTRAINT ck_agent_sandboxes_restore_source
                CHECK (state NOT IN ('cold', 'restoring') OR restore_checkpoint_id IS NOT NULL)
        """
    )
    op.execute(
        """
        ALTER TABLE agent_sandbox_checkpoints
            ADD COLUMN start_seq BIGINT CHECK (start_seq >= 0)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_sandboxes_restorable
            ON agent_sandboxes (state)
            WHERE state IN ('cold', 'restoring')
        """
    )


def downgrade() -> None:
    """Refuse while any group lives only in its checkpoint; otherwise drop the columns."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_sandboxes WHERE state IN ('cold', 'restoring')) THEN
                RAISE EXCEPTION 'sandbox groups are cold or restoring; restore them first';
            END IF;
        END
        $$
        """
    )
    op.execute("DROP INDEX idx_agent_sandboxes_restorable")
    op.execute("ALTER TABLE agent_sandbox_checkpoints DROP COLUMN start_seq")
    op.execute(
        f"""
        ALTER TABLE agent_sandboxes
            DROP CONSTRAINT ck_agent_sandboxes_restore_source,
            DROP COLUMN restore_attempt_id,
            DROP COLUMN restore_attempts,
            DROP COLUMN restore_checkpoint_id,
            DROP COLUMN start_seq,
            DROP CONSTRAINT agent_sandboxes_state_check,
            ADD CONSTRAINT agent_sandboxes_state_check CHECK (state IN ({_STATES}))
        """
    )
