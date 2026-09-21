"""Give the goal three durable states, a durable scheduling obligation, and a strict report.

Revision ID: 0020_goal_controller
Revises: 0019_conversation_goal

``0017`` shipped a four-state goal whose "blocked" was a lifecycle state, a
two-line text convention that could not be refused, and a scheduling decision
taken inline after a finish with no record of it. This migration moves the
stored goal to what the controller design actually needs:

* ``active`` / ``paused`` / ``complete``, with the *reason* it stopped kept as
  data (``goal_pause_reason`` + ``goal_pause_detail``). An old ``blocked`` row
  becomes ``paused`` and keeps its block message as its detail — it was never a
  state, it was a reason, and rewriting it as an owner pause would be a lie.
* ``goal_id``, the identity of the objective being worked toward. The revision
  is the thread's control version and moves for edits that change nothing; a
  report has to be attributable to the objective it was written about, and only
  a stable identity can do that.
* ``agent_goal_wakeups``, so "this turn ended, that goal has to be looked at"
  is a row rather than a code path. The consumer can be replaying after a crash
  and the answer must be the same.
* a strict report envelope on ``agent_jobs`` (parse result, outcome, evidence,
  digest) replacing the two free-text columns, and an input counter so a
  completion claim cannot finish a goal the owner has already moved past.
* ``goal_repairs_used``, the bound on restating a malformed report.

Nothing is backfilled that cannot be proven: existing rows keep their objective,
their identity is minted, an old ``blocked`` keeps the message it stopped with,
and an old ``complete`` is recorded as ``model_reported`` because that is what
it was.
"""

from alembic import op

revision = "0020_goal_controller"
down_revision = "0019_conversation_goal"
branch_labels = None
depends_on = None

_STATES = "'active', 'paused', 'complete'"
_PAUSE_REASONS = (
    "'user', 'fork', 'needs_input', 'round_limit', 'execution_failed', "
    "'cancelled', 'report_invalid', 'report_unavailable', 'runtime_unsupported', "
    "'context_lost', 'access_denied', 'quota_unavailable', 'scheduling_failed', "
    "'archived'"
)


