"""Replace the provider catalogue with user-defined MCP connections.

``agent_mcp_connections`` was one row per user per *catalogue entry*: the
``server`` column named a provider we had reviewed, ``UNIQUE(user_id, server)``
enforced one account each, and the row held a bearer token. Everything a user
could connect had to be added to ``mcp_catalogue.py`` first.

This revision makes the connection the thing itself. A row now carries its own
transport, execution location, endpoint or launch spec and authentication
profile, so a user can add any standard MCP server; the catalogue becomes
optional templates rather than a gate. The pieces that made the old shape
unusable for that are removed deliberately:

- ``UNIQUE(user_id, server)`` goes, because a person may connect the same
  service twice under different accounts and the two must not be one row.
- ``server`` becomes ``template``, because it described which reviewed entry a
  row came from and stopped describing what the connection *is*.
- the credential columns stay on this table, and gain the fields a refresh
  needs. The design allows a separate credential table; keeping them together
  means the version that fences a decision and the secret it fences are read in
  one row rather than two that can disagree.

Four new tables carry what a connection cannot: the discovery result, a
conversation's use of it, one call held for a decision, and the user-run
connectors that serve private and stdio servers.

``agent_threads.mcp_servers`` is migrated, then emptied and constrained to stay
empty. The old API still projects this column while the new host is deploying.
An inert column keeps ordinary conversation reads/writes working without leaving
a second authorization path that no version fence covers.
Each legacy entry is mapped to the user's connection for that provider when
there is exactly one, and quarantined in ``agent_mcp_legacy_bindings`` when
there is not. A mapped binding is created with ``selection_pending`` set, so a
session that predates this revision keeps working while every tool it calls is
confirmed by its owner once.

Revision ID: 0008_generic_mcp_connections
Revises: 0007_attempt_turn_resources
"""

from __future__ import annotations

from alembic import op

revision = "0008_generic_mcp_connections"
down_revision = "0007_attempt_turn_resources"
branch_labels = None
depends_on = None

_TRANSPORTS = "('streamable-http', 'http-sse', 'stdio')"
_LOCATIONS = "('remote', 'managed-worker', 'user-connector')"
_AUTH_KINDS = "('none', 'static', 'oauth')"
_STATUSES = "('draft', 'active', 'error', 'disabled')"
_ERROR_KINDS = (
    "('none', 'unreachable', 'blocked_target', 'needs_authorization', "
    "'client_registration_required', 'launch_failed', 'protocol_incompatible', "
    "'partial_capabilities')"
)
_INVOCATION_STATUSES = (
    "('pending', 'approved', 'denied', 'expired', 'dispatching', 'completed', "
    "'failed', 'outcome_unknown')"
)
_CONNECTOR_STATUSES = "('pending', 'online', 'offline', 'revoked')"


def upgrade() -> None:
    """Widen the connection row, add the supporting tables, move the old list."""
    _widen_connections()
    _create_connectors()
    _create_capabilities()
    _create_oauth_flows()
    _create_thread_bindings()
    _create_invocations()
    _create_transport_sessions()
    _migrate_thread_server_names()


