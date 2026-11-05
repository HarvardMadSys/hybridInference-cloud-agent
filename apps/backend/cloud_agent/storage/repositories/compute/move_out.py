"""Moving conversation sandboxes off a compute Host."""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.storage._sql.hosts import GROUP_TURN_CLAIMED_SQL, authorize_superseded_removals
from cloud_agent.storage._sql.jobs import fail_unrunnable_follow_ups
from cloud_agent.storage._sql.move_out import (
    UNMOVABLE_GROUP_SQL,
    UNMOVED_GROUP_SQL,
    move_out_blocker,
)
from cloud_agent.storage._sql.sandbox_transitions import (
    LOSE_RECLAIMED_GROUP_MEMBERS_SQL,
    LOSE_RECLAIMED_GROUP_SQL,
)
from cloud_agent.storage.repositories.compute._common import ComputeRepositoryBase

#: A group whose only Docker copy went with a machine its provider reclaimed:
#: still bound to that machine's Host, not handed over through a checkpoint,
#: and with no turn, claim or Fork in flight. ``g`` is the group and ``h`` its
#: Host; ``$2`` is how long the Host must have been silent.
_RECLAIMED_GROUP_SQL = f"""(
    {UNMOVED_GROUP_SQL}
    AND (h.last_heartbeat_at IS NULL
         OR h.last_heartbeat_at < NOW() - make_interval(secs => $2))
    AND NOT EXISTS (
        SELECT 1 FROM agent_thread_sandbox_memberships busy
        WHERE busy.sandbox_id = g.id
          AND (busy.active_attempt_id IS NOT NULL OR busy.fork_operation_id IS NOT NULL)
    )
    AND NOT {GROUP_TURN_CLAIMED_SQL}
)"""

_RECLAIMED_DETAIL = (
    "not run because the server holding this conversation's sandbox was reclaimed by its provider"
)


