"""Postgres implementation of the conversation write port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.execution import QUEUED, SUCCEEDED
from cloud_agent.domain.ids import new_job_id, new_thread_id
from cloud_agent.domain.pending_messages import QUEUE_PAUSED_ARCHIVED
from cloud_agent.domain.read_receipts import READ_RECEIPT_BATCH_SIZE
from cloud_agent.domain.requests import check_client_request
from cloud_agent.execution_profiles import default_execution_profile
from cloud_agent.goals.models import GOAL_REPORT_PROTOCOL
from cloud_agent.storage._sql.attachments import claim_attachments
from cloud_agent.storage._sql.goals import (
    CANCEL_QUEUED_GOAL_ROUNDS_SQL,
    EDITED_WHILE_PAUSED_DETAIL,
    PAUSE_GOALS_SQL,
    SET_GOAL_SQL,
    arm_goal_wakeup,
)
from cloud_agent.storage._sql.jobs import JOB_SELECT, SESSION_DELETE_BLOCK_SQL
from cloud_agent.storage._sql.mcp import write_mcp_bindings
from cloud_agent.storage._sql.pending_messages import PAUSE_QUEUE_SQL
from cloud_agent.storage._sql.read_receipts import merge_read_receipts
from cloud_agent.storage._sql.rows import job_row_to_dict, load_json
from cloud_agent.storage._sql.ssh import enable_ssh_servers
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries
from cloud_agent.storage.repositories.environments import PostgresEnvironmentRepository
from cloud_agent.storage.repositories.goals import new_goal_id
from cloud_agent.task_titles import (
    fallback_title,
    normalize_generated_title,
    thread_title_needs_generation,
)

if TYPE_CHECKING:
    from cloud_agent.goals.models import GoalInput


def _goal_round_metadata(inherited: Any) -> dict[str, Any]:
    """The parent turn's metadata, plus what an automatic round requires.

    A goal round is only useful on a host that can deliver a turn's final
    assistant message before any log truncation, because that message is the
    only place the report exists. The claim path already routes work by
    ``metadata.requires`` against the host's advertised capabilities, so saying
    so here is what keeps a round off a host that would run it and report
    nothing — which is a goal that spends its budget in silence.

    ``requires`` is a reserved key: it is not part of what a client may set, and
    the controller is the only writer of an automatic round.
    """
    metadata = dict(inherited) if isinstance(inherited, dict) else {}
    requires = metadata.get("requires")
    required = list(requires) if isinstance(requires, list) else []
    if GOAL_REPORT_PROTOCOL not in required:
        required.append(GOAL_REPORT_PROTOCOL)
    metadata["requires"] = required
    return metadata


class PostgresConversationCommands:
    """Submissions, follow-ups, and the thread metadata a person edits."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns.

        A submission that loses the idempotency race reads the winner back,
        and that read is ``PostgresConversationQueries.get_job`` -- the same
        call it made when both lived on one class, over the same pool, and
        outside any transaction this class holds. Held as a peer rather than
        reimplemented, so there is still one statement behind it.
        """
        self._pool = pool
        self._queries = PostgresConversationQueries(pool)

    async def merge_read_receipts(
        self, *, user_id: str, receipts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Advance the owner's read progress without modifying chat activity."""
        if len(receipts) > READ_RECEIPT_BATCH_SIZE:
            raise ValueError(
                f"At most {READ_RECEIPT_BATCH_SIZE} read receipts may be merged at once"
            )
        async with self._pool.acquire() as conn, conn.transaction():
            # Allocate revisions only after acquiring the owner's transaction lock.
            # A sequence alone can commit out of order and lose a late commit when
            # a polling client has already advanced past its revision.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('agent_read_receipts'), hashtext($1))",
                user_id,
            )
            return await merge_read_receipts(conn, user_id=user_id, receipts=receipts)

    async def create_job(
        self,
        *,
        user_id: str,
        repo: str | None,
        task_prompt: str,
        runtime: str,
        model: str,
        execution_profile: str | None = None,
        reasoning_effort: str | None = None,
        base_sha: str | None = None,
        setup_script: str | None = None,
        environment_selection: dict[str, Any] | None = None,
        plugins: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        mcp_bindings: list[dict[str, Any]] | None = None,
        skills: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        goal: GoalInput | None = None,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
        ssh_server_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a queued job and return it.

        A null ``repo`` creates a temporary workspace: a conversation whose
        sandbox starts empty and whose turns are never published.

        ``ssh_server_ids`` are enabled for the new chat in this transaction,
        so its first claim already carries them; any that is not the owner's
        ready server raises ``SshServerUnavailable`` and nothing is created.

        ``goal`` sets the conversation's completion condition in the same
        transaction that creates it, so the first turn is already working
        toward it rather than learning about it one turn later.

        Callers that do not name a profile still go through the deployment's
        server-owned default.  This matters for internal producers that call
        the store directly: bypassing the HTTP route must not silently turn a
        secure deployment's jobs into ``trusted-v1`` work.

        ``client_request_id`` is the caller's key for this one submission,
        unique per owner, with ``client_request_fingerprint`` describing what
        was submitted under it. A resend with the same pair returns the job
        the first send created; the same key with a different fingerprint
        returns ``{"error": "client_request_conflict"}`` instead of someone
        else's task. Both or neither.
        """
        check_client_request(client_request_id, client_request_fingerprint)
        if execution_profile is None:
            execution_profile = default_execution_profile().value
        if client_request_id is not None:
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if replayed is not None:
                return replayed
        job_id = new_job_id()
        thread_id = new_thread_id()
        # How many person-authored messages this conversation has accepted. The
        # first one is the task itself, so a conversation created with a goal
        # starts at one and one created with a plain task starts at zero; the
        # first turn stamps whatever this is, and the goal's completion check
        # compares against it.
        user_input_seq = 1 if goal is not None else 0
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                binding_id = None
                if environment_selection is not None:
                    binding_id, setup_script = await PostgresEnvironmentRepository(
                        self._pool
                    )._bind_environment(conn, user_id, repo, environment_selection)
                # What every turn of this conversation shares is written once,
                # here, on the thread; the turns below carry only their own.
                await conn.execute(
                    """
                    INSERT INTO agent_threads
                        (id, user_id, repo, title, title_source,
                         setup_script, execution_profile,
                         goal_id, goal_objective, goal_state, goal_max_rounds,
                         goal_revision, goal_created_at, goal_updated_at,
                         user_input_seq, plugins)
                    VALUES ($1, $2, $3, $4, 'fallback', $5, $6,
                            CASE WHEN $7::text IS NULL THEN NULL ELSE $9 END,
                            $7, CASE WHEN $7::text IS NULL THEN NULL ELSE 'active' END,
                            $8,
                            CASE WHEN $7::text IS NULL THEN 0 ELSE 1 END,
                            CASE WHEN $7::text IS NULL THEN NULL ELSE NOW() END,
                            CASE WHEN $7::text IS NULL THEN NULL ELSE NOW() END,
                            -- The same counter the first turn below stamps
                            -- itself with: a thread that says one while its own
                            -- first turn says zero makes that turn look older
                            -- than the message that created it.
                            $10, $11::jsonb)
                    """,
                    thread_id,
                    user_id,
                    repo,
                    fallback_title(task_prompt),
                    setup_script,
                    execution_profile,
                    goal.objective if goal is not None else None,
                    goal.max_rounds if goal is not None else None,
                    new_goal_id(),
                    user_input_seq,
                    json.dumps(plugins or []),
                )
                if binding_id is not None:
                    await conn.execute(
                        "UPDATE agent_threads SET environment_binding_id=$2 WHERE id=$1",
                        thread_id,
                        binding_id,
                    )
                await write_mcp_bindings(conn, thread_id=thread_id, bindings=mcp_bindings)
                if ssh_server_ids:
                    await enable_ssh_servers(
                        conn, user_id=user_id, thread_id=thread_id, server_ids=ssh_server_ids
                    )
                await conn.execute(
                    """
                    INSERT INTO agent_jobs
                        (id, thread_id, turn_no, user_id, base_sha, task_prompt,
                         runtime, model, reasoning_effort, metadata,
                         client_request_id, client_request_fingerprint, skills,
                         attachments, input_seq_at_create)
                    VALUES ($1, $2, 1, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12::jsonb,
                            $13::jsonb, $14)
                    """,
                    job_id,
                    thread_id,
                    user_id,
                    base_sha,
                    task_prompt,
                    runtime,
                    model,
                    reasoning_effort,
                    json.dumps(metadata) if metadata is not None else None,
                    client_request_id,
                    client_request_fingerprint,
                    # No earlier skill instructions exist in a new conversation.
                    # An empty selection needs no clearing context in its prompt.
                    json.dumps(skills) if skills else None,
                    json.dumps(attachments) if attachments else None,
                    # The conversation's counter as this first turn saw it. Zero
                    # here while the thread says one would make the very first
                    # turn look older than its own message: a completion it
                    # reported would be refused as "the owner has spoken since",
                    # and no goal on a fresh conversation could ever complete.
                    user_input_seq,
                )
                await claim_attachments(
                    conn,
                    user_id=user_id,
                    job_id=job_id,
                    thread_id=thread_id,
                    attachments=attachments,
                )
                row = await conn.fetchrow(f"{JOB_SELECT} WHERE j.id = $1", job_id)
                await conn.execute(
                    """
                    INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
                    VALUES ($1, $2, 'user', $3)
                    """,
                    thread_id,
                    job_id,
                    task_prompt,
                )
                if goal is not None:
                    # A conversation created with a goal already owes a
                    # decision, and it is recorded in the transaction that
                    # created it: the first turn does the work, and this
                    # obligation is what queues the round after it. Without it
                    # the goal would wait for a finish that owes it nothing,
                    # and the only repair would be the recovery sweep.
                    await arm_goal_wakeup(
                        conn,
                        thread_id=thread_id,
                        trigger="owner",
                        trigger_ref="set:1",
                    )
        except asyncpg.UniqueViolationError as exc:
            # Two sends of one submission racing: the loser reads the winner.
            if exc.constraint_name != "uq_agent_jobs_client_request":
                raise
            # The violated constraint is on client_request_id, so the winner has one.
            assert client_request_id is not None
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            assert replayed is not None
            return replayed
        result = job_row_to_dict(row)
        result["title"] = fallback_title(task_prompt)
        result["title_source"] = "fallback"
        return result

    async def create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        reset_reasoning_effort: bool = False,
        skills: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        goal: GoalInput | None = None,
        turn_source: str = "human",
        goal_round: int | None = None,
        goal_round_kind: str | None = None,
        goal_revision: int | None = None,
        yield_to_queued: bool = False,
        client_request_id: str | None = None,
        client_request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Append a user turn and create the run that will answer it.

        ``yield_to_queued`` refuses to append while another turn of this
        conversation is already queued or running, answering
        ``{"error": "yielded"}``. It is asked inside this transaction's thread
        lock, which is the only place the answer cannot go stale.

        ``goal`` sets or replaces the conversation's completion condition in the
        same transaction that appends this turn.

        ``turn_source``, ``goal_round``, ``goal_round_kind`` and
        ``goal_revision`` describe an automatic round the platform is running
        toward the conversation's goal. They are written by the continuation
        path only; a person's message never sets them, and there is deliberately
        no parameter here that could change the objective — an automatic round
        cannot edit the goal it is running for.

        ``reasoning_effort`` overrides the inherited depth; omitting it keeps
        the parent's, as an omitted model does. ``reset_reasoning_effort``
        clears it back to Auto — the one thing a null override cannot express,
        since null is what "inherit" already sends.

        Every follow-up is ``queued`` until all earlier turns have finished.
        The thread row is locked before selecting the latest turn, so
        concurrent submissions form one linear chain instead of siblings that
        could later mutate the same branch at once. A request made from an old
        turn also appends to the current tip, matching chat semantics.

        ``client_request_id`` and ``client_request_fingerprint`` work as for
        :meth:`create_job`: a resend returns the turn the first send made,
        the same key under different content returns
        ``{"error": "client_request_conflict"}``.
        """
        check_client_request(client_request_id, client_request_fingerprint)
        if client_request_id is not None:
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            if replayed is not None:
                return replayed
        try:
            return await self._create_follow_up(
                parent_job_id=parent_job_id,
                user_id=user_id,
                prompt=prompt,
                runtime=runtime,
                model=model,
                reasoning_effort=reasoning_effort,
                reset_reasoning_effort=reset_reasoning_effort,
                skills=skills,
                attachments=attachments,
                goal=goal,
                turn_source=turn_source,
                goal_round=goal_round,
                goal_round_kind=goal_round_kind,
                goal_revision=goal_revision,
                yield_to_queued=yield_to_queued,
                client_request_id=client_request_id,
                client_request_fingerprint=client_request_fingerprint,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "uq_agent_jobs_client_request":
                raise
            # The violated constraint is on client_request_id, so the winner has one.
            assert client_request_id is not None
            replayed = await self.replayed_submission(
                user_id=user_id,
                client_request_id=client_request_id,
                fingerprint=client_request_fingerprint,
            )
            assert replayed is not None
            return replayed

    async def replayed_submission(
        self, *, user_id: str, client_request_id: str, fingerprint: str | None
    ) -> dict[str, Any] | None:
        """The job an owner's key already made, a conflict, or ``None`` for a new key.

        Public because the routes ask it *before* their checks that can change
        between two sends of one submission — model resolution, repo
        entitlement, tool-server connections, whether the session is still
        open. A resend must reach the job the first send made whatever has
        happened since; only a key that has never been used runs those checks.
        :meth:`create_job` and :meth:`create_follow_up` still ask it
        themselves, so a race that slips past the route is still resolved by
        the unique index rather than by a second task.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, client_request_fingerprint FROM agent_jobs "
                "WHERE user_id = $1 AND client_request_id = $2",
                user_id,
                client_request_id,
            )
        if row is None:
            return None
        if row["client_request_fingerprint"] != fingerprint:
            return {"error": "client_request_conflict"}
        return await self._queries.get_job(str(row["id"]))

    async def set_sessions_archived(
        self, *, thread_ids: list[str], user_id: str, archived: bool
    ) -> dict[str, Any]:
        """Set every owned selected thread's archive state, or change none."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id FROM agent_threads "
                "WHERE id = ANY($1::text[]) AND user_id = $2 AND deleted_at IS NULL "
                "ORDER BY id FOR UPDATE",
                thread_ids,
                user_id,
            )
            if len(rows) != len(thread_ids):
                return {"error": "not_found"}
            updated = await conn.fetch(
                """
                UPDATE agent_threads
                SET archived_at = CASE WHEN $3 THEN COALESCE(archived_at, NOW()) ELSE NULL END
                WHERE id = ANY($1::text[]) AND user_id = $2
                RETURNING id AS thread_id, archived_at
                """,
                thread_ids,
                user_id,
                archived,
            )
            if archived:
                # The same statement the reaper uses for the same reason: an
                # archived conversation is not one automatic rounds belong in,
                # and the owner keeps the goal to resume when it comes back.
                await conn.execute(
                    PAUSE_GOALS_SQL,
                    thread_ids,
                    "archived",
                    "the conversation was archived; resume the goal to continue it",
                )
                await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, thread_ids)
                # Messages the owner left waiting stay waiting, and stay
                # stopped after a restore: continuing is their call.
                await conn.execute(PAUSE_QUEUE_SQL, thread_ids, QUEUE_PAUSED_ARCHIVED)
        return {"sessions": [dict(row) for row in updated]}

    async def delete_sessions(self, *, thread_ids: list[str], user_id: str) -> dict[str, Any]:
        """Irreversibly hide chats from their owner while retaining audit rows.

        A chat need not be archived or idle. The thread lock serializes owner
        deletion against follow-up insertion, native-fork preparation and
        archive changes, so the same commit can end everything the chat would
        otherwise keep doing: queued turns are cancelled, a running turn is
        asked to stop, an active goal is paused, and the chat's residency is
        released. The caller revokes each returned grant and asks the owning
        host to close that member's process; a missed request is repeated by
        the host's next residency heartbeat, which now finds it lost.

        The job, residency and sandbox ledgers are kept. A fork's shared
        workspace is never removed here; the sandbox deletion worker removes a
        stopped sandbox once every chat in it is deleted.

        Files the owner uploaded are the exception: those rows are deleted
        rather than hidden, because they are the owner's content rather than an
        audit trail, and nothing else would ever remove them.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id FROM agent_threads "
                "WHERE id = ANY($1::text[]) AND user_id = $2 AND deleted_at IS NULL "
                "ORDER BY id FOR UPDATE",
                thread_ids,
                user_id,
            )
            if len(rows) != len(thread_ids):
                return {"error": "not_found"}
            blocked = await conn.fetchrow(
                f"""
                WITH selected AS (
                    SELECT id AS session_id, archived_at FROM agent_threads
                    WHERE id = ANY($1::text[]) AND user_id = $2
                ), checked AS (
                    SELECT t.session_id, {SESSION_DELETE_BLOCK_SQL} AS reason
                    FROM selected t
                )
                SELECT reason FROM checked WHERE reason IS NOT NULL ORDER BY session_id LIMIT 1
                """,
                thread_ids,
                user_id,
            )
            if blocked is not None:
                return {"error": blocked["reason"]}
            await conn.execute(
                "UPDATE agent_threads SET deleted_at = NOW() "
                "WHERE id = ANY($1::text[]) AND user_id = $2",
                thread_ids,
                user_id,
            )
            # Claims read the thread tombstone only for an automatic round, so
            # any other turn left queued would still run. Cancel it the way a
            # cancelled goal round is cancelled; a running turn is asked to stop
            # and reaches its terminal state through the worker's fenced
            # transition.
            await conn.execute(
                "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                "terminal_resume_pending = TRUE, updated_at = NOW() "
                "WHERE thread_id = ANY($1::text[]) AND state = 'queued'",
                thread_ids,
            )
            await conn.execute(
                "UPDATE agent_jobs SET cancel_requested = TRUE, updated_at = NOW() "
                "WHERE thread_id = ANY($1::text[]) AND state = 'running'",
                thread_ids,
            )
            await conn.execute(
                PAUSE_GOALS_SQL, thread_ids, "cancelled", "the conversation was deleted"
            )
            # Thread -> membership, the order Fork preparation also takes. A
            # released generation opens nothing more: no heartbeat extends it
            # and no credential is renewed for it.
            released = await conn.fetch(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g
                WHERE m.thread_id = ANY($1::text[]) AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          COALESCE(m.residency_grant_id, '') AS grant_id
                """,
                thread_ids,
            )
            # Uploaded bytes are removed outright, not soft-deleted with the
            # rest. A soft delete keeps the ledger — jobs, residency, sandbox
            # history — because those are audit rows. A file the owner attached
            # is their content, not an audit row, and the foreign key's cascade
            # never fires here because the job rows above deliberately survive.
            # Without this the bytes would outlive the conversation forever.
            await conn.execute(
                "DELETE FROM agent_job_attachments WHERE thread_id = ANY($1::text[]) "
                "AND user_id = $2",
                thread_ids,
                user_id,
            )
            # Messages that were never sent are the owner's content for the
            # same reason, and so are the uploads they hold — which are still
            # staged, with no thread of their own, so the delete above cannot
            # see them.
            await conn.execute(
                """
                DELETE FROM agent_job_attachments
                WHERE user_id = $2 AND pending_message_id IN (
                    SELECT id FROM agent_pending_messages WHERE thread_id = ANY($1::text[])
                )
                """,
                thread_ids,
                user_id,
            )
            await conn.execute(
                "DELETE FROM agent_pending_messages WHERE thread_id = ANY($1::text[])",
                thread_ids,
            )
        return {
            "thread_ids": thread_ids,
            "released_residencies": [dict(row) for row in released],
        }

    async def set_thread_archived(
        self, *, job_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Archive or restore the owned thread containing ``job_id``."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET archived_at = CASE
                    WHEN $3 THEN COALESCE(t.archived_at, NOW())
                    ELSE NULL
                END
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM agent_jobs j
                      WHERE j.id = $1
                        AND j.thread_id = t.id
                        AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.archived_at
                """,
                job_id,
                user_id,
                archived,
            )
            if row is None:
                return None
            if archived:
                # An archived conversation is not one automatic rounds belong
                # in. Pausing here, rather than refusing the archive, keeps the
                # owner's action authoritative and leaves the goal resumable
                # afterwards — and it shares the transaction, so "archived" and
                # "still running itself" are never both true.
                await conn.execute(
                    PAUSE_GOALS_SQL,
                    [row["thread_id"]],
                    "archived",
                    "the conversation was archived; resume the goal to continue it",
                )
                await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [row["thread_id"]])
                # The same for the owner's waiting messages: archived, they do
                # not send themselves, and restoring does not start them.
                await conn.execute(PAUSE_QUEUE_SQL, [row["thread_id"]], QUEUE_PAUSED_ARCHIVED)
            return {"thread_id": row["thread_id"], "archived_at": row["archived_at"]}

    async def set_thread_title(
        self, *, job_id: str, user_id: str, title: str
    ) -> dict[str, Any] | None:
        """Rename the owned conversation without changing any turn's prompt."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET title = $3, title_source = 'manual'
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM agent_jobs j
                      WHERE j.id = $1 AND j.thread_id = t.id AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.title
                """,
                job_id,
                user_id,
                title,
            )
        return dict(row) if row is not None else None

    async def get_thread_title_prompt(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> str | None:
        """Return the first prompt if this fenced attempt may still name its thread."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT t.title, t.title_source, first.task_prompt
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id
                JOIN agent_jobs first ON first.thread_id = t.id AND first.turn_no = 1
                JOIN agent_attempts a ON a.id = j.current_attempt_id AND a.job_id = j.id
                WHERE j.id = $1 AND a.id = $2 AND a.lease_generation = $3
                  AND t.deleted_at IS NULL AND NOT j.cancel_requested
                  AND (
                      (j.state = 'running' AND a.status = 'running'
                       AND a.lease_expires_at > clock_timestamp())
                      OR (j.state = 'succeeded' AND a.status = 'finished')
                  )
                """,
                job_id,
                attempt_id,
                lease_generation,
            )
        if row is None or not thread_title_needs_generation(row["title_source"], row["title"]):
            return None
        return str(row["task_prompt"])

    async def set_generated_thread_title(
        self, *, job_id: str, attempt_id: int, lease_generation: int, title: str
    ) -> bool:
        """Save a missing/recovered title while its attempt remains current."""
        if normalize_generated_title(title) is None:
            return False
        async with self._pool.acquire() as conn, conn.transaction():
            locked_attempt = await conn.fetchval(
                "SELECT id FROM agent_attempts WHERE id = $1 AND lease_generation = $2 FOR SHARE",
                attempt_id,
                lease_generation,
            )
            if locked_attempt is None:
                return False
            # Match follow-up/fork lock ordering: thread before job. Locking the
            # source also makes a simultaneous manual rename win in either order.
            thread = await conn.fetchrow(
                """
                SELECT t.id, t.title, t.title_source FROM agent_threads t
                JOIN agent_jobs j ON j.thread_id = t.id
                WHERE j.id = $1 AND t.deleted_at IS NULL
                FOR UPDATE OF t
                """,
                job_id,
            )
            if thread is None or not thread_title_needs_generation(
                thread["title_source"], thread["title"]
            ):
                return False
            authority = await conn.fetchrow(
                """
                SELECT j.state AS job_state, a.status AS attempt_status
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE j.id = $1 AND j.current_attempt_id = a.id
                  AND a.id = $2 AND a.lease_generation = $3 AND NOT j.cancel_requested
                  AND (
                      (j.state = 'running' AND a.status = 'running')
                      OR (j.state = 'succeeded' AND a.status = 'finished')
                  )
                FOR SHARE OF j
                """,
                job_id,
                attempt_id,
                lease_generation,
            )
            if authority is None:
                return False
            if authority["job_state"] == "running" and not await conn.fetchval(
                "SELECT lease_expires_at > clock_timestamp() FROM agent_attempts WHERE id = $1",
                attempt_id,
            ):
                return False
            updated = await conn.fetchval(
                """
                UPDATE agent_threads SET title = $2, title_source = 'generated'
                WHERE id = $1 AND deleted_at IS NULL
                RETURNING id
                """,
                thread["id"],
                title,
            )
            return updated is not None

    async def set_thread_pinned(
        self, *, job_id: str, user_id: str, pinned: bool
    ) -> dict[str, Any] | None:
        """Pin or unpin the owned thread containing ``job_id``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE agent_threads t
                SET pinned_at = CASE
                    WHEN $3 THEN COALESCE(t.pinned_at, NOW())
                    ELSE NULL
                END
                WHERE t.user_id = $2
                  AND t.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM agent_jobs j
                      WHERE j.id = $1
                        AND j.thread_id = t.id
                        AND j.user_id = $2
                  )
                RETURNING t.id AS thread_id, t.pinned_at
                """,
                job_id,
                user_id,
                pinned,
            )
        if row is None:
            return None
        return {"thread_id": row["thread_id"], "pinned_at": row["pinned_at"]}

    async def create_goal_round(
        self,
        *,
        conn: Any | None = None,
        thread_id: str,
        goal_id: str,
        revision: int,
        kind: str,
        round_number: int,
        prompt: str,
        client_request_id: str,
        client_request_fingerprint: str,
        wakeup_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Append one automatic round inside the caller's transaction.

        The scheduling obligation and the turn it produced commit together —
        that pair is the whole reason this is not a second call to
        :meth:`create_follow_up`. ``conn`` is already holding the conversation's
        lock, taken by the controller before it read the state this decision was
        made from, so nothing can be appended between the read and this write.

        Returns ``None`` when this event already produced a round. That is not
        an error: a replayed wakeup is supposed to find the work it already did
        rather than starting a second turn, and the identity that makes it find
        it is the turn that triggered the decision. It also returns ``None``
        when the conversation is gone.

        ``conn`` is the caller's transaction, and the controller always passes
        one: this insert commits with the wakeup's completion. The fallback is
        for a caller that wants a round on its own, and in that case this method
        owns the transaction.
        """
        if conn is not None:
            return await self._create_goal_round_on(
                conn,
                thread_id=thread_id,
                goal_id=goal_id,
                revision=revision,
                kind=kind,
                round_number=round_number,
                prompt=prompt,
                client_request_id=client_request_id,
                client_request_fingerprint=client_request_fingerprint,
                wakeup_id=wakeup_id,
            )
        async with self._pool.acquire() as acquired, acquired.transaction():
            return await self._create_goal_round_on(
                acquired,
                thread_id=thread_id,
                goal_id=goal_id,
                revision=revision,
                kind=kind,
                round_number=round_number,
                prompt=prompt,
                client_request_id=client_request_id,
                client_request_fingerprint=client_request_fingerprint,
                wakeup_id=wakeup_id,
            )

    async def _create_goal_round_on(
        self,
        conn: Any,
        *,
        thread_id: str,
        goal_id: str,
        revision: int,
        kind: str,
        round_number: int,
        prompt: str,
        client_request_id: str,
        client_request_fingerprint: str,
        wakeup_id: str | None,
    ) -> dict[str, Any] | None:
        """The one-transaction body of :meth:`create_goal_round`."""
        existing = await conn.fetchval(
            """
            SELECT j.id FROM agent_jobs j
            JOIN agent_threads t ON t.id = j.thread_id
            WHERE t.id = $1 AND j.client_request_id = $2
            """,
            thread_id,
            client_request_id,
        )
        if existing is not None:
            if wakeup_id is not None:
                await conn.execute(
                    "UPDATE agent_goal_wakeups SET state = 'processed', "
                    "lease_owner = NULL, lease_expires_at = NULL, updated_at = NOW() "
                    "WHERE id = $1",
                    wakeup_id,
                )
            return None
        parent = await conn.fetchrow(
            f"{JOB_SELECT} WHERE j.thread_id = $1 ORDER BY j.turn_no DESC LIMIT 1 FOR UPDATE OF j",
            thread_id,
        )
        if parent is None:  # Defensive: every thread is created with turn one.
            return None
        job_id = new_job_id()
        await conn.execute(
            """
            INSERT INTO agent_jobs
                (id, thread_id, parent_job_id, turn_no, user_id, base_sha,
                 task_prompt, runtime, model, reasoning_effort, state, metadata,
                 client_request_id, client_request_fingerprint, skills,
                 turn_source, goal_round, goal_round_kind, goal_revision, goal_id,
                 input_seq_at_create)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb,
                    $13, $14, $15::jsonb, 'goal', $16, $17, $18, $19,
                    -- The input counter as this round was created, so that a
                    -- completion it reports can be checked against whether the
                    -- owner has spoken since. Zero here would make every round
                    -- look older than the first person's message, and rule 7
                    -- would refuse every completion the platform ever read.
                    (SELECT user_input_seq FROM agent_threads WHERE id = $2))
            """,
            job_id,
            thread_id,
            parent["id"],
            parent["turn_no"] + 1,
            parent["user_id"],
            parent["published_commit_sha"] or parent["base_sha"],
            prompt,
            parent["runtime"],
            parent["model"],
            parent["reasoning_effort"],
            QUEUED,
            json.dumps(_goal_round_metadata(load_json(parent["metadata"]))),
            client_request_id,
            client_request_fingerprint,
            parent["skills"],
            round_number,
            kind,
            revision,
            goal_id,
        )
        await conn.execute(
            """
            INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
            VALUES ($1, $2, 'user', $3)
            """,
            thread_id,
            job_id,
            prompt,
        )
        await conn.execute("UPDATE agent_threads SET updated_at = NOW() WHERE id = $1", thread_id)
        if wakeup_id is not None:
            # The obligation and the turn it produced, one commit. A crash
            # before this cannot leave a round nobody accounted for.
            await conn.execute(
                "UPDATE agent_goal_wakeups SET state = 'processed', lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = NOW() WHERE id = $1",
                wakeup_id,
            )
        row = await conn.fetchrow(f"{JOB_SELECT} WHERE j.id = $1", job_id)
        return job_row_to_dict(row)

    async def _create_follow_up(
        self,
        *,
        parent_job_id: str,
        user_id: str,
        prompt: str,
        runtime: str | None,
        model: str | None,
        reasoning_effort: str | None,
        reset_reasoning_effort: bool,
        skills: list[dict[str, Any]] | None,
        attachments: list[dict[str, Any]] | None,
        goal: GoalInput | None,
        turn_source: str,
        goal_round: int | None,
        goal_round_kind: str | None,
        goal_revision: int | None,
        yield_to_queued: bool,
        client_request_id: str | None,
        client_request_fingerprint: str | None,
    ) -> dict[str, Any] | None:
        """The one-transaction body of :meth:`create_follow_up`."""
        async with self._pool.acquire() as conn, conn.transaction():
            return await insert_follow_up(
                conn,
                parent_job_id=parent_job_id,
                user_id=user_id,
                prompt=prompt,
                runtime=runtime,
                model=model,
                reasoning_effort=reasoning_effort,
                reset_reasoning_effort=reset_reasoning_effort,
                skills=skills,
                attachments=attachments,
                goal=goal,
                turn_source=turn_source,
                goal_round=goal_round,
                goal_round_kind=goal_round_kind,
                goal_revision=goal_revision,
                yield_to_queued=yield_to_queued,
                client_request_id=client_request_id,
                client_request_fingerprint=client_request_fingerprint,
            )


