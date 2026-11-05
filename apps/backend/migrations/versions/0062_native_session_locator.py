"""Reject blank durable native-session locators.

Revision ID: 0062_native_session_locator
Revises: 0061_mcp_catalog_sync_fence
"""

from alembic import op

revision = "0062_native_session_locator"
down_revision = "0061_mcp_catalog_sync_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Normalize legacy blanks and prevent another impossible restart state."""
    op.execute(
        """
        UPDATE agent_thread_sandbox_memberships
        SET native_session_id = NULL,
            resident_state = CASE
                WHEN resident_state = 'restarting'
                 AND (native_state = 'lost' OR context_status = 'lost') THEN 'lost'
                WHEN resident_state = 'restarting' THEN 'stopped'
                ELSE resident_state
            END,
            updated_at = NOW()
        WHERE native_session_id IS NOT NULL
          AND native_session_id !~ '[^[:space:]]'
        """
    )
    op.execute(
        """
        UPDATE agent_thread_sandbox_memberships
        SET resident_state = CASE
                WHEN native_state = 'lost' OR context_status = 'lost' THEN 'lost'
                ELSE 'stopped'
            END,
            updated_at = NOW()
        WHERE native_session_id IS NULL AND resident_state = 'restarting'
        """
    )
    op.execute(
        """
        ALTER TABLE agent_thread_sandbox_memberships
            ADD CONSTRAINT ck_agent_memberships_native_session_locator
            CHECK (
                native_session_id IS NULL
                OR native_session_id ~ '[^[:space:]]'
            ) NOT VALID
        """
    )
    # Do not add ``restarting => locator`` as a database CHECK in this
    # revision. Migrations run before replicas roll, and the previous release
    # can still write NULL + restarting while abandoning a pre-admission
    # activation. Rejecting that write would roll back its whole cleanup
    # transaction. The writers in this release derive STOPPED instead; a
    # later migration can enforce the semantic pair after the compatibility
    # window closes.
    op.execute(
        """
        ALTER TABLE agent_thread_sandbox_memberships
            VALIDATE CONSTRAINT ck_agent_memberships_native_session_locator
        """
    )


def downgrade() -> None:
    """Remove the locator guard; normalized legacy values stay normalized."""
    op.execute(
        """
        ALTER TABLE agent_thread_sandbox_memberships
            DROP CONSTRAINT ck_agent_memberships_native_session_locator
        """
    )
