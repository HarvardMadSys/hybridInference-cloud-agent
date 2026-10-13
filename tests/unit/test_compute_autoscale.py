"""The compute autoscaler is a fenced state machine, not a browser loop."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import pytest

from cloud_agent.application.compute_autoscale import (
    CloudLabAutoscaler,
    desired_slots_v1,
)
from cloud_agent.domain.cloudlab import DEFAULT_CLOUDLAB_NODE_TYPES
from cloud_agent.domain.compute import fingerprint

if TYPE_CHECKING:
    from cloud_agent.ports.compute_resources import ComputeResourceRepository
    from cloud_agent.ports.host_pool import HostPoolRepository

NOW = datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)
GIB = 1024**3


def policy(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "revision": 4,
        "enabled": True,
        "min_hosts": 0,
        "max_hosts": 2,
        "connection_id": "cloudlab-connection",
        "ssh_connection_id": "ssh-connection",
        "configuration": {
            "project": "staging",
            "profile_project": "cloud-agent",
            "profile_name": "cloud-agent-node",
            "duration": 4,
            "host_key_source": "registration",
        },
        "deployment_profiles": ["profile-a", "profile-b", "rotation-spare"],
        "sandbox_storage_path": None,
        "renewal_policy": {"enabled": False},
        "slot_policy": {"formula_version": 1},
        "scale_up_sustain_seconds": 120,
        "scale_down_idle_seconds": 600,
        "cooldown_seconds": 300,
        "scale_down_enabled": False,
    }
    value.update(changes)
    return value


def runtime(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "generation": 0,
        "pressure_since": None,
        "idle_since": None,
        "cooldown_until": None,
        "queue_pressure": 0,
        "slot_status": [],
        "last_decision": {},
        "blocker_code": None,
        "blocker_message": None,
        "observed_at": NOW,
        "updated_at": NOW,
    }
    value.update(changes)
    return value


def member(
    allocation_id: str,
    *,
    state: str = "active",
    operation_state: str = "succeeded",
    operation_kind: str = "acquire",
    acquire_state: str | None = None,
    created_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "allocation_id": allocation_id,
        "deployment_profile": f"profile-{allocation_id}",
        "state": state,
        "latest_operation_id": f"operation-{allocation_id}",
        "latest_operation_kind": operation_kind,
        "latest_operation_state": operation_state,
        "latest_operation_message": None,
        "latest_acquire_state": operation_state if acquire_state is None else acquire_state,
        "host": f"host-{allocation_id}",
        "slots_total": 6,
        "slots_desired": 6,
        "created_at": created_at,
        "updated_at": created_at,
    }


def host(
    allocation_id: str,
    *,
    slots_used: int = 0,
    retained_groups: int = 0,
    slots_total: int = 6,
    slots_desired: int | None = 6,
    sampled_at: datetime | None = NOW,
    heartbeat_at: datetime | None = NOW,
    launch_error: str | None = None,
    drain_fence_active: bool = False,
    snapshot: object | None = None,
) -> dict[str, Any]:
    return {
        "allocation_id": allocation_id,
        "host": f"host-{allocation_id}",
        "status": "online",
        "enrolled": True,
        "last_heartbeat_at": heartbeat_at,
        "launch_error": launch_error,
        "drain_fence_active": drain_fence_active,
        "slots_used": slots_used,
        "slots_total": slots_total,
        "slots_desired": slots_desired,
        "retained_groups": retained_groups,
        "capacity_sampled_at": sampled_at,
        "capacity_snapshot": snapshot
        if snapshot is not None
        else {"cpu_logical_count": 16, "memory_total_bytes": 64 * GIB},
    }


class Repository:
    def __init__(
        self,
        *,
        configured_policy: dict[str, Any],
        configured_runtime: dict[str, Any],
        members: list[dict[str, Any]],
        slot_results: dict[str, dict[str, Any]] | None = None,
        cloudlab_defaults: dict[str, Any] | None = None,
    ) -> None:
        self.state: dict[str, Any] = {
            "policy": configured_policy,
            "runtime": configured_runtime,
            "members": members,
            "profile_reservations": [],
        }
        self.runtime_updates: list[dict[str, Any]] = []
        self.scale_ups: list[dict[str, Any]] = []
        self.scale_downs: list[dict[str, Any]] = []
        self.slot_target_writes: list[dict[str, Any]] = []
        self.slot_results = slot_results or {}
        self.cloudlab_defaults = cloudlab_defaults or {}

    async def provider_defaults(self, provider_id: str) -> dict[str, Any]:
        assert provider_id == "cloudlab"
        return deepcopy(self.cloudlab_defaults)

    async def get_cloudlab_autoscaling(self) -> dict[str, Any]:
        return deepcopy(self.state)

    async def update_cloudlab_autoscaling_runtime(
        self, *, expected_generation: int, runtime: dict[str, Any]
    ) -> dict[str, Any]:
        assert expected_generation == self.state["runtime"]["generation"]
        self.runtime_updates.append(deepcopy(runtime))
        updated = {
            **self.state["runtime"],
            **runtime,
            "generation": expected_generation + 1,
            "observed_at": NOW,
            "updated_at": NOW,
        }
        self.state["runtime"] = updated
        return deepcopy(updated)

    async def create_autoscale_allocation_operation(self, **values: Any) -> dict[str, Any]:
        assert values["expected_generation"] == self.state["runtime"]["generation"]
        self.scale_ups.append(deepcopy(values))
        return {"operation": {"id": "up"}, "runtime": self.state["runtime"]}

    async def queue_autoscale_retire(self, **values: Any) -> dict[str, Any]:
        assert values["expected_generation"] == self.state["runtime"]["generation"]
        self.scale_downs.append(deepcopy(values))
        return {"operation": {"id": "down"}, "runtime": self.state["runtime"]}

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
        values = {
            "expected_revision": expected_revision,
            "expected_generation": expected_generation,
            "allocation_id": allocation_id,
            "host": host,
            "target_slots": target_slots,
            "capacity_sampled_at": capacity_sampled_at,
            "capacity_snapshot": capacity_snapshot,
        }
        self.slot_target_writes.append(deepcopy(values))
        result = deepcopy(self.slot_results.get(allocation_id))
        if result is not None:
            return result
        if target_slots is None:
            return {"outcome": "held", "blocker_code": None}
        return {
            "outcome": "ready",
            "blocker_code": None,
            "slots_desired": target_slots,
            "slots_total": target_slots,
        }


class HostPool:
    def __init__(
        self,
        *,
        hosts: list[dict[str, Any]] | None = None,
        runnable: int = 0,
        free: int = 0,
        unmatched: int | None = None,
        templates: list[str] | None = None,
        active_host: str | None = None,
    ) -> None:
        self.hosts = hosts or []
        self.runnable = runnable
        self.free = free
        self.unmatched = unmatched
        self.templates = (
            [str(item["host"]) for item in self.hosts] if templates is None else templates
        )
        self.active_host = active_host
        self.slot_writes: list[tuple[str, int | None]] = []

    async def autoscaling_observation(self, *, expected_deployment_sha: str) -> dict[str, Any]:
        assert expected_deployment_sha == "deployment-sha"
        observation: dict[str, Any] = {
            "observed_at": NOW,
            "active_host": self.active_host,
            "runnable_new_group_jobs": self.runnable,
            "fresh_effective_free_slots": self.free,
            "autoscaling_template_hosts": list(self.templates),
            "hosts": deepcopy(self.hosts),
        }
        if self.unmatched is not None:
            observation["unmatched_new_group_jobs"] = self.unmatched
        return observation

    async def set_host_slots_desired(self, *, host: str, slots: int | None) -> str:
        self.slot_writes.append((host, slots))
        return "ok"


def autoscaler(repository: Repository, hosts: HostPool) -> CloudLabAutoscaler:
    return CloudLabAutoscaler(
        repository=cast("ComputeResourceRepository", repository),
        host_pool=cast("HostPoolRepository", hosts),
        expected_deployment_sha="deployment-sha",
    )


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"cpu_logical_count": 16, "memory_total_bytes": 64 * GIB}, (6, None)),
        (
            {"cpu_logical_count": 512, "memory_total_bytes": 1024 * GIB},
            (128, None),
        ),
        (
            {"cpu_logical_count": 4, "memory_total_bytes": 64 * GIB},
            (None, "capacity_below_minimum"),
        ),
        ({"cpu_logical_count": 16}, (None, "capacity_incomplete")),
        (None, (None, "capacity_missing")),
    ],
)
def test_slot_formula_v1(snapshot: object, expected: tuple[int | None, str | None]) -> None:
    assert desired_slots_v1(snapshot) == expected


@pytest.mark.asyncio
async def test_below_minimum_queues_one_acquire_immediately() -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=1),
        configured_runtime=runtime(),
        members=[],
    )
    pool = HostPool()

    await autoscaler(repository, pool).reconcile()

    assert len(repository.scale_ups) == 1
    queued = repository.scale_ups[0]
    assert queued["decision"] == {"action": "scale_up_minimum"}
    assert queued["expected_generation"] == 1
    assert queued["allocation"]["configuration"]["host_key_source"] == "registration"
    assert queued["allocation"]["configuration"]["node_types"] == list(DEFAULT_CLOUDLAB_NODE_TYPES)
    assert queued["parameters"]["configuration"]["node_types"] == list(DEFAULT_CLOUDLAB_NODE_TYPES)
    assert queued["parameters"]["configuration"]["node_type_binding"] == "phystype"
    assert queued["fingerprint"] == fingerprint("acquire", queued["parameters"])


@pytest.mark.asyncio
async def test_existing_policy_snapshots_current_defaults_for_each_new_allocation() -> None:
    configured_policy = policy(min_hosts=1)
    repository = Repository(
        configured_policy=configured_policy,
        configured_runtime=runtime(),
        members=[],
        cloudlab_defaults={"node_types": ["r650", "c6620"], "node_type_binding": "hardware"},
    )
    controller = autoscaler(repository, HostPool())

    await controller.reconcile()
    repository.cloudlab_defaults = {
        "node_types": ["c6620", "r6615"],
        "node_type_binding": "nodeType",
    }
    await controller.reconcile()

    assert len(repository.scale_ups) == 2
    first, second = repository.scale_ups
    for snapshot_name in ("allocation", "parameters"):
        assert first[snapshot_name]["configuration"]["node_types"] == ["r650", "c6620"]
        assert first[snapshot_name]["configuration"]["node_type_binding"] == "hardware"
        assert second[snapshot_name]["configuration"]["node_types"] == ["c6620", "r6615"]
        assert second[snapshot_name]["configuration"]["node_type_binding"] == "nodeType"
    assert "node_types" not in configured_policy["configuration"]
    assert first["fingerprint"] == fingerprint("acquire", first["parameters"])
    assert second["fingerprint"] == fingerprint("acquire", second["parameters"])


@pytest.mark.asyncio
@pytest.mark.parametrize("node_types", [["r650"], []])
async def test_explicit_policy_preferences_override_saved_defaults(node_types: list[str]) -> None:
    configuration = {
        **policy()["configuration"],
        "node_types": node_types,
        "node_type_binding": "customType",
        "bindings": {"phystype": "c6525-25g"},
    }
    repository = Repository(
        configured_policy=policy(min_hosts=1, configuration=configuration),
        configured_runtime=runtime(),
        members=[],
        cloudlab_defaults={"node_types": ["c6620"], "node_type_binding": "hardware"},
    )

    await autoscaler(repository, HostPool()).reconcile()

    assert len(repository.scale_ups) == 1
    queued = repository.scale_ups[0]
    for snapshot_name in ("allocation", "parameters"):
        supplied = queued[snapshot_name]["configuration"]
        assert supplied["node_types"] == node_types
        assert supplied["node_type_binding"] == "customType"
        assert supplied["bindings"] == {"phystype": "c6525-25g"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "provisioning"])
async def test_cancelled_retire_member_still_counts_toward_minimum(state: str) -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=1),
        configured_runtime=runtime(),
        members=[
            member(
                "a",
                state=state,
                operation_kind="retire",
                operation_state="cancelled",
                acquire_state="succeeded",
            )
        ],
    )
    pool = HostPool(hosts=[host("a")] if state == "active" else [])

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []


@pytest.mark.asyncio
async def test_steady_state_still_persists_a_controller_heartbeat() -> None:
    repository = Repository(
        configured_policy=policy(enabled=False),
        configured_runtime=runtime(last_decision={"action": "disabled"}),
        members=[],
    )
    pool = HostPool()

    await autoscaler(repository, pool).reconcile()
    await autoscaler(repository, pool).reconcile()

    assert len(repository.runtime_updates) == 2
    assert repository.state["runtime"]["generation"] == 2


@pytest.mark.asyncio
async def test_pressure_must_be_sustained_before_scale_up() -> None:
    repository = Repository(
        configured_policy=policy(scale_up_sustain_seconds=120),
        configured_runtime=runtime(),
        members=[member("a")],
    )
    pool = HostPool(hosts=[host("a", slots_used=6)], runnable=1, free=0)

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []
    assert repository.state["runtime"]["pressure_since"] == NOW
    assert repository.state["runtime"]["queue_pressure"] == 1


@pytest.mark.asyncio
async def test_sustained_pressure_scales_one_at_a_time() -> None:
    repository = Repository(
        configured_policy=policy(scale_up_sustain_seconds=120),
        configured_runtime=runtime(pressure_since=NOW - timedelta(seconds=121)),
        members=[member("a")],
    )
    pool = HostPool(hosts=[host("a", slots_used=6)], runnable=1, free=0)

    await autoscaler(repository, pool).reconcile()

    assert len(repository.scale_ups) == 1
    assert repository.scale_ups[0]["decision"] == {"action": "scale_up_pressure"}


@pytest.mark.asyncio
async def test_pressure_allows_only_one_probe_before_a_template_exists() -> None:
    repository = Repository(
        configured_policy=policy(max_hosts=3, scale_up_sustain_seconds=0),
        configured_runtime=runtime(pressure_since=NOW),
        members=[],
    )
    pool = HostPool(runnable=1, unmatched=1, templates=[])

    await autoscaler(repository, pool).reconcile()

    assert len(repository.scale_ups) == 1

    repository = Repository(
        configured_policy=policy(max_hosts=3, scale_up_sustain_seconds=0),
        configured_runtime=runtime(pressure_since=NOW),
        members=[member("probe", state="provisioning")],
    )
    pool = HostPool(hosts=[host("probe")], runnable=1, unmatched=1, templates=[])

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []


@pytest.mark.asyncio
async def test_unresolved_slot_sizing_blocks_pressure_but_not_minimum_recovery() -> None:
    stale = host("a", sampled_at=NOW - timedelta(seconds=91))
    repository = Repository(
        configured_policy=policy(min_hosts=1, max_hosts=3, scale_up_sustain_seconds=0),
        configured_runtime=runtime(pressure_since=NOW),
        members=[member("a")],
    )
    pool = HostPool(
        hosts=[stale],
        runnable=1,
        unmatched=1,
        templates=["host-a"],
    )

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []

    repository = Repository(
        configured_policy=policy(min_hosts=2, max_hosts=3, scale_up_sustain_seconds=0),
        configured_runtime=runtime(pressure_since=NOW),
        members=[member("a")],
    )
    pool = HostPool(hosts=[stale], runnable=1, unmatched=1, templates=[])

    await autoscaler(repository, pool).reconcile()

    assert len(repository.scale_ups) == 1
    assert repository.scale_ups[0]["decision"] == {"action": "scale_up_minimum"}


@pytest.mark.parametrize(
    "operation_state", ["queued", "pending", "running", "retry_wait", "waiting"]
)
@pytest.mark.asyncio
async def test_an_acquire_in_flight_prevents_a_duplicate(operation_state: str) -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=2),
        configured_runtime=runtime(),
        members=[
            member(
                "a",
                state="provisioning",
                operation_state=operation_state,
            )
        ],
    )
    pool = HostPool()

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []
    assert repository.state["runtime"]["last_decision"]["action"] == "acquire_in_progress"


@pytest.mark.parametrize("operation_state", ["blocked", "unknown", "failed"])
@pytest.mark.asyncio
async def test_failed_acquisition_opens_the_circuit(operation_state: str) -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=2),
        configured_runtime=runtime(),
        members=[
            member(
                "a",
                state="blocked",
                operation_state=operation_state,
            )
        ],
    )
    pool = HostPool()

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []
    assert repository.state["runtime"]["blocker_code"] == f"acquire_{operation_state}"


@pytest.mark.parametrize("operation_state", ["blocked", "unknown", "failed"])
@pytest.mark.asyncio
async def test_failed_retirement_opens_the_circuit(operation_state: str) -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=1),
        configured_runtime=runtime(),
        members=[
            member(
                "a",
                state="blocked",
                operation_kind="retire",
                operation_state=operation_state,
                acquire_state="succeeded",
            )
        ],
    )
    pool = HostPool()

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []
    assert repository.scale_downs == []
    assert repository.state["runtime"]["blocker_code"] == f"retire_{operation_state}"


@pytest.mark.asyncio
async def test_newer_inspection_cannot_hide_a_failed_acquisition() -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=2),
        configured_runtime=runtime(),
        members=[
            member(
                "a",
                state="blocked",
                operation_kind="inspect",
                operation_state="succeeded",
                acquire_state="unknown",
            )
        ],
    )
    pool = HostPool()

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []
    assert repository.state["runtime"]["blocker_code"] == "acquire_unknown"


@pytest.mark.asyncio
async def test_released_history_does_not_hold_the_acquire_circuit_open() -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=1),
        configured_runtime=runtime(),
        members=[
            member(
                "failed",
                state="released",
                operation_state="succeeded",
                acquire_state="failed",
            ),
            member("acquiring", state="released", operation_state="pending"),
            member(
                "retiring",
                state="released",
                operation_kind="retire",
                operation_state="pending",
                acquire_state="succeeded",
            ),
        ],
    )
    pool = HostPool()

    await autoscaler(repository, pool).reconcile()

    assert len(repository.scale_ups) == 1
    assert repository.scale_ups[0]["decision"] == {"action": "scale_up_minimum"}


@pytest.mark.asyncio
async def test_unmatched_jobs_take_precedence_over_global_free_slots() -> None:
    repository = Repository(
        configured_policy=policy(scale_up_sustain_seconds=120),
        configured_runtime=runtime(pressure_since=NOW - timedelta(seconds=121)),
        members=[member("a")],
    )
    pool = HostPool(hosts=[host("a")], runnable=1, free=20, unmatched=1)

    await autoscaler(repository, pool).reconcile()

    assert repository.state["runtime"]["queue_pressure"] == 1
    assert len(repository.scale_ups) == 1
    assert repository.scale_ups[0]["decision"] == {"action": "scale_up_pressure"}


@pytest.mark.asyncio
async def test_fresh_capacity_sets_and_reports_the_v1_limit() -> None:
    repository = Repository(
        configured_policy=policy(),
        configured_runtime=runtime(),
        members=[member("a")],
        slot_results={
            "a": {
                "outcome": "awaiting_host_apply",
                "blocker_code": None,
                "slots_desired": 6,
                "slots_total": 2,
            }
        },
    )
    pool = HostPool(hosts=[host("a", slots_total=2, slots_desired=None)])

    await autoscaler(repository, pool).reconcile()

    assert pool.slot_writes == []
    assert repository.slot_target_writes == [
        {
            "expected_revision": 4,
            "expected_generation": 0,
            "allocation_id": "a",
            "host": "host-a",
            "target_slots": 6,
            "capacity_sampled_at": NOW,
            "capacity_snapshot": {"cpu_logical_count": 16, "memory_total_bytes": 64 * GIB},
        }
    ]
    assert repository.state["runtime"]["slot_status"] == [
        {
            "allocation_id": "a",
            "host": "host-a",
            "desired_slots": 6,
            "applied_slots": 2,
            "blocker_code": "awaiting_host_apply",
        }
    ]


@pytest.mark.asyncio
async def test_stale_capacity_never_increases_slots() -> None:
    repository = Repository(
        configured_policy=policy(),
        configured_runtime=runtime(),
        members=[member("a")],
    )
    pool = HostPool(
        hosts=[
            host(
                "a",
                slots_total=2,
                slots_desired=2,
                sampled_at=NOW - timedelta(seconds=91),
            )
        ]
    )

    await autoscaler(repository, pool).reconcile()

    assert pool.slot_writes == []
    assert len(repository.slot_target_writes) == 1
    assert repository.slot_target_writes[0]["target_slots"] is None
    assert repository.state["runtime"]["slot_status"][0]["blocker_code"] == "capacity_stale"


@pytest.mark.parametrize(
    ("configured_host", "expected_blocker"),
    [
        (None, "host_not_ready"),
        (host("a", sampled_at=None), "capacity_missing"),
        (
            host(
                "a",
                slots_total=1,
                slots_desired=1,
                snapshot={"cpu_logical_count": 4, "memory_total_bytes": 64 * GIB},
            ),
            "capacity_below_minimum",
        ),
        (host("a", launch_error="sandbox launch failed"), "host_launch_error"),
        (host("a", heartbeat_at=NOW - timedelta(seconds=91)), "host_heartbeat_stale"),
    ],
)
@pytest.mark.asyncio
async def test_unverified_capacity_keeps_the_slot_hold(
    configured_host: dict[str, Any] | None,
    expected_blocker: str,
) -> None:
    repository = Repository(
        configured_policy=policy(),
        configured_runtime=runtime(),
        members=[member("a")],
    )
    pool = HostPool(hosts=[] if configured_host is None else [configured_host])

    await autoscaler(repository, pool).reconcile()

    assert pool.slot_writes == []
    assert len(repository.slot_target_writes) == 1
    assert repository.slot_target_writes[0]["target_slots"] is None
    assert repository.state["runtime"]["slot_status"][0]["blocker_code"] == expected_blocker


@pytest.mark.asyncio
async def test_confirmed_slot_limit_clears_only_the_autoscaling_hold() -> None:
    repository = Repository(
        configured_policy=policy(),
        configured_runtime=runtime(),
        members=[member("a")],
    )
    pool = HostPool(hosts=[host("a", slots_total=6, slots_desired=6)])

    await autoscaler(repository, pool).reconcile()

    assert pool.slot_writes == []
    assert len(repository.slot_target_writes) == 1
    assert repository.slot_target_writes[0]["target_slots"] == 6
    assert repository.state["runtime"]["slot_status"][0]["blocker_code"] is None


@pytest.mark.asyncio
async def test_slot_fence_refusal_is_reported_as_a_host_blocker() -> None:
    repository = Repository(
        configured_policy=policy(),
        configured_runtime=runtime(),
        members=[member("a")],
        slot_results={
            "a": {
                "outcome": "refused",
                "blocker_code": "autoscaling_placement_pinned",
                "slots_desired": 6,
                "slots_total": 6,
            }
        },
    )
    pool = HostPool(hosts=[host("a")])

    await autoscaler(repository, pool).reconcile()

    assert repository.slot_target_writes[0]["expected_revision"] == 4
    assert repository.slot_target_writes[0]["expected_generation"] == 0
    assert repository.state["runtime"]["slot_status"][0]["blocker_code"] == (
        "autoscaling_placement_pinned"
    )
    assert repository.scale_ups == []


@pytest.mark.asyncio
async def test_scale_down_uses_only_an_idle_unretained_owned_member() -> None:
    old = member("old", created_at=NOW - timedelta(days=2))
    newest = member("new", created_at=NOW - timedelta(days=1))
    repository = Repository(
        configured_policy=policy(
            min_hosts=1,
            scale_down_enabled=True,
            scale_down_idle_seconds=600,
        ),
        configured_runtime=runtime(idle_since=NOW - timedelta(seconds=601)),
        members=[old, newest],
    )
    pool = HostPool(hosts=[host("old"), host("new")])

    await autoscaler(repository, pool).reconcile()

    assert len(repository.scale_downs) == 1
    assert repository.scale_downs[0]["allocation_id"] == "new"
    assert repository.scale_downs[0]["decision"] == {
        "action": "scale_down_idle",
        "allocation_id": "new",
    }


@pytest.mark.asyncio
async def test_scale_down_skips_a_deployment_fenced_host() -> None:
    old = member("old", created_at=NOW - timedelta(days=2))
    newest = member("new", created_at=NOW - timedelta(days=1))
    repository = Repository(
        configured_policy=policy(
            min_hosts=1,
            scale_down_enabled=True,
            scale_down_idle_seconds=600,
        ),
        configured_runtime=runtime(idle_since=NOW - timedelta(seconds=601)),
        members=[old, newest],
    )
    pool = HostPool(hosts=[host("old"), host("new", drain_fence_active=True)])

    await autoscaler(repository, pool).reconcile()

    assert len(repository.scale_downs) == 1
    assert repository.scale_downs[0]["allocation_id"] == "old"


@pytest.mark.asyncio
async def test_busy_or_retained_capacity_resets_the_idle_window() -> None:
    repository = Repository(
        configured_policy=policy(
            min_hosts=1,
            scale_down_enabled=True,
            scale_down_idle_seconds=600,
        ),
        configured_runtime=runtime(idle_since=NOW - timedelta(hours=1)),
        members=[member("a"), member("b")],
    )
    pool = HostPool(hosts=[host("a"), host("b", retained_groups=1)])

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_downs == []
    assert repository.state["runtime"]["idle_since"] is None


@pytest.mark.asyncio
async def test_pinned_placement_blocks_actions_and_slot_mutation() -> None:
    repository = Repository(
        configured_policy=policy(min_hosts=2),
        configured_runtime=runtime(),
        members=[member("a")],
    )
    pool = HostPool(
        hosts=[host("a", slots_total=2, slots_desired=None)],
        active_host="host-a",
    )

    await autoscaler(repository, pool).reconcile()

    assert repository.scale_ups == []
    assert pool.slot_writes == []
    assert repository.slot_target_writes == []
    assert repository.state["runtime"]["blocker_code"] == "placement_pinned"


@pytest.mark.asyncio
async def test_disabled_policy_leaves_the_bootstrap_slot_hold_unchanged() -> None:
    repository = Repository(
        configured_policy=policy(enabled=False),
        configured_runtime=runtime(),
        members=[member("a")],
    )
    pool = HostPool(hosts=[host("a", slots_total=2, slots_desired=None)])

    await autoscaler(repository, pool).reconcile()

    assert pool.slot_writes == []
    assert repository.slot_target_writes == []
