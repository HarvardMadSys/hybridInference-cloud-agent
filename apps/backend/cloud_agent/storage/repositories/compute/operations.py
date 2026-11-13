"""Compute operations: intents, worker leases, cancellation and retry."""

from __future__ import annotations

from typing import Any

import asyncpg

from cloud_agent.domain.compute import (
    acquisition_absence_confirmable,
    autoscaling_acquire_can_cancel,
    autoscaling_operation_is_terminal,
    autoscaling_retirement_needs_settlement,
    discard_request_id,
    fingerprint as operation_fingerprint,
    retirement_cancellable,
)
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.compute._common import (
    ComputeRepositoryBase,
    _json,
    _optional_row,
    _row,
)


class OperationsMixin(ComputeRepositoryBase):
    """Operation intents, their worker leases, and how they end."""

    async def create_operation(
        self,
        *,
        request_id: str,
        fingerprint: str,
        kind: str,
        allocation_id: str | None,
        parameters: dict[str, Any],
        requested_by: str | None,
    ) -> dict[str, Any]:
        """Persist an idempotent operation with immutable execution parameters."""
        async with self._pool.acquire() as conn, conn.transaction():
            existing = await self._existing_request(conn, request_id, fingerprint)
            if existing is not None:
                return existing
            if allocation_id:
                allocation = await conn.fetchrow(
                    "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE", allocation_id
                )
                if allocation is None:
                    raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
                expected_revision = parameters.get("expected_revision")
                if (
                    kind == "retire"
                    and expected_revision is not None
                    and allocation["revision"] != expected_revision
                ):
                    raise StorageConflict(ConflictCode.REVISION_CONFLICT)
                if allocation["release_committed_at"] is not None and kind != "inspect":
                    raise StorageConflict(ConflictCode.RELEASE_COMMITTED)
                if kind == "retire":
                    existing = await self._existing_request(conn, request_id, fingerprint)
                    if existing is not None:
                        return existing
                    if await conn.fetchval(
                        """SELECT 1 FROM agent_compute_operations
                           WHERE allocation_id=$1 AND kind='retire'
                             AND state IN ('pending','running','waiting','blocked','unknown')
                           LIMIT 1""",
                        allocation_id,
                    ):
                        raise StorageConflict(ConflictCode.OPERATION_IN_FLIGHT)
            try:
                async with conn.transaction():
                    return await self._insert_operation(
                        conn,
                        request_id=request_id,
                        fingerprint=fingerprint,
                        kind=kind,
                        allocation_id=allocation_id,
                        parameters=parameters,
                        requested_by=requested_by,
                    )
            except asyncpg.UniqueViolationError as exc:
                if exc.constraint_name == "idx_compute_one_active_rotation":
                    raise StorageConflict(ConflictCode.ROTATION_IN_PROGRESS) from None
                raise

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Read the durable state and recovery receipt of one operation."""
        async with self._pool.acquire() as conn:
            return _optional_row(
                await conn.fetchrow(
                    "SELECT * FROM agent_compute_operations WHERE id=$1", operation_id
                )
            )

    async def list_operations(
        self,
        *,
        allocation_id: str | None = None,
        request_id: str | None = None,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find recent operation state, including recovery by request ID.

        Newest first: the 200 newest matches, every match not yet settled
        however old, and every failed or blocked acquisition of a server not
        confirmed gone. Each live server is inspected every few minutes, so
        newer rows soon push an acquisition out of the window. One whose
        outcome is unknown holds its server until an administrator adopts or
        retries it, and one that stopped past its first step holds it until a
        retry or "Nothing was created" settles it; the Servers page reaches
        either only through this listing. A server has one acquisition, so
        this stays small, unlike its stream of inspections.
        """
        async with self._pool.acquire() as conn:
            return [
                _row(r)
                for r in await conn.fetch(
                    """SELECT * FROM agent_compute_operations
                WHERE ($1::text IS NULL OR allocation_id=$1) AND ($2::text IS NULL OR request_id=$2)
                  AND ($3::text[] IS NULL OR kind=ANY($3::text[]))
                  AND (state IN ('pending','running','waiting','unknown')
                       OR (state IN ('failed','blocked') AND kind IN ('acquire','add','import')
                           AND allocation_id IN (
                               SELECT id FROM agent_compute_allocations
                               WHERE observed_state NOT IN ('terminated','absent')))
                       OR id IN (
                      SELECT id FROM agent_compute_operations
                      WHERE ($1::text IS NULL OR allocation_id=$1)
                        AND ($2::text IS NULL OR request_id=$2)
                        AND ($3::text[] IS NULL OR kind=ANY($3::text[]))
                      ORDER BY created_at DESC LIMIT 200))
                ORDER BY created_at DESC""",
                    allocation_id,
                    request_id,
                    kinds,
                )
            ]

    async def claim_operation(
        self, *, worker_id: str, lease_seconds: float = 120
    ) -> dict[str, Any] | None:
        """Claim one allocation at a time, recovering an expired call before newer intents.

        An expired call retains its step/result for query recovery. The lease
        cannot cancel a remote request; adapters must never blindly replay it.
        A waiting extension may coexist with recovery rotation: it only changes
        the old resource's deadline, and no local provider call is still running.
        Other mutations, especially retirement, still wait for confirmation.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Serialize the short dispatcher decision. Provider calls are outside
            # this transaction; a blocked retirement relinquishes its lease.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('compute-dispatch',0))"
            )
            row = await conn.fetchrow("""SELECT o.* FROM agent_compute_operations o
                LEFT JOIN agent_compute_allocations a ON a.id=o.allocation_id
                WHERE ((o.state IN ('pending','waiting') AND o.next_attempt_at<=clock_timestamp())
                       OR (o.state='running' AND o.lease_expires_at<=clock_timestamp()))
                  AND (a.release_committed_at IS NULL OR o.kind IN ('retire','inspect','discard'))
                  AND NOT EXISTS (SELECT 1 FROM agent_compute_operations busy
                    WHERE busy.allocation_id=o.allocation_id AND busy.id<>o.id
                      AND (busy.state='running' OR busy.state='unknown' OR (busy.state='waiting' AND busy.result->>'external_pending'='true'))
                      AND NOT (o.kind='rotate' AND busy.kind='extend'
                               AND busy.state='waiting')
                      AND NOT (busy.state='running' AND busy.lease_expires_at<=clock_timestamp()
                               AND o.state='running' AND o.created_at<busy.created_at))
                ORDER BY (o.state='running') DESC,o.created_at LIMIT 1 FOR UPDATE OF o SKIP LOCKED""")
            if row is None:
                return None
            return _row(
                await conn.fetchrow(
                    """UPDATE agent_compute_operations
                SET state='running',worker_id=$2,generation=generation+1,
                    lease_expires_at=clock_timestamp()+make_interval(secs=>$3),updated_at=NOW()
                WHERE id=$1 RETURNING *""",
                    row["id"],
                    worker_id,
                    lease_seconds,
                )
            )

    async def renew_operation_lease(
        self, *, operation_id: str, generation: int, worker_id: str, lease_seconds: float = 120
    ) -> bool:
        """Extend only the still-current worker lease."""
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                await self._fence(conn, operation_id, generation, worker_id)
            except ValueError:
                return False
            return bool(
                await conn.fetchval(
                    """UPDATE agent_compute_operations
                SET lease_expires_at=clock_timestamp()+make_interval(secs=>$4),updated_at=NOW()
                WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
                  AND lease_expires_at>clock_timestamp() RETURNING id""",
                    operation_id,
                    generation,
                    worker_id,
                    lease_seconds,
                )
            )

    async def checkpoint_operation(
        self,
        *,
        operation_id: str,
        generation: int,
        worker_id: str,
        step: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Merge a durable step receipt without releasing the external-call lease."""
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                await self._fence(conn, operation_id, generation, worker_id)
            except ValueError:
                return False
            return bool(
                await conn.fetchval(
                    """UPDATE agent_compute_operations
                SET step=$4,result=result || $5::jsonb,updated_at=NOW()
                WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
                  AND lease_expires_at>clock_timestamp() RETURNING id""",
                    operation_id,
                    generation,
                    worker_id,
                    step,
                    _json(result or {}),
                )
            )

    async def finish_operation(
        self,
        *,
        operation_id: str,
        generation: int,
        worker_id: str,
        state: str,
        step: str,
        message: str | None = None,
        result: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> bool:
        """Commit a fenced result and relinquish the external-call lease."""
        if state not in {"waiting", "blocked", "succeeded", "failed", "unknown"}:
            raise StorageConflict(ConflictCode.INVALID_OPERATION_STATE)
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            try:
                await self._fence(conn, operation_id, generation, worker_id)
            except ValueError:
                return False
            row = await conn.fetchrow(
                """UPDATE agent_compute_operations
                SET state=$4,step=$5,message=$6,result=result || $7::jsonb,
                    next_attempt_at=clock_timestamp()+make_interval(secs=>$8),lease_expires_at=NULL,worker_id=NULL,
                    updated_at=NOW(),completed_at=CASE WHEN $4 IN ('succeeded','failed') THEN NOW() ELSE NULL END
                WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
                  AND lease_expires_at>clock_timestamp() RETURNING id,allocation_id,kind""",
                operation_id,
                generation,
                worker_id,
                state,
                step,
                message,
                _json(result or {}),
                retry_after_seconds or 30.0,
            )
            if row is None:
                return False
            if row["kind"] in {"acquire", "retire", "discard"} and row["allocation_id"] is not None:
                await self._release_terminal_autoscaling_reservation(conn, row["allocation_id"])
            return True

    async def cancel_operation(self, operation_id: str) -> dict[str, Any]:  # noqa: C901, PLR0912 complexity baseline, #771
        """Cancel a safe pre-submit acquisition, retirement or rotation intent.

        A retirement only until it stops its runners; a rotation until its old node stops.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('compute-dispatch',0))"
            )
            row = await conn.fetchrow(
                "SELECT * FROM agent_compute_operations WHERE id=$1 FOR UPDATE", operation_id
            )
            if row is None:
                raise StorageConflict(ConflictCode.OPERATION_NOT_FOUND)
            if row["state"] == "running":
                raise StorageConflict(ConflictCode.OPERATION_IN_FLIGHT)
            allocation = await conn.fetchrow(
                """SELECT a.*,
                          EXISTS (
                              SELECT 1 FROM agent_cloudlab_autoscaling_members m
                              WHERE m.allocation_id=a.id
                          ) AS autoscaling_owned,
                          (SELECT m.state FROM agent_cloudlab_autoscaling_members m
                           WHERE m.allocation_id=a.id) AS autoscaling_member_state,
                          EXISTS (
                              SELECT 1 FROM agent_compute_nodes n
                              WHERE n.allocation_id=a.id
                          ) AS has_nodes
                   FROM agent_compute_allocations a
                   WHERE a.id=$1 FOR UPDATE OF a""",
                row["allocation_id"],
            )
            if allocation is not None and allocation["release_committed_at"]:
                raise StorageConflict(ConflictCode.RELEASE_COMMITTED)
            operation = _row(row)
            candidate = _row(allocation) if allocation is not None else None
            if autoscaling_retirement_needs_settlement(operation, candidate):
                raise StorageConflict(ConflictCode.OPERATION_NOT_CANCELLABLE)
            if autoscaling_operation_is_terminal(operation, candidate):
                if (
                    row["kind"] == "retire"
                    and row["state"] in ("pending", "waiting", "blocked")
                    and candidate is not None
                    and candidate.get("autoscaling_member_state") != "released"
                ):
                    await conn.execute(
                        "DELETE FROM agent_host_admission_holds WHERE allocation_id=$1 AND purpose='retirement' AND source_id=$2",
                        row["allocation_id"],
                        operation_id,
                    )
                    cancelled = _row(
                        await conn.fetchrow(
                            """UPDATE agent_compute_operations
                               SET state='cancelled',
                                   message='Retirement cancelled after terminal provider observation.',
                                   completed_at=NOW(),updated_at=NOW()
                               WHERE id=$1 RETURNING *""",
                            operation_id,
                        )
                    )
                    if not await self._release_terminal_autoscaling_reservation(
                        conn, row["allocation_id"]
                    ):
                        raise StorageConflict(ConflictCode.OPERATION_NOT_CANCELLABLE)
                    return cancelled
                raise StorageConflict(ConflictCode.OPERATION_NOT_CANCELLABLE)
            if row["kind"] == "acquire":
                if candidate is not None:
                    candidate["nodes"] = [True] if candidate.pop("has_nodes") else []
                if not autoscaling_acquire_can_cancel(operation, candidate):
                    raise StorageConflict(ConflictCode.OPERATION_NOT_CANCELLABLE)
                cancelled = _row(
                    await conn.fetchrow(
                        """UPDATE agent_compute_operations
                           SET state='cancelled',
                               message='Acquisition cancelled before provider submission.',
                               completed_at=NOW(),updated_at=NOW()
                           WHERE id=$1 RETURNING *""",
                        operation_id,
                    )
                )
                await conn.execute(
                    """UPDATE agent_compute_allocations
                       SET desired_state='absent',observed_state='absent',
                           scope_complete=TRUE,observation_error=NULL,observed_at=NOW(),
                           revision=revision+1,updated_at=NOW()
                       WHERE id=$1""",
                    row["allocation_id"],
                )
                if not await self._release_terminal_autoscaling_reservation(
                    conn, row["allocation_id"]
                ):
                    raise StorageConflict(ConflictCode.OPERATION_NOT_CANCELLABLE)
                return cancelled
            if row["kind"] == "rotate":
                return await self._cancel_rotation(conn, row)
            if (load_json(row["result"]) or {}).get("external_pending"):
                raise StorageConflict(ConflictCode.EXTERNAL_OPERATION_UNRESOLVED)
            if not retirement_cancellable(row["kind"], row["state"], load_json(row["result"])):
                raise StorageConflict(ConflictCode.OPERATION_NOT_CANCELLABLE)
            await conn.execute(
                "DELETE FROM agent_host_admission_holds WHERE allocation_id=$1 AND purpose='retirement' AND source_id=$2",
                row["allocation_id"],
                operation_id,
            )
            other = await conn.fetchval(
                "SELECT 1 FROM agent_compute_operations WHERE allocation_id=$1 AND id<>$2 AND kind='retire' AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1",
                row["allocation_id"],
                operation_id,
            )
            if not other:
                await conn.execute(
                    "UPDATE agent_compute_allocations SET desired_state='present',revision=revision+1,updated_at=NOW() WHERE id=$1",
                    row["allocation_id"],
                )
                await conn.execute(
                    """UPDATE agent_cloudlab_autoscaling_members m
                       SET state=CASE
                               WHEN a.observed_state='ready'
                                AND a.desired_state='present'
                                AND a.release_committed_at IS NULL
                               THEN 'active'
                               ELSE 'provisioning'
                           END,
                           updated_at=NOW()
                       FROM agent_compute_allocations a
                       WHERE a.id=m.allocation_id AND m.allocation_id=$1""",
                    row["allocation_id"],
                )
            cancelled = _row(
                await conn.fetchrow(
                    "UPDATE agent_compute_operations SET state='cancelled',completed_at=NOW(),updated_at=NOW() WHERE id=$1 RETURNING *",
                    operation_id,
                )
            )
            await self._release_terminal_autoscaling_reservation(conn, row["allocation_id"])
            return cancelled

    async def _cancel_rotation(self, conn: Any, row: Any) -> dict[str, Any]:
        result = load_json(row["result"]) or {}
        if row["state"] not in ("pending", "waiting", "blocked"):
            raise StorageConflict(ConflictCode.OPERATION_NOT_CANCELLABLE)
        if result.get("source_stopping") or result.get("source_stopped") or result.get("rebound"):
            # The old node is stopped or the name has moved: only the worker's
            # own rollback can bring the Host back.
            raise StorageConflict(ConflictCode.ROTATION_SWITCHING)
        candidate = (result.get("candidate") or {}).get("id")
        if candidate and await conn.fetchval(
            """SELECT 1 FROM agent_compute_operations
            WHERE allocation_id=$1 AND state IN ('running','unknown') LIMIT 1""",
            candidate,
        ):
            raise StorageConflict(ConflictCode.OPERATION_IN_FLIGHT)
        await conn.execute(
            "DELETE FROM agent_host_admission_holds WHERE purpose='rotation' AND source_id=$1",
            row["id"],
        )
        if candidate and not await conn.fetchval(
            "SELECT release_committed_at FROM agent_compute_allocations WHERE id=$1", candidate
        ):
            await conn.execute(
                """UPDATE agent_compute_operations
                SET state='cancelled',message='candidate_abandoned',completed_at=NOW(),updated_at=NOW()
                WHERE allocation_id=$1 AND state IN ('pending','waiting','blocked')""",
                candidate,
            )
            parameters = {"allocation_id": candidate}
            request_id = discard_request_id(candidate)
            if not await conn.fetchval(
                "SELECT 1 FROM agent_compute_operations WHERE request_id=$1", request_id
            ):
                await self._insert_operation(
                    conn,
                    request_id=request_id,
                    fingerprint=operation_fingerprint("discard", parameters),
                    kind="discard",
                    allocation_id=candidate,
                    parameters=parameters,
                    requested_by="compute-rotation",
                )
        return _row(
            await conn.fetchrow(
                """UPDATE agent_compute_operations SET state='cancelled',completed_at=NOW(),updated_at=NOW()
                WHERE id=$1 RETURNING *""",
                row["id"],
            )
        )

    async def resolve_acquisition(self, *, operation_id: str, external_id: str) -> dict[str, Any]:
        """Record explicit administrator identification of an unknown acquisition.

        This never asserts ownership or success. The same operation resumes
        through provider inspection and scope validation before any deployment.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('compute-dispatch',0))"
            )
            operation = await conn.fetchrow(
                "SELECT * FROM agent_compute_operations WHERE id=$1 FOR UPDATE", operation_id
            )
            if operation is None:
                raise StorageConflict(ConflictCode.OPERATION_NOT_FOUND)
            if operation["kind"] != "acquire" or operation["state"] != "unknown":
                raise StorageConflict(ConflictCode.OPERATION_NOT_RESOLVABLE)
            if (load_json(operation["result"]) or {}).get("resource_ref"):
                raise StorageConflict(ConflictCode.OPERATION_NOT_RESOLVABLE)
            allocation = await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE",
                operation["allocation_id"],
            )
            if (
                allocation is None
                or allocation["external_id"] is not None
                or allocation["release_committed_at"] is not None
            ):
                raise StorageConflict(ConflictCode.OPERATION_NOT_RESOLVABLE)
            if await conn.fetchval(
                "SELECT 1 FROM agent_compute_allocations WHERE provider_id=$1 AND external_id=$2",
                allocation["provider_id"],
                external_id,
            ):
                raise StorageConflict(ConflictCode.RESOURCE_ALREADY_MANAGED)
            await conn.execute(
                "UPDATE agent_compute_allocations SET external_id=$2,revision=revision+1,updated_at=NOW() WHERE id=$1",
                allocation["id"],
                external_id,
            )
            return _row(
                await conn.fetchrow(
                    """UPDATE agent_compute_operations
                SET state='pending',next_attempt_at=NOW(),updated_at=NOW(),completed_at=NULL,
                    result=result || jsonb_build_object('resolved_external_id',$2::text)
                WHERE id=$1 RETURNING *""",
                    operation_id,
                    external_id,
                )
            )

    async def confirm_acquisition_absent(self, *, operation_id: str) -> dict[str, Any]:
        """Close an acquisition an administrator confirmed created nothing.

        Nothing here asks the provider: the administrator checked its portal,
        and ``acquisition_absence_confirmable`` names the acquisitions whose
        records cannot contradict them. A node that registered can, since only
        a created machine runs one. The operation is cancelled, so nothing
        retries it into a new request, and the allocation becomes absent,
        which lets its records be removed.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('compute-dispatch',0))"
            )
            operation = await conn.fetchrow(
                "SELECT * FROM agent_compute_operations WHERE id=$1 FOR UPDATE", operation_id
            )
            if operation is None:
                raise StorageConflict(ConflictCode.OPERATION_NOT_FOUND)
            await conn.execute(
                "SELECT 1 FROM agent_compute_allocations WHERE id=$1 FOR UPDATE",
                operation["allocation_id"],
            )
            allocation = await self._allocation(conn, operation["allocation_id"])
            if not acquisition_absence_confirmable(_row(operation), allocation):
                raise StorageConflict(ConflictCode.ABSENCE_NOT_CONFIRMABLE)
            assert allocation is not None
            # A node's first boot registers under its registration row alone,
            # without the compute fence. Taking the open rows waits for one
            # still in flight, which the check below then sees, and leaves a
            # node that boots later no token to register with.
            await conn.execute(
                "DELETE FROM agent_compute_node_registrations "
                "WHERE allocation_id=$1 AND registered_at IS NULL",
                allocation["id"],
            )
            if await conn.fetchval(
                "SELECT 1 FROM agent_compute_node_registrations "
                "WHERE allocation_id=$1 AND registered_at IS NOT NULL LIMIT 1",
                allocation["id"],
            ):
                raise StorageConflict(ConflictCode.PROVIDER_NODE_REGISTERED)
            await conn.execute(
                """UPDATE agent_compute_allocations
                   SET desired_state='absent',observed_state='absent',
                       scope_complete=TRUE,observation_error=NULL,observed_at=NOW(),
                       revision=revision+1,updated_at=NOW()
                   WHERE id=$1""",
                allocation["id"],
            )
            return _row(
                await conn.fetchrow(
                    """UPDATE agent_compute_operations
                       SET state='cancelled',
                           message='An administrator confirmed the provider created nothing for this request.',
                           result=result || jsonb_build_object('confirmed_absent',TRUE),
                           completed_at=NOW(),updated_at=NOW()
                       WHERE id=$1 RETURNING *""",
                    operation_id,
                )
            )

    async def retry_operation(self, operation_id: str) -> dict[str, Any]:
        """Resume an operation from its durable step and original parameters."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('compute-dispatch',0))"
            )
            row = await conn.fetchrow(
                "SELECT * FROM agent_compute_operations WHERE id=$1 FOR UPDATE", operation_id
            )
            if row is None:
                raise StorageConflict(ConflictCode.OPERATION_NOT_FOUND)
            if row["state"] not in ("failed", "blocked", "unknown"):
                raise StorageConflict(ConflictCode.OPERATION_NOT_RETRYABLE)
            if row["allocation_id"] is not None and row["kind"] not in {
                "retire",
                "inspect",
                "discard",
            }:
                release_committed = await conn.fetchval(
                    """SELECT release_committed_at
                       FROM agent_compute_allocations WHERE id=$1 FOR UPDATE""",
                    row["allocation_id"],
                )
                if release_committed is not None:
                    raise StorageConflict(ConflictCode.OPERATION_NOT_RETRYABLE)
            if row["kind"] in {"acquire", "retire", "discard"} and row["allocation_id"] is not None:
                allocation = await conn.fetchrow(
                    """SELECT a.observed_state,a.release_committed_at,
                              m.state AS autoscaling_member_state
                       FROM agent_compute_allocations a
                       JOIN agent_cloudlab_autoscaling_members m
                         ON m.allocation_id=a.id
                       WHERE a.id=$1 FOR UPDATE OF a,m""",
                    row["allocation_id"],
                )
                if allocation is not None:
                    allocation_view = {
                        "autoscaling_owned": True,
                        **dict(allocation),
                    }
                    if autoscaling_operation_is_terminal(_row(row), allocation_view):
                        raise StorageConflict(ConflictCode.OPERATION_NOT_RETRYABLE)
            # Unknown retains the exact step and parameters. Worker must query
            # recover_acquire/release/extend rather than repeat the side effect.
            return _row(
                await conn.fetchrow(
                    """UPDATE agent_compute_operations
                SET state='pending',next_attempt_at=NOW(),completed_at=NULL,updated_at=NOW(),
                    result=CASE WHEN kind='upgrade'
                           THEN result - 'release_attempts' - 'restore_attempted'
                           ELSE result END
                WHERE id=$1 RETURNING *""",
                    operation_id,
                )
            )
