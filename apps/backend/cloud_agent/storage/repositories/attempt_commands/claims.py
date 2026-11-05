"""Claiming a queued turn for a host, and releasing a claim."""

from __future__ import annotations

import json
import math
from typing import Any

from cloud_agent.domain.execution import (
    ATTEMPT_ABORTED,
    CANCELLED,
    EVENT_ATTEMPT_ABORTED,
    FAILED,
    QUEUED,
    RUNNING,
)
from cloud_agent.domain.execution_profiles import (
    KATA_RUNTIME,
    ExecutionProfile,
    compatible_shared_sandbox_followup_attested,
    shared_sandbox_claim_attested,
)
from cloud_agent.domain.host_pool import CLAIMABLE_STATUSES, HOST_ONLINE
from cloud_agent.domain.runtimes import resolve_runtime_version
from cloud_agent.domain.sandbox_group import GroupState
from cloud_agent.sandbox_archive.sandbox_archive_credentials import stored_archive_policy
from cloud_agent.storage._sql.compute_admission import host_compute_admission, lock_compute_topology
from cloud_agent.storage._sql.environment_runs import env_run_blocks_host, env_runs_holding_slots
from cloud_agent.storage._sql.events import insert_event
from cloud_agent.storage._sql.goals import goal_after_terminal
from cloud_agent.storage._sql.hosts import (
    DELETED_GROUP_STATES_SQL,
    SILENT_HOST_GROUP_SQL,
    host_slots_exhausted,
    release_host_slot,
    take_silent_host_group,
)
from cloud_agent.storage._sql.jobs import (
    QUALIFIED_JOB_COLUMNS,
    fail_discontinuous_follow_ups,
    fail_unrunnable_follow_ups,
    lock_conversation_by_job,
    runnable_queued_turn,
)
from cloud_agent.storage._sql.rows import job_row_to_dict, load_json
from cloud_agent.storage._sql.sandbox import (
    THREAD_SANDBOX_CLAIM_SOURCE,
    THREAD_SANDBOX_SOURCE,
    release_restore,
    restore_claim_due,
)
from cloud_agent.storage._sql.sandbox_transitions import (
    CLAIM_COLD_GROUP_FOR_RESTORE_SQL,
    RELEASE_CLAIM_MEMBERS_SQL,
    SET_RESTORE_ATTEMPT_SQL,
)
from cloud_agent.storage.repositories.attempt_commands._common import AttemptCommandsBase
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_CAPABILITY,
    SandboxArchivePolicyError,
)
from cloud_agent_shared.sandbox_restore import (
    MAX_RESTORE_ATTEMPTS,
    RESTORE_FAILED_RELEASE_REASON,
    SANDBOX_RESTORE_CAPABILITY,
)


