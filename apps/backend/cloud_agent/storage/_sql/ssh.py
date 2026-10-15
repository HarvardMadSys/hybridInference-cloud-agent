"""SSH selection statements that run inside another repository's transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.domain.ssh_servers import SshServerUnavailable

if TYPE_CHECKING:
    import asyncpg


async def enable_ssh_servers(
    conn: asyncpg.Connection, *, user_id: str, thread_id: str, server_ids: list[str]
) -> None:
    """Enable the owner's ready servers for a new chat, or raise naming the ones that are not."""
    wanted = list(dict.fromkeys(server_ids))
    rows = await conn.fetch(
        """
        INSERT INTO agent_ssh_thread_bindings (thread_id, server_id, enabled)
        SELECT $1, s.id, TRUE
        FROM agent_ssh_servers s
        WHERE s.user_id = $2 AND s.id = ANY($3::uuid[])
          AND s.status = 'ready' AND s.archived_at IS NULL
        RETURNING server_id::text AS server_id
        """,
        thread_id,
        user_id,
        wanted,
    )
    enabled = {row["server_id"] for row in rows}
    refused = [server_id for server_id in wanted if server_id not in enabled]
    if refused:
        raise SshServerUnavailable(refused)
    await conn.execute(
        """
        INSERT INTO agent_ssh_server_events (user_id, server_id, thread_id, kind)
        SELECT $1, server_id, $2, 'enabled' FROM unnest($3::uuid[]) AS server_id
        """,
        user_id,
        thread_id,
        wanted,
    )
