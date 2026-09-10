"""Move what a conversation shares between its turns onto the conversation.

``repo``, ``setup_script``, ``execution_profile`` and ``mcp_servers`` were
columns of every job, written once when a thread was created and copied
verbatim into every follow-up and every fork copy. Four values that never
differed between the turns of one thread were held once per turn, and each
copy was one more place to keep in step. They now live on ``agent_threads``,
written once, read by joining the thread — which every job read already did.

The move changes nothing a client sees: a job still reports its repo, setup
script, execution profile and tool servers, sourced from its thread. It
changes what the database can promise — a follow-up cannot carry a different
repo or profile than its thread, because there is no column for it to carry
one in.

The backfill takes each thread's values from its first turn, which is the
turn that was created with them. Every thread has one, so the profile can be
made required in the same revision.

Revision ID: 0003_thread_level_config
Revises: 0002_request_keys_and_actors
"""

from __future__ import annotations

from alembic import op

revision = "0003_thread_level_config"
down_revision = "0002_request_keys_and_actors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the thread columns, backfill from turn one, drop the per-turn copies."""
    op.execute("ALTER TABLE agent_threads ADD COLUMN setup_script TEXT")
    op.execute("ALTER TABLE agent_threads ADD COLUMN execution_profile TEXT")
    op.execute("ALTER TABLE agent_threads ADD COLUMN mcp_servers TEXT[]")
    op.execute(
        """
        UPDATE agent_threads t
        SET setup_script = j.setup_script,
            execution_profile = j.execution_profile,
            mcp_servers = j.mcp_servers
        FROM agent_jobs j
        WHERE j.thread_id = t.id AND j.turn_no = 1
        """
    )
    op.execute("ALTER TABLE agent_threads ALTER COLUMN execution_profile SET NOT NULL")
    op.execute(
        """
        ALTER TABLE agent_threads
            ADD CONSTRAINT ck_agent_threads_execution_profile
            CHECK (execution_profile IN ('trusted-v1', 'untrusted-kata-v1'))
        """
    )
    op.execute("CREATE INDEX idx_agent_threads_user_repo ON agent_threads (user_id, repo)")
    # The per-turn copies, and the index that was keyed on one of them.
    op.execute("DROP INDEX IF EXISTS idx_agent_jobs_user_repo")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN repo")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN setup_script")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN execution_profile")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN mcp_servers")


def downgrade() -> None:
    """Put the per-turn copies back, filled from the thread."""
    op.execute("ALTER TABLE agent_jobs ADD COLUMN repo TEXT")
    op.execute("ALTER TABLE agent_jobs ADD COLUMN setup_script TEXT")
    op.execute("ALTER TABLE agent_jobs ADD COLUMN execution_profile TEXT")
    op.execute("ALTER TABLE agent_jobs ADD COLUMN mcp_servers TEXT[]")
    op.execute(
        """
        UPDATE agent_jobs j
        SET repo = t.repo,
            setup_script = t.setup_script,
            execution_profile = t.execution_profile,
            mcp_servers = t.mcp_servers
        FROM agent_threads t
        WHERE t.id = j.thread_id
        """
    )
    op.execute("ALTER TABLE agent_jobs ALTER COLUMN repo SET NOT NULL")
    op.execute("ALTER TABLE agent_jobs ALTER COLUMN execution_profile SET NOT NULL")
    op.execute(
        """
        ALTER TABLE agent_jobs
            ADD CONSTRAINT agent_jobs_execution_profile_check
            CHECK (execution_profile IN ('trusted-v1', 'untrusted-kata-v1'))
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_jobs_user_repo ON agent_jobs (user_id, repo, created_at DESC)"
    )
    op.execute("DROP INDEX IF EXISTS idx_agent_threads_user_repo")
    op.execute("ALTER TABLE agent_threads DROP CONSTRAINT IF EXISTS ck_agent_threads_execution_profile")
    op.execute("ALTER TABLE agent_threads DROP COLUMN mcp_servers")
    op.execute("ALTER TABLE agent_threads DROP COLUMN execution_profile")
    op.execute("ALTER TABLE agent_threads DROP COLUMN setup_script")
