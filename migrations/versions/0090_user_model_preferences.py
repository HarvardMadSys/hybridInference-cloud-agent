"""Add each owner's pinned default model for new tasks.

Revision ID: 0090_user_model_preferences
Revises: 0089_superseded_removals
"""

from alembic import op

revision = "0090_user_model_preferences"
down_revision = "0089_superseded_removals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store only the explicit pin; the last used model is read from its turn."""
    op.execute(
        """
        CREATE TABLE agent_user_model_preferences (
            user_id TEXT PRIMARY KEY,
            default_model TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_user_model_preference_default_model CHECK (
                default_model IS NULL OR (
                    char_length(default_model) BETWEEN 1 AND 200
                    AND default_model !~ '[[:cntrl:]]'
                )
            )
        )
        """
    )


def downgrade() -> None:
    """Remove only the model pins introduced by this revision."""
    op.execute("DROP TABLE agent_user_model_preferences")
