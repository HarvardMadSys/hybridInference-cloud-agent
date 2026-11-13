"""Messages the owner has written but the platform has not sent yet.

Revision ID: 0023_pending_messages
Revises: 0022_goal_open_ended

Until this revision a message typed while a turn was running became the next
turn immediately: a queued job and a transcript row, committed together, which
the owner could neither edit nor take back. A pending message is the step before
that. It lives in its own small table, it can be rewritten or deleted, and it is
turned into a turn — the same job and transcript row as before — only when the
turn in front of it has ended and the queue is allowed to move.

Three states and no history. ``queued`` waits its turn; ``held`` is a message
the owner is editing, or one the platform could not send (``last_error`` says
why), and nothing is sent past it; ``sending`` is a message being handed to the
running turn right now ("send now"). A message that is sent is *deleted*: the job
and transcript row it became are the record, and a second record here would be a
second truth.

A message handed to a running turn becomes a second user row of *that* turn's
job rather than a job of its own. ``agent_thread_messages.mid_turn`` marks such
a row: the one-user-row-per-job index now covers only the turn's prompt, and a
mid-turn row reads in order with the turn's replies instead of at its top.

``position`` comes from a sequence, not from a per-thread counter, and editing
never changes it — rewriting a message does not move it in line.

The thread carries two facts about its queue. ``queue_paused_reason`` says why
automatic sending stopped (a Stop, a failed turn, an archive) and stays until the
owner continues. ``queue_settled_job_id`` names the newest turn whose ending the
queue has already acted on, so that one cancelled or failed turn pauses the queue
once, and continuing past it is not undone by looking at it again.

Uploads a pending message carries are pinned to it through
``agent_job_attachments.pending_message_id``: the staged-upload sweeper leaves
them alone while it is set, and deleting the message releases them back to
ordinary staged uploads.
"""

from __future__ import annotations

from alembic import op

revision = "0023_pending_messages"
down_revision = "0022_goal_open_ended"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the pending-message table, the thread's queue columns, mid-turn rows."""
    op.execute("CREATE SEQUENCE agent_pending_message_position_seq")
    op.execute("""
        CREATE TABLE agent_pending_messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES agent_threads (id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            position BIGINT NOT NULL DEFAULT nextval('agent_pending_message_position_seq'),
            state TEXT NOT NULL CHECK (state IN ('queued', 'held', 'sending')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            prompt TEXT NOT NULL CHECK (char_length(prompt) BETWEEN 1 AND 100000),
            -- The same meaning as on a follow-up request: NULL inherits what the
            -- previous turn used, a value overrides it for this turn only.
            skill_ids JSONB
                CHECK (skill_ids IS NULL OR jsonb_typeof(skill_ids) = 'array'),
            attachment_ids JSONB
                CHECK (attachment_ids IS NULL OR jsonb_typeof(attachment_ids) = 'array'),
            model TEXT,
            -- The request's literal reset value is kept as written: it is how a
            -- message says "back to Auto", which NULL (inherit) cannot.
            reasoning_effort TEXT,
            client_request_id TEXT,
            client_request_fingerprint TEXT,
            send_now_job_id TEXT REFERENCES agent_jobs (id) ON DELETE SET NULL,
            send_now_requested_at TIMESTAMPTZ,
            last_error TEXT CHECK (last_error IS NULL OR char_length(last_error) <= 2000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_agent_pending_messages_client_request
                UNIQUE (user_id, client_request_id),
            CONSTRAINT ck_agent_pending_messages_client_request
                CHECK ((client_request_id IS NULL) = (client_request_fingerprint IS NULL))
        )
    """)
    op.execute(
        "ALTER SEQUENCE agent_pending_message_position_seq OWNED BY agent_pending_messages.position"
    )
    op.execute(
        "CREATE INDEX ix_agent_pending_messages_thread_position "
        "ON agent_pending_messages (thread_id, position)"
    )
    op.execute(
        "CREATE INDEX ix_agent_pending_messages_send_now "
        "ON agent_pending_messages (send_now_job_id) WHERE send_now_job_id IS NOT NULL"
    )
    op.execute("""
        ALTER TABLE agent_threads
            ADD COLUMN queue_paused_reason TEXT
                CHECK (queue_paused_reason IN ('stopped', 'failed', 'archived')),
            ADD COLUMN queue_settled_job_id TEXT
    """)
    op.execute("""
        ALTER TABLE agent_job_attachments
            ADD COLUMN pending_message_id TEXT
                REFERENCES agent_pending_messages (id) ON DELETE SET NULL
    """)
    # The foreign key's own lookup on delete, and the pin the sweeper checks.
    op.execute(
        "CREATE INDEX ix_agent_job_attachments_pending "
        "ON agent_job_attachments (pending_message_id) WHERE pending_message_id IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE agent_thread_messages ADD COLUMN mid_turn BOOLEAN NOT NULL DEFAULT FALSE "
        "CONSTRAINT ck_agent_thread_messages_mid_turn_user CHECK (NOT mid_turn OR role = 'user')"
    )
    # Still one prompt per turn; a message handed to the running turn is another
    # user row of the same job and is not the prompt.
    op.execute("DROP INDEX idx_agent_thread_messages_user_job")
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_thread_messages_user_job "
        "ON agent_thread_messages (job_id) WHERE role = 'user' AND NOT mid_turn"
    )


def downgrade() -> None:
    """Remove the queue; refuse while it still holds a message someone wrote.

    A pending message exists nowhere else — it has no job and no transcript row
    yet — so dropping the table with rows in it would delete what an owner typed
    without telling them. Send or delete them first.
    """
    op.execute("LOCK TABLE agent_pending_messages IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_pending_messages) THEN
                RAISE EXCEPTION 'Send or delete pending messages before downgrading';
            END IF;
        END $$
    """)
    # A message handed to a running turn is a second user row of its job, which
    # the older one-user-row index cannot hold. Keeping it means not going back.
    op.execute("LOCK TABLE agent_thread_messages IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM agent_thread_messages WHERE mid_turn) THEN
                RAISE EXCEPTION 'Transcripts hold messages sent into running turns; '
                    'the previous schema cannot represent them';
            END IF;
        END $$
    """)
    op.execute("DROP INDEX idx_agent_thread_messages_user_job")
    op.execute(
        "CREATE UNIQUE INDEX idx_agent_thread_messages_user_job "
        "ON agent_thread_messages (job_id) WHERE role = 'user'"
    )
    op.execute("ALTER TABLE agent_thread_messages DROP COLUMN mid_turn")
    op.execute("DROP INDEX ix_agent_job_attachments_pending")
    op.execute("ALTER TABLE agent_job_attachments DROP COLUMN pending_message_id")
    op.execute(
        "ALTER TABLE agent_threads DROP COLUMN queue_settled_job_id, "
        "DROP COLUMN queue_paused_reason"
    )
    op.execute("DROP TABLE agent_pending_messages")
