"""Compute allocation holds: bootstrap, admission and move-out."""

from __future__ import annotations

from typing import Any

from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.move_out import move_out_possible
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase


class HoldsMixin(ComputeRepositoryBase):
    """The holds that keep new work off an allocation."""

    async def prepare_bootstrap_binding(
        self,
        *,
        node_id: str,
        host: str,
        operation_id: str,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind before token issuance; no runner-supplied cloud identity is trusted."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            if generation is not None:
                await self._fence(conn, operation_id, generation, worker_id)
            node = await conn.fetchrow(
                """SELECT n.*,a.desired_state,
                          a.release_committed_at,a.observed_state,a.observation_error,
                          a.scope_complete,a.observed_at,
                          (a.observed_at >= NOW() - INTERVAL '5 minutes'
                           AND a.observed_at <= NOW() + INTERVAL '30 seconds') AS observation_fresh,
                          (SELECT COUNT(*) FROM agent_compute_nodes current_nodes
                           WHERE current_nodes.allocation_id=a.id) AS allocation_node_count
                FROM agent_compute_nodes n JOIN agent_compute_allocations a ON a.id=n.allocation_id
                WHERE n.id=$1 FOR UPDATE OF a""",
                node_id,
            )
            if node is None or node["desired_state"] != "present" or node["release_committed_at"]:
                raise StorageConflict(ConflictCode.RESOURCE_RETIRING)
            if (
                not node["scope_complete"]
                or node["observed_state"] not in {"ready", "active", "running"}
                or node["observation_error"] is not None
                or not node["observation_fresh"]
                or node["allocation_node_count"] != 1
            ):
                raise StorageConflict(ConflictCode.RESOURCE_OBSERVATION_NOT_READY)
            existing = await conn.fetchrow(
                "SELECT * FROM agent_runner_hosts WHERE host=$1 FOR UPDATE", host
            )
            if existing is not None and existing["compute_node_id"] != node_id:
                # Even an idle unbound Host may own stopped groups. Adoption is
                # an explicit verified workflow, never a hostname collision.
                raise StorageConflict(ConflictCode.HOST_IDENTITY_CONFLICT)
            if existing is None:
                await conn.execute(
                    "INSERT INTO agent_runner_hosts(host,status,compute_node_id) VALUES ($1,'enrolling',$2)",
                    host,
                    node_id,
                )
            await conn.execute(
                """INSERT INTO agent_host_admission_holds
                (host,allocation_id,purpose,source_id,scope) VALUES ($1,$2,'bootstrap',$3,'all_claims')
                ON CONFLICT (host,purpose,source_id) DO NOTHING""",
                host,
                node["allocation_id"],
                operation_id,
            )
            await conn.execute(
                """INSERT INTO agent_host_admission_holds
                   (host,allocation_id,purpose,source_id,scope)
                   SELECT $1,$2,'autoscaling_slots',$2,'all_claims'
                   WHERE EXISTS (
                       SELECT 1 FROM agent_cloudlab_autoscaling_members
                       WHERE allocation_id=$2
                   )
                   ON CONFLICT (host,purpose,source_id) DO UPDATE
                   SET scope='all_claims',revision=agent_host_admission_holds.revision+1""",
                host,
                node["allocation_id"],
            )
            return {
                "host": host,
                "compute_node_id": node_id,
                "allocation_id": node["allocation_id"],
            }

    async def clear_bootstrap_hold(
        self,
        *,
        node_id: str,
        host: str,
        operation_id: str,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> bool:
        """Open an exact ready binding without clearing other restrictions."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            if generation is not None:
                await self._fence(conn, operation_id, generation, worker_id)
            # Readiness may only open the exact live binding, never undo a
            # retirement or a human's status choice.
            valid = await conn.fetchval(
                """SELECT 1 FROM agent_runner_hosts h
                JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                JOIN agent_compute_allocations a ON a.id=n.allocation_id
                WHERE h.host=$1 AND n.id=$2 AND a.desired_state='present'
                  AND a.release_committed_at IS NULL AND h.status='online'""",
                host,
                node_id,
            )
            if not valid:
                return False
            await conn.execute(
                "DELETE FROM agent_host_admission_holds WHERE host=$1 AND purpose='bootstrap' AND source_id=$2",
                host,
                operation_id,
            )
            return True

    async def set_admission_hold(
        self,
        *,
        allocation_id: str,
        purpose: str,
        source_id: str,
        scope: str | None,
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Controller-owned restrictions never mutate Host status or sibling holds."""
        if purpose == "autoscaling_slots" and scope is None:
            raise ValueError("autoscaling_slot_fence_required")
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            if scope is None:
                await conn.execute(
                    "DELETE FROM agent_host_admission_holds WHERE allocation_id=$1 AND purpose=$2 AND source_id=$3",
                    allocation_id,
                    purpose,
                    source_id,
                )
            else:
                await conn.execute(
                    """INSERT INTO agent_host_admission_holds(host,allocation_id,purpose,source_id,scope)
                    SELECT h.host,$1,$2,$3,$4 FROM agent_runner_hosts h
                    JOIN agent_compute_nodes n ON n.id=h.compute_node_id WHERE n.allocation_id=$1
                    ON CONFLICT(host,purpose,source_id) DO UPDATE
                    SET allocation_id=EXCLUDED.allocation_id,scope=EXCLUDED.scope,
                        revision=agent_host_admission_holds.revision+1
                    WHERE agent_host_admission_holds.scope IS DISTINCT FROM EXCLUDED.scope
                       OR agent_host_admission_holds.allocation_id
                          IS DISTINCT FROM EXCLUDED.allocation_id""",
                    allocation_id,
                    purpose,
                    source_id,
                    scope,
                )

    async def set_move_out_hold(
        self,
        *,
        allocation_id: str,
        due: bool,
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> bool:
        """Set or clear this allocation's ``move_out`` hold; return whether it holds.

        Inside its window (``due``) the Host holds every claim only while its
        groups can actually move: checkpoints are on and its Hosts capture
        them, and another Host would claim the groups now, by the claim's own
        admission rule. A started move continues without a target; it stops
        if checkpoints stop. The check and the write share one exclusive
        topology fence, so two Hosts entering their windows together cannot
        each count on the other and both stop taking work.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            moving = due and await self._move_out_possible(conn, allocation_id)
            if moving:
                await conn.execute(
                    """INSERT INTO agent_host_admission_holds(host,allocation_id,purpose,source_id,scope)
                    SELECT h.host,$1,'move_out',$1,'all_claims' FROM agent_runner_hosts h
                    JOIN agent_compute_nodes n ON n.id=h.compute_node_id WHERE n.allocation_id=$1
                    ON CONFLICT(host,purpose,source_id) DO NOTHING""",
                    allocation_id,
                )
            else:
                await conn.execute(
                    "DELETE FROM agent_host_admission_holds "
                    "WHERE allocation_id=$1 AND purpose='move_out' AND source_id=$1",
                    allocation_id,
                )
            return moving

    @staticmethod
    async def _move_out_possible(conn: Any, allocation_id: str) -> bool:
        hosts = [
            row["host"]
            for row in await conn.fetch(
                """SELECT h.host FROM agent_compute_nodes n
                   JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                   WHERE n.allocation_id=$1 ORDER BY h.host""",
                allocation_id,
            )
        ]
        started = bool(
            await conn.fetchval(
                "SELECT 1 FROM agent_host_admission_holds "
                "WHERE allocation_id=$1 AND purpose='move_out' LIMIT 1",
                allocation_id,
            )
        )
        # A started move continues without a target, but never once its checkpoints stop.
        return await move_out_possible(conn, hosts, need_target=not started)