async def insert_follow_up(
    conn: Any,
    *,
    parent_job_id: str,
    user_id: str,
    prompt: str,
    runtime: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    reset_reasoning_effort: bool = False,
    skills: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    goal: GoalInput | None = None,
    turn_source: str = "human",
    goal_round: int | None = None,
    goal_round_kind: str | None = None,
    goal_revision: int | None = None,
    yield_to_queued: bool = False,
    client_request_id: str | None = None,
    client_request_fingerprint: str | None = None,
    pending_message_id: str | None = None,
) -> dict[str, Any] | None:
    """Append a user turn inside the caller's transaction.

    Everything :meth:`PostgresConversationCommands.create_follow_up` does after
    its replay check, on a connection the caller already holds, so that a turn
    can be appended in the same commit as whatever decided to append it. The
    caller owns the transaction: a ``None`` or ``{"error": ...}`` answer is
    returned from inside it, and whether what was written before that answer
    commits is the caller's decision, exactly as it was when this was the body
    of ``create_follow_up``'s own transaction.

    ``pending_message_id`` names the waiting message this turn is sent from,
    whose pinned uploads it may claim; every other turn claims only uploads no
    message holds.
    """
    job_id = new_job_id()
    requested = await conn.fetchrow(
        "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
        parent_job_id,
        user_id,
    )
    if requested is None:
        return None
    thread = await conn.fetchrow(
        "SELECT id, title, title_source, pinned_at, archived_at, "
        "goal_id, goal_objective, goal_state FROM agent_threads "
        "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
        requested["thread_id"],
        user_id,
    )
    if thread is None:
        return None
    if thread["archived_at"] is not None:
        return {"error": "session_archived"}
    thread_id = thread["id"]
    if yield_to_queued:
        # Under the lock this transaction already holds, so a message
        # that arrives after this point either saw this round or waits
        # behind it — there is no moment where both are queued.
        waiting = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM agent_jobs pending
                WHERE pending.thread_id = $1
                  AND pending.state IN ('queued', 'running')
            )
            """,
            thread_id,
        )
        if waiting:
            return {"error": "yielded"}
    if goal is not None:
        # The objective and the message are one commit. A turn that
        # carried a new goal into a transaction that then failed would
        # otherwise leave a goal nothing had been asked to work on.
        # A goal that had already finished is being authorized
        # again even when its words are identical: the previous
        # authorization was spent, and its identity must not be reused.
        # A paused goal stays paused however it is edited — the owner
        # stopped it, and only the owner starts it again.
        await conn.execute(
            SET_GOAL_SQL,
            thread_id,
            goal.objective,
            goal.max_rounds,
            new_goal_id(),
            "paused" if thread["goal_state"] == "paused" else "active",
            thread["goal_state"] == "complete",
            EDITED_WHILE_PAUSED_DETAIL,
            # The same tri-state the goal repository's own ``set_goal``
            # applies, and for the same reason: a message that carries a
            # new objective and says nothing about a bound must not
            # delete one the owner stated earlier.
            goal.states_max_rounds,
        )
        # The same rule the goal repository's own ``set_goal`` applies,
        # and it has to be applied here too: a round queued for the old
        # objective carries a `<goal_round>` body saying what *it* was
        # for, so leaving it queued runs a turn toward an objective its
        # owner just replaced — and pays for it.
        await conn.execute(CANCEL_QUEUED_GOAL_ROUNDS_SQL, [thread_id])
    parent = await conn.fetchrow(
        f"{JOB_SELECT} WHERE j.thread_id = $1 ORDER BY j.turn_no DESC LIMIT 1 FOR UPDATE OF j",
        thread_id,
    )
    if parent is None:  # Defensive: every thread is created with turn one.
        return None
    # The owner-facing route checks this before doing entitlement and
    # MCP work, but that check is necessarily advisory: an activation
    # can become indeterminate while those awaits run. Locking the
    # durable sandbox here closes that TOCTOU. If this transaction
    # wins the row first, the concurrent LOST writer subsequently
    # fails the inserted turn in its own transaction; if LOST wins,
    # no turn is inserted at all.
    # Every lifecycle writer takes membership before sandbox. Locking
    # the two joined rows in planner order would make that order
    # implicit and could invert against host reconciliation.
    await conn.fetchval(
        """
        SELECT thread_id
        FROM agent_thread_sandbox_memberships
        WHERE thread_id = $1
        FOR UPDATE
        """,
        thread_id,
    )
    sandbox = await conn.fetchrow(
        """
        SELECT g.state, m.context_status, TRUE AS shared_live
        FROM agent_thread_sandbox_memberships m
        JOIN agent_sandboxes g ON g.id = m.sandbox_id
        WHERE m.thread_id = $1
        FOR UPDATE OF g
        """,
        thread_id,
    )
    if sandbox is not None and (
        sandbox["context_status"] == "lost" or sandbox["state"] in {"stopped_dirty", "lost"}
    ):
        return {"error": "sandbox_context_lost"}
    if sandbox is None and parent["state"] == SUCCEEDED:
        # A successful native turn must have settled inside its group.
        # Rows that predate that invariant are audit history, not a
        # source from which the new runtime can fabricate continuity.
        return {"error": "sandbox_context_lost"}
    turn_no = parent["turn_no"] + 1
    previous_skills = load_json(parent["skills"]) if parent["skills"] is not None else None
    selected_skills = previous_skills if skills is None else skills
    if selected_skills == [] and previous_skills is None:
        selected_skills = None
    # Repo, setup script, execution profile and tool servers are the
    # thread's, not copied per turn: the next turn of a conversation
    # runs with the surface the thread has been running with, and a
    # follow-up cannot quietly gain a server the owner never chose.
    await conn.execute(
        """
        INSERT INTO agent_jobs
            (id, thread_id, parent_job_id, turn_no, user_id, base_sha,
             task_prompt, runtime, model, reasoning_effort, state, metadata,
             client_request_id, client_request_fingerprint, skills, attachments,
             turn_source, goal_round, goal_round_kind, goal_revision, goal_id,
             input_seq_at_create)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14,
                $15::jsonb, $16::jsonb, $17, $18, $19, $20, $21, $22)
        """,
        job_id,
        thread_id,
        parent["id"],
        turn_no,
        user_id,
        parent["published_commit_sha"] or parent["base_sha"],
        prompt,
        runtime or parent["runtime"],
        model or parent["model"],
        None if reset_reasoning_effort else (reasoning_effort or parent["reasoning_effort"]),
        QUEUED,
        json.dumps(load_json(parent["metadata"])) if parent["metadata"] is not None else None,
        client_request_id,
        client_request_fingerprint,
        json.dumps(selected_skills) if selected_skills is not None else None,
        # Not inherited. Earlier turns' files are already under /uploads
        # and are re-imported whenever the sandbox is rebuilt, so this
        # column records only what this turn added.
        json.dumps(attachments) if attachments else None,
        turn_source,
        goal_round,
        goal_round_kind,
        goal_revision,
        thread["goal_id"],
        # A person's turn is what "the owner has moved on" is measured
        # against: the sequence it lands on is stamped below, after the
        # thread's counter has moved. An automatic round carries zero
        # and never advances it.
        0,
    )
    await claim_attachments(
        conn,
        user_id=user_id,
        job_id=job_id,
        thread_id=thread_id,
        attachments=attachments,
        pending_message_id=pending_message_id,
    )
    row = await conn.fetchrow(f"{JOB_SELECT} WHERE j.id = $1", job_id)
    await conn.execute(
        """
        INSERT INTO agent_thread_messages (thread_id, job_id, role, content)
        VALUES ($1, $2, 'user', $3)
        """,
        thread_id,
        job_id,
        prompt,
    )
    if turn_source != "goal":
        # The bump and the turn are one commit. A completion claim is
        # honored only while the thread's counter still equals the one
        # its turn was created at, so the message that would make an
        # in-flight claim stale is stamped by the same transaction that
        # accepts it.
        stamped = await conn.fetchval(
            """
            UPDATE agent_threads
            SET user_input_seq = user_input_seq + 1, updated_at = NOW()
            WHERE id = $1
            RETURNING user_input_seq
            """,
            thread_id,
        )
        await conn.execute(
            "UPDATE agent_jobs SET input_seq_at_create = $2 WHERE id = $1",
            job_id,
            stamped,
        )
        # The same commit records that this conversation's active goal
        # has to be looked at again. Without it, a goal whose one
        # obligation was consumed before the message arrived — which is
        # exactly what a message racing a decision looks like — would
        # wait for a finish that no longer owes it anything.
        await arm_goal_wakeup(
            conn,
            thread_id=thread_id,
            trigger="finish",
            trigger_ref=f"input:{stamped}",
        )
    else:
        await conn.execute("UPDATE agent_threads SET updated_at = NOW() WHERE id = $1", thread_id)
    result = job_row_to_dict(row)
    result["title"] = thread["title"]
    result["title_source"] = thread["title_source"]
    result["pinned_at"] = thread["pinned_at"]
    return result
