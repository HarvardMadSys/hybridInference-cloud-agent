"""Fixed-round-trip compute inventory reads; callers own the snapshot."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cloud_agent.storage._sql import environment_runs
from cloud_agent.storage._sql.compute_admission import compute_admission

RowDecoder = Callable[[Any], dict[str, Any]]


async def load_allocations(conn: Any, decode: RowDecoder) -> list[dict[str, Any]]:
    """Hydrate every allocation in five reads, preserving child ordering."""
    allocations = [
        decode(row)
        for row in await conn.fetch("""
        SELECT a.*, m.state AS autoscaling_member_state,
               (m.allocation_id IS NOT NULL) AS autoscaling_owned
        FROM agent_compute_allocations a
        LEFT JOIN agent_cloudlab_autoscaling_members m ON m.allocation_id=a.id
        ORDER BY a.created_at,a.id
    """)
    ]
    by_id = {row["id"]: row for row in allocations}
    for row in allocations:
        row.update(nodes=[], hosts=[], holds=[], rotation=None)
    ids = list(by_id)
    for row in await conn.fetch(
        "SELECT * FROM agent_compute_nodes WHERE allocation_id=ANY($1::text[]) ORDER BY id", ids
    ):
        by_id[row["allocation_id"]]["nodes"].append(decode(row))
    for row in await conn.fetch(
        f"""
        SELECT n.allocation_id AS inventory_allocation_id,h.host,h.status,h.compute_node_id,
               (h.slots_used+{environment_runs.env_runs_holding_slots("h.host")}) AS slots_used,
               h.last_heartbeat_at,h.broker_url AS sandbox_manager_url,h.drain_fence_active,
               (SELECT count(*) FROM agent_sandboxes g
                WHERE g.host=h.host AND g.deletion_state<>'deleted') AS retained_groups
        FROM agent_runner_hosts h JOIN agent_compute_nodes n ON n.id=h.compute_node_id
        WHERE n.allocation_id=ANY($1::text[]) ORDER BY h.host
    """,
        ids,
    ):
        host = dict(row)
        by_id[host.pop("inventory_allocation_id")]["hosts"].append(host)
    for row in await conn.fetch(
        "SELECT * FROM agent_host_admission_holds WHERE allocation_id=ANY($1::text[]) "
        "ORDER BY host,purpose",
        ids,
    ):
        by_id[row["allocation_id"]]["holds"].append(dict(row))
    for row in await conn.fetch(
        """
        SELECT DISTINCT ON (allocation_id) allocation_id,id,state,step,message,result,
               created_at,updated_at,completed_at
        FROM agent_compute_operations WHERE allocation_id=ANY($1::text[]) AND kind='rotate'
        ORDER BY allocation_id,created_at DESC
    """,
        ids,
    ):
        rotation = decode(row)
        by_id[rotation.pop("allocation_id")]["rotation"] = rotation
    return allocations


async def load_hosts(conn: Any, allocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Observe all Hosts and their admission without per-Host round trips."""
    by_id = {row["id"]: row for row in allocations}
    rows = await conn.fetch(f"""
        SELECT h.host,h.status,h.drain_fence_active,
               (h.removal_requested_at IS NOT NULL) AS removal_pending,h.compute_node_id,
               n.allocation_id,
               (h.slots_used+{environment_runs.env_runs_holding_slots("h.host")}) AS slots_used,
               h.slots_total,h.slots_desired,h.last_heartbeat_at,
               h.labels->>'physical_host' AS physical_host,h.broker_url AS sandbox_manager_url,
               (SELECT count(*) FROM agent_sandboxes g
                WHERE g.host=h.host AND g.deletion_state<>'deleted') AS retained_groups,
               clock_timestamp() AS database_now,
               EXISTS (SELECT 1 FROM agent_host_admission_holds x WHERE x.host=h.host) AS held,
               EXISTS (SELECT 1 FROM agent_host_admission_holds x
                       WHERE x.host=h.host AND x.scope='all_claims') AS all_held
        FROM agent_runner_hosts h LEFT JOIN agent_compute_nodes n ON n.id=h.compute_node_id
        ORDER BY h.host
    """)
    hosts = []
    for row in rows:
        host = dict(row)
        admission = dict(by_id.get(host["allocation_id"], {"id": None}))
        admission.update({key: host.pop(key) for key in ("database_now", "held", "all_held")})
        host["allows_new_groups"], host["allows_claims"] = compute_admission(admission)
        hosts.append(host)
    return hosts


async def load_scheduling_operations(
    conn: Any, allocation_ids: list[str], decode: RowDecoder
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Retain both recent-200 windows and all unresolved work per allocation.

    Filtering a global recent-200 list, or filtering only each allocation's
    recent-200 list for renewals, can lose an old rotation behind inspections.
    Keep the general and lifecycle windows independently, in one query.
    """
    rows = await conn.fetch(
        """
        WITH ranked AS (
            SELECT o.*,
                   row_number() OVER (PARTITION BY allocation_id ORDER BY created_at DESC) AS recent,
                   row_number() OVER (PARTITION BY allocation_id,(kind IN ('extend','rotate'))
                                      ORDER BY created_at DESC) AS lifecycle_recent,
                   (state IN ('pending','running','waiting','unknown')
                    OR (state IN ('failed','blocked') AND kind IN ('acquire','add','import')
                        AND allocation_id IN (SELECT id FROM agent_compute_allocations
                                              WHERE observed_state NOT IN ('terminated','absent'))))
                    AS unresolved
            FROM agent_compute_operations o WHERE allocation_id=ANY($1::text[])
        )
        SELECT * FROM ranked WHERE recent<=200 OR unresolved
            OR (kind IN ('extend','rotate') AND lifecycle_recent<=200)
        ORDER BY created_at DESC
    """,
        allocation_ids,
    )
    operations: dict[str, list[dict[str, Any]]] = {}
    lifecycle: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        operation = decode(row)
        recent = operation.pop("recent")
        lifecycle_recent = operation.pop("lifecycle_recent")
        unresolved = operation.pop("unresolved")
        allocation_id = operation["allocation_id"]
        if recent <= 200 or unresolved:
            operations.setdefault(allocation_id, []).append(operation)
        if operation["kind"] in {"extend", "rotate"} and (lifecycle_recent <= 200 or unresolved):
            lifecycle.setdefault(allocation_id, []).append(operation)
    return operations, lifecycle
