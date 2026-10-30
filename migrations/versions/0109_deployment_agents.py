"""Deployment agents, their enrollment tokens, intents, reports and workflows.

A deployment agent runs on a self-managed machine, outside the database this
control plane uses, and pulls intents from here with its own credential.
These tables hold what the control plane knows about each agent (its public
key, role, last report), the intents administrators create for it, and the
one-time tokens that enroll a new agent. No secret value is stored: agent
credentials and enrollment tokens are SHA-256 digests, and sealed values in
an intent are ciphertext only the agent's private key opens.

Revision ID: 0109_deployment_agents
Revises: 0108_control_plane_instances
"""

from __future__ import annotations

from alembic import op

revision = "0109_deployment_agents"
down_revision = "0108_control_plane_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the agent, token and intent tables."""
    op.execute(
        """
        CREATE TABLE agent_deploy_agents (
            agent_id TEXT PRIMARY KEY CHECK (agent_id ~ '^[0-9a-f]{32}$'),
            name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128),
            role TEXT NOT NULL CHECK (role IN ('control-plane', 'host')),
            machine TEXT NOT NULL CHECK (char_length(machine) BETWEEN 1 AND 255),
            credential_hash TEXT NOT NULL UNIQUE,
            public_key TEXT NOT NULL CHECK (char_length(public_key) BETWEEN 32 AND 256),
            report JSONB NOT NULL DEFAULT '{}'::jsonb,
            enrolled_by TEXT NOT NULL,
            enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_report_at TIMESTAMPTZ,
            next_seq BIGINT NOT NULL DEFAULT 1,
            revoked_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_deploy_agents_name ON agent_deploy_agents (lower(name)) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE agent_deploy_enroll_tokens (
            token_hash TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK (role IN ('control-plane', 'host')),
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            agent_id TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_deploy_intents (
            intent_id TEXT PRIMARY KEY CHECK (intent_id ~ '^[0-9a-f]{32}$'),
            agent_id TEXT NOT NULL REFERENCES agent_deploy_agents (agent_id),
            seq BIGINT NOT NULL,
            operation TEXT NOT NULL CHECK (char_length(operation) BETWEEN 1 AND 64),
            params JSONB NOT NULL DEFAULT '{}'::jsonb,
            sealed JSONB NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
            origin TEXT NOT NULL DEFAULT 'intent' CHECK (origin IN ('intent', 'local')),
            state TEXT NOT NULL CHECK (state IN (
                'pending', 'delivered', 'running', 'succeeded', 'failed', 'rejected',
                'expired', 'cancelled'
            )),
            detail TEXT NOT NULL DEFAULT '' CHECK (char_length(detail) <= 2000),
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            workflow_id TEXT,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            delivered_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            UNIQUE (agent_id, seq),
            UNIQUE (agent_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_deploy_intents_agent ON agent_deploy_intents (agent_id, seq DESC)"
    )
    op.execute(
        "CREATE INDEX idx_agent_deploy_intents_workflow ON agent_deploy_intents (workflow_id) "
        "WHERE workflow_id IS NOT NULL"
    )
    # A workflow is a multi-step operation across agents and the control plane:
    # a release rollout, a key rotation, a database switch or migration. Its
    # step, cursor and evidence live here; each step's work is an intent.
    op.execute(
        """
        CREATE TABLE agent_deploy_workflows (
            workflow_id TEXT PRIMARY KEY CHECK (workflow_id ~ '^[0-9a-f]{32}$'),
            kind TEXT NOT NULL CHECK (kind IN (
                'rollout', 'root_key', 'admin_token', 'control_signing', 'sandbox_manager_token',
                'credential_key', 'database_connection', 'database_migration'
            )),
            state TEXT NOT NULL CHECK (state IN ('running', 'waiting', 'succeeded', 'failed', 'cancelled')),
            step TEXT NOT NULL,
            params JSONB NOT NULL DEFAULT '{}'::jsonb,
            data JSONB NOT NULL DEFAULT '{}'::jsonb,
            detail TEXT NOT NULL DEFAULT '' CHECK (char_length(detail) <= 2000),
            version BIGINT NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # At most one unfinished workflow of each kind: two rollouts or two
    # rotations of one key at once would race each other's steps.
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_deploy_workflows_active ON agent_deploy_workflows (kind) "
        "WHERE state IN ('running', 'waiting')"
    )


def downgrade() -> None:
    """Drop the tables; an older release has no deployment agents."""
    op.execute("DROP TABLE IF EXISTS agent_deploy_workflows")
    op.execute("DROP TABLE IF EXISTS agent_deploy_intents")
    op.execute("DROP TABLE IF EXISTS agent_deploy_enroll_tokens")
    op.execute("DROP TABLE IF EXISTS agent_deploy_agents")
