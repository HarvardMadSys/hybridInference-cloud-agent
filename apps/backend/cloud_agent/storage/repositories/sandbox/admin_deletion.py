"""Admin archive and deletion of a sandbox group.

The archive flag, the deletion request, the worker's claim on a due
deletion and its attempt telemetry, and the archive, completion, discard and
failure that settle a deletion. A ``cold`` group whose host is gone is
discarded at once, by the request itself.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import asyncpg

from cloud_agent.domain.sandbox_deletion import (
    DELETION_RETRY_MAX_ATTEMPTS,
    DELETION_RETRY_WINDOW_SECONDS,
    DeletionFailure,
    decide_deletion_failure,
)
from cloud_agent.domain.sandbox_group import (
    DeletionState,
    GroupState,
)
from cloud_agent.ports.sandbox import DeletionFailureDecision
from cloud_agent.schemas.capacity_protocol import FRESH_AFTER_SECONDS
from cloud_agent.storage._sql.admin_audit import insert_audit_row, report_final_outcome
from cloud_agent.storage._sql.hosts import GONE_HOST_SQL, RECLAIMED_HOST_SQL
from cloud_agent.storage._sql.jobs import fail_unrunnable_follow_ups
from cloud_agent.storage._sql.sandbox_transitions import (
    CLAIM_DUE_DELETION_SQL,
    COMPLETE_ADMIN_SANDBOX_DELETION_GROUP_SQL,
    COMPLETE_ADMIN_SANDBOX_DELETION_MEMBERS_SQL,
    COMPLETE_ADMIN_SANDBOX_DISCARD_GROUP_SQL,
    COMPLETE_ADMIN_SANDBOX_DISCARD_MEMBERS_SQL,
    EXHAUST_SPENT_DELETIONS_SQL,
    FAIL_ADMIN_SANDBOX_DELETION_GROUP_SQL,
    MEMBERS_CONFIRMED_STOPPED_SQL,
    RECORD_ADMIN_SANDBOX_ARCHIVE_GROUP_SQL,
    RENEW_ADMIN_SANDBOX_DELETION_CLAIM_GROUP_SQL,
    REQUEST_ADMIN_DELETION_SQL,
    RESUME_ADMIN_DELETION_SQL,
    SET_ADMIN_ARCHIVED_GROUP_SQL,
)
from cloud_agent.storage.repositories.sandbox._common import (
    _GROUP_WORK_PENDING_SQL,
    SandboxRepositoryBase,
    logger,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cloud_agent.domain.audit import FinalOutcome

#: The rows a deletion's final result is reported against: the request, and
#: each administrator's resumption of it. The newest one gets the result, so a
#: resumed request keeps the failure that stopped it and the resumption gets
#: its own result.
_DELETION_REQUESTS = ("agent.sandbox.delete.requested", "agent.sandbox.delete.retry_resumed")
_DELETION_WORKER = "sandbox-deletion-worker"
_DELETION_ATTEMPT_TELEMETRY_TIMEOUT_SECONDS = 0.25
_DELETION_RETRY_EXHAUSTION_BATCH = 50

#: The queued turns of a group's chats, taken without waiting. A restore claim
#: holds the queued turn it hands out while it waits for the group's row, so a
#: deletion holding that row must not wait for the turn: it gives way to the
#: claim instead. A ``NOWAIT`` lock may come after the group (``cloud_agent.storage``).
_LOCK_QUEUED_TURNS_NOWAIT_SQL = """
    SELECT id FROM agent_jobs
    WHERE thread_id = ANY($1::text[]) AND state = 'queued'
    ORDER BY id FOR UPDATE NOWAIT
"""

#: Retire a deleted group's checkpoint receipts: none of them backs a group
#: any more. The objects they name stay in S3, under the bucket's lifecycle
#: rules, as archives do; the control plane does not delete them.
_RETIRE_CHECKPOINTS_SQL = """
    UPDATE agent_sandbox_checkpoints SET state = 'superseded'
    WHERE sandbox_id = $1 AND state IN ('reported', 'verified')
    RETURNING id
