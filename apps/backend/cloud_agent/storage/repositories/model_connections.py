"""Postgres persistence for owners' model connections."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.model_connections import ModelConnectionConflict
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import Mapping

_COLUMNS = (
    "id, user_id, name, base_url, protocols, models, api_key_ciphertext, "
    "credential_revision, config_revision, enabled, last_check, created_at, updated_at, "
    "deleted_at"
)
#: What :meth:`PostgresModelConnectionRepository.update_model_connection` may change.
_UPDATABLE = frozenset({"name", "base_url", "protocols", "models", "api_key_ciphertext", "enabled"})
#: Changes after which a process holding the old state must not serve a turn.
_CREDENTIAL_COLUMNS = frozenset({"base_url", "protocols", "api_key_ciphertext"})
#: Changes after which a check result no longer describes the connection.
_CHECKED_COLUMNS = _CREDENTIAL_COLUMNS | {"models"}
_ARRAY_COLUMNS = frozenset({"protocols", "models"})

#: A connection a residency may still run on: the owner's, live, enabled, and
#: at the revision the residency was started with. ``b`` is its generation-bound binding.
_STILL_CURRENT = """EXISTS (
    SELECT 1 FROM agent_model_connections c
    WHERE c.id = b.connection_id
      AND c.deleted_at IS NULL AND c.enabled
      AND c.credential_revision = b.credential_revision
)"""


def _connection(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["protocols"] = list(result.get("protocols") or [])
    result["models"] = list(result.get("models") or [])
    result["last_check"] = load_json(result.get("last_check"))
    return result


class PostgresModelConnectionRepository:
    """Owners' model connections, each read and written only for its owner."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def create_model_connection(
        self, *, user_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert an enabled connection at its first revisions."""
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO agent_model_connections
                        (id, user_id, name, base_url, protocols, models, api_key_ciphertext)
                    VALUES ($1, $2, $3, $4, $5::text[], $6::text[], $7)
                    RETURNING {_COLUMNS}
                    """,
                    values["id"],
                    user_id,
                    values["name"],
                    values["base_url"],
                    values["protocols"],
                    values["models"],
                    values["api_key_ciphertext"],
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModelConnectionConflict(values["name"]) from exc
        assert row is not None
        return _connection(row)

    async def list_model_connections(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every live connection of one owner, oldest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS} FROM agent_model_connections
                WHERE user_id = $1 AND deleted_at IS NULL
                ORDER BY created_at, id
                """,
                user_id,
            )
        return [_connection(row) for row in rows]

    async def get_model_connection(
        self, *, user_id: str, connection_id: str
    ) -> dict[str, Any] | None:
        """One connection of this owner, tombstones included, or ``None``.

        A tombstone is answered so a turn naming it is refused as deleted rather
        than as never having existed.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLUMNS} FROM agent_model_connections WHERE id = $1 AND user_id = $2",
                connection_id,
                user_id,
            )
        return None if row is None else _connection(row)

    async def update_model_connection(
        self, *, user_id: str, connection_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply ``changes``, raising whichever revisions they invalidate."""
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise ValueError(f"model connection cannot change {sorted(unknown)}")
        if not changes:
            return await self.get_model_connection(user_id=user_id, connection_id=connection_id)
        assignments: list[str] = []
        args: list[Any] = [connection_id, user_id]
        for column, value in changes.items():
            args.append(value)
            cast = "::text[]" if column in _ARRAY_COLUMNS else ""
            assignments.append(f"{column} = ${len(args)}{cast}")
        if _CREDENTIAL_COLUMNS & set(changes) or changes.get("enabled") is False:
            assignments.append("credential_revision = credential_revision + 1")
        if _CHECKED_COLUMNS & set(changes):
            # A result about the old endpoint, key or models says nothing about the new.
            assignments.append("config_revision = config_revision + 1")
            assignments.append("last_check = NULL")
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_model_connections
                    SET {", ".join(assignments)}, updated_at = NOW()
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                    RETURNING {_COLUMNS}
                    """,
                    *args,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModelConnectionConflict(str(changes.get("name"))) from exc
        return None if row is None else _connection(row)

    async def delete_model_connection(
        self, *, user_id: str, connection_id: str
    ) -> dict[str, Any] | None:
        """Keep the row as a tombstone for the turns that name it, and erase the key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agent_model_connections
                SET deleted_at = NOW(), enabled = FALSE, api_key_ciphertext = NULL,
                    credential_revision = credential_revision + 1,
                    config_revision = config_revision + 1, last_check = NULL,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING {_COLUMNS}
                """,
                connection_id,
                user_id,
            )
        return None if row is None else _connection(row)

    async def reserve_model_connection_check(
        self, *, user_id: str, connection_id: str, limit: int, seconds: int
    ) -> dict[str, Any]:
        """Count one check against the owner's limit before anything dials out.

        One transaction under a per-owner advisory lock, so two replicas, or
        two requests on one, cannot both take the last slot; the attempt is
        recorded before the call, so a cancelled check still counts. Answers
        ``{"connection": row}`` -- the live connection exactly as it is
        checked -- or ``{"error": "not_found" | "limited"}``.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"agent-model-connection-check:{user_id}",
            )
            row = await conn.fetchrow(
                f"""
                SELECT {_COLUMNS} FROM agent_model_connections
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                """,
                connection_id,
                user_id,
            )
            if row is None:
                return {"error": "not_found"}
            used = await conn.fetchval(
                """
                SELECT count(*) FROM agent_model_connection_checks
                WHERE user_id = $1 AND created_at > NOW() - make_interval(secs => $2)
                """,
                user_id,
                seconds,
            )
            if int(used or 0) >= limit:
                return {"error": "limited"}
            await conn.execute(
                "INSERT INTO agent_model_connection_checks (user_id, connection_id) "
                "VALUES ($1, $2)",
                user_id,
                connection_id,
            )
        return {"connection": _connection(row)}

    async def record_model_connection_check(
        self,
        *,
        user_id: str,
        connection_id: str,
        config_revision: int,
        result: dict[str, Any],
    ) -> bool:
        """Keep a finished check's result, only if the connection is still what it tested."""
        async with self._pool.acquire() as conn:
            stamped = await conn.fetchval(
                """
                UPDATE agent_model_connections SET last_check = $4::jsonb
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                  AND config_revision = $3
                RETURNING id
                """,
                connection_id,
                user_id,
                config_revision,
                json.dumps(result),
            )
        return stamped is not None

    async def scan_key_ciphertexts(self, *, sandbox_id: str, limit: int) -> list[str]:
        """Scan copies of the key revisions handed to one sandbox group, at most ``limit``.

        Only that group's, whichever of its members was bound to them: a
        same-owner Fork shares the group, so its keys are scanned for too, and
        a key the owner never used in this group is never read here.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key_ciphertext FROM agent_model_connection_scan_keys
                WHERE sandbox_id = $1 ORDER BY id LIMIT $2
                """,
                sandbox_id,
                limit,
            )
        return [str(row["key_ciphertext"]) for row in rows]

    async def bind_residency(
        self,
        *,
        user_id: str,
        connection_id: str,
        revision: int,
        thread_id: str,
        generation: int,
        scan_key_ciphertext: str,
    ) -> bool:
        """Stamp the residency with its connection revision, under the connection's row lock.

        In the same transaction, remember the key revision as handed to this
        sandbox group, as its scan-only copy, so the group's trusted snapshots
        and publications are refused if they carry it later.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchval(
                """
                SELECT credential_revision FROM agent_model_connections
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND enabled
                FOR SHARE
                """,
                connection_id,
                user_id,
            )
            if current is None or int(current) != revision:
                return False
            sandbox_id = await conn.fetchval(
                """
                SELECT m.sandbox_id FROM agent_thread_sandbox_memberships m
                JOIN agent_threads t ON t.id = m.thread_id
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND t.user_id = $3
                FOR UPDATE OF m
                """,
                thread_id,
                generation,
                user_id,
            )
            if sandbox_id is None:
                return False
            await conn.execute(
                """
                INSERT INTO agent_model_connection_residencies
                    (thread_id, residency_generation, connection_id, credential_revision)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (thread_id) DO UPDATE
                SET residency_generation = EXCLUDED.residency_generation,
                    connection_id = EXCLUDED.connection_id,
                    credential_revision = EXCLUDED.credential_revision
                """,
                thread_id,
                generation,
                connection_id,
                revision,
            )
            await conn.execute(
                """
                INSERT INTO agent_model_connection_scan_keys
                    (sandbox_id, connection_id, credential_revision, thread_id, key_ciphertext)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (sandbox_id, connection_id, credential_revision) DO NOTHING
                """,
                sandbox_id,
                connection_id,
                revision,
                thread_id,
                scan_key_ciphertext,
            )
        return True

    async def release_stale_residencies(
        self, *, user_id: str, connection_id: str
    ) -> list[dict[str, Any]]:
        """Release the open residencies a change to this connection left behind."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                UPDATE agent_thread_sandbox_memberships m
                SET residency_status = 'none', residency_last_active_at = NOW()
                FROM agent_sandboxes g, agent_threads t, agent_model_connection_residencies b
                WHERE b.thread_id = m.thread_id
                  AND b.residency_generation = m.residency_generation
                  AND b.connection_id = $1
                  AND m.residency_status <> 'none'
                  AND g.id = m.sandbox_id
                  AND t.id = m.thread_id AND t.user_id = $2
                  AND NOT {_STILL_CURRENT}
                RETURNING m.thread_id, m.residency_generation AS generation, g.host,
                          COALESCE(m.residency_grant_id, '') AS grant_id
                """,
                connection_id,
                user_id,
            )
        return [dict(row) for row in rows]
