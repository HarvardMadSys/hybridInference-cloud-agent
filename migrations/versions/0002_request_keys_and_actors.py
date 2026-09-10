"""Recognise a resent submission, and record which administrator acted.

**Submissions.** A create or follow-up whose response is lost — a timeout, a
dropped connection — is resent by the client, and until now every resend was
a new task: a second thread, a second turn, a second sandbox admission, twice
the inference. Fork already carried an ``Idempotency-Key``; this gives the
other two submissions the same contract. The key is the client's per-action
identifier, unique per owner, stored beside a fingerprint of what was
submitted under it: the same key with the same fingerprint returns the job it
made the first time, and the same key with a different fingerprint is refused
rather than answered with somebody else's turn. Both columns travel together
(the CHECK), and a submission without a key is what every submission was
before — nothing changes for a client that does not send one.

**Administrators.** The audit trail recorded an address and never a person.
Every admin route already authenticates an identity; the row now carries it.
Rows written before this revision say ``unrecorded`` rather than pretending
to know, and the default is dropped afterwards so a new row must say who.

Revision ID: 0002_request_keys_and_actors
Revises: 0001_schema_baseline
"""

from __future__ import annotations

from alembic import op

revision = "0002_request_keys_and_actors"
down_revision = "0001_schema_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the submission key pair and the audit actor."""
    op.execute("ALTER TABLE agent_jobs ADD COLUMN client_request_id TEXT")
    op.execute("ALTER TABLE agent_jobs ADD COLUMN client_request_fingerprint TEXT")
    op.execute(
        """
        ALTER TABLE agent_jobs
            ADD CONSTRAINT ck_agent_jobs_client_request_complete
            CHECK ((client_request_id IS NULL) = (client_request_fingerprint IS NULL))
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_jobs_client_request
            ON agent_jobs (user_id, client_request_id)
            WHERE client_request_id IS NOT NULL
        """
    )
    op.execute(
        "ALTER TABLE agent_admin_audit_log ADD COLUMN actor_id TEXT NOT NULL DEFAULT 'unrecorded'"
    )
    op.execute("ALTER TABLE agent_admin_audit_log ALTER COLUMN actor_id DROP DEFAULT")


def downgrade() -> None:
    """Remove the submission key pair and the audit actor."""
    op.execute("ALTER TABLE agent_admin_audit_log DROP COLUMN IF EXISTS actor_id")
    op.execute("DROP INDEX IF EXISTS uq_agent_jobs_client_request")
    op.execute(
        "ALTER TABLE agent_jobs DROP CONSTRAINT IF EXISTS ck_agent_jobs_client_request_complete"
    )
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS client_request_fingerprint")
    op.execute("ALTER TABLE agent_jobs DROP COLUMN IF EXISTS client_request_id")
