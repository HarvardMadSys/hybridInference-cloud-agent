"""Cross-user running-container inventory using explicit metadata projections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.admin_observability import encode_cursor
from cloud_agent.admin_sandboxes import (
    SANDBOX_CURSOR_SCOPE,
    AdminSandbox,
    AdminSandboxList,
    SandboxCursor,
    resource_usage_view,
)
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg


class AdminSandboxQueries:
    """Read one row per running shared group, independent of job history."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the shared Agent database pool."""
        self._pool = pool

    async def list_running(self, *, cursor: SandboxCursor | None, limit: int) -> AdminSandboxList:
        """Include retained idle groups and paginate before joining their members."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            observed_at = await conn.fetchval("SELECT NOW()")
            total_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_sandboxes WHERE state = 'running'"
            )
            rows = await conn.fetch(
                """
                WITH page AS (
                    SELECT g.id, g.origin_thread_id, g.user_id, g.host,
                           g.container_id, g.container_name,
                           g.runtime_id, g.runtime_version, g.execution_profile,
                           g.state, g.memory_limit, g.cpu_limit,
                           g.resident_slot_capacity, g.active_operation_count, g.snapshot_state,
                           g.terminal_count, g.terminal_count_observed_at,
                           g.resource_snapshot, g.resource_sampled_at, g.resource_received_at,
                           g.created_at, g.last_activity_at, g.idle_deadline, g.updated_at
                    FROM agent_sandboxes g
                    WHERE g.state = 'running'
                      AND ($1::text IS NULL OR g.id > $1)
                    ORDER BY g.id LIMIT $2
                )
                SELECT g.id, g.origin_thread_id,
                       CASE WHEN t.title_source IN ('generated', 'manual')
                            THEN LEFT(t.title, 160) END AS title,
                       LEFT(t.repo, 256) AS repo, LEFT(g.user_id, 256) AS user_id,
                       g.host, g.container_id, g.container_name,
                       g.runtime_id, g.runtime_version, g.execution_profile,
                       g.state, g.memory_limit, g.cpu_limit,
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
                SandboxCursor(scope=SANDBOX_CURSOR_SCOPE, after_id=sandboxes[-1].id)
            )
        return AdminSandboxList(
            sandboxes=sandboxes,
            total_count=total_count,
            observed_at=observed_at,
            next_cursor=next_cursor,
        )
