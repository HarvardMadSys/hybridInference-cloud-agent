"""Hold each person's own MCP credentials, one row per user per server.

#48 removed the deployment-owned MCP registry: one operator-supplied credential
that every user who could create a job could call an upstream with. This is
where the replacement lives — a connection belongs to a user, carries their
credential, and is the only thing that can make a server reachable for them.

**Shaped after ``agent_gitlab_connections``**, deliberately: encrypted access
token, optional encrypted refresh token, an expiry, and a uniqueness constraint
that makes "connected" a yes/no per person. The same properties come with it —
the credential never leaves this service in plaintext, and there is no column a
URL or a key could leak through into an API response.

``refresh_token_ciphertext`` and ``expires_at`` are **nullable**, which
GitLab's are not. MCP providers vary: some issue refreshable tokens, some issue
long-lived ones, some revoke server-side with no notice to us. ``NULL`` reads
as "nothing to refresh, no expiry we know of" — the connection then fails at
its next tool call if the upstream has revoked it, which the relay reports as
that one server being unavailable. Encoding an expiry we do not have would
either disconnect working servers or promise a liveness check nobody performs.

**The ``agent_oauth_states`` provider check is widened.** That table already
holds single-use, user-bound OAuth state, and MCP connections need exactly the
same thing; its ``CHECK (provider IN ('github','gitlab'))`` was the only thing
in the way. The replacement checks that a provider is non-empty rather than
enumerating names: the catalogue in ``mcp_catalogue.py`` is the authority on
which servers exist, and an enum here would mean a schema migration every time
a provider is added — a constraint that has to be edited to keep working is one
that gets edited without thought. What the enum actually protected against is
covered where it belongs: state is issued and consumed under a provider name
the code supplies, never one a request does.

Revision ID: 0006_per_user_mcp_connections
Revises: 0005_grants_and_audit
"""

from __future__ import annotations

from alembic import op

revision = "0006_per_user_mcp_connections"
down_revision = "0005_grants_and_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add per-user MCP connections and let OAuth state name their providers."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_mcp_connections (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            server TEXT NOT NULL,
            account_label TEXT,
            access_token_ciphertext TEXT NOT NULL,
            refresh_token_ciphertext TEXT,
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, server)
        )
        """
    )
    # Every read of this table is "what has this user connected" — for the
    # composer, for job creation, and for each tool call the relay authorizes.
    # The UNIQUE (user_id, server) index already leads with user_id and serves
    # all of them, so there is deliberately no second index here.

    # Dropped by *definition*, not by name. The baseline declared this check
    # inline and unnamed, so its name is whatever PostgreSQL derived — and a
    # `DROP CONSTRAINT IF EXISTS <guess>` that guesses wrong does nothing at
    # all, silently leaving the old enum in place. The failure that produces is
    # not a migration error: it is every MCP connection attempt rejected at
    # `issue_oauth_state`, long after this ran green.
    op.execute(
        """
        DO $$
        DECLARE constraint_name TEXT;
        BEGIN
            FOR constraint_name IN
                SELECT con.conname
                FROM pg_constraint con
                -- Resolved through the search path rather than matched by
                -- name: `relname = '...'` finds a same-named table in any
                -- schema, and the ALTER below would then be aimed at a table
                -- this migration was not talking about.
                WHERE con.conrelid = 'agent_oauth_states'::regclass
                  AND con.contype = 'c'
                  AND pg_get_constraintdef(con.oid) ILIKE '%provider%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE agent_oauth_states DROP CONSTRAINT %I', constraint_name
                );
            END LOOP;
        END $$
        """
    )
    op.execute(
        "ALTER TABLE agent_oauth_states ADD CONSTRAINT agent_oauth_states_provider_check "
        "CHECK (provider <> '')"
    )


def downgrade() -> None:
    """Drop the connections and narrow OAuth state back to source control.

    The narrowing fails if MCP OAuth state rows are still present, which is the
    honest outcome: they are rows this constraint says cannot exist. Clear them
    before downgrading.
    """
    op.execute("DROP TABLE IF EXISTS agent_mcp_connections")
    op.execute(
        "ALTER TABLE agent_oauth_states DROP CONSTRAINT IF EXISTS agent_oauth_states_provider_check"
    )
    op.execute(
        "ALTER TABLE agent_oauth_states ADD CONSTRAINT agent_oauth_states_provider_check "
        "CHECK (provider IN ('github', 'gitlab'))"
    )
