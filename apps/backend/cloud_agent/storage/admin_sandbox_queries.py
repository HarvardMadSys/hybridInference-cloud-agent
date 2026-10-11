"""Cross-user sandbox inventory using explicit, content-free projections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.admin_observability import encode_cursor
from cloud_agent.admin_sandboxes import (
    AdminSandbox,
    AdminSandboxDetail,
    AdminSandboxList,
    AdminSandboxMember,
    SandboxCursor,
    SandboxStateFilter,
    resource_usage_view,
    sandbox_cursor_scope,
)
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg


class AdminSandboxQueries:
    """Read one row per shared group, independent of job history."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the shared Agent database pool."""
        self._pool = pool

    async def list_running(self, *, cursor: SandboxCursor | None, limit: int) -> AdminSandboxList:
        """Include retained idle groups and paginate before joining their members."""
        return await self.list_sandboxes(cursor=cursor, limit=limit, state="running")

    async def list_sandboxes(
        self, *, cursor: SandboxCursor | None, limit: int, state: SandboxStateFilter
    ) -> AdminSandboxList:
        """Read a bounded lifecycle-filtered page with a matching count."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            observed_at = await conn.fetchval("SELECT NOW()")
            total_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_sandboxes WHERE $1 = 'all' OR state = $1", state
            )
            rows = await conn.fetch(
                """
                WITH page AS (
                    SELECT g.id, g.generation, g.stop_requested_at,
                           g.origin_thread_id, g.user_id, g.host,
                           g.container_id, g.container_name,
                           g.runtime_id, g.runtime_version, g.execution_profile,
                           g.state, g.memory_limit, g.cpu_limit, g.disk_limit_mib,
                           g.resident_slot_capacity, g.active_operation_count, g.snapshot_state,
                           g.terminal_count, g.terminal_count_observed_at,
                           g.resource_snapshot, g.resource_sampled_at, g.resource_received_at,
                           g.created_at, g.last_activity_at, g.idle_deadline, g.updated_at
                    FROM agent_sandboxes g
                    WHERE ($3 = 'all' OR g.state = $3)
                      AND ($1::text IS NULL OR g.id > $1)
                    ORDER BY g.id LIMIT $2
                )
                SELECT g.id, g.generation, g.stop_requested_at, g.origin_thread_id,
                       CASE WHEN t.title_source IN ('generated', 'manual')
                            THEN LEFT(t.title, 160) END AS title,
                       LEFT(t.repo, 256) AS repo, LEFT(g.user_id, 256) AS user_id,
                       g.host, g.container_id, g.container_name,
                       g.runtime_id, g.runtime_version, g.execution_profile,
                       g.state, g.memory_limit, g.cpu_limit, g.disk_limit_mib,
                       g.resident_slot_capacity, g.active_operation_count, g.snapshot_state,
                       g.terminal_count, g.terminal_count_observed_at,
                       g.resource_snapshot, g.resource_sampled_at, g.resource_received_at,
                       members.member_count, members.active_attempt_count,
                       g.created_at, g.last_activity_at, g.idle_deadline, g.updated_at,
                       h.last_heartbeat_at AS host_last_heartbeat_at
                FROM page g
                JOIN agent_threads t ON t.id = g.origin_thread_id AND t.user_id = g.user_id
                LEFT JOIN agent_runner_hosts h ON h.host = g.host
                CROSS JOIN LATERAL (
                    SELECT COUNT(*) FILTER (WHERE m.resident_slot_id IS NOT NULL) AS member_count,
                           COUNT(*) FILTER (WHERE m.active_attempt_id IS NOT NULL) AS active_attempt_count
                    FROM agent_thread_sandbox_memberships m
                    WHERE m.sandbox_id = g.id
                ) members
                ORDER BY g.id
                """,
                cursor.after_id if cursor else None,
                limit + 1,
                state,
            )
        sandboxes = []
        for row in rows[:limit]:
            item = dict(row)
            item["resource_usage"] = resource_usage_view(
                load_json(item.pop("resource_snapshot")),
                sampled_at=item.pop("resource_sampled_at"),
                received_at=item.pop("resource_received_at"),
                now=observed_at,
            )
            sandboxes.append(AdminSandbox.model_validate(item))
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(
                SandboxCursor(scope=sandbox_cursor_scope(state), after_id=sandboxes[-1].id)
            )
        return AdminSandboxList(
            sandboxes=sandboxes,
            total_count=total_count,
            observed_at=observed_at,
            next_cursor=next_cursor,
        )

    async def detail(self, sandbox_id: str) -> AdminSandboxDetail | None:
        """Inspect one group and its current bounded membership in a single snapshot."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            observed_at = await conn.fetchval("SELECT NOW()")
            row = await conn.fetchrow(
                """
                SELECT g.id, g.generation, g.stop_requested_at, g.origin_thread_id,
                       CASE WHEN t.title_source IN ('generated', 'manual')
                            THEN LEFT(t.title, 160) END AS title,
                       LEFT(t.repo, 256) AS repo, LEFT(g.user_id, 256) AS user_id,
                       g.host, g.container_id, g.container_name,
                       g.runtime_id, g.runtime_version, g.execution_profile,
                       g.state, g.memory_limit, g.cpu_limit, g.disk_limit_mib,
                       g.resident_slot_capacity, g.active_operation_count, g.snapshot_state,
                       g.terminal_count, g.terminal_count_observed_at,
                       g.resource_snapshot, g.resource_sampled_at, g.resource_received_at,
                       members.member_count, members.active_attempt_count,
                       g.created_at, g.last_activity_at, g.idle_deadline, g.updated_at,
                       h.last_heartbeat_at AS host_last_heartbeat_at
                FROM agent_sandboxes g
                JOIN agent_threads t ON t.id = g.origin_thread_id AND t.user_id = g.user_id
                LEFT JOIN agent_runner_hosts h ON h.host = g.host
                CROSS JOIN LATERAL (
                    SELECT COUNT(*) FILTER (WHERE m.resident_slot_id IS NOT NULL) AS member_count,
                           COUNT(*) FILTER (WHERE m.active_attempt_id IS NOT NULL) AS active_attempt_count
                    FROM agent_thread_sandbox_memberships m WHERE m.sandbox_id = g.id
                ) members
                WHERE g.id = $1
                """,
                sandbox_id,
            )
            if row is None:
                return None
            member_rows = await conn.fetch(
                """
                SELECT m.thread_id, LEFT(m.user_id, 256) AS user_id,
                       CASE WHEN t.title_source IN ('generated', 'manual')
                            THEN LEFT(t.title, 160) END AS title,
                       m.resident_slot_id, m.resident_state, m.context_status, m.native_state,
                       m.active_attempt_id, m.last_settled_job_id, m.created_at, m.updated_at
                FROM agent_thread_sandbox_memberships m
                JOIN agent_threads t ON t.id = m.thread_id AND t.user_id = m.user_id
                WHERE m.sandbox_id = $1 AND m.resident_slot_id IS NOT NULL
                ORDER BY m.resident_slot_id, m.thread_id LIMIT 128
                """,
                sandbox_id,
            )
        item = dict(row)
        item["resource_usage"] = resource_usage_view(
            load_json(item.pop("resource_snapshot")),
            sampled_at=item.pop("resource_sampled_at"),
            received_at=item.pop("resource_received_at"),
            now=observed_at,
        )
        return AdminSandboxDetail(
            sandbox=AdminSandbox.model_validate(item),
            members=[AdminSandboxMember.model_validate(dict(member)) for member in member_rows],
            observed_at=observed_at,
        )
