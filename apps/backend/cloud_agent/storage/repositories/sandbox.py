"""Postgres implementation of the retained-sandbox port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.jobs import fail_unrunnable_follow_ups
from cloud_agent.storage._sql.rows import load_json, sandbox_row_to_dict
from cloud_agent.storage._sql.sandbox import THREAD_SANDBOX_SOURCE
from cloud_agent_shared.sandbox_resources import parse_sandbox_resource_sample

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg

logger = get_logger(__name__)


class PostgresSandboxRepository:
    """One chat, one restartable sandbox: reserve, admit, settle, abandon."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def request_admin_stop(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str,
        last_activity_at: datetime,
        actor_id: str,
        admin_ip: str,
    ) -> str:
        """Queue a reviewed, quiescent container stop under its admission fence.

        The host still checks its local terminals and operations before acting.
        The database marker survives observations and excludes new admission
        until Docker confirms the stop. No container or workspace is deleted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            # The same membership -> group order as reservation, Fork and
            # reconciliation. Claims hold a shared group lock until committed.
            await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                "SELECT id, user_id, host, generation, container_id, last_activity_at, state, "
                "stop_requested_at, active_operation_count, snapshot_state, "
                "terminal_count, terminal_count_observed_at, NOW() AS observed_at "
                "FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
                sandbox_id,
            )
            if group is None:
                return "not_found"
            if (
                group["generation"] != generation
                or group["container_id"] != container_id
                or group["last_activity_at"] != last_activity_at
            ):
                return "stale"
            if group["state"] == "stopping" and group["stop_requested_at"] is not None:
                return "accepted"
            if group["state"] != "running":
                return "not_running"
            busy = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_thread_sandbox_memberships m
                    WHERE m.sandbox_id = $1 AND (
                        m.active_attempt_id IS NOT NULL OR m.fork_operation_id IS NOT NULL
                        OR EXISTS (
                            SELECT 1 FROM agent_jobs j
                            WHERE j.thread_id = m.thread_id AND j.state = 'running'
                        )
                    )
                ) OR EXISTS (
                    SELECT 1 FROM agent_sandbox_snapshots s
                    WHERE s.sandbox_id = $1
                      AND s.state IN ('pending', 'capturing', 'captured', 'publishing')
                )
                """,
                sandbox_id,
            )
            if (
                busy
                or group["active_operation_count"]
                or group["snapshot_state"] in {"pending", "snapshotting", "importing"}
            ):
                return "busy"
            if group["terminal_count"]:
                return "terminals_open"
            terminal_observed = group["terminal_count_observed_at"]
            if (
                group["terminal_count"] is None
                or terminal_observed is None
                or (group["observed_at"] - terminal_observed).total_seconds() > 90
            ):
                return "activity_unknown"
            await conn.execute(
                "UPDATE agent_sandboxes SET state = 'stopping', "
                "stop_requested_at = NOW(), idle_deadline = NULL, updated_at = NOW() WHERE id = $1",
                sandbox_id,
            )
            # Stop and its audit receipt commit together. An unavailable audit
            # table must never leave an unrecorded cross-owner mutation.
            await conn.execute(
                "INSERT INTO agent_admin_audit_log "
                "(action, actor_id, admin_ip, target_user_id, details, success) "
                "VALUES ('agent.sandbox.stop', $1, $2, $3, $4::jsonb, TRUE)",
                actor_id,
                admin_ip,
                group["user_id"],
                json.dumps(
                    {
                        "sandbox_id": sandbox_id,
                        "host": group["host"],
                        "generation": generation,
                        "container_id": container_id,
                    }
                ),
            )
        return "accepted"

    async def thread_workspace_ready(self, *, thread_id: str) -> bool:
        """Return whether a retained thread permits owner workspace mutation.

        The broker's in-memory suspension is the immediate barrier. This
        durable copy closes broker-restart and old-turn-URL gaps: an active
        attempt must have published ``workspace_ready``.
        """
        async with self._pool.acquire() as conn:
            ready = await conn.fetchval(
                """
                SELECT
                    m.context_status <> 'lost'
                    AND m.native_state <> 'lost'
                    AND g.state NOT IN (
                        'provisioning', 'stopping', 'stopped_dirty', 'lost'
                    )
                    AND g.stop_requested_at IS NULL
                    AND m.fork_operation_id IS NULL
                    AND (
                        m.active_attempt_id IS NULL
                        OR EXISTS (
                            SELECT 1 FROM agent_attempts a
                            WHERE a.id = m.active_attempt_id
                              AND a.status = 'running'
                              AND a.lease_expires_at > NOW()
                              AND a.terminal_ready = TRUE
                        )
                    )
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                """,
                thread_id,
            )
        return bool(ready)

    async def fence_terminal_workspace(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
    ) -> bool:
        """Revoke readiness only for the still-live fenced attempt."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agent_attempts
                SET terminal_ready = FALSE
                WHERE id = $1
                  AND lease_generation = $2
                  AND status = 'running'
                  AND lease_expires_at > NOW()
                """,
                attempt_id,
                lease_generation,
            )
        return result == "UPDATE 1"

    async def list_terminal_resumes_pending(self, *, limit: int = 100) -> list[str]:
        """List settled workspaces whose broker resume still needs confirmation."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id
                FROM agent_jobs
                WHERE terminal_resume_pending = TRUE
                  AND state = ANY($1::text[])
                ORDER BY updated_at
                LIMIT $2
                """,
                list(TERMINAL_STATES),
                limit,
            )
        return [row["id"] for row in rows]

    async def mark_terminal_resume_complete(self, *, job_id: str) -> bool:
        """Clear durable recovery state after the broker confirms a resume."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agent_jobs
                SET terminal_resume_pending = FALSE
                WHERE id = $1
                  AND terminal_resume_pending = TRUE
                  AND state = ANY($2::text[])
                """,
                job_id,
                list(TERMINAL_STATES),
            )
        return result == "UPDATE 1"

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
            earns = await conn.fetchrow(
                """
                SELECT j.id, j.thread_id, j.user_id
                FROM agent_attempts a
                JOIN agent_jobs j ON j.id = a.job_id
                WHERE a.id = $1 AND a.lease_generation = $2
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                  AND a.host_id = $3 AND j.thread_id = $4
                  AND j.current_attempt_id = a.id
                FOR SHARE OF a, j
                """,
                attempt_id,
                lease_generation,
                host,
                thread_id,
            )
            if earns is None:
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
            shared = await conn.fetchrow(
                """
                WITH updated_group AS (
                    UPDATE agent_sandboxes g
                    SET state = 'running', container_id = $6,
                        last_activity_at = NOW(), updated_at = NOW()
                    FROM agent_thread_sandbox_memberships m
                    JOIN agent_attempts a ON a.id = m.active_attempt_id
                    JOIN agent_jobs j ON j.id = a.job_id
                    WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                      AND g.generation = $4 AND g.stop_requested_at IS NULL
                      AND m.activation_generation = $5
                      AND m.active_attempt_id = $2
                      AND (g.container_id IS NULL OR g.container_id = $6)
                      AND a.id = $2 AND a.lease_generation = $3
                      AND a.status = 'running' AND a.lease_expires_at > NOW()
                      AND j.current_attempt_id = a.id AND j.thread_id = m.thread_id
                    RETURNING g.id
                )
                UPDATE agent_thread_sandbox_memberships m
                SET context_status = $7,
                    resident_state = CASE
                        WHEN m.resident_state = 'live' THEN 'live' ELSE 'starting' END,
                    updated_at = NOW()
                FROM updated_group u
                WHERE m.thread_id = $1 AND m.sandbox_id = u.id
                RETURNING m.thread_id
                """,
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
        process_generation: int,
    ) -> bool:
        """Record the supervisor's diagnostic CLI process generation."""
        async with self._pool.acquire() as conn:
            shared = await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET process_generation = $6, resident_state = 'live', updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4 AND g.stop_requested_at IS NULL
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                RETURNING m.thread_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
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
            if (
                await conn.fetchval(
                    "SELECT id FROM agent_attempts WHERE id = $1 AND lease_generation = $2 "
                    "AND status = 'running' AND lease_expires_at > NOW() FOR SHARE",
                    attempt_id,
                    lease_generation,
                )
                is None
            ):
                return False
            shared = await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET resident_state = CASE
                        WHEN resident_state = 'live' THEN 'live' ELSE 'starting' END,
                    updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND m.sandbox_id = $4
                  AND g.id = m.sandbox_id
                  AND m.residency_generation = $5 AND m.residency_status = 'hot'
                  AND m.active_attempt_id = $2
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                RETURNING m.thread_id
                """,
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
            if (
                await conn.fetchval(
                    "SELECT id FROM agent_attempts WHERE id = $1 AND lease_generation = $2 "
                    "AND status = 'running' AND lease_expires_at > NOW() FOR SHARE",
                    attempt_id,
                    lease_generation,
                )
                is None
            ):
                return False
            shared = await conn.fetchval(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET admitted_job_id = $6,
                    admitted_at = COALESCE(m.admitted_at, NOW()),
                    updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a, agent_jobs j
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4 AND g.stop_requested_at IS NULL
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                  AND j.id = $6 AND j.current_attempt_id = a.id
                  AND j.thread_id = m.thread_id AND j.state = 'running'
                  AND (m.admitted_job_id IS NULL OR m.admitted_job_id = $6)
                RETURNING m.thread_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
                job_id,
            )
            if shared is None:
                return False
            await self._capture_attempt_ledger(conn, thread_id=thread_id, attempt_id=attempt_id)
            await conn.execute(
                """
                UPDATE agent_sandboxes g
                SET idle_deadline = NULL, last_activity_at = NOW(), updated_at = NOW()
                FROM agent_thread_sandbox_memberships m
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                """,
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
        job_id: str,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Close one turn boundary and arm deactivation without deleting state."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Match create_follow_up and fork finalization: thread -> job ->
            # membership. Taking the membership first while a concurrent send
            # held the job produced a real PostgreSQL deadlock cycle.
            locked_thread = await conn.fetchval(
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
            # creating another snapshot. The original attempt must still own
            # a live fence; a retry cannot resurrect a superseded activation.
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
                  AND m.active_attempt_id IS NULL
                  AND m.admitted_job_id = $6 AND m.last_settled_job_id = $6
                  AND m.native_state = 'ready' AND m.context_status = $7
                  AND ($8::text IS NULL OR m.native_session_id = $8)
                  AND a.lease_generation = $3 AND a.job_id = $6
                  AND a.status = 'running' AND a.lease_expires_at > NOW()
                  AND j.thread_id = m.thread_id AND j.current_attempt_id = a.id
                  AND j.state = 'running'
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
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
            shared = await conn.fetchrow(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET active_attempt_id = NULL, last_settled_job_id = $6,
                    context_status = $7,
                    native_session_id = COALESCE($8, m.native_session_id),
                    native_state = 'ready', resident_state = 'live',
                    last_error = NULL, updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND m.admitted_job_id = $6
                  AND a.id = $2 AND a.lease_generation = $3
                  AND a.job_id = $6 AND a.status = 'running'
                  AND a.lease_expires_at > NOW()
                RETURNING m.sandbox_id
                """,
                thread_id,
                attempt_id,
                lease_generation,
                sandbox_generation,
                activation_generation,
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
            group = await conn.fetchrow(
                """
                    UPDATE agent_sandboxes g
                    SET state = 'running', last_activity_at = NOW(),
                        idle_timeout_seconds = $2,
                        idle_deadline = CASE
                            WHEN g.active_operation_count = 0
                             AND NOT EXISTS (
                                SELECT 1
                                FROM agent_thread_sandbox_memberships peer
                                WHERE peer.sandbox_id = g.id
                                  AND peer.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => $2)
                            ELSE NULL
                        END,
                        workspace_epoch = workspace_epoch + 1,
                        published_base_sha = COALESCE(g.published_base_sha, $3),
                        snapshot_state = CASE
                            WHEN g.snapshot_state IN ('snapshotting', 'importing', 'dirty')
                                THEN g.snapshot_state
                            ELSE 'pending'
                        END,
                        updated_at = NOW()
                    WHERE g.id = $1
                    RETURNING g.workspace_epoch, g.published_base_sha
                    """,
                shared["sandbox_id"],
                idle_timeout_seconds,
                base_sha,
            )
            if group is None or not group["published_base_sha"]:
                raise ValueError("shared workspace settlement has no resolved Git base")
            pending = await conn.fetchrow(
                """
                    SELECT id FROM agent_sandbox_snapshots
                    WHERE sandbox_id = $1 AND state = 'pending'
                    FOR UPDATE
                    """,
                shared["sandbox_id"],
            )
            # The anchor must be one of the included jobs — a deferred foreign
            # key checks that at commit, so the snapshot row and its
            # membership row land in this same transaction, in either order.
            if pending is None:
                snapshot_id = await conn.fetchval(
                    """
                    INSERT INTO agent_sandbox_snapshots (
                        sandbox_id, workspace_epoch, anchor_job_id, base_sha
                    )
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    shared["sandbox_id"],
                    group["workspace_epoch"],
                    job_id,
                    group["published_base_sha"],
                )
            else:
                snapshot_id = pending["id"]
                await conn.execute(
                    """
                    UPDATE agent_sandbox_snapshots
                    SET workspace_epoch = $2, anchor_job_id = $3, updated_at = NOW()
                    WHERE id = $1 AND state = 'pending'
                    """,
                    snapshot_id,
                    group["workspace_epoch"],
                    job_id,
                )
            await conn.execute(
                """
                INSERT INTO agent_snapshot_jobs (snapshot_id, job_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                snapshot_id,
                job_id,
            )
            row = await conn.fetchrow(
                f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
                thread_id,
            )
        return None if row is None else sandbox_row_to_dict(row)

    async def reconcile_thread_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None = None,
        idle_remaining_seconds: float | None = None,
        members: list[dict[str, Any]] | None = None,
        resource_usage: dict[str, Any] | None = None,
        terminal_count: int | None = None,
    ) -> str | None:
        """Reconcile one authenticated shared-group Docker observation."""
        return await self._reconcile_shared_sandbox_observation(
            thread_id=thread_id,
            host=host,
            sandbox_id=sandbox_id,
            container_id=container_id,
            sandbox_generation=sandbox_generation,
            running=running,
            activity_active=activity_active,
            idle_remaining_seconds=idle_remaining_seconds,
            members=members or [],
            resource_usage=resource_usage,
            terminal_count=terminal_count,
        )

    async def due_thread_sandbox_stop(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
    ) -> dict[str, Any] | None:
        """Return the exact shared-group fence for a due retained stop."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT origin_thread_id AS thread_id, generation,
                       0::bigint AS activation_generation,
                       CASE
                           WHEN stop_requested_at IS NOT NULL THEN 'idle'
                           WHEN state IN ('stopped_dirty', 'lost') THEN 'lost'
                           WHEN state = 'stopping' THEN 'orphaned_attempt'
                           ELSE 'idle'
                       END AS reason,
                       TRUE AS shared_live,
                       CASE WHEN stop_requested_at IS NOT NULL THEN (
                           SELECT COALESCE(jsonb_object_agg(m.thread_id, m.activation_generation),
                                           '{}'::jsonb)
                           FROM agent_thread_sandbox_memberships m
                           WHERE m.sandbox_id = g.id AND m.resident_slot_id IS NOT NULL
                       ) END AS member_activation_generations
                FROM agent_sandboxes g
                WHERE g.id = $1 AND g.origin_thread_id = $2 AND g.host = $3
                  AND g.container_id = $4 AND g.generation = $5
                  AND g.active_operation_count = 0
                  AND g.snapshot_state <> 'snapshotting'
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships m
                      WHERE m.sandbox_id = g.id AND m.active_attempt_id IS NOT NULL
                  )
                  AND (
                        (state = 'running' AND idle_deadline IS NOT NULL
                            AND idle_deadline <= NOW())
                        OR state IN ('stopping', 'stopped_dirty', 'lost')
                  )
                """,
                sandbox_id,
                thread_id,
                host,
                container_id,
                sandbox_generation,
            )
        if row is None:
            return None
        result = dict(row)
        result["member_activation_generations"] = load_json(result["member_activation_generations"])
        return result

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
            shared = await conn.fetchrow(
                """
                UPDATE agent_thread_sandbox_memberships m
                SET active_attempt_id = NULL,
                    context_status = CASE WHEN $6 THEN 'lost' ELSE m.context_status END,
                    native_state = CASE WHEN $6 THEN 'lost' ELSE m.native_state END,
                    resident_state = CASE WHEN $6 THEN 'lost' ELSE 'restarting' END,
                    last_error = $7, updated_at = NOW()
                FROM agent_sandboxes g, agent_attempts a
                WHERE m.thread_id = $1 AND g.id = m.sandbox_id
                  AND g.generation = $4
                  AND m.activation_generation = $5
                  AND m.active_attempt_id = $2
                  AND (
                        ($6 AND (m.admitted_job_id IS NULL OR m.admitted_job_id = a.job_id))
                        OR (NOT $6 AND m.admitted_job_id IS NULL)
                  )
                  AND a.id = $2 AND a.lease_generation = $3
                RETURNING m.sandbox_id
                """,
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
            await conn.execute(
                """
                UPDATE agent_sandboxes g
                SET state = CASE
                        WHEN $3 AND g.container_id IS NULL THEN 'lost'
                        WHEN g.container_id IS NULL THEN 'provisioning'
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
                              AND peer.thread_id <> $5
                              AND peer.context_status <> 'lost'
                              AND peer.native_state <> 'lost'
                        ) THEN 'running'
                        ELSE 'stopping'
                    END,
                    last_activity_at = NOW(),
                    idle_timeout_seconds = $2,
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
                              AND peer.thread_id <> $5
                              AND peer.context_status <> 'lost'
                              AND peer.native_state <> 'lost'
                         )
                        THEN NOW() + make_interval(secs => $2)
                        ELSE NULL
                    END,
                    last_error = CASE WHEN $3 THEN $4 ELSE g.last_error END,
                    updated_at = NOW()
                WHERE g.id = $1
                """,
                shared["sandbox_id"],
                idle_timeout_seconds,
                lost,
                detail,
                thread_id,
            )
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

    async def _reserve_shared_thread_sandbox(
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
        stop_timeout_seconds: int,
        fork_helper_version: str | None,
        user_id: str,
        job_id: str,
    ) -> dict[str, Any] | None:
        """Reserve one independently fenced member in a shared-live group."""
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
                """
                INSERT INTO agent_sandboxes (
                    id, origin_thread_id, user_id, host, container_name,
                    runtime_id, runtime_version, sandbox_image_digest,
                    execution_profile, resident_slot_capacity, memory_limit,
                    cpu_limit, stop_timeout_seconds, fork_helper_version
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
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
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_sandbox_memberships (
                    thread_id, user_id, sandbox_id, resident_slot_id
                ) VALUES ($1, $2, $3, 0)
                """,
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
            "stop_timeout_seconds": stop_timeout_seconds,
        }
        if any(group[key] != value for key, value in immutable.items()):
            raise ValueError("shared sandbox immutable identity or runtime changed")
        if (
            group["state"] in {"lost", "stopped_dirty", "stopping"}
            or group["stop_requested_at"] is not None
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
        activation_generation = int(membership["activation_generation"])
        if not already_admitted:
            activation_generation += 1
        context_status = "new" if membership["last_settled_job_id"] is None else "restored"
        await conn.execute(
            """
            UPDATE agent_thread_sandbox_memberships
            SET active_attempt_id = $2, activation_generation = $3,
                context_status = $4, resident_state = CASE
                    WHEN resident_state = 'live' THEN resident_state ELSE 'starting' END,
                last_error = NULL, updated_at = NOW()
            WHERE thread_id = $1
            """,
            thread_id,
            attempt_id,
            activation_generation,
            context_status,
        )
        # Activation starts this member's process, not the group's container:
        # an idle retained group is already running until Docker says otherwise.
        # Newly created and stopped groups still await the startup confirmation.
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET state = CASE WHEN state = 'running' THEN 'running' ELSE 'starting' END,
                idle_deadline = NULL,
                last_activity_at = NOW(), updated_at = NOW()
            WHERE id = $1
            """,
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

    async def _reconcile_shared_sandbox_observation(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None,
        idle_remaining_seconds: float | None,
        members: list[dict[str, Any]],
        resource_usage: dict[str, Any] | None,
        terminal_count: int | None,
    ) -> str | None:
        """Join one Docker group observation with independently fenced members."""
        async with self._pool.acquire() as conn, conn.transaction():
            # The stopped path updates every durable member, including one a
            # partial host census omitted. Lock the complete bounded group in
            # deterministic order before its sandbox row. Settlement,
            # admission, fork, and terminal writers use the same membership
            # -> sandbox order; taking only reported members would recreate
            # the inverse order when the bulk stopped-state update runs.
            await conn.fetch(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE sandbox_id = $1
                ORDER BY thread_id
                FOR UPDATE
                """,
                sandbox_id,
            )
            group = await conn.fetchrow(
                """
                SELECT * FROM agent_sandboxes
                WHERE id = $1 AND origin_thread_id = $2 AND host = $3
                  AND generation = $4
                FOR UPDATE
                """,
                sandbox_id,
                thread_id,
                host,
                sandbox_generation,
            )
            if group is None:
                return None
            if group["container_id"] not in {None, container_id}:
                return None
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET container_id = COALESCE(container_id, $2), updated_at = NOW()
                WHERE id = $1
                """,
                sandbox_id,
                container_id,
            )
            # Diagnostics share the exact ownership fence above, but their
            # savepoint cannot roll back the lifecycle observation if monitoring
            # becomes unavailable during a rollout or a collector failure.
            try:
                async with conn.transaction():
                    await self._record_resource_observation(
                        conn,
                        sandbox_id=sandbox_id,
                        running=running,
                        resource_usage=resource_usage,
                        terminal_count=terminal_count,
                    )
            except Exception:
                logger.warning("sandbox_resource_snapshot_unavailable", exc_info=True)
            for member in members[:128]:
                member_thread = str(member.get("thread_id") or "")
                if not member_thread:
                    continue
                observed_activation = int(member.get("activation_generation") or 0)
                observed_process = int(member.get("process_generation") or 0)
                observed_residency = int(member.get("residency_generation") or 0)
                resident_state = str(member.get("resident_state") or "stopped")
                if resident_state not in {"stopped", "starting", "live", "restarting", "lost"}:
                    continue
                # A heartbeat may confirm an equal/newer local process state,
                # but cannot move a durable activation fence backwards or
                # overwrite a process belonging to a newer residency.
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET process_generation = CASE
                            WHEN native_state = 'lost' OR context_status = 'lost'
                                THEN process_generation
                            ELSE GREATEST(process_generation, $3)
                        END,
                        resident_state = CASE
                            WHEN native_state = 'lost' OR context_status = 'lost'
                                THEN 'lost'
                            WHEN residency_generation <= $4
                             AND process_generation <= $3
                             AND activation_generation <= $2 THEN $5
                            ELSE resident_state
                        END,
                        updated_at = NOW()
                    WHERE thread_id = $1 AND sandbox_id = $6
                    """,
                    member_thread,
                    observed_activation,
                    observed_process,
                    observed_residency,
                    resident_state,
                    sandbox_id,
                )

            active_member = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_thread_sandbox_memberships
                    WHERE sandbox_id = $1 AND active_attempt_id IS NOT NULL
                )
                """,
                sandbox_id,
            )
            # A host heartbeat is an observation sampled before this database
            # transaction.  It may say "locally idle" just before a native
            # fork/snapshot coordinator increments its durable operation
            # fence.  Never let that stale sample re-arm an idle deadline in
            # the middle of the externally visible mutation.
            durable_activity = (
                bool(active_member)
                or int(group["active_operation_count"]) > 0
                or (group["snapshot_state"] == "snapshotting")
            )
            # Docker creation can outlive the runner that requested it. An
            # explicit pre-admission abandon leaves the group PROVISIONING
            # with no active member; if Docker reports that exact labelled
            # object afterwards, it is an orphan to stop, not a fresh active
            # group to adopt. This recovery fence prevents a broker-local
            # lease from keeping an unowned container resident forever.
            if group["state"] == "provisioning" and not durable_activity:
                recovered = await conn.fetchval(
                    """
                    UPDATE agent_sandboxes
                    SET state = CASE WHEN $2 THEN 'stopping' ELSE 'stopped' END,
                        idle_deadline = NULL, updated_at = NOW()
                    WHERE id = $1
                    RETURNING state
                    """,
                    sandbox_id,
                    running,
                )
                return None if recovered is None else str(recovered)
            if running:
                if group["stop_requested_at"] is not None:
                    # A heartbeat is an observation of a still-running
                    # container, not cancellation of an administrator's
                    # request. Keep admission closed and retry the safe
                    # host-side idle check on every subsequent heartbeat.
                    return "stopping"
                deadline_sql = "idle_deadline"
                deadline_arg: float | None = None
                if activity_active is True or durable_activity:
                    deadline_sql = "NULL"
                elif activity_active is False and idle_remaining_seconds is not None:
                    deadline_sql = "NOW() + make_interval(secs => $2)"
                    deadline_arg = idle_remaining_seconds
                elif activity_active is None:
                    # A broker reconstructed this running container without
                    # its old monotonic clock. Give a group whose durable
                    # deadline was cleared one bounded recovery window, but do
                    # not refresh that window on every host heartbeat. Once a
                    # real terminal/operation closes, the broker reports its
                    # concrete decreasing remainder through the branch above.
                    deadline_sql = (
                        "COALESCE(idle_deadline, "
                        "NOW() + make_interval(secs => idle_timeout_seconds))"
                    )
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_sandboxes
                    SET state = 'running', idle_deadline = {deadline_sql},
                        updated_at = NOW()
                    WHERE id = $1
                    RETURNING state
                    """,
                    *((sandbox_id, deadline_arg) if deadline_arg is not None else (sandbox_id,)),
                )
                return None if row is None else str(row["state"])

            if active_member:
                return None
            row = await conn.fetchrow(
                """
                UPDATE agent_sandboxes
                SET state = 'stopped', idle_deadline = NULL,
                    stop_requested_at = NULL, updated_at = NOW()
                WHERE id = $1 AND (
                    state IN ('stopping', 'stopped_dirty', 'lost')
                    OR (state = 'running' AND idle_deadline IS NOT NULL
                        AND idle_deadline <= NOW())
                )
                RETURNING state
                """,
                sandbox_id,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET resident_state = CASE
                        WHEN native_state = 'lost' THEN 'lost' ELSE 'stopped' END,
                    updated_at = NOW()
                WHERE sandbox_id = $1
                """,
                sandbox_id,
            )
            return None if row is None else str(row["state"])

    @staticmethod
    async def _record_resource_observation(
        conn: asyncpg.Connection,
        *,
        sandbox_id: str,
        running: bool,
        resource_usage: dict[str, Any] | None,
        terminal_count: int | None,
    ) -> None:
        """Store only new, valid counters for an already fenced container."""
        if not running:
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET resource_snapshot = NULL, resource_sampled_at = NULL,
                    resource_received_at = NULL, terminal_count = NULL,
                    terminal_count_observed_at = NULL
                WHERE id = $1
                """,
                sandbox_id,
            )
            return
        count = terminal_count
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 4096
        ):
            count = None
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET terminal_count = $2,
                terminal_count_observed_at = CASE WHEN $2::integer IS NULL THEN NULL ELSE NOW() END
            WHERE id = $1
            """,
            sandbox_id,
            count,
        )
        sample = parse_sandbox_resource_sample(resource_usage)
        if sample is None:
            return
        await conn.execute(
            """
            UPDATE agent_sandboxes
            SET resource_snapshot = $2::jsonb,
                resource_sampled_at = NOW() - make_interval(secs => $3),
                resource_received_at = NOW()
            WHERE id = $1
              AND resource_snapshot->>'sample_id' IS DISTINCT FROM $4
              AND (resource_sampled_at IS NULL
                   OR resource_sampled_at < NOW() - make_interval(secs => $3))
            """,
            sandbox_id,
            json.dumps(sample.model_dump(mode="json")),
            sample.sample_age_ms / 1000.0,
            sample.sample_id,
        )
