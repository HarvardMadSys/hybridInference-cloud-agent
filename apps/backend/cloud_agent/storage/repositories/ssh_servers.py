"""Postgres persistence for owners' SSH servers and their per-chat selection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.ssh_servers import (
    CHECK_EVENTS,
    SshServerConflict,
    SshServerUnavailable,
)
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import Mapping

_COLUMNS = (
    "id, user_id, name, description, address, port, username, pinned_ipv4, "
    "host_key_type, host_key, scanned_ipv4, scanned_host_keys, public_key, "
    "private_key_ciphertext, pending_public_key, pending_private_key_ciphertext, "
    "status, last_check_at, last_check_error, revision, created_at, updated_at, archived_at"
)
#: What :meth:`PostgresSshServerRepository.update_ssh_server` may change.
_UPDATABLE = frozenset(
    {
        "name",
        "description",
        "address",
        "port",
        "username",
        "pinned_ipv4",
        "host_key_type",
        "host_key",
        "scanned_ipv4",
        "scanned_host_keys",
        "public_key",
        "private_key_ciphertext",
        "pending_public_key",
        "pending_private_key_ciphertext",
        "status",
        "last_check_at",
        "last_check_error",
    }
)
_JSON_COLUMNS = frozenset({"scanned_host_keys"})
_CREATE_COLUMNS = (
    "id",
    "name",
    "description",
    "address",
    "port",
    "username",
    "public_key",
    "private_key_ciphertext",
)


def _server(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["id"] = str(result["id"])
    result["scanned_host_keys"] = load_json(result.get("scanned_host_keys"))
    return result


async def _event(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    server_id: str,
    kind: str,
    thread_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO agent_ssh_server_events (user_id, server_id, thread_id, kind, detail)
        VALUES ($1, $2::uuid, $3, $4, $5::jsonb)
        """,
        user_id,
        server_id,
        thread_id,
        kind,
        json.dumps(detail or {}),
    )


_BINDINGS_SQL = """
    SELECT b.server_id::text AS server_id, b.enabled, s.name, s.status
    FROM agent_ssh_thread_bindings b
    JOIN agent_ssh_servers s ON s.id = b.server_id
    WHERE b.thread_id = $1 AND s.archived_at IS NULL
    ORDER BY s.name
"""


