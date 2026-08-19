"""Record what actually executes work: runtime, image digest, CLI versions.

Two tables, one fact each, for two different lifetimes.

``agent_runner_hosts`` gains the host's *current* stamp, refreshed by every
heartbeat: which container runtime its sandboxes launch under
(``sandbox_runtime`` — ``''`` is the host positively attesting shared-kernel
runc, NULL means it has never said), the digest of the sandbox image it would
launch right now, and the CLI versions baked into that image, keyed by runtime
id. This is what the admin page shows and what future scheduling policy reads
— until now the control plane had no way to know whether "Kata" was a fact or
a README.

``agent_attempts`` gains a *copy* of that stamp taken at claim time. The host
row answers "what would run here today"; the attempt columns answer "what ran
this attempt", and those diverge the moment a host upgrades. Retries that must
reproduce behaviour, incident forensics ("which CLI version produced the blank
thread"), and the session-continuity compatibility check
(docs/specs/2026-08-17-session-resident-harness-threads-design.md, issue #54)
all ask the second question, so it must be durable on the attempt.

All columns are nullable and additive: rows written before this migration —
and heartbeats from runners that predate the fields — keep working unchanged.

Revision ID: 0007_version_stamps
Revises: 0006_per_user_mcp_connections
"""

from __future__ import annotations

from alembic import op

revision = "0007_version_stamps"
down_revision = "0006_per_user_mcp_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the host stamp columns and their per-attempt copies."""
    op.execute("ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS sandbox_runtime TEXT")
    op.execute(
        "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS sandbox_image_digest TEXT"
    )
    op.execute("ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS runtime_versions JSONB")
    op.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS sandbox_runtime TEXT")
    op.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS sandbox_image_digest TEXT")
    op.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS runtime_version TEXT")


def downgrade() -> None:
    """Drop the stamp columns."""
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS runtime_version")
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS sandbox_image_digest")
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS sandbox_runtime")
    op.execute("ALTER TABLE agent_runner_hosts DROP COLUMN IF EXISTS runtime_versions")
    op.execute("ALTER TABLE agent_runner_hosts DROP COLUMN IF EXISTS sandbox_image_digest")
    op.execute("ALTER TABLE agent_runner_hosts DROP COLUMN IF EXISTS sandbox_runtime")