class MoveOutMixin(ComputeRepositoryBase):
    """Groups still to move off a Host, what they left behind, and reclaimed groups."""

    async def unmoved_groups(self, *, allocation_id: str) -> int:
        """Count the groups still held only by this allocation's Hosts."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    f"""SELECT count(*) FROM agent_sandboxes g
                       JOIN agent_runner_hosts h ON h.host=g.host
                       JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                       WHERE n.allocation_id=$1 AND {UNMOVED_GROUP_SQL}""",
                    allocation_id,
                )
            )

    async def authorize_superseded_removals(
        self, *, host: str, containers: list[dict[str, Any]], operation_id: str
    ) -> list[dict[str, Any]]:
        """Record which containers ``host`` reports its moved groups left behind.

        See :func:`~cloud_agent.storage._sql.hosts.authorize_superseded_removals`.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            return await authorize_superseded_removals(conn, host, containers, operation_id)

    async def record_superseded_removal(self, *, request_id: str) -> None:
        """Record that the Host confirmed an authorized removal."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_superseded_container_removals SET removed_at = NOW() "
                "WHERE request_id = $1 AND removed_at IS NULL",
                request_id,
            )

    async def superseded_removals(self, *, hosts: list[str]) -> int:
        """Count the confirmed removals of containers left behind on ``hosts``."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT count(*) FROM agent_superseded_container_removals "
                    "WHERE host = ANY($1::text[]) AND removed_at IS NOT NULL",
                    hosts,
                )
            )

    async def recorded_host_state(self, *, host: str) -> dict[str, Any]:
        """The receipts ``host`` keeps on disk that the control plane recorded.

        Each sandbox's latest checkpoint captured there, and each confirmed
        removal of a container left behind there. See ``RecordedHostState``.
        """
        async with self._pool.acquire() as conn:
            checkpoints = await conn.fetch(
                """SELECT DISTINCT ON (sandbox_id)
                          id AS checkpoint_id, sandbox_id, generation, container_id,
                          base_image, runtime_id, runtime_version, object_key,
                          object_version_id AS version_id, sha256, size_bytes,
                          new_layer_bytes, layer_count, format_version, start_seq
                   FROM agent_sandbox_checkpoints WHERE host = $1
                   ORDER BY sandbox_id, captured_at DESC, recorded_at DESC""",
                host,
            )
            removals = await conn.fetch(
                "SELECT request_id, origin_thread_id, sandbox_id, generation, container_id "
                "FROM agent_superseded_container_removals "
                "WHERE host = $1 AND removed_at IS NOT NULL ORDER BY request_id",
                host,
            )
        return {
            "checkpoints": {str(row["sandbox_id"]): dict(row) for row in checkpoints},
            "removals": {
                str(row["request_id"]): {
                    "thread_id": str(row["origin_thread_id"]),
                    "sandbox_id": str(row["sandbox_id"]),
                    "generation": int(row["generation"]),
                    "container_id": str(row["container_id"]),
                }
                for row in removals
            },
        }

    async def move_out_report(self, *, allocation_id: str) -> dict[str, Any]:
        """Whether this allocation's groups are moving out, why not, and what stays.

        ``moving`` is whether its ``move_out`` hold is in place, ``blocker`` the
        reason code a move could not start or continue with now (``None`` when
        it can), ``unmoved`` the groups still held only by its Hosts, and
        ``unmovable`` those that will stay whatever happens: no checkpoint
        covers them, or their image cannot be pulled elsewhere.
        """
        async with self._pool.acquire() as conn:
            hosts = [
                row["host"]
                for row in await conn.fetch(
                    """SELECT h.host FROM agent_compute_nodes n
                       JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                       WHERE n.allocation_id=$1 ORDER BY h.host""",
                    allocation_id,
                )
            ]
            moving = bool(
                await conn.fetchval(
                    "SELECT 1 FROM agent_host_admission_holds "
                    "WHERE allocation_id=$1 AND purpose='move_out' LIMIT 1",
                    allocation_id,
                )
            )
            blocker = await move_out_blocker(conn, hosts, need_target=not moving)
            counts = await conn.fetchrow(
                f"""SELECT count(*) FILTER (WHERE {UNMOVED_GROUP_SQL}) AS unmoved,
                           count(*) FILTER (WHERE {UNMOVABLE_GROUP_SQL}) AS unmovable
                    FROM agent_sandboxes g
                    JOIN agent_runner_hosts h ON h.host=g.host
                    JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                    WHERE n.allocation_id=$1""",
                allocation_id,
            )
        return {
            "moving": moving,
            "blocker": blocker,
            "unmoved": int(counts["unmoved"]),
            "unmovable": int(counts["unmovable"]),
        }

    async def lose_reclaimed_groups(self, *, allocation_id: str, quiet_seconds: int) -> list[str]:
        """Mark LOST each group that went with a machine its provider reclaimed.

        Once the provider confirms the allocation ``terminated``, its disk is
        gone, and with it the only Docker copy of every group its Hosts still
        hold. Covered stopped groups were already handed over through their
        checkpoints; the rest can never continue. Leaving them bound to the
        Host would queue their chats' messages forever and keep the Host from
        being removed, so each becomes ``lost`` as a confirmed Docker data loss
        does, and its queued turns fail with a reason.

        A group is left alone while its Host beat within ``quiet_seconds`` or
        it has a turn, claim or Fork in flight; the attempt reaper settles
        those first, and a later pass takes the group. Returns the groups
        marked lost.
        """
        async with self._pool.acquire() as conn:
            candidates = await conn.fetch(
                f"""SELECT g.id FROM agent_compute_allocations a
                    JOIN agent_compute_nodes n ON n.allocation_id=a.id
                    JOIN agent_runner_hosts h ON h.compute_node_id=n.id
                    JOIN agent_sandboxes g ON g.host=h.host
                    WHERE a.id=$1 AND a.observed_state='terminated'
                      AND {_RECLAIMED_GROUP_SQL}
                    ORDER BY g.id""",
                allocation_id,
                quiet_seconds,
            )
        lost = []
        for candidate in candidates:
            if await self._lose_reclaimed_group(
                sandbox_id=candidate["id"],
                allocation_id=allocation_id,
                quiet_seconds=quiet_seconds,
            ):
                lost.append(str(candidate["id"]))
        return lost

    async def _lose_reclaimed_group(
        self, *, sandbox_id: str, allocation_id: str, quiet_seconds: int
    ) -> bool:
        # Conversations, then memberships, then the group: the order every
        # writer of these rows keeps.
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT t.id FROM agent_threads t "
                "JOIN agent_thread_sandbox_memberships m ON m.thread_id = t.id "
                "WHERE m.sandbox_id = $1 ORDER BY t.id FOR UPDATE OF t",
                sandbox_id,
            )
            members = await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                f"""SELECT g.id, g.generation, g.state, g.host FROM agent_sandboxes g
                    JOIN agent_runner_hosts h ON h.host=g.host
                    JOIN agent_compute_nodes n ON n.id=h.compute_node_id
                    JOIN agent_compute_allocations a ON a.id=n.allocation_id
                    WHERE g.id=$1 AND a.id=$3 AND a.observed_state='terminated'
                      AND {_RECLAIMED_GROUP_SQL}
                    FOR UPDATE OF g""",
                sandbox_id,
                quiet_seconds,
                allocation_id,
            )
            if group is None:
                return False
            await conn.execute(
                LOSE_RECLAIMED_GROUP_SQL,
                sandbox_id,
            )
            await conn.execute(
                LOSE_RECLAIMED_GROUP_MEMBERS_SQL,
                sandbox_id,
            )
            await conn.execute(
                """INSERT INTO agent_sandbox_events (
                       sandbox_id, generation, event_type, from_state, to_state,
                       source, actor_id, details
                   ) VALUES ($1, $2, 'server_reclaimed', $3, 'lost', 'system',
                             'compute-reconciler', $4::jsonb)""",
                sandbox_id,
                group["generation"],
                group["state"],
                json.dumps({"allocation_id": allocation_id, "host": group["host"]}),
            )
            for member in members:
                await fail_unrunnable_follow_ups(
                    conn, thread_id=member["thread_id"], detail=_RECLAIMED_DETAIL
                )
        return True

    async def running_attempts(self, *, host: str) -> int:
        """Count the turns a Host is executing now."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT count(*) FROM agent_attempts WHERE host_id=$1 AND status='running'",
                    host,
                )
            )
