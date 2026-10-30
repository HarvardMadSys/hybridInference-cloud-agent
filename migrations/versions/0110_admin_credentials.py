"""Named, revocable administrator API credentials.

``ADMIN_TOKEN`` stays what it is: the deployment's full administrator token,
read from the environment on every request and independent of the database.
These credentials are for administrator automation: each is shown once when
created, stored only as a SHA-256 verifier, checked against the database on
every request (so revoking one takes effect on the next request, on every
process), and carries scopes. ``deployment`` lets it create deployment
intents; without it, it cannot.

Revision ID: 0110_admin_credentials
Revises: 0109_deployment_agents
"""

from __future__ import annotations

from alembic import op

revision = "0110_admin_credentials"
down_revision = "0109_deployment_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the credential table."""
    op.execute(
        """
        CREATE TABLE agent_admin_credentials (
            credential_id TEXT PRIMARY KEY CHECK (credential_id ~ '^[0-9a-f]{32}$'),
            name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128),
            verifier TEXT NOT NULL UNIQUE CHECK (verifier ~ '^[0-9a-f]{64}$'),
            scopes TEXT[] NOT NULL CHECK (scopes <@ ARRAY['admin', 'deployment']::text[]),
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            revoked_by TEXT,
            last_used_at TIMESTAMPTZ
        )
        """
    )


def downgrade() -> None:
    """Drop the table; every credential stops working, ADMIN_TOKEN is unaffected."""
    op.execute("DROP TABLE IF EXISTS agent_admin_credentials")
