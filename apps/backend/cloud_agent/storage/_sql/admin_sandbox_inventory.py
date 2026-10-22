"""The content-free projection shared by inventory pages and changed IDs."""

from __future__ import annotations

from typing import Any

from cloud_agent.admin_sandboxes import AdminSandbox, resource_usage_view
from cloud_agent.storage._sql.rows import load_json


async def inventory_rows(
    conn: Any,
    *,
    state: str,
    include_archived: bool,
    after_id: str | None = None,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> list[Any]:
    """Bound membership aggregation to the selected page or changed identities."""
    return await conn.fetch(
        """
        WITH page AS (
            SELECT g.* FROM agent_sandboxes g
            WHERE ($3='all' OR g.state=$3) AND ($4 OR g.archived_at IS NULL)
              AND ($1::text IS NULL OR g.id>$1)
              AND ($5::text[] IS NULL OR g.id=ANY($5))
            ORDER BY g.id LIMIT $2
        )
        SELECT g.id,g.generation,g.stop_requested_at,g.archived_at,g.origin_thread_id,
               g.deletion_state,g.deletion_discard,g.deletion_request_id,g.deletion_requested_at,
               g.deletion_archive_key,g.deletion_archive_version_id,g.deletion_sha256,
               g.deletion_size_bytes,g.deletion_error,g.deletion_retry_exhausted_at,g.deleted_at,
               CASE WHEN t.title_source IN ('generated','manual') THEN LEFT(t.title,160) END AS title,
               LEFT(t.repo,256) AS repo,LEFT(g.user_id,256) AS user_id,
               g.host,g.container_id,g.container_name,g.runtime_id,g.runtime_version,
               g.execution_profile,g.state,g.memory_limit,g.cpu_limit,g.disk_limit_mib,
               g.resident_slot_capacity,g.active_operation_count,g.snapshot_state,
               g.terminal_count,g.terminal_count_observed_at,
               g.resource_snapshot,g.resource_sampled_at,g.resource_received_at,
               members.member_count,members.active_attempt_count,
               g.created_at,g.last_activity_at,g.idle_deadline,g.updated_at,
               h.last_heartbeat_at AS host_last_heartbeat_at
        FROM page g
        JOIN agent_threads t ON t.id=g.origin_thread_id AND t.user_id=g.user_id
        LEFT JOIN agent_runner_hosts h ON h.host=g.host
        CROSS JOIN LATERAL (
            SELECT COUNT(*) FILTER (WHERE m.resident_slot_id IS NOT NULL) AS member_count,
                   COUNT(*) FILTER (WHERE m.active_attempt_id IS NOT NULL) AS active_attempt_count
            FROM agent_thread_sandbox_memberships m WHERE m.sandbox_id=g.id
        ) members
        ORDER BY g.id
        """,
        after_id,
        limit,
        state,
        include_archived,
        ids,
    )


def inventory_view(row: Any, observed_at: Any) -> AdminSandbox:
    """Validate the public projection and age samples against its observation."""
    item = dict(row)
    item["resource_usage"] = resource_usage_view(
        load_json(item.pop("resource_snapshot")),
        sampled_at=item.pop("resource_sampled_at"),
        received_at=item.pop("resource_received_at"),
        now=observed_at,
    )
    return AdminSandbox.model_validate(item)
