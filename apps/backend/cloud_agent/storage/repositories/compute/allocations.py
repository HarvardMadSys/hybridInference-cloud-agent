"""Compute allocations: acquisition, policy, inventory, observation and removal."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from cloud_agent.domain.compute import provider_resource_absent
from cloud_agent.ports.compute_resources import ComputeSchedulingSnapshot
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.compute_inventory import (
    load_allocations,
    load_hosts,
    load_scheduling_operations,
)
from cloud_agent.storage._sql.compute_observation import (
    release_terminated_groups,
    resume_blocked_retirement,
    terminal_proof_stands,
)
from cloud_agent.storage._sql.hosts import HOST_RETAINED_GROUP
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase, _json, _row


class AllocationsMixin(ComputeRepositoryBase):
    """Allocations, their policy, their inventory, and what observing them finds."""

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
                    raise StorageConflict(ConflictCode.RESOURCE_ALREADY_MANAGED)
                if kind != "import":
                    occupied = await conn.fetchval(
                        """SELECT 1 FROM agent_runner_hosts h
                        JOIN agent_compute_nodes n ON n.id=h.compute_node_id WHERE n.allocation_id=$1
                        UNION ALL SELECT 1 FROM agent_compute_operations WHERE allocation_id=$1
                            AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1""",
                        reused["id"],
                    )
                    if occupied or load_json(reused["deployment_profile"]):
                        raise StorageConflict(ConflictCode.RESOURCE_ALREADY_MANAGED)
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

    async def get_allocation(self, allocation_id: str) -> dict[str, Any] | None:
        """Read one allocation and its stable node and Host bindings."""
        async with self._pool.acquire() as conn:
            return await self._allocation(conn, allocation_id)

    async def remove_allocation(self, *, allocation_id: str, expected_revision: int) -> None:  # noqa: PLR0912, PLR0915 complexity baseline, #771
        """Forget an unused server and its inventory after rechecking dependencies.

        This performs no provider or filesystem action. A releasable resource
        must already be confirmed absent; an acquisition that never submitted
        and a disabled attached machine can instead be deregistered locally.
        User jobs and deleted sandbox audit rows survive the inventory cleanup,
        and so do ``cold`` groups, which no longer need the machine: any
        restore-capable host may claim them (``HOST_RETAINED_GROUP``).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            active = await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            await lock_compute_entry(conn, exclusive=True)
            row = await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE", allocation_id
            )
            if row is None:
                raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
            if row["revision"] != expected_revision:
                raise StorageConflict(ConflictCode.REVISION_CONFLICT)
            # The dispatcher locks operation rows without taking the topology
            # fence. Lock them here so a queued acquisition is either removed
            # before it is claimed, or observed running and refused.
            related_operations = [
                _row(operation)
                for operation in await conn.fetch(
                    """SELECT o.*,a.desired_state AS allocation_desired_state,
                              a.observed_state AS allocation_observed_state,
                              a.release_committed_at AS allocation_release_committed_at
                       FROM agent_compute_operations o
                       LEFT JOIN agent_compute_allocations a ON a.id=o.allocation_id
                       WHERE o.allocation_id=$1 OR o.allocation_id=$2
                          OR a.predecessor_id=$1 OR o.result->'candidate'->>'id'=$1
                          OR o.result->'attempts' @> jsonb_build_array(
                              jsonb_build_object('candidate',$1::text)
                          ) OR o.parameters->>'source_allocation_id'=$1
                          OR o.id IN (
                              SELECT parameters->>'rotation_operation_id'
                              FROM agent_compute_operations WHERE allocation_id=$1
                          )
                       ORDER BY o.id FOR UPDATE OF o""",
                    allocation_id,
                    row["predecessor_id"],
                )
            ]
            operations = [
                operation
                for operation in related_operations
                if operation["allocation_id"] == allocation_id
            ]
            if any(
                operation["kind"] == "rotate"
                and (
                    operation["state"] not in {"succeeded", "failed", "cancelled"}
                    or (
                        operation["state"] == "failed"
                        and operation["result"].get("candidate")
                        and operation["allocation_desired_state"] == "present"
                        and operation["allocation_release_committed_at"] is None
                        and operation["allocation_observed_state"] not in {"terminated", "absent"}
                    )
                )
                for operation in related_operations
            ):
                raise StorageConflict(ConflictCode.ROTATION_IN_PROGRESS)
            if any(
                operation["allocation_id"] != allocation_id
                and operation["parameters"].get("source_allocation_id") == allocation_id
                and operation["kind"] in {"acquire", "discard"}
                and operation["state"] not in {"succeeded", "failed", "cancelled"}
                for operation in related_operations
            ):
                raise StorageConflict(ConflictCode.ROTATION_IN_PROGRESS)
            # Staging and abandoned-candidate release both need the source's
            # predecessor link. A successor already serving its Host may keep
            # running after the old server's historical link is cleared.
            if await conn.fetchval(
                """SELECT 1 FROM agent_compute_allocations candidate
                   WHERE candidate.predecessor_id=$1 AND (
                       EXISTS (
                           SELECT 1 FROM agent_compute_operations o
                           WHERE o.allocation_id=candidate.id
                             AND o.kind IN ('acquire','discard')
                             AND o.state NOT IN ('succeeded','failed','cancelled')
                       ) OR (
                           candidate.observed_state NOT IN ('terminated','absent')
                           AND NOT EXISTS (
                               SELECT 1 FROM agent_runner_hosts h
                               JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                               WHERE n.allocation_id=candidate.id
                           )
                       )
                   ) LIMIT 1""",
                allocation_id,
            ):
                raise StorageConflict(ConflictCode.ROTATION_IN_PROGRESS)
            # Only an operation that can still act holds the records: one a
            # worker runs, one whose outcome is unknown, or one still confirming
            # a provider call, as the dispatcher and release count them. One
            # that stopped keeps the flag of the call it last confirmed, since
            # finishing merges results and a release cancels the rest as they
            # stand. Once the provider confirms the machine gone, that call can
            # change nothing; until then a resource must be released anyway,
            # and without one the flag is a provider receipt like those below.
            if any(
                operation["state"] in {"running", "unknown"}
                or (operation["state"] == "waiting" and operation["result"].get("external_pending"))
                for operation in operations
            ):
                raise StorageConflict(ConflictCode.EXTERNAL_OPERATION_UNRESOLVED)
            allocation = await self._allocation(conn, allocation_id)
            assert allocation is not None
            hosts = [host["host"] for host in allocation["hosts"]]
            if active in hosts:
                raise StorageConflict(ConflictCode.PLACEMENT_PINNED)
            # Attempt settlement locks its attempt before returning the host's
            # slot. Keep that order when deleting hosts and nulling audit FKs.
            attempts = await conn.fetch(
                "SELECT status FROM agent_attempts WHERE host_id=ANY($1::text[]) "
                "ORDER BY id FOR UPDATE",
                hosts,
            )
            if any(attempt["status"] == "running" for attempt in attempts):
                raise StorageConflict(ConflictCode.ACTIVE_ATTEMPTS_PRESENT)
            host_rows = await conn.fetch(
                "SELECT host,status,drain_fence_active FROM agent_runner_hosts "
                "WHERE host=ANY($1::text[]) ORDER BY host FOR UPDATE",
                hosts,
            )
            if any(host["drain_fence_active"] for host in host_rows):
                raise StorageConflict(ConflictCode.DEPLOYMENT_DRAIN_ACTIVE)
            if await conn.fetchval(
                "SELECT 1 FROM agent_sandboxes g WHERE g.host=ANY($1::text[]) "
                f"AND {HOST_RETAINED_GROUP} LIMIT 1",
                hosts,
            ):
                raise StorageConflict(ConflictCode.RETAINED_GROUPS_PRESENT)
            terminal = (
                allocation["observed_state"] in {"terminated", "absent"}
                and allocation["observed_at"] is not None
            )
            uncreated = provider_resource_absent(allocation)
            if (
                uncreated
                and not terminal
                and (
                    any(
                        operation["result"].get("resource_ref")
                        or operation["result"].get("external_id")
                        or operation["result"].get("resolved_external_id")
                        or operation["result"].get("external_pending")
                        or (
                            operation["kind"] in {"acquire", "add", "import"}
                            and operation["step"] != "pending"
                        )
                        for operation in operations
                    )
                    or await conn.fetchval(
                        "SELECT 1 FROM agent_compute_node_registrations "
                        "WHERE allocation_id=$1 AND registered_at IS NOT NULL LIMIT 1",
                        allocation_id,
                    )
                )
            ):
                raise StorageConflict(ConflictCode.EXTERNAL_OPERATION_UNRESOLVED)
            local_attach = (
                allocation["mode"] == "attach"
                and not allocation["release_supported"]
                and all(host["status"] == "disabled" for host in host_rows)
            )
            if not terminal and not uncreated and not local_attach:
                raise StorageConflict(ConflictCode.RESOURCE_RELEASE_REQUIRED)
            if allocation.get("autoscaling_owned") and not terminal:
                raise StorageConflict(ConflictCode.AUTOSCALING_MEMBER_ACTIVE)
            await conn.execute(
                "DELETE FROM agent_host_enroll_tokens "
                "WHERE host=ANY($1::text[]) OR consumed_host=ANY($1::text[])",
                hosts,
            )
            await conn.execute("DELETE FROM agent_runner_hosts WHERE host=ANY($1::text[])", hosts)
            await conn.execute(
                """DELETE FROM agent_host_admission_holds
                   WHERE allocation_id=$1 OR (
                       purpose IN ('bootstrap','retirement','rotation')
                       AND source_id=ANY($2::text[])
                   )""",
                allocation_id,
                [operation["id"] for operation in operations],
            )
            await conn.execute(
                "DELETE FROM agent_compute_node_registrations WHERE allocation_id=$1", allocation_id
            )
            await conn.execute(
                "DELETE FROM agent_cloudlab_autoscaling_members WHERE allocation_id=$1",
                allocation_id,
            )
            # The reservation belongs to the autoscaling policy's configured
            # setup. Recycle its slot while retaining a still-configured profile.
            await conn.execute(
                """DELETE FROM agent_cloudlab_autoscaling_profile_reservations r
                   USING agent_cloudlab_autoscaling_policy p
                   WHERE p.id AND r.allocation_id=$1
                     AND NOT (r.deployment_profile=ANY(p.deployment_profiles))""",
                allocation_id,
            )
            await conn.execute(
                "UPDATE agent_cloudlab_autoscaling_profile_reservations "
                "SET allocation_id=NULL,updated_at=NOW() WHERE allocation_id=$1",
                allocation_id,
            )
            await conn.execute(
                "UPDATE agent_compute_allocations SET predecessor_id=NULL,updated_at=NOW() "
                "WHERE predecessor_id=$1",
                allocation_id,
            )
            await conn.execute(
                "DELETE FROM agent_compute_operations WHERE allocation_id=$1", allocation_id
            )
            await conn.execute(
                "DELETE FROM agent_compute_nodes WHERE allocation_id=$1", allocation_id
            )
            await conn.execute("DELETE FROM agent_compute_allocations WHERE id=$1", allocation_id)

    async def list_allocations(self) -> list[dict[str, Any]]:
        """Read all managed inventory from one local snapshot."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            return await load_allocations(conn, _row)

    async def scheduling_inventory(self) -> ComputeSchedulingSnapshot:
        """Read the scheduler's allocation and operation views in six bulk queries."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            allocations = await load_allocations(conn, _row)
            ids = [
                row["id"]
                for row in allocations
                if row.get("observed_state") not in {"terminated", "absent"}
            ]
            operations, lifecycle = await load_scheduling_operations(conn, ids, _row)
            return ComputeSchedulingSnapshot(allocations, operations, lifecycle)

    async def server_inventory(self) -> dict[str, Any]:
        """Read global placement and resource/Host distribution in one snapshot."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            allocations = await load_allocations(conn, _row)
            hosts = await load_hosts(conn, allocations)
            metadata = await conn.fetchrow(
                "SELECT (SELECT active_host FROM agent_runner_policy WHERE id) AS active_host,"
                " NOW() AS observed_at"
            )
            return {"allocations": allocations, "hosts": hosts, **dict(metadata)}

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
                "UPDATE agent_compute_allocations SET observation_error=$2,updated_at=NOW() WHERE id=$1 AND NOT (observed_state IN ('terminated','absent') AND external_id IS NOT NULL)",
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
        """Accept a trusted sandbox manager scan only against the exact fenced topology.

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
                raise StorageConflict(ConflictCode.REVISION_CONFLICT)
            return _row(row)

    async def observe_allocation(
        self,
        *,
        allocation_id: str,
        observation: dict[str, Any],
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a fenced observation, keeping a known deadline and any terminal proof."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            allocation = await conn.fetchrow(
                "SELECT * FROM agent_compute_allocations WHERE id=$1 FOR UPDATE", allocation_id
            )
            if allocation is None:
                raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
            current_identity = allocation["external_id"]
            if current_identity is not None and observation.get("external_id") not in (
                None,
                current_identity,
            ):
                raise StorageConflict(ConflictCode.RESOURCE_IDENTITY_CHANGED)
            if terminal_proof_stands(allocation, observation):
                return _row(allocation)
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
                    raise StorageConflict(ConflictCode.RESOURCE_IDENTITY_CHANGED)
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
                    raise StorageConflict(ConflictCode.RESOURCE_IDENTITY_CHANGED)
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
                raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
            if row["observed_state"] in {"terminated", "absent"}:
                await self._release_terminal_autoscaling_reservation(conn, allocation_id)
                if row["observed_state"] == "terminated":
                    await release_terminated_groups(conn, allocation_id)
                if row["desired_state"] == "absent" and row["release_committed_at"] is None:
                    await resume_blocked_retirement(conn, allocation_id)
                row = await conn.fetchrow(
                    "SELECT * FROM agent_compute_allocations WHERE id=$1", allocation_id
                )
            return _row(row)

    async def _upsert_node(
        self, conn: Any, allocation_id: str, node: dict[str, Any]
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """INSERT INTO agent_compute_nodes
            (id,allocation_id,provider_node_id,instance_identity,name,connection,managed)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)
            ON CONFLICT (allocation_id,provider_node_id) DO UPDATE
            SET name=EXCLUDED.name,updated_at=NOW(),
                -- A registered host key belongs to this exact instance and
                -- provider observations never carry it, at most a null
                -- placeholder from the serialized connection: keep it.
                connection=CASE WHEN agent_compute_nodes.connection->>'host_key' IS NOT NULL
                    AND EXCLUDED.connection->>'host_key' IS NULL
                    THEN EXCLUDED.connection
                        || jsonb_build_object('host_key', agent_compute_nodes.connection->'host_key')
                    ELSE EXCLUDED.connection END
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
            raise StorageConflict(ConflictCode.RESOURCE_IDENTITY_CHANGED)
        return _row(row)
