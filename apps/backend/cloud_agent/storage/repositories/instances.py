"""Control-plane process reports (``agent_control_plane_instances``)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    import asyncpg

#: How long a report stays current. Processes report every five seconds; three
#: missed reports make one stale.
STALE_AFTER_SECONDS = 20
#: Stopped or stale reports older than this are deleted.
RETAIN_SECONDS = 7 * 24 * 3600


class InstanceRepository:
    """Write this process's report and read everyone's."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind to the control plane's pool."""
        self._pool = pool

    async def report(
        self,
        *,
        instance_id: str,
        role: str,
        machine: str,
        build_sha: str,
        capabilities: Sequence[str],
        adopted_settings_revision: int,
        restart_required: Sequence[str],
        details: Mapping[str, Any],
        started_at: datetime,
    ) -> None:
        """Insert or refresh this process's row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_control_plane_instances (
                    instance_id, role, machine, build_sha, capabilities,
                    adopted_settings_revision, restart_required, details, started_at,
                    last_seen_at
                ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8::jsonb, $9, NOW())
                ON CONFLICT (instance_id) DO UPDATE SET
                    build_sha = EXCLUDED.build_sha,
                    capabilities = EXCLUDED.capabilities,
                    adopted_settings_revision = EXCLUDED.adopted_settings_revision,
                    restart_required = EXCLUDED.restart_required,
                    details = EXCLUDED.details,
                    last_seen_at = NOW(),
                    stopped_at = NULL
                """,
                instance_id,
                role,
                machine[:255] or "unknown",
                build_sha[:64],
                json.dumps(sorted(set(capabilities))),
                adopted_settings_revision,
                json.dumps(sorted(set(restart_required))),
                json.dumps(dict(details)),
                started_at,
            )

    async def stop(self, instance_id: str) -> None:
        """Mark this process stopped on a clean shutdown."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_control_plane_instances SET stopped_at = NOW() "
                "WHERE instance_id = $1",
                instance_id,
            )

    async def list_instances(self) -> list[dict[str, Any]]:
        """Every retained report, with whether it is current, newest first."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_control_plane_instances "
                "WHERE last_seen_at < NOW() - make_interval(secs => $1)",
                RETAIN_SECONDS,
            )
            rows = await conn.fetch(
                """
                SELECT instance_id, role, machine, build_sha, capabilities,
                       adopted_settings_revision, restart_required, details,
                       started_at, last_seen_at, stopped_at,
                       (stopped_at IS NULL
                        AND last_seen_at >= NOW() - make_interval(secs => $1)) AS current
                FROM agent_control_plane_instances
                ORDER BY last_seen_at DESC
                """,
                STALE_AFTER_SECONDS,
            )
        result = []
        for row in rows:
            item = dict(row)
            item["capabilities"] = load_json(item["capabilities"]) or []
            item["restart_required"] = load_json(item["restart_required"]) or []
            item["details"] = load_json(item["details"]) or {}
            result.append(item)
        return result

    async def unidentified_sessions(self) -> list[dict[str, Any]]:
        """Database sessions on this database that no reporting process opened.

        Every control-plane process names its connections
        ``cloud-agent:<role>:<instance>``. A session without that name is an
        older release, a tool, or something else: it is listed, never assumed
        harmless. Only what identifies it is returned, not its query.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pid, usename, application_name, client_addr::text AS client_addr,
                       backend_start
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND backend_type = 'client backend'
                  AND COALESCE(application_name, '') NOT LIKE 'cloud-agent:%'
                ORDER BY backend_start
                LIMIT 50
                """
            )
        return [dict(row) for row in rows]
