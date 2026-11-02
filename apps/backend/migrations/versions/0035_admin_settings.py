"""Persist deployment settings managed by the administrator Settings page.

Secrets are encrypted by the application before entering the values document.
The singleton is initialized by startup, which imports existing deployment
values only once; a later deployment cannot overwrite administrator changes.
"""

from __future__ import annotations

from alembic import op

revision = "0035_admin_settings"
down_revision = "0034_sandbox_resource_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the singleton settings document."""
    op.execute(
        """
        CREATE TABLE agent_settings (
            id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
            revision BIGINT NOT NULL CHECK (revision >= 1),
            "values" JSONB NOT NULL CHECK (jsonb_typeof("values") = 'object'),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    """Remove persisted administrator settings."""
    op.execute("DROP TABLE agent_settings")
