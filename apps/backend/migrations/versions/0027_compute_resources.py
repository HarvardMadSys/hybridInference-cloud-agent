"""Durable compute inventory, operations, and independent admission restrictions."""

from __future__ import annotations

from alembic import op

revision = "0027_compute_resources"
down_revision = "0026_fork_helper_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep provider resources separate from the existing execution pool."""
    op.execute("""
        CREATE TABLE agent_compute_connections (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, provider_id TEXT NOT NULL,
            configuration JSONB NOT NULL DEFAULT '{}', secret_ciphertext TEXT NOT NULL,
            masked_summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'unverified',
            revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0), observed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE agent_compute_allocations (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, provider_id TEXT NOT NULL,
            connection_id TEXT REFERENCES agent_compute_connections(id),
            ssh_connection_id TEXT REFERENCES agent_compute_connections(id),
            external_id TEXT, mode TEXT NOT NULL CHECK (mode IN ('attach', 'provision')),
            management_mode TEXT NOT NULL DEFAULT 'observe' CHECK (management_mode IN ('observe','manage')),
            configuration JSONB NOT NULL DEFAULT '{}', deployment_profile JSONB NOT NULL DEFAULT '{}',
            policy JSONB NOT NULL DEFAULT '{}', revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
            desired_state TEXT NOT NULL DEFAULT 'present' CHECK (desired_state IN ('present','absent')),
            observed_state TEXT NOT NULL DEFAULT 'unknown', provider_state TEXT,
            expires_at TIMESTAMPTZ, expiration_known BOOLEAN NOT NULL DEFAULT FALSE,
            lease_managed BOOLEAN NOT NULL DEFAULT FALSE,
            release_supported BOOLEAN NOT NULL DEFAULT FALSE,
            observed_at TIMESTAMPTZ, observation_error TEXT,
            scope_complete BOOLEAN NOT NULL DEFAULT FALSE,
            inventory_verified_at TIMESTAMPTZ, inventory_verified_nodes JSONB NOT NULL DEFAULT '[]',
            release_committed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (provider_id, external_id)
        );
        CREATE UNIQUE INDEX idx_compute_allocation_deployment_profile
            ON agent_compute_allocations ((deployment_profile->>'id'))
            WHERE deployment_profile ? 'id' AND observed_state NOT IN ('terminated','absent');
        CREATE TABLE agent_compute_nodes (
            id TEXT PRIMARY KEY, allocation_id TEXT NOT NULL REFERENCES agent_compute_allocations(id),
            provider_node_id TEXT NOT NULL, instance_identity TEXT NOT NULL,
            name TEXT NOT NULL, connection JSONB NOT NULL DEFAULT '{}',
            managed BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (allocation_id, provider_node_id), UNIQUE (allocation_id, instance_identity)
        );
        ALTER TABLE agent_runner_hosts ADD COLUMN compute_node_id TEXT REFERENCES agent_compute_nodes(id);
        CREATE INDEX idx_runner_host_compute_node ON agent_runner_hosts(compute_node_id);
        CREATE TABLE agent_compute_operations (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
            kind TEXT NOT NULL, allocation_id TEXT REFERENCES agent_compute_allocations(id),
            parameters JSONB NOT NULL DEFAULT '{}', requested_by TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','running','waiting','blocked','succeeded','failed','unknown','cancelled')),
            step TEXT NOT NULL DEFAULT 'pending', message TEXT,
            result JSONB NOT NULL DEFAULT '{}', generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
            worker_id TEXT, lease_expires_at TIMESTAMPTZ, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        );
        CREATE INDEX idx_compute_operations_due ON agent_compute_operations(next_attempt_at)
            WHERE state IN ('pending','running','waiting');
        CREATE TABLE agent_host_admission_holds (
            host TEXT NOT NULL REFERENCES agent_runner_hosts(host) ON DELETE CASCADE,
            allocation_id TEXT NOT NULL REFERENCES agent_compute_allocations(id),
            purpose TEXT NOT NULL CHECK (purpose IN ('bootstrap','lease_deadline','retirement')),
            source_id TEXT NOT NULL,
            scope TEXT NOT NULL CHECK (scope IN ('new_groups','all_claims')),
            revision BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (host, purpose, source_id)
        );
    """)


def downgrade() -> None:
    """Remove compute bookkeeping without destroying any provider resource."""
    op.execute("""
        DROP TABLE agent_host_admission_holds;
        DROP TABLE agent_compute_operations;
        ALTER TABLE agent_runner_hosts DROP COLUMN compute_node_id;
        DROP TABLE agent_compute_nodes;
        DROP TABLE agent_compute_allocations;
        DROP TABLE agent_compute_connections;
    """)
