"""Record the identity issuer without guessing ownership of existing data."""

from alembic import op

revision = "0029_identity_namespace"
down_revision = "0028_task_titles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the single immutable-by-application namespace record."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_identity_namespace (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            gateway_issuer TEXT NOT NULL CHECK (length(gateway_issuer) > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    """Retain the issuer so a later upgrade cannot reassign historical owners."""
    pass
