"""Stopping a sandbox group.

For an admin, for chats their owner deleted, for a host moving out, and when
the group's idle stop falls due.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.domain.sandbox_group import (
    DeletionState,
    GroupState,
    sql_list,
)
from cloud_agent.storage._sql.admin_audit import insert_audit_row
from cloud_agent.storage._sql.compute_admission import lock_compute_entry
from cloud_agent.storage._sql.hosts import (
    MOVING_OUT_HOST_SQL,
    UNDELETED_GROUP_SQL,
    moving_out_hosts,
    release_covered_sandboxes,
)
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox import member_turn_running
from cloud_agent.storage._sql.sandbox_transitions import REQUEST_GROUP_STOP_SQL
from cloud_agent.storage.repositories.sandbox._common import (
    _GROUP_WORK_PENDING_SQL,
    SandboxRepositoryBase,
)

if TYPE_CHECKING:
    from datetime import datetime

#: True when sandbox ``g`` has members and its owner deleted every one of
#: their chats. A failed Fork's placeholder is tombstoned the same way.
_EVERY_CHAT_DELETED_SQL = """
EXISTS (SELECT 1 FROM agent_thread_sandbox_memberships m WHERE m.sandbox_id = g.id)
AND NOT EXISTS (
    SELECT 1 FROM agent_thread_sandbox_memberships m
    JOIN agent_threads t ON t.id = m.thread_id
    WHERE m.sandbox_id = g.id AND t.deleted_at IS NULL
)
"""


class GroupStopMixin(SandboxRepositoryBase):
    """Request and fence the stop of a sandbox group."""

    async def request_admin_stop(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str,
        last_activity_at: datetime,
        actor_id: str,
        admin_ip: str,
    ) -> str:
        """Queue a reviewed, quiescent container stop under its admission fence.

        The host still checks its local terminals and operations before acting.
        The database marker survives observations and excludes new admission
        until Docker confirms the stop. No container or workspace is deleted.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            # The same membership -> group order as reservation, Fork and
            # reconciliation. Claims hold a shared group lock until committed.
            await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                "SELECT id, user_id, host, generation, container_id, last_activity_at, state, "
                "stop_requested_at, active_operation_count, "
                "terminal_count, terminal_count_observed_at, NOW() AS observed_at "
                "FROM agent_sandboxes WHERE id = $1 FOR UPDATE",
                sandbox_id,
            )
            if group is None:
                return "not_found"
            if (
                group["generation"] != generation
                or group["container_id"] != container_id
                or group["last_activity_at"] != last_activity_at
            ):
                return "stale"
            if group["state"] == "stopping" and group["stop_requested_at"] is not None:
                return "accepted"
            if group["state"] != "running":
                return "not_running"
            busy = await conn.fetchval(_GROUP_WORK_PENDING_SQL, sandbox_id)
            if busy or group["active_operation_count"]:
                return "busy"
            if group["terminal_count"]:
                return "terminals_open"
            terminal_observed = group["terminal_count_observed_at"]
            if (
                group["terminal_count"] is None
                or terminal_observed is None
                or (group["observed_at"] - terminal_observed).total_seconds() > 90
            ):
                return "activity_unknown"
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="admin_stop_requested",
                source="admin",
                actor_id=actor_id,
                from_state="running",
                to_state="stopping",
                details={"container_present": True},
            )
            await conn.execute(
                REQUEST_GROUP_STOP_SQL,
                sandbox_id,
            )
            # Stop and its audit receipt commit together; a receipt that cannot
            # be written is counted, never allowed to fail the stop.
            await insert_audit_row(
                conn,
                action="agent.sandbox.stop",
                actor_id=actor_id,
                admin_ip=admin_ip,
                target_user_id=group["user_id"],
                details={
                    "sandbox_id": sandbox_id,
                    "host": group["host"],
                    "generation": generation,
                    "container_id": container_id,
                },
            )
        return "accepted"

    async def sandboxes_of_deleted_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """List live sandboxes on online hosts whose every chat was deleted.

        Read without locks. The stop and deletion requests that act on a row
        lock the group and check its own fence again.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT g.id AS sandbox_id, g.generation, g.container_id,
                       g.last_activity_at, g.state
                FROM agent_sandboxes g
                WHERE g.deletion_state = {DeletionState.NONE.sql}
                  AND g.state IN {sql_list(GroupState.RUNNING, GroupState.STOPPED)}
                  AND g.container_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM agent_runner_hosts h
                      WHERE h.host = g.host AND h.status = 'online'
                  )
                  AND {_EVERY_CHAT_DELETED_SQL}
                ORDER BY g.updated_at, g.id
                LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def request_deleted_sessions_stop(
        self, *, sandbox_id: str, generation: int, container_id: str, actor_id: str
    ) -> str:
        """Ask the host to stop a running sandbox whose every chat was deleted.

        The same durable marker an administrator's stop writes, so admission
        stays closed and the host's next heartbeat receives the stop. The host
        still refuses while a terminal or operation is open there, and the
        heartbeat retries. Nothing is deleted here: once the stop is observed,
        the sandbox deletion request takes over.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                f"""
                SELECT g.*, ({_EVERY_CHAT_DELETED_SQL}) AS every_chat_deleted
                FROM agent_sandboxes g WHERE g.id = $1 FOR UPDATE
                """,
                sandbox_id,
            )
            if group is None:
                return "not_found"
            if group["generation"] != generation or group["container_id"] != container_id:
                return "stale"
            if not group["every_chat_deleted"] or group["deletion_state"] != "none":
                return "stale"
            if group["state"] == "stopping" and group["stop_requested_at"] is not None:
                return "accepted"
            if group["state"] != "running":
                return "not_running"
            busy = await conn.fetchval(_GROUP_WORK_PENDING_SQL, sandbox_id)
            if busy or group["active_operation_count"]:
                return "busy"
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="deleted_sessions_stop_requested",
                source="system",
                actor_id=actor_id,
                from_state="running",
                to_state="stopping",
            )
            await conn.execute(
                REQUEST_GROUP_STOP_SQL,
                sandbox_id,
            )
        return "accepted"

    async def sandboxes_moving_out(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List running groups on Hosts moving out, oldest activity first.

        Read without locks: :meth:`request_move_out_stop` locks the group and
        checks every fence again.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT g.id AS sandbox_id, g.host, g.generation, g.container_id
                FROM agent_sandboxes g
                WHERE g.state = {GroupState.RUNNING.sql} AND {UNDELETED_GROUP_SQL}
                  AND g.container_id IS NOT NULL AND g.stop_requested_at IS NULL
                  AND {MOVING_OUT_HOST_SQL.format(host="g.host")}
                ORDER BY g.last_activity_at NULLS FIRST, g.id
                LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def request_move_out_stop(
        self, *, sandbox_id: str, generation: int, container_id: str, actor_id: str
    ) -> str:
        """Ask a Host moving out to stop one idle group so its checkpoint can move it.

        The durable marker the other stops write, so the Host's next heartbeat
        receives it. A turn, operation or snapshot still running leaves the
        group to a later pass; nothing running is ever interrupted, and the
        Host refuses while a terminal is open there.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.fetch(
                "SELECT thread_id FROM agent_thread_sandbox_memberships "
                "WHERE sandbox_id = $1 ORDER BY thread_id FOR UPDATE",
                sandbox_id,
            )
            group = await conn.fetchrow(
                f"""
                SELECT g.*, {MOVING_OUT_HOST_SQL.format(host="g.host")} AS moving_out,
                       {UNDELETED_GROUP_SQL} AS undeleted
                FROM agent_sandboxes g WHERE g.id = $1 FOR UPDATE
                """,
                sandbox_id,
            )
            if group is None:
                return "not_found"
            if group["generation"] != generation or group["container_id"] != container_id:
                return "stale"
            if not group["moving_out"] or not group["undeleted"]:
                return "stale"
            if group["state"] == "stopping" and group["stop_requested_at"] is not None:
                return "accepted"
            if group["state"] != "running":
                return "not_running"
            busy = await conn.fetchval(_GROUP_WORK_PENDING_SQL, sandbox_id)
            if busy or group["active_operation_count"]:
                return "busy"
            await self._record_trajectory_event(
                conn,
                group=group,
                event_type="move_out_stop_requested",
                source="system",
                actor_id=actor_id,
                from_state="running",
                to_state="stopping",
            )
            await conn.execute(
                REQUEST_GROUP_STOP_SQL,
                sandbox_id,
            )
        return "accepted"

    async def release_moving_out_groups(self) -> list[str]:
        """Hand every covered stopped group on a Host moving out to another Host."""
        async with self._pool.acquire() as conn, conn.transaction():
            # Held to the end: an extension cannot clear a hold between the
            # list read here and the hand-over below.
            await lock_compute_entry(conn)
            hosts = await moving_out_hosts(conn)
            if not hosts:
                return []
            # A claim of a group's next turn holds the group row; waiting for
            # it here lets the hand-over see that turn and leave the group.
            await conn.fetch(
                "SELECT g.id FROM agent_sandboxes g WHERE g.host = ANY($1::text[]) "
                f"AND g.state = {GroupState.STOPPED.sql} AND {UNDELETED_GROUP_SQL} ORDER BY g.id FOR UPDATE",
                hosts,
            )
            return await release_covered_sandboxes(conn, hosts, live_host=True)

    async def due_thread_sandbox_stop(
        self,
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
    ) -> dict[str, Any] | None:
        """Return the exact shared-group fence for a due retained stop."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT g.origin_thread_id AS thread_id, g.generation,
                       CASE
                           WHEN (g.state = {GroupState.STOPPING.sql} AND g.stop_requested_at IS NULL)
                             OR origin.activation_generation = 0 THEN 0::bigint
                           ELSE origin.activation_generation
                       END AS activation_generation,
                       CASE
                           WHEN EXISTS (
                               SELECT 1 FROM agent_thread_sandbox_memberships m
                               WHERE m.sandbox_id = g.id
                           ) AND NOT EXISTS (
                               SELECT 1 FROM agent_thread_sandbox_memberships m
                               WHERE m.sandbox_id = g.id
                                 AND COALESCE(m.context_status, '') <> 'lost'
                                 AND COALESCE(m.native_state, '') <> 'lost'
                           ) THEN 'lost'
                           WHEN g.state IN {sql_list(GroupState.STOPPED_DIRTY, GroupState.LOST)} THEN 'lost'
                           WHEN origin.activation_generation = 0 THEN 'orphaned_attempt'
                           WHEN g.state = {GroupState.STOPPING.sql} AND g.stop_requested_at IS NULL
                               THEN 'orphaned_attempt'
                           ELSE 'idle'
                       END AS reason,
                       TRUE AS shared_live,
                       (
                           SELECT COALESCE(
                               jsonb_object_agg(m.thread_id, m.activation_generation),
                               '{{}}'::jsonb
                           )
                           FROM agent_thread_sandbox_memberships m
                           WHERE m.sandbox_id = g.id
                       ) AS member_activation_generations
                FROM agent_sandboxes g
                JOIN agent_thread_sandbox_memberships origin
                  ON origin.sandbox_id = g.id AND origin.thread_id = g.origin_thread_id
                WHERE g.id = $1 AND g.origin_thread_id = $2 AND g.host = $3
                  AND g.container_id = $4 AND g.generation = $5
                  AND g.active_operation_count = 0
                  AND NOT {member_turn_running("g")}
                  AND (
                        (state = {GroupState.RUNNING.sql} AND idle_deadline IS NOT NULL
                            AND idle_deadline <= clock_timestamp())
                        OR state IN {sql_list(GroupState.STOPPING, GroupState.STOPPED_DIRTY, GroupState.LOST)}
                  )
                """,
                sandbox_id,
                thread_id,
                host,
                container_id,
                sandbox_generation,
            )
        if row is None:
            return None
        result = dict(row)
        result["member_activation_generations"] = load_json(result["member_activation_generations"])
        return result
