"""Make isolation policy server-owned and stamp the claim-time kernel probe.

``agent_jobs.execution_profile`` is the immutable minimum boundary for the
thread turn. Existing rows become ``trusted-v1``; deployments opt new root
threads into ``untrusted-kata-v1`` through control-plane configuration, and
follow-ups/forks inherit their source profile.

The attempt records the host and guest kernels reported by the exact runner
replica that claimed it. Together with the runtime/image/version columns from
0007 this is the durable claim-time attestation; the mutable host heartbeat row
is deliberately not used to decide or describe what one replica ran.

Revision ID: 0015_execution_profiles
Revises: 0014_reasoning_effort
"""

from __future__ import annotations

from alembic import op

revision = "0015_execution_profiles"
down_revision = "0014_reasoning_effort"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the job policy and per-attempt kernel attestation."""
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS execution_profile TEXT "
        "NOT NULL DEFAULT 'trusted-v1'"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'agent_jobs_execution_profile_check' "
        "AND conrelid = 'agent_jobs'::regclass) THEN "
        "ALTER TABLE agent_jobs ADD CONSTRAINT agent_jobs_execution_profile_check "
        "CHECK (execution_profile IN ('trusted-v1', 'untrusted-kata-v1')); "
        "END IF; END $$"
    )
    op.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS host_kernel TEXT")
    op.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS guest_kernel TEXT")


def downgrade() -> None:
    """Drop the execution profile and kernel attestation."""
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS guest_kernel")
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS host_kernel")
    op.execute(
        "ALTER TABLE agent_jobs DROP CONSTRAINT IF EXISTS agent_jobs_execution_profile_check"
    )
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS execution_profile")
