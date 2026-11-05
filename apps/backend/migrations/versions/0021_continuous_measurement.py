"""Allow Admin collection to run until explicitly disabled.

Revision ID: 0021_continuous_measurement
Revises: 0020_goal_controller

A null expiry is the immutable identity of an untimed Admin run. Stop still
records lifecycle/stopped_at rather than rewriting that identity.
"""

from alembic import op

revision = "0021_continuous_measurement"
down_revision = "0020_goal_controller"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allow explicit untimed runs without changing existing expiries."""
    op.execute("ALTER TABLE agent_operation_profiling_runs ALTER COLUMN expires_at DROP NOT NULL")


def downgrade() -> None:
    """Refuse to invent an expiry for existing continuous run manifests."""
    op.execute("ALTER TABLE agent_operation_profiling_runs ALTER COLUMN expires_at SET NOT NULL")
