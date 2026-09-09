"""Shared sandbox groups with independently resident chat memberships.

The restartable preview originally combined container lifecycle and one chat's
native session in ``agent_thread_sandboxes``.  Native fork needs a different
cardinality: one Docker container owns one workspace while several chats own
independent native sessions and live process generations inside it.

This migration is additive.  Existing rows are copied as ``legacy_1to1``
groups for audit and rolling-read compatibility; no old row is deleted or
silently upgraded to concurrent execution.  Only newly capability-gated
``shared_live`` groups use these tables as their lifecycle authority.

Revision ID: 0019_shared_live_sandboxes
Revises: 0017_restartable_chat_sandboxes
"""

from __future__ import annotations

from alembic import op

revision = "0019_shared_live_sandboxes"
down_revision = "0017_restartable_chat_sandboxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add group, membership, native-fork and publication-snapshot ledgers."""
    op.execute(
        """
        CREATE TABLE agent_sandboxes (
            id TEXT PRIMARY KEY,
            generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
            origin_thread_id TEXT NOT NULL UNIQUE
                REFERENCES agent_threads(id) ON DELETE RESTRICT,
            user_id TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'shared_live'
                CHECK (mode IN ('legacy_1to1', 'shared_live')),
            host TEXT NOT NULL,
            container_id TEXT UNIQUE,
            container_name TEXT NOT NULL UNIQUE,
            runtime_id TEXT NOT NULL,
            runtime_version TEXT,
            sandbox_image_digest TEXT,
            execution_profile TEXT NOT NULL,
            memory_limit TEXT NOT NULL DEFAULT '4g',
            cpu_limit TEXT NOT NULL DEFAULT '2',
            stop_timeout_seconds INTEGER NOT NULL DEFAULT 10
                CHECK (stop_timeout_seconds BETWEEN 1 AND 300),
            state TEXT NOT NULL DEFAULT 'provisioning'
                CHECK (state IN (
                    'provisioning', 'starting', 'running', 'stopping',
                    'stopped', 'stopped_dirty', 'lost'
                )),
            last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            idle_deadline TIMESTAMPTZ,
            idle_timeout_seconds DOUBLE PRECISION NOT NULL DEFAULT 1800
                CHECK (idle_timeout_seconds > 0),
            active_operation_count INTEGER NOT NULL DEFAULT 0
                CHECK (active_operation_count >= 0),
            resident_slot_capacity INTEGER NOT NULL DEFAULT 8
                CHECK (resident_slot_capacity BETWEEN 2 AND 128),
            workspace_epoch BIGINT NOT NULL DEFAULT 0 CHECK (workspace_epoch >= 0),
            snapshot_state TEXT NOT NULL DEFAULT 'quiescent'
                CHECK (snapshot_state IN (
                    'quiescent', 'pending', 'snapshotting', 'importing', 'dirty'
                )),
            published_base_sha TEXT,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_thread_sandbox_memberships (
            thread_id TEXT PRIMARY KEY REFERENCES agent_threads(id) ON DELETE CASCADE,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id) ON DELETE RESTRICT,
            native_session_id TEXT,
            native_state TEXT NOT NULL DEFAULT 'new'
                CHECK (native_state IN ('new', 'ready', 'fork_preparing', 'lost')),
            context_status TEXT NOT NULL DEFAULT 'new'
                CHECK (context_status IN ('new', 'live', 'restored', 'lost')),
            activation_generation BIGINT NOT NULL DEFAULT 0
                CHECK (activation_generation >= 0),
            process_generation BIGINT NOT NULL DEFAULT 0
                CHECK (process_generation >= 0),
            residency_generation BIGINT NOT NULL DEFAULT 0
                CHECK (residency_generation >= 0),
            resident_state TEXT NOT NULL DEFAULT 'stopped'
                CHECK (resident_state IN ('stopped', 'starting', 'live', 'restarting', 'lost')),
            resident_slot_id INTEGER CHECK (resident_slot_id >= 0),
            active_attempt_id BIGINT REFERENCES agent_attempts(id),
            admitted_job_id TEXT REFERENCES agent_jobs(id),
            admitted_at TIMESTAMPTZ,
            last_settled_job_id TEXT REFERENCES agent_jobs(id),
            forked_from_thread_id TEXT REFERENCES agent_threads(id),
            fork_anchor_job_id TEXT REFERENCES agent_jobs(id),
            fork_operation_id TEXT,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_agent_sandboxes_host_state ON agent_sandboxes(host, state)")
    op.execute(
        "CREATE INDEX idx_agent_sandboxes_idle "
        "ON agent_sandboxes(idle_deadline) "
        "WHERE state = 'running' AND active_operation_count = 0"
    )
    op.execute(
        "CREATE INDEX idx_agent_memberships_sandbox ON agent_thread_sandbox_memberships(sandbox_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_memberships_resident_slot "
        "ON agent_thread_sandbox_memberships(sandbox_id, resident_slot_id) "
        "WHERE resident_slot_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_memberships_native_session "
        "ON agent_thread_sandbox_memberships(sandbox_id, native_session_id) "
        "WHERE native_session_id IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE agent_session_forks (
            id TEXT PRIMARY KEY,
            generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
            user_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id) ON DELETE RESTRICT,
            sandbox_generation BIGINT NOT NULL CHECK (sandbox_generation > 0),
            container_id TEXT NOT NULL,
            runtime_id TEXT NOT NULL,
            runtime_version TEXT,
            source_thread_id TEXT NOT NULL
                REFERENCES agent_threads(id) ON DELETE RESTRICT,
            source_job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE RESTRICT,
            source_native_session_id TEXT NOT NULL,
            source_activation_generation BIGINT NOT NULL
                CHECK (source_activation_generation >= 0),
            -- Preallocated while the child is intentionally invisible.  The
            -- agent_threads row is created only by READY finalization, so this
            -- cannot be a foreign key during PREPARING/MATERIALIZED.
            target_thread_id TEXT NOT NULL UNIQUE,
            target_native_session_id TEXT,
            target_anchor_job_id TEXT REFERENCES agent_jobs(id),
            target_resident_slot_id INTEGER CHECK (target_resident_slot_id >= 0),
            source_residency_generation BIGINT CHECK (source_residency_generation > 0),
            target_residency_generation BIGINT CHECK (target_residency_generation > 0),
            source_process_generation BIGINT CHECK (source_process_generation > 0),
            target_process_generation BIGINT CHECK (target_process_generation > 0),
            previous_source_grant_id TEXT,
            previous_target_grant_id TEXT,
            state TEXT NOT NULL DEFAULT 'preparing'
                CHECK (state IN ('preparing', 'materialized', 'ready', 'failed')),
            host_receipt JSONB,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            UNIQUE (user_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_session_forks_source_active "
        "ON agent_session_forks(source_thread_id) "
        "WHERE state IN ('preparing', 'materialized')"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_session_forks_target_slot_active "
        "ON agent_session_forks(sandbox_id, target_resident_slot_id) "
        "WHERE state IN ('preparing', 'materialized')"
    )
    op.execute(
        "ALTER TABLE agent_thread_sandbox_memberships "
        "ADD CONSTRAINT fk_agent_membership_fork "
        "FOREIGN KEY (fork_operation_id) REFERENCES agent_session_forks(id)"
    )
    op.execute(
        """
        CREATE TABLE agent_native_session_edges (
            id BIGSERIAL PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE
                REFERENCES agent_session_forks(id) ON DELETE RESTRICT,
            sandbox_id TEXT NOT NULL
                REFERENCES agent_sandboxes(id) ON DELETE RESTRICT,
            source_thread_id TEXT NOT NULL
                REFERENCES agent_threads(id) ON DELETE RESTRICT,
            target_thread_id TEXT NOT NULL UNIQUE
                REFERENCES agent_threads(id) ON DELETE RESTRICT,
            source_native_session_id TEXT NOT NULL,
            target_native_session_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (source_native_session_id <> target_native_session_id),
            UNIQUE (sandbox_id, target_native_session_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE agent_sandbox_snapshots (
            id BIGSERIAL PRIMARY KEY,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes(id) ON DELETE CASCADE,
            workspace_epoch BIGINT NOT NULL CHECK (workspace_epoch > 0),
            anchor_job_id TEXT NOT NULL REFERENCES agent_jobs(id) ON DELETE RESTRICT,
            included_job_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            base_sha TEXT NOT NULL,
            content_sha256 TEXT,
            patch_content TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN (
                    'pending', 'capturing', 'captured', 'publishing',
                    'published', 'failed'
                )),
            published_commit_sha TEXT,
            published_pr_url TEXT,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            captured_at TIMESTAMPTZ,
            published_at TIMESTAMPTZ,
            UNIQUE (sandbox_id, workspace_epoch)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_sandbox_snapshots_pending "
        "ON agent_sandbox_snapshots(sandbox_id) WHERE state = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_sandbox_snapshots_capturing "
        "ON agent_sandbox_snapshots(sandbox_id) WHERE state = 'capturing'"
    )
    op.execute(
        "CREATE INDEX idx_agent_sandbox_snapshots_publish "
        "ON agent_sandbox_snapshots(state, updated_at)"
    )

    # Copy the unreleased retained preview into the new shape without changing
    # which table owns it.  These rows remain capability-off legacy groups;
    # production code reads/writes the old row until an explicit migration is
    # designed for the immutable container image it actually uses.
    op.execute(
        """
        INSERT INTO agent_sandboxes (
            id, generation, origin_thread_id, user_id, mode, host,
            container_id, container_name, runtime_id, runtime_version,
            sandbox_image_digest, execution_profile, state,
            last_activity_at, idle_deadline, last_error, created_at, updated_at
        )
        SELECT s.sandbox_id, s.generation, s.thread_id, t.user_id,
               'legacy_1to1', s.host, s.container_id, s.container_name,
               s.runtime_id, s.runtime_version, s.sandbox_image_digest,
               s.execution_profile, s.state, s.updated_at, s.idle_deadline,
               s.last_error, s.created_at, s.updated_at
        FROM agent_thread_sandboxes s
        JOIN agent_threads t ON t.id = s.thread_id
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO agent_thread_sandbox_memberships (
            thread_id, sandbox_id, native_session_id, native_state,
            context_status, activation_generation, process_generation,
            resident_state, active_attempt_id, admitted_job_id, admitted_at,
            last_settled_job_id, fork_operation_id, last_error,
            created_at, updated_at
        )
        SELECT s.thread_id, s.sandbox_id, s.native_session_id,
               CASE WHEN s.context_status = 'lost' THEN 'lost'
                    WHEN s.native_session_id IS NULL THEN 'new' ELSE 'ready' END,
               s.context_status, s.activation_generation, s.process_generation,
               CASE WHEN s.context_status = 'lost' THEN 'lost' ELSE 'stopped' END,
               s.active_attempt_id, s.admitted_job_id, s.admitted_at,
               s.last_settled_job_id, NULL, s.last_error,
               s.created_at, s.updated_at
        FROM agent_thread_sandboxes s
        ON CONFLICT (thread_id) DO NOTHING
        """
    )


def downgrade() -> None:
    """Remove the additive shared-live ledgers; legacy retained rows survive."""
    op.execute("DROP INDEX IF EXISTS idx_agent_sandbox_snapshots_publish")
    op.execute("DROP INDEX IF EXISTS idx_agent_sandbox_snapshots_capturing")
    op.execute("DROP INDEX IF EXISTS idx_agent_sandbox_snapshots_pending")
    op.execute("DROP TABLE IF EXISTS agent_sandbox_snapshots")
    op.execute("DROP TABLE IF EXISTS agent_native_session_edges")
    op.execute(
        "ALTER TABLE agent_thread_sandbox_memberships "
        "DROP CONSTRAINT IF EXISTS fk_agent_membership_fork"
    )
    op.execute("DROP INDEX IF EXISTS idx_agent_session_forks_source_active")
    op.execute("DROP INDEX IF EXISTS idx_agent_session_forks_target_slot_active")
    op.execute("DROP TABLE IF EXISTS agent_session_forks")
    op.execute("DROP INDEX IF EXISTS idx_agent_memberships_resident_slot")
    op.execute("DROP INDEX IF EXISTS idx_agent_memberships_native_session")
    op.execute("DROP INDEX IF EXISTS idx_agent_memberships_sandbox")
    op.execute("DROP INDEX IF EXISTS idx_agent_sandboxes_idle")
    op.execute("DROP INDEX IF EXISTS idx_agent_sandboxes_host_state")
    op.execute("DROP TABLE IF EXISTS agent_thread_sandbox_memberships")
    op.execute("DROP TABLE IF EXISTS agent_sandboxes")
