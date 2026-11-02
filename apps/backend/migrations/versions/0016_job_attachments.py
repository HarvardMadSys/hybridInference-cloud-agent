"""Per-task uploaded files and the manifest a turn retains.

Revision ID: 0016_job_attachments
Revises: 0015_mcp_enablement
"""

from __future__ import annotations

from alembic import op

revision = "0016_job_attachments"
down_revision = "0015_mcp_enablement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store uploads in the control plane's own database, bounded by CHECKs."""
    # Bytes live here rather than in an object store because this service has
    # none, and the bounds below are small enough that introducing one would
    # cost more than it saves: a new dependency, a new credential and a new
    # deployment surface for at most 20 MB a task.
    #
    # ``job_id``/``thread_id`` are NULL while an upload is staged — the browser
    # sends the file before the job exists, so the create request can stay JSON
    # and keep the Idempotency-Key fingerprint it already had. The composite
    # foreign key is not enforced while they are NULL. Its cascade covers a
    # hard delete of a job row; an owner deleting a conversation is a *soft*
    # delete that deliberately keeps the job ledger, so `delete_sessions`
    # removes these rows explicitly. Both paths exist because neither alone
    # stops an upload outliving the conversation that held it.
    op.execute("""
        CREATE TABLE agent_job_attachments (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            job_id TEXT,
            thread_id TEXT,
            path TEXT NOT NULL CHECK (char_length(path) BETWEEN 1 AND 512),
            content BYTEA NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 5242880),
            content_type TEXT NOT NULL CHECK (char_length(content_type) BETWEEN 1 AND 255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at TIMESTAMPTZ,
            CONSTRAINT agent_job_attachments_job_fk
                FOREIGN KEY (job_id, thread_id) REFERENCES agent_jobs (id, thread_id)
                ON DELETE CASCADE,
            CONSTRAINT agent_job_attachments_claim_ck
                CHECK ((job_id IS NULL) = (thread_id IS NULL))
        )
    """)
    # The runner reads a whole thread's uploads, not one turn's: a retry on a
    # fresh container has to rebuild /uploads completely, and a retained one
    # simply overwrites the same paths.
    op.execute(
        "CREATE INDEX ix_agent_job_attachments_thread "
        "ON agent_job_attachments (thread_id, id) WHERE thread_id IS NOT NULL"
    )
    # Staged rows nobody claimed are swept by age.
    op.execute(
        "CREATE INDEX ix_agent_job_attachments_staged "
        "ON agent_job_attachments (created_at) WHERE job_id IS NULL"
    )
    op.execute("CREATE INDEX ix_agent_job_attachments_owner ON agent_job_attachments (user_id)")
    # What this turn attached, without the bytes: id, path, size and type. The
    # rows above stay the single copy of the content, so a fork or a retry
    # references the same immutable bytes rather than duplicating them.
    op.execute(
        "ALTER TABLE agent_jobs ADD COLUMN attachments JSONB "
        "CHECK (attachments IS NULL OR jsonb_typeof(attachments) = 'array')"
    )


def downgrade() -> None:
    """Remove the new feature's data; only downgrade with the API stopped."""
    op.execute("ALTER TABLE agent_jobs DROP COLUMN attachments")
    op.execute("DROP TABLE agent_job_attachments")
