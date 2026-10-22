"""Consistent initial pages and indexed, commit-safe inventory delta reads.

Only a snapshot's xmax and in-progress top-level XIDs are needed to find
transactions that can become visible later. BIGSERIAL alone loses a delayed
commit; using xmin alone repeatedly scans unrelated changes behind one slow
transaction. Per-entity markers coalesce updates without a fleet-wide lock.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.admin_observability import encode_cursor
from cloud_agent.admin_sandbox_inventory import (
    AdminSandboxHostObservation,
    AdminSandboxInventoryPage,
    SandboxInventoryCursor,
    inventory_scope,
)
from cloud_agent.admin_sandboxes import AdminSandbox
from cloud_agent.storage._sql.admin_sandbox_inventory import inventory_rows, inventory_view
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    import asyncpg


class AdminSandboxInventory:
    """Store short-lived projected pages; read only changed identities thereafter."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def read(
        self,
        *,
        cursor: SandboxInventoryCursor | None,
        state: str,
        include_archived: bool,
        limit: int,
    ) -> AdminSandboxInventoryPage:
        """Return one projected snapshot page or one bounded change page."""
        scope = inventory_scope(state, include_archived, limit)
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            if cursor is None:
                await _prune(conn)
                cursor = await _capture(conn, state, include_archived, limit, scope)
            if cursor.snapshot_id:
                return await _snapshot_page(conn, cursor)
            if not cursor.database_snapshot:
                raise ValueError("Invalid inventory cursor.")
            return await _changes(conn, cursor, state, include_archived, limit)


async def _capture(
    conn: Any, state: str, include_archived: bool, limit: int, scope: str
) -> SandboxInventoryCursor:
    observed_at, snapshot = await conn.fetchrow("SELECT NOW(),pg_current_snapshot()::text")
    identity = uuid4().hex
    await conn.execute(
        """INSERT INTO agent_admin_sandbox_inventory_snapshots
           (id,scope,observed_at,expires_at,database_snapshot,total_count)
           VALUES ($1,$2,$3,NOW()+interval '5 minutes',$4::text::pg_snapshot,0)""",
        identity,
        scope,
        observed_at,
        snapshot,
    )
    await _capture_pages(conn, identity, observed_at, state, include_archived, limit)
    return SandboxInventoryCursor(scope=scope, snapshot_id=identity)


async def _capture_pages(
    conn: Any, identity: str, observed_at: Any, state: str, include_archived: bool, limit: int
) -> None:
    """Keep application memory bounded while capturing one repeatable-read view."""
    after_id, page, total = None, 0, 0
    while True:
        rows = await inventory_rows(
            conn, state=state, include_archived=include_archived, after_id=after_id, limit=limit + 1
        )
        sandboxes = [
            inventory_view(row, observed_at).model_dump(mode="json") for row in rows[:limit]
        ]
        await conn.execute(
            "INSERT INTO agent_admin_sandbox_inventory_pages VALUES ($1,$2,$3::jsonb)",
            identity,
            page,
            json.dumps(sandboxes),
        )
        total += len(sandboxes)
        if len(rows) <= limit:
            break
        after_id = sandboxes[-1]["id"]
        page += 1
    await conn.execute(
        "UPDATE agent_admin_sandbox_inventory_snapshots SET total_count=$2 WHERE id=$1",
        identity,
        total,
    )


async def _snapshot_page(conn: Any, cursor: SandboxInventoryCursor) -> AdminSandboxInventoryPage:
    row = await conn.fetchrow(
        """SELECT s.observed_at,s.total_count,p.sandboxes,s.database_snapshot::text AS database_snapshot,
           EXISTS (SELECT 1 FROM agent_admin_sandbox_inventory_pages n
                   WHERE n.snapshot_id=s.id AND n.page=$2+1) AS more
           FROM agent_admin_sandbox_inventory_snapshots s
           JOIN agent_admin_sandbox_inventory_pages p ON p.snapshot_id=s.id AND p.page=$2
           WHERE s.id=$1 AND s.scope=$3 AND s.expires_at>NOW()""",
        cursor.snapshot_id,
        cursor.page,
        cursor.scope,
    )
    if row is None:
        return AdminSandboxInventoryPage(
            observed_at=await conn.fetchval("SELECT NOW()"), reset_required=True
        )
    continuation = cursor.model_copy(update={"page": cursor.page + 1})
    changes = SandboxInventoryCursor(scope=cursor.scope, database_snapshot=row["database_snapshot"])
    return AdminSandboxInventoryPage(
        sandboxes=[AdminSandbox.model_validate(item) for item in load_json(row["sandboxes"])],
        total_count=row["total_count"],
        observed_at=row["observed_at"],
        next_cursor=encode_cursor(continuation) if row["more"] else None,
        change_cursor=encode_cursor(changes) if not row["more"] else None,
    )


