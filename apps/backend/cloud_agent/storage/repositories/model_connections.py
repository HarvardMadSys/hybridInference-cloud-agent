"""Postgres persistence for owners' model connections."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.model_connections import ModelConnectionAccessEnded, ModelConnectionConflict
from cloud_agent.storage._sql.accounts import local_access_ended
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import Mapping

_COLUMNS = (
    "id, user_id, name, base_url, models, api_key_ciphertext, "
    "credential_revision, config_revision, enabled, model_checks, created_at, updated_at, "
    "deleted_at"
)
#: What :meth:`PostgresModelConnectionRepository.commit_verified_update` may change.
_UPDATABLE = frozenset({"name", "base_url", "models", "api_key_ciphertext", "enabled"})
#: Changes after which a process holding the old state must not serve a turn,
#: and no model's verification describes the endpoint any more.
_ENDPOINT_COLUMNS = ("base_url", "api_key_ciphertext")
_ARRAY_COLUMNS = frozenset({"models"})

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
    result["models"] = list(result.get("models") or [])
    checks = load_json(result.get("model_checks"))
    result["model_checks"] = checks if isinstance(checks, dict) else {}
    return result


#: What an edit may change without a verification: removing models never adds
#: one the connection has no facts for, and neither a rename nor enabling or
#: disabling touches the endpoint.
_UNVERIFIED = frozenset({"name", "models", "enabled"})


async def _lock_admitted_local_owner(conn: asyncpg.Connection, user_id: str) -> None:
    """Fence a save against local account edits, within the caller's transaction.

    Lock the account alone before reading its joined admission and login:
    a locking JOIN can return their old statement snapshot after waiting on
    a revocation. The separate statement sees the committed change; this
    SHARE lock then keeps approval stable until the connection write commits.
    Gateway owners have no local account and keep their existing behaviour.
    """
    local = await conn.fetchval(
        "SELECT 1 FROM agent_local_accounts WHERE id = $1 FOR SHARE", user_id
    )
    if not local:
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM agent_local_identity_namespace)"):
            raise ModelConnectionAccessEnded
        return
    admitted = await conn.fetchval(
        "SELECT coalesce((SELECT has_use FROM agent_local_account_access WHERE account_id = $1), "
        "FALSE)",
        user_id,
    )
    if not admitted:
        raise ModelConnectionAccessEnded


def _update_sql(changes: dict[str, Any], args: list[Any]) -> tuple[list[str], list[str]]:
    """The ``SET`` list and extra ``WHERE`` terms for an edit that needs no verification.

    Judged inside the ``UPDATE`` against the row it updates, so a value sent
    unchanged changes nothing: a rename leaves the credential, the running
    processes and every verification as they were. Disabling raises the
    credential revision. A model list may only shrink or reorder here, and a
    model no longer listed takes its verification along.
    """
    placeholders: dict[str, str] = {}
    assignments: list[str] = []
    for column, value in changes.items():
        args.append(value)
        placeholders[column] = f"${len(args)}" + ("::text[]" if column in _ARRAY_COLUMNS else "")
        assignments.append(f"{column} = {placeholders[column]}")
    where: list[str] = []
    if "enabled" in changes:
        assignments.append(
            "credential_revision = credential_revision + "
            f"CASE WHEN enabled AND NOT {placeholders['enabled']} THEN 1 ELSE 0 END"
        )
    if "models" in changes:
        assignments.append(
            "model_checks = (SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) "
            f"FROM jsonb_each(model_checks) WHERE key = ANY({placeholders['models']}))"
        )
        where.append(f"{placeholders['models']} <@ models")
    return assignments, where


class PostgresModelConnectionRepository:
    """Owners' model connections, each read and written only for its owner."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    async def create_model_connection(
        self, *, user_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert an enabled connection at its first revisions, with its verified models.

        ``values["model_checks"]`` holds each model's verification, made by the
        request that saves it; each is stamped with revision 1, the new row's.
        A local owner's admission is re-read under its account lock at commit,
        so a successful verification cannot outlive a completed revocation.
        """
        checks = {
            model: {**result, "revision": 1} for model, result in values["model_checks"].items()
        }
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_admitted_local_owner(conn, user_id)
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO agent_model_connections
                        (id, user_id, name, base_url, models, api_key_ciphertext, model_checks)
                    VALUES ($1, $2, $3, $4, $5::text[], $6, $7::jsonb)
                    RETURNING {_COLUMNS}
                    """,
                    values["id"],
                    user_id,
                    values["name"],
                    values["base_url"],
                    values["models"],
                    values["api_key_ciphertext"],
                    json.dumps(checks),
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
        """Rename, remove models from, enable or disable a connection.

        Nothing here can make a connection reach a new endpoint or list a
        model it has no verification for: a Base URL, a key or an added model
        goes through :meth:`commit_verified_update`. A model list that is not a
        subset of the stored one answers ``None`` with nothing written. Only
        an actual change raises a revision.
        """
        unknown = set(changes) - _UNVERIFIED
        if unknown:
            raise ValueError(f"model connection cannot change {sorted(unknown)} unverified")
        if not changes:
            return await self.get_model_connection(user_id=user_id, connection_id=connection_id)
        args: list[Any] = [connection_id, user_id]
        assignments, where = _update_sql(changes, args)
        guard = "".join(f" AND {term}" for term in where)
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_model_connections
                    SET {", ".join(assignments)}, updated_at = NOW()
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL{guard}
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
                    config_revision = config_revision + 1, model_checks = '{{}}'::jsonb,
                    updated_at = NOW()
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                RETURNING {_COLUMNS}
                """,
                connection_id,
                user_id,
            )
        return None if row is None else _connection(row)

    async def reserve_model_connection_checks(
        self, *, user_id: str, count: int, limit: int, seconds: int
    ) -> bool:
        """Count ``count`` provider calls against the owner's limit, all or none.

        Taken before anything dials out, in one transaction under a per-owner
        advisory lock, so two replicas, or two requests on one, cannot both
        take the last slots; a request that is cancelled after this still
        counts. Owner-level: a connection is verified before it exists.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"agent-model-connection-check:{user_id}",
            )
            used = await conn.fetchval(
                """
                SELECT count(*) FROM agent_model_connection_checks
                WHERE user_id = $1 AND created_at > NOW() - make_interval(secs => $2)
                """,
                user_id,
                seconds,
            )
            if int(used or 0) + count > limit:
                return False
            await conn.execute(
                "INSERT INTO agent_model_connection_checks (user_id) "
                "SELECT $1 FROM generate_series(1, $2)",
                user_id,
                count,
            )
        return True

    async def commit_verified_update(
        self,
        *,
        user_id: str,
        connection_id: str,
        expected: Mapping[str, Any],
        changes: dict[str, Any],
        model_checks: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Apply an edit whose new models or endpoint were verified, if nothing moved meanwhile.

        ``expected`` is the row the verification started from. The update
        lands only while the connection is live and still at that row's
        ``credential_revision``, ``config_revision``, model list and stored
        facts: a rotation, Base URL change, disable, delete or model edit made
        during the verification wins, and ``None`` is answered with nothing
        written. The facts are compared too because a model edit raises no
        revision: removing a model and adding it back, freshly verified,
        restores the list but not the facts, and a late save must not put the
        old ones back.
        A concurrent rename does not conflict, since only ``changes`` are
        written. ``model_checks`` is the whole verified map for the new state;
        a new endpoint raises both revisions and stamps it with the new
        ``config_revision``.
        The local owner's account lock and a fresh admission check also fence
        the commit against a revocation or suspension during verification.
        """
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise ValueError(f"model connection cannot change {sorted(unknown)}")
        endpoint = any(column in changes for column in _ENDPOINT_COLUMNS)
        revision = int(expected["config_revision"]) + (1 if endpoint else 0)
        checks = {model: {**result, "revision": revision} for model, result in model_checks.items()}
        args: list[Any] = [
            connection_id,
            user_id,
            int(expected["credential_revision"]),
            int(expected["config_revision"]),
            list(expected["models"]),
            json.dumps(checks),
            json.dumps(expected["model_checks"]),
        ]
        assignments = ["model_checks = $6::jsonb"]
        for column, value in changes.items():
            args.append(value)
            cast = "::text[]" if column in _ARRAY_COLUMNS else ""
            assignments.append(f"{column} = ${len(args)}{cast}")
        if endpoint or changes.get("enabled") is False:
            assignments.append("credential_revision = credential_revision + 1")
        if endpoint:
            assignments.append("config_revision = config_revision + 1")
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_admitted_local_owner(conn, user_id)
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_model_connections
                    SET {", ".join(assignments)}, updated_at = NOW()
                    WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                      AND credential_revision = $3 AND config_revision = $4
                      AND models = $5::text[] AND model_checks = $7::jsonb
                    RETURNING {_COLUMNS}
                    """,
                    *args,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModelConnectionConflict(str(changes.get("name"))) from exc
        return None if row is None else _connection(row)

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

        A local owner whose access has ended gets no binding, and so no key:
        the statement is the fence, as for establishing the residency itself,
        so a revocation that commits between the two hands the key to nobody.
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
                f"""
                SELECT m.sandbox_id FROM agent_thread_sandbox_memberships m
                JOIN agent_threads t ON t.id = m.thread_id
                WHERE m.thread_id = $1 AND m.residency_generation = $2
                  AND m.residency_status = 'hot'
                  AND m.residency_lease_expires_at > clock_timestamp()
                  AND t.user_id = $3
                  AND NOT {local_access_ended("t.user_id")}
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
