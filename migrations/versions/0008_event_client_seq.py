"""Make event re-upload idempotent: a client-owned sequence per attempt.

``agent_job_events.seq`` is server-assigned under the attempt row lock, which
serializes writers but cannot recognize a *retry*: a runner whose POST died on
the way back re-sends, and the same event lands twice under two seqs. The fix
is a second, client-owned dimension — the runner numbers its own uploads and a
replayed number returns the stored row instead of inserting a new one.

Deliberately a separate column rather than reusing ``seq``: the control plane
also appends to a running attempt (terminal suspend lifecycle rows), and a
server-assigned MAX+1 landing between two runner uploads would collide with
the runner's next number — a retry answered with someone else's event. The
partial unique index keeps the two numbering spaces disjoint and costs nothing
for rows that carry no client number (design §5, event envelope idempotency).

Revision ID: 0008_event_client_seq
Revises: 0007_version_stamps
"""

from __future__ import annotations

from alembic import op

revision = "0008_event_client_seq"
down_revision = "0007_version_stamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the client-owned sequence and its dedupe index."""
    op.execute("ALTER TABLE agent_job_events ADD COLUMN IF NOT EXISTS client_seq BIGINT")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_job_events_attempt_client_seq "
        "ON agent_job_events(attempt_id, client_seq) WHERE client_seq IS NOT NULL"
    )


def downgrade() -> None:
    """Drop the client sequence."""
    op.execute("DROP INDEX IF EXISTS uq_agent_job_events_attempt_client_seq")
    op.execute("ALTER TABLE agent_job_events DROP COLUMN IF EXISTS client_seq")