async def _changes(
    conn: Any, cursor: SandboxInventoryCursor, state: str, include_archived: bool, limit: int
) -> AdminSandboxInventoryPage:
    metadata = await conn.fetchrow(
        """SELECT NOW() AS observed_at,pg_current_snapshot()::text AS snapshot,
           COALESCE((SELECT floor_xid FROM agent_admin_sandbox_change_floor WHERE id),'0')
               > pg_snapshot_xmin($1::text::pg_snapshot) AS expired""",
        cursor.database_snapshot,
    )
    if metadata["expired"]:
        return AdminSandboxInventoryPage(observed_at=metadata["observed_at"], reset_required=True)
    # Keep the target fixed while paging. A change behind the current page is
    # still newer than/in-progress in this target and is caught next round.
    target = cursor.target_snapshot or metadata["snapshot"]
    changes = await conn.fetch(
        """WITH changed AS MATERIALIZED (
            SELECT kind,entity_id FROM agent_admin_sandbox_changes
            WHERE change_xid>=pg_snapshot_xmax($1::text::pg_snapshot)
            UNION ALL
            SELECT kind,entity_id FROM agent_admin_sandbox_changes
            WHERE change_xid=ANY(ARRAY(SELECT pg_snapshot_xip($1::text::pg_snapshot)))
        ) SELECT kind,entity_id FROM changed
          WHERE $2::text IS NULL OR (kind,entity_id)>($2,$3)
          ORDER BY kind,entity_id LIMIT $4""",
        cursor.database_snapshot,
        cursor.after_kind,
        cursor.after_id,
        limit + 1,
    )
    selected = changes[:limit]
    ids = [row["entity_id"] for row in selected if row["kind"] == "sandbox"]
    host_ids = [row["entity_id"] for row in selected if row["kind"] == "host"]
    sandboxes = (
        [
            inventory_view(row, metadata["observed_at"])
            for row in await inventory_rows(
                conn, state=state, include_archived=include_archived, ids=ids
            )
        ]
        if ids
        else []
    )
    hosts = await _hosts(conn, host_ids)
    present = {row.id for row in sandboxes}
    continuation = cursor.model_copy(
        update={
            "target_snapshot": target,
            "after_kind": selected[-1]["kind"] if selected else None,
            "after_id": selected[-1]["entity_id"] if selected else None,
        }
    )
    return AdminSandboxInventoryPage(
        mode="delta",
        sandboxes=sandboxes,
        deleted_ids=[identity for identity in ids if identity not in present],
        hosts=hosts,
        observed_at=metadata["observed_at"],
        next_cursor=encode_cursor(continuation) if len(changes) > limit else None,
        change_cursor=encode_cursor(
            SandboxInventoryCursor(scope=cursor.scope, database_snapshot=target)
        )
        if len(changes) <= limit
        else None,
    )


async def _hosts(conn: Any, ids: list[str]) -> list[AdminSandboxHostObservation]:
    if not ids:
        return []
    rows = await conn.fetch(
        """SELECT wanted.host,h.last_heartbeat_at FROM unnest($1::text[]) wanted(host)
           LEFT JOIN agent_runner_hosts h ON h.host=wanted.host""",
        ids,
    )
    return [AdminSandboxHostObservation.model_validate(dict(row)) for row in rows]


async def _prune(conn: Any) -> None:
    """Bound cleanup work; old cursors explicitly resync after marker retention."""
    await conn.execute(
        """DELETE FROM agent_admin_sandbox_inventory_snapshots WHERE id IN (
           SELECT id FROM agent_admin_sandbox_inventory_snapshots WHERE expires_at<=NOW()
           ORDER BY expires_at LIMIT 20)"""
    )
    await conn.execute(
        """WITH expired AS (
            SELECT kind,entity_id FROM agent_admin_sandbox_changes
            WHERE changed_at<NOW()-interval '24 hours'
              AND change_xid<pg_snapshot_xmin(pg_current_snapshot())
            ORDER BY changed_at LIMIT 500
        ), removed AS (
            DELETE FROM agent_admin_sandbox_changes c USING expired e
            WHERE c.kind=e.kind AND c.entity_id=e.entity_id RETURNING change_xid
        ) INSERT INTO agent_admin_sandbox_change_floor (id,floor_xid)
          SELECT true,(max(change_xid::text::numeric)+1)::text::xid8 FROM removed HAVING count(*)>0
          ON CONFLICT (id) DO UPDATE SET floor_xid=GREATEST(
              agent_admin_sandbox_change_floor.floor_xid,EXCLUDED.floor_xid)"""
    )
