"""Fold the residency lease into the membership row it always described.

A thread's residency — the generation-fenced lease that says "a live CLI
process for this chat holds a renewable credential" — lived in its own table
beside the membership row, and the two repeated each other: host, sandbox,
runtime and generation were held twice, and every writer that touched one
had to keep the other in step. ``agent_thread_residencies`` also had to exist
before the thread it named did, because a native fork established the
target's residency while the target was still being materialized, which is
why the table could never carry a foreign key to threads.

Both go away here. The lease columns move onto
``agent_thread_sandbox_memberships`` — status, expiry, grant, binding and the
two timestamps beside the ``residency_generation`` the row already carried —
and a fork creates its target thread and membership when it is prepared,
hidden until finalization by having no turns, so the target's residency has
a row from the start. A fork that fails before finalization leaves that
placeholder soft-deleted with its slot released, the same tombstone an
owner's delete leaves.

The fencing semantics do not change: the generation only ever increments, a
beat or release names the generation it holds, and a credential minted under
one generation stays dead after the next. What changes is that there is one
row per chat to say so.

Revision ID: 0004_residency_on_membership
Revises: 0003_thread_level_config
"""

from __future__ import annotations

from alembic import op

revision = "0004_residency_on_membership"
down_revision = "0003_thread_level_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Move the lease onto the membership, then drop the residency table."""
    op.execute(
        """
        ALTER TABLE agent_thread_sandbox_memberships
            ADD COLUMN residency_status TEXT NOT NULL DEFAULT 'none'
                CHECK (residency_status IN ('hot', 'none')),
            ADD COLUMN residency_lease_expires_at TIMESTAMPTZ,
            ADD COLUMN residency_grant_id TEXT,
            ADD COLUMN residency_binding_id TEXT,
            ADD COLUMN residency_established_at TIMESTAMPTZ,
            ADD COLUMN residency_last_active_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        UPDATE agent_thread_sandbox_memberships m
        SET residency_status = CASE WHEN r.status = 'hot' THEN 'hot' ELSE 'none' END,
            residency_lease_expires_at = r.lease_expires_at,
            residency_grant_id = r.grant_id,
            residency_binding_id = r.ledger_binding_id,
            residency_established_at = r.established_at,
            residency_last_active_at = r.last_active_at,
            residency_generation = GREATEST(m.residency_generation, r.generation)
        FROM agent_thread_residencies r
        WHERE r.thread_id = m.thread_id
        """
    )
    op.execute(
        """
        ALTER TABLE agent_thread_sandbox_memberships
            ADD CONSTRAINT ck_agent_memberships_hot_residency_has_lease
            CHECK (residency_status <> 'hot' OR residency_lease_expires_at IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_memberships_hot_residency
            ON agent_thread_sandbox_memberships (residency_lease_expires_at)
            WHERE residency_status = 'hot'
        """
    )
    op.execute("DROP TABLE agent_thread_residencies")
    # A fork's target thread exists from preparation now, so the operation
    # can name it the way it names its source.
    op.execute(
        """
        ALTER TABLE agent_session_forks
            ADD CONSTRAINT fk_agent_session_forks_target_thread_owner
            FOREIGN KEY (target_thread_id, user_id) REFERENCES agent_threads (id, user_id)
        """
    )


def downgrade() -> None:
    """Recreate the residency table from the membership columns."""
    op.execute(
        "ALTER TABLE agent_session_forks "
        "DROP CONSTRAINT IF EXISTS fk_agent_session_forks_target_thread_owner"
    )
    op.execute(
        """
        CREATE TABLE agent_thread_residencies (
            thread_id TEXT PRIMARY KEY,
            generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
            host TEXT NOT NULL,
            sandbox_id TEXT REFERENCES agent_sandboxes (id),
            runtime_id TEXT NOT NULL,
            runtime_version TEXT,
            status TEXT NOT NULL DEFAULT 'hot' CHECK (status IN ('hot', 'draining', 'none')),
            lease_expires_at TIMESTAMPTZ NOT NULL,
            grant_id TEXT,
            ledger_binding_id TEXT,
            established_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        INSERT INTO agent_thread_residencies
            (thread_id, generation, host, sandbox_id, runtime_id, runtime_version, status,
             lease_expires_at, grant_id, ledger_binding_id, established_at, last_active_at)
        SELECT m.thread_id, m.residency_generation, g.host, g.id, g.runtime_id,
               g.runtime_version, m.residency_status,
               COALESCE(m.residency_lease_expires_at, NOW()),
               m.residency_grant_id, m.residency_binding_id,
               COALESCE(m.residency_established_at, m.created_at),
               COALESCE(m.residency_last_active_at, m.updated_at)
        FROM agent_thread_sandbox_memberships m
        JOIN agent_sandboxes g ON g.id = m.sandbox_id
        WHERE m.residency_generation > 0
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_thread_residencies_hot
            ON agent_thread_residencies (host, lease_expires_at) WHERE status = 'hot'
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_agent_memberships_hot_residency")
    op.execute(
        """
        ALTER TABLE agent_thread_sandbox_memberships
            DROP CONSTRAINT IF EXISTS ck_agent_memberships_hot_residency_has_lease,
            DROP COLUMN residency_last_active_at,
            DROP COLUMN residency_established_at,
            DROP COLUMN residency_binding_id,
            DROP COLUMN residency_grant_id,
            DROP COLUMN residency_lease_expires_at,
            DROP COLUMN residency_status
        """
    )
