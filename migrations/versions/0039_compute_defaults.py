"""Store administrator defaults for compute-provider forms.

The first consumer is CloudLab: project, profile, duration and SSH user values
that otherwise have to be re-entered for every allocation. The document is
keyed by provider so another adapter can add defaults without another column.

Revision ID: 0039_compute_defaults
Revises: 0038_compute_deployment_profiles
"""

from alembic import op

revision = "0039_compute_defaults"
down_revision = "0038_compute_deployment_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add global provider defaults to the existing singleton policy row."""
    op.execute(
        """
        ALTER TABLE agent_runner_policy
            ADD COLUMN compute_defaults JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(compute_defaults) = 'object')
        """
    )


def downgrade() -> None:
    """Remove saved compute defaults; provider built-ins remain unchanged."""
    op.execute("ALTER TABLE agent_runner_policy DROP COLUMN IF EXISTS compute_defaults")
