"""Postgres compute inventory and recoverable operations.

All provider/SSH calls belong to the application layer. Every external result
may be fenced against its worker lease, and every destructive decision is
rechecked in the same transaction as its irreversible commit point.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import asyncpg

from cloud_agent.capacity_protocol import FRESH_AFTER_SECONDS
from cloud_agent.domain.compute import (
    autoscaling_acquire_can_cancel,
    autoscaling_operation_is_terminal,
    autoscaling_retirement_needs_settlement,
    discard_request_id,
    fingerprint as operation_fingerprint,
    provider_resource_absent,
)
from cloud_agent.storage._sql.compute_admission import host_compute_admission, lock_compute_entry
from cloud_agent.storage._sql.hosts import revoke_host_credentials
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from datetime import datetime

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
        raise ValueError("record_not_found")
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


class PostgresComputeResourceRepository:
    """One durable identity for each resource instance and operation intent."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_deployment_profiles(self) -> list[dict[str, Any]]:
        """List profile metadata without bundles, plaintext inputs or ciphertext."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id,label,configuration,source_sha256,revision,created_at,updated_at
                FROM agent_compute_deployment_profiles ORDER BY created_at,id"""
            )
        return [_row(row) for row in rows]

    async def get_deployment_profile(self, profile_id: str) -> dict[str, Any] | None:
        """Read one trusted profile including its bundle and encrypted inputs."""
        async with self._pool.acquire() as conn:
            return _optional_row(
                await conn.fetchrow(
                    "SELECT * FROM agent_compute_deployment_profiles WHERE id=$1", profile_id
                )
            )

    async def save_deployment_profile(
        self,
        *,
        profile_id: str,
        label: str,
        configuration: dict[str, Any],
        source_bundle: bytes,
        source_sha256: str,
        secret_ciphertext: str,
    ) -> dict[str, Any]:
        """Create one profile. A host release may later advance its commit and bundle."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO agent_compute_deployment_profiles
                (id,label,configuration,source_bundle,source_sha256,secret_ciphertext)
                VALUES ($1,$2,$3::jsonb,$4,$5,$6)
                ON CONFLICT DO NOTHING RETURNING *""",
                profile_id,
                label,
                _json(configuration),
                source_bundle,
                source_sha256,
                secret_ciphertext,
            )
        if row is None:
            raise ValueError("deployment_profile_exists")
        return _row(row)

    async def advance_host_release(
        self,
        *,
        allocation_id: str,
        previous_sha: str,
        previous_digest: str,
        deployment_sha: str,
        source_sha256: str,
        source_bundle: bytes,
        release_bundle_path: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Move every stored profile of one release, and the live hosts using it.

        A profile another deployment is currently installing is left unchanged.
        The calling allocation must end on the new revision.
        """
        if previous_sha == deployment_sha and previous_digest == source_sha256:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            await conn.execute(
                """UPDATE agent_compute_deployment_profiles AS profile
                SET source_bundle=$3,
                    source_sha256=$4,
                    configuration=jsonb_set(
                        configuration, '{deployment_sha}', to_jsonb($5::text), true
                    ),
                    revision=revision+1,
                    updated_at=NOW()
                WHERE configuration->>'deployment_sha'=$1
                  AND source_sha256=$2
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_compute_allocations AS allocation
                    JOIN agent_compute_operations AS operation
                      ON operation.allocation_id=allocation.id
                    WHERE allocation.deployment_profile->>'id'=profile.id
                      AND operation.id<>$6
                      AND operation.kind IN ('acquire','upgrade','rotate')
                      AND operation.state IN ('pending','running','waiting','unknown')
                  )""",
                previous_sha,
                previous_digest,
                source_bundle,
                source_sha256,
                deployment_sha,
                operation_id,
            )
            await conn.execute(
                """UPDATE agent_compute_allocations AS allocation
                SET deployment_profile=allocation.deployment_profile
                      || jsonb_build_object(
                           'deployment_sha',$3::text,'source_sha256',$4::text
                         )
                      || CASE
                           WHEN allocation.deployment_profile->>'source_bundle'
                                LIKE 'stored-profile:%'
                           THEN '{}'::jsonb
                           ELSE jsonb_build_object('source_bundle',$5::text)
                         END,
                    revision=revision+1,
                    updated_at=NOW()
                WHERE allocation.deployment_profile->>'deployment_sha'=$1
                  AND allocation.deployment_profile->>'source_sha256'=$2
                  AND allocation.release_committed_at IS NULL
                  AND COALESCE(allocation.observed_state,'') NOT IN ('terminated','absent')
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_compute_operations AS operation
                    WHERE operation.allocation_id=allocation.id
                      AND operation.id<>$6
                      AND operation.kind IN ('acquire','upgrade','rotate')
                      AND operation.state IN ('pending','running','waiting','unknown')
                  )""",
                previous_sha,
                previous_digest,
                deployment_sha,
                source_sha256,
                release_bundle_path,
                operation_id,
            )
            recorded = await conn.fetchval(
                """SELECT deployment_profile->>'deployment_sha'=$2
                      AND deployment_profile->>'source_sha256'=$3
                FROM agent_compute_allocations WHERE id=$1""",
                allocation_id,
                deployment_sha,
                source_sha256,
            )
            if recorded is not True:
                raise ValueError("host_release_not_recorded")

    async def activate_automatic_profiles(
        self,
        *,
        profile_ids: list[str],
        request_id: str,
        request_fingerprint: str,
    ) -> None:
        """Atomically admit provisioned slots while keeping release inputs immutable."""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id,configuration FROM agent_compute_deployment_profiles WHERE id=ANY($1::text[]) FOR UPDATE",
                profile_ids,
            )
            if len(rows) != len(profile_ids):
                raise ValueError("record_not_found")
            for row in rows:
                configuration = load_json(row["configuration"]) or {}
                metadata = configuration.get("automatic_setup", {})
                if (
                    metadata.get("request_id") != request_id
                    or metadata.get("fingerprint") != request_fingerprint
                ):
                    raise ValueError("idempotency_conflict")
            await conn.execute(
                """UPDATE agent_compute_deployment_profiles
                SET configuration=jsonb_set(configuration,'{automatic_setup,ready}','true'::jsonb),
                    revision=revision+1,updated_at=NOW()
                WHERE id=ANY($1::text[]) AND configuration #>> '{automatic_setup,ready}' = 'false'""",
                profile_ids,
            )

    async def provider_defaults(self, provider_id: str) -> dict[str, Any]:
        """Return the saved non-secret form defaults for one provider."""
        async with self._pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT compute_defaults FROM agent_runner_policy WHERE id"
            )
        defaults = load_json(stored) or {}
        value = defaults.get(provider_id)
        return dict(value) if isinstance(value, dict) else {}

    async def set_provider_defaults(
        self, provider_id: str, defaults: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace one provider's defaults without disturbing other providers."""
        async with self._pool.acquire() as conn, conn.transaction():
            stored = await conn.fetchval(
                "SELECT compute_defaults FROM agent_runner_policy WHERE id FOR UPDATE"
            )
            current = load_json(stored) or {}
            current[provider_id] = defaults
            await conn.execute(
                """UPDATE agent_runner_policy
                SET compute_defaults=$1::jsonb,updated_at=NOW() WHERE id""",
                _json(current),
            )
        return dict(defaults)

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
            await lock_compute_entry(conn, exclusive=True)
            if expected_revision is not None and await conn.fetchval(
                """SELECT 1 FROM agent_cloudlab_autoscaling_policy p
                   WHERE p.enabled AND (p.connection_id=$1 OR p.ssh_connection_id=$1)
                   UNION ALL
                   SELECT 1 FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   JOIN agent_compute_operations o ON o.allocation_id=a.id
                   WHERE (a.connection_id=$1 OR a.ssh_connection_id=$1)
                     AND m.state<>'released'
                     AND a.external_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM agent_compute_nodes n WHERE n.allocation_id=a.id
                     )
                     AND o.kind='acquire' AND o.state IN ('pending','running')
                   LIMIT 1""",
                connection_id,
            ):
                raise ValueError("autoscaling_connection_in_use")
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
        profile_id = profile.get("id")
        if profile_id and await conn.fetchval(
            """SELECT 1 FROM agent_cloudlab_autoscaling_profile_reservations
               WHERE deployment_profile=$1""",
            profile_id,
        ):
            raise ValueError("deployment_profile_reserved")
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
                        raise ValueError("operation_in_flight")
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
                    raise ValueError("rotation_in_progress") from None
                raise

    async def get_allocation(self, allocation_id: str) -> dict[str, Any] | None:
        """Read one allocation and its stable node and Host bindings."""
        async with self._pool.acquire() as conn:
            return await self._allocation(conn, allocation_id)

    async def remove_allocation(self, *, allocation_id: str, expected_revision: int) -> None:
        """Forget an unused server and its inventory after rechecking dependencies.

        This performs no provider or filesystem action. A releasable resource
        must already be confirmed absent; an acquisition that never submitted
        and a disabled attached machine can instead be deregistered locally.
        User jobs and deleted sandbox audit rows survive the inventory cleanup.
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
                raise ValueError("allocation_not_found")
            if row["revision"] != expected_revision:
                raise ValueError("revision_conflict")
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
                raise ValueError("rotation_in_progress")
            if any(
                operation["allocation_id"] != allocation_id
                and operation["parameters"].get("source_allocation_id") == allocation_id
                and operation["kind"] in {"acquire", "discard"}
                and operation["state"] not in {"succeeded", "failed", "cancelled"}
                for operation in related_operations
            ):
                raise ValueError("rotation_in_progress")
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
                raise ValueError("rotation_in_progress")
            if any(
                operation["state"] in {"running", "unknown"}
                or operation["result"].get("external_pending")
                for operation in operations
            ):
                raise ValueError("external_operation_unresolved")
            allocation = await self._allocation(conn, allocation_id)
            assert allocation is not None
            hosts = [host["host"] for host in allocation["hosts"]]
            if active in hosts:
                raise ValueError("placement_pinned")
            # Attempt settlement locks its attempt before returning the host's
            # slot. Keep that order when deleting hosts and nulling audit FKs.
            attempts = await conn.fetch(
                "SELECT status FROM agent_attempts WHERE host_id=ANY($1::text[]) "
                "ORDER BY id FOR UPDATE",
                hosts,
            )
            if any(attempt["status"] == "running" for attempt in attempts):
                raise ValueError("active_attempts_present")
            host_rows = await conn.fetch(
                "SELECT host,status,drain_fence_active FROM agent_runner_hosts "
                "WHERE host=ANY($1::text[]) ORDER BY host FOR UPDATE",
                hosts,
            )
            if any(host["drain_fence_active"] for host in host_rows):
                raise ValueError("deployment_drain_active")
            if await conn.fetchval(
                "SELECT 1 FROM agent_sandboxes WHERE host=ANY($1::text[]) "
                "AND deletion_state<>'deleted' LIMIT 1",
                hosts,
            ):
                raise ValueError("retained_groups_present")
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
                raise ValueError("external_operation_unresolved")
            local_attach = (
                allocation["mode"] == "attach"
                and not allocation["release_supported"]
                and all(host["status"] == "disabled" for host in host_rows)
            )
            if not terminal and not uncreated and not local_attach:
                raise ValueError("resource_release_required")
            if allocation.get("autoscaling_owned") and not terminal:
                raise ValueError("autoscaling_member_active")
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
                    """SELECT h.host,h.status,h.compute_node_id,
                (h.slots_used+(SELECT COUNT(*) FROM agent_environment_runs r WHERE r.host=h.host AND NOT r.cleanup_complete AND r.lease_expires_at>NOW() AND r.started_at>NOW()-INTERVAL '30 minutes')) AS slots_used,h.last_heartbeat_at,h.broker_url AS sandbox_manager_url,h.drain_fence_active,
                (SELECT count(*) FROM agent_sandboxes g
                 WHERE g.host=h.host AND g.deletion_state<>'deleted') AS retained_groups
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
                n.allocation_id,(h.slots_used+(SELECT COUNT(*) FROM agent_environment_runs r WHERE r.host=h.host AND NOT r.cleanup_complete AND r.lease_expires_at>NOW() AND r.started_at>NOW()-INTERVAL '30 minutes')) AS slots_used,h.slots_total,h.slots_desired,h.last_heartbeat_at,
                h.labels->>'physical_host' AS physical_host,h.broker_url AS sandbox_manager_url,
                (SELECT count(*) FROM agent_sandboxes g
                 WHERE g.host=h.host AND g.deletion_state<>'deleted') AS retained_groups
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

    @staticmethod
    def _autoscaling_policy_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "revision",
                "enabled",
                "min_hosts",
                "max_hosts",
                "connection_id",
                "ssh_connection_id",
                "configuration",
                "deployment_profiles",
                "sandbox_storage_path",
                "renewal_policy",
                "slot_policy",
                "scale_up_sustain_seconds",
                "scale_down_idle_seconds",
                "cooldown_seconds",
                "scale_down_enabled",
                "created_at",
                "updated_at",
            )
        }

    @staticmethod
    def _autoscaling_runtime_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "generation",
                "pressure_since",
                "idle_since",
                "cooldown_until",
                "queue_pressure",
                "slot_status",
                "last_decision",
                "last_decision_at",
                "blocker_code",
                "blocker_message",
                "observed_at",
                "updated_at",
            )
        }

    @staticmethod
    def _public_operation_state(state: str | None) -> str | None:
        if state is None:
            return None
        mapping: dict[str, str] = {
            "pending": "queued",
            "running": "running",
            "waiting": "retry_wait",
            "blocked": "blocked",
            "unknown": "unknown",
            "failed": "failed",
            "succeeded": "succeeded",
            "cancelled": "cancelled",
        }
        return mapping.get(state, state)

    async def _cloudlab_autoscaling_snapshot(self, conn: Any) -> dict[str, Any]:
        policy = _row(
            await conn.fetchrow("SELECT * FROM agent_cloudlab_autoscaling_policy WHERE id")
        )
        runtime = _row(
            await conn.fetchrow("SELECT * FROM agent_cloudlab_autoscaling_runtime WHERE id")
        )
        member_rows = await conn.fetch(
            """SELECT m.allocation_id,m.deployment_profile,m.state AS persisted_state,
                      m.created_at,m.updated_at,a.desired_state,a.observed_state,
                      a.release_committed_at,h.host,h.slots_total,h.slots_desired,
                      latest.id AS latest_operation_id,latest.kind AS latest_operation_kind,
                      latest.state AS latest_operation_state,
                      latest.message AS latest_operation_message,
                      acquire.id AS latest_acquire_operation_id,
                      acquire.state AS latest_acquire_state,
                      acquire.message AS latest_acquire_message
               FROM agent_cloudlab_autoscaling_members m
               JOIN agent_compute_allocations a ON a.id=m.allocation_id
               LEFT JOIN LATERAL (
                   SELECT rh.host,rh.slots_total,rh.slots_desired
                   FROM agent_compute_nodes n
                   JOIN agent_runner_hosts rh ON rh.compute_node_id=n.id
                   WHERE n.allocation_id=m.allocation_id
                   ORDER BY rh.host LIMIT 1
               ) h ON TRUE
               LEFT JOIN LATERAL (
                   SELECT o.id,o.kind,o.state,o.message
                   FROM agent_compute_operations o
                   WHERE o.allocation_id=m.allocation_id
                   ORDER BY o.created_at DESC,o.id DESC LIMIT 1
               ) latest ON TRUE
               LEFT JOIN LATERAL (
                   SELECT o.id,o.state,o.message
                   FROM agent_compute_operations o
                   WHERE o.allocation_id=m.allocation_id AND o.kind='acquire'
                   ORDER BY o.created_at DESC,o.id DESC LIMIT 1
               ) acquire ON TRUE
               ORDER BY m.created_at,m.allocation_id"""
        )
        members = []
        for raw in member_rows:
            member = dict(raw)
            if (
                member["observed_state"] in {"terminated", "absent"}
                and member["release_committed_at"]
            ):
                state = "released"
            elif member["observed_state"] in {"terminated", "absent"}:
                state = "blocked"
            elif (
                member["desired_state"] == "absent"
                or member["release_committed_at"]
                or member["persisted_state"] == "retiring"
            ):
                state = "retiring"
            elif member["latest_acquire_state"] in {"blocked", "failed", "unknown"}:
                state = "blocked"
            elif member["observed_state"] == "ready":
                state = "active"
            else:
                state = member["persisted_state"]
            members.append(
                {
                    "allocation_id": member["allocation_id"],
                    "deployment_profile": member["deployment_profile"],
                    "state": state,
                    "host": member["host"],
                    "slots_total": member["slots_total"],
                    "slots_desired": member["slots_desired"],
                    "latest_operation_id": member["latest_operation_id"],
                    "latest_operation_kind": member["latest_operation_kind"],
                    "latest_operation_state": self._public_operation_state(
                        member["latest_operation_state"]
                    ),
                    "latest_operation_message": member["latest_operation_message"],
                    "latest_acquire_operation_id": member["latest_acquire_operation_id"],
                    "latest_acquire_state": self._public_operation_state(
                        member["latest_acquire_state"]
                    ),
                    "latest_acquire_message": member["latest_acquire_message"],
                    "created_at": member["created_at"],
                    "updated_at": member["updated_at"],
                }
            )
        reservations = [
            dict(row)
            for row in await conn.fetch(
                """SELECT deployment_profile,allocation_id,created_at,updated_at
                   FROM agent_cloudlab_autoscaling_profile_reservations
                   ORDER BY created_at,deployment_profile"""
            )
        ]
        return {
            "policy": self._autoscaling_policy_view(policy),
            "runtime": self._autoscaling_runtime_view(runtime),
            "members": members,
            "profile_reservations": reservations,
            "observed_at": await conn.fetchval("SELECT NOW()"),
        }

    async def get_cloudlab_autoscaling(self) -> dict[str, Any]:
        """Read policy, runtime and ownership without exposing profile secrets."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            return await self._cloudlab_autoscaling_snapshot(conn)

    async def list_reserved_deployment_profiles(self) -> list[str]:
        """Keep manually created resources out of autoscaler-reserved routes."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT deployment_profile
                   FROM agent_cloudlab_autoscaling_profile_reservations
                   ORDER BY deployment_profile"""
            )
        return [row["deployment_profile"] for row in rows]

    async def update_cloudlab_autoscaling(
        self,
        *,
        expected_revision: int,
        policy: dict[str, Any],
        profile_snapshots: dict[str, dict[str, Any]],
        requested_by: str | None,
    ) -> dict[str, Any]:
        """Compare-and-swap policy and reserve every selected sandbox manager route."""
        selected = list(policy["deployment_profiles"])
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            current = await conn.fetchrow(
                "SELECT * FROM agent_cloudlab_autoscaling_policy WHERE id FOR UPDATE"
            )
            runtime = await conn.fetchrow(
                "SELECT generation FROM agent_cloudlab_autoscaling_runtime WHERE id FOR UPDATE"
            )
            if current is None or runtime is None:
                raise ValueError("record_not_found")
            current = _row(current)
            if current["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            pause_existing = bool(
                current["enabled"]
                and not policy["enabled"]
                and all(
                    current.get(key) == value for key, value in policy.items() if key != "enabled"
                )
            )
            if not pause_existing and set(profile_snapshots) != set(selected):
                raise ValueError("autoscaling_profile_unavailable")
            if policy["enabled"] and await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id"
            ):
                raise ValueError("autoscaling_placement_pinned")
            for connection_id, provider_id in (
                (policy.get("connection_id"), "cloudlab"),
                (policy.get("ssh_connection_id"), "existing_ssh"),
            ):
                if pause_existing:
                    break
                if connection_id is None:
                    continue
                valid = await conn.fetchval(
                    """SELECT 1 FROM agent_compute_connections
                       WHERE id=$1 AND provider_id=$2 AND status='verified' FOR SHARE""",
                    connection_id,
                    provider_id,
                )
                if not valid:
                    raise ValueError("autoscaling_connection_unverified")
            for profile_id in selected:
                if pause_existing:
                    break
                manual_owner = await conn.fetchval(
                    """SELECT a.id FROM agent_compute_allocations a
                       LEFT JOIN agent_cloudlab_autoscaling_members m
                         ON m.allocation_id=a.id
                       WHERE a.deployment_profile->>'id'=$1
                         AND a.observed_state NOT IN ('terminated','absent')
                         AND m.allocation_id IS NULL LIMIT 1""",
                    profile_id,
                )
                if manual_owner:
                    raise ValueError("autoscaling_profile_in_use")
                reservation = await conn.fetchrow(
                    """SELECT allocation_id
                       FROM agent_cloudlab_autoscaling_profile_reservations
                       WHERE deployment_profile=$1 FOR UPDATE""",
                    profile_id,
                )
                if reservation is None:
                    await conn.execute(
                        """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
                           (deployment_profile,profile_snapshot)
                           VALUES ($1,$2::jsonb)""",
                        profile_id,
                        _json(profile_snapshots[profile_id]),
                    )
                elif reservation["allocation_id"] is None:
                    await conn.execute(
                        """UPDATE agent_cloudlab_autoscaling_profile_reservations
                           SET profile_snapshot=$2::jsonb,updated_at=NOW()
                           WHERE deployment_profile=$1""",
                        profile_id,
                        _json(profile_snapshots[profile_id]),
                    )
            await conn.execute(
                """DELETE FROM agent_cloudlab_autoscaling_profile_reservations
                   WHERE allocation_id IS NULL
                     AND NOT (deployment_profile=ANY($1::text[]))""",
                selected,
            )
            updated = await conn.fetchrow(
                """UPDATE agent_cloudlab_autoscaling_policy SET
                   enabled=$2,min_hosts=$3,max_hosts=$4,connection_id=$5,
                   ssh_connection_id=$6,configuration=$7::jsonb,
                   deployment_profiles=$8::text[],sandbox_storage_path=$9,
                   renewal_policy=$10::jsonb,slot_policy=$11::jsonb,
                   scale_up_sustain_seconds=$12,scale_down_idle_seconds=$13,
                   cooldown_seconds=$14,scale_down_enabled=$15,
                   revision=revision+1,updated_by=$16,updated_at=NOW()
                   WHERE id AND revision=$1 RETURNING *""",
                expected_revision,
                policy["enabled"],
                policy["min_hosts"],
                policy["max_hosts"],
                policy.get("connection_id"),
                policy.get("ssh_connection_id"),
                _json(policy.get("configuration", {})),
                selected,
                policy.get("sandbox_storage_path"),
                _json(policy.get("renewal_policy", {})),
                _json(policy.get("slot_policy", {})),
                policy["scale_up_sustain_seconds"],
                policy["scale_down_idle_seconds"],
                policy["cooldown_seconds"],
                policy["scale_down_enabled"],
                requested_by,
            )
            if updated is None:
                raise ValueError("revision_conflict")
            renewal_policy = _json(policy.get("renewal_policy", {}))
            await conn.execute(
                """UPDATE agent_compute_allocations a
                   SET policy=$1::jsonb,revision=a.revision+1,updated_at=NOW()
                   FROM agent_cloudlab_autoscaling_members m
                   WHERE m.allocation_id=a.id
                     AND m.state<>'released'
                     AND a.observed_state NOT IN ('terminated','absent')
                     AND a.policy IS DISTINCT FROM $1::jsonb""",
                renewal_policy,
            )
            await conn.execute(
                """UPDATE agent_cloudlab_autoscaling_runtime
                   SET generation=generation+1,pressure_since=NULL,idle_since=NULL,
                       cooldown_until=NULL,blocker_code=NULL,blocker_message=NULL,
                       updated_at=NOW() WHERE id"""
            )
            return await self._cloudlab_autoscaling_snapshot(conn)

    async def _advance_autoscaling_runtime(
        self,
        conn: Any,
        *,
        expected_generation: int,
        decision: dict[str, Any],
        cooldown_seconds: int,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """UPDATE agent_cloudlab_autoscaling_runtime
               SET generation=generation+1,pressure_since=NULL,idle_since=NULL,
                   cooldown_until=NOW()+make_interval(secs=>$2),
                   last_decision=$3::jsonb,last_decision_at=NOW(),
                   blocker_code=NULL,blocker_message=NULL,observed_at=NOW(),updated_at=NOW()
               WHERE id AND generation=$1 RETURNING *""",
            expected_generation,
            cooldown_seconds,
            _json(decision),
        )
        if row is None:
            raise ValueError("autoscaling_runtime_conflict")
        return self._autoscaling_runtime_view(_row(row))

    async def create_autoscale_allocation_operation(
        self,
        *,
        expected_revision: int,
        expected_generation: int,
        request_id: str,
        fingerprint: str,
        allocation: dict[str, Any],
        parameters: dict[str, Any],
        requested_by: str | None,
        decision: dict[str, Any],
        cooldown_seconds: int,
    ) -> dict[str, Any]:
        """Atomically claim one reserved profile and queue one owned acquisition."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            existing = await self._existing_request(conn, request_id, fingerprint)
            if existing is not None:
                runtime = _row(
                    await conn.fetchrow("SELECT * FROM agent_cloudlab_autoscaling_runtime WHERE id")
                )
                return {
                    "operation": existing,
                    "runtime": self._autoscaling_runtime_view(runtime),
                }
            policy = _row(
                await conn.fetchrow(
                    "SELECT * FROM agent_cloudlab_autoscaling_policy WHERE id FOR UPDATE"
                )
            )
            runtime_generation = await conn.fetchval(
                "SELECT generation FROM agent_cloudlab_autoscaling_runtime WHERE id FOR UPDATE"
            )
            if policy["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            if runtime_generation != expected_generation:
                raise ValueError("autoscaling_runtime_conflict")
            if not policy["enabled"]:
                raise ValueError("autoscaling_disabled")
            for connection_id, provider_id in (
                (policy.get("connection_id"), "cloudlab"),
                (policy.get("ssh_connection_id"), "existing_ssh"),
            ):
                if connection_id is None or not await conn.fetchval(
                    """SELECT 1 FROM agent_compute_connections
                       WHERE id=$1 AND provider_id=$2 AND status='verified'
                       FOR SHARE""",
                    connection_id,
                    provider_id,
                ):
                    raise ValueError("autoscaling_connection_unverified")
            await conn.execute(
                """UPDATE agent_cloudlab_autoscaling_profile_reservations r
                   SET allocation_id=NULL,updated_at=NOW()
                   FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE r.allocation_id=m.allocation_id
                     AND a.observed_state IN ('terminated','absent')
                     AND a.release_committed_at IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM agent_compute_operations o
                         WHERE o.allocation_id=a.id
                           AND o.kind IN ('acquire','retire','discard')
                           AND o.state IN ('pending','running','waiting','blocked','unknown')
                     )"""
            )
            await conn.execute(
                """UPDATE agent_cloudlab_autoscaling_members m
                   SET state='released',updated_at=NOW()
                   FROM agent_compute_allocations a
                   WHERE a.id=m.allocation_id
                     AND a.observed_state IN ('terminated','absent')
                     AND a.release_committed_at IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM agent_compute_operations o
                         WHERE o.allocation_id=a.id
                           AND o.kind IN ('acquire','retire','discard')
                           AND o.state IN ('pending','running','waiting','blocked','unknown')
                     )"""
            )
            await conn.execute(
                """DELETE FROM agent_cloudlab_autoscaling_profile_reservations
                   WHERE allocation_id IS NULL
                     AND NOT (deployment_profile=ANY($1::text[]))""",
                policy["deployment_profiles"],
            )
            blocked = await conn.fetchrow(
                """SELECT o.state FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   JOIN LATERAL (
                       SELECT state FROM agent_compute_operations
                       WHERE allocation_id=m.allocation_id AND kind='acquire'
                       ORDER BY created_at DESC,id DESC LIMIT 1
                   ) o ON TRUE
                   WHERE m.state<>'released'
                     AND a.observed_state NOT IN ('terminated','absent')
                     AND o.state IN
                       ('pending','running','waiting','blocked','unknown','failed')
                   ORDER BY m.created_at LIMIT 1"""
            )
            if blocked is not None:
                if blocked["state"] in {"blocked", "unknown", "failed"}:
                    raise ValueError("autoscaling_acquire_blocked")
                raise ValueError("autoscaling_operation_in_flight")
            member_count = await conn.fetchval(
                """SELECT count(*) FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE a.observed_state NOT IN ('terminated','absent')"""
            )
            if member_count >= policy["max_hosts"]:
                raise ValueError("autoscaling_at_capacity")
            reservation = await conn.fetchrow(
                """SELECT r.* FROM unnest($1::text[]) WITH ORDINALITY wanted(id,position)
                   JOIN agent_cloudlab_autoscaling_profile_reservations r
                     ON r.deployment_profile=wanted.id
                   WHERE r.allocation_id IS NULL
                   ORDER BY wanted.position LIMIT 1 FOR UPDATE OF r""",
                policy["deployment_profiles"],
            )
            if reservation is None:
                raise ValueError("autoscaling_profile_unavailable")
            profile = load_json(reservation["profile_snapshot"]) or {}
            if policy.get("sandbox_storage_path") is not None:
                profile["sandbox_storage_path"] = policy["sandbox_storage_path"]
            allocation_id = allocation.get("id") or str(uuid4())
            configuration = dict(policy.get("configuration") or {})
            configuration.pop("external_id", None)
            configuration.pop("name", None)
            configuration.update(
                {
                    "credential_ref": policy["connection_id"],
                    "ssh_credential_ref": policy["ssh_connection_id"],
                    "host_key_source": "registration",
                }
            )
            if await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR SHARE"
            ):
                raise ValueError("autoscaling_placement_pinned")
            await conn.execute(
                """INSERT INTO agent_compute_allocations
                   (id,name,provider_id,connection_id,ssh_connection_id,mode,
                    management_mode,configuration,deployment_profile,policy,
                    lease_managed,release_supported)
                   VALUES ($1,$2,'cloudlab',$3,$4,'provision','manage',$5::jsonb,
                           $6::jsonb,$7::jsonb,TRUE,TRUE)""",
                allocation_id,
                allocation.get("name") or f"Autoscaled CloudLab {allocation_id[:8]}",
                policy["connection_id"],
                policy["ssh_connection_id"],
                _json(configuration),
                _json(profile),
                _json(policy.get("renewal_policy") or {}),
            )
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_members
                   (allocation_id,deployment_profile)
                   VALUES ($1,$2)""",
                allocation_id,
                reservation["deployment_profile"],
            )
            await conn.execute(
                """UPDATE agent_cloudlab_autoscaling_profile_reservations
                   SET allocation_id=$2,updated_at=NOW()
                   WHERE deployment_profile=$1 AND allocation_id IS NULL""",
                reservation["deployment_profile"],
                allocation_id,
            )
            operation = await self._insert_operation(
                conn,
                request_id=request_id,
                fingerprint=fingerprint,
                kind="acquire",
                allocation_id=allocation_id,
                parameters=parameters,
                requested_by=requested_by,
            )
            runtime = await self._advance_autoscaling_runtime(
                conn,
                expected_generation=expected_generation,
                decision=decision,
                cooldown_seconds=cooldown_seconds,
            )
            return {"operation": operation, "runtime": runtime}

    async def assert_autoscale_acquire_connections_verified(
        self,
        *,
        allocation_id: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Fence the last credential-health check before an autoscaled provider call."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn)
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            allocation = await conn.fetchrow(
                """SELECT a.connection_id,a.ssh_connection_id,a.desired_state,
                          a.release_committed_at,m.state AS member_state
                   FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE m.allocation_id=$1 FOR SHARE OF a,m""",
                allocation_id,
            )
            if allocation is None:
                return
            if (
                allocation["member_state"] == "released"
                or allocation["desired_state"] != "present"
                or allocation["release_committed_at"] is not None
            ):
                raise ValueError("autoscaling_member_not_retirable")
            for connection_id, provider_id in (
                (allocation["connection_id"], "cloudlab"),
                (allocation["ssh_connection_id"], "existing_ssh"),
            ):
                if connection_id is None or not await conn.fetchval(
                    """SELECT 1 FROM agent_compute_connections
                       WHERE id=$1 AND provider_id=$2 AND status='verified'
                       FOR SHARE""",
                    connection_id,
                    provider_id,
                ):
                    raise ValueError("autoscaling_connection_unverified")

    async def queue_autoscale_retire(
        self,
        *,
        expected_revision: int,
        expected_generation: int,
        allocation_id: str,
        request_id: str,
        fingerprint: str,
        parameters: dict[str, Any],
        requested_by: str | None,
        decision: dict[str, Any],
        cooldown_seconds: int,
    ) -> dict[str, Any]:
        """Queue guarded retirement only for an autoscaler-owned allocation."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            existing = await self._existing_request(conn, request_id, fingerprint)
            if existing is not None:
                runtime = _row(
                    await conn.fetchrow("SELECT * FROM agent_cloudlab_autoscaling_runtime WHERE id")
                )
                return {
                    "operation": existing,
                    "runtime": self._autoscaling_runtime_view(runtime),
                }
            policy = await conn.fetchrow(
                "SELECT revision,enabled,scale_down_enabled,min_hosts FROM agent_cloudlab_autoscaling_policy WHERE id FOR UPDATE"
            )
            runtime_generation = await conn.fetchval(
                "SELECT generation FROM agent_cloudlab_autoscaling_runtime WHERE id FOR UPDATE"
            )
            if policy is None or policy["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            if runtime_generation != expected_generation:
                raise ValueError("autoscaling_runtime_conflict")
            if not policy["enabled"] or not policy["scale_down_enabled"]:
                raise ValueError("autoscaling_scale_down_disabled")
            if await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR SHARE"
            ):
                raise ValueError("autoscaling_placement_pinned")
            live_members = await conn.fetchval(
                """SELECT count(*) FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE a.observed_state NOT IN ('terminated','absent')
                     AND a.desired_state='present'"""
            )
            if live_members <= policy["min_hosts"]:
                raise ValueError("autoscaling_minimum_capacity")
            member = await conn.fetchrow(
                """SELECT m.*,a.observed_state,a.desired_state,a.release_committed_at
                   FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE m.allocation_id=$1 FOR UPDATE OF m,a""",
                allocation_id,
            )
            if member is None:
                raise ValueError("autoscaling_member_not_found")
            if (
                member["observed_state"] != "ready"
                or member["desired_state"] != "present"
                or member["release_committed_at"] is not None
            ):
                raise ValueError("autoscaling_member_not_retirable")
            hosts = await conn.fetch(
                """SELECT h.host,(h.slots_used+(SELECT COUNT(*) FROM agent_environment_runs r WHERE r.host=h.host AND NOT r.cleanup_complete AND r.lease_expires_at>NOW() AND r.started_at>NOW()-INTERVAL '30 minutes')) AS slots_used FROM agent_compute_nodes n
                   JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                   WHERE n.allocation_id=$1 FOR UPDATE OF h""",
                allocation_id,
            )
            if len(hosts) != 1 or any(host["slots_used"] != 0 for host in hosts):
                raise ValueError("autoscaling_member_not_idle")
            host_names = [host["host"] for host in hosts]
            if await conn.fetchval(
                "SELECT 1 FROM agent_sandboxes "
                "WHERE host=ANY($1::text[]) AND deletion_state<>'deleted' LIMIT 1",
                host_names,
            ):
                raise ValueError("autoscaling_member_not_idle")
            in_flight = await conn.fetchval(
                """SELECT 1 FROM agent_compute_operations o
                   JOIN agent_cloudlab_autoscaling_members m
                     ON m.allocation_id=o.allocation_id
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE m.state<>'released'
                     AND a.observed_state NOT IN ('terminated','absent')
                     AND o.kind IN ('acquire','retire','discard')
                     AND o.state IN ('pending','running','waiting','blocked','unknown')
                   LIMIT 1"""
            )
            if in_flight:
                raise ValueError("autoscaling_operation_in_flight")
            operation = await self._insert_operation(
                conn,
                request_id=request_id,
                fingerprint=fingerprint,
                kind="retire",
                allocation_id=allocation_id,
                parameters=parameters,
                requested_by=requested_by,
            )
            await conn.execute(
                """INSERT INTO agent_host_admission_holds
                   (host,allocation_id,purpose,source_id,scope)
                   SELECT host,$2,'retirement',$3,'all_claims'
                   FROM unnest($1::text[]) host
                   ON CONFLICT(host,purpose,source_id) DO NOTHING""",
                host_names,
                allocation_id,
                operation["id"],
            )
            await conn.execute(
                """UPDATE agent_cloudlab_autoscaling_members
                   SET state='retiring',updated_at=NOW() WHERE allocation_id=$1""",
                allocation_id,
            )
            runtime = await self._advance_autoscaling_runtime(
                conn,
                expected_generation=expected_generation,
                decision=decision,
                cooldown_seconds=cooldown_seconds,
            )
            return {"operation": operation, "runtime": runtime}

    async def update_cloudlab_autoscaling_runtime(
        self,
        *,
        expected_generation: int,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a controller observation under generation compare-and-swap."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """UPDATE agent_cloudlab_autoscaling_runtime SET
                   generation=generation+1,pressure_since=$2,idle_since=$3,
                   cooldown_until=$4,queue_pressure=$5,slot_status=$6::jsonb,
                   last_decision=$7::jsonb,
                   last_decision_at=CASE WHEN last_decision IS DISTINCT FROM $7::jsonb
                       THEN NOW() ELSE last_decision_at END,
                   blocker_code=$8,blocker_message=$9,observed_at=NOW(),updated_at=NOW()
                   WHERE id AND generation=$1 RETURNING *""",
                expected_generation,
                runtime.get("pressure_since"),
                runtime.get("idle_since"),
                runtime.get("cooldown_until"),
                runtime.get("queue_pressure", 0),
                _json(runtime.get("slot_status", [])),
                _json(runtime.get("last_decision", {})),
                runtime.get("blocker_code"),
                runtime.get("blocker_message"),
            )
            if row is None:
                raise ValueError("autoscaling_runtime_conflict")
            return self._autoscaling_runtime_view(_row(row))

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
        """Find recent operation state, including recovery by request ID."""
        async with self._pool.acquire() as conn:
            return [
                _row(r)
                for r in await conn.fetch(
                    """SELECT * FROM agent_compute_operations
                WHERE ($1::text IS NULL OR allocation_id=$1) AND ($2::text IS NULL OR request_id=$2)
                  AND ($3::text[] IS NULL OR kind=ANY($3::text[]))
                ORDER BY created_at DESC LIMIT 200""",
                    allocation_id,
                    request_id,
                    kinds,
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
            raise ValueError("operation_lease_lost")

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
        hosts = [
            row["host"]
            for row in await conn.fetch(
                """SELECT h.host FROM agent_compute_nodes n
                   JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                   WHERE n.allocation_id=$1 FOR UPDATE OF h""",
                allocation_id,
            )
        ]
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
            if row["observed_state"] in {"terminated", "absent"}:
                await self._release_terminal_autoscaling_reservation(conn, allocation_id)
                row = await conn.fetchrow(
                    "SELECT * FROM agent_compute_allocations WHERE id=$1", allocation_id
                )
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
            SET name=EXCLUDED.name,updated_at=NOW(),
                -- A registered host key belongs to this exact instance and
                -- provider observations never carry it: keep it.
                connection=CASE WHEN agent_compute_nodes.connection ? 'host_key'
                    AND NOT EXCLUDED.connection ? 'host_key'
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
            raise ValueError("resource_identity_changed")
        return _row(row)

    async def create_node_registration(
        self,
        *,
        allocation_id: str,
        token_hash: str,
        ttl_seconds: float,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> dict[str, Any]:
        """Open the one registration a new node may complete; only the hash is kept."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._fence(conn, operation_id, generation, worker_id, allocation_id)
            row = await conn.fetchrow(
                """INSERT INTO agent_compute_node_registrations
                (id,allocation_id,operation_id,token_hash,expires_at)
                VALUES ($1,$2,$3,$4,clock_timestamp()+make_interval(secs=>$5))
                RETURNING id,expires_at,created_at""",
                str(uuid4()),
                allocation_id,
                operation_id,
                token_hash,
                ttl_seconds,
            )
            return dict(row)

    async def complete_node_registration(self, *, token_hash: str, host_keys: list[str]) -> str:
        """Record a node's host keys once: registered, replayed, conflict or invalid.

        Only the newest registration of a live allocation is open. The same
        keys again get the same answer; a different set is never swapped in.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT r.id,r.allocation_id,r.host_keys,r.registered_at,
                    r.expires_at,a.desired_state,a.release_committed_at
                FROM agent_compute_node_registrations r
                JOIN agent_compute_allocations a ON a.id=r.allocation_id
                WHERE r.token_hash=$1 FOR UPDATE OF r""",
                token_hash,
            )
            if row is None:
                return "invalid"
            live = await conn.fetchval(
                "SELECT expires_at > clock_timestamp() "
                "FROM agent_compute_node_registrations WHERE id=$1",
                row["id"],
            )
            if row["registered_at"] is not None:
                return "replayed" if load_json(row["host_keys"]) == host_keys else "conflict"
            newest = await conn.fetchval(
                """SELECT id FROM agent_compute_node_registrations WHERE allocation_id=$1
                ORDER BY created_at DESC,id DESC LIMIT 1""",
                row["allocation_id"],
            )
            if (
                not live
                or row["desired_state"] != "present"
                or row["release_committed_at"] is not None
                or newest != row["id"]
            ):
                return "invalid"
            await conn.execute(
                """UPDATE agent_compute_node_registrations
                SET host_keys=$2::jsonb,registered_at=NOW() WHERE id=$1""",
                row["id"],
                _json(host_keys),
            )
            return "registered"

    async def node_registration(self, *, allocation_id: str) -> dict[str, Any] | None:
        """Read the newest registration of an allocation, without its token."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id,expires_at,host_keys,registered_at,created_at,
                    expires_at<=clock_timestamp() AS expired
                FROM agent_compute_node_registrations WHERE allocation_id=$1
                ORDER BY created_at DESC,id DESC LIMIT 1""",
                allocation_id,
            )
        if row is None:
            return None
        result = dict(row)
        result["host_keys"] = load_json(result["host_keys"]) or []
        return result

    async def record_host_key(
        self,
        *,
        node_id: str,
        host_key: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Pin the host key a node registered, for this exact node instance only."""
        async with self._pool.acquire() as conn, conn.transaction():
            await lock_compute_entry(conn, exclusive=True)
            await self._fence(conn, operation_id, generation, worker_id)
            updated = await conn.fetchval(
                """UPDATE agent_compute_nodes
                SET connection=connection || jsonb_build_object('host_key',$2::text),updated_at=NOW()
                WHERE id=$1 AND connection <> '{}'::jsonb RETURNING id""",
                node_id,
                host_key,
            )
            if updated is None:
                raise ValueError("resource_identity_changed")

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
            raise ValueError("invalid_operation_state")
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

    async def apply_cloudlab_autoscaling_slot_target(
        self,
        *,
        expected_revision: int,
        expected_generation: int,
        allocation_id: str,
        host: str | None,
        target_slots: int | None,
        capacity_sampled_at: datetime | None,
        capacity_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Apply one slot target only while its observed safety facts still hold."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Policy updates and placement pins take these locks in the same
            # order. A decision observed before either change therefore waits,
            # rechecks the committed state, and cannot clear this safety hold.
            await lock_compute_entry(conn, exclusive=True)
            policy = await conn.fetchrow(
                """SELECT revision,enabled
                   FROM agent_cloudlab_autoscaling_policy WHERE id FOR SHARE"""
            )
            runtime_generation = await conn.fetchval(
                """SELECT generation FROM agent_cloudlab_autoscaling_runtime
                   WHERE id FOR UPDATE"""
            )
            active_host = await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id"
            )
            if policy is None or policy["revision"] != expected_revision:
                return {
                    "outcome": "refused",
                    "blocker_code": "autoscaling_revision_conflict",
                }
            if runtime_generation != expected_generation:
                return {
                    "outcome": "refused",
                    "blocker_code": "autoscaling_runtime_conflict",
                }
            if not policy["enabled"]:
                return {
                    "outcome": "refused",
                    "blocker_code": "autoscaling_disabled",
                }
            if active_host is not None:
                return {
                    "outcome": "refused",
                    "blocker_code": "autoscaling_placement_pinned",
                }
            member = await conn.fetchrow(
                """SELECT m.state,a.desired_state,a.observed_state,
                          a.release_committed_at,a.scope_complete
                   FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE m.allocation_id=$1 FOR UPDATE OF m,a""",
                allocation_id,
            )
            holdable = bool(
                member is not None
                and member["state"] not in {"retiring", "released"}
                and member["desired_state"] == "present"
                and member["observed_state"] not in {"terminated", "absent", "failed"}
                and member["release_committed_at"] is None
            )
            if holdable:
                # Once the policy fence passes, reassert before validating
                # mutable member, binding, readiness, and capacity facts.
                await conn.execute(
                    """INSERT INTO agent_host_admission_holds
                       (host,allocation_id,purpose,source_id,scope)
                       SELECT h.host,$1,'autoscaling_slots',$1,'all_claims'
                       FROM agent_runner_hosts h
                       JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                       WHERE n.allocation_id=$1
                       ON CONFLICT(host,purpose,source_id) DO UPDATE
                       SET allocation_id=EXCLUDED.allocation_id,
                           scope='all_claims',
                           revision=agent_host_admission_holds.revision+1
                       WHERE agent_host_admission_holds.allocation_id
                                 IS DISTINCT FROM EXCLUDED.allocation_id
                          OR agent_host_admission_holds.scope<>'all_claims'""",
                    allocation_id,
                )

            host_row = None
            if host is not None:
                host_row = await conn.fetchrow(
                    """SELECT h.host,h.slots_total,h.slots_desired,h.status,
                              h.launch_error,h.last_heartbeat_at,
                              (h.credential_hash IS NOT NULL) AS enrolled,
                              h.capacity_sampled_at,h.capacity_snapshot,
                              NOW() AS database_now
                       FROM agent_runner_hosts h
                       JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                       WHERE h.host=$1 AND n.allocation_id=$2
                       FOR UPDATE OF h""",
                    host,
                    allocation_id,
                )

            def result(
                outcome: str,
                blocker_code: str | None = None,
                *,
                desired_override: int | None = None,
            ) -> dict[str, Any]:
                value: dict[str, Any] = {
                    "outcome": outcome,
                    "blocker_code": blocker_code,
                }
                if host_row is not None:
                    value["slots_desired"] = host_row["slots_desired"]
                    value["slots_total"] = host_row["slots_total"]
                if desired_override is not None:
                    value["slots_desired"] = desired_override
                return value

            if not holdable or member is None:
                return result("refused", "autoscaling_member_not_live")
            if member["observed_state"] != "ready" or not member["scope_complete"]:
                return result("refused", "autoscaling_member_not_ready")
            if host is None:
                return result("held")
            if host_row is None:
                return result("refused", "autoscaling_host_binding_changed")
            if target_slots is None:
                return result("held")
            heartbeat_at = host_row["last_heartbeat_at"]
            database_now = host_row["database_now"]
            if (
                host_row["status"] != "online"
                or not host_row["enrolled"]
                or bool(host_row["launch_error"])
                or heartbeat_at is None
                or (database_now - heartbeat_at).total_seconds() > FRESH_AFTER_SECONDS
            ):
                return result("refused", "autoscaling_host_not_ready")
            current_snapshot = load_json(host_row["capacity_snapshot"])
            sampled_at = host_row["capacity_sampled_at"]
            if (
                sampled_at is None
                or sampled_at != capacity_sampled_at
                or current_snapshot != capacity_snapshot
            ):
                return result("refused", "autoscaling_capacity_changed")
            if (database_now - sampled_at).total_seconds() > FRESH_AFTER_SECONDS:
                return result("refused", "autoscaling_capacity_stale")

            await conn.execute(
                "UPDATE agent_runner_hosts SET slots_desired=$2 WHERE host=$1",
                host,
                target_slots,
            )
            if host_row["slots_total"] != target_slots:
                return result(
                    "awaiting_host_apply",
                    desired_override=target_slots,
                )
            await conn.execute(
                """DELETE FROM agent_host_admission_holds
                   WHERE host=$1 AND allocation_id=$2
                     AND purpose='autoscaling_slots' AND source_id=$2""",
                host,
                allocation_id,
            )
            return result("ready", desired_override=target_slots)

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

    async def running_attempts(self, *, host: str) -> int:
        """Count the turns a Host is executing now."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT count(*) FROM agent_attempts WHERE host_id=$1 AND status='running'",
                    host,
                )
            )

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
                raise ValueError("resource_retiring")
            if not await conn.fetchval(
                """SELECT 1 FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                WHERE n.allocation_id=$1 AND h.host=$2""",
                source_allocation_id,
                allocation["slot_host"],
            ):
                raise ValueError("host_identity_conflict")
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
    ) -> dict[str, Any] | None:
        """Stop a replacement node's pending work and queue its release.

        Returns the release operation, or None while the candidate's own work is
        executing or its outcome is unknown and it cannot be stopped safely.
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
                raise ValueError("candidate_mismatch")
            existing = await self._existing_request(conn, request_id, fingerprint)
            if existing is not None:
                return existing
            if await conn.fetchval(
                """SELECT 1 FROM agent_compute_operations
                WHERE allocation_id=$1 AND state IN ('running','unknown') LIMIT 1""",
                allocation_id,
            ):
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
                raise ValueError("allocation_not_found")
            if candidate["release_committed_at"] is not None:
                return {"release_committed": True, "blocked_reasons": []}
            if candidate["predecessor_id"] is None:
                raise ValueError("not_a_candidate")
            if await conn.fetchval(
                """SELECT 1 FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                WHERE n.allocation_id=$1""",
                allocation_id,
            ):
                raise ValueError("candidate_bound")
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
                raise ValueError("host_not_found")
            if current["compute_node_id"] == to_node_id:
                return  # Replayed after a lost acknowledgement.
            if current["compute_node_id"] != from_node_id:
                raise ValueError("host_identity_conflict")
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
                raise ValueError("resource_retiring")
            if (
                target["predecessor_id"] != source["id"]
                and source["predecessor_id"] != target["id"]
            ):
                raise ValueError("host_identity_conflict")
            if await conn.fetchval(
                "SELECT 1 FROM agent_runner_hosts WHERE compute_node_id=$1 AND host<>$2",
                to_node_id,
                host,
            ):
                raise ValueError("host_identity_conflict")
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
        groups = await conn.fetch(
            "SELECT id,host,state,active_operation_count FROM agent_sandboxes "
            "WHERE host=ANY($1::text[]) AND deletion_state<>'deleted'",
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
            WHERE g.host=ANY($1::text[]) AND g.deletion_state<>'deleted'
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
        """Commit release after validation, or settle one already terminal upstream."""
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
            await conn.fetch(
                """SELECT h.host FROM agent_compute_nodes n
                   JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                   WHERE n.allocation_id=$1 FOR UPDATE OF h""",
                allocation_id,
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
        """Cancel a safe pre-submit acquisition, retirement or rotation intent.

        A rotation can be cancelled until the old node stops: its claim pause
        is lifted and its replacement node, if any, is queued for release.
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
                raise ValueError("operation_not_found")
            if row["state"] == "running":
                raise ValueError("operation_in_flight")
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
                raise ValueError("release_committed")
            operation = _row(row)
            candidate = _row(allocation) if allocation is not None else None
            if autoscaling_retirement_needs_settlement(operation, candidate):
                raise ValueError("operation_not_cancellable")
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
                        raise ValueError("operation_not_cancellable")
                    return cancelled
                raise ValueError("operation_not_cancellable")
            if row["kind"] == "acquire":
                if candidate is not None:
                    candidate["nodes"] = [True] if candidate.pop("has_nodes") else []
                if not autoscaling_acquire_can_cancel(operation, candidate):
                    raise ValueError("operation_not_cancellable")
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
                    raise ValueError("operation_not_cancellable")
                return cancelled
            if row["kind"] == "rotate":
                return await self._cancel_rotation(conn, row)
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
            raise ValueError("operation_not_cancellable")
        if result.get("source_stopping") or result.get("source_stopped") or result.get("rebound"):
            # The old node is stopped or the name has moved: only the worker's
            # own rollback can bring the Host back.
            raise ValueError("rotation_switching")
        candidate = (result.get("candidate") or {}).get("id")
        if candidate and await conn.fetchval(
            """SELECT 1 FROM agent_compute_operations
            WHERE allocation_id=$1 AND state IN ('running','unknown') LIMIT 1""",
            candidate,
        ):
            raise ValueError("operation_in_flight")
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
            await lock_compute_entry(conn, exclusive=True)
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
                    raise ValueError("operation_not_retryable")
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
                        raise ValueError("operation_not_retryable")
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