class PostgresSshServerRepository:
    """Owners' SSH servers, the chats that enable them, and what they did."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def create_ssh_server(self, *, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a pending server with its first key pair and record the event."""
        missing = [column for column in _CREATE_COLUMNS if column not in values]
        if missing:
            raise ValueError(f"SSH server is missing {missing}")
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO agent_ssh_servers
                        (id, user_id, name, description, address, port, username,
                         public_key, private_key_ciphertext)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING {_COLUMNS}
                    """,
                    values["id"],
                    user_id,
                    *(values[column] for column in _CREATE_COLUMNS[1:]),
                )
            except asyncpg.UniqueViolationError as exc:
                raise SshServerConflict(values["name"]) from exc
            assert row is not None
            await _event(conn, user_id=user_id, server_id=values["id"], kind="created")
        return _server(row)

    async def list_ssh_servers(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every unarchived server of one owner, oldest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS} FROM agent_ssh_servers
                WHERE user_id = $1 AND archived_at IS NULL
                ORDER BY created_at, id
                """,
                user_id,
            )
        return [_server(row) for row in rows]

    async def get_ssh_server(self, *, user_id: str, server_id: str) -> dict[str, Any] | None:
        """One unarchived server of this owner, or ``None``."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_COLUMNS} FROM agent_ssh_servers
                WHERE id = $1::uuid AND user_id = $2 AND archived_at IS NULL
                """,
                server_id,
                user_id,
            )
        return None if row is None else _server(row)

    async def update_ssh_server(
        self,
        *,
        user_id: str,
        server_id: str,
        revision: int,
        values: dict[str, Any],
        event: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Apply ``values`` if the row is still at ``revision``; ``None`` if it moved or is gone."""
        unknown = set(values) - _UPDATABLE
        if unknown or not values:
            raise ValueError(f"SSH server columns cannot be updated: {sorted(unknown)}")
        columns = sorted(values)
        assignments = ", ".join(
            f"{column} = ${index + 4}" + ("::jsonb" if column in _JSON_COLUMNS else "")
            for index, column in enumerate(columns)
        )
        arguments = [
            json.dumps(values[column]) if column in _JSON_COLUMNS else values[column]
            for column in columns
        ]
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_ssh_servers
                    SET {assignments}, revision = revision + 1, updated_at = NOW()
                    WHERE id = $1::uuid AND user_id = $2 AND revision = $3
                      AND archived_at IS NULL
                    RETURNING {_COLUMNS}
                    """,
                    server_id,
                    user_id,
                    revision,
                    *arguments,
                )
            except asyncpg.UniqueViolationError as exc:
                raise SshServerConflict(str(values.get("name"))) from exc
            if row is None:
                return None
            await _event(conn, user_id=user_id, server_id=server_id, kind=event, detail=detail)
        return _server(row)

    async def archive_ssh_server(self, *, user_id: str, server_id: str) -> dict[str, Any] | None:
        """Archive a server and erase its key material; ``None`` if it was not found."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                UPDATE agent_ssh_servers
                SET archived_at = NOW(), status = 'pending',
                    private_key_ciphertext = NULL,
                    pending_public_key = NULL, pending_private_key_ciphertext = NULL,
                    revision = revision + 1, updated_at = NOW()
                WHERE id = $1::uuid AND user_id = $2 AND archived_at IS NULL
                RETURNING {_COLUMNS}
                """,
                server_id,
                user_id,
            )
            if row is None:
                return None
            await _event(conn, user_id=user_id, server_id=server_id, kind="archived")
        return _server(row)

    async def record_ssh_check(
        self, *, user_id: str, server_id: str, kind: str, detail: dict[str, Any]
    ) -> None:
        """Record one check against the server, counted toward the hourly limit."""
        if kind not in CHECK_EVENTS:
            raise ValueError(f"{kind!r} is not a check event")
        async with self._pool.acquire() as conn:
            await _event(conn, user_id=user_id, server_id=server_id, kind=kind, detail=detail)

    async def count_recent_checks(self, *, user_id: str, seconds: int) -> int:
        """How many checks this owner ran in the last ``seconds``."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT count(*) FROM agent_ssh_server_events
                WHERE user_id = $1 AND kind = ANY($2::text[])
                  AND created_at > NOW() - make_interval(secs => $3)
                """,
                user_id,
                list(CHECK_EVENTS),
                float(seconds),
            )
        return int(count or 0)

    async def list_thread_ssh_bindings(
        self, *, user_id: str, thread_id: str
    ) -> list[dict[str, Any]] | None:
        """This chat's selection; ``None`` when the chat is not this owner's."""
        async with self._pool.acquire() as conn:
            owned = await conn.fetchval(
                "SELECT 1 FROM agent_threads WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                thread_id,
                user_id,
            )
            if owned is None:
                return None
            rows = await conn.fetch(_BINDINGS_SQL, thread_id)
        return [dict(row) for row in rows]

    async def replace_thread_ssh_bindings(
        self, *, user_id: str, thread_id: str, server_ids: list[str]
    ) -> list[dict[str, Any]] | None:
        """Enable exactly ``server_ids`` for the chat and disable the rest."""
        wanted = list(dict.fromkeys(server_ids))
        async with self._pool.acquire() as conn, conn.transaction():
            owned = await conn.fetchval(
                """
                SELECT 1 FROM agent_threads
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                FOR UPDATE
                """,
                thread_id,
                user_id,
            )
            if owned is None:
                return None
            previous = {
                str(row["server_id"]): bool(row["enabled"])
                for row in await conn.fetch(
                    """
                    SELECT server_id, enabled FROM agent_ssh_thread_bindings
                    WHERE thread_id = $1 FOR UPDATE
                    """,
                    thread_id,
                )
            }
            live = {
                str(row["id"]): row["status"]
                for row in await conn.fetch(
                    """
                    SELECT id, status FROM agent_ssh_servers
                    WHERE user_id = $1 AND id = ANY($2::uuid[]) AND archived_at IS NULL
                    """,
                    user_id,
                    wanted,
                )
            }
            # A server must have passed its check to be turned on. One already
            # on for this chat may stay on while it is being checked again: it
            # is left out of the chat's state until it is ready, and keeping it
            # lets the owner change other servers without losing it.
            refused = [
                server_id
                for server_id in wanted
                if server_id not in live
                or (live[server_id] != "ready" and previous.get(server_id) is not True)
            ]
            if refused:
                raise SshServerUnavailable(refused)
            for server_id in wanted:
                if previous.get(server_id) is True:
                    continue
                await conn.execute(
                    """
                    INSERT INTO agent_ssh_thread_bindings (thread_id, server_id, enabled)
                    VALUES ($1, $2::uuid, TRUE)
                    ON CONFLICT (thread_id, server_id)
                    DO UPDATE SET enabled = TRUE, updated_at = NOW()
                    """,
                    thread_id,
                    server_id,
                )
                await _event(
                    conn, user_id=user_id, server_id=server_id, kind="enabled", thread_id=thread_id
                )
            for server_id, enabled in previous.items():
                if not enabled or server_id in wanted:
                    continue
                await conn.execute(
                    """
                    UPDATE agent_ssh_thread_bindings SET enabled = FALSE, updated_at = NOW()
                    WHERE thread_id = $1 AND server_id = $2::uuid
                    """,
                    thread_id,
                    server_id,
                )
                await _event(
                    conn, user_id=user_id, server_id=server_id, kind="disabled", thread_id=thread_id
                )
            rows = await conn.fetch(_BINDINGS_SQL, thread_id)
        return [dict(row) for row in rows]

    async def ssh_group_state(self, *, thread_id: str) -> dict[str, Any] | None:
        """The SSH state of the sandbox group ``thread_id`` belongs to, at a fresh revision."""
        async with self._pool.acquire() as conn:
            # One query for every claim, so a group that never selected a
            # server costs nothing more than this.
            selected = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM agent_ssh_thread_bindings b
                    WHERE b.thread_id = $1
                       OR b.thread_id IN (
                           SELECT member.thread_id
                           FROM agent_thread_sandbox_memberships mine
                           JOIN agent_thread_sandbox_memberships member
                             ON member.sandbox_id = mine.sandbox_id
                           WHERE mine.thread_id = $1
                       )
                )
                """,
                thread_id,
            )
            if not selected:
                return None
            # Taken before the state is read: a change committed before this
            # number is visible below, and one committed after it sends its
            # own, higher revision.
            revision = int(await conn.fetchval("SELECT nextval('agent_ssh_access_revision_seq')"))
            sandbox_id = await conn.fetchval(
                "SELECT sandbox_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
                thread_id,
            )
            slots: dict[str, int | None] = {thread_id: None}
            if sandbox_id is not None:
                for member in await conn.fetch(
                    """
                    SELECT thread_id, resident_slot_id FROM agent_thread_sandbox_memberships
                    WHERE sandbox_id = $1
                    """,
                    sandbox_id,
                ):
                    slots[str(member["thread_id"])] = member["resident_slot_id"]
            rows = await conn.fetch(
                f"""
                SELECT b.thread_id AS binding_thread_id, b.enabled AS binding_enabled,
                       t.deleted_at AS thread_deleted_at,
                       {", ".join(f"s.{column.strip()}" for column in _COLUMNS.split(","))}
                FROM agent_ssh_thread_bindings b
                JOIN agent_threads t ON t.id = b.thread_id
                JOIN agent_ssh_servers s ON s.id = b.server_id AND s.user_id = t.user_id
                WHERE b.thread_id = ANY($1::text[])
                ORDER BY b.thread_id, s.name
                """,
                list(slots),
            )
        if not rows:
            return None
        servers: dict[str, list[dict[str, Any]]] = {member: [] for member in slots}
        for row in rows:
            # A deleted chat, or a failed Fork's placeholder tombstoned like
            # one, keeps its membership and bindings but never runs again, and
            # its owner can no longer switch them off: it opens nothing.
            if (
                row["binding_enabled"]
                and row["thread_deleted_at"] is None
                and row["status"] == "ready"
                and row["archived_at"] is None
                and row["private_key_ciphertext"] is not None
            ):
                servers[str(row["binding_thread_id"])].append(_server(row))
        return {
            "revision": revision,
            "sandbox_id": sandbox_id,
            "members": [
                {"thread_id": member, "slot_id": slot, "servers": servers[member]}
                for member, slot in slots.items()
            ],
        }

    async def running_groups_for_ssh_change(
        self, *, thread_ids: list[str] | None = None, server_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Running groups an SSH change reaches: each group's host and the chats it is known by.

        A push is for the whole group, so it goes to the host the group is on,
        not to wherever one member's latest job ran. It names the group by its
        origin chat, which a sandbox manager re-learns from the container after
        a restart. ``resident_thread_id`` is another chat holding a slot, which
        every manifest the manager is sent names: a deleted origin chat gives
        its slot up, and the next manifest drops it.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH touched AS (
                    SELECT DISTINCT m.sandbox_id
                    FROM agent_thread_sandbox_memberships m
                    WHERE m.thread_id = ANY($1::text[])
                       OR m.thread_id IN (
                           SELECT thread_id FROM agent_ssh_thread_bindings
                           WHERE server_id = $2::uuid
                       )
                )
                SELECT s.id AS sandbox_id, s.origin_thread_id, s.host,
                       (
                           SELECT m.thread_id
                           FROM agent_thread_sandbox_memberships m
                           WHERE m.sandbox_id = s.id
                             AND m.thread_id <> s.origin_thread_id
                             AND m.resident_slot_id IS NOT NULL
                             AND m.native_session_id IS NOT NULL
                           ORDER BY m.resident_slot_id
                           LIMIT 1
                       ) AS resident_thread_id
                FROM touched
                JOIN agent_sandboxes s ON s.id = touched.sandbox_id AND s.state = 'running'
                ORDER BY s.id
                """,
                list(thread_ids or []),
                server_id,
            )
        return [dict(row) for row in rows]
