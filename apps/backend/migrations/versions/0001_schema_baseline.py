"""The agent schema, rewritten once before the service takes real traffic.

This replaces the twenty-eight revisions that accumulated between the split
from the gateway (2026-08-04) and the pre-launch schema review (2026-09-10).
Nothing that deployed against those revisions survives: the staging database
held one internal user's test runs and the production database held the
retired on-call bot's, and both are rebuilt from this file rather than
upgraded. A database still stamped with one of the old revision ids therefore
fails ``alembic upgrade head`` loudly — "can't locate revision" — instead of
being silently mistaken for a migrated one, which is why this revision does
not reuse the old ``0001_baseline`` id.

**What changed, and why it could not be a twenty-ninth revision.**

The old chain carried the gateway's schema verbatim, then added every feature
as a nullable column so that a control plane one version behind could keep
writing during a rolling deploy. Once the rollouts were over the compatibility
stayed: two ``agent_thread_sandboxes``-era tables nothing reads, a
``legacy_1to1`` sandbox mode no row could take, a cutover flag on every job,
and two triggers whose only purpose was to assign ``native_turn_no`` on behalf
of a writer that no longer exists — at the cost of a sequential scan of every
snapshot on every job insert. None of it can be removed one revision at a
time without a further round of "old writer" guards, so it is removed here,
in one place, and the guards go with it.

The other half is what the review's probes showed the database *accepting*: a
job whose thread does not exist, a job owned by someone other than its thread,
a ``current_attempt_id`` pointing at another job's attempt, an event filed
under an attempt of a different job, a membership attaching one user's chat to
another user's sandbox, a snapshot including a job that does not exist. Every
one of those was prevented by store code, and only by store code. The
constraints below make the database refuse them, so a new write path, an
import script or one missed check cannot produce a row the readers were never
written to handle.

**How the constraints are shaped.** Wherever two identifiers on a row must
belong to the same chain of execution, the pair is a composite foreign key
against a unique pair on the parent: ``(thread_id, id)`` on jobs is what lets
a message, a membership, a parent link and a fork source all prove they name a
job *of that thread*; ``(job_id, id)`` and ``(thread_id, id)`` on attempts do
the same for events, resource rows and the job's own ``current_attempt_id``.
Ownership travels the same way: ``(id, user_id)`` on threads and sandboxes is
referenced by jobs, memberships and forks, so a row cannot cross tenants
without the database noticing. State vocabularies are CHECKs, generations and
ordinals are positive, and a succeeded job must carry its native turn ordinal
— the writer that settles a turn assigns it before the terminal transition,
and the constraint is what replaces the trigger that used to guess it.

Snapshot membership was a JSONB array; it is now ``agent_snapshot_jobs``,
which is what let it stop being unindexable. The anchor job must be one of the
included jobs, and that is a deferred foreign key from the snapshot back into
the association table: circular by construction, checked at commit.

A residency's ``thread_id`` is not a foreign key here: a fork established the
target chat's residency while materializing it, before the target thread row
existed. Revision ``0004_residency_on_membership`` folds the residency into
the membership row, which does carry the key, and gives a fork its target
thread from preparation. ``sandbox_id`` on the residency row is one.

**What deliberately stays loose.** ``user_id`` is an opaque identifier minted
by the gateway's identity service, so no table here references a users table
— there is none to reference. Hosts are labels on sandboxes and residencies
rather than foreign keys: forgetting a decommissioned machine is operator
housekeeping and must not fail because a stopped container still names it
(``remove_host`` and ``forget_runner_host`` document that choice). Attempts
keep ``ON DELETE SET NULL`` to hosts for the same reason. And ``repo``,
``setup_script``, ``execution_profile`` and ``mcp_servers`` remain on every
job although they are written once per thread and inherited turn by turn:
moving them is a store refactor with its own review, not a baseline decision
(revision ``0003_thread_level_config`` is that review).

Revision ID: 0001_schema_baseline
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None

# Creation order is dependency order. Circular references (jobs ⇄ attempts,
# snapshots ⇄ snapshot_jobs) are added as ALTERs once both sides exist.
_STATEMENTS: tuple[str, ...] = (
    # ── Host pool ─────────────────────────────────────────────────────
    """
    CREATE TABLE agent_runner_hosts (
        host TEXT PRIMARY KEY,
        last_worker_id TEXT,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_heartbeat_at TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'online'
            CHECK (status IN ('enrolling', 'online', 'draining', 'disabled', 'offline')),
        labels JSONB NOT NULL DEFAULT '{}'::jsonb,
        capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
        slots_total INTEGER CHECK (slots_total IS NULL OR slots_total > 0),
        slots_used INTEGER NOT NULL DEFAULT 0 CHECK (slots_used >= 0),
        runner_version TEXT,
        broker_url TEXT,
        credential_hash TEXT,
        sandbox_runtime TEXT,
        sandbox_image_digest TEXT,
        runtime_versions JSONB,
        runtime_versions_available JSONB
    )
    """,
    "CREATE INDEX idx_agent_runner_hosts_online ON agent_runner_hosts (host) WHERE status = 'online'",
    """
    CREATE TABLE agent_runner_policy (
        id BOOLEAN PRIMARY KEY DEFAULT TRUE,
        active_host TEXT REFERENCES agent_runner_hosts (host) ON DELETE RESTRICT,
        runtime_versions JSONB,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT agent_runner_policy_singleton CHECK (id)
    )
    """,
    "INSERT INTO agent_runner_policy (id, active_host) VALUES (TRUE, NULL)",
    """
    CREATE TABLE agent_host_enroll_tokens (
        token_hash TEXT PRIMARY KEY,
        host TEXT NOT NULL,
        issued_by TEXT,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ,
        consumed_host TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX idx_agent_host_enroll_tokens_expiry ON agent_host_enroll_tokens (expires_at)",
    # ── Conversations and turns ───────────────────────────────────────
    """
    CREATE TABLE agent_threads (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        repo TEXT NOT NULL,
        title TEXT NOT NULL,
        title_source TEXT NOT NULL DEFAULT 'manual'
            CHECK (title_source IN ('fallback', 'generated', 'manual', 'inherited')),
        archived_at TIMESTAMPTZ,
        pinned_at TIMESTAMPTZ,
        deleted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_agent_threads_owner UNIQUE (id, user_id)
    )
    """,
    """
    CREATE INDEX idx_agent_threads_archive_history
        ON agent_threads (user_id, archived_at DESC, id DESC)
        WHERE deleted_at IS NULL
    """,
    """
    CREATE TABLE agent_jobs (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        parent_job_id TEXT,
        turn_no INTEGER NOT NULL CHECK (turn_no > 0),
        native_turn_no INTEGER CHECK (native_turn_no > 0),
        fork_source_job_id TEXT REFERENCES agent_jobs (id),
        repo TEXT NOT NULL,
        base_sha TEXT,
        task_prompt TEXT NOT NULL,
        setup_script TEXT,
        runtime TEXT NOT NULL,
        model TEXT NOT NULL,
        execution_profile TEXT NOT NULL DEFAULT 'trusted-v1'
            CHECK (execution_profile IN ('trusted-v1', 'untrusted-kata-v1')),
        reasoning_effort TEXT,
        state TEXT NOT NULL DEFAULT 'queued'
            CHECK (state IN ('queued', 'waiting', 'running', 'succeeded', 'failed', 'cancelled')),
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        terminal_resume_pending BOOLEAN NOT NULL DEFAULT FALSE,
        current_attempt_id BIGINT,
        published_pr_url TEXT,
        published_commit_sha TEXT,
        detail TEXT,
        metadata JSONB,
        mcp_servers TEXT[],
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_agent_jobs_thread UNIQUE (thread_id, id),
        CONSTRAINT uq_agent_jobs_thread_turn UNIQUE (thread_id, turn_no),
        CONSTRAINT fk_agent_jobs_thread_owner
            FOREIGN KEY (thread_id, user_id) REFERENCES agent_threads (id, user_id),
        CONSTRAINT fk_agent_jobs_parent_same_thread
            FOREIGN KEY (thread_id, parent_job_id) REFERENCES agent_jobs (thread_id, id),
        CONSTRAINT ck_agent_jobs_root_turn_has_no_parent
            CHECK ((turn_no = 1) = (parent_job_id IS NULL)),
        CONSTRAINT ck_agent_jobs_succeeded_has_native_turn
            CHECK (state <> 'succeeded' OR native_turn_no IS NOT NULL),
        CONSTRAINT ck_agent_jobs_fork_copy_is_terminal
            CHECK (fork_source_job_id IS NULL OR state IN ('succeeded', 'failed', 'cancelled'))
    )
    """,
    "CREATE INDEX idx_agent_jobs_queued ON agent_jobs (created_at) WHERE state = 'queued'",
    "CREATE INDEX idx_agent_jobs_waiting_parent ON agent_jobs (parent_job_id) WHERE state = 'waiting'",
    """
    CREATE INDEX idx_agent_jobs_pending_thread ON agent_jobs (thread_id)
        WHERE state IN ('waiting', 'queued')
    """,
    "CREATE INDEX idx_agent_jobs_user ON agent_jobs (user_id, created_at DESC)",
    "CREATE INDEX idx_agent_jobs_thread_activity ON agent_jobs (thread_id, updated_at DESC)",
    "CREATE INDEX idx_agent_jobs_user_repo ON agent_jobs (user_id, repo, created_at DESC)",
    """
    CREATE UNIQUE INDEX idx_agent_jobs_native_turn ON agent_jobs (thread_id, native_turn_no)
        WHERE native_turn_no IS NOT NULL
    """,
    """
    CREATE INDEX idx_agent_jobs_fork_source ON agent_jobs (fork_source_job_id)
        WHERE fork_source_job_id IS NOT NULL
    """,
    """
    CREATE INDEX idx_agent_jobs_current_attempt ON agent_jobs (current_attempt_id)
        WHERE current_attempt_id IS NOT NULL
    """,
    # ── Execution ─────────────────────────────────────────────────────
    """
    CREATE TABLE agent_attempts (
        id BIGSERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
        lease_owner TEXT NOT NULL,
        lease_generation BIGINT NOT NULL CHECK (lease_generation > 0),
        lease_expires_at TIMESTAMPTZ NOT NULL,
        terminal_ready BOOLEAN NOT NULL DEFAULT FALSE,
        base_sha TEXT,
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'finished', 'superseded', 'aborted')),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        host_id TEXT REFERENCES agent_runner_hosts (host) ON DELETE SET NULL,
        grant_id TEXT,
        sandbox_runtime TEXT,
        sandbox_image_digest TEXT,
        runtime_version TEXT,
        host_kernel TEXT,
        guest_kernel TEXT,
        continuity_mode TEXT CHECK (continuity_mode IN ('hot', 'warm_native')),
        context_status TEXT CHECK (context_status IN ('new', 'live', 'restored', 'lost')),
        CONSTRAINT uq_agent_attempts_job_no UNIQUE (job_id, attempt_no),
        CONSTRAINT uq_agent_attempts_job UNIQUE (job_id, id),
        CONSTRAINT uq_agent_attempts_thread UNIQUE (thread_id, id),
        CONSTRAINT fk_agent_attempts_job_thread
            FOREIGN KEY (job_id, thread_id) REFERENCES agent_jobs (id, thread_id),
        CONSTRAINT ck_agent_attempts_finished_when_not_running
            CHECK ((status = 'running') = (finished_at IS NULL))
    )
    """,
    "CREATE INDEX idx_agent_attempts_expiry ON agent_attempts (lease_expires_at) WHERE status = 'running'",
    "CREATE INDEX idx_agent_attempts_grant ON agent_attempts (grant_id) WHERE grant_id IS NOT NULL",
    "CREATE INDEX idx_agent_attempts_host ON agent_attempts (host_id) WHERE host_id IS NOT NULL",
    """
    ALTER TABLE agent_jobs
        ADD CONSTRAINT fk_agent_jobs_current_attempt
        FOREIGN KEY (id, current_attempt_id) REFERENCES agent_attempts (job_id, id)
    """,
    """
    CREATE TABLE agent_job_events (
        id BIGSERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        attempt_id BIGINT NOT NULL,
        seq INTEGER NOT NULL CHECK (seq > 0),
        client_seq BIGINT,
        event_type TEXT NOT NULL CHECK (event_type <> ''),
        payload JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_agent_job_events_attempt_seq UNIQUE (attempt_id, seq),
        CONSTRAINT fk_agent_job_events_attempt_job
            FOREIGN KEY (job_id, attempt_id) REFERENCES agent_attempts (job_id, id)
    )
    """,
    "CREATE INDEX idx_agent_job_events_job ON agent_job_events (job_id, id)",
    """
    CREATE UNIQUE INDEX uq_agent_job_events_attempt_client_seq
        ON agent_job_events (attempt_id, client_seq) WHERE client_seq IS NOT NULL
    """,
    """
    CREATE TABLE agent_thread_messages (
        id BIGSERIAL PRIMARY KEY,
        thread_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        source_event_id BIGINT UNIQUE REFERENCES agent_job_events (id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT fk_agent_thread_messages_job_thread
            FOREIGN KEY (thread_id, job_id) REFERENCES agent_jobs (thread_id, id)
    )
    """,
    "CREATE INDEX idx_agent_thread_messages_thread ON agent_thread_messages (thread_id, id)",
    """
    CREATE UNIQUE INDEX idx_agent_thread_messages_user_job
        ON agent_thread_messages (job_id) WHERE role = 'user'
    """,
    # ── Retained sandboxes, native sessions and forks ─────────────────
    """
    CREATE TABLE agent_sandboxes (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        origin_thread_id TEXT NOT NULL UNIQUE,
        generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
        host TEXT NOT NULL,
        container_id TEXT UNIQUE,
        container_name TEXT NOT NULL UNIQUE,
        runtime_id TEXT NOT NULL,
        runtime_version TEXT,
        sandbox_image_digest TEXT,
        execution_profile TEXT NOT NULL
            CHECK (execution_profile IN ('trusted-v1', 'untrusted-kata-v1')),
        memory_limit TEXT NOT NULL DEFAULT '4g',
        cpu_limit TEXT NOT NULL DEFAULT '2',
        stop_timeout_seconds INTEGER NOT NULL DEFAULT 10
            CHECK (stop_timeout_seconds BETWEEN 1 AND 300),
        state TEXT NOT NULL DEFAULT 'provisioning'
            CHECK (state IN ('provisioning', 'starting', 'running', 'stopping',
                             'stopped', 'stopped_dirty', 'lost')),
        last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        idle_deadline TIMESTAMPTZ,
        idle_timeout_seconds DOUBLE PRECISION NOT NULL DEFAULT 1800
            CHECK (idle_timeout_seconds > 0),
        active_operation_count INTEGER NOT NULL DEFAULT 0 CHECK (active_operation_count >= 0),
        resident_slot_capacity INTEGER NOT NULL DEFAULT 8
            CHECK (resident_slot_capacity BETWEEN 2 AND 128),
        workspace_epoch BIGINT NOT NULL DEFAULT 0 CHECK (workspace_epoch >= 0),
        snapshot_state TEXT NOT NULL DEFAULT 'quiescent'
            CHECK (snapshot_state IN ('quiescent', 'pending', 'snapshotting', 'importing', 'dirty')),
        published_base_sha TEXT,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_agent_sandboxes_owner UNIQUE (id, user_id),
        CONSTRAINT fk_agent_sandboxes_origin_thread_owner
            FOREIGN KEY (origin_thread_id, user_id) REFERENCES agent_threads (id, user_id)
    )
    """,
    "CREATE INDEX idx_agent_sandboxes_host_state ON agent_sandboxes (host, state)",
    """
    CREATE INDEX idx_agent_sandboxes_idle ON agent_sandboxes (idle_deadline)
        WHERE state = 'running' AND active_operation_count = 0
    """,
    """
    CREATE TABLE agent_session_forks (
        id TEXT PRIMARY KEY,
        generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
        user_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        sandbox_id TEXT NOT NULL,
        sandbox_generation BIGINT NOT NULL CHECK (sandbox_generation > 0),
        container_id TEXT NOT NULL,
        runtime_id TEXT NOT NULL,
        runtime_version TEXT,
        source_thread_id TEXT NOT NULL,
        source_job_id TEXT NOT NULL,
        source_native_session_id TEXT NOT NULL,
        source_activation_generation BIGINT NOT NULL CHECK (source_activation_generation >= 0),
        target_thread_id TEXT NOT NULL UNIQUE,
        target_native_session_id TEXT,
        target_anchor_job_id TEXT,
        target_resident_slot_id INTEGER CHECK (target_resident_slot_id >= 0),
        source_residency_generation BIGINT CHECK (source_residency_generation > 0),
        target_residency_generation BIGINT CHECK (target_residency_generation > 0),
        source_process_generation BIGINT CHECK (source_process_generation > 0),
        target_process_generation BIGINT CHECK (target_process_generation > 0),
        previous_source_grant_id TEXT,
        previous_target_grant_id TEXT,
        state TEXT NOT NULL DEFAULT 'preparing'
            CHECK (state IN ('preparing', 'materialized', 'ready', 'failed')),
        host_receipt JSONB,
        last_error TEXT,
        coordinator_token TEXT,
        coordinator_lease_expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        CONSTRAINT uq_agent_session_forks_idempotency UNIQUE (user_id, idempotency_key),
        CONSTRAINT fk_agent_session_forks_sandbox_owner
            FOREIGN KEY (sandbox_id, user_id) REFERENCES agent_sandboxes (id, user_id),
        CONSTRAINT fk_agent_session_forks_source_thread_owner
            FOREIGN KEY (source_thread_id, user_id) REFERENCES agent_threads (id, user_id),
        CONSTRAINT fk_agent_session_forks_source_job
            FOREIGN KEY (source_thread_id, source_job_id) REFERENCES agent_jobs (thread_id, id),
        CONSTRAINT fk_agent_session_forks_target_anchor
            FOREIGN KEY (target_thread_id, target_anchor_job_id)
            REFERENCES agent_jobs (thread_id, id)
    )
    """,
    """
    CREATE UNIQUE INDEX idx_agent_session_forks_source_active
        ON agent_session_forks (source_thread_id)
        WHERE state IN ('preparing', 'materialized')
    """,
    """
    CREATE UNIQUE INDEX idx_agent_session_forks_target_slot_active
        ON agent_session_forks (sandbox_id, target_resident_slot_id)
        WHERE state IN ('preparing', 'materialized')
    """,
    """
    CREATE TABLE agent_thread_sandbox_memberships (
        thread_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        sandbox_id TEXT NOT NULL,
        native_session_id TEXT,
        native_state TEXT NOT NULL DEFAULT 'new'
            CHECK (native_state IN ('new', 'ready', 'fork_preparing', 'lost')),
        context_status TEXT NOT NULL DEFAULT 'new'
            CHECK (context_status IN ('new', 'live', 'restored', 'lost')),
        activation_generation BIGINT NOT NULL DEFAULT 0 CHECK (activation_generation >= 0),
        process_generation BIGINT NOT NULL DEFAULT 0 CHECK (process_generation >= 0),
        residency_generation BIGINT NOT NULL DEFAULT 0 CHECK (residency_generation >= 0),
        resident_state TEXT NOT NULL DEFAULT 'stopped'
            CHECK (resident_state IN ('stopped', 'starting', 'live', 'restarting', 'lost')),
        resident_slot_id INTEGER CHECK (resident_slot_id >= 0),
        active_attempt_id BIGINT,
        admitted_job_id TEXT,
        admitted_at TIMESTAMPTZ,
        last_settled_job_id TEXT,
        forked_from_thread_id TEXT REFERENCES agent_threads (id),
        fork_anchor_job_id TEXT,
        fork_operation_id TEXT REFERENCES agent_session_forks (id),
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT fk_agent_memberships_thread_owner
            FOREIGN KEY (thread_id, user_id) REFERENCES agent_threads (id, user_id)
            ON DELETE CASCADE,
        CONSTRAINT fk_agent_memberships_sandbox_owner
            FOREIGN KEY (sandbox_id, user_id) REFERENCES agent_sandboxes (id, user_id)
            ON DELETE RESTRICT,
        CONSTRAINT fk_agent_memberships_active_attempt
            FOREIGN KEY (thread_id, active_attempt_id) REFERENCES agent_attempts (thread_id, id),
        CONSTRAINT fk_agent_memberships_admitted_job
            FOREIGN KEY (thread_id, admitted_job_id) REFERENCES agent_jobs (thread_id, id),
        CONSTRAINT fk_agent_memberships_last_settled_job
            FOREIGN KEY (thread_id, last_settled_job_id) REFERENCES agent_jobs (thread_id, id),
        CONSTRAINT fk_agent_memberships_fork_anchor
            FOREIGN KEY (forked_from_thread_id, fork_anchor_job_id)
            REFERENCES agent_jobs (thread_id, id),
        CONSTRAINT ck_agent_memberships_fork_lineage_complete
            CHECK ((forked_from_thread_id IS NULL) = (fork_anchor_job_id IS NULL)),
        CONSTRAINT ck_agent_memberships_admission_stamped
            CHECK ((admitted_job_id IS NULL) = (admitted_at IS NULL))
    )
    """,
    "CREATE INDEX idx_agent_memberships_sandbox ON agent_thread_sandbox_memberships (sandbox_id)",
    """
    CREATE UNIQUE INDEX idx_agent_memberships_native_session
        ON agent_thread_sandbox_memberships (sandbox_id, native_session_id)
        WHERE native_session_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX idx_agent_memberships_resident_slot
        ON agent_thread_sandbox_memberships (sandbox_id, resident_slot_id)
        WHERE resident_slot_id IS NOT NULL
    """,
    """
    CREATE TABLE agent_native_session_edges (
        id BIGSERIAL PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES agent_session_forks (id) ON DELETE RESTRICT,
        sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes (id) ON DELETE RESTRICT,
        source_thread_id TEXT NOT NULL REFERENCES agent_threads (id) ON DELETE RESTRICT,
        target_thread_id TEXT NOT NULL UNIQUE REFERENCES agent_threads (id) ON DELETE RESTRICT,
        source_native_session_id TEXT NOT NULL,
        target_native_session_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_agent_native_session_edges_target UNIQUE (sandbox_id, target_native_session_id),
        CONSTRAINT ck_agent_native_session_edges_distinct
            CHECK (source_native_session_id <> target_native_session_id)
    )
    """,
    """
    CREATE TABLE agent_thread_residencies (
        thread_id TEXT PRIMARY KEY,
        generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
        host TEXT NOT NULL,
        sandbox_id TEXT REFERENCES agent_sandboxes (id),
        runtime_id TEXT NOT NULL,
        runtime_version TEXT,
        status TEXT NOT NULL DEFAULT 'hot' CHECK (status IN ('hot', 'draining', 'none')),
        lease_expires_at TIMESTAMPTZ NOT NULL,
        grant_id TEXT,
        ledger_binding_id TEXT,
        established_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX idx_agent_thread_residencies_hot
        ON agent_thread_residencies (host, lease_expires_at) WHERE status = 'hot'
    """,
    # ── Publication ───────────────────────────────────────────────────
    """
    CREATE TABLE agent_sandbox_snapshots (
        id BIGSERIAL PRIMARY KEY,
        sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes (id) ON DELETE CASCADE,
        workspace_epoch BIGINT NOT NULL CHECK (workspace_epoch > 0),
        anchor_job_id TEXT NOT NULL REFERENCES agent_jobs (id) ON DELETE RESTRICT,
        base_sha TEXT NOT NULL,
        content_sha256 TEXT,
        patch_content TEXT,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'capturing', 'captured', 'publishing', 'published', 'failed')),
        published_commit_sha TEXT,
        published_pr_url TEXT,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        captured_at TIMESTAMPTZ,
        published_at TIMESTAMPTZ,
        CONSTRAINT uq_agent_sandbox_snapshots_epoch UNIQUE (sandbox_id, workspace_epoch)
    )
    """,
    """
    CREATE UNIQUE INDEX idx_agent_sandbox_snapshots_pending
        ON agent_sandbox_snapshots (sandbox_id) WHERE state = 'pending'
    """,
    """
    CREATE UNIQUE INDEX idx_agent_sandbox_snapshots_capturing
        ON agent_sandbox_snapshots (sandbox_id) WHERE state = 'capturing'
    """,
    "CREATE INDEX idx_agent_sandbox_snapshots_publish ON agent_sandbox_snapshots (state, updated_at)",
    """
    CREATE TABLE agent_snapshot_jobs (
        snapshot_id BIGINT NOT NULL REFERENCES agent_sandbox_snapshots (id) ON DELETE CASCADE,
        job_id TEXT NOT NULL REFERENCES agent_jobs (id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (snapshot_id, job_id)
    )
    """,
    "CREATE INDEX idx_agent_snapshot_jobs_job ON agent_snapshot_jobs (job_id)",
    """
    ALTER TABLE agent_sandbox_snapshots
        ADD CONSTRAINT fk_agent_sandbox_snapshots_anchor_included
        FOREIGN KEY (id, anchor_job_id) REFERENCES agent_snapshot_jobs (snapshot_id, job_id)
        DEFERRABLE INITIALLY DEFERRED
    """,
    # ── Inference attribution and resource accounting ─────────────────
    """
    CREATE TABLE agent_attempt_ledger (
        id BIGSERIAL PRIMARY KEY,
        attempt_id BIGINT NOT NULL REFERENCES agent_attempts (id) ON DELETE CASCADE,
        grant_id TEXT NOT NULL,
        residency_generation BIGINT NOT NULL CHECK (residency_generation > 0),
        binding_id TEXT NOT NULL,
        window_since TIMESTAMPTZ NOT NULL,
        snapshot JSONB,
        queried_at TIMESTAMPTZ,
        last_error_code TEXT,
        next_refresh_at TIMESTAMPTZ,
        last_refresh_attempt_at TIMESTAMPTZ,
        refresh_generation BIGINT NOT NULL DEFAULT 0 CHECK (refresh_generation >= 0),
        refresh_lease_expires_at TIMESTAMPTZ,
        terminal_refresh_count INTEGER NOT NULL DEFAULT 0 CHECK (terminal_refresh_count >= 0),
        CONSTRAINT uq_agent_attempt_ledger_grant UNIQUE (attempt_id, grant_id)
    )
    """,
    """
    CREATE INDEX idx_agent_attempt_ledger_due ON agent_attempt_ledger (next_refresh_at)
        WHERE next_refresh_at IS NOT NULL
    """,
    """
    CREATE INDEX idx_agent_attempt_ledger_initial ON agent_attempt_ledger (attempt_id)
        WHERE terminal_refresh_count = 0
    """,
    """
    CREATE INDEX idx_agent_attempt_ledger_leased ON agent_attempt_ledger (refresh_lease_expires_at)
        WHERE refresh_lease_expires_at IS NOT NULL
    """,
    """
    CREATE TABLE agent_attempt_resources (
        attempt_id BIGINT PRIMARY KEY REFERENCES agent_attempts (id) ON DELETE CASCADE,
        job_id TEXT NOT NULL,
        cpu_usec BIGINT,
        mem_peak_bytes BIGINT,
        mem_avg_bytes BIGINT,
        io_read_bytes BIGINT,
        io_write_bytes BIGINT,
        net_rx_bytes BIGINT,
        net_tx_bytes BIGINT,
        sample_count INTEGER NOT NULL DEFAULT 0,
        coverage DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (coverage >= 0 AND coverage <= 1),
        sampler_version TEXT NOT NULL,
        tool_calls_truncated BOOLEAN NOT NULL DEFAULT FALSE,
        tool_time_ms BIGINT,
        process_observer TEXT,
        process_observation_complete BOOLEAN NOT NULL DEFAULT FALSE,
        process_events_lost BIGINT NOT NULL DEFAULT 0,
        processes_truncated BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT fk_agent_attempt_resources_attempt_job
            FOREIGN KEY (attempt_id, job_id) REFERENCES agent_attempts (id, job_id)
    )
    """,
    "CREATE INDEX idx_agent_attempt_resources_job ON agent_attempt_resources (job_id)",
    """
    CREATE TABLE agent_tool_call_resources (
        attempt_id BIGINT NOT NULL REFERENCES agent_attempts (id) ON DELETE CASCADE,
        call_seq INTEGER NOT NULL,
        tool_use_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        timing_source TEXT NOT NULL DEFAULT 'message',
        duration_ms BIGINT,
        sample_count INTEGER NOT NULL DEFAULT 0,
        overlap_n INTEGER NOT NULL DEFAULT 1 CHECK (overlap_n >= 1),
        measured_span_ms BIGINT,
        cpu_usec_exclusive BIGINT,
        cpu_usec_shared BIGINT,
        mem_peak_exclusive_bytes BIGINT,
        mem_peak_shared_bytes BIGINT,
        process_count INTEGER NOT NULL DEFAULT 0 CHECK (process_count >= 0),
        process_cpu_usec BIGINT CHECK (process_cpu_usec IS NULL OR process_cpu_usec >= 0),
        process_mem_peak_bytes BIGINT CHECK (process_mem_peak_bytes IS NULL OR process_mem_peak_bytes >= 0),
        process_io_read_bytes BIGINT CHECK (process_io_read_bytes IS NULL OR process_io_read_bytes >= 0),
        process_io_write_bytes BIGINT CHECK (process_io_write_bytes IS NULL OR process_io_write_bytes >= 0),
        process_resources_partial BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (attempt_id, call_seq),
        CONSTRAINT ck_agent_tool_call_resources_exclusive_xor_shared CHECK (NOT (
            (cpu_usec_exclusive IS NOT NULL OR mem_peak_exclusive_bytes IS NOT NULL)
            AND (cpu_usec_shared IS NOT NULL OR mem_peak_shared_bytes IS NOT NULL)
        )),
        CONSTRAINT ck_agent_tool_call_resources_overlap_columns CHECK (
            (overlap_n = 1 AND cpu_usec_shared IS NULL AND mem_peak_shared_bytes IS NULL)
            OR (overlap_n > 1 AND cpu_usec_exclusive IS NULL AND mem_peak_exclusive_bytes IS NULL)
        ),
        CONSTRAINT ck_agent_tool_call_resources_needs_two_samples CHECK (
            sample_count >= 2
            OR (measured_span_ms IS NULL AND cpu_usec_exclusive IS NULL AND cpu_usec_shared IS NULL
                AND mem_peak_exclusive_bytes IS NULL AND mem_peak_shared_bytes IS NULL)
        ),
        CONSTRAINT ck_agent_tool_call_resources_process_resources_need_processes CHECK (
            process_count > 0
            OR (process_cpu_usec IS NULL AND process_mem_peak_bytes IS NULL
                AND process_io_read_bytes IS NULL AND process_io_write_bytes IS NULL
                AND NOT process_resources_partial)
        )
    )
    """,
    """
    CREATE TABLE agent_process_observations (
        attempt_id BIGINT NOT NULL REFERENCES agent_attempts (id) ON DELETE CASCADE,
        process_seq INTEGER NOT NULL,
        uid BIGINT NOT NULL CHECK (uid >= 0),
        pid BIGINT NOT NULL,
        tgid BIGINT NOT NULL,
        ppid BIGINT CHECK (ppid IS NULL OR ppid > 0),
        comm TEXT NOT NULL,
        executable TEXT,
        started_offset_ms BIGINT NOT NULL CHECK (started_offset_ms >= 0),
        duration_ms BIGINT CHECK (duration_ms IS NULL OR duration_ms >= 0),
        exit_code INTEGER CHECK (exit_code IS NULL OR (exit_code >= 0 AND exit_code <= 255)),
        observed_fork BOOLEAN NOT NULL DEFAULT FALSE,
        observed_exec BOOLEAN NOT NULL DEFAULT FALSE,
        observed_exit BOOLEAN NOT NULL DEFAULT FALSE,
        tool_call_seqs INTEGER[] NOT NULL DEFAULT '{}',
        cpu_usec BIGINT CHECK (cpu_usec IS NULL OR cpu_usec >= 0),
        mem_peak_bytes BIGINT CHECK (mem_peak_bytes IS NULL OR mem_peak_bytes >= 0),
        io_read_bytes BIGINT CHECK (io_read_bytes IS NULL OR io_read_bytes >= 0),
        io_write_bytes BIGINT CHECK (io_write_bytes IS NULL OR io_write_bytes >= 0),
        io_rchar_bytes BIGINT CHECK (io_rchar_bytes IS NULL OR io_rchar_bytes >= 0),
        io_wchar_bytes BIGINT CHECK (io_wchar_bytes IS NULL OR io_wchar_bytes >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (attempt_id, process_seq),
        CONSTRAINT ck_agent_process_observations_pids CHECK (pid > 0 AND tgid > 0)
    )
    """,
    # ── Identity ──────────────────────────────────────────────────────
    """
    CREATE TABLE agent_identity_namespace (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        gateway_issuer TEXT NOT NULL CHECK (length(gateway_issuer) > 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ── Integrations and administration ───────────────────────────────
    """
    CREATE TABLE agent_repo_grants (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        installation_id BIGINT NOT NULL,
        account_login TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_agent_repo_grants_installation UNIQUE (user_id, installation_id)
    )
    """,
    "CREATE INDEX idx_agent_repo_grants_user ON agent_repo_grants (user_id)",
    """
    CREATE TABLE agent_oauth_states (
        state_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider <> ''),
        code_verifier_ciphertext TEXT,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX idx_agent_oauth_states_expiry ON agent_oauth_states (expires_at)",
    """
    CREATE TABLE agent_gitlab_connections (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL UNIQUE,
        external_user_id BIGINT NOT NULL,
        username TEXT NOT NULL,
        display_name TEXT,
        web_url TEXT,
        access_token_ciphertext TEXT NOT NULL,
        refresh_token_ciphertext TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE agent_mcp_connections (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        server TEXT NOT NULL,
        account_label TEXT,
        access_token_ciphertext TEXT NOT NULL,
        refresh_token_ciphertext TEXT,
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_agent_mcp_connections_server UNIQUE (user_id, server)
    )
    """,
    """
    CREATE TABLE agent_admin_audit_log (
        id BIGSERIAL PRIMARY KEY,
        action TEXT NOT NULL,
        admin_ip TEXT NOT NULL,
        target_user_id TEXT,
        details JSONB,
        success BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX idx_agent_admin_audit_created ON agent_admin_audit_log (created_at DESC)",
)

# Reverse dependency order, for the downgrade. CASCADE covers the circular
# references, whose ALTERs have no natural place in a linear order.
_TABLES: tuple[str, ...] = (
    "agent_identity_namespace",
    "agent_admin_audit_log",
    "agent_mcp_connections",
    "agent_gitlab_connections",
    "agent_oauth_states",
    "agent_repo_grants",
    "agent_process_observations",
    "agent_tool_call_resources",
    "agent_attempt_resources",
    "agent_attempt_ledger",
    "agent_snapshot_jobs",
    "agent_sandbox_snapshots",
    "agent_thread_residencies",
    "agent_native_session_edges",
    "agent_thread_sandbox_memberships",
    "agent_session_forks",
    "agent_sandboxes",
    "agent_thread_messages",
    "agent_job_events",
    "agent_attempts",
    "agent_jobs",
    "agent_threads",
    "agent_host_enroll_tokens",
    "agent_runner_policy",
    "agent_runner_hosts",
)


def upgrade() -> None:
    """Create the whole agent schema and seed the singleton policy row."""
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop every table this baseline created."""
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
