"""CloudLab autoscaling: policy, runtime and slot targets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.domain.compute import move_out_window_seconds
from cloud_agent.domain.sandbox_group import DeletionState
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.schemas.capacity_protocol import FRESH_AFTER_SECONDS
from cloud_agent.storage._sql import environment_runs
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase, _json, _row

if TYPE_CHECKING:
    from datetime import datetime


def _in_move_out_window(member: dict[str, Any]) -> bool:
    """Whether a member's lease has entered the window in which its groups move out."""
    expires = member.get("expires_at")
    if expires is None or not member.get("expiration_known"):
        return False
    window = move_out_window_seconds(load_json(member.get("renewal_policy")) or {})
    remaining = (expires - member["observed_now"]).total_seconds()
    return window > 0 and remaining <= window


class AutoscalingMixin(ComputeRepositoryBase):
    """The CloudLab autoscaling policy, its runtime, and the operations it queues."""

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
                      a.release_committed_at,a.expires_at,a.expiration_known,
                      a.policy AS renewal_policy,NOW() AS observed_now,
                      h.host,h.slots_total,h.slots_desired,
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
                    "moving_out": _in_move_out_window(member),
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

    async def update_cloudlab_autoscaling(  # noqa: PLR0912 complexity baseline, #771
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
                raise StorageConflict(ConflictCode.RECORD_NOT_FOUND)
            current = _row(current)
            if current["revision"] != expected_revision:
                raise StorageConflict(ConflictCode.REVISION_CONFLICT)
            pause_existing = bool(
                current["enabled"]
                and not policy["enabled"]
                and all(
                    current.get(key) == value for key, value in policy.items() if key != "enabled"
                )
            )
            if not pause_existing and set(profile_snapshots) != set(selected):
                raise StorageConflict(ConflictCode.AUTOSCALING_PROFILE_UNAVAILABLE)
            if policy["enabled"] and await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id"
            ):
                raise StorageConflict(ConflictCode.AUTOSCALING_PLACEMENT_PINNED)
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
                    raise StorageConflict(ConflictCode.AUTOSCALING_CONNECTION_UNVERIFIED)
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
                    raise StorageConflict(ConflictCode.AUTOSCALING_PROFILE_IN_USE)
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
                raise StorageConflict(ConflictCode.REVISION_CONFLICT)
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
            raise StorageConflict(ConflictCode.AUTOSCALING_RUNTIME_CONFLICT)
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
                raise StorageConflict(ConflictCode.REVISION_CONFLICT)
            if runtime_generation != expected_generation:
                raise StorageConflict(ConflictCode.AUTOSCALING_RUNTIME_CONFLICT)
            if not policy["enabled"]:
                raise StorageConflict(ConflictCode.AUTOSCALING_DISABLED)
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
                    raise StorageConflict(ConflictCode.AUTOSCALING_CONNECTION_UNVERIFIED)
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
                    raise StorageConflict(ConflictCode.AUTOSCALING_ACQUIRE_BLOCKED)
                raise StorageConflict(ConflictCode.AUTOSCALING_OPERATION_IN_FLIGHT)
            member_count = await conn.fetchval(
                """SELECT count(*) FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE a.observed_state NOT IN ('terminated','absent')"""
            )
            if member_count >= policy["max_hosts"]:
                raise StorageConflict(ConflictCode.AUTOSCALING_AT_CAPACITY)
            reservation = await conn.fetchrow(
                """SELECT r.* FROM unnest($1::text[]) WITH ORDINALITY wanted(id,position)
                   JOIN agent_cloudlab_autoscaling_profile_reservations r
                     ON r.deployment_profile=wanted.id
                   WHERE r.allocation_id IS NULL
                   ORDER BY wanted.position LIMIT 1 FOR UPDATE OF r""",
                policy["deployment_profiles"],
            )
            if reservation is None:
                raise StorageConflict(ConflictCode.AUTOSCALING_PROFILE_UNAVAILABLE)
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
                raise StorageConflict(ConflictCode.AUTOSCALING_PLACEMENT_PINNED)
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
                raise StorageConflict(ConflictCode.AUTOSCALING_MEMBER_NOT_RETIRABLE)
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
                    raise StorageConflict(ConflictCode.AUTOSCALING_CONNECTION_UNVERIFIED)

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
                raise StorageConflict(ConflictCode.REVISION_CONFLICT)
            if runtime_generation != expected_generation:
                raise StorageConflict(ConflictCode.AUTOSCALING_RUNTIME_CONFLICT)
            if not policy["enabled"] or not policy["scale_down_enabled"]:
                raise StorageConflict(ConflictCode.AUTOSCALING_SCALE_DOWN_DISABLED)
            if await conn.fetchval(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR SHARE"
            ):
                raise StorageConflict(ConflictCode.AUTOSCALING_PLACEMENT_PINNED)
            live_members = await conn.fetchval(
                """SELECT count(*) FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE a.observed_state NOT IN ('terminated','absent')
                     AND a.desired_state='present'"""
            )
            if live_members <= policy["min_hosts"]:
                raise StorageConflict(ConflictCode.AUTOSCALING_MINIMUM_CAPACITY)
            member = await conn.fetchrow(
                """SELECT m.*,a.observed_state,a.desired_state,a.release_committed_at
                   FROM agent_cloudlab_autoscaling_members m
                   JOIN agent_compute_allocations a ON a.id=m.allocation_id
                   WHERE m.allocation_id=$1 FOR UPDATE OF m,a""",
                allocation_id,
            )
            if member is None:
                raise StorageConflict(ConflictCode.AUTOSCALING_MEMBER_NOT_FOUND)
            if (
                member["observed_state"] != "ready"
                or member["desired_state"] != "present"
                or member["release_committed_at"] is not None
            ):
                raise StorageConflict(ConflictCode.AUTOSCALING_MEMBER_NOT_RETIRABLE)
            hosts = await conn.fetch(
                f"""SELECT h.host,(h.slots_used+{environment_runs.env_runs_holding_slots("h.host")}) AS slots_used
                   FROM agent_compute_nodes n JOIN agent_runner_hosts h ON h.compute_node_id=n.id WHERE n.allocation_id=$1 FOR UPDATE OF h""",
                allocation_id,
            )
            if len(hosts) != 1 or any(host["slots_used"] != 0 for host in hosts):
                raise StorageConflict(ConflictCode.AUTOSCALING_MEMBER_NOT_IDLE)
            host_names = [host["host"] for host in hosts]
            if await conn.fetchval(
                "SELECT 1 FROM agent_sandboxes "
                f"WHERE host=ANY($1::text[]) AND deletion_state<>{DeletionState.DELETED.sql} LIMIT 1",
                host_names,
            ):
                raise StorageConflict(ConflictCode.AUTOSCALING_MEMBER_NOT_IDLE)
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
                raise StorageConflict(ConflictCode.AUTOSCALING_OPERATION_IN_FLIGHT)
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
                raise StorageConflict(ConflictCode.AUTOSCALING_RUNTIME_CONFLICT)
            return self._autoscaling_runtime_view(_row(row))

    async def apply_cloudlab_autoscaling_slot_target(  # noqa: C901, PLR0912 complexity baseline, #771
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
