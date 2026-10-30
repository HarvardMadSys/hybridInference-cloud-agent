"""Credential re-encryption, host credential renewal, maintenance state, tunnel key workflows.

* Environment version values stay immutable to every ordinary write. A
  credential key rotation may rewrite only ``value_ciphertext`` (the same
  value under the new key; the application decrypts and re-encrypts it) and
  only inside a transaction that set ``cloud_agent.credential_reencryption``.
* A host renews its credential without re-enrolling: the renewed credential's
  digest waits as ``pending`` until the host presents it, the replaced one
  stays accepted as ``previous`` for a short grace, and ``credential_key_id``
  names the signing key that minted the current one (empty: unknown, from
  before this revision).
* ``agent_maintenance`` holds the one maintenance state every control-plane
  process reads: while a database move runs, writes are refused with a clear
  answer instead of failing on a read-only database.
* ``tunnel_keys`` joins the workflow kinds.

Revision ID: 0112_rotation_closure
Revises: 0111_database_probes
"""

from __future__ import annotations

from alembic import op

revision = "0112_rotation_closure"
down_revision = "0111_database_probes"
branch_labels = None
depends_on = None

_KINDS = (
    "'rollout', 'root_key', 'admin_token', 'control_signing', 'sandbox_manager_token', "
    "'credential_key', 'database_connection', 'database_migration'"
)


def upgrade() -> None:
    """Add the columns, the table, the narrowed trigger and the workflow kind."""
    op.execute(
        """
        CREATE FUNCTION agent_environment_value_reencrypt_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF current_setting('cloud_agent.credential_reencryption', true) = 'on'
               AND NEW.version_id = OLD.version_id AND NEW.name = OLD.name
               AND NEW.secret = OLD.secret AND NEW.phase = OLD.phase
               AND NEW.value_bytes = OLD.value_bytes
               AND NEW.value_ciphertext IS DISTINCT FROM OLD.value_ciphertext THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'environment versions are immutable';
        END; $$;
        DROP TRIGGER agent_environment_value_immutable ON agent_environment_version_variables;
        CREATE TRIGGER agent_environment_value_immutable
            BEFORE UPDATE ON agent_environment_version_variables
            FOR EACH ROW EXECUTE FUNCTION agent_environment_value_reencrypt_only();
        """
    )
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            ADD COLUMN credential_key_id TEXT,
            ADD COLUMN pending_credential_hash TEXT,
            ADD COLUMN pending_credential_key_id TEXT,
            ADD COLUMN previous_credential_hash TEXT,
            ADD COLUMN previous_credential_expires_at TIMESTAMPTZ,
            ADD COLUMN credential_renewal_requested BOOLEAN NOT NULL DEFAULT FALSE,
            ADD CONSTRAINT ck_agent_runner_hosts_previous_credential_expires
                CHECK ((previous_credential_hash IS NULL) = (previous_credential_expires_at IS NULL)),
            ADD CONSTRAINT ck_agent_runner_hosts_pending_credential_key
                CHECK ((pending_credential_hash IS NULL) = (pending_credential_key_id IS NULL))
        """
    )
    op.execute(
        """
        CREATE TABLE agent_maintenance (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            state TEXT NOT NULL CHECK (state IN ('migrating', 'completed', 'aborted')),
            kind TEXT NOT NULL CHECK (kind IN ('database_migration')),
            workflow_id TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '' CHECK (char_length(detail) <= 2000),
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "ALTER TABLE agent_deploy_workflows DROP CONSTRAINT agent_deploy_workflows_kind_check"
    )
    op.execute(
        "ALTER TABLE agent_deploy_workflows ADD CONSTRAINT agent_deploy_workflows_kind_check "
        f"CHECK (kind IN ({_KINDS}, 'tunnel_keys'))"
    )


def downgrade() -> None:
    """Restore the previous trigger, columns and kinds."""
    op.execute("DELETE FROM agent_deploy_workflows WHERE kind = 'tunnel_keys'")
    op.execute(
        "ALTER TABLE agent_deploy_workflows DROP CONSTRAINT agent_deploy_workflows_kind_check"
    )
    op.execute(
        "ALTER TABLE agent_deploy_workflows ADD CONSTRAINT agent_deploy_workflows_kind_check "
        f"CHECK (kind IN ({_KINDS}))"
    )
    op.execute("DROP TABLE IF EXISTS agent_maintenance")
    op.execute(
        """
        ALTER TABLE agent_runner_hosts
            DROP CONSTRAINT IF EXISTS ck_agent_runner_hosts_pending_credential_key,
            DROP CONSTRAINT IF EXISTS ck_agent_runner_hosts_previous_credential_expires,
            DROP COLUMN IF EXISTS credential_renewal_requested,
            DROP COLUMN IF EXISTS previous_credential_expires_at,
            DROP COLUMN IF EXISTS previous_credential_hash,
            DROP COLUMN IF EXISTS pending_credential_key_id,
            DROP COLUMN IF EXISTS pending_credential_hash,
            DROP COLUMN IF EXISTS credential_key_id
        """
    )
    op.execute(
        """
        DROP TRIGGER agent_environment_value_immutable ON agent_environment_version_variables;
        CREATE TRIGGER agent_environment_value_immutable
            BEFORE UPDATE ON agent_environment_version_variables
            FOR EACH ROW EXECUTE FUNCTION agent_environment_immutable();
        DROP FUNCTION agent_environment_value_reencrypt_only();
        """
    )