def _widen_connections() -> None:
    """Turn the catalogue row into a self-describing connection."""
    # A second connection to the same service is the point of the revision.
    op.execute("ALTER TABLE agent_mcp_connections DROP CONSTRAINT uq_agent_mcp_connections_server")
    # ``server`` named a catalogue entry. Renamed rather than dropped so the
    # existing rows keep their provenance and the upgrade is data-preserving.
    op.execute("ALTER TABLE agent_mcp_connections RENAME COLUMN server TO template")
    op.execute("ALTER TABLE agent_mcp_connections ALTER COLUMN template DROP NOT NULL")
    # The old composer and Integrations API still SELECT/ORDER BY server.
    # Never expose a new connection through their provider-name authorizer.
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN server TEXT "
        "GENERATED ALWAYS AS (NULL::text) STORED"
    )
    # An unauthenticated or connector-local server has no platform-held secret.
    op.execute("ALTER TABLE agent_mcp_connections ALTER COLUMN access_token_ciphertext DROP NOT NULL")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN label TEXT")
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN transport TEXT NOT NULL "
        f"DEFAULT 'streamable-http' CHECK (transport IN {_TRANSPORTS})"
    )
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN location TEXT NOT NULL "
        f"DEFAULT 'remote' CHECK (location IN {_LOCATIONS})"
    )
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN url TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN launch_spec JSONB")
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN auth_kind TEXT NOT NULL "
        f"DEFAULT 'none' CHECK (auth_kind IN {_AUTH_KINDS})"
    )
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN auth_config JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN connector_id BIGINT")
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN status TEXT NOT NULL "
        f"DEFAULT 'draft' CHECK (status IN {_STATUSES})"
    )
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD COLUMN error_kind TEXT NOT NULL "
        f"DEFAULT 'none' CHECK (error_kind IN {_ERROR_KINDS})"
    )
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN error_detail TEXT")
    # Refresh material. The issuer and resource are stored rather than
    # re-discovered so a refresh works when the metadata endpoint is briefly
    # unreachable, and so a token cannot be refreshed against a *different*
    # authorization server than the one that issued it.
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN client_secret_ciphertext TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN issuer TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN resource TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN scopes TEXT[]")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN refresh_rotates BOOLEAN NOT NULL DEFAULT FALSE")
    # The endpoints a refresh and a revocation need. Recorded rather than
    # re-discovered so a refresh works when the metadata endpoint is briefly
    # unreachable, and so the token cannot be refreshed against a *different*
    # authorization server than the one that issued it.
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN token_endpoint TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN revocation_endpoint TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN registration_endpoint TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN client_auth_method TEXT")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN registration TEXT")
    # The five versions a decision is fenced against.
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN config_revision BIGINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN authorization_epoch BIGINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN oauth_generation BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN credential_version BIGINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN capability_revision BIGINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN last_checked_at TIMESTAMPTZ")
    # One service's patience is not every service's limit: a repository search
    # and a single-record fetch are not the same wait, and a platform-wide
    # constant makes the slower one fail and the faster one wait. NULL means the
    # relay's default, which is what every existing row gets.
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN request_timeout_s DOUBLE PRECISION")
    op.execute("ALTER TABLE agent_mcp_connections ADD COLUMN deleted_at TIMESTAMPTZ")

    # Existing rows: a reviewed provider with a bearer token, which is exactly
    # the ``static`` profile. Their label comes from the account they were
    # connected with, falling back to the provider name they were made from.
    op.execute("UPDATE agent_mcp_connections SET label = COALESCE(NULLIF(account_label, ''), template)")
    op.execute("UPDATE agent_mcp_connections SET transport = 'streamable-http', location = 'remote'")
    op.execute(
        "UPDATE agent_mcp_connections SET url = CASE template "
        "WHEN 'github' THEN 'https://api.githubcopilot.com/mcp/readonly' ELSE NULL END"
    )
    op.execute(
        "UPDATE agent_mcp_connections SET auth_kind = 'static', "
        "auth_config = '{\"header_name\": \"Authorization\", \"scheme\": \"Bearer\"}'::jsonb "
        "WHERE access_token_ciphertext IS NOT NULL"
    )
    op.execute("UPDATE agent_mcp_connections SET status = 'active' WHERE access_token_ciphertext IS NOT NULL")
    op.execute("ALTER TABLE agent_mcp_connections ALTER COLUMN label SET NOT NULL")
    op.execute("CREATE INDEX idx_agent_mcp_connections_user ON agent_mcp_connections (user_id) WHERE deleted_at IS NULL")
    op.execute("CREATE INDEX idx_agent_mcp_connections_connector ON agent_mcp_connections (connector_id)")


