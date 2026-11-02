"""Keep the user's own GitHub token, so repositories are theirs, not the App's.

Revision ID: 0025_github_user_tokens
Revises: 0024_github_accounts

Which repositories a user could work on was every repository an installation
they reached covered, read with the installation's own token. GitHub reports an
organization's installation to anyone with access to *one* of its repositories,
so an outside collaborator on a single repository was offered — and could clone
into a sandbox and read — every repository the App could see there.

The answer has to be asked as the user: GitHub's
``/user/installations/{id}/repositories`` lists only the repositories that user
can access, with their permission on each. That takes the user's token after the
connect flow has finished, so it is kept, encrypted with the source-control key
exactly like a GitLab connection's.

A GitHub App's user tokens usually expire after eight hours and come with a
single-use refresh token, so the refresh is fenced the way the MCP credential
refresh is (``0009_mcp_refresh_lease``): ``refresh_generation`` proves a write
still holds the lease, and ``refresh_lease_expires_at`` keeps a refresher that
died from locking the row forever. Two callers refreshing at once would
otherwise spend the same refresh token, and the loser would destroy the
connection both were keeping.

A row with no token is a connection made before this revision, or one whose
authorization GitHub has since refused: its user connects again.
"""

from __future__ import annotations

from alembic import op

revision = "0025_github_user_tokens"
down_revision = "0024_github_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the encrypted token pair and the refresh lease to the account row."""
    op.execute("""
        ALTER TABLE agent_github_accounts
            ADD COLUMN access_token_ciphertext TEXT,
            ADD COLUMN access_expires_at TIMESTAMPTZ,
            ADD COLUMN refresh_token_ciphertext TEXT,
            ADD COLUMN refresh_expires_at TIMESTAMPTZ,
            ADD COLUMN refresh_generation BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN refresh_lease_expires_at TIMESTAMPTZ,
            ADD CONSTRAINT ck_agent_github_accounts_refresh_needs_access
                CHECK (refresh_token_ciphertext IS NULL OR access_token_ciphertext IS NOT NULL)
    """)


def downgrade() -> None:
    """Drop the tokens; accounts and grants stay, and every user reconnects after re-upgrading."""
    op.execute("""
        ALTER TABLE agent_github_accounts
            DROP CONSTRAINT ck_agent_github_accounts_refresh_needs_access,
            DROP COLUMN refresh_lease_expires_at,
            DROP COLUMN refresh_generation,
            DROP COLUMN refresh_expires_at,
            DROP COLUMN refresh_token_ciphertext,
            DROP COLUMN access_expires_at,
            DROP COLUMN access_token_ciphertext
    """)
