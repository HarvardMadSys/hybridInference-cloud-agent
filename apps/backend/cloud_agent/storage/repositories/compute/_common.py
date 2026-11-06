"""State and steps the compute aggregates share.

The pool, the row decoding, and the private steps more than one aggregate
takes inside its own transaction: fencing a worker lease, recording an
operation intent, reading an allocation, and settling an autoscaling
reservation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.domain.sandbox_group import DeletionState
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql import environment_runs
from cloud_agent.storage._sql.hosts import lock_hosts_for_revocation, revoke_host_credentials
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg

_JSON_FIELDS = {
    "configuration",
    "policy",
    "deployment_profile",
    "parameters",
    "result",
    "connection",
    "renewal_policy",
    "slot_policy",
    "profile_snapshot",
    "last_decision",
}
_JSON_ARRAY_FIELDS = {"slot_status"}


def _row(row: Any) -> dict[str, Any]:
    if row is None:
        raise StorageConflict(ConflictCode.RECORD_NOT_FOUND)
    result = dict(row)
    for field in _JSON_FIELDS & result.keys():
        result[field] = load_json(result[field]) or {}
    for field in _JSON_ARRAY_FIELDS & result.keys():
        result[field] = load_json(result[field]) or []
    if "inventory_verified_nodes" in result:
        result["inventory_verified_nodes"] = load_json(result["inventory_verified_nodes"]) or []
    return result


def _optional_row(row: Any) -> dict[str, Any] | None:
    return None if row is None else _row(row)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class ComputeRepositoryBase:
    """The pool and the private steps every compute aggregate builds on."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def _existing_request(
        self, conn: Any, request_id: str, fingerprint: str
    ) -> dict[str, Any] | None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "compute-request:" + request_id
        )
        existing = await conn.fetchrow(
            "SELECT * FROM agent_compute_operations WHERE request_id=$1", request_id
        )
        if existing is not None and existing["fingerprint"] != fingerprint:
            raise StorageConflict(ConflictCode.IDEMPOTENCY_CONFLICT)
        return _optional_row(existing)

    async def _insert_operation(
        self,
        conn: Any,
        *,
        request_id: str,
        fingerprint: str,
        kind: str,
        allocation_id: str | None,
        parameters: dict[str, Any],
        requested_by: str | None,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """INSERT INTO agent_compute_operations
            (id,request_id,fingerprint,kind,allocation_id,parameters,requested_by)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7) RETURNING *""",
            str(uuid4()),
            request_id,
            fingerprint,
            kind,
            allocation_id,
            _json(parameters),
            requested_by,
        )
        return _row(row)

    async def _check_profile_available(self, conn: Any, profile: dict[str, Any]) -> None:
        profile_id = profile.get("id")
        if profile_id and await conn.fetchval(
            """SELECT 1 FROM agent_cloudlab_autoscaling_profile_reservations
               WHERE deployment_profile=$1""",
            profile_id,
        ):
            raise StorageConflict(ConflictCode.DEPLOYMENT_PROFILE_RESERVED)
        if profile.get("id") and await conn.fetchval(
            """SELECT 1 FROM agent_compute_allocations
            WHERE deployment_profile->>'id'=$1 AND observed_state NOT IN ('terminated','absent') LIMIT 1""",
            profile["id"],
        ):
            raise StorageConflict(ConflictCode.DEPLOYMENT_PROFILE_IN_USE)

    async def _allocation(self, conn: Any, allocation_id: str) -> dict[str, Any] | None:
        result = _optional_row(
            await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1", allocation_id
            )
        )
        if result is not None:
            member_state = await conn.fetchval(
                "SELECT state FROM agent_cloudlab_autoscaling_members WHERE allocation_id=$1",
                allocation_id,
            )
            result["autoscaling_owned"] = member_state is not None
            result["autoscaling_member_state"] = member_state
            result["nodes"] = [
                _row(r)
                for r in await conn.fetch(
                    "SELECT * FROM agent_compute_nodes WHERE allocation_id=$1 ORDER BY id",
                    allocation_id,
                )
            ]
            result["hosts"] = [
                dict(r)
                for r in await conn.fetch(
                    f"""SELECT h.host,h.status,h.compute_node_id,
                (h.slots_used+{environment_runs.env_runs_holding_slots("h.host")}) AS slots_used,h.last_heartbeat_at,h.broker_url AS sandbox_manager_url,h.drain_fence_active,
                (SELECT count(*) FROM agent_sandboxes g
                 WHERE g.host=h.host AND g.deletion_state<>{DeletionState.DELETED.sql}) AS retained_groups
                FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                WHERE n.allocation_id=$1 ORDER BY h.host""",
                    allocation_id,
                )
            ]
            result["holds"] = [
                dict(r)
                for r in await conn.fetch(
                    "SELECT * FROM agent_host_admission_holds WHERE allocation_id=$1 ORDER BY host,purpose",
                    allocation_id,
                )
            ]
            result["rotation"] = _optional_row(
                await conn.fetchrow(
                    """SELECT id,state,step,message,result,created_at,updated_at,completed_at
                    FROM agent_compute_operations WHERE allocation_id=$1 AND kind='rotate'
                    ORDER BY created_at DESC LIMIT 1""",
                    allocation_id,
                )
            )
        return result

    async def _fence(
        self,
        conn: Any,
        operation_id: str | None,
        generation: int | None,
        worker_id: str | None,
        allocation_id: str | None = None,
    ) -> None:
        if operation_id is None:
            return
        locked = await conn.fetchval(
            """SELECT id FROM agent_compute_operations
            WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
              AND ($4::text IS NULL OR allocation_id=$4) FOR UPDATE""",
            operation_id,
            generation,
            worker_id,
            allocation_id,
        )
        if locked is None or not await conn.fetchval(
            "SELECT lease_expires_at > clock_timestamp() FROM agent_compute_operations WHERE id=$1",
            operation_id,
        ):
            raise StorageConflict(ConflictCode.OPERATION_LEASE_LOST)

    async def _release_terminal_autoscaling_reservation(
        self, conn: Any, allocation_id: str
    ) -> bool:
        """Fence a terminal owned Host before recycling its deployment profile."""
        allocation = await conn.fetchrow(
            """SELECT a.observed_state,a.desired_state,a.release_committed_at
               FROM agent_compute_allocations a
               JOIN agent_cloudlab_autoscaling_members m ON m.allocation_id=a.id
               WHERE a.id=$1 FOR UPDATE OF a,m""",
            allocation_id,
        )
        if allocation is None or allocation["observed_state"] not in {"terminated", "absent"}:
            return False
        if await conn.fetchval(
            """SELECT 1 FROM agent_compute_operations
               WHERE allocation_id=$1 AND kind IN ('acquire','retire','discard')
                 AND state IN ('pending','running','waiting')
               LIMIT 1""",
            allocation_id,
        ):
            return False
        # Read under the exclusive compute fence, which every binding change
        # takes; the revocation's rows are locked before the host rows.
        hosts = [
            row["host"]
            for row in await conn.fetch(
                """SELECT h.host FROM agent_compute_nodes n
                   JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                   WHERE n.allocation_id=$1 ORDER BY h.host""",
                allocation_id,
            )
        ]
        await lock_hosts_for_revocation(conn, hosts)
        await conn.execute(
            """UPDATE agent_compute_allocations
               SET desired_state='absent',
                   release_committed_at=COALESCE(release_committed_at,NOW()),
                   revision=revision+CASE
                       WHEN desired_state<>'absent' OR release_committed_at IS NULL THEN 1 ELSE 0
                   END,
                   updated_at=NOW()
               WHERE id=$1""",
            allocation_id,
        )
        await revoke_host_credentials(conn, hosts)
        await conn.execute(
            """UPDATE agent_compute_operations
               SET state='cancelled',message='release_committed',completed_at=NOW(),updated_at=NOW()
               WHERE allocation_id=$1 AND kind='extend'
                 AND state IN ('pending','waiting','blocked')""",
            allocation_id,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_members
               SET state='released',updated_at=NOW()
               WHERE allocation_id=$1""",
            allocation_id,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_profile_reservations
               SET allocation_id=NULL,updated_at=NOW()
               WHERE allocation_id=$1""",
            allocation_id,
        )
        await conn.execute(
            """DELETE FROM agent_cloudlab_autoscaling_profile_reservations r
               USING agent_cloudlab_autoscaling_policy p
               WHERE p.id AND r.allocation_id IS NULL
                 AND NOT (r.deployment_profile=ANY(p.deployment_profiles))"""
        )
        return True
