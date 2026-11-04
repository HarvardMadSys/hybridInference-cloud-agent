"""Record the pull requests a conversation's agent opened through the GitHub relay.

The agent now opens its own pull requests: its ``POST /repos/{owner}/{repo}/pulls``
passes through the GitHub relay, which records the one GitHub created. This is
where the conversation's pull request links come from, instead of the
platform publisher's ``published_pr_url``.

One row per pull request per conversation. ``job_id`` is the turn that opened
it, kept for attribution only: a turn's row may outlive nothing it refers to,
so it is not a foreign key that a turn's deletion would have to wait on.

Revision ID: 0111_thread_pull_requests
Revises: 0110_host_late_settings
"""

from __future__ import annotations

from alembic import op

revision = "0111_thread_pull_requests"
down_revision = "0110_host_late_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the table; no conversation has opened a pull request through the relay yet."""
    op.execute(
        """
        CREATE TABLE agent_thread_pull_requests (
            thread_id TEXT NOT NULL REFERENCES agent_threads (id) ON DELETE CASCADE,
            repo TEXT NOT NULL,
            number INTEGER NOT NULL CHECK (number > 0),
            url TEXT NOT NULL,
            job_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            head_ref TEXT NOT NULL,
            base_ref TEXT NOT NULL,
            draft BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (thread_id, repo, number)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_thread_pull_requests_recent "
        "ON agent_thread_pull_requests (thread_id, created_at DESC)"
    )


def downgrade() -> None:
    """Drop the table; an older release reads pull request links from its publisher."""
    op.execute("DROP TABLE IF EXISTS agent_thread_pull_requests")
