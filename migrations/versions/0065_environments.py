"""Persist reusable environment versions, frozen bindings and fenced test runs."""

from alembic import op

revision = "0065_environments"
down_revision = "0064_mcp_remembered_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add immutable configuration snapshots without changing legacy sessions."""
    op.execute("""
        CREATE TABLE agent_environments (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, repo TEXT,
            name TEXT NOT NULL, normalized_name TEXT NOT NULL,
            revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            copy_request_key TEXT, copy_request_fingerprint TEXT,
            UNIQUE(user_id, copy_request_key),
            archived_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (id, user_id)
        );
        CREATE UNIQUE INDEX agent_environment_names ON agent_environments
            (user_id, repo, normalized_name) WHERE repo IS NOT NULL AND archived_at IS NULL;
        CREATE UNIQUE INDEX agent_environment_globals ON agent_environments(user_id)
            WHERE repo IS NULL;
        CREATE TABLE agent_environment_versions (
            id TEXT PRIMARY KEY, environment_id TEXT NOT NULL REFERENCES agent_environments(id),
            revision BIGINT NOT NULL CHECK (revision > 0), document JSONB NOT NULL,
            changes JSONB NOT NULL DEFAULT '[]', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(environment_id, revision)
        );
        CREATE TABLE agent_environment_version_variables (
            version_id TEXT NOT NULL REFERENCES agent_environment_versions(id) ON DELETE CASCADE,
            name TEXT NOT NULL, value_ciphertext TEXT NOT NULL, secret BOOLEAN NOT NULL,
            phase TEXT NOT NULL CHECK (phase IN ('setup', 'runtime', 'both')),
            value_bytes INTEGER NOT NULL CHECK (value_bytes BETWEEN 0 AND 32768),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(version_id, name)
        );
        CREATE TABLE agent_repository_environment_defaults (
            user_id TEXT NOT NULL, repo TEXT NOT NULL, environment_id TEXT,
            revision BIGINT NOT NULL DEFAULT 1,
            PRIMARY KEY(user_id, repo),
            FOREIGN KEY(environment_id, user_id) REFERENCES agent_environments(id, user_id)
        );
        CREATE TABLE agent_environment_bindings (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, repo TEXT NOT NULL,
            environment_version_id TEXT REFERENCES agent_environment_versions(id),
            global_version_id TEXT REFERENCES agent_environment_versions(id),
            overrides JSONB NOT NULL DEFAULT '[]', setup_override TEXT,
            has_setup_override BOOLEAN NOT NULL DEFAULT FALSE,
            required BOOLEAN NOT NULL DEFAULT FALSE,
            runtime_revision BIGINT NOT NULL DEFAULT 0,
            runtime_variables JSONB, runtime_secret_history JSONB NOT NULL DEFAULT '[]',
            skip_start_after_job_id TEXT, skip_start_job_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(id, user_id)
        );
        CREATE FUNCTION agent_environment_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'environment versions are immutable'; END; $$;
        CREATE TRIGGER agent_environment_version_immutable BEFORE UPDATE ON agent_environment_versions
            FOR EACH ROW EXECUTE FUNCTION agent_environment_immutable();
        CREATE TRIGGER agent_environment_value_immutable BEFORE UPDATE ON agent_environment_version_variables
            FOR EACH ROW EXECUTE FUNCTION agent_environment_immutable();
        ALTER TABLE agent_threads ADD COLUMN environment_binding_id TEXT
            REFERENCES agent_environment_bindings(id);
        CREATE TABLE agent_environment_runs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, repo TEXT NOT NULL, base_sha TEXT NOT NULL,
            binding_id TEXT NOT NULL REFERENCES agent_environment_bindings(id),
            execution_profile TEXT NOT NULL DEFAULT 'trusted-v1' CHECK(execution_profile IN ('trusted-v1','untrusted-kata-v1')),
            state TEXT NOT NULL DEFAULT 'queued' CHECK
                (state IN ('queued','running','succeeded','failed','cancelled')),
            host TEXT, lease_generation BIGINT NOT NULL DEFAULT 0,
            lease_expires_at TIMESTAMPTZ, cleanup_complete BOOLEAN NOT NULL DEFAULT FALSE, cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
            image_digest TEXT, steps JSONB NOT NULL DEFAULT '[]',
            idempotency_key TEXT, fingerprint TEXT, detail TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), started_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
            UNIQUE(user_id, idempotency_key)
        );
        CREATE UNIQUE INDEX agent_environment_active_test ON agent_environment_runs(user_id)
            WHERE state IN ('queued','running');
        CREATE TABLE agent_environment_action_runs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES agent_threads(id),
            binding_id TEXT NOT NULL REFERENCES agent_environment_bindings(id), action_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, terminal_id TEXT, state TEXT NOT NULL DEFAULT 'starting',
            exit_code INTEGER, output TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(user_id, thread_id, idempotency_key)
        );
        CREATE TABLE agent_environment_preparation (
            sandbox_id TEXT PRIMARY KEY REFERENCES agent_sandboxes(id),
            binding_id TEXT NOT NULL REFERENCES agent_environment_bindings(id),
            container_id TEXT NOT NULL, boot_id TEXT NOT NULL,
            setup_done BOOLEAN NOT NULL DEFAULT FALSE, start_done BOOLEAN NOT NULL DEFAULT FALSE,
            steps JSONB NOT NULL DEFAULT '[]', updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX agent_environment_thread_binding ON agent_threads(environment_binding_id)
            WHERE environment_binding_id IS NOT NULL;
        CREATE INDEX agent_environment_run_binding ON agent_environment_runs(binding_id);
        CREATE INDEX agent_environment_action_binding ON agent_environment_action_runs(binding_id);
        CREATE INDEX agent_environment_preparation_binding ON agent_environment_preparation(binding_id);
        CREATE INDEX agent_environment_binding_version ON agent_environment_bindings(environment_version_id);
        CREATE INDEX agent_environment_binding_global ON agent_environment_bindings(global_version_id);
        CREATE INDEX agent_environment_binding_gc ON agent_environment_bindings(user_id,created_at);
        CREATE INDEX agent_environment_host_cleanup ON agent_environment_runs(host,lease_expires_at)
            WHERE NOT cleanup_complete;
        CREATE INDEX agent_environment_lease_expiry ON agent_environment_runs(lease_expires_at)
            WHERE state='running';
        CREATE INDEX agent_environment_archived_gc ON agent_environments(archived_at,user_id)
            WHERE archived_at IS NOT NULL;
    """)


def downgrade() -> None:
    """Refuse removal of configuration required by existing workspaces."""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_threads t JOIN agent_environment_bindings b ON b.id=t.environment_binding_id WHERE b.required)
               OR EXISTS (SELECT 1 FROM agent_environment_runs WHERE NOT cleanup_complete) THEN
                RAISE EXCEPTION 'environment bindings must be drained before downgrade';
            END IF;
        END $$;
        DROP TABLE agent_environment_preparation;
        DROP TABLE agent_environment_action_runs;
        DROP TABLE agent_environment_runs;
        DROP INDEX agent_environment_thread_binding;
        ALTER TABLE agent_threads DROP COLUMN environment_binding_id;
        DROP TABLE agent_environment_bindings;
        DROP TABLE agent_repository_environment_defaults;
        DROP TABLE agent_environment_version_variables;
        DROP TABLE agent_environment_versions;
        DROP FUNCTION agent_environment_immutable();
        DROP TABLE agent_environments;
    """)
