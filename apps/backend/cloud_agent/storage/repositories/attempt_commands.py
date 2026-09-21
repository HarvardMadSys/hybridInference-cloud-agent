"""Postgres implementation of the attempt lifecycle port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import asyncpg

from cloud_agent.domain.execution import (
    ATTEMPT_ABORTED,
    ATTEMPT_RUNNING,
    CANCELLED,
    EVENT_ATTEMPT_ABORTED,
    EVENT_ATTEMPT_SUPERSEDED,
    FAILED,
    QUEUED,
    RUNNING,
    TERMINAL_STATES,
)
from cloud_agent.domain.runtimes import resolve_runtime_version
from cloud_agent.execution_profiles import (
    KATA_RUNTIME,
    ExecutionProfile,
    compatible_shared_sandbox_followup_attested,
    shared_sandbox_claim_attested,
)
from cloud_agent.host_pool import CLAIMABLE_STATUSES, HOST_ONLINE
from cloud_agent.storage._sql.events import insert_event
from cloud_agent.storage._sql.hosts import host_slots_exhausted, release_host_slot
from cloud_agent.storage._sql.jobs import (
    QUALIFIED_JOB_COLUMNS,
    fail_discontinuous_follow_ups,
    fail_unrunnable_follow_ups,
)
from cloud_agent.storage._sql.rows import job_row_to_dict, load_json
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE
from cloud_agent.task_titles import thread_title_needs_generation

if TYPE_CHECKING:
    from cloud_agent.ports.attempt_commands import TurnSummaryWrite

#: What the ordering rules decide, before the caller writes anything.
#: ``accept`` is this function's own answer and never leaves the repository:
#: the write either succeeds (``stored``) or the row moved under it
#: (``conflict``), and the caller only ever sees the port's four outcomes.
TurnSummaryDecision = Literal["accept", "duplicate", "conflict"]


class PostgresAttemptCommands:
    """Claim, heartbeat, transition and reap -- every fenced write a worker makes."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def sweep_queued_follow_ups(self) -> None:
        """Fail queued follow-ups whose earlier native context is unavailable.

        Claiming already excludes these rows. This periodic safety net gives
        legacy pending turns a terminal answer using the partial queued
        indexes, without a cleanup scan on every claim poll.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await fail_discontinuous_follow_ups(conn)

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: float,
        host: str,
        host_credential_hash: str,
        claim_attestation: dict[str, Any] | None = None,
        expected_deployment_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim the oldest queued job, creating a new fenced attempt.

        Returns the job dict extended with ``attempt_id``, ``attempt_no`` and
        ``lease_generation``, or ``None`` when the queue is empty. Concurrent
        workers are safe via ``FOR UPDATE SKIP LOCKED``.

        A queued follow-up becomes eligible only after every earlier turn in
        its thread is terminal. Checking all unfinished earlier turns also
        prevents a cancelled intermediate turn from letting its descendants
        overtake an active ancestor. The indexed eligibility check precedes
        the limit, so a blocked thread cannot hide runnable work behind it.

        The caller is always an enrolled host whose exact credential hash is
        re-checked while its capacity slot is taken. An operator pin chooses
        the host for chats that do not have a sandbox yet. A retained chat is
        instead hard-pinned to the host that owns its Docker writable layer;
        without portable storage, sending that follow-up to the newly selected
        host would be data loss disguised as failover.

        The policy read is the first statement of the *claiming* transaction and
        takes the row shared, so it cannot be overtaken by a switch committing
        between the check and the dequeue. The same query uses that decision to
        exclude new chats from the old host while still admitting only retained
        groups whose immutable ``host`` is that old host.

        **The pool gate sits after the pin and answers the same way.**
        A reported host must be ``online`` and have a free slot under the lower
        of its reported executor count and the size an operator asked for, and the job it
        takes must require nothing the host did not declare. All three refusals
        return ``None`` — the same "nothing for you" a drained or unpinned
        runner already gets — because a runner has no useful different
        behaviour for "the queue is empty" and "the queue has work you cannot
        run". Capacity and capability are operator-visible on the admin
        surface, which is where the question is answerable.

        Every profile requires a shared-live capability attestation from the
        exact claiming replica. New groups require the current protocol; the
        explicitly compatible v1 protocol may claim only an existing group on
        its owning host. ``untrusted-kata-v1`` additionally requires the Kata
        runtime plus unequal host/guest kernels. The mutable heartbeat row is
        not evidence for a particular replica during a rolling deploy.
        """
        attestation = claim_attestation or {}
        sandbox_runtime = attestation.get("sandbox_runtime")
        sandbox_image_digest = attestation.get("sandbox_image_digest")
        runtime_versions = attestation.get("runtime_versions")
        if not isinstance(runtime_versions, dict):
            runtime_versions = {}
        # The menu, from the same place and for the same reason as the default
        # beside it: the attestation is *this replica's* image, and the
        # heartbeat row is not evidence about it during a rolling deployment.
        # A selection is only honoured when the machine about to run it says it
        # can.
        runtime_versions_available = attestation.get("runtime_versions_available")
        if not isinstance(runtime_versions_available, dict):
            runtime_versions_available = {}
        host_kernel = attestation.get("host_kernel")
        guest_kernel = attestation.get("guest_kernel")
        shared_attestation = shared_sandbox_claim_attested(
            attestation,
            expected_deployment_sha=expected_deployment_sha,
        )
        followup_attestation = compatible_shared_sandbox_followup_attested(attestation)
        if not host or not host_credential_hash or not followup_attestation:
            return None
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host, runtime_versions FROM agent_runner_policy WHERE id FOR SHARE"
            )
            active_host = None if policy is None else policy["active_host"]
            selected_versions = (
                {} if policy is None else load_json(policy["runtime_versions"]) or {}
            )
            pin_allows_new_sandbox = active_host is None or host == active_host
            host_row = await conn.fetchrow(
                "SELECT status, capabilities, slots_total, slots_used, slots_desired, "
                "(credential_hash IS NOT NULL AND credential_hash = $2) "
                "AS credential_matches "
                "FROM agent_runner_hosts WHERE host = $1",
                host,
                host_credential_hash,
            )
            # This read only chooses which profile may be considered. The slot
            # UPDATE below is the decision point and closes concurrent drain,
            # revocation, and re-enrollment races.
            if host_row is None or host_row["status"] not in CLAIMABLE_STATUSES:
                return None
            if host_slots_exhausted(host_row):
                return None
            capabilities = host_row["capabilities"]
            credential_matches = bool(host_row["credential_matches"])
            secure_attestation = bool(
                credential_matches
                and sandbox_runtime == KATA_RUNTIME
                and isinstance(host_kernel, str)
                and bool(host_kernel.strip())
                and isinstance(guest_kernel, str)
                and bool(guest_kernel.strip())
                and host_kernel.strip() != guest_kernel.strip()
            )
            job_row = await conn.fetchrow(
                f"""
                SELECT {QUALIFIED_JOB_COLUMNS}, to_jsonb(s) AS thread_sandbox,
                       t.title_source, t.title
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN {THREAD_SANDBOX_SOURCE} ON s.thread_id = j.thread_id
                WHERE j.state = 'queued' AND j.cancel_requested = FALSE
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_jobs earlier
                        WHERE earlier.thread_id = j.thread_id
                          AND earlier.turn_no < j.turn_no
                          AND earlier.state IN ('queued', 'running')
                  )
                  AND COALESCE(j.metadata->'requires', '[]'::jsonb) <@ $1::jsonb
                  AND (
                        t.execution_profile = '{ExecutionProfile.TRUSTED_V1.value}'
                        OR (
                            t.execution_profile = '{ExecutionProfile.UNTRUSTED_KATA_V1.value}'
                            AND $3::boolean
                        )
                  )
                  AND (
                        (
                            s.thread_id IS NULL AND $5::boolean AND $4::boolean
                            AND NOT EXISTS (
                                SELECT 1 FROM agent_jobs parent
                                WHERE parent.id = j.parent_job_id
                                  AND parent.state = 'succeeded'
                            )
                        )
                        OR (
                            s.host = $2
                            AND $6::boolean
                            AND s.fork_operation_id IS NULL
                            AND s.context_status <> 'lost'
                            AND s.native_state <> 'lost'
                            AND s.state IN (
                                'provisioning', 'starting', 'running', 'stopped'
                            )
                            AND s.shared_live
                        )
                  )
                ORDER BY j.created_at
                LIMIT 1
                FOR UPDATE OF j SKIP LOCKED
                """,
                capabilities,
                host,
                secure_attestation,
                shared_attestation,
                pin_allows_new_sandbox,
                followup_attestation,
            )
            if job_row is None:
                return None
            job_id = job_row["id"]
            # The atomic half of the slot claim. Under READ COMMITTED two
            # claims hitting the same host row serialize and the loser
            # re-evaluates capacity against the winner's committed value.
            took_slot = await conn.fetchval(
                f"""
                UPDATE agent_runner_hosts
                SET slots_used = slots_used + 1
                WHERE host = $1
                  AND status = '{HOST_ONLINE}'
                  AND (slots_total IS NULL OR slots_used < slots_total)
                  AND (slots_desired IS NULL OR slots_used < slots_desired)
                  AND credential_hash = $2
                RETURNING slots_used
                """,
                host,
                host_credential_hash,
            )
            if took_slot is None:
                return None
            next_no = await conn.fetchval(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM agent_attempts WHERE job_id = $1",
                job_id,
            )
            thread_sandbox = load_json(job_row["thread_sandbox"])
            # A thread keeps the version it started on; see
            # ``resolve_runtime_version``. Read here, inside the claim, because
            # the answer must be the one the attempt is stamped with — the
            # TOCTOU that stamping exists to close.
            thread_version = None
            if job_row["thread_id"]:
                thread_version = await conn.fetchval(
                    """
                    SELECT a.runtime_version
                    FROM agent_attempts a
                    JOIN agent_jobs j ON j.id = a.job_id
                    WHERE j.thread_id = $1 AND a.runtime_version IS NOT NULL
                    ORDER BY a.started_at DESC
                    LIMIT 1
                    """,
                    job_row["thread_id"],
                )
            runtime_version = resolve_runtime_version(
                runtime=job_row["runtime"],
                thread_version=thread_version,
                selected=selected_versions,
                available=runtime_versions_available,
                host_default=runtime_versions,
            )
            attempt_image_digest = sandbox_image_digest
            if isinstance(thread_sandbox, dict):
                # A retained chat executes in the already-created container,
                # not in the runner replica's current image. A rolling image
                # upgrade must therefore keep stamping the immutable image and
                # CLI version that this exact writable layer actually uses.
                # This also preserves an explicit NULL version: it means the
                # old container's image default, not today's selected version.
                runtime_version = thread_sandbox.get("runtime_version")
                attempt_image_digest = thread_sandbox.get("sandbox_image_digest")
            attempt = await conn.fetchrow(
                """
                INSERT INTO agent_attempts
                    (job_id, thread_id, attempt_no, lease_owner, lease_generation,
                     lease_expires_at, base_sha, host_id,
                     sandbox_runtime, sandbox_image_digest, runtime_version,
                     host_kernel, guest_kernel)
                VALUES ($1, $13, $2, $3, $4, NOW() + make_interval(secs => $5), $6, $7,
                        $8, $9, $10, $11, $12)
                RETURNING id, attempt_no, lease_generation,
                          sandbox_runtime, sandbox_image_digest, runtime_version
                """,
                job_id,
                next_no,
                worker_id,
                next_no,
                lease_ttl_seconds,
                job_row["base_sha"],
                host,
                sandbox_runtime,
                attempt_image_digest,
                runtime_version,
                host_kernel,
                guest_kernel,
                job_row["thread_id"],
            )
            await conn.execute(
                "UPDATE agent_jobs SET state = 'running', current_attempt_id = $2, "
                "updated_at = NOW() WHERE id = $1",
                job_id,
                attempt["id"],
            )
        claim = job_row_to_dict(job_row)
        claim["state"] = RUNNING
        claim["current_attempt_id"] = attempt["id"]
        claim["attempt_id"] = attempt["id"]
        claim["attempt_no"] = attempt["attempt_no"]
        claim["title_generation_pending"] = thread_title_needs_generation(
            job_row["title_source"], job_row["title"]
        )
        claim["lease_generation"] = attempt["lease_generation"]
        # The stamp as written, not as the host row reads later: a rolling
        # upgrade heartbeat can move that row after this attempt starts.
        claim["sandbox_runtime"] = attempt["sandbox_runtime"]
        claim["sandbox_image_digest"] = attempt["sandbox_image_digest"]
        claim["runtime_version"] = attempt["runtime_version"]
        # The runner receives the exact retained ownership row so it can ask
        # this host's supervisor to start the recorded container.
        claim["thread_sandbox"] = thread_sandbox
        return claim

    async def release_claim(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int | None = None,
        reason: str = "credential_unavailable",
    ) -> bool:
        """Hand a just-claimed job back to the queue without spending a retry.

        For the case where the claim was made and the turn then provably never
        began — the platform could not mint the repository credential, or the
        host could not start the process that would have run it. The job never
        got its turn, so making it pay for the attempt is wrong twice over: it
        waits out a lease it will never use, and after ``max_attempts`` such
        failures the reaper fails it outright. A brief GitHub outage would
        terminally fail every queued private-repo job without an agent ever
        starting.

        **Only before execution, and the database is what decides that.** A
        turn whose prompt reached the CLI is not releasable here at any
        caller's request: an admitted turn that is replayed is a second
        execution of the same instruction, which is the outcome the retained
        session rules exist to prevent. The membership's ``admitted_job_id`` is
        that proof, checked in the statement rather than by the caller, so a
        host that is wrong about what its child did cannot requeue work that
        ran. A caller whose turn has been admitted gets ``False`` and the lease
        decides, exactly as it would have.

        The attempt row stays, marked ``aborted``, with a control event saying
        why: history is append-only, and "this one never started" is exactly
        the kind of thing an operator later needs to see. It simply does not
        count — see the retry budget in :meth:`reap_expired`.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            released_row = await conn.fetchrow(
                """
                UPDATE agent_attempts
                SET status = $2, finished_at = NOW()
                WHERE id = $1 AND status = 'running'
                  AND ($3::bigint IS NULL OR lease_generation = $3)
                  AND lease_expires_at > NOW()
                  AND NOT EXISTS (
                        SELECT 1 FROM agent_thread_sandbox_memberships m
                        WHERE m.admitted_job_id = agent_attempts.job_id
                  )
                RETURNING job_id, host_id
                """,
                attempt_id,
                ATTEMPT_ABORTED,
                lease_generation,
            )
            if released_row is None:
                return False
            released = released_row["job_id"]
            # The store's own answer, not the caller's. `agent_job_events.job_id`
            # has no foreign key, so a mismatched (job_id, attempt_id) pair would
            # write a control event into a different job's stream — potentially a
            # different tenant's.
            await insert_event(
                conn,
                job_id=released,
                attempt_id=attempt_id,
                event_type=EVENT_ATTEMPT_ABORTED,
                payload={"reason": reason},
            )
            # Fenced on this attempt still being the current one, so a job that
            # has since moved on is never dragged back to `queued`.
            #
            # An owner who cancelled while we held the claim gets `cancelled`,
            # not `queued`. Requeueing them was a dead end: `claim_job` skips
            # queued rows with `cancel_requested`, and the reaper only reaches
            # jobs with a *running* attempt — which this no longer has — so the
            # job sat in `queued` that nothing could ever move again.
            released_state = await conn.fetchval(
                """
                UPDATE agent_jobs
                SET state = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,
                    terminal_resume_pending = cancel_requested,
                    current_attempt_id = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND current_attempt_id = $2 AND state = 'running'
                RETURNING state
                """,
                released,
                attempt_id,
            )
            # All close-attempt paths take locks in attempt -> job -> host
            # order. Previously transition took job -> attempt while this path
            # took attempt -> host -> job, so concurrent finishes and aborts
            # could form a cycle across two jobs. Hung off the guarded attempt
            # UPDATE above, this still runs once and cannot double-count even
            # when the job UPDATE changed no row.
            await release_host_slot(conn, released_row["host_id"])
            if released_state == CANCELLED:
                # A cancelled parent is a turn boundary like any other
                # terminal outcome: fail any child that cannot preserve
                # continuity in this transaction.
                await fail_discontinuous_follow_ups(conn, parent_job_id=released)
        return True

    async def heartbeat(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        lease_ttl_seconds: float,
    ) -> dict[str, Any]:
        """Extend a live lease and report cancellation, fenced by generation.

        Returns ``{"ok": False}`` when the attempt is no longer the owner
        (superseded, finished, or generation mismatch) — the worker must stop
        immediately. On success also returns ``cancel_requested`` so workers
        learn about cancellation without extra polling.
        """
        async with self._pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                UPDATE agent_attempts
                SET lease_expires_at = NOW() + make_interval(secs => $3)
                WHERE id = $1 AND lease_generation = $2 AND status = 'running'
                  AND lease_expires_at > NOW()
                RETURNING job_id
                """,
                attempt_id,
                lease_generation,
                lease_ttl_seconds,
            )
            if job_id is None:
                return {"ok": False}
            row = await conn.fetchrow(
                "SELECT state, cancel_requested FROM agent_jobs WHERE id = $1", job_id
            )
        return {
            "ok": True,
            "job_id": job_id,
            "state": row["state"],
            "cancel_requested": row["cancel_requested"],
        }

    async def transition(
        self,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None = None,
        base_sha: str | None = None,
    ) -> bool:
        """Move a job between states, fenced by the attempt's live lease.

        Terminal transitions also close the attempt (``finished``). Returns
        False when the caller lost the lease or the job is not in
        ``from_states`` — the caller must stop.

        ``base_sha`` fills in the commit the worker resolved, and only when the
        job does not already have one: an owner who pinned a commit must get a
        patch against *that* commit, so a worker may report the base it used
        but never overwrite the base it was given.

        Raises ``ValueError`` for a ``succeeded`` transition on a turn that
        never settled its native context: the schema refuses the row, and the
        caller — not this method — is what has to change.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Lock the attempt before its job, matching release_claim and the
            # reaper. Without an explicit first lock, the job UPDATE below and
            # a concurrent abort can each hold the row the other needs.
            live_attempt = await conn.fetchval(
                """
                SELECT id
                FROM agent_attempts
                WHERE id = $1 AND lease_generation = $2
                  AND status = 'running' AND lease_expires_at > NOW()
                FOR UPDATE
                """,
                attempt_id,
                lease_generation,
            )
            if live_attempt is None:
                return False
            # A success carries its native turn ordinal. The runner settles the
            # shared workspace before it reports success and settlement assigns
            # the ordinal; the schema refuses a succeeded row without one, and
            # nothing here guesses it on the caller's behalf — a success that
            # skipped settlement is a protocol violation and fails loudly.
            try:
                updated = await self._transition_job(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                    from_states=from_states,
                    to_state=to_state,
                    detail=detail,
                    base_sha=base_sha,
                )
            except asyncpg.CheckViolationError as exc:
                if "native_turn" not in str(exc):
                    raise
                raise ValueError(
                    "a succeeded turn must settle its native context before it finishes"
                ) from exc
            if updated is None:
                return False
            if to_state in TERMINAL_STATES:
                sandbox_transition = None
                # Close any retained-sandbox turn boundary in the same
                # transaction as the job outcome.  A successful turn has
                # already persisted ``last_settled_job_id`` and only clears
                # its admission receipt.  Finishing before that boundary is
                # either an admitted/ambiguous turn (LOST) or a pre-admission
                # failure (STOPPING, safe to contain without replaying stdin).
                #
                # This is also the crash backstop for the runner's finally:
                # a terminal job must never leave ``active_attempt_id`` stuck
                # on an attempt the reaper will no longer visit.
                if updated["thread_id"] is not None:
                    sandbox_transition = await conn.fetchrow(
                        """
                        UPDATE agent_thread_sandbox_memberships m
                        SET context_status = CASE
                                WHEN m.last_settled_job_id IS DISTINCT FROM $1
                                     AND (
                                        m.admitted_job_id = $1
                                        OR g.container_id IS NULL
                                     ) THEN 'lost'
                                ELSE m.context_status
                            END,
                            native_state = CASE
                                WHEN m.last_settled_job_id IS DISTINCT FROM $1
                                     AND (
                                        m.admitted_job_id = $1
                                        OR g.container_id IS NULL
                                     ) THEN 'lost'
                                ELSE m.native_state
                            END,
                            resident_state = CASE
                                WHEN m.last_settled_job_id = $1 THEN m.resident_state
                                WHEN m.admitted_job_id = $1 OR g.container_id IS NULL
                                    THEN 'lost'
                                ELSE 'restarting'
                            END,
                            active_attempt_id = CASE
                                WHEN m.active_attempt_id = $2 THEN NULL
                                ELSE m.active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN m.admitted_job_id = $1 THEN NULL
                                ELSE m.admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN m.admitted_job_id = $1 THEN NULL
                                ELSE m.admitted_at
                            END,
                            last_error = CASE
                                WHEN m.last_settled_job_id = $1 THEN m.last_error
                                ELSE COALESCE($4, 'retained turn ended before settlement')
                            END,
                            updated_at = NOW()
                        FROM agent_sandboxes g
                        WHERE m.thread_id = $3 AND g.id = m.sandbox_id
                          AND (
                                m.active_attempt_id = $2
                                OR m.admitted_job_id = $1
                          )
                        RETURNING m.context_status, m.sandbox_id,
                                  m.last_settled_job_id = $1 AS settled
                        """,
                        job_id,
                        attempt_id,
                        updated["thread_id"],
                        detail,
                    )
                    if sandbox_transition is not None:
                        await conn.execute(
                            """
                            UPDATE agent_sandboxes g
                            SET state = CASE
                                    WHEN $3 AND g.container_id IS NULL THEN 'lost'
                                    WHEN $2 THEN 'running'
                                    WHEN g.active_operation_count > 0 OR EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                    ) THEN 'running'
                                    WHEN EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.thread_id <> $4
                                          AND peer.context_status <> 'lost'
                                          AND peer.native_state <> 'lost'
                                    ) THEN 'running'
                                    ELSE 'stopping'
                                END,
                                last_activity_at = NOW(),
                                idle_deadline = CASE
                                    WHEN (
                                        $2 OR EXISTS (
                                            SELECT 1
                                            FROM agent_thread_sandbox_memberships peer
                                            WHERE peer.sandbox_id = g.id
                                              AND peer.thread_id <> $4
                                              AND peer.context_status <> 'lost'
                                              AND peer.native_state <> 'lost'
                                        )
                                     )
                                     AND g.active_operation_count = 0
                                     AND NOT EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                     )
                                    THEN COALESCE(
                                        g.idle_deadline,
                                        NOW() + make_interval(secs => g.idle_timeout_seconds)
                                    )
                                    ELSE NULL
                                END,
                                updated_at = NOW()
                            WHERE g.id = $1
                            """,
                            sandbox_transition["sandbox_id"],
                            bool(sandbox_transition["settled"]),
                            sandbox_transition["context_status"] == "lost",
                            updated["thread_id"],
                        )
                    if (
                        sandbox_transition is not None
                        and sandbox_transition["context_status"] == "lost"
                    ):
                        await conn.execute(
                            "UPDATE agent_attempts SET context_status = 'lost' WHERE id = $1",
                            attempt_id,
                        )
                        await fail_unrunnable_follow_ups(
                            conn,
                            thread_id=updated["thread_id"],
                        )
                finished = await conn.fetchrow(
                    "UPDATE agent_attempts SET status = 'finished', finished_at = NOW() "
                    "WHERE id = $1 AND status = 'running' RETURNING host_id",
                    attempt_id,
                )
                # Only when this call is the one that closed the attempt. The
                # job-level fence above already makes a second terminal
                # transition impossible, but the slot must not depend on that
                # being true two layers away.
                if finished is not None:
                    await release_host_slot(conn, finished["host_id"])
                await conn.execute(
                    "UPDATE agent_jobs SET terminal_resume_pending = FALSE WHERE id = $1",
                    job_id,
                )
                # Native-session continuity is ready at settlement. Group
                # snapshot publication is asynchronous and never gates the
                # next message in this shared workspace.
                if not (
                    sandbox_transition is not None
                    and sandbox_transition["context_status"] == "lost"
                ):
                    await fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
        return True

    async def append_event(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
        event_seq: int | None = None,
    ) -> int | None:
        """Append one event, fenced by the live lease.

        Returns the global event id (the SSE ``Last-Event-ID`` cursor), or
        ``None`` when the caller lost the lease. The row lock on the attempt
        serializes ``seq`` assignment for the single legitimate writer.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            attempt = await conn.fetchrow(
                "SELECT job_id, status, lease_generation, "
                "lease_expires_at <= NOW() AS expired "
                "FROM agent_attempts WHERE id = $1 FOR UPDATE",
                attempt_id,
            )
            if (
                attempt is None
                or attempt["status"] != ATTEMPT_RUNNING
                or attempt["lease_generation"] != lease_generation
                or attempt["expired"]
            ):
                return None
            phase = (payload or {}).get("phase") if event_type == "lifecycle" else None
            if phase == "workspace_ready":
                await conn.execute(
                    "UPDATE agent_attempts SET terminal_ready = TRUE WHERE id = $1",
                    attempt_id,
                )
            elif phase in {"workspace_preparing", "workspace_finalizing"}:
                await conn.execute(
                    "UPDATE agent_attempts SET terminal_ready = FALSE WHERE id = $1",
                    attempt_id,
                )
            return await insert_event(
                conn,
                job_id=attempt["job_id"],
                attempt_id=attempt_id,
                event_type=event_type,
                payload=payload,
                client_seq=event_seq,
            )

    async def record_continuity_mode(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        mode: str | None,
        context_status: str | None = None,
    ) -> bool:
        """Stamp the diagnostic rung and optional product context semantic."""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    UPDATE agent_attempts
                    SET continuity_mode = COALESCE($3, continuity_mode),
                        context_status = COALESCE($4, context_status)
                    WHERE id = $1 AND lease_generation = $2
                      AND status = 'running' AND lease_expires_at > NOW()
                    RETURNING id
                    """,
                    attempt_id,
                    lease_generation,
                    mode,
                    context_status,
                )
            )

    async def record_attempt_resources(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        rollup: dict[str, Any],
    ) -> bool:
        """Store one attempt's resource rollup, lease-fenced and idempotent.

        Fenced like every other worker write: a superseded attempt must not be
        able to overwrite what the live one consumed. Idempotent because the
        runner may retry the post — a rollup describes a closed window, so a
        repeat is the same window said twice, never a second window to add.

        Absent counters stay NULL. Current shared-member writers intentionally
        cannot assign whole-container totals to one member; historical sampler
        failures likewise must not be reported as a machine that used nothing.

        The per-call rows ride the same fence and the same transaction: they
        describe spans inside this window, so a caller that could not write
        the window must not be able to write its parts. They are replaced
        rather than merged, because the list is one statement about the whole
        attempt — a retry reporting fewer calls than a previous post means
        fewer calls, not a set to union with.
        """
        tool_calls = rollup.get("tool_calls") or []
        processes = rollup.get("processes") or []
        async with self._pool.acquire() as conn, conn.transaction():
            recorded = bool(
                await conn.fetchval(
                    """
                    WITH live AS (
                        SELECT id, job_id
                        FROM agent_attempts
                        WHERE id = $1 AND lease_generation = $2
                          AND status = 'running' AND lease_expires_at > NOW()
                    )
                    INSERT INTO agent_attempt_resources (
                        attempt_id, job_id, cpu_usec, mem_peak_bytes, mem_avg_bytes,
                        io_read_bytes, io_write_bytes, net_rx_bytes, net_tx_bytes,
                        sample_count, coverage, sampler_version, tool_calls_truncated,
                        tool_time_ms, process_observer, process_observation_complete,
                        process_events_lost, processes_truncated
                    )
                    SELECT live.id, live.job_id, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                           $13, $14, $15, $16, $17, $18
                    FROM live
                    ON CONFLICT (attempt_id) DO UPDATE SET
                        cpu_usec = EXCLUDED.cpu_usec,
                        mem_peak_bytes = EXCLUDED.mem_peak_bytes,
                        mem_avg_bytes = EXCLUDED.mem_avg_bytes,
                        io_read_bytes = EXCLUDED.io_read_bytes,
                        io_write_bytes = EXCLUDED.io_write_bytes,
                        net_rx_bytes = EXCLUDED.net_rx_bytes,
                        net_tx_bytes = EXCLUDED.net_tx_bytes,
                        sample_count = EXCLUDED.sample_count,
                        coverage = EXCLUDED.coverage,
                        sampler_version = EXCLUDED.sampler_version,
                        tool_calls_truncated = EXCLUDED.tool_calls_truncated,
                        tool_time_ms = EXCLUDED.tool_time_ms,
                        process_observer = EXCLUDED.process_observer,
                        process_observation_complete = EXCLUDED.process_observation_complete,
                        process_events_lost = EXCLUDED.process_events_lost,
                        processes_truncated = EXCLUDED.processes_truncated
                    RETURNING attempt_id
                    """,
                    attempt_id,
                    lease_generation,
                    rollup.get("cpu_usec"),
                    rollup.get("mem_peak_bytes"),
                    rollup.get("mem_avg_bytes"),
                    rollup.get("io_read_bytes"),
                    rollup.get("io_write_bytes"),
                    rollup.get("net_rx_bytes"),
                    rollup.get("net_tx_bytes"),
                    int(rollup.get("sample_count") or 0),
                    float(rollup.get("coverage") or 0.0),
                    str(rollup.get("sampler_version") or "unknown"),
                    bool(rollup.get("tool_calls_truncated")),
                    rollup.get("tool_time_ms"),
                    rollup.get("process_observer"),
                    bool(rollup.get("process_observation_complete")),
                    int(rollup.get("process_events_lost") or 0),
                    bool(rollup.get("processes_truncated")),
                )
            )
            if not recorded:
                return False
            await conn.execute(
                "DELETE FROM agent_tool_call_resources WHERE attempt_id = $1",
                attempt_id,
            )
            if tool_calls:
                await conn.executemany(
                    """
                    INSERT INTO agent_tool_call_resources (
                        attempt_id, call_seq, tool_use_id, tool_name, timing_source,
                        duration_ms, sample_count, overlap_n, measured_span_ms,
                        cpu_usec_exclusive, cpu_usec_shared,
                        mem_peak_exclusive_bytes, mem_peak_shared_bytes, process_count,
                        process_cpu_usec, process_mem_peak_bytes,
                        process_io_read_bytes, process_io_write_bytes,
                        process_resources_partial
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                            $15, $16, $17, $18, $19)
                    """,
                    [
                        (
                            attempt_id,
                            int(call["call_seq"]),
                            str(call["tool_use_id"]),
                            str(call["tool_name"]),
                            str(call.get("timing_source") or "message"),
                            call.get("duration_ms"),
                            int(call.get("sample_count") or 0),
                            int(call.get("overlap_n") or 1),
                            call.get("measured_span_ms"),
                            call.get("cpu_usec_exclusive"),
                            call.get("cpu_usec_shared"),
                            call.get("mem_peak_exclusive_bytes"),
                            call.get("mem_peak_shared_bytes"),
                            int(call.get("process_count") or 0),
                            call.get("process_cpu_usec"),
                            call.get("process_mem_peak_bytes"),
                            call.get("process_io_read_bytes"),
                            call.get("process_io_write_bytes"),
                            bool(call.get("process_resources_partial")),
                        )
                        for call in tool_calls
                    ],
                )
            await conn.execute(
                "DELETE FROM agent_process_observations WHERE attempt_id = $1",
                attempt_id,
            )
            if processes:
                await conn.executemany(
                    """
                    INSERT INTO agent_process_observations (
                        attempt_id, process_seq, uid, pid, tgid, ppid, comm, executable,
                        started_offset_ms, duration_ms, exit_code,
                        observed_fork, observed_exec, observed_exit, tool_call_seqs,
                        cpu_usec, mem_peak_bytes, io_read_bytes, io_write_bytes,
                        io_rchar_bytes, io_wchar_bytes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                            $14, $15, $16, $17, $18, $19, $20, $21)
                    """,
                    [
                        (
                            attempt_id,
                            int(process["process_seq"]),
                            int(process["uid"]),
                            int(process["pid"]),
                            int(process["tgid"]),
                            process.get("ppid"),
                            str(process["comm"]),
                            process.get("executable"),
                            int(process.get("started_offset_ms") or 0),
                            process.get("duration_ms"),
                            process.get("exit_code"),
                            bool(process.get("observed_fork")),
                            bool(process.get("observed_exec")),
                            bool(process.get("observed_exit")),
                            [int(seq) for seq in process.get("tool_call_seqs") or []],
                            process.get("cpu_usec"),
                            process.get("mem_peak_bytes"),
                            process.get("io_read_bytes"),
                            process.get("io_write_bytes"),
                            process.get("io_rchar_bytes"),
                            process.get("io_wchar_bytes"),
                        )
                        for process in processes
                    ],
                )
            return True

    async def record_attempt_turn_summary(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
        measurement_id: str,
        report_seq: int,
        state: str,
        summary: dict[str, Any],
    ) -> TurnSummaryWrite:
        """Store one cumulative ``member_execution_window`` snapshot per attempt.

        Fenced like every other worker write: a superseded attempt must not be
        able to relabel what the live one consumed. The fence predicate is
        evaluated against the locked attempt row and the database clock, so a
        lease that expired a microsecond ago cannot write even if the reaper
        has not noticed it yet.

        Unlike :meth:`record_attempt_resources`, this body is a cumulative
        snapshot rather than a closed window. The runner posts checkpoints
        while the turn runs and a final one when it ends, and a delivery can be
        duplicated, reordered, or replayed after a retry. ``report_seq`` is
        therefore the ordering key and ``turn_summary_seq`` is what it was
        accepted at: a lower or equal-and-different sequence must never win,
        because that would let a stale checkpoint roll a measured window
        backwards. Equal sequences with a byte-identical payload are the same
        snapshot arriving twice, which is what a retry of an acknowledged post
        looks like.

        The first accepted snapshot fixes the attempt's ``measurement_id``.
        A later report naming a different measurement is not an update to the
        same window — it is a different window claiming the same attempt, and
        accepting it would silently splice two measurements together.

        This never touches the legacy aggregate columns or the tool-call and
        process child rows. Those describe what the container sampler and the
        process observer saw, which is independent evidence; this snapshot
        describes the member's own window. Neither replaces the other, so a
        turn may have either, both, or neither.

        When this runs first it inserts a legacy-compatible base row — the
        same "no container totals were attributed to this member" statement
        ``shared-member-v1`` already makes, never a fabricated measurement.

        Args:
            attempt_id: The attempt the snapshot describes.
            lease_generation: The caller's fencing generation for that attempt.
            measurement_id: The window identity fixed at the first accepted report.
            report_seq: Strictly monotonic per ``measurement_id``.
            state: ``collecting`` or ``final`` as the guest reported it.
            summary: The canonical snake_case snapshot to store.

        Returns:
            ``stored`` when this call accepted the snapshot, ``duplicate`` when
            the identical snapshot was already stored, ``conflict`` when the
            ordering rules refuse it, and ``lease_lost`` when the attempt is
            not running under this lease. Only ``stored`` and ``duplicate`` are
            success for the caller; the other two are not retryable.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Lock the attempt before reading its resource row. The lock is
            # what makes the read-decide-write below atomic: two concurrent
            # checkpoints of the same attempt serialize here, so the second
            # one sees what the first wrote instead of racing it.
            fenced = await conn.fetchrow(
                """
                SELECT id, job_id FROM agent_attempts
                WHERE id = $1 AND lease_generation = $2
                  AND status = 'running' AND lease_expires_at > NOW()
                FOR UPDATE
                """,
                attempt_id,
                lease_generation,
            )
            if fenced is None:
                return "lease_lost"
            inserted = await conn.fetchval(
                """
                INSERT INTO agent_attempt_resources (
                    attempt_id, job_id, sample_count, coverage, sampler_version,
                    turn_summary, turn_summary_seq, turn_summary_received_at
                )
                VALUES ($1, $2, 0, 0, 'shared-member-v1', $3::jsonb, $4, NOW())
                ON CONFLICT (attempt_id) DO NOTHING
                RETURNING attempt_id
                """,
                attempt_id,
                fenced["job_id"],
                json.dumps(summary),
                report_seq,
            )
            if inserted is not None:
                return "stored"
            stored = await conn.fetchrow(
                """
                SELECT turn_summary, turn_summary_seq
                FROM agent_attempt_resources
                WHERE attempt_id = $1
                """,
                attempt_id,
            )
            if stored is None:
                # The row must exist after the INSERT above found a conflict.
                # Reaching here means it was deleted between the two
                # statements, and the safe answer is that nothing was written.
                return "conflict"
            stored_summary = load_json(stored["turn_summary"])
            stored_object = stored_summary if isinstance(stored_summary, dict) else {}
            decision = self._turn_summary_decision(
                stored_seq=stored["turn_summary_seq"],
                stored_summary=stored_summary,
                stored_measurement=stored_object.get("measurement_id"),
                stored_state=stored_object.get("state"),
                measurement_id=measurement_id,
                report_seq=report_seq,
                state=state,
                summary=summary,
            )
            if decision != "accept":
                return decision
            updated = await conn.fetchval(
                """
                UPDATE agent_attempt_resources
                SET turn_summary = $2::jsonb,
                    turn_summary_seq = $3,
                    turn_summary_received_at = NOW()
                WHERE attempt_id = $1
                RETURNING attempt_id
                """,
                attempt_id,
                json.dumps(summary),
                report_seq,
            )
            if updated is None:
                return "conflict"
            return "stored"

    @staticmethod
    def _turn_summary_decision(
        *,
        stored_seq: int | None,
        stored_summary: Any,
        stored_measurement: Any,
        stored_state: Any,
        measurement_id: str,
        report_seq: int,
        state: str,
        summary: dict[str, Any],
    ) -> TurnSummaryDecision:
        """Whether an incoming snapshot may replace the stored one, and how.

        Separated from the statement so the ordering rules read as rules
        rather than as one long SQL condition, and so each refusal is a named
        decision: the caller gets ``conflict`` for every one of them, because
        none of them is a transient failure worth retrying.
        """
        if stored_seq is None:
            # The row exists for the legacy accounting only. The first
            # snapshot may take it over, and nothing can conflict with a
            # sequence that was never recorded.
            return "accept"
        if report_seq < stored_seq:
            return "conflict"
        if report_seq == stored_seq:
            # Same sequence means the same snapshot. A different payload at
            # the same sequence is two states claiming one position, and
            # last-write-wins would let whichever arrived second decide.
            # An identical payload is the retry this whole path exists for.
            return "duplicate" if stored_summary == summary else "conflict"
        # From here the incoming snapshot is newer, which still does not make
        # it authoritative: these two rules outrank recency.
        if stored_state == "final" and state == "collecting":
            return "conflict"
        if stored_measurement is not None and stored_measurement != measurement_id:
            return "conflict"
        return "accept"

    async def request_cancel(self, *, job_id: str, user_id: str | None = None) -> str | None:
        """Request cancellation; returns the job's resulting state.

        Queued jobs cancel immediately. Running jobs get the flag
        set — the worker observes it on its next heartbeat and performs the
        fenced transition. Terminal jobs are a no-op. ``user_id`` scopes the
        cancel to the owner. Returns ``None`` for an unknown job.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT state FROM agent_jobs WHERE id = $1 "
                "AND ($2::text IS NULL OR user_id = $2) FOR UPDATE",
                job_id,
                user_id,
            )
            if row is None:
                return None
            state = row["state"]
            if state == QUEUED:
                await conn.execute(
                    "UPDATE agent_jobs SET state = 'cancelled', cancel_requested = TRUE, "
                    "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                    job_id,
                )
                await fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
                return CANCELLED
            if state == RUNNING:
                await conn.execute(
                    "UPDATE agent_jobs SET cancel_requested = TRUE, updated_at = NOW() "
                    "WHERE id = $1",
                    job_id,
                )
            return state

    async def reap_expired(self, *, max_attempts: int = 3) -> list[dict[str, Any]]:
        """Close expired attempts and requeue (or fail) their jobs.

        For each expired ``running`` attempt: mark it ``superseded``, append an
        ``attempt_superseded`` control event, then decide the job's fate —
        requeue while attempts remain, ``cancelled`` if cancellation was
        pending, ``failed`` once ``max_attempts`` is exhausted. Publication is
        a snapshot's lifecycle, not the job's, so no job is ever caught
        mid-publish here. Returns a summary per reaped attempt.
        """
        actions: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            expired = await conn.fetch(
                """
                SELECT a.id AS attempt_id, a.job_id, a.attempt_no, a.host_id,
                       j.state, j.cancel_requested, j.current_attempt_id,
                       member.thread_id AS sandbox_thread_id,
                       member.sandbox_id,
                       member.active_attempt_id AS sandbox_active_attempt_id,
                       member.admitted_job_id AS sandbox_admitted_job_id,
                       member.last_settled_job_id AS sandbox_last_settled_job_id,
                       member.residency_generation
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                LEFT JOIN (
                    agent_thread_sandbox_memberships member
                    JOIN agent_sandboxes sandbox
                      ON sandbox.id = member.sandbox_id
                ) ON member.thread_id = j.thread_id
                WHERE a.status = 'running' AND a.lease_expires_at < NOW()
                ORDER BY a.id
                FOR UPDATE OF a, j SKIP LOCKED
                """
            )
            for row in expired:
                attempt_id = row["attempt_id"]
                job_id = row["job_id"]
                await conn.execute(
                    "UPDATE agent_attempts SET status = 'superseded', finished_at = NOW() "
                    "WHERE id = $1",
                    attempt_id,
                )
                # The SELECT took this row ``FOR UPDATE`` while it was still
                # ``running``, so this loop body runs once per attempt however
                # many reaper passes overlap — which is what makes the slot
                # release safe here without a second predicate.
                await release_host_slot(conn, row["host_id"])
                await insert_event(
                    conn,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type=EVENT_ATTEMPT_SUPERSEDED,
                    payload={"attempt_no": row["attempt_no"], "reason": "lease_expired"},
                )
                sandbox_thread_id = row["sandbox_thread_id"]
                sandbox_active_attempt_id = row["sandbox_active_attempt_id"]
                sandbox_admitted_job_id = row["sandbox_admitted_job_id"]
                sandbox_last_settled_job_id = row["sandbox_last_settled_job_id"]
                settled_retained_turn = sandbox_last_settled_job_id == job_id
                admitted_ambiguously = (
                    sandbox_admitted_job_id == job_id and not settled_retained_turn
                )
                owns_retained_activation = sandbox_active_attempt_id == attempt_id
                retained_activation_expired = (
                    admitted_ambiguously or owns_retained_activation
                ) and not settled_retained_turn
                settled_finalization_expired = (
                    settled_retained_turn and sandbox_admitted_job_id == job_id
                )
                cleanup_residency: dict[str, Any] | None = None
                if retained_activation_expired:
                    # The admission receipt identifies a definitely ambiguous
                    # prompt. Even before admission, an expired runner may
                    # have created a container without returning its identity
                    # to the control plane. The preview therefore fails all
                    # expired retained activations closed instead of guessing
                    # that a retry is safe; ordinary CLI/process replacement
                    # at a clean turn boundary remains supported.
                    lost_member = await conn.fetchrow(
                        """
                        UPDATE agent_thread_sandbox_memberships
                        SET native_state = 'lost', context_status = 'lost',
                            resident_state = 'lost',
                            active_attempt_id = CASE
                                WHEN active_attempt_id = $2 THEN NULL
                                ELSE active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_at
                            END,
                            last_error = CASE
                                WHEN admitted_job_id = $1
                                    THEN 'attempt lease expired after prompt admission'
                                ELSE 'retained activation lease expired'
                            END,
                            updated_at = NOW()
                        WHERE thread_id = $3 AND sandbox_id = $4
                          AND last_settled_job_id IS DISTINCT FROM $1
                          AND (active_attempt_id = $2 OR admitted_job_id = $1)
                        RETURNING residency_generation
                        """,
                        job_id,
                        attempt_id,
                        sandbox_thread_id,
                        row["sandbox_id"],
                    )
                    if lost_member is not None:
                        await conn.execute(
                            """
                            UPDATE agent_sandboxes g
                            SET state = CASE
                                    WHEN g.container_id IS NULL THEN 'lost'
                                    WHEN g.active_operation_count > 0 OR EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                    ) THEN 'running'
                                    WHEN EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.context_status <> 'lost'
                                          AND peer.native_state <> 'lost'
                                    ) THEN 'running'
                                    ELSE 'stopping'
                                END,
                                last_activity_at = NOW(),
                                idle_deadline = CASE
                                    WHEN g.container_id IS NOT NULL
                                     AND g.active_operation_count = 0
                                     AND NOT EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.active_attempt_id IS NOT NULL
                                     )
                                     AND EXISTS (
                                        SELECT 1
                                        FROM agent_thread_sandbox_memberships peer
                                        WHERE peer.sandbox_id = g.id
                                          AND peer.context_status <> 'lost'
                                          AND peer.native_state <> 'lost'
                                     )
                                    THEN NOW() + make_interval(
                                        secs => g.idle_timeout_seconds
                                    )
                                    ELSE NULL
                                END,
                                updated_at = NOW()
                            WHERE g.id = $1
                            """,
                            row["sandbox_id"],
                        )
                        residency_generation = int(lost_member["residency_generation"] or 0)
                        if residency_generation > 0:
                            released = await conn.fetchrow(
                                """
                                UPDATE agent_thread_sandbox_memberships m
                                SET residency_status = 'none',
                                    residency_last_active_at = NOW()
                                FROM agent_sandboxes g
                                WHERE m.thread_id = $1 AND m.residency_generation = $2
                                  AND m.residency_status <> 'none'
                                  AND g.id = m.sandbox_id
                                RETURNING m.thread_id,
                                          m.residency_generation AS generation,
                                          g.host, m.residency_grant_id AS grant_id
                                """,
                                sandbox_thread_id,
                                residency_generation,
                            )
                            if released is not None:
                                cleanup_residency = dict(released)
                    await conn.execute(
                        "UPDATE agent_attempts SET context_status = 'lost' WHERE id = $1",
                        attempt_id,
                    )
                    await fail_unrunnable_follow_ups(
                        conn,
                        thread_id=sandbox_thread_id,
                    )
                elif settled_finalization_expired:
                    # The native context boundary is durable; only the final
                    # job transition was lost. Never replay this prompt and
                    # never relabel its usable context LOST. Close admission,
                    # preserve the settled idle deadline, and fail the job for
                    # explicit operator/user review.
                    await conn.execute(
                        """
                        UPDATE agent_thread_sandbox_memberships
                        SET active_attempt_id = CASE
                                WHEN active_attempt_id = $2 THEN NULL
                                ELSE active_attempt_id
                            END,
                            admitted_job_id = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_job_id
                            END,
                            admitted_at = CASE
                                WHEN admitted_job_id = $1 THEN NULL
                                ELSE admitted_at
                            END,
                            last_error = 'job finalization expired after context settlement',
                            updated_at = NOW()
                        WHERE thread_id = $3
                          AND last_settled_job_id = $1
                        """,
                        job_id,
                        attempt_id,
                        sandbox_thread_id,
                    )
                if row["current_attempt_id"] != attempt_id or row["state"] in TERMINAL_STATES:
                    # Stale attempt of an already-moved-on job: closing it is enough.
                    action = "closed_stale"
                elif retained_activation_expired:
                    action = CANCELLED if row["cancel_requested"] else FAILED
                    await conn.execute(
                        """
                        UPDATE agent_jobs
                        SET state = $2,
                            detail = $3,
                            terminal_resume_pending = TRUE,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        job_id,
                        action,
                        (
                            "cancelled after an indeterminate retained Agent CLI turn"
                            if row["cancel_requested"]
                            else (
                                "retained Agent CLI turn became indeterminate after prompt admission"
                                if admitted_ambiguously
                                else "retained sandbox activation lease expired; context marked lost"
                            )
                        ),
                    )
                elif settled_finalization_expired:
                    action = CANCELLED if row["cancel_requested"] else FAILED
                    await conn.execute(
                        """
                        UPDATE agent_jobs
                        SET state = $2,
                            detail = $3,
                            terminal_resume_pending = TRUE,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        job_id,
                        action,
                        (
                            "cancelled after the Agent CLI turn settled"
                            if row["cancel_requested"]
                            else (
                                "Agent CLI context settled, but job finalization expired; "
                                "the prompt was not replayed"
                            )
                        ),
                    )
                elif row["cancel_requested"]:
                    action = CANCELLED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'cancelled', "
                        "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                        job_id,
                    )
                elif await self._attempts_spent(conn, job_id) >= max_attempts:
                    action = FAILED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'failed', detail = $2, "
                        "terminal_resume_pending = TRUE, updated_at = NOW() WHERE id = $1",
                        job_id,
                        f"exhausted {max_attempts} attempts (lease expired)",
                    )
                else:
                    action = QUEUED
                    await conn.execute(
                        "UPDATE agent_jobs SET state = 'queued', updated_at = NOW() WHERE id = $1",
                        job_id,
                    )
                if action in TERMINAL_STATES and not retained_activation_expired:
                    # A terminal reaper outcome is semantically the same turn
                    # boundary as ``transition``. Keep the linear chat moving
                    # when the context is usable; the LOST branch above has
                    # already failed every unrunnable descendant instead.
                    await fail_discontinuous_follow_ups(conn, parent_job_id=job_id)
                action_result: dict[str, Any] = {
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "action": action,
                }
                if cleanup_residency is not None:
                    action_result["cleanup_residency"] = cleanup_residency
                actions.append(action_result)
        return actions

    @staticmethod
    async def _attempts_spent(conn: Any, job_id: str) -> int:
        """Count attempts that actually got their turn.

        The retry budget used to be read off ``attempt_no``, which numbers
        every claim including ones the platform abandoned before the agent
        started. Counting instead means an aborted claim is free: numbers still
        advance (they are referenced by events, and gaps are informative), but
        only attempts that ran spend the budget.
        """
        return await conn.fetchval(
            "SELECT count(*) FROM agent_attempts WHERE job_id = $1 AND status <> $2",
            job_id,
            ATTEMPT_ABORTED,
        )

    @staticmethod
    async def _transition_job(
        conn: Any,
        *,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        from_states: tuple[str, ...],
        to_state: str,
        detail: str | None,
        base_sha: str | None,
    ) -> Any:
        """The fenced job UPDATE behind :meth:`transition`; ``None`` when fenced out."""
        return await conn.fetchrow(
            """
            UPDATE agent_jobs j
            SET state = $4,
                detail = COALESCE($5, j.detail),
                base_sha = COALESCE(j.base_sha, $7),
                updated_at = NOW()
            WHERE j.id = $1
              AND j.state = ANY($3::text[])
              AND j.current_attempt_id = $2
              AND EXISTS (
                    SELECT 1 FROM agent_attempts a
                    WHERE a.id = $2 AND a.lease_generation = $6
                      AND a.status = 'running'
                      AND a.lease_expires_at > NOW()
              )
            RETURNING j.id, j.thread_id
            """,
            job_id,
            attempt_id,
            list(from_states),
            to_state,
            detail,
            lease_generation,
            base_sha,
        )
