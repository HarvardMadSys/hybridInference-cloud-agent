"""The GitHub account a user connected, apart from the installations it reached.

Revision ID: 0024_github_accounts
Revises: 0023_pending_messages

Until this revision a GitHub connection *was* its installation grants: a user
who authorized the App but could not yet reach any installation left no row
at all, and a user whose installations covered no repository read as never
having connected. The composer inferred "connected" from "has a repository",
so both were sent back to a Connect button they had already pressed.

One row per platform user: the GitHub identity GitHub reported for the token
the connect flow exchanged, and nothing else. It is display and state, never
authority — what a user may work on is still decided per installation, by
``agent_repo_grants`` and by GitHub's own answer. Reconnecting as a different
GitHub account replaces the row, and with it the grants.
"""

from __future__ import annotations

from alembic import op

revision = "0024_github_accounts"
down_revision = "0023_pending_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the one-per-user GitHub account table."""
    op.execute("""
        CREATE TABLE agent_github_accounts (
            user_id TEXT PRIMARY KEY,
            github_user_id BIGINT NOT NULL,
            login TEXT NOT NULL CHECK (login <> ''),
            connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    """Drop the table; the installation grants it sat beside are untouched."""
    op.execute("DROP TABLE agent_github_accounts")
