"""What a terminal provider observation settles, and what may never reopen it."""

from __future__ import annotations

from typing import Any

from cloud_agent.storage._sql.hosts import release_covered_sandboxes

TERMINAL_STATES = frozenset({"terminated", "absent"})


def terminal_proof_stands(current: Any, observation: dict[str, Any]) -> bool:
    """Whether an allocation's recorded terminal state outranks a new observation.

    A provider that ended an immutable identity cannot bring it back, so any
    later read of it, pending, ready, unknown or terminal again, is stale and
    leaves the first proof, its source and its time as they are. Absence
    without an identity is only the local record that nothing was created: an
    observation that names a resource is new evidence and is saved.
    """
    return current["observed_state"] in TERMINAL_STATES and (
        current["external_id"] is not None or observation.get("external_id") is None
    )


async def resume_blocked_retirement(conn: Any, allocation_id: str) -> None:
    """Queue again a release that stopped for attention once its machine is gone.

    A release blocked by what the Host still held waits for an
    administrator, but the provider ending the machine settles it: the
    next run finds the resource terminal and commits the release
    (``_settle_terminal_retirement``). Without this the operation stayed
    blocked, and the allocation unreleased, after the machine was gone.
    A pool member's terminal release settles through its reservation.
    """
    await conn.execute(
        """UPDATE agent_compute_operations o
           SET state='pending',next_attempt_at=NOW(),completed_at=NULL,updated_at=NOW()
           WHERE o.allocation_id=$1 AND o.kind='retire' AND o.state='blocked'
             AND NOT EXISTS (
                 SELECT 1 FROM agent_cloudlab_autoscaling_members m
                 WHERE m.allocation_id=o.allocation_id
             )""",
        allocation_id,
    )


async def release_terminated_groups(conn: Any, allocation_id: str) -> None:
    """The provider ended this machine: hand each covered stopped group elsewhere.

    Its Hosts keep their rows, credentials and any deployment fence for
    the administrator's removal. Only the groups move: a group can never
    start again on a machine that is gone, and the claim of a ``cold``
    group refuses the Host that still names its container.
    """
    hosts = [
        row["host"]
        for row in await conn.fetch(
            """SELECT h.host FROM agent_compute_nodes n
               JOIN agent_runner_hosts h ON h.compute_node_id=n.id
               WHERE n.allocation_id=$1 ORDER BY h.host""",
            allocation_id,
        )
    ]
    if hosts:
        await release_covered_sandboxes(conn, hosts)
