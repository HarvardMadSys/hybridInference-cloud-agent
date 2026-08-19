"""Thread residency: a sandbox that outlives its attempts, fenced by generation.

HOT continuity (design §7, issue #57) decouples sandbox lifetime from attempt
lifetime: one hot sandbox hosts many turns with idle windows between them.
That creates objects the platform never had to account for — an idle sandbox
with no attempt to lease it — so it gets its own row and its own lease.

One row per thread, forever. The row is **never deleted** and ``generation``
only ever increments: a takeover bumps it atomically, and a residency ending
sets ``status='none'`` rather than removing the row. Deletion would reset the
counter, and a per-residency-generation credential handle (design §8) minted
under generation N must stay dead for the rest of the thread's life — a
re-established thread that restarted counting at 1 would resurrect it.

Attempt fencing guards event and result writes; this table's generation
guards heartbeats, sandbox management and credential resolution. Both are
required for the design's "double fencing" to be true.

Revision ID: 0009_thread_residency
Revises: 0008_event_client_seq
"""

from __future__ import annotations

from alembic import op

revision = "0009_thread_residency"
down_revision = "0008_event_client_seq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the residency ledger."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_thread_residencies (
            thread_id TEXT PRIMARY KEY,
            generation BIGINT NOT NULL DEFAULT 1,
            host TEXT NOT NULL,
            sandbox_id TEXT,
            runtime_id TEXT NOT NULL,
            runtime_version TEXT,
            status TEXT NOT NULL DEFAULT 'hot'
                CHECK (status IN ('hot', 'draining', 'none')),
            lease_expires_at TIMESTAMPTZ NOT NULL,
            last_checkpoint_turn_id TEXT,
            established_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # The reaper's scan and the eviction budget's count, off a full-table walk.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_thread_residencies_hot "
        "ON agent_thread_residencies(host, lease_expires_at) WHERE status = 'hot'"
    )


def downgrade() -> None:
    """Drop the residency ledger."""
    op.execute("DROP INDEX IF EXISTS idx_agent_thread_residencies_hot")
    op.execute("DROP TABLE IF EXISTS agent_thread_residencies")
