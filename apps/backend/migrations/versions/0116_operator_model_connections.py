"""Add the models a deployment provides, and each owner's allowance on them.

An operator connection is the deployment's own inference endpoint, offered to
every admitted owner. Its key is stored encrypted and stays in the control
plane: a turn on one holds a residency placeholder, and the model relay
exchanges it on every request. Each model has a default daily request
allowance per owner, an owner may have one of their own, and the relay counts
accepted requests per owner, model and UTC day.

Only new tables. A turn names an operator connection in the same
``model_connection_id`` column an owner's connection uses (it has no foreign
key), by an id with its own prefix.

See ``docs/agents/specs/2026-11-12-platform-model-relay.zh-CN.md``.

Revision ID: 0116_operator_model_connections
Revises: 0115_mcp_retire_dead_locations
"""

from alembic import op

revision = "0116_operator_model_connections"
down_revision = "0115_mcp_retire_dead_locations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the connection, limit, usage and residency tables."""
    op.execute(
        """
        CREATE TABLE agent_operator_model_connections (
            id TEXT PRIMARY KEY CHECK (id ~ '^oconn_[0-9a-f]{32}$'),
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            models TEXT[] NOT NULL,
            default_model TEXT,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            api_key_ciphertext TEXT,
            end_user_header TEXT,
            user_concurrency INTEGER NOT NULL DEFAULT 4
                CHECK (user_concurrency BETWEEN 1 AND 64),
            model_checks JSONB NOT NULL DEFAULT '{}'::jsonb,
            config_revision BIGINT NOT NULL DEFAULT 1 CHECK (config_revision >= 1),
            credential_revision BIGINT NOT NULL DEFAULT 1 CHECK (credential_revision >= 1),
            key_refused_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            CHECK (deleted_at IS NOT NULL OR api_key_ciphertext IS NOT NULL),
            CHECK (deleted_at IS NULL OR NOT enabled)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_operator_model_connections_name "
        "ON agent_operator_model_connections (lower(name)) WHERE deleted_at IS NULL"
    )
    # At most one connection is where an owner with no history starts.
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_operator_model_connections_default "
        "ON agent_operator_model_connections ((TRUE)) WHERE is_default AND deleted_at IS NULL"
    )
    # A model's default allowance. NULL is unlimited; 0 offers the model only
    # to owners with an allowance of their own. A model with no row is offered
    # to nobody, and its connection cannot be enabled.
    op.execute(
        """
        CREATE TABLE agent_operator_model_limits (
            operator_connection_id TEXT NOT NULL
                REFERENCES agent_operator_model_connections (id),
            model TEXT NOT NULL,
            user_daily_requests INTEGER CHECK (user_daily_requests >= 0),
            global_concurrency INTEGER CHECK (global_concurrency >= 1),
            PRIMARY KEY (operator_connection_id, model)
        )
        """
    )
    # One owner's own allowance on one model; it replaces the model's default.
    op.execute(
        """
        CREATE TABLE agent_operator_model_user_limits (
            operator_connection_id TEXT NOT NULL
                REFERENCES agent_operator_model_connections (id),
            user_id TEXT NOT NULL,
            model TEXT NOT NULL,
            daily_requests INTEGER CHECK (daily_requests >= 0),
            PRIMARY KEY (operator_connection_id, user_id, model)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_operator_model_user_limits_user "
        "ON agent_operator_model_user_limits (user_id)"
    )
    # Requests the model relay accepted, per owner, model and UTC day.
    op.execute(
        """
        CREATE TABLE agent_operator_model_usage (
            operator_connection_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            model TEXT NOT NULL,
            day DATE NOT NULL,
            requests BIGINT NOT NULL DEFAULT 0 CHECK (requests >= 0),
            PRIMARY KEY (operator_connection_id, user_id, model, day)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_operator_model_usage_day "
        "ON agent_operator_model_usage (operator_connection_id, day)"
    )
    op.execute("CREATE INDEX idx_agent_operator_model_usage_user ON agent_operator_model_usage (user_id)")
    # Which revision of an operator connection a residency's process started
    # on. The twin of agent_model_connection_residencies, whose connection_id
    # references owners' connections.
    op.execute(
        """
        CREATE TABLE agent_operator_connection_residencies (
            thread_id TEXT PRIMARY KEY
                REFERENCES agent_thread_sandbox_memberships (thread_id) ON DELETE CASCADE,
            residency_generation BIGINT NOT NULL CHECK (residency_generation >= 1),
            operator_connection_id TEXT NOT NULL
                REFERENCES agent_operator_model_connections (id),
            credential_revision BIGINT NOT NULL CHECK (credential_revision >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_operator_connection_residencies_connection "
        "ON agent_operator_connection_residencies (operator_connection_id)"
    )


def downgrade() -> None:
    """Drop the tables, once no connection exists and no turn names one.

    The previous release reads a turn's ``oconn_`` source as an owner's
    connection it cannot find, so a conversation that ran on a provided model
    would be refused at its next message. Like ``0106``, removing them is a
    deliberate data decision for the operator, not a side effect of a schema
    rollback: the downgrade refuses while any connection, turn, waiting
    message or residency names one.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_operator_model_connections)
               OR EXISTS (
                   SELECT 1 FROM agent_jobs WHERE model_connection_id LIKE 'oconn\\_%'
               )
               OR EXISTS (
                   SELECT 1 FROM agent_pending_messages
                   WHERE model_connection_id LIKE 'oconn\\_%'
               )
               OR EXISTS (SELECT 1 FROM agent_operator_connection_residencies)
            THEN
                RAISE EXCEPTION
                    'provided model connections or conversations that used one still exist';
            END IF;
        END
        $$
        """
    )
    op.execute("DROP TABLE agent_operator_connection_residencies")
    op.execute("DROP TABLE agent_operator_model_usage")
    op.execute("DROP TABLE agent_operator_model_user_limits")
    op.execute("DROP TABLE agent_operator_model_limits")
    op.execute("DROP TABLE agent_operator_model_connections")
