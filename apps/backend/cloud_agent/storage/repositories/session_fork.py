"""Postgres implementation of the native session-fork port.

Lifted verbatim from the single storage class this package replaced: the
statements, their bound parameters and their transaction ownership are what
they were, and only the class holding them changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.domain.execution import CANCELLED, SUCCEEDED, TERMINAL_STATES
from cloud_agent.domain.ids import new_job_id, new_session_fork_id, new_thread_id
from cloud_agent.storage._sql.jobs import JOB_SELECT
from cloud_agent.storage._sql.rows import job_row_to_dict, load_json, sandbox_row_to_dict
from cloud_agent.storage._sql.sandbox import NATIVE_FORK_CAPABLE, THREAD_SANDBOX_SOURCE
from cloud_agent.storage.repositories.conversation_queries import PostgresConversationQueries
from cloud_agent.storage.repositories.goals import new_goal_id

if TYPE_CHECKING:
    import asyncpg


class PostgresSessionForkRepository:
    """Forking a retained chat: prepare, materialize, finalize, reap."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns.

        A finalized fork answers with the child turn it just made, and that
        read is ``PostgresConversationQueries.get_job`` -- the same call it
        made when both lived on one class, over the same pool, and after this
        class's transaction has already committed. Held as a peer rather than
        reimplemented, so there is still one statement behind it.
        """
        self._pool = pool
        self._queries = PostgresConversationQueries(pool)

    async def prepare_session_fork(
        self,
        *,
        source_job_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Fence a settled native boundary and reserve an invisible child."""
        if not idempotency_key or len(idempotency_key) > 256 or "\x00" in idempotency_key:
            raise ValueError("session fork idempotency key is invalid")
        operation_id = new_session_fork_id()
        target_thread_id = new_thread_id()
        async with self._pool.acquire() as conn, conn.transaction():
            # A request may race itself across two HTTP workers. Serialize the
            # unique-key lookup before either can observe a missing row.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                json.dumps([user_id, idempotency_key], separators=(",", ":")),
            )
            # Finalization holds the operation before the source thread. An
            # idempotent retry must use that same order before checking the
            # owner-history tombstone, or it could deadlock with finalization.
            existing = await conn.fetchrow(
                """
                SELECT * FROM agent_session_forks
                WHERE user_id = $1 AND idempotency_key = $2
                FOR UPDATE
                """,
                user_id,
                idempotency_key,
            )
            thread_id = await conn.fetchval(
                "SELECT thread_id FROM agent_jobs WHERE id = $1 AND user_id = $2",
                source_job_id,
                user_id,
            )
            owned_thread = await conn.fetchval(
                "SELECT id FROM agent_threads "
                "WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
                thread_id,
                user_id,
            )
            if owned_thread is None:
                return None
            if existing is not None:
                result = await self._session_fork_details(conn, str(existing["id"]))
                if result is not None and existing["source_job_id"] != source_job_id:
                    result["error"] = "idempotency_conflict"
                return result

            # Replay above may recover a committed child after archival. Only
            # new operations are refused, under the source thread lock.
            if (
                await conn.fetchval(
                    "SELECT archived_at FROM agent_threads WHERE id = $1", thread_id
                )
                is not None
            ):
                return {"error": "session_archived"}

            source = await conn.fetchrow(
                f"{JOB_SELECT} WHERE j.id = $1 AND j.user_id = $2 FOR UPDATE OF j",
                source_job_id,
                user_id,
            )
            if source is None:
                return None
            # A failed/cancelled platform job is not proof of a completed native
            # exchange. Historical rewind is anchored only on successful turns;
            # their ordinal is resolved against the provider transcript later.
            if source["state"] != SUCCEEDED:
                return {"error": "anchor_not_settled"}
            if not source["native_turn_no"]:
                return {"error": "native_context_unavailable"}
            await conn.fetchval(
                """
                SELECT thread_id
                FROM agent_thread_sandbox_memberships
                WHERE thread_id = $1
                FOR UPDATE
                """,
                source["thread_id"],
            )
            row = await conn.fetchrow(
                f"""
                SELECT m.*, g.generation AS sandbox_generation, g.host,
                       g.container_id, g.container_name, g.runtime_id,
                       g.runtime_version, g.sandbox_image_digest,
                       g.execution_profile, g.state AS sandbox_state,
                       g.resident_slot_capacity, g.memory_limit, g.cpu_limit,
                       g.stop_timeout_seconds,
                       {NATIVE_FORK_CAPABLE} AS native_fork_capable
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                FOR UPDATE OF g
                """,
                source["thread_id"],
            )
            if row is None:
                return {"error": "not_shared_live"}
            if not row["native_fork_capable"]:
                # Existing retained v1 containers remain valid for follow-up
                # turns.  Their bundled helpers do not understand historical
                # anchors, however, so never degrade an at-anchor fork into a
                # latest-session fork during a rolling upgrade.
                return {"error": "native_fork_unavailable"}
            if row["active_attempt_id"] is not None:
                return {"error": "source_busy"}
            if row["native_state"] != "ready" or not row["native_session_id"]:
                return {"error": "native_context_unavailable"}
            latest_settled = await conn.fetchrow(
                """
                SELECT turn_no, native_turn_no FROM agent_jobs
                WHERE id = $1 AND thread_id = $2
                """,
                row["last_settled_job_id"],
                source["thread_id"],
            )
            if (
                latest_settled is None
                or latest_settled["native_turn_no"] is None
                or int(latest_settled["turn_no"]) < int(source["turn_no"])
                or int(latest_settled["native_turn_no"]) < int(source["native_turn_no"])
            ):
                return {"error": "native_context_unavailable"}
            if row["resident_slot_id"] is None:
                return {"error": "sandbox_not_ready"}
            if row["fork_operation_id"] is not None:
                return {"error": "fork_in_progress"}
            if row["sandbox_state"] in {
                "lost",
                "stopped_dirty",
                "provisioning",
                "stopping",
            }:
                return {"error": "sandbox_not_ready"}
            if not row["container_id"]:
                return {"error": "sandbox_not_ready"}
            if row["runtime_id"] != source["runtime"]:
                return {"error": "runtime_mismatch"}

            occupied_rows = await conn.fetch(
                """
                SELECT resident_slot_id AS slot
                FROM agent_thread_sandbox_memberships
                WHERE sandbox_id = $1 AND resident_slot_id IS NOT NULL
                UNION ALL
                SELECT target_resident_slot_id AS slot
                FROM agent_session_forks
                WHERE sandbox_id = $1 AND state IN ('preparing', 'materialized')
                  AND target_resident_slot_id IS NOT NULL
                """,
                row["sandbox_id"],
            )
            occupied = {int(item["slot"]) for item in occupied_rows}
            target_slot = next(
                (
                    slot
                    for slot in range(int(row["resident_slot_capacity"]))
                    if slot not in occupied
                ),
                None,
            )
            if target_slot is None:
                return {"error": "resident_capacity_exhausted"}

            # The child exists from here on: a thread with no turns, invisible
            # to every owner-facing list until finalization copies the
            # history in, and a membership that already owns its resident
            # slot and can carry the residency the host is about to open.
            source_thread = await conn.fetchrow(
                "SELECT repo, title, setup_script, execution_profile, "
                "goal_objective, goal_max_rounds "
                "FROM agent_threads WHERE id = $1",
                source["thread_id"],
            )
            # A fork inherits the objective, because the work it copies was
            # work toward that objective. It does not inherit the right to keep
            # spending on it: a fork is a different conversation exploring a
            # different branch of the same task, and it starts paused so the
            # person who made it decides when — and whether — it runs. The
            # counters start over, so the new conversation gets the whole round
            # budget and cannot be blocked by rounds its source spent.
            await conn.execute(
                """
                INSERT INTO agent_threads
                    (id, user_id, repo, title, title_source,
                     setup_script, execution_profile,
                     goal_id, goal_objective, goal_state, goal_pause_reason,
                     goal_pause_detail, goal_max_rounds, goal_revision,
                     goal_created_at, goal_updated_at)
                VALUES ($1, $2, $3, $4, 'inherited', $5, $6,
                        CASE WHEN $7::text IS NULL THEN NULL ELSE $9 END,
                        $7, CASE WHEN $7::text IS NULL THEN NULL ELSE 'paused' END,
                        CASE WHEN $7::text IS NULL THEN NULL ELSE 'fork' END,
                        CASE WHEN $7::text IS NULL THEN NULL
                             ELSE 'copied from another conversation; resume it to start working' END,
                        $8, CASE WHEN $7::text IS NULL THEN 0 ELSE 1 END,
                        CASE WHEN $7::text IS NULL THEN NULL ELSE NOW() END,
                        CASE WHEN $7::text IS NULL THEN NULL ELSE NOW() END)
                """,
                target_thread_id,
                user_id,
                source_thread["repo"],
                f"{source_thread['title']} (fork)"[:160],
                source_thread["setup_script"],
                source_thread["execution_profile"],
                source_thread["goal_objective"],
                source_thread["goal_max_rounds"],
                new_goal_id(),
            )
            await conn.execute(
                """
                INSERT INTO agent_mcp_thread_bindings
                    (thread_id, connection_id, alias, enabled, selection_pending,
                     config_revision, authorization_epoch, capability_revision,
                     policy_revision, rules, resource_rules, prompt_rules)
                SELECT $1, connection_id, alias, enabled, selection_pending,
                       config_revision, authorization_epoch, capability_revision,
                       policy_revision, rules, resource_rules, prompt_rules
                FROM agent_mcp_thread_bindings WHERE thread_id = $2
                """,
                target_thread_id,
                source["thread_id"],
            )
            await conn.execute(
                """
                INSERT INTO agent_session_forks (
                    id, user_id, idempotency_key, sandbox_id,
                    sandbox_generation, container_id, runtime_id,
                    runtime_version, source_thread_id, source_job_id,
                    source_native_session_id, source_activation_generation,
                    target_thread_id, target_resident_slot_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14
                )
                """,
                operation_id,
                user_id,
                idempotency_key,
                row["sandbox_id"],
                row["sandbox_generation"],
                row["container_id"],
                row["runtime_id"],
                row["runtime_version"],
                source["thread_id"],
                source_job_id,
                row["native_session_id"],
                row["activation_generation"],
                target_thread_id,
                target_slot,
            )
            await conn.execute(
                """
                INSERT INTO agent_thread_sandbox_memberships (
                    thread_id, user_id, sandbox_id, resident_slot_id,
                    forked_from_thread_id, fork_anchor_job_id, fork_operation_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                target_thread_id,
                user_id,
                row["sandbox_id"],
                target_slot,
                source["thread_id"],
                source_job_id,
                operation_id,
            )
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET native_state = 'fork_preparing', fork_operation_id = $2,
                    updated_at = NOW()
                WHERE thread_id = $1
                """,
                source["thread_id"],
                operation_id,
            )
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET active_operation_count = active_operation_count + 1,
                    idle_deadline = NULL, last_activity_at = NOW(), updated_at = NOW()
                WHERE id = $1
                """,
                row["sandbox_id"],
            )
            return await self._session_fork_details(conn, operation_id)

    async def establish_session_fork_residencies(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        ttl_seconds: float,
    ) -> dict[str, Any] | None:
        """Create/recover the two independent process credentials for a fork."""
        if not coordinator_token or len(coordinator_token) > 256 or "\x00" in coordinator_token:
            raise ValueError("session fork coordinator token is invalid")
        async with self._pool.acquire() as conn, conn.transaction():
            operation = await conn.fetchrow(
                "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if (
                operation is None
                or operation["state"] != "preparing"
                or operation["coordinator_token"] != coordinator_token
            ):
                return None
            source_job = await conn.fetchrow(
                """
                SELECT user_id, model FROM agent_jobs
                WHERE id = $1 AND user_id = $2
                """,
                operation["source_job_id"],
                operation["user_id"],
            )
            if source_job is None:
                return None
            generations: dict[str, int] = {}
            previous: dict[str, str | None] = {}
            thread_ids = {
                "source": str(operation["source_thread_id"]),
                "target": str(operation["target_thread_id"]),
            }
            sandbox_host = await conn.fetchval(
                "SELECT host FROM agent_sandboxes WHERE id = $1 AND generation = $2",
                operation["sandbox_id"],
                operation["sandbox_generation"],
            )
            if sandbox_host is None:
                return None
            for role, thread_id in thread_ids.items():
                current = await conn.fetchrow(
                    """
                    SELECT residency_grant_id FROM agent_thread_sandbox_memberships
                    WHERE thread_id = $1 AND sandbox_id = $2
                    FOR UPDATE
                    """,
                    thread_id,
                    operation["sandbox_id"],
                )
                if current is None:
                    return None
                # The grant token itself is deliberately never stored.  A
                # retried HTTP fork therefore cannot safely reuse a residency
                # whose token may already be inside an unacknowledged host
                # process: minting over the same generation would relabel that
                # old-token process as current.  Rotate the generation on every
                # PREPARING attempt, revoke the prior linked grant, and make the
                # supervisor restart only these two member processes.
                previous[role] = (
                    current["residency_grant_id"] or operation[f"previous_{role}_grant_id"]
                )
                created = await conn.fetchrow(
                    """
                    UPDATE agent_thread_sandbox_memberships m
                    SET residency_generation = m.residency_generation + 1,
                        residency_status = 'hot',
                        residency_lease_expires_at = NOW() + make_interval(secs => $3),
                        residency_grant_id = NULL,
                        residency_binding_id = NULL,
                        residency_established_at = NOW(),
                        residency_last_active_at = NOW(),
                        updated_at = NOW()
                    FROM agent_sandboxes g
                    WHERE m.thread_id = $1 AND m.sandbox_id = $2
                      AND g.id = m.sandbox_id AND g.generation = $4
                    RETURNING m.residency_generation AS generation
                    """,
                    thread_id,
                    operation["sandbox_id"],
                    ttl_seconds,
                    operation["sandbox_generation"],
                )
                if created is None:
                    return None
                generations[role] = int(created["generation"])

            await conn.execute(
                """
                UPDATE agent_session_forks
                SET source_residency_generation = $2,
                    target_residency_generation = $3,
                    previous_source_grant_id = $4,
                    previous_target_grant_id = $5,
                    updated_at = NOW()
                WHERE id = $1
                """,
                operation_id,
                generations["source"],
                generations["target"],
                previous["source"],
                previous["target"],
            )
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET resident_state = 'restarting', updated_at = NOW()
                WHERE thread_id = $1 AND residency_generation = $2
                  AND fork_operation_id = $3
                """,
                operation["source_thread_id"],
                generations["source"],
                operation_id,
            )
            common = {
                "user_id": source_job["user_id"],
                "model": source_job["model"],
            }
            return {
                "source": {
                    **common,
                    "thread_id": thread_ids["source"],
                    "generation": generations["source"],
                    "previous_grant_id": previous["source"],
                    "establishing_attempt_id": f"fork-source:{operation_id}",
                },
                "target": {
                    **common,
                    "thread_id": thread_ids["target"],
                    "generation": generations["target"],
                    "previous_grant_id": previous["target"],
                    "establishing_attempt_id": f"fork-target:{operation_id}",
                },
            }

    async def claim_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        lease_seconds: float,
    ) -> bool:
        """Lease the one external native mutation for a PREPARING saga.

        Idempotency-key insertion is serialized in :meth:`prepare_session_fork`,
        but its HTTP handler necessarily leaves that transaction before minting
        credentials and calling the host. This second durable fence prevents
        two workers handling the same request from rotating both member
        residencies and invoking the native fork concurrently. An abandoned
        coordinator can be taken over after the bounded lease.
        """
        if not coordinator_token or len(coordinator_token) > 256 or "\x00" in coordinator_token:
            raise ValueError("session fork coordinator token is invalid")
        if lease_seconds <= 0:
            raise ValueError("session fork coordinator lease must be positive")
        async with self._pool.acquire() as conn:
            claimed = await conn.fetchval(
                """
                UPDATE agent_session_forks
                SET coordinator_token = $2,
                    coordinator_lease_expires_at = NOW() + make_interval(secs => $3),
                    updated_at = NOW()
                WHERE id = $1 AND state = 'preparing'
                  AND (
                    coordinator_token IS NULL
                    OR coordinator_lease_expires_at IS NULL
                    OR coordinator_lease_expires_at <= NOW()
                    OR coordinator_token = $2
                  )
                RETURNING 1
                """,
                operation_id,
                coordinator_token,
                lease_seconds,
            )
        return claimed is not None

    async def release_session_fork_materialization(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
    ) -> bool:
        """Release a coordinator that failed before any host mutation began."""
        async with self._pool.acquire() as conn:
            released = await conn.fetchval(
                """
                UPDATE agent_session_forks
                SET coordinator_token = NULL, coordinator_lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND state = 'preparing' AND coordinator_token = $2
                RETURNING 1
                """,
                operation_id,
                coordinator_token,
            )
        return released is not None

    async def record_session_fork_materialized(
        self,
        *,
        operation_id: str,
        coordinator_token: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a validated host receipt before exposing the child chat."""
        async with self._pool.acquire() as conn, conn.transaction():
            operation = await conn.fetchrow(
                "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if operation is None or operation["state"] not in {"preparing", "materialized"}:
                return None
            if (
                operation["state"] == "preparing"
                and operation["coordinator_token"] != coordinator_token
            ):
                return None
            expected = {
                "operation_id": operation_id,
                "sandbox_id": operation["sandbox_id"],
                "sandbox_generation": int(operation["sandbox_generation"]),
                "container_id": operation["container_id"],
                "runtime_id": operation["runtime_id"],
                "runtime_version": operation["runtime_version"],
                "source_thread_id": operation["source_thread_id"],
                "source_native_session_id": operation["source_native_session_id"],
                "target_thread_id": operation["target_thread_id"],
                "target_resident_slot_id": int(operation["target_resident_slot_id"]),
                "source_residency_generation": int(operation["source_residency_generation"] or 0),
                "target_residency_generation": int(operation["target_residency_generation"] or 0),
            }
            if any(receipt.get(key) != value for key, value in expected.items()):
                return None
            target_native_id = str(receipt.get("target_native_session_id") or "")
            source_process = int(receipt.get("source_process_generation") or 0)
            target_process = int(receipt.get("target_process_generation") or 0)
            if (
                not target_native_id
                or target_native_id == operation["source_native_session_id"]
                or source_process <= 0
                or target_process <= 0
            ):
                return None
            if operation["state"] == "materialized":
                prior = load_json(operation["host_receipt"])
                return (
                    await self._session_fork_details(conn, operation_id)
                    if prior == receipt
                    else None
                )
            collision = await conn.fetchval(
                """
                SELECT 1 FROM agent_thread_sandbox_memberships
                WHERE sandbox_id = $1 AND native_session_id = $2
                """,
                operation["sandbox_id"],
                target_native_id,
            )
            if collision is not None:
                return None
            await conn.execute(
                """
                UPDATE agent_session_forks
                SET target_native_session_id = $2,
                    source_process_generation = $3,
                    target_process_generation = $4,
                    host_receipt = $5::jsonb, state = 'materialized',
                    coordinator_token = NULL, coordinator_lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = $1
                """,
                operation_id,
                target_native_id,
                source_process,
                target_process,
                json.dumps(receipt),
            )
            await conn.execute(
                """
                UPDATE agent_thread_sandbox_memberships
                SET process_generation = $2, residency_generation = $3,
                    resident_state = 'live', updated_at = NOW()
                WHERE thread_id = $1 AND fork_operation_id = $4
                """,
                operation["source_thread_id"],
                source_process,
                operation["source_residency_generation"],
                operation_id,
            )
            await conn.execute(
                """
                UPDATE agent_sandboxes
                SET state = 'running', last_activity_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND generation = $2 AND container_id = $3
                """,
                operation["sandbox_id"],
                operation["sandbox_generation"],
                operation["container_id"],
            )
            return await self._session_fork_details(conn, operation_id)

    async def finalize_session_fork(self, *, operation_id: str) -> dict[str, Any] | None:
        """Atomically create the visible child history, membership and native edge."""
        anchor_id: str | None = None
        async with self._pool.acquire() as conn, conn.transaction():
            operation = await conn.fetchrow(
                "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE",
                operation_id,
            )
            if operation is None:
                return None
            if operation["state"] == "ready":
                anchor_id = str(operation["target_anchor_job_id"] or "") or None
            elif operation["state"] == "materialized":
                # Match create_follow_up's thread -> job -> membership lock
                # order. Locking the source job first here while a concurrent
                # follow-up held the thread row produced a real PostgreSQL
                # deadlock cycle (each transaction then waited for the other).
                thread = await conn.fetchrow(
                    "SELECT repo, title, setup_script, execution_profile "
                    "FROM agent_threads WHERE id = $1 FOR UPDATE",
                    operation["source_thread_id"],
                )
                if thread is None:
                    return None
                source = await conn.fetchrow(
                    f"{JOB_SELECT} WHERE j.id = $1 FOR UPDATE OF j",
                    operation["source_job_id"],
                )
                membership = await conn.fetchrow(
                    """
                    SELECT * FROM agent_thread_sandbox_memberships
                    WHERE thread_id = $1 FOR UPDATE
                    """,
                    operation["source_thread_id"],
                )
                if (
                    source is None
                    or membership is None
                    or membership["fork_operation_id"] != operation_id
                    or membership["native_session_id"] != operation["source_native_session_id"]
                    or not operation["target_native_session_id"]
                ):
                    return None
                turns = await conn.fetch(
                    f"{JOB_SELECT} WHERE j.thread_id = $1 AND j.turn_no <= $2 ORDER BY j.turn_no",
                    operation["source_thread_id"],
                    source["turn_no"],
                )
                messages = await conn.fetch(
                    """
                    SELECT m.job_id, m.role, m.content, m.created_at, m.mid_turn
                    FROM agent_thread_messages m
                    JOIN agent_jobs j ON j.id = m.job_id
                    WHERE m.thread_id = $1 AND j.turn_no <= $2
                    ORDER BY j.turn_no,
                             CASE WHEN m.role = 'user' AND NOT m.mid_turn THEN 0 ELSE 1 END,
                             m.id
                    """,
                    operation["source_thread_id"],
                    source["turn_no"],
                )
                copied: dict[str, str] = {}
                previous_copy: str | None = None
                for turn in turns:
                    copy_id = new_job_id()
                    copied[str(turn["id"])] = copy_id
                    await conn.execute(
                        """
                        INSERT INTO agent_jobs (
                            id, thread_id, parent_job_id, turn_no, native_turn_no, user_id,
                            base_sha, task_prompt, runtime, model, reasoning_effort, state,
                            published_pr_url, published_commit_sha, detail, metadata,
                            fork_source_job_id, skills, created_at, updated_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            $13, $14, $15, $16::jsonb, $17, $18::jsonb,
                            clock_timestamp(), clock_timestamp()
                        )
                        """,
                        copy_id,
                        operation["target_thread_id"],
                        previous_copy,
                        turn["turn_no"],
                        turn["native_turn_no"],
                        operation["user_id"],
                        turn["base_sha"],
                        turn["task_prompt"],
                        turn["runtime"],
                        turn["model"],
                        turn["reasoning_effort"],
                        turn["state"] if turn["state"] in TERMINAL_STATES else CANCELLED,
                        turn["published_pr_url"],
                        turn["published_commit_sha"],
                        turn["detail"],
                        json.dumps(load_json(turn["metadata"]))
                        if turn["metadata"] is not None
                        else None,
                        turn["id"],
                        json.dumps(load_json(turn["skills"]))
                        if turn["skills"] is not None
                        else None,
                    )
                    previous_copy = copy_id
                await conn.executemany(
                    """
                    INSERT INTO agent_thread_messages
                        (thread_id, job_id, role, content, created_at, mid_turn)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    [
                        (
                            operation["target_thread_id"],
                            copied[str(message["job_id"])],
                            message["role"],
                            message["content"],
                            message["created_at"],
                            # A copy keeps what each row was: a turn with a
                            # message sent into it has two user rows here too.
                            message["mid_turn"],
                        )
                        for message in messages
                    ],
                )
                anchor_id = copied[str(source["id"])]
                # Preparation made the member; finalization makes it live.
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_session_id = $3, native_state = 'ready',
                        context_status = 'live', process_generation = $4,
                        residency_generation = $5, resident_state = 'live',
                        resident_slot_id = $6, last_settled_job_id = $7,
                        fork_operation_id = NULL, last_error = NULL,
                        updated_at = NOW()
                    WHERE thread_id = $1 AND sandbox_id = $2 AND fork_operation_id = $8
                    """,
                    operation["target_thread_id"],
                    operation["sandbox_id"],
                    operation["target_native_session_id"],
                    operation["target_process_generation"],
                    operation["target_residency_generation"],
                    operation["target_resident_slot_id"],
                    anchor_id,
                    operation_id,
                )
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_state = 'ready', fork_operation_id = NULL,
                        process_generation = $2, residency_generation = $3,
                        resident_state = 'live', updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $4
                    """,
                    operation["source_thread_id"],
                    operation["source_process_generation"],
                    operation["source_residency_generation"],
                    operation_id,
                )
                await conn.execute(
                    """
                    INSERT INTO agent_native_session_edges (
                        operation_id, sandbox_id, source_thread_id,
                        target_thread_id, source_native_session_id,
                        target_native_session_id
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    operation_id,
                    operation["sandbox_id"],
                    operation["source_thread_id"],
                    operation["target_thread_id"],
                    operation["source_native_session_id"],
                    operation["target_native_session_id"],
                )
                await conn.execute(
                    """
                    UPDATE agent_session_forks
                    SET target_anchor_job_id = $2, state = 'ready',
                        completed_at = NOW(), updated_at = NOW()
                    WHERE id = $1
                    """,
                    operation_id,
                    anchor_id,
                )
                await conn.execute(
                    """
                    UPDATE agent_sandboxes g
                    SET active_operation_count = GREATEST(active_operation_count - 1, 0),
                        last_activity_at = NOW(),
                        idle_deadline = CASE
                            WHEN active_operation_count = 1
                             AND NOT EXISTS (
                                SELECT 1 FROM agent_thread_sandbox_memberships m
                                WHERE m.sandbox_id = g.id
                                  AND m.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE g.id = $1
                    """,
                    operation["sandbox_id"],
                )
            else:
                return None
        return None if anchor_id is None else await self._queries.get_job(anchor_id)

    async def reap_stalled_session_forks(
        self, *, stall_seconds: float = 900.0
    ) -> list[dict[str, Any]]:
        """Recover durable native-fork receipts or fail an ambiguous attempt.

        ``materialized`` has a validated host receipt and is safe to finalize.
        A stale ``preparing`` operation has no receipt the control plane can
        trust, so it is failed without exposing its hidden target. Its exact
        source/target residency generations are lapsed for member-scoped host
        cleanup and grant revocation; siblings are never touched.
        """
        actions: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            candidates = await conn.fetch(
                """
                SELECT id FROM agent_session_forks
                WHERE state IN ('preparing', 'materialized')
                  AND updated_at < NOW() - make_interval(secs => $1)
                ORDER BY id
                """,
                stall_seconds,
            )
            for candidate in candidates:
                operation = await conn.fetchrow(
                    "SELECT * FROM agent_session_forks WHERE id = $1 FOR UPDATE SKIP LOCKED",
                    candidate["id"],
                )
                if operation is None:
                    continue
                if operation["state"] == "materialized":
                    item = dict(operation)
                    item["recovery_action"] = "finalize"
                    actions.append(item)
                    continue
                if operation["state"] != "preparing":
                    continue
                changed = await conn.fetchrow(
                    """
                    UPDATE agent_session_forks
                    SET state = 'failed',
                        last_error = 'native fork coordinator stopped before a receipt was recorded',
                        coordinator_token = NULL,
                        coordinator_lease_expires_at = NULL,
                        updated_at = NOW(), completed_at = NOW()
                    WHERE id = $1 AND state = 'preparing'
                      AND updated_at < NOW() - make_interval(secs => $2)
                    RETURNING *
                    """,
                    operation["id"],
                    stall_seconds,
                )
                if changed is None:
                    continue
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET native_state = CASE
                            WHEN context_status = 'lost' THEN 'lost' ELSE 'ready' END,
                        fork_operation_id = NULL,
                        resident_state = CASE
                            WHEN context_status = 'lost' THEN 'lost' ELSE 'restarting' END,
                        last_error = $3, updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $2
                    """,
                    operation["source_thread_id"],
                    operation["id"],
                    changed["last_error"],
                )
                await conn.execute(
                    """
                    UPDATE agent_sandboxes g
                    SET active_operation_count = GREATEST(active_operation_count - 1, 0),
                        idle_deadline = CASE
                            WHEN active_operation_count = 1
                             AND NOT EXISTS (
                                SELECT 1 FROM agent_thread_sandbox_memberships m
                                WHERE m.sandbox_id = g.id
                                  AND m.active_attempt_id IS NOT NULL
                             )
                            THEN NOW() + make_interval(secs => idle_timeout_seconds)
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    operation["sandbox_id"],
                )
                cleanup: list[dict[str, Any]] = []
                sandbox_host = await conn.fetchval(
                    "SELECT host FROM agent_sandboxes WHERE id = $1",
                    operation["sandbox_id"],
                )
                for role, thread_column, generation_column in (
                    (
                        "source",
                        "source_thread_id",
                        "source_residency_generation",
                    ),
                    (
                        "target",
                        "target_thread_id",
                        "target_residency_generation",
                    ),
                ):
                    generation = operation[generation_column]
                    if generation is None:
                        continue
                    residency = await conn.fetchrow(
                        """
                        UPDATE agent_thread_sandbox_memberships m
                        SET residency_status = 'none', residency_last_active_at = NOW()
                        FROM agent_sandboxes g
                        WHERE m.thread_id = $1 AND m.residency_generation = $2
                          AND g.id = m.sandbox_id AND g.host = $3
                          AND m.residency_status <> 'none'
                        RETURNING m.thread_id, m.residency_generation AS generation,
                                  m.residency_grant_id AS grant_id
                        """,
                        operation[thread_column],
                        generation,
                        sandbox_host,
                    )
                    if residency is not None:
                        member = dict(residency)
                        member["role"] = role
                        cleanup.append(member)
                # The target existed only as the placeholder preparation made.
                # No turn ever landed in it, so it is tombstoned the way an
                # owner's delete tombstones a chat, and its resident slot is
                # given back to the group.
                await conn.execute(
                    """
                    UPDATE agent_thread_sandbox_memberships
                    SET resident_slot_id = NULL, native_state = 'lost',
                        context_status = 'lost', resident_state = 'lost',
                        fork_operation_id = NULL, last_error = $3, updated_at = NOW()
                    WHERE thread_id = $1 AND fork_operation_id = $2
                    """,
                    operation["target_thread_id"],
                    operation["id"],
                    changed["last_error"],
                )
                await conn.execute(
                    """
                    UPDATE agent_threads
                    SET deleted_at = NOW(), updated_at = NOW()
                    WHERE id = $1 AND deleted_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM agent_jobs WHERE thread_id = $1)
                    """,
                    operation["target_thread_id"],
                )
                item = dict(changed)
                item["recovery_action"] = "cleanup"
                item["cleanup_residencies"] = cleanup
                actions.append(item)
        return actions

    async def _session_fork_details(self, conn: Any, operation_id: str) -> dict[str, Any] | None:
        """Return one native-fork saga with its immutable host launch inputs."""
        operation = await conn.fetchrow(
            "SELECT * FROM agent_session_forks WHERE id = $1",
            operation_id,
        )
        if operation is None:
            return None
        sandbox = await conn.fetchrow(
            f"SELECT s.* FROM {THREAD_SANDBOX_SOURCE} WHERE s.thread_id = $1",
            operation["source_thread_id"],
        )
        source = await conn.fetchrow(
            f"{JOB_SELECT} WHERE j.id = $1",
            operation["source_job_id"],
        )
        result = dict(operation)
        result["host_receipt"] = load_json(result.get("host_receipt"))
        if sandbox is not None:
            sandbox_dict = sandbox_row_to_dict(sandbox)
            result["thread_sandbox"] = sandbox_dict
            for key, value in sandbox_dict.items():
                result.setdefault(key, value)
        if source is not None:
            result["source_job"] = job_row_to_dict(source)
            # Assigned at the native settlement boundary, not inferred from
            # the final job state. A lease may expire after context settlement
            # but before job finalization; subsequent successful turns must
            # still resolve to the provider's correct ordinal.
            result["source_native_turn_no"] = int(source["native_turn_no"] or 0)
        return result
