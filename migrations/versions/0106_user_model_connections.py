"""Add owners' own OpenAI-compatible model connections.

A connection is private to its owner: its key is stored encrypted and never
read back. A turn names the connection it runs on beside its model, NULL being
the Gateway, so the same model id offered by the Gateway and by several
connections is never ambiguous. A deleted connection keeps its row, without its
key, so a turn that named it fails as "deleted" instead of falling back.

Revision ID: 0106_user_model_connections
Revises: 0105_job_event_usage_summary
"""

from alembic import op

revision = "0106_user_model_connections"
down_revision = "0105_job_event_usage_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the connection table and the nullable source columns beside each model."""
    op.execute(
        """
        CREATE TABLE agent_model_connections (
            id TEXT PRIMARY KEY CHECK (id ~ '^mconn_[0-9a-f]{32}$'),
            user_id TEXT NOT NULL,
            name TEXT NOT NULL CHECK (
                char_length(name) BETWEEN 1 AND 64 AND name !~ '[[:cntrl:]]'
            ),
            base_url TEXT NOT NULL CHECK (
                char_length(base_url) BETWEEN 9 AND 512 AND base_url LIKE 'https://%'
            ),
            protocols TEXT[] NOT NULL CHECK (
                cardinality(protocols) BETWEEN 1 AND 3
                AND protocols <@ ARRAY[
                    'chat_completions', 'responses', 'anthropic_messages'
                ]::text[]
            ),
            models TEXT[] NOT NULL CHECK (cardinality(models) BETWEEN 1 AND 64),
            api_key_ciphertext TEXT,
            credential_revision BIGINT NOT NULL DEFAULT 1 CHECK (credential_revision >= 1),
            -- Raised by every change a check result depends on: the key, the
            -- Base URL, the protocols and the models. A result is stored only
            -- against the revision it tested.
            config_revision BIGINT NOT NULL DEFAULT 1 CHECK (config_revision >= 1),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            last_check JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT ck_agent_model_connections_key CHECK (
                (deleted_at IS NULL) = (api_key_ciphertext IS NOT NULL)
            ),
            CONSTRAINT ck_agent_model_connections_deleted CHECK (
                deleted_at IS NULL OR NOT enabled
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_model_connections_owner_name
            ON agent_model_connections (user_id, lower(name))
            WHERE deleted_at IS NULL
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_model_connections_owner "
        "ON agent_model_connections (user_id, created_at)"
    )
    op.execute(
        """
        CREATE TABLE agent_model_connection_checks (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            connection_id TEXT NOT NULL
                REFERENCES agent_model_connections (id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_model_connection_checks_owner "
        "ON agent_model_connection_checks (user_id, created_at)"
    )
    # The scan-only copy of each connection key revision actually handed to a
    # sandbox group: written when a residency is bound, just before the key
    # reaches that group's CLI, and read only to refuse that group's trusted
    # snapshots and publications that carry it -- after the key was rotated or
    # the connection deleted too. It lives as long as the group's own record.
    # Execution paths never select these copies. Their separate cipher domain
    # prevents ciphertext substitution; the plaintext is still a real provider
    # key until its owner revokes it upstream.
    op.execute(
        """
        CREATE TABLE agent_model_connection_scan_keys (
            id BIGSERIAL PRIMARY KEY,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes (id) ON DELETE CASCADE,
            connection_id TEXT NOT NULL
                REFERENCES agent_model_connections (id) ON DELETE CASCADE,
            credential_revision BIGINT NOT NULL,
            thread_id TEXT NOT NULL,
            key_ciphertext TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_agent_model_connection_scan_keys
                UNIQUE (sandbox_id, connection_id, credential_revision)
        )
        """
    )
    # No foreign keys from turns: a delete must never rewrite a turn's source
    # to NULL, which would read as the Gateway.
    op.execute("ALTER TABLE agent_jobs ADD COLUMN model_connection_id TEXT")
    op.execute("ALTER TABLE agent_pending_messages ADD COLUMN model_connection_id TEXT")
    # The previous release prepares SELECT m.* inside a transaction. Adding
    # columns to memberships changes that result type and fails its next turn
    # with InvalidCachedStatementError during a rolling deployment. Keep the
    # new binding in its own table, fenced to one residency generation.
    op.execute(
        """
        CREATE TABLE agent_model_connection_residencies (
            thread_id TEXT PRIMARY KEY
                REFERENCES agent_thread_sandbox_memberships (thread_id) ON DELETE CASCADE,
            residency_generation BIGINT NOT NULL CHECK (residency_generation >= 1),
            connection_id TEXT NOT NULL REFERENCES agent_model_connections (id),
            credential_revision BIGINT NOT NULL CHECK (credential_revision >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_model_connection_residencies_connection "
        "ON agent_model_connection_residencies (connection_id)"
    )


def downgrade() -> None:
    """Drop the connections and every source column, once nothing persisted names one.

    Without the columns every turn reads as the Gateway's. Not only a queued or
    running turn: a finished conversation that ran on a connection would
    continue on the Gateway at its next message, a Fork or goal round would
    copy that, and a waiting message or a resident member would lose its
    source. So the downgrade refuses while any turn, waiting message or
    membership names a connection, or any connection exists at all. Removing
    them is a deliberate data decision for the operator, not a side effect of
    a schema rollback.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_model_connections)
               OR EXISTS (SELECT 1 FROM agent_jobs WHERE model_connection_id IS NOT NULL)
               OR EXISTS (
                   SELECT 1 FROM agent_pending_messages WHERE model_connection_id IS NOT NULL
               )
               OR EXISTS (
                   SELECT 1 FROM agent_model_connection_residencies
               )
            THEN
                RAISE EXCEPTION
                    'personal model connections or conversations that used one still exist';
            END IF;
        END
        $$
        """
    )
    op.execute("DROP TABLE agent_model_connection_residencies")
    op.execute("ALTER TABLE agent_pending_messages DROP COLUMN model_connection_id")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN model_connection_id")
    op.execute("DROP TABLE agent_model_connection_scan_keys")
    op.execute("DROP TABLE agent_model_connection_checks")
    op.execute("DROP TABLE agent_model_connections")
