"""Add owner-authored display names and profile pictures.

Revision ID: 0063_user_profiles
Revises: 0062_native_session_locator
"""

from alembic import op

revision = "0063_user_profiles"
down_revision = "0062_native_session_locator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the Agent-owned display fields without storing identity claims."""
    op.execute(
        """
        CREATE TABLE agent_user_profiles (
            user_id TEXT PRIMARY KEY,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            image_data_url TEXT,
            revision BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_user_profile_first_name CHECK (char_length(first_name) <= 100),
            CONSTRAINT ck_user_profile_last_name CHECK (char_length(last_name) <= 100),
            CONSTRAINT ck_user_profile_revision CHECK (revision >= 0),
            CONSTRAINT ck_user_profile_image CHECK (
                image_data_url IS NULL OR (
                    image_data_url LIKE 'data:image/png;base64,%'
                    AND octet_length(image_data_url) <= 2800000
                )
            )
        )
        """
    )


def downgrade() -> None:
    """Remove only the display profiles introduced by this revision."""
    op.execute("DROP TABLE agent_user_profiles")
