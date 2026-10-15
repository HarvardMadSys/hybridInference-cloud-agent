"""Let an owner register SSH servers and enable them per chat.

Revision ID: 0080_user_ssh_servers
Revises: 0079_teams

A server holds the platform-generated key pair that logs in to it, the address
and host key confirmed when it was checked, and whether it is ready to use.
A chat enables servers through bindings; turning one off keeps the row
disabled rather than deleting it, as MCP bindings do. Events record who opened
which server to which chat. The revision sequence orders every SSH state the
control plane sends a host, so a late, older one cannot reopen what a newer one
closed. See ``docs/agents/specs/2026-10-15-user-ssh-servers-design.zh-CN.md``.
"""

from alembic import op

revision = "0080_user_ssh_servers"
down_revision = "0079_teams"
branch_labels = None
depends_on = None

_EVENT_KINDS = (
    "'created', 'updated', 'scanned', 'verified', 'check_failed', "
    "'enabled', 'disabled', 'rekeyed', 'archived'"
)


def upgrade() -> None:
    """Create the servers, their per-chat bindings, their events and the revision sequence."""
    op.execute("CREATE SEQUENCE agent_ssh_access_revision_seq")
    op.execute(
        """
        CREATE TABLE agent_ssh_servers (
            id UUID PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL CHECK (name ~ '^[a-z][a-z0-9-]{1,31}$'),
            description TEXT NOT NULL DEFAULT ''
                CHECK (char_length(description) <= 200),
            address TEXT NOT NULL CHECK (char_length(address) BETWEEN 1 AND 253),
            port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
            username TEXT NOT NULL CHECK (char_length(username) BETWEEN 1 AND 32),
            pinned_ipv4 TEXT,
            host_key_type TEXT,
            host_key TEXT,
            scanned_ipv4 TEXT,
            scanned_host_keys JSONB,
            public_key TEXT NOT NULL,
            private_key_ciphertext TEXT,
            pending_public_key TEXT,
            pending_private_key_ciphertext TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
            last_check_at TIMESTAMPTZ,
            last_check_error TEXT,
            revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at TIMESTAMPTZ,
            CONSTRAINT ck_agent_ssh_servers_ready CHECK (
                status <> 'ready'
                OR (
                    pinned_ipv4 IS NOT NULL
                    AND host_key_type IS NOT NULL
                    AND host_key IS NOT NULL
                    AND private_key_ciphertext IS NOT NULL
                )
            ),
            CONSTRAINT ck_agent_ssh_servers_archived CHECK (
                archived_at IS NULL
                OR (
                    status = 'pending'
                    AND private_key_ciphertext IS NULL
                    AND pending_private_key_ciphertext IS NULL
                )
            ),
            CONSTRAINT ck_agent_ssh_servers_pending_key CHECK (
                (pending_public_key IS NULL) = (pending_private_key_ciphertext IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_ssh_servers_owner_name
            ON agent_ssh_servers (user_id, name)
            WHERE archived_at IS NULL
        """
    )
    op.execute("CREATE INDEX idx_agent_ssh_servers_owner ON agent_ssh_servers (user_id, created_at)")
    op.execute(
        """
        CREATE TABLE agent_ssh_thread_bindings (
            thread_id TEXT NOT NULL REFERENCES agent_threads (id) ON DELETE CASCADE,
            server_id UUID NOT NULL REFERENCES agent_ssh_servers (id) ON DELETE CASCADE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, server_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_ssh_thread_bindings_server ON agent_ssh_thread_bindings (server_id)"
    )
    op.execute(
        f"""
        CREATE TABLE agent_ssh_server_events (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            server_id UUID NOT NULL REFERENCES agent_ssh_servers (id) ON DELETE CASCADE,
            thread_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ({_EVENT_KINDS})),
            detail JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_ssh_server_events_server
            ON agent_ssh_server_events (server_id, created_at)
        """
    )


def downgrade() -> None:
    """Drop the SSH tables; a host keeps nothing it cannot rebuild from a newer state."""
    op.execute("DROP TABLE agent_ssh_server_events")
    op.execute("DROP TABLE agent_ssh_thread_bindings")
    op.execute("DROP TABLE agent_ssh_servers")
    op.execute("DROP SEQUENCE agent_ssh_access_revision_seq")
