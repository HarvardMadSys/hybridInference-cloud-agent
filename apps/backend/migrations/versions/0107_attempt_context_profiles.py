"""Persist each attempt's derived context profile.

Revision ID: 0107_attempt_context_profiles
Revises: 0106_user_model_connections

Like ``agent_attempt_thread_stats`` (0099) this is a rebuildable cache: the
event ledger stays authoritative, a reader compares ``event_seq`` and
``reducer_version`` with the ledger and rebuilds a missing or stale row, and
existing attempts are profiled lazily, so the migration scans nothing. The
summary columns let an owner's conversations be ranked in SQL; ``profile``
holds the bounded detail one conversation's Context view reads.
"""

from alembic import op

revision = "0107_attempt_context_profiles"
down_revision = "0106_user_model_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the cache table; nothing is backfilled."""
    op.execute(
        """
        CREATE TABLE agent_attempt_context_profiles (
            attempt_id BIGINT PRIMARY KEY
                REFERENCES agent_attempts(id) ON DELETE CASCADE,
            event_seq INTEGER NOT NULL CHECK (event_seq >= 0),
            reducer_version INTEGER NOT NULL CHECK (reducer_version > 0),
            main_requests INTEGER NOT NULL CHECK (main_requests >= 0),
            subagent_requests INTEGER NOT NULL CHECK (subagent_requests >= 0),
            peak_prompt_tokens BIGINT CHECK (peak_prompt_tokens >= 0),
            latest_prompt_tokens BIGINT CHECK (latest_prompt_tokens >= 0),
            latest_observed_at TIMESTAMPTZ,
            context_window BIGINT CHECK (context_window > 0),
            compactions INTEGER NOT NULL CHECK (compactions >= 0),
            profile JSONB NOT NULL CHECK (jsonb_typeof(profile) = 'object'),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    """Drop only the rebuildable cache."""
    op.execute("DROP TABLE agent_attempt_context_profiles")
