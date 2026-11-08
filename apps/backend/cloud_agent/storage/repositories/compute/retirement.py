"""Compute allocation retirement and release."""

from __future__ import annotations

from typing import Any

from cloud_agent.domain.compute import provider_resource_absent
from cloud_agent.domain.sandbox_group import DeletionState
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.hosts import (
    HANDED_OVER_GROUP_SQL,
    lock_hosts_for_revocation,
    revoke_host_credentials,
)
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase


class RetirementMixin(ComputeRepositoryBase):
    """Retirement, its impact, and the release that commits it."""

    async def begin_retirement(
        self,
        *,
        allocation_id: str,
        operation_id: str,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Close new dependencies before observing retirement blockers."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            if generation is not None:
                await self._fence(conn, operation_id, generation, worker_id)
            await conn.execute(
                "UPDATE agent_compute_allocations SET desired_state='absent',inventory_verified_at=NULL,inventory_verified_nodes='[]'::jsonb,revision=revision+1,updated_at=NOW() WHERE id=$1 AND desired_state<>'absent'",
                allocation_id,
            )
            await conn.execute(
                """INSERT INTO agent_host_admission_holds(host,allocation_id,purpose,source_id,scope)
                SELECT h.host,$1,'retirement',$2,'all_claims' FROM agent_runner_hosts h
                JOIN agent_compute_nodes n ON n.id=h.compute_node_id WHERE n.allocation_id=$1
                ON CONFLICT(host,purpose,source_id) DO NOTHING""",
                allocation_id,
                operation_id,
            )
            return await self._retirement_impact(conn, allocation_id)

    async def _retirement_impact(self, conn: Any, allocation_id: str) -> dict[str, Any]:
        allocation = await self._allocation(conn, allocation_id)
        if allocation is None:
            raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
        hosts = [h["host"] for h in allocation["hosts"]]
        reasons: list[str] = []
        # A create that never received an experiment has no scope to adopt and
        # no observation that can become fresh. Those checks would wait forever.
        uncreated = provider_resource_absent(allocation)
        if allocation["management_mode"] != "manage" or (
            not uncreated and not allocation["scope_complete"]
        ):
            reasons.append("unmanaged_scope")
        if any(not n["managed"] for n in allocation["nodes"]):
            reasons.append("unmanaged_scope")
        active = await conn.fetchval("SELECT active_host FROM agent_runner_policy WHERE id")
        if active in hosts:
            reasons.append("placement_pinned")
        if any(host["drain_fence_active"] for host in allocation["hosts"]):
            reasons.append("deployment_drain_active")
        # A group handed over lives in its checkpoint, whatever host it still
        # names; the container it left here goes before the inventory below.
        groups = await conn.fetch(
            f"""SELECT g.id,g.host,g.state,g.active_operation_count,
                       {HANDED_OVER_GROUP_SQL} AS handed_over
                FROM agent_sandboxes g
                WHERE g.host=ANY($1::text[]) AND g.deletion_state<>{DeletionState.DELETED.sql}""",
            hosts,
        )
        retained = [g for g in groups if not g["handed_over"]]
        if retained:
            reasons.append("retained_groups_present")
        if any(g["active_operation_count"] for g in groups):
            reasons.append("workspace_operations_present")
        attempts = await conn.fetchval(
            "SELECT count(*) FROM agent_attempts WHERE host_id=ANY($1::text[]) AND status='running'",
            hosts,
        )
        if attempts:
            reasons.append("active_attempts_present")
        forks = await conn.fetchval(
            f"""SELECT count(*) FROM agent_session_forks f JOIN agent_sandboxes g ON g.id=f.sandbox_id
            WHERE g.host=ANY($1::text[]) AND g.deletion_state<>{DeletionState.DELETED.sql}
              AND f.state IN ('preparing','materialized')""",
            hosts,
        )
        if forks:
            reasons.append("forks_present")
        # Inventory verification comes from a trusted provider + sandbox manager scan,
        # not from heartbeat slot counts. Even zero registered hosts may hide
        # unadopted containers, so absence is not proof of an empty resource.
        now = await conn.fetchval("SELECT NOW()")
        verified = allocation["inventory_verified_at"]
        if not uncreated and (verified is None or (now - verified).total_seconds() > 120):
            reasons.append("inventory_unverified")
        if not uncreated and (
            allocation["observed_at"] is None
            or (now - allocation["observed_at"]).total_seconds() > 300
        ):
            reasons.append("observation_stale")
        ssh_verified = (
            set(allocation["inventory_verified_nodes"])
            if verified is not None and (now - verified).total_seconds() <= 120
            else set()
        )
        if any(
            h["compute_node_id"] not in ssh_verified
            and (
                h["last_heartbeat_at"] is None
                or (now - h["last_heartbeat_at"]).total_seconds() > 90
            )
            for h in allocation["hosts"]
        ):
            reasons.append("host_observation_stale")
        return {
            "allocation_id": allocation_id,
            "revision": allocation["revision"],
            "global_active_host": active if active in hosts else None,
            "management_mode": allocation["management_mode"],
            "scope_complete": allocation["scope_complete"],
            "provider_id": allocation["provider_id"],
            "deployment_profile_configured": bool(allocation["deployment_profile"]),
            "nodes": allocation["nodes"],
            "hosts": allocation["hosts"],
            "retained_groups": [
                {key: g[key] for key in ("id", "host", "state", "active_operation_count")}
                for g in retained
            ],
            "active_attempts": attempts,
            "active_forks": forks,
            "blocked_reasons": list(dict.fromkeys(reasons)),
            "release_committed": allocation["release_committed_at"] is not None,
        }

    async def retirement_impact(self, allocation_id: str) -> dict[str, Any]:
        """Read the complete allocation scope and retained-state blockers."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            return await self._retirement_impact(conn, allocation_id)

    async def commit_release(
        self, *, allocation_id: str, operation_id: str, generation: int, worker_id: str
    ) -> dict[str, Any]:
        """Commit release after validation, or settle one already terminal upstream."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            allocation = await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE", allocation_id
            )
            if allocation is None:
                raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
            if allocation["release_committed_at"] is not None:
                return {"release_committed": True, "blocked_reasons": []}
            # The rows the revocation below writes, then the host rows: a
            # provider-terminal release proceeds past attempts still running.
            await lock_hosts_for_revocation(
                conn,
                [
                    row["host"]
                    for row in await conn.fetch(
                        """SELECT h.host FROM agent_compute_nodes n
                           JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                           WHERE n.allocation_id=$1 ORDER BY h.host""",
                        allocation_id,
                    )
                ],
            )
            impact = await self._retirement_impact(conn, allocation_id)
            provider_terminal = allocation["desired_state"] == "absent" and allocation[
                "observed_state"
            ] in {"terminated", "absent"}
            if provider_terminal:
                # The provider has already crossed the irreversible boundary.
                # Local retention can no longer preserve that Host, so revoke
                # its execution identity and settle the release immediately.
                impact["blocked_reasons"] = []
            else:
                if allocation["desired_state"] != "absent":
                    impact["blocked_reasons"].append("retirement_not_started")
                other = await conn.fetchval(
                    """SELECT 1 FROM agent_compute_operations
                    WHERE allocation_id=$1 AND id<>$2 AND (state IN ('running','unknown') OR (state='waiting' AND result->>'external_pending'='true')) LIMIT 1""",
                    allocation_id,
                    operation_id,
                )
                if other:
                    impact["blocked_reasons"].append("external_operation_unresolved")
                if impact["blocked_reasons"]:
                    return impact
            if provider_resource_absent(await self._allocation(conn, allocation_id)):
                # Stop a create that has not reached the provider. Retry after
                # this commit is already refused; cancelling it keeps a later
                # worker from posting an experiment for a released server.
                await conn.execute(
                    """UPDATE agent_compute_operations
                    SET state='cancelled',
                        message='released before a provider resource existed',
                        completed_at=NOW(),updated_at=NOW()
                    WHERE allocation_id=$1 AND id<>$2
                      AND kind IN ('acquire','import')
                      AND state IN ('pending','waiting','blocked')
                      AND COALESCE(result->>'external_pending','false') <> 'true'
                      AND NOT (result ? 'resource_ref')""",
                    allocation_id,
                    operation_id,
                )
            else:
                # An acquisition between its checks -- waiting for its Host's
                # first heartbeat, or blocked on a deployment given up on -- is
                # never claimed again after this commit. Left waiting, it kept
                # an autoscaled server's route reserved for good. The release
                # deletes the resource by the allocation's own identity.
                await conn.execute(
                    """UPDATE agent_compute_operations
                    SET state='cancelled',message='release_committed',
                        completed_at=NOW(),updated_at=NOW()
                    WHERE allocation_id=$1 AND id<>$2
                      AND kind IN ('acquire','import')
                      AND state IN ('pending','waiting','blocked')""",
                    allocation_id,
                    operation_id,
                )
            await conn.execute(
                "UPDATE agent_compute_allocations SET release_committed_at=NOW(),updated_at=NOW() WHERE id=$1",
                allocation_id,
            )
            await revoke_host_credentials(conn, [h["host"] for h in impact["hosts"]])
            # Later pending extensions must never escape after the commit. Nor
            # is an upgrade claimed after it (claim_operation), and one left
            # waiting would hold its deployment profile back from every later
            # host release, as if it were still being installed.
            await conn.execute(
                """UPDATE agent_compute_operations SET state='cancelled',message='release_committed',completed_at=NOW()
                WHERE allocation_id=$1 AND id<>$2 AND kind IN ('extend','upgrade') AND state IN ('pending','waiting','blocked')""",
                allocation_id,
                operation_id,
            )
            return {**impact, "release_committed": True}