def upgrade() -> None:
    """Move the goal to three states and make its scheduling obligation durable."""
    # -- the goal itself ---------------------------------------------------
    # Columns first, constraints after the rows have been rewritten: the shape
    # constraints below are only true of the new shape.
    op.execute(
        """
        ALTER TABLE agent_threads
            ADD COLUMN goal_id TEXT,
            ADD COLUMN goal_pause_detail TEXT
                CHECK (goal_pause_detail IS NULL
                       OR char_length(goal_pause_detail) <= 2000),
            ADD COLUMN goal_completion_basis TEXT
                CHECK (goal_completion_basis IS NULL
                       OR goal_completion_basis = 'model_reported'),
            ADD COLUMN goal_completed_by_job_id TEXT,
            ADD COLUMN goal_repairs_used INT NOT NULL DEFAULT 0
        """
    )
    # ``0017`` declared four of these inline, so Postgres named them after the
    # table and column. They still enforce the vocabulary this migration is
    # replacing, and a constraint with the *same* predicate under a new name
    # would not shadow them -- it would sit beside them and reject every one of
    # the new pause reasons. Dropped before the first statement that writes one.
    for legacy in (
        "agent_threads_goal_state_check",
        "agent_threads_goal_pause_reason_check",
        "agent_threads_goal_max_rounds_check",
        "agent_threads_goal_objective_check",
        "agent_threads_goal_completion_basis_check",
    ):
        op.execute(f"ALTER TABLE agent_threads DROP CONSTRAINT IF EXISTS {legacy}")
    # An identity per live objective. Existing rows get one here; every write
    # after this mints its own through ``SET_GOAL_SQL``.
    op.execute(
        """
        UPDATE agent_threads
        SET goal_id = gen_random_uuid()::text
        WHERE goal_objective IS NOT NULL AND goal_id IS NULL
        """
    )
    # ``blocked`` was a state that only ever carried a reason. Fold it into
    # ``paused`` with that reason kept as the detail, in one statement, so no
    # row is ever momentarily neither.
    op.execute(
        """
        UPDATE agent_threads
        SET goal_pause_detail = COALESCE(goal_pause_detail, goal_blocked_message),
            goal_pause_reason = CASE goal_blocked_code
                WHEN 'round-limit' THEN 'round_limit'
                ELSE 'needs_input'
            END,
            goal_state = 'paused'
        WHERE goal_state = 'blocked'
        """
    )
    # Every legacy stopped goal gets a reason and a sentence that is true of it.
    # The alternative — nulling the reason so the shape constraint still holds —
    # would show the owner a paused goal that cannot say why, which is the state
    # this whole migration exists to make impossible.
    op.execute(
        """
        UPDATE agent_threads
        SET goal_pause_reason = 'scheduling_failed',
            goal_pause_detail = COALESCE(
                goal_pause_detail,
                'this goal was paused under an earlier version of the feature; '
                'resume it to continue working toward the objective')
        WHERE goal_state = 'paused' AND goal_pause_reason IS NULL
        """
    )
    # A completed first-version goal was completed because the model said so.
    # Recording that is not a claim of independent verification; the column's
    # name says which basis it is. The turn that earned it is the last
    # automatic round that actually succeeded in that conversation -- the only
    # turn the old protocol could have completed on. Where there is no such
    # turn the row cannot answer "completed by what?", so the goal goes back to
    # ``active`` and is judged again rather than being shown as completed on no
    # evidence at all.
    op.execute(
        """
        UPDATE agent_threads t
        SET goal_completion_basis = 'model_reported',
            goal_completed_by_job_id = (
                SELECT j.id FROM agent_jobs j
                WHERE j.thread_id = t.id AND j.state = 'succeeded'
                ORDER BY j.turn_no DESC LIMIT 1
            )
        WHERE t.goal_state = 'complete' AND t.goal_completion_basis IS NULL
          AND EXISTS (
              SELECT 1 FROM agent_jobs j
              WHERE j.thread_id = t.id AND j.state = 'succeeded'
          )
        """
    )
    op.execute(
        """
        UPDATE agent_threads
        SET goal_state = 'active', goal_completion_basis = NULL,
            goal_completed_by_job_id = NULL
        WHERE goal_state = 'complete' AND goal_completed_by_job_id IS NULL
        """
    )
    op.execute("ALTER TABLE agent_threads DROP COLUMN goal_blocked_code")
    op.execute("ALTER TABLE agent_threads DROP COLUMN goal_blocked_message")
    op.execute("ALTER TABLE agent_threads DROP COLUMN goal_blocked_streak")
    op.execute(
        """
        ALTER TABLE agent_threads DROP CONSTRAINT ck_agent_threads_goal_shape,
            ADD CONSTRAINT ck_agent_threads_goal_state
                CHECK (goal_state IS NULL OR goal_state IN (""" + _STATES + """)),
            ADD CONSTRAINT ck_agent_threads_goal_pause_reason
                CHECK (goal_pause_reason IS NULL
                       OR goal_pause_reason IN (""" + _PAUSE_REASONS + """)),
            -- A goal that exists has an objective, an identity and a cap --
            -- all three or none. The *state* pairings are deliberately absent:
            -- these are checked per statement, not per transaction, so tying
            -- the state to the objective would make writing a fresh goal a
            -- multi-statement dance that cannot be ordered to satisfy the
            -- constraint at every step.
            ADD CONSTRAINT ck_agent_threads_goal_shape CHECK (
                (goal_objective IS NULL) = (goal_id IS NULL)
                AND (goal_objective IS NULL) = (goal_max_rounds IS NULL)
                AND (goal_pause_reason IS NOT NULL OR goal_pause_detail IS NULL)
                AND goal_repairs_used >= 0
            ),
            -- Why it stopped belongs to the stop. A row that says it is paused
            -- without saying why is the state the previous design kept drifting
            -- into, and it is unreadable to the owner it exists for.
            ADD CONSTRAINT ck_agent_threads_goal_paused_shape CHECK (
                goal_state <> 'paused'
                OR (goal_pause_reason IS NOT NULL AND goal_pause_detail IS NOT NULL)
            ),
            -- A goal that exists is always in one of the three states. ``NULL``
            -- state beside a live objective would be a goal the decision reads
            -- as "not active" and the owner's page cannot draw at all.
            ADD CONSTRAINT ck_agent_threads_goal_state_present CHECK (
                (goal_objective IS NULL) = (goal_state IS NULL)
            ),
            -- Completed means completed on a named basis, by a turn we can name.
            -- "Complete, but nobody knows how" is not a state this service has.
            ADD CONSTRAINT ck_agent_threads_goal_complete_shape CHECK (
                goal_state <> 'complete'
                OR (goal_completion_basis IS NOT NULL AND goal_completed_by_job_id IS NOT NULL)
            )
        """
    )
    # -- the per-turn snapshot and the report envelope ---------------------
    op.execute(
        """
        ALTER TABLE agent_jobs
            ADD COLUMN goal_id TEXT,
            ADD COLUMN goal_round_kind_new TEXT,
            ADD COLUMN goal_repair_round INT,
            ADD COLUMN goal_report_parse TEXT
                CHECK (goal_report_parse IS NULL
                       OR goal_report_parse IN ('absent', 'valid', 'invalid', 'unavailable')),
            ADD COLUMN goal_report_outcome TEXT
                CHECK (goal_report_outcome IS NULL
                       OR goal_report_outcome IN ('complete', 'needs_input')),
            ADD COLUMN goal_report_evidence JSONB,
            -- What the model said, in its own words. Kept apart from
            -- ``goal_report_detail``, which is what the *platform* has to say
            -- about reading the message: an owner reading "needs input" needs
            -- the model's sentence, and an owner reading "unreadable" needs the
            -- platform's reason, and one column cannot be both.
            ADD COLUMN goal_report_reason TEXT
                CHECK (goal_report_reason IS NULL
                       OR char_length(goal_report_reason) <= 2000),
            ADD COLUMN goal_report_digest TEXT,
            ADD COLUMN goal_report_detail TEXT
                CHECK (goal_report_detail IS NULL
                       OR char_length(goal_report_detail) <= 500),
            -- The conversation's input counter as this turn was created. A
            -- completion claim is honored only while this still equals the
            -- thread's: once a person has spoken, the claim describes a state
            -- of the conversation that no longer exists.
            ADD COLUMN input_seq_at_create BIGINT NOT NULL DEFAULT 0
        """
    )
    # Every turn that was already part of a goal inherits the objective it was
    # run against. That is a historical attribution, and it is the best one that
    # exists: the old schema did not keep a per-turn identity.
    op.execute(
        """
        UPDATE agent_jobs j
        SET goal_id = t.goal_id
        FROM agent_threads t
        WHERE j.thread_id = t.id AND j.goal_id IS NULL
          AND (j.turn_source = 'goal' OR j.goal_revision IS NOT NULL)
        """
    )
    # ``verify`` rounds do not exist under this protocol: a completion report is
    # honored directly, and the round that restates a malformed report is a
    # ``repair``. Old queued verify rounds become work rounds rather than being
    # cancelled — they were already paid for.
    op.execute(
        """
        UPDATE agent_jobs
        SET goal_round_kind_new = CASE goal_round_kind
                WHEN 'verify' THEN 'work'
                ELSE goal_round_kind
            END,
            goal_report_parse = CASE
                WHEN goal_report IS NULL THEN NULL
                WHEN goal_report IN ('complete', 'blocked') THEN 'valid'
                ELSE 'absent'
            END,
            goal_report_outcome = CASE
                WHEN goal_report = 'complete' THEN 'complete'
                WHEN goal_report = 'blocked' THEN 'needs_input'
                ELSE NULL
            END,
            -- The old column was the report itself; it is the only digest the
            -- previous protocol kept, and a digest is all this one needs to
            -- prove which message a report came from.
            goal_report_digest = COALESCE(goal_report, goal_reason)
        WHERE goal_report IS NOT NULL OR goal_round_kind IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE agent_jobs DROP CONSTRAINT agent_jobs_goal_round_kind_check,
            DROP COLUMN goal_round_kind,
            DROP COLUMN goal_report,
            DROP COLUMN goal_reason
        """
    )
    # ``0017`` named the old round-kind constraint inline too.
    op.execute(
        "ALTER TABLE agent_jobs DROP CONSTRAINT IF EXISTS agent_jobs_goal_round_kind_check"
    )
    op.execute("ALTER TABLE agent_jobs RENAME COLUMN goal_round_kind_new TO goal_round_kind")
    op.execute(
        """
        ALTER TABLE agent_jobs
            ADD CONSTRAINT ck_agent_jobs_goal_round_kind
                CHECK (goal_round_kind IS NULL OR goal_round_kind IN ('work', 'repair')),
            -- A parsed report belongs to a goal turn, and a goal turn names the
            -- objective it was created for. An implication, not an equivalence:
            -- a *queued* automatic round has the identity and no report yet,
            -- because the report is what its final message will produce.
            ADD CONSTRAINT ck_agent_jobs_goal_report_shape CHECK (
                goal_report_parse IS NULL OR goal_id IS NOT NULL
            )
        """
    )
    # Whether a runtime clipped the assistant message it stored. The goal report
    # lives at the *end* of a final message, and a truncated message is not
    # evidence that the model said nothing — it is a message whose ending was
    # removed. Recorded at the point the truncation happens so the reader does
    # not have to guess from a length.
    op.execute(
        """
        ALTER TABLE agent_thread_messages
            ADD COLUMN truncated BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    # The per-owner input sequence. It is read under the thread lock on every
    # finish, so nothing beyond the primary key is needed to find it.
    op.execute(
        """
        ALTER TABLE agent_threads
            ADD COLUMN user_input_seq BIGINT NOT NULL DEFAULT 0
        """
    )
    # -- the durable scheduling obligation ---------------------------------
    op.execute(
        """
        CREATE TABLE agent_goal_wakeups (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            goal_id TEXT,
            revision INT NOT NULL,
            trigger TEXT NOT NULL
                CHECK (trigger IN ('owner', 'finish')),
            trigger_ref TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'processed')),
            -- The turn this decision is waiting behind, when it yielded to one.
            waiting_job_id TEXT,
            attempts INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            detail TEXT,
            -- Taken while a consumer works the row, and released with it.
            lease_owner TEXT,
            lease_expires_at TIMESTAMPTZ,
            -- When a *failed* attempt may be tried again. Deliberately not the
            -- lease: a lease expiry means "this worker may have died, take it
            -- back", and a backoff means "do not touch this yet". One column
            -- for both would let a retry delay be mistaken for a dead worker.
            not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # One live wakeup per event. Replaying a finish or retrying an owner write
    # finds the row it already made rather than adding a second opinion about
    # it; a *processed* one stays as the audit record and does not block a
    # genuinely new event with the same name.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_goal_wakeups_event
            ON agent_goal_wakeups (thread_id, trigger, trigger_ref)
            WHERE state = 'pending'
        """
    )
    # The consumer's "what is due" query, and the recovery sweep's "what is this
    # conversation waiting on" query.
    op.execute(
        """
        CREATE INDEX idx_agent_goal_wakeups_due
            ON agent_goal_wakeups (not_before)
            WHERE state = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_goal_wakeups_thread
            ON agent_goal_wakeups (thread_id, created_at DESC)
        """
    )
    # Every terminal turn releases whatever was parked behind it, by job id.
    op.execute(
        """
        CREATE INDEX idx_agent_goal_wakeups_waiting
            ON agent_goal_wakeups (waiting_job_id)
            WHERE state = 'pending' AND waiting_job_id IS NOT NULL
        """
    )
    # The recovery sweep looks for active goals with no obligation left; this
    # index is what keeps that off a scan of every conversation.
    op.execute(
        """
        CREATE INDEX idx_agent_threads_active_goal
            ON agent_threads (goal_updated_at)
            WHERE goal_state = 'active'
        """
    )


def downgrade() -> None:
    """Put the previous four-state goal and its two-line report back."""
    op.execute("DROP INDEX idx_agent_threads_active_goal")
    op.execute("ALTER TABLE agent_thread_messages DROP COLUMN truncated")
    op.execute("DROP TABLE agent_goal_wakeups")
    op.execute(
        """
        ALTER TABLE agent_threads
            DROP COLUMN user_input_seq,
            DROP CONSTRAINT ck_agent_threads_goal_state,
            DROP CONSTRAINT ck_agent_threads_goal_pause_reason,
            DROP CONSTRAINT ck_agent_threads_goal_shape,
            DROP CONSTRAINT ck_agent_threads_goal_paused_shape,
            DROP CONSTRAINT ck_agent_threads_goal_complete_shape,
            DROP CONSTRAINT ck_agent_threads_goal_state_present,
            ADD COLUMN goal_blocked_code TEXT
                CHECK (goal_blocked_code IS NULL
                       OR goal_blocked_code IN ('round-limit', 'model-reported')),
            ADD COLUMN goal_blocked_message TEXT,
            ADD COLUMN goal_blocked_streak INT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE agent_threads
        SET goal_state = 'blocked',
            goal_blocked_code = CASE goal_pause_reason
                WHEN 'round_limit' THEN 'round-limit'
                ELSE 'model-reported'
            END,
            goal_blocked_message = goal_pause_detail
        WHERE goal_state = 'paused' AND goal_pause_reason IN ('round_limit', 'needs_input')
        """
    )
    op.execute(
        """
        ALTER TABLE agent_threads
            DROP COLUMN goal_id,
            DROP COLUMN goal_pause_detail,
            DROP COLUMN goal_completion_basis,
            DROP COLUMN goal_completed_by_job_id,
            DROP COLUMN goal_repairs_used
        """
    )
    # ``0017`` owns this constraint under this name, and its ``downgrade``
    # drops it by name: leaving it out would make downgrading past this
    # revision raise instead of restoring the previous release's schema.
    op.execute(
        """
        ALTER TABLE agent_threads
            ADD CONSTRAINT ck_agent_threads_goal_shape CHECK (
                (goal_objective IS NULL) = (goal_state IS NULL)
                AND (goal_objective IS NULL) = (goal_max_rounds IS NULL)
            )
        """
    )
    op.execute(
        """
        ALTER TABLE agent_jobs
            DROP CONSTRAINT ck_agent_jobs_goal_report_shape,
            DROP CONSTRAINT ck_agent_jobs_goal_round_kind,
            ADD COLUMN goal_report TEXT
                CHECK (goal_report IS NULL
                       OR goal_report IN ('continue', 'complete', 'blocked')),
            ADD COLUMN goal_reason TEXT
                CHECK (goal_reason IS NULL OR char_length(goal_reason) <= 2000)
        """
    )
    op.execute(
        """
        UPDATE agent_jobs
        SET goal_report = CASE goal_report_outcome
            WHEN 'complete' THEN 'complete'
            WHEN 'needs_input' THEN 'blocked'
            ELSE NULL
        END
        """
    )
    op.execute(
        """
        ALTER TABLE agent_jobs
            DROP COLUMN goal_id,
            DROP COLUMN goal_repair_round,
            DROP COLUMN goal_report_parse,
            DROP COLUMN goal_report_outcome,
            DROP COLUMN goal_report_evidence,
            DROP COLUMN goal_report_reason,
            DROP COLUMN goal_report_digest,
            DROP COLUMN goal_report_detail,
            DROP COLUMN input_seq_at_create
        """
    )
