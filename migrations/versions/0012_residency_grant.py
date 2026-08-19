"""Link each residency to the inference grant that lives and dies with it.

The §14 amendment (thread-level attribution) makes the resident credential an
ordinary grant with a residency-shaped lifecycle: minted at establish, renewed
on beats, revoked at release, lapse and takeover. The link is the only thing
that can renew or revoke it later, so it lives on the residency row — the same
reason attempts carry their grant ids.

Revision ID: 0012_residency_grant
Revises: 0011_warm_native_mode
"""

from __future__ import annotations

from alembic import op

revision = "0012_residency_grant"
down_revision = "0011_warm_native_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the grant link."""
    op.execute("ALTER TABLE agent_thread_residencies ADD COLUMN IF NOT EXISTS grant_id TEXT")


def downgrade() -> None:
    """Drop the grant link. Live grants die at their TTL; nothing dangles."""
    op.execute("ALTER TABLE agent_thread_residencies DROP COLUMN IF EXISTS grant_id")
