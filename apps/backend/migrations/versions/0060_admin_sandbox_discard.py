"""Allow an administrator to delete a stopped sandbox without an S3 archive."""

from alembic import op

revision = "0060_admin_sandbox_discard"
down_revision = "0059_counter_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record discard requests and permit a deleted row with no archive receipt."""
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            ADD COLUMN deletion_discard BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    op.execute("ALTER TABLE agent_sandboxes DROP CONSTRAINT ck_agent_sandbox_deletion_receipt")
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            ADD CONSTRAINT ck_agent_sandbox_deletion_receipt CHECK (
                CASE
                    WHEN deletion_state = 'archived' THEN
                        NOT deletion_discard
                        AND deletion_archive_key IS NOT NULL
                        AND deletion_sha256 IS NOT NULL
                        AND deletion_size_bytes IS NOT NULL
                    WHEN deletion_state = 'deleted' AND deletion_discard THEN
                        deletion_archive_key IS NULL
                        AND deletion_sha256 IS NULL
                        AND deletion_size_bytes IS NULL
                    WHEN deletion_state = 'deleted' THEN
                        deletion_archive_key IS NOT NULL
                        AND deletion_sha256 IS NOT NULL
                        AND deletion_size_bytes IS NOT NULL
                    ELSE TRUE
                END
            )
        """
    )


def downgrade() -> None:
    """Restore the archive receipt requirement when no discard is in progress."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_sandboxes WHERE deletion_discard) THEN
                RAISE EXCEPTION
                    'cannot downgrade while a sandbox deletion is marked discard';
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE agent_sandboxes DROP CONSTRAINT ck_agent_sandbox_deletion_receipt")
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            ADD CONSTRAINT ck_agent_sandbox_deletion_receipt CHECK (
                deletion_state NOT IN ('archived', 'deleted') OR (
                    deletion_archive_key IS NOT NULL AND deletion_sha256 IS NOT NULL
                    AND deletion_size_bytes IS NOT NULL
                )
            )
        """
    )
    op.execute("ALTER TABLE agent_sandboxes DROP COLUMN deletion_discard")
