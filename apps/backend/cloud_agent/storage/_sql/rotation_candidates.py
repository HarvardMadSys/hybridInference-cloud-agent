"""When a move may stop the work of a replacement node it gives up."""

from __future__ import annotations

from typing import Any

from cloud_agent.domain.compute import refused_for_capacity
from cloud_agent.storage._sql.rows import load_json


async def candidate_work_stoppable(
    conn: Any, allocation_id: str, refused_acquisition_id: str | None
) -> bool:
    """Whether the candidate's own work can be stopped now; hold the compute-entry lock.

    Work that is executing, or whose outcome is unknown, never can. A move that
    gives the candidate up for a capacity refusal names that acquisition: it is
    locked before anything else is read and judged as it is now, so a retry
    that ran since the move read it keeps whatever it got. Only a refusal for
    capacity that still left no provider identity lets it go.
    """
    acquisition = None
    if refused_acquisition_id is not None:
        acquisition = await conn.fetchrow(
            """SELECT state,step,result FROM agent_compute_operations
            WHERE id=$1 AND allocation_id=$2 AND kind='acquire' FOR UPDATE""",
            refused_acquisition_id,
            allocation_id,
        )
        if acquisition is None:
            return False
    if await conn.fetchval(
        """SELECT 1 FROM agent_compute_operations
        WHERE allocation_id=$1 AND state IN ('running','unknown') LIMIT 1""",
        allocation_id,
    ):
        return False
    if acquisition is None:
        return True
    candidate = await conn.fetchrow(
        "SELECT external_id FROM agent_compute_allocations WHERE id=$1", allocation_id
    )
    nodes = await conn.fetch(
        "SELECT id FROM agent_compute_nodes WHERE allocation_id=$1", allocation_id
    )
    return candidate is not None and refused_for_capacity(
        {
            "state": acquisition["state"],
            "step": acquisition["step"],
            "result": load_json(acquisition["result"]) or {},
        },
        {"external_id": candidate["external_id"], "nodes": [dict(node) for node in nodes]},
    )