def _create_connectors() -> None:
    """User-run connectors, and the connections they are authorized to serve."""
    op.execute(
        f"""
        CREATE TABLE agent_mcp_connectors (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            registration_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN {_CONNECTOR_STATUSES}),
            mounts TEXT[] NOT NULL DEFAULT '{{}}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX idx_agent_mcp_connectors_user ON agent_mcp_connectors (user_id)")
    op.execute(
        """
        CREATE TABLE agent_mcp_connector_connections (
            connector_id BIGINT NOT NULL REFERENCES agent_mcp_connectors (id) ON DELETE CASCADE,
            connection_id BIGINT NOT NULL REFERENCES agent_mcp_connections (id) ON DELETE CASCADE,
            authorized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (connector_id, connection_id)
        )
        """
    )
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD CONSTRAINT fk_agent_mcp_connections_connector "
        "FOREIGN KEY (connector_id) REFERENCES agent_mcp_connectors (id) ON DELETE SET NULL"
    )


def _create_capabilities() -> None:
    """What each connection's server offered, as one versioned snapshot."""
    op.execute(
        """
        CREATE TABLE agent_mcp_capabilities (
            connection_id BIGINT PRIMARY KEY
                REFERENCES agent_mcp_connections (id) ON DELETE CASCADE,
            capability_revision BIGINT NOT NULL,
            config_revision BIGINT NOT NULL,
            protocol_version TEXT NOT NULL,
            server_name TEXT NOT NULL DEFAULT '',
            server_version TEXT NOT NULL DEFAULT '',
            tools JSONB NOT NULL DEFAULT '[]'::jsonb,
            resources JSONB NOT NULL DEFAULT '[]'::jsonb,
            resource_templates JSONB NOT NULL DEFAULT '[]'::jsonb,
            prompts JSONB NOT NULL DEFAULT '[]'::jsonb,
            support JSONB NOT NULL DEFAULT '{}'::jsonb,
            discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _create_oauth_flows() -> None:
    """One row per started authorization, fenced to the connection versions."""
    op.execute(
        """
        CREATE TABLE agent_mcp_oauth_flows (
            state_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            connection_id BIGINT NOT NULL
                REFERENCES agent_mcp_connections (id) ON DELETE CASCADE,
            config_revision BIGINT NOT NULL,
            authorization_epoch BIGINT NOT NULL,
            oauth_generation BIGINT NOT NULL,
            code_verifier_ciphertext TEXT NOT NULL,
            client_id TEXT NOT NULL,
            client_secret_ciphertext TEXT,
            client_auth_method TEXT NOT NULL DEFAULT 'none',
            registration TEXT NOT NULL DEFAULT 'dcr',
            issuer TEXT NOT NULL,
            resource TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            scopes TEXT[] NOT NULL DEFAULT '{}',
            authorization_endpoint TEXT NOT NULL,
            token_endpoint TEXT NOT NULL,
            revocation_endpoint TEXT,
            registration_endpoint TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX idx_agent_mcp_oauth_flows_expiry ON agent_mcp_oauth_flows (expires_at)")
    op.execute("CREATE INDEX idx_agent_mcp_oauth_flows_connection ON agent_mcp_oauth_flows (connection_id)")


def _create_thread_bindings() -> None:
    """One conversation's use of one connection, with its exact capability rules."""
    op.execute(
        """
        CREATE TABLE agent_mcp_thread_bindings (
            thread_id TEXT NOT NULL REFERENCES agent_threads (id) ON DELETE CASCADE,
            connection_id BIGINT NOT NULL REFERENCES agent_mcp_connections (id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            selection_pending BOOLEAN NOT NULL DEFAULT FALSE,
            config_revision BIGINT NOT NULL,
            authorization_epoch BIGINT NOT NULL,
            capability_revision BIGINT NOT NULL,
            policy_revision BIGINT NOT NULL DEFAULT 1,
            rules JSONB NOT NULL DEFAULT '{}'::jsonb,
            resource_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
            prompt_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, connection_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_mcp_thread_bindings_connection ON agent_mcp_thread_bindings (connection_id)"
    )


def _create_invocations() -> None:
    """A call that needed a decision, and the versions it was approved under."""
    op.execute(
        f"""
        CREATE TABLE agent_mcp_invocations (
            id BIGSERIAL PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES agent_jobs (id) ON DELETE CASCADE,
            attempt_id BIGINT REFERENCES agent_attempts (id) ON DELETE SET NULL,
            lease_generation BIGINT NOT NULL DEFAULT 0,
            thread_id TEXT NOT NULL,
            connection_id BIGINT NOT NULL REFERENCES agent_mcp_connections (id) ON DELETE CASCADE,
            capability_name TEXT NOT NULL,
            -- Which surface the name belongs to. Without it a tool called
            -- "search" and a prompt called "search" are one row, and an
            -- approval of one would authorize the other.
            capability_kind TEXT NOT NULL DEFAULT 'tool',
            arguments_digest TEXT NOT NULL,
            arguments_ciphertext TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN {_INVOCATION_STATUSES}),
            config_revision BIGINT NOT NULL,
            authorization_epoch BIGINT NOT NULL,
            capability_revision BIGINT NOT NULL,
            policy_revision BIGINT NOT NULL,
            dispatch_generation BIGINT NOT NULL DEFAULT 0,
            decided_by TEXT,
            decided_at TIMESTAMPTZ,
            result_summary TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_mcp_invocations_open ON agent_mcp_invocations (job_id, status) "
        "WHERE status IN ('pending', 'approved')"
    )
    op.execute("CREATE INDEX idx_agent_mcp_invocations_expiry ON agent_mcp_invocations (expires_at)")


def _create_transport_sessions() -> None:
    """Live protocol sessions, so a handle can never be reused across members."""
    op.execute(
        """
        CREATE TABLE agent_mcp_transport_sessions (
            handle TEXT PRIMARY KEY,
            connection_id BIGINT NOT NULL REFERENCES agent_mcp_connections (id) ON DELETE CASCADE,
            member TEXT NOT NULL,
            job_id TEXT,
            attempt_id BIGINT,
            owner_lease_generation BIGINT NOT NULL DEFAULT 0,
            protocol_version TEXT NOT NULL,
            upstream_session_id TEXT,
            config_revision BIGINT NOT NULL,
            authorization_epoch BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_mcp_transport_sessions_member "
        "ON agent_mcp_transport_sessions (connection_id, member)"
    )


def _migrate_thread_server_names() -> None:
    """Map the old provider-name list onto connections, or quarantine it.

    The rule is deliberately conservative. A legacy name maps only when the
    thread's owner has exactly one connection for that template; anything else
    -- no connection, two connections, a name with no template -- is recorded
    as quarantined rather than guessed at. Guessing would let a connection
    created later inherit a session that was authorized against a different
    one, which is the failure this whole revision exists to prevent.
    """
    op.execute(
        """
        CREATE TABLE agent_mcp_legacy_bindings (
            thread_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            server TEXT NOT NULL,
            reason TEXT NOT NULL,
            quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, server)
        )
        """
    )
    op.execute(
        """
        INSERT INTO agent_mcp_thread_bindings
            (thread_id, connection_id, alias, enabled, selection_pending,
             config_revision, authorization_epoch, capability_revision)
        SELECT legacy.thread_id, c.id, legacy.server, TRUE, TRUE,
               c.config_revision, c.authorization_epoch, c.capability_revision
        FROM (
            SELECT t.id AS thread_id, t.user_id, unnest(t.mcp_servers) AS server
            FROM agent_threads t
            WHERE t.mcp_servers IS NOT NULL AND cardinality(t.mcp_servers) > 0
        ) AS legacy
        JOIN agent_mcp_connections c
          ON c.user_id = legacy.user_id
         AND c.template = legacy.server
         AND c.deleted_at IS NULL
        WHERE (
            SELECT COUNT(*) FROM agent_mcp_connections c2
            WHERE c2.user_id = legacy.user_id
              AND c2.template = legacy.server
              AND c2.deleted_at IS NULL
        ) = 1
        ON CONFLICT (thread_id, connection_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO agent_mcp_legacy_bindings (thread_id, user_id, server, reason)
        SELECT legacy.thread_id, legacy.user_id, legacy.server,
               CASE
                   WHEN NOT EXISTS (
                       SELECT 1 FROM agent_mcp_connections c
                       WHERE c.user_id = legacy.user_id AND c.template = legacy.server
                         AND c.deleted_at IS NULL
                   ) THEN 'no_connection'
                   ELSE 'ambiguous_connection'
               END
        FROM (
            SELECT t.id AS thread_id, t.user_id, unnest(t.mcp_servers) AS server
            FROM agent_threads t
            WHERE t.mcp_servers IS NOT NULL AND cardinality(t.mcp_servers) > 0
        ) AS legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM agent_mcp_thread_bindings b
            WHERE b.thread_id = legacy.thread_id
              AND b.alias = legacy.server
        )
        ON CONFLICT (thread_id, server) DO NOTHING
        """
    )
    # Keep old job projections and empty selections executable until the API
    # is replaced. Bindings above preserve the selection; this column cannot
    # authorize anything, including when an old process tries to write it.
    op.execute("UPDATE agent_threads SET mcp_servers = '{}'::text[]")
    op.execute("ALTER TABLE agent_threads ALTER COLUMN mcp_servers SET DEFAULT '{}'::text[]")
    op.execute(
        "ALTER TABLE agent_threads ADD CONSTRAINT ck_agent_threads_legacy_mcp_empty "
        "CHECK (mcp_servers IS NULL OR cardinality(mcp_servers) = 0)"
    )


def downgrade() -> None:
    """Put the provider-shaped schema back, keeping what can be mapped."""
    op.execute("ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS mcp_servers TEXT[]")
    op.execute(
        "ALTER TABLE agent_threads DROP CONSTRAINT IF EXISTS ck_agent_threads_legacy_mcp_empty"
    )
    op.execute(
        """
        UPDATE agent_threads t
        SET mcp_servers = mapped.servers
        FROM (
            SELECT b.thread_id, array_agg(c.template ORDER BY c.template) AS servers
            FROM agent_mcp_thread_bindings b
            JOIN agent_mcp_connections c ON c.id = b.connection_id
            WHERE c.template IS NOT NULL
            GROUP BY b.thread_id
        ) AS mapped
        WHERE mapped.thread_id = t.id
        """
    )
    op.execute(
        """
        UPDATE agent_threads t
        SET mcp_servers = quarantined.servers
        FROM (
            SELECT thread_id, array_agg(server ORDER BY server) AS servers
            FROM agent_mcp_legacy_bindings
            GROUP BY thread_id
        ) AS quarantined
        WHERE quarantined.thread_id = t.id
          AND (t.mcp_servers IS NULL OR cardinality(t.mcp_servers) = 0)
        """
    )
    op.execute("DROP TABLE agent_mcp_legacy_bindings")
    op.execute("DROP TABLE agent_mcp_transport_sessions")
    op.execute("DROP TABLE agent_mcp_invocations")
    op.execute("DROP TABLE agent_mcp_thread_bindings")
    op.execute("DROP TABLE agent_mcp_oauth_flows")
    op.execute("DROP TABLE agent_mcp_capabilities")
    op.execute("ALTER TABLE agent_mcp_connections DROP CONSTRAINT fk_agent_mcp_connections_connector")
    op.execute("DROP TABLE agent_mcp_connector_connections")
    op.execute("DROP TABLE agent_mcp_connectors")
    op.execute("DROP INDEX IF EXISTS idx_agent_mcp_connections_user")
    op.execute("DROP INDEX IF EXISTS idx_agent_mcp_connections_connector")
    for column in (
        "label",
        "transport",
        "location",
        "url",
        "launch_spec",
        "auth_kind",
        "auth_config",
        "connector_id",
        "status",
        "error_kind",
        "error_detail",
        "client_secret_ciphertext",
        "issuer",
        "resource",
        "scopes",
        "refresh_rotates",
        "token_endpoint",
        "revocation_endpoint",
        "registration_endpoint",
        "client_auth_method",
        "registration",
        "config_revision",
        "authorization_epoch",
        "oauth_generation",
        "credential_version",
        "capability_revision",
        "last_checked_at",
        "request_timeout_s",
        "deleted_at",
    ):
        op.execute(f"ALTER TABLE agent_mcp_connections DROP COLUMN {column}")
    # Rows this revision added cannot be represented in the old shape, and one
    # connection per provider is what the old constraint allowed.
    op.execute(
        "DELETE FROM agent_mcp_connections a USING agent_mcp_connections b "
        "WHERE a.template IS NOT NULL AND a.template = b.template "
        "AND a.user_id = b.user_id AND a.id > b.id"
    )
    op.execute("DELETE FROM agent_mcp_connections WHERE template IS NULL")
    op.execute("UPDATE agent_mcp_connections SET template = 'github' WHERE template IS NULL")
    op.execute("ALTER TABLE agent_mcp_connections DROP COLUMN IF EXISTS server")
    op.execute("ALTER TABLE agent_mcp_connections RENAME COLUMN template TO server")
    op.execute("ALTER TABLE agent_mcp_connections ALTER COLUMN server SET NOT NULL")
    op.execute("DELETE FROM agent_mcp_connections WHERE access_token_ciphertext IS NULL")
    op.execute("ALTER TABLE agent_mcp_connections ALTER COLUMN access_token_ciphertext SET NOT NULL")
    op.execute(
        "ALTER TABLE agent_mcp_connections ADD CONSTRAINT uq_agent_mcp_connections_server "
        "UNIQUE (user_id, server)"
    )
