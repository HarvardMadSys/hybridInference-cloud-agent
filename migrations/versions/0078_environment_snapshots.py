"""Record installation snapshots that let a new workspace skip its setup script.

Revision ID: 0078_environment_snapshots
Revises: 0077_sandbox_usage_days

A snapshot is a container after its environment's setup, without the
repository's files, kept in S3 like a checkpoint. It is built by an isolated
environment run: a passing test, or a ``prebuild`` the control plane queues
when a task found none. A prebuild does not count as the owner's one test.

Bindings gain the setup fingerprint (an HMAC of the setup script and
setup-time variables) that decides which snapshot a workspace may start from.
Bindings made earlier have none and never start from a snapshot.

The environment and run a snapshot names are not foreign keys: environments
are archived and runs kept, never deleted, and earlier revisions' downgrades
drop both tables without knowing this one.
"""

from alembic import op

revision = "0078_environment_snapshots"
down_revision = "0077_sandbox_usage_days"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the snapshot table, the run purpose and the binding fingerprint."""
    op.execute(
        """
        ALTER TABLE agent_environment_bindings ADD COLUMN setup_fingerprint TEXT
            CHECK (setup_fingerprint ~ '^[0-9a-f]{64}$');
        ALTER TABLE agent_environment_runs
            ADD COLUMN purpose TEXT NOT NULL DEFAULT 'test'
                CHECK (purpose IN ('test', 'prebuild')),
            ADD COLUMN snapshot_id TEXT;
        DROP INDEX agent_environment_active_test;
        CREATE UNIQUE INDEX agent_environment_active_test ON agent_environment_runs(user_id)
            WHERE purpose = 'test' AND state IN ('queued', 'running');
        CREATE TABLE agent_environment_snapshots (
            id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
            user_id TEXT NOT NULL,
            environment_id TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK (revision > 0),
            setup_fingerprint TEXT NOT NULL CHECK (setup_fingerprint ~ '^[0-9a-f]{64}$'),
            run_id TEXT,
            state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN
                ('queued', 'building', 'ready', 'failed', 'superseded', 'deleting', 'deleted')),
            base_image TEXT,
            source_sha TEXT,
            lockfiles JSONB NOT NULL DEFAULT '{}',
            object_key TEXT,
            object_version_id TEXT,
            sha256 TEXT CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            size_bytes BIGINT CHECK (size_bytes > 0),
            new_layer_bytes BIGINT CHECK (new_layer_bytes >= 0),
            host TEXT,
            durations_ms JSONB NOT NULL DEFAULT '{}',
            detail TEXT,
            use_count BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ready_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            deleted_at TIMESTAMPTZ,
            CHECK (state <> 'ready' OR (object_key IS NOT NULL AND sha256 IS NOT NULL
                AND size_bytes IS NOT NULL AND base_image IS NOT NULL
                AND source_sha IS NOT NULL AND ready_at IS NOT NULL))
        );
        CREATE UNIQUE INDEX agent_environment_snapshot_pending
            ON agent_environment_snapshots(environment_id, setup_fingerprint)
            WHERE state IN ('queued', 'building');
        CREATE INDEX agent_environment_snapshot_ready
            ON agent_environment_snapshots(environment_id, setup_fingerprint, base_image, ready_at)
            WHERE state = 'ready';
        CREATE INDEX agent_environment_snapshot_owner
            ON agent_environment_snapshots(user_id, state);
        CREATE INDEX agent_environment_snapshot_cleanup
            ON agent_environment_snapshots(updated_at)
            WHERE state IN ('superseded', 'deleting');
        CREATE INDEX agent_environment_snapshot_run ON agent_environment_snapshots(run_id);
        """
    )


def downgrade() -> None:
    """Refuse while a prebuild runs or a stored snapshot is not yet deleted."""
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_environment_runs
                       WHERE purpose = 'prebuild' AND NOT cleanup_complete)
               OR EXISTS (SELECT 1 FROM agent_environment_snapshots
                          WHERE object_key IS NOT NULL AND state <> 'deleted') THEN
                RAISE EXCEPTION 'environment snapshots must be deleted before downgrade';
            END IF;
        END $$;
        DROP TABLE agent_environment_snapshots;
        DROP INDEX agent_environment_active_test;
        DELETE FROM agent_environment_runs WHERE purpose = 'prebuild';
        CREATE UNIQUE INDEX agent_environment_active_test ON agent_environment_runs(user_id)
            WHERE state IN ('queued', 'running');
        ALTER TABLE agent_environment_runs DROP COLUMN snapshot_id, DROP COLUMN purpose;
        ALTER TABLE agent_environment_bindings DROP COLUMN setup_fingerprint;
        """
    )