class ClaimsMixin(AttemptCommandsBase):
    """Claim turns and release claims."""

    async def sweep_queued_follow_ups(self) -> None:
        """Fail queued follow-ups whose earlier native context is unavailable.

        Claiming already excludes these rows. This periodic safety net gives
        them a terminal answer using the partial queued indexes, without a
        cleanup scan on every claim poll: legacy pending turns, and the turns a
        Fork sibling had queued when a writer for another chat lost the group
        they share.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await fail_discontinuous_follow_ups(conn)

    async def claim_job(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
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
        exact claiming replica. New groups require the completed target
        deployment; a replica of the previous generation may claim only an
        existing group on its owning host. ``untrusted-kata-v1`` additionally
        requires the Kata runtime plus unequal host/guest kernels. The mutable
        heartbeat row is not evidence for a particular replica during a
        rolling deploy.
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
        runner_capabilities = attestation.get("runner_capabilities")
        if not isinstance(runner_capabilities, list):
            runner_capabilities = []
        attested_archive_revision = attestation.get("sandbox_archive_settings_applied_revision")
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
                """
                SELECT active_host, runtime_versions, sandbox_archive_settings,
                       sandbox_archive_revision
                FROM agent_runner_policy WHERE id FOR SHARE
                """
            )
            await lock_compute_topology(conn)
            resource_new, resource_claims = await host_compute_admission(conn, host)
            if not resource_claims:
                return None
            active_host = None if policy is None else policy["active_host"]
            selected_versions = (
                {} if policy is None else load_json(policy["runtime_versions"]) or {}
            )
            pin_allows_new_sandbox = resource_new and (active_host is None or host == active_host)
            host_row = await conn.fetchrow(
                "SELECT status, capabilities, slots_total, slots_used, slots_desired, "
                "sandbox_archive_settings_applied_revision, "
                "(credential_hash IS NOT NULL AND credential_hash = $2) "
                "AS credential_matches "
                "FROM agent_runner_hosts WHERE host = $1",
                host,
                host_credential_hash,
            )
            # This read only chooses which profile may be considered, so it
            # locks nothing: the row is locked after the turn's group, below.
            # The slot UPDATE there is the decision point and closes concurrent
            # drain, revocation, and re-enrollment races.
            if host_row is None or host_row["status"] not in CLAIMABLE_STATUSES:
                return None
            if host_slots_exhausted(host_row):
                return None
            capabilities = load_json(host_row["capabilities"]) or []
            try:
                archive_policy = stored_archive_policy(policy)
            except SandboxArchivePolicyError:
                return None
            archive_required = archive_policy.enabled
            archive_revision = archive_policy.revision
            if archive_required and (
                SANDBOX_ARCHIVE_CAPABILITY not in capabilities
                or host_row["sandbox_archive_settings_applied_revision"] != archive_revision
                or SANDBOX_ARCHIVE_CAPABILITY not in runner_capabilities
                or isinstance(attested_archive_revision, bool)
                or not isinstance(attested_archive_revision, int)
                or attested_archive_revision != archive_revision
            ):
                return None
            # A cold group lives only in its checkpoint, which this replica's
            # sandbox manager must be able to download (archives on) and turn
            # back into the group (it attests restore). The host row and the
            # replica both say so: during a rolling deploy the row describes
            # whichever replica beat last. Restoring creates the group's only
            # container here, so the machine must also still take new groups:
            # not one whose lease is about to end, or that a hold keeps empty.
            restore_capable = bool(
                resource_new
                and archive_required
                and SANDBOX_RESTORE_CAPABILITY in capabilities
                and SANDBOX_RESTORE_CAPABILITY in runner_capabilities
            )
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
                SELECT {QUALIFIED_JOB_COLUMNS},
                       -- Read for the turn this hands out, once every condition
                       -- below has passed, not for each queued turn before it.
                       (
                           SELECT to_jsonb(s) FROM {THREAD_SANDBOX_SOURCE}
                           WHERE s.thread_id = j.thread_id
                       ) AS thread_sandbox,
                       t.title_source, t.title
                FROM agent_jobs j
                JOIN agent_threads t ON t.id = j.thread_id
                LEFT JOIN {THREAD_SANDBOX_CLAIM_SOURCE} ON s.thread_id = j.thread_id
                -- An automatic round is re-checked against the goal it was
                -- created for here, immediately before it is handed out.
                WHERE {runnable_queued_turn("j")}
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
                            AND NOT EXISTS (
                                SELECT 1 FROM agent_sandboxes deleting
                                WHERE deleting.id = s.sandbox_id
                                  AND deleting.deletion_state IN {DELETED_GROUP_STATES_SQL}
                            )
                            AND s.fork_operation_id IS NULL
                            AND s.context_status <> 'lost'
                            AND s.native_state <> 'lost'
                            AND s.state IN (
                                {GroupState.PROVISIONING.sql}, {GroupState.STARTING.sql},
                                {GroupState.RUNNING.sql}, {GroupState.STOPPED.sql}
                            )
                            AND s.shared_live
                        )
                        OR (
                            -- A group whose host is gone, held by its
                            -- checkpoint: any restore-capable host may take
                            -- it, and the claim below rehomes it. Never the
                            -- host it left while that one still names its
                            -- container: a Host moving out, or the same name
                            -- enrolled again, holds it under the group's
                            -- stable name. A failed restore has no container
                            -- and may be retried where it ran, after the wait
                            -- that follows each failed try.
                            s.state = {GroupState.COLD.sql}
                            AND (s.container_id IS NULL OR s.host IS DISTINCT FROM $2)
                            AND $7::boolean
                            AND $6::boolean
                            AND s.restore_attempts < {MAX_RESTORE_ATTEMPTS}
                            AND {restore_claim_due("s.restore_attempts", "s.group_last_activity_at")}
                            AND NOT EXISTS (
                                SELECT 1 FROM agent_sandboxes deleting
                                WHERE deleting.id = s.sandbox_id
                                  AND deleting.deletion_state IN {DELETED_GROUP_STATES_SQL}
                            )
                            AND s.fork_operation_id IS NULL
                            AND s.context_status <> 'lost'
                            AND s.native_state <> 'lost'
                            AND s.shared_live
                        )
                        OR (
                            -- A stopped group of a Host silent for a long
                            -- time, held by its checkpoint: a restore-capable
                            -- host may take it, and the claim below hands it
                            -- over and rehomes it. Until then it stays, so a
                            -- Host that returns continues with it.
                            s.state = {GroupState.STOPPED.sql}
                            AND s.host IS DISTINCT FROM $2
                            AND $7::boolean
                            AND $6::boolean
                            AND s.fork_operation_id IS NULL
                            AND s.context_status <> 'lost'
                            AND s.native_state <> 'lost'
                            AND s.shared_live
                            AND EXISTS (
                                SELECT 1 FROM agent_sandboxes g
                                WHERE g.id = s.sandbox_id AND {SILENT_HOST_GROUP_SQL}
                            )
                        )
                  )
                ORDER BY j.created_at
                LIMIT 1
                FOR UPDATE OF j SKIP LOCKED
                """,
                json.dumps(capabilities),
                host,
                secure_attestation,
                shared_attestation,
                pin_allows_new_sandbox,
                followup_attestation,
                restore_capable,
            )
            if job_row is None:
                return None
            job_id = job_row["id"]
            thread_sandbox = load_json(job_row["thread_sandbox"])
            restore = isinstance(thread_sandbox, dict) and thread_sandbox.get("state") == "cold"
            # Only the silent-Host branch admits a stopped group on another host.
            take_silent = (
                isinstance(thread_sandbox, dict)
                and thread_sandbox.get("state") == "stopped"
                and thread_sandbox.get("host") != host
            )
            handover = None
            if isinstance(thread_sandbox, dict):
                # Serialize a retained claim with an administrative stop. A
                # claim committed first is visible to the stop's busy check;
                # a stop committed first leaves this job queued. A cold group
                # is locked for the rehome below, so of two siblings claiming
                # it only the first moves it.
                claimable = await conn.fetchval(
                    f"SELECT (state IN ({GroupState.PROVISIONING.sql}, {GroupState.STARTING.sql}, "
                    f"{GroupState.RUNNING.sql}, {GroupState.STOPPED.sql}) "
                    f"OR ($2 AND state = {GroupState.COLD.sql} AND restore_checkpoint_id IS NOT NULL AND "
                    f"{restore_claim_due('restore_attempts', 'last_activity_at')})) "
                    "AND stop_requested_at IS NULL "
                    f"AND deletion_state NOT IN {DELETED_GROUP_STATES_SQL} "
                    "FROM agent_sandboxes "
                    "WHERE id = $1 " + ("FOR UPDATE" if restore or take_silent else "FOR SHARE"),
                    thread_sandbox["sandbox_id"],
                    restore,
                )
                if not claimable:
                    return None
                if take_silent:
                    # A savepoint, so a claim that then finds no slot leaves
                    # the group with its silent Host instead of cold.
                    handover = conn.transaction()
                    await handover.start()
                    if not await take_silent_host_group(
                        conn, sandbox_id=thread_sandbox["sandbox_id"], claimant=host
                    ):
                        await handover.rollback()
                        return None
                    restore = True
            # The host row after the group, as finishing or reaping a member
            # takes them: that writes the group and then gives its slot back,
            # so a claim holding the row while it waited for the group
            # deadlocked with it. Locked in a statement of its own, before the
            # slot UPDATE counts this host's environment runs: claiming one
            # locks this row without writing it, and a count that began while
            # it waited would miss the run.
            await conn.execute(
                "SELECT host FROM agent_runner_hosts WHERE host = $1 FOR UPDATE", host
            )
            # The atomic half of the slot claim. Under READ COMMITTED two
            # claims hitting the same host row serialize and the loser
            # re-evaluates capacity against the winner's committed value.
            took_slot = await conn.fetchval(
                f"""
                UPDATE agent_runner_hosts
                SET slots_used = slots_used + 1
                WHERE host = $1
                  AND status = '{HOST_ONLINE}'
                  AND (slots_total IS NULL OR slots_used + {env_runs_holding_slots("$1")} < slots_total)
                  AND (slots_desired IS NULL OR slots_used + {env_runs_holding_slots("$1")} < slots_desired)
                  AND NOT EXISTS (SELECT 1 FROM agent_environment_runs er
                      WHERE er.host=$1 AND {env_run_blocks_host("er")})
                  AND credential_hash = $2
                  AND (
                        $3::boolean
                        OR (
                            sandbox_archive_settings_applied_revision = $4
                            AND capabilities @> $5::jsonb
                        )
                  )
                RETURNING slots_used
                """,
                host,
                host_credential_hash,
                not archive_required,
                archive_revision,
                json.dumps([SANDBOX_ARCHIVE_CAPABILITY]),
            )
            if took_slot is None:
                if handover is not None:
                    await handover.rollback()
                return None
            if handover is not None:
                await handover.commit()
            if restore:
                # The fence that makes this host the group's only owner: a new
                # generation, no container yet, and the checkpoint kept as the
                # source. Anything the previous host still holds names the old
                # generation and is refused from here on.
                await conn.execute(
                    CLAIM_COLD_GROUP_FOR_RESTORE_SQL,
                    thread_sandbox["sandbox_id"],
                    host,
                )
            next_no = await conn.fetchval(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM agent_attempts WHERE job_id = $1",
                job_id,
            )
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
                VALUES ($1, $13, $2, $3, $4, clock_timestamp() + make_interval(secs => $5), $6, $7,
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
                "claim_retry_at = NULL, "
                "updated_at = NOW() WHERE id = $1",
                job_id,
                attempt["id"],
            )
            if restore:
                # However this attempt ends before the recreated container is
                # confirmed, ``release_restore`` finds the group by it.
                await conn.execute(
                    SET_RESTORE_ATTEMPT_SQL,
                    thread_sandbox["sandbox_id"],
                    attempt["id"],
                )
                thread_sandbox = load_json(
                    await conn.fetchval(
                        f"SELECT to_jsonb(s) FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                        job_row["thread_id"],
                    )
                )
            # Nothing is charged here. What this goal has spent is *derived*
            # from the rounds that have actually started — see
            # ``storage._sql.goals.ROUNDS_USED_SQL`` — so a round costs
            # nothing until this INSERT runs and costs once however often it is
            # retried. Keeping a counter here meant this statement also had to
            # update ``agent_threads`` while already holding this job's row
            # lock, which is the reverse of the thread-before-job order the
            # rest of the repository takes and deadlocked against pausing or
            # messaging at exactly the moment a runner claimed a round.
        claim = job_row_to_dict(job_row)
        claim["state"] = RUNNING
        claim["current_attempt_id"] = attempt["id"]
        claim["attempt_id"] = attempt["id"]
        claim["attempt_no"] = attempt["attempt_no"]
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
        retry_after_seconds: float = 0,
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

        ``retry_after_seconds`` delays eligibility after transient failures,
        allowing later queued jobs to run while this job's dependency recovers.
        Raises ``ValueError`` when the delay is not finite and nonnegative.
        """
        if not math.isfinite(retry_after_seconds) or retry_after_seconds < 0:
            raise ValueError("claim retry delay must be finite and nonnegative")
        async with self._pool.acquire() as conn, conn.transaction():
            if not await self._lock_live_attempt(conn, attempt_id, lease_generation):
                return False
            released_row = await conn.fetchrow(
                """
                UPDATE agent_attempts
                SET status = $2, finished_at = NOW()
                WHERE id = $1 AND status = 'running'
                  AND ($3::bigint IS NULL OR lease_generation = $3)
                  AND lease_expires_at > clock_timestamp()
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
            # Attempt taken, conversation next, job last — the same order
            # ``transition`` keeps. A release that lands on ``cancelled``
            # pauses an automatic round's goal, which writes the conversation.
            await lock_conversation_by_job(conn, job_id=released)
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
                    -- Cleared only on the way back to the queue. A cancelled
                    -- turn keeps whatever it reported, because that is the
                    -- answer it ended on; the next attempt has to give its own.
                    goal_report_parse = CASE WHEN cancel_requested
                                             THEN goal_report_parse END,
                    goal_report_outcome = CASE WHEN cancel_requested
                                               THEN goal_report_outcome END,
                    current_attempt_id = NULL,
                    claim_retry_at = CASE WHEN cancel_requested OR $3::double precision = 0 THEN NULL
                                         ELSE clock_timestamp() + make_interval(secs => $3) END,
                    updated_at = NOW()
                WHERE id = $1 AND current_attempt_id = $2 AND state = 'running'
                RETURNING state
                """,
                released,
                attempt_id,
                retry_after_seconds,
            )
            # A member this attempt reserved is released with it, as an
            # abandoned activation is. Nothing else clears it once the attempt
            # stops running, and a member still naming the attempt refused
            # every later reservation of its chat and kept its group busy. A
            # group this attempt was restoring is left to the restore release
            # below, which reads this reservation to tell whether it began.
            await conn.execute(
                RELEASE_CLAIM_MEMBERS_SQL,
                attempt_id,
            )
            # All close-attempt paths take locks in attempt -> job -> host
            # order. Previously transition took job -> attempt while this path
            # took attempt -> host -> job, so concurrent finishes and aborts
            # could form a cycle across two jobs. Hung off the guarded attempt
            # UPDATE above, this still runs once and cannot double-count even
            # when the job UPDATE changed no row.
            await release_host_slot(conn, released_row["host_id"])
            # A restore handed back before its container was confirmed leaves
            # the group to its checkpoint for the next claim. It spends one of
            # the group's attempts only if it may have begun: its host reserved
            # the group, or says the restore failed. A claim handed back before
            # that never reached the checkpoint, and counts as no failure.
            restored_state = await release_restore(
                conn,
                attempt_id,
                f"sandbox restore released: {reason}",
                reported_failed=reason == RESTORE_FAILED_RELEASE_REASON,
            )
            if restored_state == "lost":
                # The attempts ran out: this turn and those queued behind it
                # can never run, including the one just put back.
                thread_id = await conn.fetchval(
                    "SELECT thread_id FROM agent_jobs WHERE id = $1", released
                )
                await fail_unrunnable_follow_ups(
                    conn,
                    thread_id=thread_id,
                    detail="not run because the sandbox could not be restored",
                )
                if released_state == QUEUED:
                    # An automatic round failed with them stops its goal now,
                    # as the reaper's does when the attempts run out under it.
                    # Left active with nothing to run, the goal waited for the
                    # stall sweep to pause it as a failure nobody could explain.
                    await goal_after_terminal(
                        conn,
                        job_id=released,
                        thread_id=thread_id,
                        state=FAILED,
                        detail="the automatic round could not run: "
                        "its sandbox could not be restored",
                    )
            if released_state == CANCELLED:
                # The owner's Stop ended the turn before it began, as a cancel
                # in the queue does, and an automatic round stops its goal
                # with it. Left active with nothing to run, the goal was armed
                # again by the scheduling sweep, which queued another round.
                await goal_after_terminal(
                    conn,
                    job_id=released,
                    thread_id=await conn.fetchval(
                        "SELECT thread_id FROM agent_jobs WHERE id = $1", released
                    ),
                    state=CANCELLED,
                    detail="the automatic round was cancelled before it ran",
                )
                # A cancelled parent is a turn boundary like any other
                # terminal outcome: fail any child that cannot preserve
                # continuity in this transaction.
                await fail_discontinuous_follow_ups(conn, parent_job_id=released)
        return True