"""


class AdminDeletionMixin(SandboxRepositoryBase):
    """Archive, delete and discard a sandbox group for an admin."""

    async def set_admin_archived(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str | None,
        last_activity_at: datetime,
        archived: bool,
        actor_id: str,
        admin_ip: str,
        allow_stale_host: bool = False,
    ) -> str:
        """Audit a reviewed inventory removal or restore without deleting state.

        Stopped, quiescent groups may be hidden. An explicit stale-host
        removal also permits quiescent groups whose host has no fresh
        heartbeat, without changing their lifecycle or native state. A new
        turn or running host observation makes a removed group visible again
        automatically. Containers, native sessions and ledger rows survive.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            # Admission, Fork and reconciliation lock memberships before the
            # group. Keep that order so hiding cannot race an admitted turn.
            await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                "SELECT id, user_id, host, generation, container_id, last_activity_at, "
                "state, archived_at, deletion_state, active_operation_count "
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
            if (group["archived_at"] is not None) == archived:
                return "accepted"
            if group["deletion_state"] in {"pending", "archived"}:
                return "busy"
            stale_host_removal = archived and allow_stale_host
            if archived:
                if stale_host_removal:
                    # This is a reversible visibility change, so do not add
                    # host row locks to the lifecycle lock order. A matching
                    # running observation restores visibility if the host
                    # returns while or just after this transaction commits.
                    host_online = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM agent_runner_hosts
                            WHERE host = $1
                              AND last_heartbeat_at >= clock_timestamp()
                                  - make_interval(secs => $2)
                        )
                        """,
                        group["host"],
                        FRESH_AFTER_SECONDS,
                    )
                    if host_online:
                        return "host_online"
                elif group["state"] != "stopped" and group["deletion_state"] != "deleted":
                    return "not_stopped"
                busy = await conn.fetchval(_GROUP_WORK_PENDING_SQL, sandbox_id)
                if busy or group["active_operation_count"]:
                    return "busy"
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="admin_inventory_archived" if archived else "admin_inventory_restored",
                source="admin",
                actor_id=actor_id,
                from_state="visible" if archived else "archived",
                to_state="archived" if archived else "visible",
                details={"container_present": container_id is not None}
                | ({"stale_host_removal": True} if stale_host_removal else {}),
            )
            await conn.execute(
                SET_ADMIN_ARCHIVED_GROUP_SQL,
                sandbox_id,
                archived,
            )
            # Visibility and its audit receipt commit together. Never delete
            # the container, native membership or immutable execution ledger.
            await insert_audit_row(
                conn,
                action="agent.sandbox.archive" if archived else "agent.sandbox.restore",
                actor_id=actor_id,
                admin_ip=admin_ip,
                target_user_id=group["user_id"],
                details={
                    "sandbox_id": sandbox_id,
                    "host": group["host"],
                    "generation": generation,
                    "container_id": container_id,
                }
                | ({"stale_host_removal": True} if stale_host_removal else {}),
            )
        return "accepted"

    async def request_admin_delete(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str | None,
        last_activity_at: datetime,
        actor_id: str,
        admin_ip: str,
        sandbox_manager_confirmed_stopped: bool = False,
        confirmed_docker_status: str | None = None,
        discard: bool = False,
        source: str = "admin",
    ) -> str:
        """Fence an idle stopped sandbox for deletion.

        A durable request closes admission before the host removes Docker. The
        default path exports and verifies an S3 archive first. ``discard``
        removes the exact stopped or dead container without uploading it. No native
        state or ledger row is removed until the host confirms that removal.
        A stale RUNNING row may be repaired only after the API proves the exact
        host container is stopped or dead, and idle. ``source`` is ``system`` when
        the request removes the sandbox of chats their owner deleted.

        A ``cold`` group has no container to archive (``checkpoint_only``). A
        discard deletes it immediately once its host is gone, without asking any
        host (:meth:`_discard_cold_group`); its ``container_id`` may be
        ``None``. Every other deletion names the exact container its host
        removes (``no_container`` otherwise).
        """
        async with self._pool.acquire() as conn:
            # Read without a lock only to choose the path: each path checks the
            # group again under its own locks.
            cold = await conn.fetchval(
                f"SELECT state = {GroupState.COLD.sql} FROM agent_sandboxes WHERE id = $1",
                sandbox_id,
            )
        if cold:
            if not discard:
                return "checkpoint_only"
            return await self._discard_cold_group(
                sandbox_id=sandbox_id,
                generation=generation,
                container_id=container_id,
                last_activity_at=last_activity_at,
                actor_id=actor_id,
                admin_ip=admin_ip,
                source=source,
            )
        if container_id is None:
            return "no_container"
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                "SELECT * FROM agent_sandboxes WHERE id = $1 FOR UPDATE", sandbox_id
            )
            if group is None:
                return "not_found"
            if (
                group["generation"] != generation
                or group["container_id"] != container_id
                or group["last_activity_at"] != last_activity_at
                # Handed over to its checkpoint since the read above: the
                # administrator reviews it again, as a cold sandbox.
                or group["state"] == GroupState.COLD
            ):
                return "stale"
            resumable_failed = (
                group["deletion_state"] == "failed"
                and group["deletion_request_id"] is not None
                and group["deletion_archive_key"] is not None
            )
            if group["deletion_state"] in {"pending", "archived"} or resumable_failed:
                if not resumable_failed and group["deletion_retry_exhausted_at"] is None:
                    return "accepted"
                state = str(group["deletion_state"])
                request_id = str(group["deletion_request_id"])
                resumed_state = (
                    "archived" if group["deletion_archive_key"] is not None else "pending"
                )
                await self._record_trajectory_event(
                    conn,
                    group=group,
                    event_type="deletion_retry_resumed",
                    source=source,
                    actor_id=actor_id,
                    from_state=state,
                    to_state=resumed_state,
                    details={"request_id": request_id},
                )
                await conn.execute(
                    RESUME_ADMIN_DELETION_SQL,
                    sandbox_id,
                    resumed_state,
                )
                await self._audit_admin_deletion(
                    conn,
                    group=group,
                    request_id=request_id,
                    action="agent.sandbox.delete.retry_resumed",
                    actor_id=actor_id,
                    admin_ip=admin_ip,
                    archive_key=group["deletion_archive_key"],
                )
                return "accepted"
            if group["deletion_state"] == "deleted":
                return "accepted"
            forced_stopped_recovery = (
                group["state"] == "running"
                and sandbox_manager_confirmed_stopped
                and confirmed_docker_status in {"stopped", "dead"}
            )
            if group["state"] != "stopped" and not forced_stopped_recovery:
                return "not_stopped"
            busy = await conn.fetchval(_GROUP_WORK_PENDING_SQL, sandbox_id)
            if busy or group["active_operation_count"]:
                return "busy"
            if forced_stopped_recovery:
                await conn.execute(
                    MEMBERS_CONFIRMED_STOPPED_SQL,
                    sandbox_id,
                )
            request_id = str(uuid4())
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="admin_delete_requested",
                source=source,
                actor_id=actor_id,
                from_state=str(group["deletion_state"]),
                to_state="pending",
                details={"request_id": request_id, "discard": discard},
            )
            await conn.execute(
                REQUEST_ADMIN_DELETION_SQL,
                sandbox_id,
                request_id,
                forced_stopped_recovery,
                discard,
            )
            await self._audit_admin_deletion(
                conn,
                group=group,
                request_id=request_id,
                action="agent.sandbox.delete.requested",
                actor_id=actor_id,
                admin_ip=admin_ip,
                sandbox_manager_confirmed_stopped=forced_stopped_recovery,
                confirmed_docker_status=confirmed_docker_status,
            )
        return "accepted"

    async def _discard_cold_group(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str | None,
        last_activity_at: datetime,
        actor_id: str,
        admin_ip: str,
        source: str,
    ) -> str:
        """Delete a ``cold`` group whose host is gone, at once and without that host.

        Only its checkpoint holds the group. The host it still names was
        removed, forgotten or long silent (``GONE_HOST_SQL``), so no host can
        be asked to remove anything, and none has to be. The request and its
        completion commit together, as the deletion worker completes a discard
        on a machine its provider reclaimed: the group becomes ``lost`` and
        ``deleted``, its chats lose their native context and their queued turns
        fail, and its checkpoint receipts are retired. The checkpoint objects
        stay in S3 under the bucket's lifecycle rules.

        The one writer that can take a cold group is a restore claim, and it
        locks the queued turn it hands out before the group. Under the group's
        row lock this checks the group is still the reviewed cold one: a claim
        that committed first advanced its generation (``stale``). The queued
        turns are then taken without waiting. A claim holding one while it
        waits for this group wins, and this changes nothing (``claimed``); a
        claim that comes later skips the turns held here, and once this commits
        its own check under the group's lock finds the group deleted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            # Conversations, then memberships, then the group: the order every
            # writer of these rows keeps, deletion's completion included. The
            # conversations are taken ``NO KEY``: that still waits for every
            # writer that locks them, but not for a foreign-key check. A claim
            # restoring the group writes the group twice, and Postgres checks
            # the group's key to its origin conversation again on the second
            # write, after the group; waiting on that would close a cycle.
            await conn.fetch(
                "SELECT t.id FROM agent_threads t "
                "JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id "
                "WHERE m.sandbox_id = $1 ORDER BY t.id FOR NO KEY UPDATE OF t",
                sandbox_id,
            )
            members = await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                "SELECT * FROM agent_sandboxes WHERE id = $1 FOR UPDATE", sandbox_id
            )
            if group is None:
                return "not_found"
            if (
                group["generation"] != generation
                or group["container_id"] != container_id
                or group["last_activity_at"] != last_activity_at
            ):
                return "stale"
            if group["deletion_state"] == "deleted":
                return "accepted"
            if group["state"] != GroupState.COLD or group["deletion_state"] in {
                "pending",
                "archived",
            }:
                return "stale"
            if not await conn.fetchval("SELECT " + GONE_HOST_SQL.format(host="$1"), group["host"]):
                return "host_present"
            busy = await conn.fetchval(_GROUP_WORK_PENDING_SQL, sandbox_id)
            if busy or group["active_operation_count"]:
                return "busy"
            try:
                # A savepoint: a refused lock leaves this transaction usable.
                async with conn.transaction():
                    await conn.fetch(
                        _LOCK_QUEUED_TURNS_NOWAIT_SQL,
                        [member["thread_id"] for member in members],
                    )
            except asyncpg.LockNotAvailableError:
                return "claimed"
            request_id = str(uuid4())
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="admin_delete_requested",
                source=source,
                actor_id=actor_id,
                from_state=str(group["deletion_state"]),
                to_state="pending",
                details={"request_id": request_id, "discard": True, "checkpoint_only": True},
            )
            await conn.execute(REQUEST_ADMIN_DELETION_SQL, sandbox_id, request_id, False, True)
            await self._audit_admin_deletion(
                conn,
                group=group,
                request_id=request_id,
                action="agent.sandbox.delete.requested",
                actor_id=actor_id,
                admin_ip=admin_ip,
            )
            retired = await conn.fetch(_RETIRE_CHECKPOINTS_SQL, sandbox_id)
            await self._complete_admin_sandbox_discard_in_transaction(
                conn,
                group=dict(group) | {"deletion_request_id": request_id},
                members=members,
                attempt_correlation_id=None,
                host_reclaimed=False,
                retired_checkpoints=len(retired),
                actor_id=actor_id,
                source=source,
            )
        return "accepted"

    async def claim_due_admin_sandbox_deletion(
        self, *, ttl_seconds: float
    ) -> dict[str, Any] | None:
        """Lease one deletion while enforcing its persisted automatic retry budget."""
        if ttl_seconds <= 0:
            raise ValueError("deletion claim TTL must be positive")
        async with self._pool.acquire() as conn, conn.transaction():
            exhausted = await conn.fetch(
                EXHAUST_SPENT_DELETIONS_SQL,
                DELETION_RETRY_MAX_ATTEMPTS,
                DELETION_RETRY_WINDOW_SECONDS,
                _DELETION_RETRY_EXHAUSTION_BATCH,
            )
            if exhausted:
                request_ids = [str(group["deletion_request_id"]) for group in exhausted]
                try:
                    async with asyncio.timeout(_DELETION_ATTEMPT_TELEMETRY_TIMEOUT_SECONDS):
                        # Exhaustion is authoritative; its attempt history is diagnostic.
                        async with conn.transaction():
                            await conn.execute(
                                """
                                UPDATE agent_sandbox_deletion_attempts
                                SET outcome='failed', failure_code='retry_budget_exhausted',
                                    retry_class='operator_action', retry_scheduled=FALSE,
                                    finished_at=NOW(), updated_at=NOW()
                                WHERE request_id = ANY($1::text[]) AND outcome='running'
                                """,
                                request_ids,
                            )
                except Exception:
                    logger.warning(
                        "Sandbox deletion exhaustion telemetry update failed",
                        extra={"request_count": len(request_ids)},
                    )
            for group in exhausted:
                state = str(group["deletion_state"])
                request_id = str(group["deletion_request_id"])
                await self._record_trajectory_event(
                    conn,
                    group=group,
                    event_type="deletion_retry_budget_exhausted",
                    source="system",
                    actor_id="sandbox-deletion-worker",
                    from_state=state,
                    to_state=state,
                    details={"request_id": request_id},
                )
                await self._report_deletion_result(conn, request_id=request_id, outcome="failed")

            row = await conn.fetchrow(
                CLAIM_DUE_DELETION_SQL,
                str(uuid4()),
                ttl_seconds,
                DELETION_RETRY_MAX_ATTEMPTS,
                DELETION_RETRY_WINDOW_SECONDS,
            )
            if row is None:
                return None
            correlation_id: str | None = None
            attempt_number: int | None = None
            try:
                async with asyncio.timeout(_DELETION_ATTEMPT_TELEMETRY_TIMEOUT_SECONDS):
                    # A savepoint keeps optional diagnostics from rolling back the
                    # already-fenced business claim when the ledger is unavailable.
                    async with conn.transaction():
                        correlation_id = str(uuid4())
                        attempt_number = int(
                            await conn.fetchval(
                                """
                                SELECT COALESCE(MAX(attempt_number), 0) + 1
                                FROM agent_sandbox_deletion_attempts WHERE request_id=$1
                                """,
                                row["deletion_request_id"],
                            )
                        )
                        await conn.execute(
                            """
                            UPDATE agent_sandbox_deletion_attempts
                            SET outcome='lease_expired', failure_code='claim_lease_expired',
                                retry_class='transient', retry_scheduled=TRUE,
                                finished_at=NOW(), updated_at=NOW()
                            WHERE request_id=$1 AND outcome='running'
                            """,
                            row["deletion_request_id"],
                        )
                        await conn.execute(
                            """
                            INSERT INTO agent_sandbox_deletion_attempts
                                (correlation_id, sandbox_id, request_id, host_id, attempt_number,
                                 stage, outcome)
                            VALUES ($1, $2, $3, $4, $5, 'sandbox_manager_resolution', 'running')
                            """,
                            correlation_id,
                            row["id"],
                            row["deletion_request_id"],
                            row["host"],
                            attempt_number,
                        )
            except Exception:
                logger.warning(
                    "Sandbox deletion attempt telemetry insert failed",
                    extra={"request_id": row["deletion_request_id"]},
                )
                # Neither value describes a persisted attempt after savepoint rollback.
                correlation_id = None
                attempt_number = None
        return {
            **dict(row),
            "deletion_attempt_correlation_id": correlation_id,
            "deletion_attempt_number": attempt_number,
        }

    async def update_admin_sandbox_deletion_attempt_stage(
        self,
        *,
        correlation_id: str,
        stage: str,
        sandbox_manager_status: int | None = None,
        cleanup_outcome: str | None = None,
    ) -> bool:
        """Best-effort telemetry update; it never participates in deletion fencing."""
        allowed_stages = {
            "sandbox_manager_resolution",
            "stop_verification",
            "archive_prepare",
            "upload_authorization",
            "archive_upload",
            "archive_verification",
            "durable_receipt",
            "container_removal",
            "database_finalization",
            "discard_container_removal",
            "discard_receipt_validation",
            "cleanup_claim_renewal",
            "cleanup_abort",
        }
        allowed_cleanup = {None, "not_required", "pending", "acknowledged", "unconfirmed"}
        if stage not in allowed_stages or cleanup_outcome not in allowed_cleanup:
            return False
        if sandbox_manager_status is not None and not 100 <= sandbox_manager_status <= 599:
            sandbox_manager_status = None
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE agent_sandbox_deletion_attempts
                    SET stage=$2,
                        sandbox_manager_status=COALESCE($3, sandbox_manager_status),
                        cleanup_outcome=COALESCE($4, cleanup_outcome),
                        updated_at=NOW()
                    WHERE correlation_id=$1 AND outcome='running'
                    """,
                    correlation_id,
                    stage,
                    sandbox_manager_status,
                    cleanup_outcome,
                )
            return result.endswith(" 1")
        except Exception:
            logger.warning(
                "Sandbox deletion attempt telemetry update failed",
                extra={"correlation_id": correlation_id},
            )
            return False

    async def finish_admin_sandbox_deletion_attempt(
        self,
        *,
        correlation_id: str,
        outcome: str,
        failure_code: str | None = None,
        sandbox_manager_status: int | None = None,
        retry_class: str | None = None,
        retry_scheduled: bool | None = None,
        cleanup_outcome: str = "not_required",
    ) -> bool:
        """Best-effort attempt result written after the deletion decision commits."""
        if outcome not in {"retrying", "succeeded", "failed", "lease_expired"}:
            return False
        if failure_code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", failure_code):
            return False
        if retry_class not in {None, "transient", "permanent", "operator_action"}:
            return False
        if (failure_code is None) != (retry_class is None):
            return False
        if sandbox_manager_status is not None and not 100 <= sandbox_manager_status <= 599:
            sandbox_manager_status = None
        if cleanup_outcome not in {"not_required", "pending", "acknowledged", "unconfirmed"}:
            cleanup_outcome = "unconfirmed"
        if outcome == "retrying":
            retry_scheduled = True
        elif outcome in {"succeeded", "failed"}:
            retry_scheduled = False
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE agent_sandbox_deletion_attempts
                    SET outcome=$2, failure_code=$3,
                        sandbox_manager_status=COALESCE($4, sandbox_manager_status),
                        retry_class=$5, retry_scheduled=$6, cleanup_outcome=$7,
                        finished_at=NOW(), updated_at=NOW()
                    WHERE correlation_id=$1 AND outcome='running'
                    """,
                    correlation_id,
                    outcome,
                    failure_code,
                    sandbox_manager_status,
                    retry_class,
                    retry_scheduled,
                    cleanup_outcome,
                )
            return result.endswith(" 1")
        except Exception:
            logger.warning(
                "Sandbox deletion attempt result could not be recorded",
                extra={"correlation_id": correlation_id},
            )
            return False

    async def record_admin_sandbox_archive(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        archive_key: str,
        sha256: str,
        size_bytes: int,
        archive_version_id: str | None = None,
    ) -> bool:
        """Checkpoint an independently verified S3 receipt under the live worker lease."""
        if not archive_key.startswith("s3://") or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("verified archive requires an S3 location and SHA256 digest")
        if size_bytes <= 0:
            raise ValueError("verified archive must contain data")
        async with self._pool.acquire() as conn, conn.transaction():
            group = await self._locked_admin_deletion(
                conn, sandbox_id, request_id, claim_token, generation, container_id
            )
            if group is None:
                return False
            if group["deletion_cleanup_required"]:
                return False
            if group["deletion_state"] == "archived":
                return (
                    group["deletion_archive_key"] == archive_key
                    and group["deletion_sha256"] == sha256
                    and group["deletion_size_bytes"] == size_bytes
                    and group["deletion_archive_version_id"] == archive_version_id
                )
            if group["deletion_state"] != "pending":
                return False
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="deletion_archive_verified",
                source="system",
                actor_id="sandbox-deletion-worker",
                from_state="pending",
                to_state="archived",
                details={
                    "request_id": request_id,
                    "archive_key": archive_key,
                    "archive_version_id": archive_version_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                },
            )
            await conn.execute(
                RECORD_ADMIN_SANDBOX_ARCHIVE_GROUP_SQL,
                sandbox_id,
                archive_key,
                sha256,
                size_bytes,
                archive_version_id,
            )
        return True

    async def renew_admin_sandbox_deletion_claim(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        ttl_seconds: float = 300,
        for_cleanup: bool = False,
        cleanup_archive_key: str | None = None,
    ) -> bool:
        """Extend an unexpired exact worker claim before bounded host cleanup."""
        if ttl_seconds <= 0:
            raise ValueError("deletion claim TTL must be positive")
        if for_cleanup and not cleanup_archive_key:
            raise ValueError("archive cleanup requires the original object key")
        async with self._pool.acquire() as conn, conn.transaction():
            group = await self._locked_admin_deletion(
                conn, sandbox_id, request_id, claim_token, generation, container_id
            )
            if group is None or group["deletion_state"] not in {"pending", "archived"}:
                return False
            if for_cleanup and (
                group["deletion_state"] != "pending"
                or (
                    group["deletion_cleanup_required"]
                    and group["deletion_cleanup_archive_key"] != cleanup_archive_key
                )
            ):
                return False
            await conn.execute(
                RENEW_ADMIN_SANDBOX_DELETION_CLAIM_GROUP_SQL,
                sandbox_id,
                ttl_seconds,
                for_cleanup,
                cleanup_archive_key,
            )
        return True

    @staticmethod
    async def _finish_deletion_attempt_in_transaction(
        conn: asyncpg.Connection,
        *,
        correlation_id: str,
        outcome: str,
        failure_code: str | None = None,
        sandbox_manager_status: int | None = None,
        retry_class: str | None = None,
        retry_scheduled: bool | None = None,
        cleanup_outcome: str = "not_required",
    ) -> None:
        """Commit a worker's terminal outcome with its fenced state transition."""
        if outcome == "retrying":
            retry_scheduled = True
        elif outcome in {"succeeded", "failed"}:
            retry_scheduled = False
        try:
            # Attempt telemetry is ancillary. A failed ledger write must not
            # undo a deletion receipt or the fenced lifecycle transition.
            async with asyncio.timeout(_DELETION_ATTEMPT_TELEMETRY_TIMEOUT_SECONDS):
                async with conn.transaction():
                    result = await conn.execute(
                        """
                        UPDATE agent_sandbox_deletion_attempts
                        SET outcome=$2, failure_code=$3,
                            sandbox_manager_status=COALESCE($4, sandbox_manager_status),
                            retry_class=$5, retry_scheduled=$6, cleanup_outcome=$7,
                            finished_at=NOW(), updated_at=NOW()
                        WHERE correlation_id=$1 AND outcome='running'
                        """,
                        correlation_id,
                        outcome,
                        failure_code,
                        sandbox_manager_status,
                        retry_class,
                        retry_scheduled,
                        cleanup_outcome,
                    )
                    if not result.endswith(" 1"):
                        raise RuntimeError(
                            "sandbox deletion attempt outcome lost its transaction fence"
                        )
        except Exception:
            logger.warning(
                "Sandbox deletion attempt outcome write failed",
                extra={"correlation_id": correlation_id},
            )

    async def complete_admin_sandbox_deletion(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        attempt_correlation_id: str | None = None,
    ) -> bool:
        """Retain the archive and ledger after an exact host deletion acknowledgement."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Follow-up creation takes thread -> latest job -> membership.
            # Take the thread fence first before failing queued member jobs,
            # so a submitter cannot hold a job while waiting for our members.
            await conn.fetch(
                "SELECT t.id FROM agent_threads t "
                "JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id "
                "WHERE m.sandbox_id = $1 ORDER BY t.id FOR UPDATE OF t",
                sandbox_id,
            )
            members = await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await self._locked_admin_deletion(
                conn, sandbox_id, request_id, claim_token, generation, container_id
            )
            if group is None:
                return False
            if group["deletion_state"] == "deleted":
                return True
            if group["deletion_state"] != "archived":
                return False
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="deletion_completed",
                source="system",
                actor_id="sandbox-deletion-worker",
                from_state="archived",
                to_state="deleted",
                details={
                    "request_id": request_id,
                    "archive_key": group["deletion_archive_key"],
                    "archive_version_id": group["deletion_archive_version_id"],
                    "sha256": group["deletion_sha256"],
                    "size_bytes": group["deletion_size_bytes"],
                },
            )
            await conn.execute(
                COMPLETE_ADMIN_SANDBOX_DELETION_GROUP_SQL,
                sandbox_id,
            )
            if attempt_correlation_id is not None:
                await self._finish_deletion_attempt_in_transaction(
                    conn,
                    correlation_id=attempt_correlation_id,
                    outcome="succeeded",
                )
            await conn.execute(
                COMPLETE_ADMIN_SANDBOX_DELETION_MEMBERS_SQL,
                sandbox_id,
            )
            for member in members:
                await fail_unrunnable_follow_ups(conn, thread_id=member["thread_id"])
            await self._report_deletion_result(
                conn,
                request_id=request_id,
                outcome="applied",
                details={"archive_key": group["deletion_archive_key"]},
            )
        return True

    async def complete_admin_sandbox_discard(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        attempt_correlation_id: str | None = None,
        reclaimed_quiet_seconds: float | None = None,
    ) -> bool:
        """Mark native context lost after the host removes a container with no archive.

        With ``reclaimed_quiet_seconds`` the discard finishes without that
        host, which can never confirm it: only when its provider confirmed the
        machine it ran on terminated and it has been silent that long
        (``RECLAIMED_HOST_SQL``). The container went with the machine.
        Otherwise this returns ``False`` and changes nothing. The optional
        attempt id closes the external attempt in the same fenced transition.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.fetch(
                "SELECT t.id FROM agent_threads t "
                "JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id "
                "WHERE m.sandbox_id = $1 ORDER BY t.id FOR UPDATE OF t",
                sandbox_id,
            )
            members = await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await self._locked_admin_deletion(
                conn, sandbox_id, request_id, claim_token, generation, container_id
            )
            if group is None:
                return False
            if group["deletion_state"] == "deleted":
                return bool(group["deletion_discard"])
            if group["deletion_state"] != "pending" or not group["deletion_discard"]:
                return False
            host_reclaimed = reclaimed_quiet_seconds is not None
            if host_reclaimed and not await conn.fetchval(
                "SELECT " + RECLAIMED_HOST_SQL.format(host="$1", quiet="$2"),
                group["host"],
                reclaimed_quiet_seconds,
            ):
                return False
            await self._complete_admin_sandbox_discard_in_transaction(
                conn,
                group=group,
                members=members,
                attempt_correlation_id=attempt_correlation_id,
                host_reclaimed=host_reclaimed,
            )
        return True

    async def recover_reclaimed_admin_sandbox_discard(
        self, *, reclaimed_quiet_seconds: float
    ) -> bool:
        """Finish one provider-confirmed discard without spending another host attempt.

        The provider state and quiet-host interval are checked again under the
        sandbox row lock. This recovery remains available after automatic
        retries stop, whether the bounded budget is exhausted or the failure
        requires operator action.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            candidate = await conn.fetchrow(
                f"""
                SELECT g.id, g.deletion_request_id, g.generation, g.container_id
                FROM agent_sandboxes g
                WHERE g.deletion_state = {DeletionState.PENDING.sql} AND g.deletion_discard
                  AND g.deletion_retry_exhausted_at IS NOT NULL
                  AND g.deletion_request_id IS NOT NULL
                  AND g.generation = g.deletion_generation
                  AND g.container_id = g.deletion_container_id
                  AND g.last_activity_at = g.deletion_last_activity_at
                  AND (g.deletion_claim_expires_at IS NULL
                       OR g.deletion_claim_expires_at <= clock_timestamp())
                  AND """
                + RECLAIMED_HOST_SQL.format(host="g.host", quiet="$1")
                + " ORDER BY g.deletion_requested_at, g.id LIMIT 1",
                reclaimed_quiet_seconds,
            )
            if candidate is None:
                return False

            sandbox_id = str(candidate["id"])
            await conn.fetch(
                "SELECT t.id FROM agent_threads t "
                "JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id "
                "WHERE m.sandbox_id = $1 ORDER BY t.id FOR UPDATE OF t",
                sandbox_id,
            )
            members = await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                f"""
                SELECT * FROM agent_sandboxes
                WHERE id = $1 AND deletion_request_id = $2
                  AND generation = $3 AND container_id = $4
                  AND deletion_state = {DeletionState.PENDING.sql} AND deletion_discard
                  AND deletion_retry_exhausted_at IS NOT NULL
                  AND generation = deletion_generation
                  AND container_id = deletion_container_id
                  AND last_activity_at = deletion_last_activity_at
                  AND (deletion_claim_expires_at IS NULL
                       OR deletion_claim_expires_at <= clock_timestamp())
                FOR UPDATE
                """,
                sandbox_id,
                candidate["deletion_request_id"],
                candidate["generation"],
                candidate["container_id"],
            )
            if group is None or not await conn.fetchval(
                "SELECT " + RECLAIMED_HOST_SQL.format(host="$1", quiet="$2"),
                group["host"],
                reclaimed_quiet_seconds,
            ):
                return False

            await self._complete_admin_sandbox_discard_in_transaction(
                conn,
                group=group,
                members=members,
                attempt_correlation_id=None,
                host_reclaimed=True,
            )
            running_attempts = await conn.fetch(
                "SELECT correlation_id FROM agent_sandbox_deletion_attempts "
                "WHERE request_id = $1 AND sandbox_id = $2 AND outcome = 'running' "
                "ORDER BY attempt_number",
                candidate["deletion_request_id"],
                sandbox_id,
            )
            for attempt in running_attempts:
                await self._finish_deletion_attempt_in_transaction(
                    conn,
                    correlation_id=str(attempt["correlation_id"]),
                    outcome="succeeded",
                )
        return True

    async def _complete_admin_sandbox_discard_in_transaction(
        self,
        conn: asyncpg.Connection,
        *,
        group: Any,
        members: Any,
        attempt_correlation_id: str | None,
        host_reclaimed: bool,
        retired_checkpoints: int | None = None,
        actor_id: str = _DELETION_WORKER,
        source: str = "system",
    ) -> None:
        """Apply one locked discard finalization and its durable audit trail.

        ``retired_checkpoints`` marks a ``cold`` group's discard, which its own
        request completes, as ``actor_id``; it counts the receipts retired.
        """
        request_id = str(group["deletion_request_id"])
        sandbox_id = str(group["id"])
        without_host = (
            {"host_reclaimed": True}
            if host_reclaimed
            else {"checkpoint_only": True, "retired_checkpoints": retired_checkpoints}
            if retired_checkpoints is not None
            else {}
        )
        await self._record_trajectory_event(
            conn,
            group=group,
            event_type="deletion_completed",
            source=source,
            actor_id=actor_id,
            from_state="pending",
            to_state="deleted",
            details={"request_id": request_id, "discard": True} | without_host,
        )
        await conn.execute(
            COMPLETE_ADMIN_SANDBOX_DISCARD_GROUP_SQL,
            sandbox_id,
        )
        if attempt_correlation_id is not None:
            await self._finish_deletion_attempt_in_transaction(
                conn,
                correlation_id=attempt_correlation_id,
                outcome="succeeded",
            )
        await conn.execute(
            COMPLETE_ADMIN_SANDBOX_DISCARD_MEMBERS_SQL,
            sandbox_id,
        )
        for member in members:
            await fail_unrunnable_follow_ups(conn, thread_id=member["thread_id"])
        await self._report_deletion_result(
            conn,
            request_id=request_id,
            outcome="applied",
            details={"discarded": True} | without_host,
            reported_by=actor_id,
        )

    async def fail_admin_sandbox_deletion(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        error: str,
        retain_pending: bool = False,
        attempt_correlation_id: str | None = None,
        failure_code: str | None = None,
        sandbox_manager_status: int | None = None,
        retry_class: str | None = None,
        cleanup_outcome: str = "not_required",
    ) -> DeletionFailureDecision:
        """Apply a fenced failure and return the database's retry disposition."""
        async with self._pool.acquire() as conn, conn.transaction():
            group = await self._locked_admin_deletion(
                conn, sandbox_id, request_id, claim_token, generation, container_id
            )
            if group is None or group["deletion_state"] not in {"pending", "archived"}:
                return DeletionFailureDecision(
                    recorded=False,
                    retry_scheduled=False,
                    retry_at=None,
                    terminal_reason="fence_lost",
                )
            window_open = bool(
                await conn.fetchval(
                    "SELECT $1::timestamptz > clock_timestamp() - make_interval(secs => $2)",
                    group["deletion_retry_started_at"],
                    DELETION_RETRY_WINDOW_SECONDS,
                )
            )
            decision = decide_deletion_failure(
                DeletionFailure(
                    deletion_state=str(group["deletion_state"]),
                    retry_count=int(group["deletion_retry_count"]),
                    window_open=window_open,
                    retain_pending=retain_pending,
                    retry_class=retry_class,
                    error=error,
                )
            )
            state = str(group["deletion_state"])
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="deletion_failed",
                source="system",
                actor_id="sandbox-deletion-worker",
                from_state=state,
                to_state=state if decision.retry_stopped else "failed",
                details={
                    "request_id": request_id,
                    "retry_scheduled": decision.retry_scheduled,
                    "retry_exhausted": decision.retry_exhausted,
                    "retry_stopped": decision.retry_stopped,
                    "retry_class": decision.retry_class,
                    "error": decision.error[:500],
                },
            )
            updated = await conn.fetchrow(
                FAIL_ADMIN_SANDBOX_DELETION_GROUP_SQL,
                sandbox_id,
                decision.retry_scheduled,
                decision.error,
                decision.delay_seconds,
                decision.retry_stopped,
            )
            if attempt_correlation_id is not None:
                await self._finish_deletion_attempt_in_transaction(
                    conn,
                    correlation_id=attempt_correlation_id,
                    outcome="retrying" if decision.retry_scheduled else "failed",
                    failure_code=(
                        "retry_budget_exhausted" if decision.retry_exhausted else failure_code
                    ),
                    sandbox_manager_status=sandbox_manager_status,
                    retry_class=decision.retry_class,
                    retry_scheduled=decision.retry_scheduled,
                    cleanup_outcome=cleanup_outcome,
                )
            if not decision.retry_scheduled:
                # Stopped, exhausted or not retained: nothing runs again unless
                # an administrator resumes it.
                await self._report_deletion_result(conn, request_id=request_id, outcome="failed")
            if updated is None:
                raise RuntimeError("sandbox deletion failure transition lost its row")
            return DeletionFailureDecision(
                recorded=True,
                retry_scheduled=decision.retry_scheduled,
                retry_at=updated["deletion_retry_at"],
                terminal_reason=decision.terminal_reason,
            )

    @staticmethod
    async def _locked_admin_deletion(
        conn: asyncpg.Connection,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
    ) -> Any:
        """Reject stale workers, changed reviewed identity and expired claims."""
        group = await conn.fetchrow(
            f"""
            SELECT * FROM agent_sandboxes
            WHERE id = $1 AND deletion_request_id = $2 AND deletion_claim_token = $3
              AND generation = $4 AND container_id = $5
              AND generation = deletion_generation AND container_id = deletion_container_id
              AND last_activity_at = deletion_last_activity_at
              AND (state = {GroupState.STOPPED.sql} OR deletion_state = {DeletionState.DELETED.sql})
            FOR UPDATE
            """,
            sandbox_id,
            request_id,
            claim_token,
            generation,
            container_id,
        )
        if group is None or group["deletion_state"] == "deleted":
            return group
        return (
            group
            if await conn.fetchval(
                "SELECT deletion_claim_expires_at > clock_timestamp() "
                "FROM agent_sandboxes WHERE id = $1",
                sandbox_id,
            )
            else None
        )

    @staticmethod
    async def _audit_admin_deletion(
        conn: asyncpg.Connection,
        *,
        group: Any,
        request_id: str,
        action: str,
        actor_id: str,
        admin_ip: str,
        archive_key: str | None = None,
        sandbox_manager_confirmed_stopped: bool = False,
        confirmed_docker_status: str | None = None,
    ) -> None:
        """Record a deletion request, or its resumption, with its transition."""
        await insert_audit_row(
            conn,
            action=action,
            actor_id=actor_id,
            admin_ip=admin_ip,
            target_user_id=group["user_id"],
            details={
                "sandbox_id": group["id"],
                "request_id": request_id,
                "host": group["host"],
                "generation": group["generation"],
                "container_id": group["container_id"],
                "archive_key": archive_key,
            }
            | (
                {
                    "force": True,
                    "sandbox_manager_confirmed_stopped": True,
                    "docker_status": confirmed_docker_status,
                }
                if sandbox_manager_confirmed_stopped
                else {}
            ),
        )

    @staticmethod
    async def _report_deletion_result(
        conn: asyncpg.Connection,
        *,
        request_id: str,
        outcome: FinalOutcome,
        details: dict[str, Any] | None = None,
        reported_by: str = _DELETION_WORKER,
    ) -> None:
        """The deletion request's final result; the worker's steps stay in the trajectory."""
        await report_final_outcome(
            conn,
            requested_actions=_DELETION_REQUESTS,
            correlation_id=request_id,
            outcome=outcome,
            reported_by=reported_by,
            details=details,
        )
