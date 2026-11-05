"""Compute rotation: replacement candidates and slot rebinding."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.rotation_candidates import candidate_work_stoppable
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase, _json


class RotationMixin(ComputeRepositoryBase):
    """Rotation candidates, their release, and slot rebinding."""

    async def create_rotation_candidate(
        self,
        *,
        source_allocation_id: str,
        allocation: dict[str, Any],
        request_id: str,
        fingerprint: str,
        parameters: dict[str, Any],
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> dict[str, Any]:
        """Persist a replacement node's allocation and its staging acquisition together.

        Only the rotation of a live allocation whose node runs the named Host
        may create one. The replacement carries the Host identity it will take
        over and never shares a deployment profile with a live allocation.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, source_allocation_id)
            existing = await self._existing_request(conn, request_id, fingerprint)
            if existing is not None:
                return existing
            source = await conn.fetchrow(
                "SELECT desired_state,release_committed_at FROM agent_compute_allocations WHERE id=$1 FOR UPDATE",
                source_allocation_id,
            )
            if (
                source is None
                or source["desired_state"] != "present"
                or source["release_committed_at"] is not None
            ):
                raise StorageConflict(ConflictCode.RESOURCE_RETIRING)
            if not await conn.fetchval(
                """SELECT 1 FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                WHERE n.allocation_id=$1 AND h.host=$2""",
                source_allocation_id,
                allocation["slot_host"],
            ):
                raise StorageConflict(ConflictCode.HOST_IDENTITY_CONFLICT)
            await self._check_profile_available(conn, allocation.get("deployment_profile", {}))
            allocation_id = str(uuid4())
            await conn.execute(
                """INSERT INTO agent_compute_allocations
                (id,name,provider_id,connection_id,ssh_connection_id,mode,management_mode,
                 configuration,deployment_profile,policy,lease_managed,release_supported,
                 slot_host,slot_namespace,predecessor_id)
                VALUES ($1,$2,$3,$4,$5,'provision','manage',$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12,$13)""",
                allocation_id,
                allocation["name"],
                allocation["provider_id"],
                allocation.get("connection_id"),
                allocation.get("ssh_connection_id"),
                _json(allocation["configuration"]),
                _json(allocation["deployment_profile"]),
                _json(allocation.get("policy", {})),
                allocation.get("lease_managed", False),
                allocation.get("release_supported", False),
                allocation["slot_host"],
                allocation["slot_namespace"],
                source_allocation_id,
            )
            return await self._insert_operation(
                conn,
                request_id=request_id,
                fingerprint=fingerprint,
                kind="acquire",
                allocation_id=allocation_id,
                parameters=parameters,
                requested_by="compute-rotation",
            )

    async def abandon_candidate(
        self,
        *,
        allocation_id: str,
        request_id: str,
        fingerprint: str,
        operation_id: str,
        generation: int,
        worker_id: str,
        refused_acquisition_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Stop a replacement node's pending work and queue its release.

        Returns the release operation, or None while the candidate's own work
        cannot be stopped safely (``candidate_work_stoppable``).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id)
            rotation = await conn.fetchval(
                "SELECT allocation_id FROM agent_compute_operations WHERE id=$1", operation_id
            )
            candidate = await conn.fetchrow(
                "SELECT predecessor_id FROM agent_compute_allocations WHERE id=$1 FOR UPDATE",
                allocation_id,
            )
            if candidate is None or candidate["predecessor_id"] != rotation:
                raise StorageConflict(ConflictCode.CANDIDATE_MISMATCH)
            existing = await self._existing_request(conn, request_id, fingerprint)
            if existing is not None:
                return existing
            if not await candidate_work_stoppable(conn, allocation_id, refused_acquisition_id):
                return None
            await conn.execute(
                """UPDATE agent_compute_operations
                SET state='cancelled',message='candidate_abandoned',completed_at=NOW(),updated_at=NOW()
                WHERE allocation_id=$1 AND state IN ('pending','waiting','blocked')""",
                allocation_id,
            )
            return await self._insert_operation(
                conn,
                request_id=request_id,
                fingerprint=fingerprint,
                kind="discard",
                allocation_id=allocation_id,
                parameters={"allocation_id": allocation_id},
                requested_by="compute-rotation",
            )

    async def commit_candidate_release(
        self, *, allocation_id: str, operation_id: str, generation: int, worker_id: str
    ) -> dict[str, Any]:
        """Commit the release of a replacement node that never took its Host over.

        It holds only a copy of a Host that still runs elsewhere, so release
        needs no retained-state inventory — but only while no Host is bound to
        its nodes. Once a Host is bound, the normal guarded retirement applies.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            candidate = await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE", allocation_id
            )
            if candidate is None:
                raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
            if candidate["release_committed_at"] is not None:
                return {"release_committed": True, "blocked_reasons": []}
            if candidate["predecessor_id"] is None:
                raise StorageConflict(ConflictCode.NOT_A_CANDIDATE)
            if await conn.fetchval(
                """SELECT 1 FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                WHERE n.allocation_id=$1""",
                allocation_id,
            ):
                raise StorageConflict(ConflictCode.CANDIDATE_BOUND)
            if await conn.fetchval(
                """SELECT 1 FROM agent_compute_operations
                WHERE allocation_id=$1 AND id<>$2 AND (state IN ('running','unknown')
                    OR (state='waiting' AND result->>'external_pending'='true')) LIMIT 1""",
                allocation_id,
                operation_id,
            ):
                return {
                    "release_committed": False,
                    "blocked_reasons": ["external_operation_unresolved"],
                }
            await conn.execute(
                """UPDATE agent_compute_allocations
                SET desired_state='absent',release_committed_at=NOW(),revision=revision+1,updated_at=NOW()
                WHERE id=$1""",
                allocation_id,
            )
            await conn.execute(
                """UPDATE agent_compute_operations SET state='cancelled',message='release_committed',completed_at=NOW()
                WHERE allocation_id=$1 AND id<>$2 AND state IN ('pending','waiting','blocked')""",
                allocation_id,
                operation_id,
            )
            return {"release_committed": True, "blocked_reasons": []}

    async def set_rotation_hold(
        self,
        *,
        host: str,
        allocation_id: str,
        source_id: str,
        scope: str | None,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Pause, or with no scope resume, claims on a Host while it changes machine.

        Keyed by the Host name: the hold moves with the Host between allocations.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id)
            if scope is None:
                await conn.execute(
                    "DELETE FROM agent_host_admission_holds WHERE host=$1 AND purpose='rotation' AND source_id=$2",
                    host,
                    source_id,
                )
                return
            await conn.execute(
                """INSERT INTO agent_host_admission_holds(host,allocation_id,purpose,source_id,scope)
                VALUES ($1,$2,'rotation',$3,$4)
                ON CONFLICT(host,purpose,source_id) DO UPDATE SET scope=EXCLUDED.scope,
                    revision=agent_host_admission_holds.revision+1""",
                host,
                allocation_id,
                source_id,
                scope,
            )

    async def rebind_slot_host(
        self,
        *,
        host: str,
        from_node_id: str,
        to_node_id: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Hand a Host name from one node to the node that now holds its state.

        The one deliberate exception to "a Host name never changes node": the
        caller copied the Host's complete container state to the target and
        keeps claims paused. Only a rotation's own successor (or, to roll back,
        its predecessor) may take the name. The previous node's lease, identity
        and bootstrap holds stop applying; the rotation hold follows the Host.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id)
            current = await conn.fetchrow(
                "SELECT compute_node_id FROM agent_runner_hosts WHERE host=$1 FOR UPDATE", host
            )
            if current is None:
                raise StorageConflict(ConflictCode.HOST_NOT_FOUND)
            if current["compute_node_id"] == to_node_id:
                return  # Replayed after a lost acknowledgement.
            if current["compute_node_id"] != from_node_id:
                raise StorageConflict(ConflictCode.HOST_IDENTITY_CONFLICT)
            source = await conn.fetchrow(
                """SELECT a.id,a.predecessor_id FROM agent_compute_nodes n
                JOIN agent_compute_allocations a ON a.id=n.allocation_id WHERE n.id=$1""",
                from_node_id,
            )
            target = await conn.fetchrow(
                """SELECT a.id,a.predecessor_id,a.desired_state,a.release_committed_at
                FROM agent_compute_nodes n JOIN agent_compute_allocations a ON a.id=n.allocation_id
                WHERE n.id=$1 FOR UPDATE OF a""",
                to_node_id,
            )
            if (
                source is None
                or target is None
                or target["desired_state"] != "present"
                or target["release_committed_at"] is not None
            ):
                raise StorageConflict(ConflictCode.RESOURCE_RETIRING)
            if (
                target["predecessor_id"] != source["id"]
                and source["predecessor_id"] != target["id"]
            ):
                raise StorageConflict(ConflictCode.HOST_IDENTITY_CONFLICT)
            if await conn.fetchval(
                "SELECT 1 FROM agent_runner_hosts WHERE compute_node_id=$1 AND host<>$2",
                to_node_id,
                host,
            ):
                raise StorageConflict(ConflictCode.HOST_IDENTITY_CONFLICT)
            await conn.execute(
                "UPDATE agent_runner_hosts SET compute_node_id=$2 WHERE host=$1", host, to_node_id
            )
            await conn.execute(
                "DELETE FROM agent_host_admission_holds WHERE host=$1 AND purpose<>'rotation'", host
            )
            await conn.execute(
                """UPDATE agent_host_admission_holds SET allocation_id=$2,revision=revision+1
                WHERE host=$1 AND purpose='rotation'""",
                host,
                target["id"],
            )
