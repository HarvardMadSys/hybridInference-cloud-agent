"""Durable CloudLab capacity control.

The browser edits one revisioned policy; this controller observes queue and
Host state from Postgres and only queues the existing durable compute
operations.  It never calls a provider directly, and every decision that can
create or retire a machine is fenced by the policy and runtime generations.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from cloud_agent.capacity_protocol import FRESH_AFTER_SECONDS
from cloud_agent.domain.compute import fingerprint
from cloud_agent.schemas_compute import CloudLabDefaults

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent.ports.compute_resources import ComputeResourceRepository
    from cloud_agent.ports.host_pool import HostPoolRepository


_GIB = 1024**3
_MIN_SLOTS = 2
_MAX_SLOTS = 128
_ACQUIRE_IN_FLIGHT = frozenset(
    {
        # Public autoscaling projection.
        "queued",
        "running",
        "retry_wait",
        # Raw durable-operation vocabulary, accepted defensively so a
        # projection change can never turn into duplicate CloudLab creates.
        "pending",
        "waiting",
    }
)
_ACQUIRE_CIRCUIT_BREAKERS = frozenset({"blocked", "unknown", "failed"})
_LIVE_MEMBER_STATES = frozenset({"provisioning", "active", "retiring", "blocked"})
_CAPACITY_MEMBER_STATES = frozenset({"provisioning", "active", "blocked"})


def desired_slots_v1(snapshot: object) -> tuple[int | None, str | None]:
    """Return the reviewed v1 concurrency limit from CPU and total RAM.

    Version 1 reserves 25% of logical CPUs, uses two logical CPUs per turn,
    reserves 8 GiB for the host, and uses 4 GiB per turn.  Both dimensions
    must support at least two slots; malformed or partial telemetry is not
    evidence for raising a host limit.
    """
    if not isinstance(snapshot, dict):
        return None, "capacity_missing"
    logical = snapshot.get("cpu_logical_count")
    memory = snapshot.get("memory_total_bytes")
    if (
        isinstance(logical, bool)
        or not isinstance(logical, int)
        or logical <= 0
        or isinstance(memory, bool)
        or not isinstance(memory, int)
        or memory <= 0
    ):
        return None, "capacity_incomplete"
    cpu_slots = logical * 3 // 8
    memory_slots = (memory - 8 * _GIB) // (4 * _GIB)
    desired = min(cpu_slots, memory_slots, _MAX_SLOTS)
    if desired < _MIN_SLOTS:
        return None, "capacity_below_minimum"
    return desired, None


def _moment(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _operation_state(member: Mapping[str, Any]) -> str | None:
    value = member.get("latest_operation_state")
    return value if isinstance(value, str) else None


def _operation_kind(member: Mapping[str, Any]) -> str | None:
    value = member.get("latest_operation_kind")
    return value if isinstance(value, str) else None


def _acquire_state(member: Mapping[str, Any]) -> str | None:
    value = member.get("latest_acquire_state")
    return value if isinstance(value, str) else None


def _seconds(policy: Mapping[str, Any], key: str) -> float:
    value = policy.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


class CloudLabAutoscaler:
    """Reconcile one durable CloudLab policy on every compute-worker poll."""

    def __init__(
        self,
        *,
        repository: ComputeResourceRepository,
        host_pool: HostPoolRepository,
        expected_deployment_sha: str | None = None,
    ) -> None:
        self.repository = repository
        self.host_pool = host_pool
        self.expected_deployment_sha = (
            os.environ.get("AGENT_DEPLOYMENT_SHA", "")
            if expected_deployment_sha is None
            else expected_deployment_sha
        ).strip()

    async def reconcile(self) -> None:
        """Persist timers/status, size ready hosts, and queue at most one action."""
        state = await self.repository.get_cloudlab_autoscaling()
        policy = state["policy"]
        runtime = state["runtime"]
        members = list(state.get("members") or [])
        observation = await self.host_pool.autoscaling_observation(
            expected_deployment_sha=self.expected_deployment_sha
        )
        now = _moment(observation.get("observed_at")) or datetime.now(timezone.utc)
        enabled = bool(policy.get("enabled"))
        active_host = observation.get("active_host")

        slot_status = await self._size_owned_hosts(
            members=members,
            hosts=list(observation.get("hosts") or []),
            now=now,
            apply=enabled and active_host is None,
            expected_policy_revision=int(policy["revision"]),
            expected_runtime_generation=int(runtime["generation"]),
        )
        runnable = max(0, int(observation.get("runnable_new_group_jobs") or 0))
        free = max(0, int(observation.get("fresh_effective_free_slots") or 0))
        unmatched = observation.get("unmatched_new_group_jobs")
        pressure = (
            max(0, int(unmatched or 0))
            if "unmatched_new_group_jobs" in observation
            else max(0, runnable - free)
        )

        template_hosts = {
            str(host) for host in observation.get("autoscaling_template_hosts") or [] if host
        }
        unresolved_slot_sizing = any(item.get("blocker_code") is not None for item in slot_status)

        pressure_since = _moment(runtime.get("pressure_since")) if pressure else None
        if pressure and pressure_since is None:
            pressure_since = now
        owned_idle = self._all_owned_capacity_idle(members, list(observation.get("hosts") or []))
        idle_since = (
            _moment(runtime.get("idle_since"))
            if runnable == 0 and pressure == 0 and owned_idle
            else None
        )
        if runnable == 0 and pressure == 0 and owned_idle and idle_since is None:
            idle_since = now
        cooldown_until = _moment(runtime.get("cooldown_until"))

        blocker_code: str | None = None
        blocker_message: str | None = None
        decision = {"action": "disabled" if not enabled else "steady"}
        actionable_members = [member for member in members if member.get("state") != "released"]
        acquire_failure = next(
            (
                member
                for member in actionable_members
                if _acquire_state(member) in _ACQUIRE_CIRCUIT_BREAKERS
            ),
            None,
        )
        acquire_in_flight = next(
            (
                member
                for member in actionable_members
                if _acquire_state(member) in _ACQUIRE_IN_FLIGHT
            ),
            None,
        )
        retire_failure = next(
            (
                member
                for member in actionable_members
                if _operation_kind(member) in {"retire", "discard"}
                and _operation_state(member) in _ACQUIRE_CIRCUIT_BREAKERS
            ),
            None,
        )
        retire_in_flight = next(
            (
                member
                for member in actionable_members
                if _operation_kind(member) == "retire"
                and _operation_state(member) in _ACQUIRE_IN_FLIGHT
            ),
            None,
        )
        in_flight = acquire_in_flight or retire_in_flight
        in_flight_kind = "acquire" if acquire_in_flight is not None else "retire"
        if enabled and active_host is not None:
            blocker_code = "placement_pinned"
            blocker_message = "Set server placement to Automatic before enabling autoscaling."
            decision = {"action": "blocked", "reason": blocker_code}
        elif enabled and acquire_failure is not None:
            operation_state = _acquire_state(acquire_failure) or "blocked"
            blocker_code = f"acquire_{operation_state}"
            blocker_message = (
                "An autoscaled acquisition needs administrator resolution before another "
                "CloudLab experiment can be created."
            )
            decision = {"action": "blocked", "reason": blocker_code}
        elif enabled and retire_failure is not None:
            operation_state = _operation_state(retire_failure) or "blocked"
            blocker_code = f"retire_{operation_state}"
            blocker_message = (
                "An autoscaled retirement needs administrator resolution before another "
                "CloudLab experiment can be created."
            )
            decision = {"action": "blocked", "reason": blocker_code}
        elif enabled and in_flight is not None:
            decision = {
                "action": f"{in_flight_kind}_in_progress",
                "allocation_id": in_flight.get("allocation_id"),
            }

        updates = {
            "pressure_since": pressure_since,
            "idle_since": idle_since,
            "cooldown_until": cooldown_until,
            "last_decision": decision,
            "blocker_code": blocker_code,
            "blocker_message": blocker_message,
            "queue_pressure": pressure,
            "slot_status": slot_status,
        }
        # This write is also the controller heartbeat shown to administrators.
        # Keep it moving even when the semantic decision is unchanged so a
        # healthy steady state cannot look like a stopped worker.
        updated = await self._update_runtime(runtime, updates)
        if updated is None:
            return
        runtime = updated

        if not enabled or blocker_code is not None or in_flight is not None:
            return

        live_members = [member for member in members if member.get("state") in _LIVE_MEMBER_STATES]
        # A member whose groups are moving out is leaving: its replacement is
        # acquired now, while it still serves, not after its lease has ended.
        capacity_members = [
            member
            for member in members
            if member.get("state") in _CAPACITY_MEMBER_STATES and not member.get("moving_out")
        ]
        minimum = max(0, int(policy.get("min_hosts") or 0))
        maximum = max(minimum, int(policy.get("max_hosts") or minimum))

        # Minimum capacity bypasses demand sustain and cooldown, but never the
        # placement/circuit/in-flight gates above. A replacement for a member
        # moving out waits for room under the maximum: that member is still live.
        if len(capacity_members) < minimum and len(live_members) < maximum:
            await self._queue_scale_up(
                policy=policy,
                runtime=runtime,
                decision="scale_up_minimum",
            )
            return
        # Pressure must not fan out a pool whose first member has not yet
        # demonstrated a safe, applied slot limit. Before any eligible owned
        # Host exists, allow exactly one probe above a zero minimum so the
        # matcher can learn the pool's capability shape.
        if pressure > 0 and unresolved_slot_sizing:
            return
        if pressure > 0 and not template_hosts and len(live_members) >= max(minimum, 1):
            return

        cooling_down = cooldown_until is not None and cooldown_until > now
        if (
            pressure > 0
            and len(live_members) < maximum
            and not cooling_down
            and pressure_since is not None
            and now - pressure_since
            >= timedelta(seconds=_seconds(policy, "scale_up_sustain_seconds"))
        ):
            await self._queue_scale_up(
                policy=policy,
                runtime=runtime,
                decision="scale_up_pressure",
            )
            return

        if (
            bool(policy.get("scale_down_enabled"))
            and len(capacity_members) > minimum
            and runnable == 0
            and pressure == 0
            and not cooling_down
            and idle_since is not None
            and now - idle_since >= timedelta(seconds=_seconds(policy, "scale_down_idle_seconds"))
        ):
            candidate = self._scale_down_candidate(members, observation.get("hosts") or [])
            if candidate is not None:
                await self._queue_scale_down(
                    policy=policy,
                    runtime=runtime,
                    allocation_id=str(candidate["allocation_id"]),
                )

    async def _size_owned_hosts(
        self,
        *,
        members: list[dict[str, Any]],
        hosts: list[dict[str, Any]],
        now: datetime,
        apply: bool,
        expected_policy_revision: int,
        expected_runtime_generation: int,
    ) -> list[dict[str, Any]]:
        by_allocation = {
            str(host["allocation_id"]): host
            for host in hosts
            if host.get("allocation_id") is not None
        }
        status: list[dict[str, Any]] = []
        for member in members:
            if member.get("state") not in _CAPACITY_MEMBER_STATES:
                continue
            allocation_id = str(member["allocation_id"])
            host = by_allocation.get(allocation_id)
            if host is None:
                if apply:
                    fenced = await self.repository.apply_cloudlab_autoscaling_slot_target(
                        expected_revision=expected_policy_revision,
                        expected_generation=expected_runtime_generation,
                        allocation_id=allocation_id,
                        host=None,
                        target_slots=None,
                        capacity_sampled_at=None,
                        capacity_snapshot=None,
                    )
                    missing_blocker = fenced.get("blocker_code") or "host_not_ready"
                else:
                    missing_blocker = "host_not_ready"
                status.append(
                    {
                        "allocation_id": allocation_id,
                        "host": member.get("host"),
                        "desired_slots": member.get("slots_desired"),
                        "applied_slots": member.get("slots_total"),
                        "blocker_code": missing_blocker,
                    }
                )
                continue
            host_name = str(host["host"])
            desired = host.get("slots_desired")
            applied = host.get("slots_total")
            blocker: str | None = None
            target: int | None = None
            sampled_at = _moment(host.get("capacity_sampled_at"))
            heartbeat_at = _moment(host.get("last_heartbeat_at"))
            if host.get("status") != "online" or not host.get("enrolled"):
                blocker = "host_not_ready"
            elif host.get("launch_error"):
                blocker = "host_launch_error"
            elif heartbeat_at is None or (now - heartbeat_at).total_seconds() > FRESH_AFTER_SECONDS:
                blocker = "host_heartbeat_stale"
            elif sampled_at is None:
                blocker = "capacity_missing"
            elif (now - sampled_at).total_seconds() > FRESH_AFTER_SECONDS:
                blocker = "capacity_stale"
            else:
                target, blocker = desired_slots_v1(host.get("capacity_snapshot"))

            if not apply and blocker is None:
                blocker = "autoscaling_inactive"
            if apply:
                # The repository reasserts the all-claims hold before it
                # changes the desired limit and clears only after rechecking
                # this exact policy/runtime, binding, and capacity sample.
                fenced = await self.repository.apply_cloudlab_autoscaling_slot_target(
                    expected_revision=expected_policy_revision,
                    expected_generation=expected_runtime_generation,
                    allocation_id=allocation_id,
                    host=host_name,
                    target_slots=target if blocker is None else None,
                    capacity_sampled_at=sampled_at,
                    capacity_snapshot=(
                        host.get("capacity_snapshot")
                        if isinstance(host.get("capacity_snapshot"), dict)
                        else None
                    ),
                )
                desired = fenced.get("slots_desired", desired)
                applied = fenced.get("slots_total", applied)
                if fenced.get("blocker_code") is not None:
                    blocker = str(fenced["blocker_code"])
                elif blocker is None and fenced.get("outcome") == "awaiting_host_apply":
                    blocker = "awaiting_host_apply"
                elif blocker is None and fenced.get("outcome") != "ready":
                    blocker = "autoscaling_slot_fence_refused"
            status.append(
                {
                    "allocation_id": allocation_id,
                    "host": host_name,
                    "desired_slots": desired,
                    "applied_slots": applied,
                    "blocker_code": blocker,
                }
            )
        return sorted(status, key=lambda item: (item["allocation_id"], item.get("host") or ""))

    @staticmethod
    def _all_owned_capacity_idle(
        members: list[dict[str, Any]], hosts: list[dict[str, Any]]
    ) -> bool:
        by_allocation = {
            str(host["allocation_id"]): host
            for host in hosts
            if host.get("allocation_id") is not None
        }
        for member in members:
            if member.get("state") not in _CAPACITY_MEMBER_STATES:
                continue
            host = by_allocation.get(str(member["allocation_id"]))
            if (
                host is None
                or int(host.get("slots_used") or 0) != 0
                or int(host.get("retained_groups") or 0) != 0
            ):
                return False
        return True

    @staticmethod
    def _scale_down_candidate(
        members: list[dict[str, Any]], hosts: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        active = {
            str(member["allocation_id"]): member
            for member in members
            if member.get("state") == "active"
            and _operation_state(member) not in _ACQUIRE_IN_FLIGHT
        }
        candidates = [
            host
            for host in hosts
            if str(host.get("allocation_id")) in active
            and not bool(host.get("drain_fence_active"))
            and int(host.get("slots_used") or 0) == 0
            and int(host.get("retained_groups") or 0) == 0
        ]
        if not candidates:
            return None
        # Shed the newest idle member first, preserving older stable capacity.
        candidates.sort(
            key=lambda host: (
                _moment(active[str(host["allocation_id"])].get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )
        return candidates[0]

    async def _queue_scale_up(
        self,
        *,
        policy: Mapping[str, Any],
        runtime: Mapping[str, Any],
        decision: str,
    ) -> None:
        policy_revision = int(policy["revision"])
        generation = int(runtime["generation"])
        request_id = f"autoscale:cloudlab:up:{policy_revision}:{generation}"
        effective_configuration = dict(policy.get("configuration") or {})
        if (
            "node_types" not in effective_configuration
            or "node_type_binding" not in effective_configuration
        ):
            defaults = CloudLabDefaults.model_validate(
                await self.repository.provider_defaults("cloudlab")
            )
            effective_configuration.setdefault("node_types", list(defaults.node_types))
            effective_configuration.setdefault("node_type_binding", defaults.node_type_binding)
        # Freeze effective preferences in each newly queued operation. Changes
        # to provider defaults affect the next allocation, never one in flight.
        supplied = dict(effective_configuration)
        # CloudLab experiment names must be unique within a project. Omitting
        # it lets the existing adapter derive one from the new allocation id.
        supplied.pop("name", None)
        supplied["credential_ref"] = policy["connection_id"]
        ssh_connection_id = policy.get("ssh_connection_id")
        if ssh_connection_id:
            supplied["ssh_credential_ref"] = ssh_connection_id
            if supplied.get("host_key_source") == "known_hosts":
                supplied["ssh_known_hosts_ref"] = f"{ssh_connection_id}:known_hosts"
            else:
                supplied.pop("ssh_known_hosts_ref", None)
        parameters = {
            "trigger": "autoscaler",
            "policy_revision": policy_revision,
            "runtime_generation": generation,
            "provider_id": "cloudlab",
            "mode": "provision",
            "connection_id": policy["connection_id"],
            "ssh_connection_id": ssh_connection_id,
            "configuration": effective_configuration,
            "deployment_profiles": list(policy.get("deployment_profiles") or []),
            "sandbox_storage_path": policy.get("sandbox_storage_path"),
            "policy": policy.get("renewal_policy") or {},
        }
        allocation = {
            "name": f"CloudLab autoscale {generation}",
            "provider_id": "cloudlab",
            "mode": "provision",
            "connection_id": policy["connection_id"],
            "ssh_connection_id": ssh_connection_id,
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
            "configuration": supplied,
            "deployment_profile": {},
            "policy": policy.get("renewal_policy") or {},
        }
        try:
            await self.repository.create_autoscale_allocation_operation(
                expected_revision=policy_revision,
                expected_generation=generation,
                request_id=request_id,
                fingerprint=fingerprint("acquire", parameters),
                allocation=allocation,
                parameters=parameters,
                requested_by="system:cloudlab-autoscaler",
                decision={"action": decision},
                cooldown_seconds=int(_seconds(policy, "cooldown_seconds")),
            )
        except ValueError as exc:
            await self._record_action_refusal(runtime, str(exc))

    async def _queue_scale_down(
        self,
        *,
        policy: Mapping[str, Any],
        runtime: Mapping[str, Any],
        allocation_id: str,
    ) -> None:
        policy_revision = int(policy["revision"])
        generation = int(runtime["generation"])
        request_id = f"autoscale:cloudlab:down:{policy_revision}:{generation}"
        parameters = {
            "allocation_id": allocation_id,
            "trigger": "autoscaler",
            "policy_revision": policy_revision,
            "runtime_generation": generation,
        }
        try:
            await self.repository.queue_autoscale_retire(
                expected_revision=policy_revision,
                expected_generation=generation,
                allocation_id=allocation_id,
                request_id=request_id,
                fingerprint=fingerprint("retire", parameters),
                parameters=parameters,
                requested_by="system:cloudlab-autoscaler",
                decision={
                    "action": "scale_down_idle",
                    "allocation_id": allocation_id,
                },
                cooldown_seconds=int(_seconds(policy, "cooldown_seconds")),
            )
        except ValueError as exc:
            await self._record_action_refusal(runtime, str(exc))

    async def _record_action_refusal(self, runtime: Mapping[str, Any], raw_code: str) -> None:
        if raw_code in {
            "autoscaling_revision_conflict",
            "autoscaling_generation_conflict",
            "autoscaling_runtime_conflict",
            "revision_conflict",
            "generation_conflict",
        }:
            return
        code = re.sub(r"[^a-z0-9_]+", "_", raw_code.lower()).strip("_")[:64]
        if not code:
            code = "action_refused"
        await self._update_runtime(
            runtime,
            {
                "pressure_since": _moment(runtime.get("pressure_since")),
                "idle_since": _moment(runtime.get("idle_since")),
                "cooldown_until": _moment(runtime.get("cooldown_until")),
                "last_decision": {"action": "blocked", "reason": code},
                "blocker_code": code,
                "blocker_message": (
                    "The autoscaling action was refused by the current durable policy or "
                    "resource inventory."
                ),
                "queue_pressure": int(runtime.get("queue_pressure") or 0),
                "slot_status": list(runtime.get("slot_status") or []),
            },
        )

    async def _update_runtime(
        self, runtime: Mapping[str, Any], updates: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        try:
            result = await self.repository.update_cloudlab_autoscaling_runtime(
                expected_generation=int(runtime["generation"]),
                runtime=dict(updates),
            )
        except ValueError as exc:
            if str(exc) in {
                "autoscaling_generation_conflict",
                "autoscaling_runtime_conflict",
                "generation_conflict",
                "revision_conflict",
            }:
                return None
            raise
        if result is None:
            return None
        nested = result.get("runtime") if isinstance(result, dict) else None
        return nested if isinstance(nested, dict) else result


__all__ = ["CloudLabAutoscaler", "desired_slots_v1"]
