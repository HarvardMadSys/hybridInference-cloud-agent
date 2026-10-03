"""Postgres compute inventory and recoverable operations.

All provider/SSH calls belong to the application layer. Every external result
may be fenced against its worker lease, and every destructive decision is
rechecked in the same transaction as its irreversible commit point.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.storage._sql.compute_admission import host_compute_admission, lock_compute_entry
from cloud_agent.storage._sql.hosts import revoke_host_credentials
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
}


def _row(row: Any) -> dict[str, Any]:
    if row is None:
        raise ValueError("record_not_found")
    result = dict(row)
    for field in _JSON_FIELDS & result.keys():
        result[field] = load_json(result[field]) or {}
    if "inventory_verified_nodes" in result:
        result["inventory_verified_nodes"] = load_json(result["inventory_verified_nodes"]) or []
    return result


def _optional_row(row: Any) -> dict[str, Any] | None:
    return None if row is None else _row(row)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class PostgresComputeResourceRepository:
    """One durable identity for each resource instance and operation intent."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_connections(self) -> list[dict[str, Any]]:
        """Return metadata only; ciphertext is restricted to trusted get_connection."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""SELECT id,name,provider_id,configuration,masked_summary,
                status,revision,observed_at,created_at,updated_at FROM agent_compute_connections
                ORDER BY created_at""")
        return [_row(row) for row in rows]

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        """Read the trusted connection record, including encrypted secret material."""
        async with self._pool.acquire() as conn:
            return _optional_row(
                await conn.fetchrow(
                    "SELECT * FROM agent_compute_connections WHERE id=$1", connection_id
                )
            )

    async def save_connection(
        self,
        *,
        connection_id: str,
        name: str,
        provider_id: str,
        configuration: dict[str, Any],
        secret_ciphertext: str,
        masked_summary: str,
        status: str = "unverified",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Create or compare-and-swap encrypted connection metadata."""
        async with self._pool.acquire() as conn, conn.transaction():
            if expected_revision is None:
                row = await conn.fetchrow(
                    """INSERT INTO agent_compute_connections
                    (id,name,provider_id,configuration,secret_ciphertext,masked_summary,status)
                    VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7) ON CONFLICT (id) DO NOTHING RETURNING *""",
                    connection_id,
                    name,
                    provider_id,
                    _json(configuration),
                    secret_ciphertext,
                    masked_summary,
                    status,
                )
            else:
                row = await conn.fetchrow(
                    """UPDATE agent_compute_connections
                    SET name=$2,configuration=$4::jsonb,secret_ciphertext=$5,masked_summary=$6,
                        status=$7,revision=revision+1,updated_at=NOW(),
                        observed_at=CASE WHEN $7='verified' THEN NOW() ELSE observed_at END
                    WHERE id=$1 AND provider_id=$3 AND revision=$8 RETURNING *""",
                    connection_id,
                    name,
                    provider_id,
                    _json(configuration),
                    secret_ciphertext,
                    masked_summary,
                    status,
                    expected_revision,
                )
            if row is None:
                raise ValueError("revision_conflict")
            return _row(row)

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
            raise ValueError("idempotency_conflict")
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
        if profile.get("id") and await conn.fetchval(
            """SELECT 1 FROM agent_compute_allocations
            WHERE deployment_profile->>'id'=$1 AND observed_state NOT IN ('terminated','absent') LIMIT 1""",
            profile["id"],
        ):
            raise ValueError("deployment_profile_in_use")

    async def create_allocation_operation(
        self,
        *,
        request_id: str,
        fingerprint: str,
        kind: str,
        allocation: dict[str, Any],
        parameters: dict[str, Any],
        requested_by: str | None,
    ) -> dict[str, Any]:
        """Atomically persist resource identity and its first operation intent."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            existing = await self._existing_request(conn, request_id, fingerprint)
            if existing is not None:
                return existing
            external_id = allocation.get("external_id")
            reused = None
            if external_id:
                reused = await conn.fetchrow(
                    """SELECT * FROM agent_compute_allocations
                    WHERE provider_id=$1 AND external_id=$2 FOR UPDATE""",
                    allocation["provider_id"],
                    external_id,
                )
            if reused is not None:
                if (
                    reused["connection_id"] != allocation.get("connection_id")
                    or reused["desired_state"] != "present"
                ):
                    raise ValueError("resource_already_managed")
                if kind != "import":
                    occupied = await conn.fetchval(
                        """SELECT 1 FROM agent_runner_hosts h
                        JOIN agent_compute_nodes n ON n.id=h.compute_node_id WHERE n.allocation_id=$1
                        UNION ALL SELECT 1 FROM agent_compute_operations WHERE allocation_id=$1
                            AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1""",
                        reused["id"],
                    )
                    if occupied or load_json(reused["deployment_profile"]):
                        raise ValueError("resource_already_managed")
                    await self._check_profile_available(
                        conn, allocation.get("deployment_profile", {})
                    )
                    await conn.execute(
                        """UPDATE agent_compute_allocations
                        SET name=$2,ssh_connection_id=$3,configuration=$4::jsonb,
                            deployment_profile=$5::jsonb,policy=$6::jsonb,revision=revision+1,updated_at=NOW()
                        WHERE id=$1""",
                        reused["id"],
                        allocation["name"],
                        allocation.get("ssh_connection_id"),
                        _json(allocation.get("configuration", {})),
                        _json(allocation.get("deployment_profile", {})),
                        _json(allocation.get("policy", {})),
                    )
                return await self._insert_operation(
                    conn,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    kind=kind,
                    allocation_id=reused["id"],
                    parameters=parameters,
                    requested_by=requested_by,
                )
            await self._check_profile_available(conn, allocation.get("deployment_profile", {}))
            allocation_id = allocation.get("id") or str(uuid4())
            await conn.execute(
                """INSERT INTO agent_compute_allocations
                (id,name,provider_id,connection_id,ssh_connection_id,external_id,mode,
                 management_mode,configuration,deployment_profile,policy,lease_managed,release_supported)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11::jsonb,$12,$13)""",
                allocation_id,
                allocation["name"],
                allocation["provider_id"],
                allocation.get("connection_id"),
                allocation.get("ssh_connection_id"),
                allocation.get("external_id"),
                allocation["mode"],
                allocation.get("management_mode", "observe"),
                _json(allocation.get("configuration", {})),
                _json(allocation.get("deployment_profile", {})),
                _json(allocation.get("policy", {})),
                allocation.get("lease_managed", False),
                allocation.get("release_supported", False),
            )
            return await self._insert_operation(
                conn,
                request_id=request_id,
                fingerprint=fingerprint,
                kind=kind,
                allocation_id=allocation_id,
                parameters=parameters,
                requested_by=requested_by,
            )

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
                    raise ValueError("allocation_not_found")
                expected_revision = parameters.get("expected_revision")
                if (
                    kind == "retire"
                    and expected_revision is not None
                    and allocation["revision"] != expected_revision
                ):
                    raise ValueError("revision_conflict")
                if allocation["release_committed_at"] is not None:
                    raise ValueError("release_committed")
            return await self._insert_operation(
                conn,
                request_id=request_id,
                fingerprint=fingerprint,
                kind=kind,
                allocation_id=allocation_id,
                parameters=parameters,
                requested_by=requested_by,
            )

    async def get_allocation(self, allocation_id: str) -> dict[str, Any] | None:
        """Read one allocation and its stable node and Host bindings."""
        async with self._pool.acquire() as conn:
            return await self._allocation(conn, allocation_id)

    async def _allocation(self, conn: Any, allocation_id: str) -> dict[str, Any] | None:
        result = _optional_row(
            await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1", allocation_id
            )
        )
        if result is not None:
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
                    """SELECT h.host,h.status,h.compute_node_id,
                h.slots_used,h.last_heartbeat_at,h.broker_url,
                (SELECT count(*) FROM agent_sandboxes g WHERE g.host=h.host) AS retained_groups
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
        return result

    async def list_allocations(self) -> list[dict[str, Any]]:
        """Read all managed inventory from one local snapshot."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            ids = await conn.fetch("SELECT id FROM agent_compute_allocations ORDER BY created_at")
            results = [await self._allocation(conn, r["id"]) for r in ids]
            return [r for r in results if r is not None]

    async def server_inventory(self) -> dict[str, Any]:
        """Read global placement and resource/Host distribution in one snapshot."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            allocations = [
                await self._allocation(conn, r["id"])
                for r in await conn.fetch(
                    "SELECT id FROM agent_compute_allocations ORDER BY created_at"
                )
            ]
            hosts = [
                dict(r)
                for r in await conn.fetch("""SELECT h.host,h.status,h.compute_node_id,
                n.allocation_id,h.slots_used,h.slots_total,h.slots_desired,h.last_heartbeat_at,
                h.labels->>'physical_host' AS physical_host,h.broker_url,
                (SELECT count(*) FROM agent_sandboxes g WHERE g.host=h.host) AS retained_groups
                FROM agent_runner_hosts h LEFT JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                ORDER BY h.host""")
            ]
            for host in hosts:
                new, claims = await host_compute_admission(conn, host["host"])
                host["allows_new_groups"] = new
                host["allows_claims"] = claims
            return {
                "allocations": allocations,
                "hosts": hosts,
                "active_host": await conn.fetchval(
                    "SELECT active_host FROM agent_runner_policy WHERE id"
                ),
                "observed_at": await conn.fetchval("SELECT NOW()"),
            }

    async def record_observation_error(
        self,
        *,
        allocation_id: str,
        message: str,
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        """A read failure preserves the last measured deadline and its age."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            await conn.execute(
                "UPDATE agent_compute_allocations SET observation_error=$2,updated_at=NOW() WHERE id=$1",
                allocation_id,
                message,
            )

    async def record_inventory(
        self,
        *,
        allocation_id: str,
        hosts: list[str],
        empty: bool,
        verified_node_ids: list[str] | None = None,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> bool:
        """Accept a trusted broker scan only against the exact fenced topology.

        Retirement closes bindings first. A scan must cover every node and its
        Host execution environments; an unbound node is unknown, never empty.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            allocation = await self._allocation(conn, allocation_id)
            if allocation is None or allocation["desired_state"] != "absent" or not empty:
                return False
            current = allocation["hosts"]
            covered = {h["compute_node_id"] for h in current}
            if set(hosts) != {h["host"] for h in current} or not allocation["nodes"]:
                return False
            if verified_node_ids is not None:
                covered.update(verified_node_ids)
            if covered != {n["id"] for n in allocation["nodes"]}:
                return False
            await conn.execute(
                "UPDATE agent_compute_allocations SET inventory_verified_at=NOW(),inventory_verified_nodes=$2::jsonb,updated_at=NOW() WHERE id=$1",
                allocation_id,
                _json(verified_node_ids or []),
            )
            return True

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Read the durable state and recovery receipt of one operation."""
        async with self._pool.acquire() as conn:
            return _optional_row(
                await conn.fetchrow(
                    "SELECT * FROM agent_compute_operations WHERE id=$1", operation_id
                )
            )

    async def list_operations(
        self, *, allocation_id: str | None = None, request_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Find recent operation state, including recovery by request ID."""
        async with self._pool.acquire() as conn:
            return [
                _row(r)
                for r in await conn.fetch(
                    """SELECT * FROM agent_compute_operations
                WHERE ($1::text IS NULL OR allocation_id=$1) AND ($2::text IS NULL OR request_id=$2)
                ORDER BY created_at DESC LIMIT 200""",
                    allocation_id,
                    request_id,
                )
            ]

    async def update_policy(
        self,
        *,
        allocation_id: str,
        expected_revision: int,
        policy: dict[str, Any],
        management_mode: str | None = None,
    ) -> dict[str, Any]:
        """Compare-and-swap policy without bypassing a committed release."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            row = await conn.fetchrow(
                """UPDATE agent_compute_allocations
                SET policy=$3::jsonb,management_mode=COALESCE($4,management_mode),revision=revision+1,updated_at=NOW()
                WHERE id=$1 AND revision=$2 AND release_committed_at IS NULL RETURNING *""",
                allocation_id,
                expected_revision,
                _json(policy),
                management_mode,
            )
            if row is None:
                raise ValueError("revision_conflict")
            return _row(row)

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
        valid = await conn.fetchval(
            """SELECT id FROM agent_compute_operations
            WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
              AND lease_expires_at>NOW() AND ($4::text IS NULL OR allocation_id=$4) FOR UPDATE""",
            operation_id,
            generation,
            worker_id,
            allocation_id,
        )
        if valid is None:
            raise ValueError("operation_lease_lost")

    async def observe_allocation(
        self,
        *,
        allocation_id: str,
        observation: dict[str, Any],
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a fenced observation, preserving a known deadline when renewal is unknown."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            allocation = await conn.fetchrow(
                "SELECT external_id,desired_state,release_committed_at FROM agent_compute_allocations WHERE id=$1 FOR UPDATE",
                allocation_id,
            )
            if allocation is None:
                raise ValueError("allocation_not_found")
            current_identity = allocation["external_id"]
            if current_identity is not None and observation.get("external_id") not in (
                None,
                current_identity,
            ):
                raise ValueError("resource_identity_changed")
            nodes = observation.get("nodes")
            if nodes is not None and observation.get("observed_state") not in (
                "terminated",
                "absent",
            ):
                existing_nodes = {
                    r["provider_node_id"]: r["instance_identity"]
                    for r in await conn.fetch(
                        "SELECT provider_node_id,instance_identity FROM agent_compute_nodes WHERE allocation_id=$1",
                        allocation_id,
                    )
                }
                observed_nodes = {n["provider_node_id"]: n["instance_identity"] for n in nodes}
                if len(observed_nodes) != len(nodes):
                    raise ValueError("resource_identity_changed")
                changed = any(
                    key in observed_nodes and observed_nodes[key] != identity
                    for key, identity in existing_nodes.items()
                )
                missing = (
                    observation.get("scope_complete")
                    and not existing_nodes.keys() <= observed_nodes.keys()
                )
                retiring = allocation["desired_state"] != "present"
                committed_pending = (
                    allocation["release_committed_at"] is not None
                    and observation.get("observed_state") == "pending"
                    and not observation.get("scope_complete")
                )
                if (
                    changed
                    or missing
                    or (
                        retiring
                        and not (
                            committed_pending and observed_nodes.items() <= existing_nodes.items()
                        )
                        and (
                            not observation.get("scope_complete")
                            or existing_nodes != observed_nodes
                        )
                    )
                ):
                    raise ValueError("resource_identity_changed")
                # Node identity and the allocation's readiness/lease form one
                # snapshot. Losing a worker between these writes rolls back
                # everything instead of publishing fresh readiness first.
                # After release commits, a disappearing manifest is expected;
                # outcome polling must preserve the last verified bindings.
                if allocation["release_committed_at"] is None:
                    for node in nodes:
                        await self._upsert_node(conn, allocation_id, node)
            row = await conn.fetchrow(
                """UPDATE agent_compute_allocations SET
                external_id=COALESCE($2,external_id),observed_state=$3,provider_state=$4,
                expires_at=CASE WHEN NOT $6 AND $5::timestamptz IS NULL THEN expires_at ELSE $5 END,
                expiration_known=$6,observed_at=NOW(),observation_error=$7,
                scope_complete=$8,configuration=COALESCE($9::jsonb,configuration),
                updated_at=NOW() WHERE id=$1 RETURNING *""",
                allocation_id,
                observation.get("external_id"),
                observation.get("observed_state", "unknown"),
                observation.get("provider_state"),
                observation.get("expires_at"),
                observation.get("expiration_known", False),
                observation.get("observation_error"),
                observation.get("scope_complete", False),
                _json(observation["configuration"]) if "configuration" in observation else None,
            )
            if row is None:
                raise ValueError("allocation_not_found")
            return _row(row)

    async def upsert_node(
        self,
        *,
        allocation_id: str,
        node: dict[str, Any],
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Observe an immutable node identity without rebinding retained execution state."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            allocation = await conn.fetchrow(
                "SELECT desired_state FROM agent_compute_allocations WHERE id=$1 FOR UPDATE",
                allocation_id,
            )
            if allocation is None or allocation["desired_state"] != "present":
                raise ValueError("resource_retiring")
            return await self._upsert_node(conn, allocation_id, node)

    async def _upsert_node(
        self, conn: Any, allocation_id: str, node: dict[str, Any]
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """INSERT INTO agent_compute_nodes
            (id,allocation_id,provider_node_id,instance_identity,name,connection,managed)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
            ON CONFLICT (allocation_id,provider_node_id) DO UPDATE
            SET name=EXCLUDED.name,connection=EXCLUDED.connection,updated_at=NOW()
            WHERE agent_compute_nodes.instance_identity=EXCLUDED.instance_identity RETURNING *""",
            node.get("id") or str(uuid4()),
            allocation_id,
            node["provider_node_id"],
            node["instance_identity"],
            node["name"],
            _json(node.get("connection", {})),
            node.get("managed", True),
        )
        if row is None:
            raise ValueError("resource_identity_changed")
        return _row(row)

    async def claim_operation(
        self, *, worker_id: str, lease_seconds: float = 120
    ) -> dict[str, Any] | None:
        """Claim one allocation at a time, recovering an expired call before newer intents.

        An expired call retains its step/result for query recovery. The lease
        cannot cancel a remote request; adapters must never blindly replay it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Serialize the short dispatcher decision. Provider calls are outside
            # this transaction; a blocked retirement relinquishes its lease.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('compute-dispatch',0))"
            )
            row = await conn.fetchrow("""SELECT o.* FROM agent_compute_operations o
                LEFT JOIN agent_compute_allocations a ON a.id=o.allocation_id
                WHERE ((o.state IN ('pending','waiting') AND o.next_attempt_at<=NOW())
                       OR (o.state='running' AND o.lease_expires_at<=NOW()))
                  AND (a.release_committed_at IS NULL OR o.kind IN ('retire','inspect'))
                  AND NOT EXISTS (SELECT 1 FROM agent_compute_operations busy
                    WHERE busy.allocation_id=o.allocation_id AND busy.id<>o.id
                      AND (busy.state='running' OR busy.state='unknown' OR (busy.state='waiting' AND busy.result->>'external_pending'='true'))
                      AND NOT (busy.state='running' AND busy.lease_expires_at<=NOW()
                               AND o.state='running' AND o.created_at<busy.created_at))
                ORDER BY (o.state='running') DESC,o.created_at LIMIT 1 FOR UPDATE OF o SKIP LOCKED""")
            if row is None:
                return None
            return _row(
                await conn.fetchrow(
                    """UPDATE agent_compute_operations
                SET state='running',worker_id=$2,generation=generation+1,
                    lease_expires_at=NOW()+make_interval(secs=>$3),updated_at=NOW()
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
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """UPDATE agent_compute_operations
                SET lease_expires_at=NOW()+make_interval(secs=>$4),updated_at=NOW()
                WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
                  AND lease_expires_at>NOW() RETURNING id""",
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
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """UPDATE agent_compute_operations
                SET step=$4,result=result || $5::jsonb,updated_at=NOW()
                WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
                  AND lease_expires_at>NOW() RETURNING id""",
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
            raise ValueError("invalid_operation_state")
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """UPDATE agent_compute_operations
                SET state=$4,step=$5,message=$6,result=result || $7::jsonb,
                    next_attempt_at=NOW()+make_interval(secs=>$8),lease_expires_at=NULL,worker_id=NULL,
                    updated_at=NOW(),completed_at=CASE WHEN $4 IN ('succeeded','failed') THEN NOW() ELSE NULL END
                WHERE id=$1 AND generation=$2 AND worker_id=$3 AND state='running'
                  AND lease_expires_at>NOW() RETURNING id""",
                    operation_id,
                    generation,
                    worker_id,
                    state,
                    step,
                    message,
                    _json(result or {}),
                    retry_after_seconds or 30.0,
                )
            )

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
                """SELECT n.*,a.desired_state,a.release_committed_at
                FROM agent_compute_nodes n JOIN agent_compute_allocations a ON a.id=n.allocation_id
                WHERE n.id=$1 FOR UPDATE OF a""",
                node_id,
            )
            if node is None or node["desired_state"] != "present" or node["release_committed_at"]:
                raise ValueError("resource_retiring")
            existing = await conn.fetchrow(
                "SELECT * FROM agent_runner_hosts WHERE host=$1 FOR UPDATE", host
            )
            if existing is not None and existing["compute_node_id"] != node_id:
                # Even an idle unbound Host may own stopped groups. Adoption is
                # an explicit verified workflow, never a hostname collision.
                raise ValueError("host_identity_conflict")
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
                    ON CONFLICT(host,purpose,source_id) DO UPDATE SET scope=EXCLUDED.scope,
                        revision=agent_host_admission_holds.revision+1""",
                    allocation_id,
                    purpose,
                    source_id,
                    scope,
                )

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
            raise ValueError("allocation_not_found")
        hosts = [h["host"] for h in allocation["hosts"]]
        reasons: list[str] = []
        if allocation["management_mode"] != "manage" or not allocation["scope_complete"]:
            reasons.append("unmanaged_scope")
        if any(not n["managed"] for n in allocation["nodes"]):
            reasons.append("unmanaged_scope")
        active = await conn.fetchval("SELECT active_host FROM agent_runner_policy WHERE id")
        if active in hosts:
            reasons.append("placement_pinned")
        groups = await conn.fetch(
            "SELECT id,host,state,active_operation_count FROM agent_sandboxes WHERE host=ANY($1::text[])",
            hosts,
        )
        if groups:
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
            """SELECT count(*) FROM agent_session_forks f JOIN agent_sandboxes g ON g.id=f.sandbox_id
            WHERE g.host=ANY($1::text[]) AND f.state IN ('preparing','materialized')""",
            hosts,
        )
        if forks:
            reasons.append("forks_present")
        # Inventory verification comes from a trusted provider + broker scan,
        # not from heartbeat slot counts. Even zero registered hosts may hide
        # unadopted containers, so absence is not proof of an empty resource.
        now = await conn.fetchval("SELECT NOW()")
        verified = allocation["inventory_verified_at"]
        if verified is None or (now - verified).total_seconds() > 120:
            reasons.append("inventory_unverified")
        if (
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
            "retained_groups": [dict(g) for g in groups],
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
        """Commit irreversible release only after atomic dependency validation."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            allocation = await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE", allocation_id
            )
            if allocation is None:
                raise ValueError("allocation_not_found")
            if allocation["release_committed_at"] is not None:
                return {"release_committed": True, "blocked_reasons": []}
            impact = await self._retirement_impact(conn, allocation_id)
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
            await conn.execute(
                "UPDATE agent_compute_allocations SET release_committed_at=NOW(),updated_at=NOW() WHERE id=$1",
                allocation_id,
            )
            await revoke_host_credentials(conn, [h["host"] for h in impact["hosts"]])
            # Later pending extensions must never escape after the commit.
            await conn.execute(
                """UPDATE agent_compute_operations SET state='cancelled',message='release_committed',completed_at=NOW()
                WHERE allocation_id=$1 AND id<>$2 AND kind='extend' AND state IN ('pending','waiting','blocked')""",
                allocation_id,
                operation_id,
            )
            return {**impact, "release_committed": True}

    async def cancel_operation(self, operation_id: str) -> dict[str, Any]:
        """Cancel a safe retirement intent and remove only its own restriction."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            row = await conn.fetchrow(
                "SELECT * FROM agent_compute_operations WHERE id=$1 FOR UPDATE", operation_id
            )
            if row is None:
                raise ValueError("operation_not_found")
            if row["state"] == "running":
                raise ValueError("operation_in_flight")
            committed = await conn.fetchval(
                "SELECT release_committed_at FROM agent_compute_allocations WHERE id=$1",
                row["allocation_id"],
            )
            if committed:
                raise ValueError("release_committed")
            if (load_json(row["result"]) or {}).get("external_pending"):
                raise ValueError("external_operation_unresolved")
            if row["kind"] != "retire" or row["state"] not in ("pending", "waiting", "blocked"):
                raise ValueError("operation_not_cancellable")
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
            return _row(
                await conn.fetchrow(
                    "UPDATE agent_compute_operations SET state='cancelled',completed_at=NOW(),updated_at=NOW() WHERE id=$1 RETURNING *",
                    operation_id,
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
                raise ValueError("operation_not_found")
            if operation["kind"] != "acquire" or operation["state"] != "unknown":
                raise ValueError("operation_not_resolvable")
            if (load_json(operation["result"]) or {}).get("resource_ref"):
                raise ValueError("operation_not_resolvable")
            allocation = await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE",
                operation["allocation_id"],
            )
            if (
                allocation is None
                or allocation["external_id"] is not None
                or allocation["release_committed_at"] is not None
            ):
                raise ValueError("operation_not_resolvable")
            if await conn.fetchval(
                "SELECT 1 FROM agent_compute_allocations WHERE provider_id=$1 AND external_id=$2",
                allocation["provider_id"],
                external_id,
            ):
                raise ValueError("resource_already_managed")
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

    async def retry_operation(self, operation_id: str) -> dict[str, Any]:
        """Resume an operation from its durable step and original parameters."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('compute-dispatch',0))"
            )
            row = await conn.fetchrow(
                "SELECT * FROM agent_compute_operations WHERE id=$1 FOR UPDATE", operation_id
            )
            if row is None:
                raise ValueError("operation_not_found")
            if row["state"] not in ("failed", "blocked", "unknown"):
                raise ValueError("operation_not_retryable")
            # Unknown retains the exact step and parameters. Worker must query
            # recover_acquire/release/extend rather than repeat the side effect.
            return _row(
                await conn.fetchrow(
                    """UPDATE agent_compute_operations
                SET state='pending',next_attempt_at=NOW(),completed_at=NULL,updated_at=NOW()
                WHERE id=$1 RETURNING *""",
                    operation_id,
                )
            )
