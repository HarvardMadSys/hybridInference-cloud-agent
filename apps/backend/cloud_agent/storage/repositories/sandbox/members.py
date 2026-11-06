"""One member's activation in its sandbox group.

Reservation of the group and the member under the attempt fence, the
confirmed start, the CLI process and residency, prompt admission, and the
settle or abandon that closes the turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.domain.sandbox_group import DELETED_GROUP_STATES
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.fences import (
    LockMode,
    attempt_and_residency_are_live,
    attempt_lease_is_live,
    lock_running_attempt,
)
from cloud_agent.storage._sql.hosts import UNDELETED_GROUP_SQL
from cloud_agent.storage._sql.jobs import fail_unrunnable_follow_ups
from cloud_agent.storage._sql.rows import load_json, sandbox_row_to_dict
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE
from cloud_agent.storage._sql.sandbox_transitions import (
    ABANDON_THREAD_SANDBOX_GROUP_SQL,
    ABANDON_THREAD_SANDBOX_MEMBERS_SQL,
    ADMIT_THREAD_SANDBOX_TURN_GROUP_SQL,
    ADMIT_THREAD_SANDBOX_TURN_MEMBERS_SQL,
    INSERT_SANDBOX_GROUP_SQL,
    INSERT_SANDBOX_MEMBERSHIP_SQL,
    MARK_THREAD_SANDBOX_RUNNING_GROUP_AND_MEMBERS_SQL,
    RECORD_SHARED_THREAD_RESIDENCY_MEMBERS_SQL,
    RECORD_THREAD_SANDBOX_PROCESS_MEMBERS_SQL,
    RESERVE_ACTIVATE_GROUP_SQL,
    RESERVE_ACTIVATE_MEMBER_SQL,
    SETTLE_THREAD_SANDBOX_GROUP_SQL,
    SETTLE_THREAD_SANDBOX_MEMBERS_SQL,
)
from cloud_agent.storage.repositories.sandbox._common import SandboxRepositoryBase

if TYPE_CHECKING:
    import asyncpg


class MemberLifecycleMixin(SandboxRepositoryBase):
    """Reserve, start, admit, settle and abandon one member of a group."""

    async def reserve_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        host: str,
        sandbox_id: str,
        container_name: str,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None,
        execution_profile: str,
        resident_slot_capacity: int = 8,
        memory_limit: str = "4g",
        cpu_limit: str = "2",
        disk_limit_mib: int = 0,
        stop_timeout_seconds: int = 10,
        fork_helper_version: str | None = None,
    ) -> dict[str, Any] | None:
        """Reserve/start one thread's durable sandbox under its attempt fence.

        Unlike HOT residency this mapping is never taken over by another host:
        before persistent storage, the retained container's writable layer is
        the only copy. A different host must leave the turn queued rather than
        create an empty replacement and call it continuity.

        ``fork_helper_version`` is the image's ``claude-session-fork`` version;
        it is recorded with a new group and ignored for one that already exists.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn)
            # The attempt, then the conversation, then the turn. A first
            # reservation creates the group and membership, whose foreign keys
            # share-lock the conversation: taken only then, after the turn, it
            # crossed a cancel, which holds the conversation and waits for the
            # turn.
            await conn.execute("SELECT id FROM agent_attempts WHERE id = $1 FOR SHARE", attempt_id)
            await conn.execute(
                "SELECT id FROM agent_threads WHERE id = $1 FOR KEY SHARE", thread_id
            )
            earns = await conn.fetchrow(
                """
                SELECT j.id, j.thread_id, j.user_id
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.id = $1 AND a.lease_generation = $2
                  AND a.status = 'running'
                  AND a.host_id = $3 AND j.thread_id = $4
                  AND j.current_attempt_id = a.id
                FOR SHARE OF a, j
                """,
                attempt_id,
                lease_generation,
                host,
                thread_id,
            )
            if earns is None or not await attempt_lease_is_live(conn, attempt_id):
                return None
            # Claiming is fenced by the worker's shared-sandbox-v1
            # attestation. Every reservation therefore creates or reuses a
            # shared group; there is no one-to-one fallback.
            return await self._reserve_shared_thread_sandbox(
                conn,
                thread_id=thread_id,
                attempt_id=attempt_id,
                host=host,
                sandbox_id=sandbox_id,
                container_name=container_name,
                runtime_id=runtime_id,
                runtime_version=runtime_version,
                sandbox_image_digest=sandbox_image_digest,
                execution_profile=execution_profile,
                resident_slot_capacity=resident_slot_capacity,
                memory_limit=memory_limit,
                cpu_limit=cpu_limit,
                disk_limit_mib=disk_limit_mib,
                stop_timeout_seconds=stop_timeout_seconds,
                fork_helper_version=fork_helper_version,
                user_id=str(earns["user_id"]),
                job_id=str(earns["id"]),
            )

    async def mark_thread_sandbox_running(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        container_id: str,
        context_status: str,
    ) -> dict[str, Any] | None:
        """Confirm Docker start under both attempt and sandbox generations."""
        async with self._pool.acquire() as conn, conn.transaction():
            if not await lock_running_attempt(conn, attempt_id, lease_generation, LockMode.SHARE):
                return None
            locked_membership = await conn.fetchval(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1
                FOR UPDATE
                """,
                thread_id,
            )
            if locked_membership is None:
                return None
            locked_group = await conn.fetchrow(
                f"""
                SELECT g.id, g.state, g.restore_checkpoint_id
                FROM agent_sandboxes g
                JOIN agent_thread_sandbox_memberships m ON m.sandbox_id = g.id
                WHERE m.thread_id = $1 AND g.generation = $2
                  AND g.stop_requested_at IS NULL
                  AND {UNDELETED_GROUP_SQL}
                  AND (g.container_id IS NULL OR g.container_id = $3)
                FOR UPDATE OF g
                """,
                thread_id,
                sandbox_generation,
                container_id,
            )
            if locked_group is None or not await attempt_lease_is_live(conn, attempt_id):
                return None
            shared = await conn.fetchrow(
                MARK_THREAD_SANDBOX_RUNNING_GROUP_AND_MEMBERS_SQL,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                container_id,
                context_status,
            )
            if shared is None:
                return None
            if locked_group["state"] == "restoring" and locked_group["restore_checkpoint_id"]:
                # A host recreated this exact container from the checkpoint and
                # Docker started it: the object was there, whole, and loadable.
                await conn.execute(
                    "UPDATE agent_sandbox_checkpoints SET state = 'verified' "
                    "WHERE id = $1 AND state = 'reported'",
                    locked_group["restore_checkpoint_id"],
                )
            row = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        return None if row is None else sandbox_row_to_dict(row)

    async def record_thread_sandbox_process(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
        process_generation: int,
    ) -> bool:
        """Record a nondecreasing supervisor CLI process generation.

        Equal reports are idempotent while the process is starting or live,
        but cannot undo a later STOPPED/LOST observation. A delayed report
        older than the durable generation is rejected so it cannot authorize
        a stale stopped-process observation to release the newer process's
        residency.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if not await lock_running_attempt(conn, attempt_id, lease_generation, LockMode.SHARE):
                return False
            locked_membership = await conn.fetchval(
                """
                SELECT thread_id FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1 AND activation_generation = $2
                  AND residency_generation = $3 AND residency_status = 'hot'
                  AND active_attempt_id = $4
                FOR UPDATE
                """,
                thread_id,
                activation_generation,
                residency_generation,
                attempt_id,
            )
            if locked_membership is None or not await attempt_and_residency_are_live(
                conn, attempt_id, thread_id
            ):
                return False
            shared = await conn.fetchval(
                RECORD_THREAD_SANDBOX_PROCESS_MEMBERS_SQL,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                residency_generation,
                process_generation,
            )
            return shared is not None

    async def record_shared_thread_residency(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_id: str,
        residency_generation: int,
    ) -> bool:
        """Bind a residency and its immutable attribution before CLI startup."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The new ledger FK locks its attempt too. Acquire that lock
            # before membership, matching terminal transition/reaper order,
            # and keep the live-attempt fence stable through the capture.
            if not await lock_running_attempt(conn, attempt_id, lease_generation, LockMode.SHARE):
                return False
            locked_membership = await conn.fetchval(
                """
                SELECT thread_id FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1 AND sandbox_id = $2
                  AND residency_generation = $3 AND residency_status = 'hot'
                  AND active_attempt_id = $4
                FOR UPDATE
                """,
                thread_id,
                sandbox_id,
                residency_generation,
                attempt_id,
            )
            if locked_membership is None or not await attempt_and_residency_are_live(
                conn, attempt_id, thread_id
            ):
                return False
            shared = await conn.fetchval(
                RECORD_SHARED_THREAD_RESIDENCY_MEMBERS_SQL,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_id,
                residency_generation,
            )
            if shared is None:
                return False
            await self._capture_attempt_ledger(conn, thread_id=thread_id, attempt_id=attempt_id)
            return True

    async def admit_thread_sandbox_turn(
        self,
        *,
        thread_id: str,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
    ) -> bool:
        """Reserve one prompt delivery durably before Agent CLI stdin.

        The update is idempotent for the same job/attempt, but a different job
        cannot replace an unresolved admission.  A control-plane reservation
        followed by a runner crash is intentionally conservative: the reaper
        records LOST instead of sending the same user message a second time.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Keep the same attempt -> membership order as startup capture
            # and finishing an attempt. The FK insert must not invert it.
            if not await lock_running_attempt(conn, attempt_id, lease_generation, LockMode.SHARE):
                return False
            locked_membership = await conn.fetchval(
                """
                SELECT thread_id FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1 AND activation_generation = $2
                  AND residency_generation = $3 AND residency_status = 'hot'
                  AND active_attempt_id = $4
                FOR UPDATE
                """,
                thread_id,
                activation_generation,
                residency_generation,
                attempt_id,
            )
            if locked_membership is None or not await attempt_and_residency_are_live(
                conn, attempt_id, thread_id
            ):
                return False
            # Released residency already refuses a deleted chat; the tombstone
            # is checked here too, in this statement's own snapshot, so no
            # prompt is delivered to a chat its owner has deleted.
            shared = await conn.fetchval(
                ADMIT_THREAD_SANDBOX_TURN_MEMBERS_SQL,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                residency_generation,
                job_id,
            )
            if shared is None:
                return False
            await self._capture_attempt_ledger(conn, thread_id=thread_id, attempt_id=attempt_id)
            await conn.execute(
                ADMIT_THREAD_SANDBOX_TURN_GROUP_SQL,
                thread_id,
            )
            return True

    async def settle_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
        job_id: str,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Close one turn boundary and arm deactivation without deleting state."""
        # The runner canonicalizes observed locators the same way. Repeat it at
        # the repository boundary for non-HTTP callers and rolling clients.
        native_session_id = (
            native_session_id.strip() if native_session_id is not None else None
        ) or None
        async with self._pool.acquire() as conn, conn.transaction():
            # Attempt first, then thread -> job -> membership. Terminal
            # transition and the reaper use the same outer order; taking the
            # membership first while a concurrent send held the job produced a
            # real PostgreSQL deadlock cycle.
            locked_attempt = await conn.fetchval(
                "SELECT id FROM agent_attempts WHERE id = $1 AND lease_generation = $2 FOR SHARE",
                attempt_id,
                lease_generation,
            )
            if locked_attempt is None:
                return None
            locked_thread = await conn.fetchrow(
                "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE",
                thread_id,
            )
            if locked_thread is None:
                return None
            locked_job = await conn.fetchval(
                "SELECT id FROM agent_jobs WHERE id = $1 AND thread_id = $2 FOR UPDATE",
                job_id,
                thread_id,
            )
            if locked_job is None:
                return None
            # A committed settlement can lose only its HTTP acknowledgement.
            # Return that same boundary without incrementing workspace_epoch or
            # creating another snapshot. Exact attempt, lease, job, activation,
            # residency, and settled-boundary fences prevent a retry from
            # acknowledging a superseding activation. The attempt clock may
            # have expired after the settlement committed, so this read-only
            # replay deliberately does not require a currently live lease.
            # No sandbox-mode predicate: the rewritten schema has no ``mode``
            # column, and the retained shared-session topology is unconditional,
            # so every sandbox row is a shared-live one.
            replay = await conn.fetchval(
                """
                SELECT m.thread_id
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                JOIN agent_attempts a ON a.id = $2
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE m.thread_id = $1
                  AND g.generation = $4 AND m.activation_generation = $5
                  AND m.residency_generation = $6
                  AND m.active_attempt_id IS NULL
                  AND m.admitted_job_id = $7 AND m.last_settled_job_id = $7
                  AND m.native_state = 'ready' AND m.context_status = $8
                  AND (
                        $9::text IS NULL
                        OR BTRIM(m.native_session_id) = $9
                  )
                  AND a.lease_generation = $3 AND a.job_id = $7
                  AND j.thread_id = m.thread_id AND j.current_attempt_id = a.id
                  AND j.state = 'running'
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                residency_generation,
                job_id,
                context_status,
                native_session_id,
            )
            if replay is not None:
                row = await conn.fetchrow(
                    f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                    thread_id,
                )
                return sandbox_row_to_dict(row)
            locked_membership = await conn.fetchval(
                """
                SELECT thread_id FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1 AND activation_generation = $2
                  AND residency_generation = $3 AND active_attempt_id = $4
                  AND admitted_job_id = $5
                FOR UPDATE
                """,
                thread_id,
                activation_generation,
                residency_generation,
                attempt_id,
                job_id,
            )
            if locked_membership is None or not await conn.fetchval(
                """
                SELECT a.status = 'running'
                   AND a.job_id = $3
                   AND a.lease_expires_at > clock_timestamp()
                   AND m.residency_status = 'hot'
                   AND m.residency_lease_expires_at > clock_timestamp()
                FROM agent_attempts a, agent_thread_sandbox_memberships m
                WHERE a.id = $1 AND m.thread_id = $2
                """,
                attempt_id,
                thread_id,
                job_id,
            ):
                return None
            shared = await conn.fetchrow(
                SETTLE_THREAD_SANDBOX_MEMBERS_SQL,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                residency_generation,
                job_id,
                context_status,
                native_session_id,
            )
            if shared is None:
                return None
            native_turn_no = await conn.fetchval(
                """
                UPDATE agent_jobs current_job
                SET native_turn_no = COALESCE(
                    current_job.native_turn_no,
                    (
                        SELECT COALESCE(MAX(previous.native_turn_no), 0) + 1
                        FROM agent_jobs previous
                        WHERE previous.thread_id = $1
                          AND previous.turn_no < current_job.turn_no
                    )
                )
                WHERE current_job.id = $2 AND current_job.thread_id = $1
                RETURNING native_turn_no
                """,
                thread_id,
                job_id,
            )
            if native_turn_no is None:
                raise ValueError("shared workspace settlement has no native turn ordinal")
            if base_sha is None:
                base_sha = await conn.fetchval(
                    "SELECT base_sha FROM agent_jobs WHERE id = $1",
                    job_id,
                )
            # A turn boundary publishes nothing: the platform never pushes.
            # Whatever reaches GitHub, the agent pushed through the GitHub relay.
            group = await conn.fetchrow(
                SETTLE_THREAD_SANDBOX_GROUP_SQL,
                shared["sandbox_id"],
                idle_timeout_seconds,
                base_sha,
            )
            if group is None or not group["published_base_sha"]:
                raise ValueError("shared workspace settlement has no resolved Git base")
            row = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        return None if row is None else sandbox_row_to_dict(row)

    async def abandon_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        lost: bool,
        detail: str | None,
        idle_timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Release a failed shared-member activation without replaying it."""
        async with self._pool.acquire() as conn, conn.transaction():
            if lost:
                # Marking the attempt's context lost writes the attempt, so it
                # is taken before its membership and group, as the reaper,
                # reservation and residency take it.
                await conn.execute(
                    "SELECT id FROM agent_attempts WHERE id = $1 FOR NO KEY UPDATE", attempt_id
                )
                # Then the conversation: losing the context fails the turns
                # queued behind this one, after the membership below, and
                # deleting the conversation holds those turns while it waits
                # for that membership. Deletion starts here, so the two
                # serialize instead of each holding what the other needs.
                await conn.execute(
                    "SELECT id FROM agent_threads WHERE id = $1 FOR UPDATE", thread_id
                )
            elif not await lock_running_attempt(
                conn, attempt_id, lease_generation, LockMode.SHARE
            ) or not await attempt_lease_is_live(conn, attempt_id):
                # Releasing an activation as intact is its runner's word that
                # the context is as the last turn left it, and only a live
                # lease can give that word. Once the lease lapses the reaper
                # decides, and it fails an expired activation closed. The
                # attempt is taken first, as the reaper takes it, so the two
                # never both decide.
                return None
            shared = await conn.fetchrow(
                ABANDON_THREAD_SANDBOX_MEMBERS_SQL,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                lost,
                detail,
            )
            if shared is None:
                return None
            group_state = await conn.fetchval(
                ABANDON_THREAD_SANDBOX_GROUP_SQL,
                shared["sandbox_id"],
                idle_timeout_seconds,
                lost,
                detail,
                thread_id,
            )
            if group_state == "lost" and not lost:
                # Restore attempts ran out. The queued turns behind this one
                # can never run; answer them now instead of leaving them queued.
                await fail_unrunnable_follow_ups(conn, thread_id=thread_id)
            if lost:
                await conn.execute(
                    "UPDATE agent_attempts SET context_status = 'lost' "
                    "WHERE id = $1 AND lease_generation = $2",
                    attempt_id,
                    lease_generation,
                )
                await fail_unrunnable_follow_ups(conn, thread_id=thread_id)
            current = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        return None if current is None else sandbox_row_to_dict(current)

    async def thread_sandbox(self, *, thread_id: str) -> dict[str, Any] | None:
        """Return the durable local sandbox mapping, including STOPPED/LOST."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        if row is None:
            return None
        result = dict(row)
        result["group_members"] = load_json(result.get("group_members")) or []
        return result

    async def _reserve_shared_thread_sandbox(  # noqa: PLR0912 complexity baseline, #771
        self,
        conn: Any,
        *,
        thread_id: str,
        attempt_id: int,
        host: str,
        sandbox_id: str,
        container_name: str,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None,
        execution_profile: str,
        resident_slot_capacity: int,
        memory_limit: str,
        cpu_limit: str,
        disk_limit_mib: int,
        stop_timeout_seconds: int,
        fork_helper_version: str | None,
        user_id: str,
        job_id: str,
    ) -> dict[str, Any] | None:
        """Reserve one independently fenced member in a shared-live group."""
        if type(disk_limit_mib) is not int or not 0 <= disk_limit_mib <= 16777216:
            raise ValueError("sandbox disk limit must be an integer between 0 and 16777216 MiB")
        if not 2 <= resident_slot_capacity <= 128:
            raise ValueError("shared sandbox resident slot capacity must be between 2 and 128")
        # Lock membership first everywhere; Docker reconciliation uses the
        # same order before it fences the shared group row.
        await conn.fetchval(
            """
            SELECT thread_id
            FROM agent_thread_sandbox_memberships
            WHERE thread_id = $1
            FOR UPDATE
            """,
            thread_id,
        )
        membership = await conn.fetchrow(
            """
            SELECT m.*, g.origin_thread_id
            FROM agent_thread_sandbox_memberships m
            JOIN agent_sandboxes g ON g.id = m.sandbox_id
            WHERE m.thread_id = $1
            FOR UPDATE OF g
            """,
            thread_id,
        )
        if membership is None:
            capability = await conn.fetchval(
                """
                SELECT 1 FROM agent_runner_hosts
                WHERE host = $1 AND status = 'online'
                  AND capabilities @> '["shared-sandbox-v1"]'::jsonb
                """,
                host,
            )
            if capability is None:
                raise ValueError("host has not advertised shared-sandbox-v1")
            await conn.execute(
                INSERT_SANDBOX_GROUP_SQL,
                sandbox_id,
                thread_id,
                user_id,
                host,
                container_name,
                runtime_id,
                runtime_version,
                sandbox_image_digest,
                execution_profile,
                resident_slot_capacity,
                memory_limit,
                cpu_limit,
                stop_timeout_seconds,
                # An image without the label says nothing, not "".
                fork_helper_version or None,
                disk_limit_mib,
            )
            await conn.execute(
                INSERT_SANDBOX_MEMBERSHIP_SQL,
                thread_id,
                user_id,
                sandbox_id,
            )
            membership = await conn.fetchrow(
                """
                SELECT m.*, g.origin_thread_id
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                FOR UPDATE OF g
                """,
                thread_id,
            )
        if membership is None:
            return None
        group = await conn.fetchrow(
            "SELECT * FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
            membership["sandbox_id"],
        )
        if group is None:
            return None
        # The image's fork helper version is written once, with the group, and is not
        # compared here: only the runner creating the container reads it off
        # the image, so a follow-up's reservation does not carry it.
        immutable = {
            "id": sandbox_id,
            "host": host,
            "container_name": container_name,
            "runtime_id": runtime_id,
            "runtime_version": runtime_version,
            "sandbox_image_digest": sandbox_image_digest,
            "execution_profile": execution_profile,
            "memory_limit": memory_limit,
            "cpu_limit": cpu_limit,
            "disk_limit_mib": disk_limit_mib,
            "stop_timeout_seconds": stop_timeout_seconds,
        }
        if any(group[key] != value for key, value in immutable.items()):
            raise ValueError("shared sandbox immutable identity or runtime changed")
        if (
            # A cold group has no container anywhere. Only the claim that
            # rehomes it (cold -> restoring) may reserve it.
            group["state"] in {"lost", "stopped_dirty", "stopping", "cold"}
            or group["stop_requested_at"] is not None
            or group["deletion_state"] in DELETED_GROUP_STATES
            or membership["native_state"] == "lost"
        ):
            return None
        if membership["fork_operation_id"] is not None:
            # The dequeue query carries the same fence, but admission is a
            # separate transaction and may race a Fork request that wins after
            # claim. Never append a turn to the source session while its
            # settled prefix is being materialized.
            return None
        active_attempt = membership["active_attempt_id"]
        if active_attempt is not None and active_attempt != attempt_id:
            return None
        admitted_job = membership["admitted_job_id"]
        if admitted_job is not None and admitted_job != job_id:
            return None
        already_admitted = active_attempt == attempt_id and group["state"] in {
            "starting",
            "running",
        }
        # The membership/group locks above may have waited until after the
        # attempt lease expired.  Evaluate wall time only after those waits and
        # immediately before granting this attempt control of the member.
        if not await attempt_lease_is_live(conn, attempt_id):
            return None
        activation_generation = int(membership["activation_generation"])
        if not already_admitted:
            activation_generation += 1
        context_status = "new" if membership["last_settled_job_id"] is None else "restored"
        await conn.execute(
            RESERVE_ACTIVATE_MEMBER_SQL,
            thread_id,
            attempt_id,
            activation_generation,
            context_status,
        )
        # Activation starts this member's process, not the group's container:
        # an idle retained group is already running until Docker says otherwise.
        # Newly created and stopped groups still await the startup confirmation.
        # A restoring group stays restoring until its recreated container is
        # confirmed, and keeps the checkpoint it is recreated from.
        await conn.execute(
            RESERVE_ACTIVATE_GROUP_SQL,
            group["id"],
        )
        row = await conn.fetchrow(
            f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
            thread_id,
        )
        return None if row is None else sandbox_row_to_dict(row)

    @staticmethod
    async def _capture_attempt_ledger(
        conn: asyncpg.Connection, *, thread_id: str, attempt_id: int
    ) -> None:
        """Copy the exact residency grant inside a fenced admission transaction.

        The per-attempt grant is a separate, disposable credential. A NULL
        residency binding means an older writer never recorded its Gateway,
        so it deliberately leaves this attempt untracked. Re-admission keeps
        the first reference; rotation preserves an additional distinct grant.
        """
        await conn.execute(
            """
            INSERT INTO agent_attempt_ledger (
                attempt_id, grant_id, residency_generation, binding_id, window_since
            )
            SELECT a.id, m.residency_grant_id, m.residency_generation,
                   m.residency_binding_id, a.started_at
            FROM agent_thread_sandbox_memberships m
            JOIN agent_attempts a ON a.id = m.active_attempt_id
            JOIN agent_jobs j ON j.id = a.job_id AND j.thread_id = m.thread_id
            WHERE m.thread_id = $1 AND a.id = $2
              AND m.residency_generation > 0
              AND m.residency_grant_id IS NOT NULL AND m.residency_grant_id <> ''
              AND m.residency_binding_id IS NOT NULL AND m.residency_binding_id <> ''
            ON CONFLICT (attempt_id, grant_id) DO NOTHING
            """,
            thread_id,
            attempt_id,
        )
