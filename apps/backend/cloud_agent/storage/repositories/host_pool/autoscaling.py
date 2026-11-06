"""The autoscaler's observation of demand and capacity."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from cloud_agent.domain.execution_profiles import KATA_RUNTIME
from cloud_agent.domain.host_pool import HOST_ONLINE
from cloud_agent.domain.sandbox_group import DeletionState
from cloud_agent.sandbox_archive.sandbox_archive_credentials import stored_archive_policy
from cloud_agent.schemas.capacity_protocol import FRESH_AFTER_SECONDS
from cloud_agent.storage._sql.compute_admission import host_compute_admission, lock_compute_topology
from cloud_agent.storage._sql.environment_runs import env_run_blocks_host, env_run_holds_slot
from cloud_agent.storage._sql.hosts import HOST_CAPACITY_COLUMNS, HOST_COLUMNS
from cloud_agent.storage._sql.jobs import runnable_queued_turn
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage.repositories.host_pool._common import HostPoolBase
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_CAPABILITY,
    SandboxArchivePolicyError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any


_JobDemand = tuple[frozenset[str] | None, str]


_HostShape = tuple[frozenset[str], str | None]


_HostCapacity = tuple[_HostShape, int]


def _job_matches_host(job: _JobDemand, host: _HostShape) -> bool:
    """Mirror the capability and execution-profile half of a new-group claim."""
    required, execution_profile = job
    capabilities, sandbox_runtime = host
    if required is None or not required.issubset(capabilities):
        return False
    return execution_profile == "trusted-v1" or (
        execution_profile == "untrusted-kata-v1" and sandbox_runtime == KATA_RUNTIME
    )


def _maximum_compatible_jobs(jobs: Sequence[_JobDemand], hosts: Sequence[_HostCapacity]) -> int:  # noqa: C901, PLR0912 complexity baseline, #771
    """Return the exact maximum cardinality of the job/host-capacity matching.

    Equal jobs and equal host shapes are aggregated before a small Dinic flow.
    That keeps the graph bounded by compatibility shapes rather than queue
    depth, while retaining the same answer as expanding every free slot into a
    separate host vertex. In particular it handles the constrained/generic
    trap where a greedy pass can waste the only flexible slot.
    """
    job_counts: dict[_JobDemand, int] = {}
    for job in jobs:
        job_counts[job] = job_counts.get(job, 0) + 1
    host_counts: dict[_HostShape, int] = {}
    for shape, capacity in hosts:
        if capacity > 0:
            host_counts[shape] = host_counts.get(shape, 0) + capacity
    if not job_counts or not host_counts:
        return 0

    job_types = list(job_counts)
    host_types = list(host_counts)
    source = 0
    job_offset = 1
    host_offset = job_offset + len(job_types)
    sink = host_offset + len(host_types)
    graph: list[list[list[int]]] = [[] for _ in range(sink + 1)]

    def add_edge(start: int, end: int, capacity: int) -> None:
        graph[start].append([end, capacity, len(graph[end])])
        graph[end].append([start, 0, len(graph[start]) - 1])

    for index, job in enumerate(job_types):
        count = job_counts[job]
        add_edge(source, job_offset + index, count)
        for host_index, host in enumerate(host_types):
            if _job_matches_host(job, host):
                add_edge(job_offset + index, host_offset + host_index, count)
    for index, host in enumerate(host_types):
        add_edge(host_offset + index, sink, host_counts[host])

    def send(node: int, available: int, level: list[int], next_edge: list[int]) -> int:
        if node == sink:
            return available
        while next_edge[node] < len(graph[node]):
            edge = graph[node][next_edge[node]]
            target, capacity, reverse = edge
            if capacity > 0 and level[target] == level[node] + 1:
                pushed = send(target, min(available, capacity), level, next_edge)
                if pushed:
                    edge[1] -= pushed
                    graph[target][reverse][1] += pushed
                    return pushed
            next_edge[node] += 1
        return 0

    total = 0
    while True:
        level = [-1] * len(graph)
        level[source] = 0
        pending = deque([source])
        while pending:
            node = pending.popleft()
            for target, capacity, _reverse in graph[node]:
                if capacity > 0 and level[target] < 0:
                    level[target] = level[node] + 1
                    pending.append(target)
        if level[sink] < 0:
            return total

        next_edge = [0] * len(graph)
        while pushed := send(source, sum(job_counts.values()), level, next_edge):
            total += pushed


class AutoscalingMixin(HostPoolBase):
    """Observe the pool for the autoscaler."""

    async def autoscaling_observation(self, *, expected_deployment_sha: str) -> dict[str, Any]:  # noqa: PLR0915 complexity baseline, #771
        """Read the durable, content-free signal used by compute autoscaling.

        A retained turn is tied to its existing Host and therefore cannot be
        relieved by adding a CloudLab node. Only a turn a claim would hand out
        now (:func:`runnable_queued_turn`), of a thread with no sandbox
        membership, is demand here. One in claim backoff is not, but it is
        counted apart: the pool is not idle while it waits. Capacity is the
        effective claim ceiling (the lower of reported and desired slots), after
        liveness, placement, release/rotation holds, deployment attestation, and
        archive-policy gates have all been applied. Pressure is an exact maximum
        compatibility matching, not queue depth minus an interchangeable slot
        count. The shared topology lock stabilizes bindings, but it deliberately
        remains compatible with claims; repeatable read prevents a claim
        committed between the job and host queries from combining pre-claim
        demand with post-claim capacity into a state that never existed.
        """
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            policy = await conn.fetchrow(
                """
                SELECT active_host, sandbox_archive_settings, sandbox_archive_revision
                FROM agent_runner_policy WHERE id FOR SHARE
                """
            )
            await lock_compute_topology(conn)
            observed_at = await conn.fetchval("SELECT NOW()")
            job_rows = await conn.fetch(
                f"""
                SELECT t.execution_profile,
                       COALESCE(j.metadata->'requires', '[]'::jsonb) AS requires,
                       {runnable_queued_turn("j")} AS claimable
                FROM agent_jobs j
                JOIN agent_threads t ON t.id=j.thread_id
                WHERE {runnable_queued_turn("j", including_backoff=True)}
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_thread_sandbox_memberships m
                      WHERE m.thread_id=j.thread_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_jobs parent
                      WHERE parent.id=j.parent_job_id AND parent.state='succeeded'
                  )
                ORDER BY j.created_at, j.id
                """
            )
            job_rows.extend(
                await conn.fetch(
                    "SELECT execution_profile, '[\"environment-v1\"]'::jsonb AS requires, TRUE AS claimable FROM agent_environment_runs WHERE state='queued'",
                )
            )
            rows = await conn.fetch(
                f"""
                SELECT {HOST_COLUMNS}, {HOST_CAPACITY_COLUMNS}, n.allocation_id,
                       (am.allocation_id IS NOT NULL) AS autoscaler_owned,
                       (SELECT COUNT(*) FROM agent_sandboxes g
                        WHERE g.host=h.host AND g.deletion_state<>{DeletionState.DELETED.sql})
                           AS retained_groups
                FROM agent_runner_hosts h
                LEFT JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                LEFT JOIN agent_cloudlab_autoscaling_members am
                  ON am.allocation_id=n.allocation_id AND am.state<>'released'
                ORDER BY h.host
                """
            )

            try:
                archive_policy = stored_archive_policy(policy)
            except SandboxArchivePolicyError:
                archive_policy = None

            jobs: list[_JobDemand] = []
            for row in job_rows:
                raw_requires = load_json(row["requires"])
                required = (
                    frozenset(raw_requires)
                    if isinstance(raw_requires, list)
                    and all(isinstance(value, str) for value in raw_requires)
                    else None
                )
                if row["claimable"]:
                    jobs.append((required, str(row["execution_profile"])))

            hosts: list[dict[str, Any]] = []
            matching_hosts: list[_HostCapacity] = []
            templates: list[tuple[str, _HostShape]] = []
            free_slots = 0
            active_tests = {
                row["host"]: int(row["count"])
                for row in await conn.fetch(
                    f"SELECT host,COUNT(*) AS count FROM agent_environment_runs WHERE host IS NOT NULL AND {env_run_holds_slot()} GROUP BY host"
                )
            }
            cleanup_blocked_hosts = {
                row["host"]
                for row in await conn.fetch(
                    "SELECT DISTINCT host FROM agent_environment_runs WHERE host IS NOT NULL "
                    f"AND {env_run_blocks_host()}"
                )
            }
            for raw in rows:
                host = self._host_row_to_dict(raw)
                host["allocation_id"] = raw["allocation_id"]
                host["retained_groups"] = int(raw["retained_groups"])
                hosts.append(host)

                capabilities = host.get("capabilities") or []
                heartbeat = host.get("last_heartbeat_at")
                deployment_ready = any(
                    isinstance(capability, str)
                    and capability.startswith("shared-sandbox-deployment:")
                    for capability in capabilities
                ) and (
                    not expected_deployment_sha
                    or f"shared-sandbox-deployment:{expected_deployment_sha}" in capabilities
                )
                archive_ready = bool(
                    archive_policy is not None
                    and (
                        not archive_policy.enabled
                        or (
                            SANDBOX_ARCHIVE_CAPABILITY in capabilities
                            and host.get("sandbox_archive_settings_applied_revision")
                            == archive_policy.revision
                        )
                    )
                )
                fresh = (
                    bool(
                        heartbeat is not None
                        and (observed_at - heartbeat).total_seconds() <= FRESH_AFTER_SECONDS
                    )
                    and host["host"] not in cleanup_blocked_hosts
                )
                if not (
                    host.get("status") == HOST_ONLINE
                    and host.get("enrolled")
                    and not host.get("launch_error")
                    and fresh
                    and (policy is None or policy["active_host"] in (None, host["host"]))
                    and "shared-sandbox-v1" in capabilities
                    and "native-fork-at-anchor-v1" in capabilities
                    and deployment_ready
                    and archive_ready
                ):
                    continue
                allows_new_groups, _ = await host_compute_admission(conn, host["host"])
                if not allows_new_groups:
                    continue
                shape = (
                    frozenset(str(capability) for capability in capabilities),
                    host.get("sandbox_runtime")
                    if isinstance(host.get("sandbox_runtime"), str)
                    else None,
                )
                if raw["autoscaler_owned"]:
                    templates.append((str(host["host"]), shape))
                total = host.get("slots_total")
                desired = host.get("slots_desired")
                ceilings = [value for value in (total, desired) if isinstance(value, int)]
                if not ceilings:
                    continue
                free = max(
                    0,
                    min(ceilings)
                    - int(host.get("slots_used") or 0)
                    - active_tests.get(host["host"], 0),
                )
                free_slots += free
                if free:
                    matching_hosts.append((shape, free))

            matchable_jobs = jobs
            if templates:
                matchable_jobs = [
                    job
                    for job in jobs
                    if any(_job_matches_host(job, shape) for _host, shape in templates)
                ]
            matched = _maximum_compatible_jobs(matchable_jobs, matching_hosts)

        return {
            "observed_at": observed_at,
            "active_host": None if policy is None else policy["active_host"],
            "runnable_new_group_jobs": len(jobs),
            "backoff_new_group_jobs": len(job_rows) - len(jobs),
            "fresh_effective_free_slots": free_slots,
            "unmatched_new_group_jobs": len(matchable_jobs) - matched,
            "autoscaling_template_hosts": [host for host, _shape in templates],
            "hosts": hosts,
        }
