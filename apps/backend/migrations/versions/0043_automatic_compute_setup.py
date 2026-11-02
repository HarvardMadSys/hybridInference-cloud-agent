"""Reserve one immutable generated profile per setup request and route slot."""

from __future__ import annotations

from alembic import op

revision = "0043_automatic_compute_setup"
down_revision = "0042_sandbox_archive_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Fence concurrent setup retries across API replicas at durable insertion."""
    op.execute("""
        CREATE UNIQUE INDEX idx_compute_automatic_setup_slot
        ON agent_compute_deployment_profiles (
            (configuration #>> '{automatic_setup,request_id}'),
            (configuration #>> '{automatic_setup,slot}')
        )
        WHERE configuration ? 'automatic_setup';
    """)


def downgrade() -> None:
    """Remove the reservation index without changing stored release inputs."""
    op.execute("DROP INDEX idx_compute_automatic_setup_slot")
