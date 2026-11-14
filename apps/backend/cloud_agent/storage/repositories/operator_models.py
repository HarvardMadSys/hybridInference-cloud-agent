"""Postgres persistence for the models a deployment provides, and owners' allowances on them."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.model_connections import ModelConnectionConflict
from cloud_agent.domain.operator_models import (
    Allowance,
    OperatorLimitsMissing,
    effective_allowance,
)
from cloud_agent.storage._sql.accounts import local_access_ended
from cloud_agent.storage._sql.fences import RESIDENCY_CALLER_FROM, live_residency_caller_sql
from cloud_agent.storage._sql.rows import load_json
from cloud_agent.storage._sql.sandbox_transitions import (
    RELEASE_STALE_OPERATOR_RESIDENCIES_MEMBERS_SQL,
)
from cloud_agent_shared.model_connections import MODEL_RELAY_CAPABILITY

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

_COLUMNS = (
    "id, name, base_url, models, default_model, is_default, enabled, api_key_ciphertext, "
    "end_user_header, user_concurrency, model_checks, config_revision, credential_revision, "
    "key_refused_at, created_at, updated_at, deleted_at"
)
#: What an edit may change without a verification: none of these makes the
#: connection reach a new endpoint or list a model it has no facts for.
_UNVERIFIED = frozenset(
    {
        "name",
        "models",
        "default_model",
        "is_default",
        "enabled",
        "end_user_header",
        "user_concurrency",
    }
)
#: What a verified edit may change. Enabling and the default flag are not
#: among them: they have rules of their own (``update_connection``).
_VERIFIED = frozenset(
    {
        "name",
        "models",
        "default_model",
        "end_user_header",
        "user_concurrency",
        "base_url",
        "api_key_ciphertext",
    }
)
_ARRAY_COLUMNS = frozenset({"models"})
#: Today, as every allowance counts it.
_TODAY = "(clock_timestamp() AT TIME ZONE 'UTC')::date"


def _connection(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["models"] = list(result.get("models") or [])
    checks = load_json(result.get("model_checks"))
    result["model_checks"] = checks if isinstance(checks, dict) else {}
    return result


def _placeholder(column: str, index: int) -> str:
    return f"${index}" + ("::text[]" if column in _ARRAY_COLUMNS else "")


async def read_operator_connection(conn: Any, connection_id: str) -> dict[str, Any] | None:
    """One provided connection, tombstones included, or ``None``."""
    row = await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM agent_operator_model_connections WHERE id = $1", connection_id
    )
    return None if row is None else _connection(row)


async def read_operator_allowance(
    conn: Any, *, connection_id: str, user_id: str, model: str
) -> Allowance:
    """One owner's allowance on one provided model, with today's use."""
    row = await conn.fetchrow(
        f"""
        SELECT l.user_daily_requests, (o.model IS NOT NULL) AS own, o.daily_requests,
               COALESCE(u.requests, 0) AS used
        FROM agent_operator_model_limits l
        LEFT JOIN agent_operator_model_user_limits o
               ON o.operator_connection_id = l.operator_connection_id
              AND o.model = l.model AND o.user_id = $2
        LEFT JOIN agent_operator_model_usage u
               ON u.operator_connection_id = l.operator_connection_id
              AND u.model = l.model AND u.user_id = $2 AND u.day = {_TODAY}
        WHERE l.operator_connection_id = $1 AND l.model = $3
        """,
        connection_id,
        user_id,
        model,
    )
    if row is None:
        return effective_allowance(None, None)
    return effective_allowance(
        {"user_daily_requests": row["user_daily_requests"]},
        {"daily_requests": row["daily_requests"]} if row["own"] else None,
        used=int(row["used"]),
    )


async def bind_operator_residency(
    conn: Any,
    *,
    user_id: str,
    connection_id: str,
    revision: int,
    thread_id: str,
    generation: int,
) -> bool:
    """Stamp a residency with the provided connection revision its process starts on.

    Within the caller's transaction. Read under the connection's row lock, as
    an owner connection's binding is: a change that commits first refuses
    this, and one that commits after finds and closes the residency. Nothing
    else is recorded, since no key enters the sandbox.
    """
    current = await conn.fetchval(
        """
        SELECT credential_revision FROM agent_operator_model_connections
        WHERE id = $1 AND deleted_at IS NULL AND enabled
        FOR SHARE
        """,
        connection_id,
    )
    if current is None or int(current) != revision:
        return False
    held = await conn.fetchval(
        f"""
        SELECT 1 FROM agent_thread_sandbox_memberships m
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
    if held is None:
        return False
    await conn.execute(
        """
        INSERT INTO agent_operator_connection_residencies
            (thread_id, residency_generation, operator_connection_id, credential_revision)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (thread_id) DO UPDATE
        SET residency_generation = EXCLUDED.residency_generation,
            operator_connection_id = EXCLUDED.operator_connection_id,
            credential_revision = EXCLUDED.credential_revision
        """,
        thread_id,
        generation,
        connection_id,
        revision,
    )
    return True


class PostgresOperatorModelRepository:
    """Provided connections, their limits, and what the model relay reads and counts."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Wrap the pool the control plane already owns."""
        self._pool = pool

    # -- connections -----------------------------------------------------------

    async def list_operator_connections(self) -> list[dict[str, Any]]:
        """Every live provided connection, oldest first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_COLUMNS} FROM agent_operator_model_connections
                WHERE deleted_at IS NULL ORDER BY created_at, id
                """
            )
        return [_connection(row) for row in rows]

    async def get_operator_connection(self, *, connection_id: str) -> dict[str, Any] | None:
        """One provided connection, tombstones included, or ``None``."""
        async with self._pool.acquire() as conn:
            return await read_operator_connection(conn, connection_id)

    async def create_operator_connection(self, *, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a disabled connection at its first revisions, with its verified models."""
        checks = {
            model: {**result, "revision": 1} for model, result in values["model_checks"].items()
        }
        async with self._pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO agent_operator_model_connections
                        (id, name, base_url, models, default_model, api_key_ciphertext,
                         end_user_header, user_concurrency, model_checks)
                    VALUES ($1, $2, $3, $4::text[], $5, $6, $7, $8, $9::jsonb)
                    RETURNING {_COLUMNS}
                    """,
                    values["id"],
                    values["name"],
                    values["base_url"],
                    values["models"],
                    values.get("default_model"),
                    values["api_key_ciphertext"],
                    values.get("end_user_header"),
                    int(values["user_concurrency"]),
                    json.dumps(checks),
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModelConnectionConflict(values["name"]) from exc
        assert row is not None
        return _connection(row)

    async def update_operator_connection(
        self, *, connection_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply an edit that needs no verification; ``None`` when nothing live matched.

        Renames, the default model and flag, the owner tag header, the
        per-owner concurrency, a model list that only shrinks, and enabling or
        disabling. Enabling is refused (:class:`OperatorLimitsMissing`) while a
        listed model has no default allowance. Disabling raises the credential
        revision, so the caller's release finds every process started before
        it. A removed model takes its verification, its allowances and, when
        it was the default model, that choice along. Making a connection the
        default takes the flag from any other.
        """
        unknown = set(changes) - _UNVERIFIED
        if unknown:
            raise ValueError(f"a provided connection cannot change {sorted(unknown)} unverified")
        async with self._pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                "SELECT models, enabled FROM agent_operator_model_connections "
                "WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
                connection_id,
            )
            if current is None:
                return None
            if not changes:
                return await read_operator_connection(conn, connection_id)
            models = list(changes.get("models", current["models"]))
            if not set(models) <= set(current["models"]):
                return None
            if changes.get("enabled") and not current["enabled"]:
                await self._require_limits(conn, connection_id, models)
            if changes.get("is_default"):
                await conn.execute(
                    "UPDATE agent_operator_model_connections SET is_default = FALSE "
                    "WHERE is_default AND id <> $1",
                    connection_id,
                )
            args: list[Any] = [connection_id]
            assignments = _unverified_assignments(changes, args)
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_operator_model_connections
                    SET {", ".join(assignments)}, updated_at = NOW()
                    WHERE id = $1 AND deleted_at IS NULL
                    RETURNING {_COLUMNS}
                    """,
                    *args,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModelConnectionConflict(str(changes.get("name"))) from exc
            if "models" in changes:
                await _forget_removed_models(conn, connection_id, models)
        return None if row is None else _connection(row)

    @staticmethod
    async def _require_limits(conn: Any, connection_id: str, models: list[str]) -> None:
        rows = await conn.fetch(
            "SELECT model FROM agent_operator_model_limits WHERE operator_connection_id = $1",
            connection_id,
        )
        limited = {str(row["model"]) for row in rows}
        missing = [model for model in models if model not in limited]
        if missing:
            raise OperatorLimitsMissing(missing)

    async def commit_verified_operator_update(
        self,
        *,
        connection_id: str,
        expected: Mapping[str, Any],
        changes: dict[str, Any],
        model_checks: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Apply an edit whose new models or endpoint were verified, if nothing moved meanwhile.

        Lands only while the connection is live and still at ``expected``'s
        revisions, model list and stored facts, as an owner connection's edit
        does. A new key or Base URL replaces every model's facts and raises
        the config revision. Only a new Base URL raises the credential
        revision: a member's process holds a placeholder and the address it
        was launched at, never the key, so a rotated key serves the very next
        request and closes nothing.
        """
        unknown = set(changes) - _VERIFIED
        if unknown:
            raise ValueError(f"a provided connection cannot change {sorted(unknown)}")
        endpoint = "base_url" in changes or "api_key_ciphertext" in changes
        revision = int(expected["config_revision"]) + (1 if endpoint else 0)
        checks = {model: {**result, "revision": revision} for model, result in model_checks.items()}
        args: list[Any] = [
            connection_id,
            int(expected["credential_revision"]),
            int(expected["config_revision"]),
            list(expected["models"]),
            json.dumps(checks),
            json.dumps(expected["model_checks"]),
        ]
        assignments = ["model_checks = $5::jsonb"]
        for column, value in changes.items():
            args.append(value)
            assignments.append(f"{column} = {_placeholder(column, len(args))}")
        if endpoint:
            assignments.append("config_revision = config_revision + 1")
        if "api_key_ciphertext" in changes:
            assignments.append("key_refused_at = NULL")
        if "base_url" in changes:
            assignments.append("credential_revision = credential_revision + 1")
        async with self._pool.acquire() as conn, conn.transaction():
            try:
                row = await conn.fetchrow(
                    f"""
                    UPDATE agent_operator_model_connections
                    SET {", ".join(assignments)}, updated_at = NOW()
                    WHERE id = $1 AND deleted_at IS NULL
                      AND credential_revision = $2 AND config_revision = $3
                      AND models = $4::text[] AND model_checks = $6::jsonb
                    RETURNING {_COLUMNS}
                    """,
                    *args,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ModelConnectionConflict(str(changes.get("name"))) from exc
            if row is not None and "models" in changes:
                await _forget_removed_models(conn, connection_id, list(changes["models"]))
        return None if row is None else _connection(row)

    async def delete_operator_connection(self, *, connection_id: str) -> dict[str, Any] | None:
        """Keep the row as a tombstone for the turns that name it, and erase the key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agent_operator_model_connections
                SET deleted_at = NOW(), enabled = FALSE, is_default = FALSE,
                    api_key_ciphertext = NULL, key_refused_at = NULL,
                    credential_revision = credential_revision + 1,
                    config_revision = config_revision + 1, model_checks = '{{}}'::jsonb,
                    updated_at = NOW()
                WHERE id = $1 AND deleted_at IS NULL
                RETURNING {_COLUMNS}
                """,
                connection_id,
            )
        return None if row is None else _connection(row)

    async def mark_operator_key_refused(self, *, connection_id: str, refused: bool) -> None:
        """Remember since when the provider refuses the stored key, or that it no longer does."""
        value = "COALESCE(key_refused_at, NOW())" if refused else "NULL"
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE agent_operator_model_connections SET key_refused_at = {value} "
                "WHERE id = $1 AND deleted_at IS NULL",
                connection_id,
            )

    async def model_relay_host_count(self) -> int:
        """How many enrolled, enabled hosts say their sandboxes reach the model relay."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT count(*) FROM agent_runner_hosts
                WHERE credential_hash IS NOT NULL AND status <> 'disabled'
                  AND capabilities @> $1::jsonb
                """,
                json.dumps([MODEL_RELAY_CAPABILITY]),
            )
        return int(count or 0)

    # -- limits ----------------------------------------------------------------

    async def operator_limits(
        self, *, connection_ids: list[str]
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Each connection's default allowances, per model."""
        result: dict[str, dict[str, dict[str, Any]]] = {cid: {} for cid in connection_ids}
        if not connection_ids:
            return result
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT operator_connection_id, model, user_daily_requests, global_concurrency
                FROM agent_operator_model_limits
                WHERE operator_connection_id = ANY($1::text[])
                """,
                connection_ids,
            )
        for row in rows:
            result[str(row["operator_connection_id"])][str(row["model"])] = {
                "user_daily_requests": row["user_daily_requests"],
                "global_concurrency": row["global_concurrency"],
            }
        return result

    async def set_operator_limit(
        self,
        *,
        connection_id: str,
        model: str,
        user_daily_requests: int | None,
        global_concurrency: int | None,
    ) -> bool:
        """Set one listed model's default allowance; ``False`` when it is not listed."""
        async with self._pool.acquire() as conn:
            written = await conn.fetchval(
                """
                INSERT INTO agent_operator_model_limits
                    (operator_connection_id, model, user_daily_requests, global_concurrency)
                SELECT c.id, $2, $3, $4 FROM agent_operator_model_connections c
                WHERE c.id = $1 AND c.deleted_at IS NULL AND $2 = ANY(c.models)
                ON CONFLICT (operator_connection_id, model) DO UPDATE
                SET user_daily_requests = EXCLUDED.user_daily_requests,
                    global_concurrency = EXCLUDED.global_concurrency
                RETURNING 1
                """,
                connection_id,
                model,
                user_daily_requests,
                global_concurrency,
            )
        return written is not None

    async def set_operator_user_limit(
        self, *, connection_id: str, user_id: str, model: str, daily_requests: int | None
    ) -> bool:
        """Give one account its own allowance on a listed model; ``False`` when not listed."""
        async with self._pool.acquire() as conn:
            written = await conn.fetchval(
                """
                INSERT INTO agent_operator_model_user_limits
                    (operator_connection_id, user_id, model, daily_requests)
                SELECT c.id, $2, $3, $4 FROM agent_operator_model_connections c
                WHERE c.id = $1 AND c.deleted_at IS NULL AND $3 = ANY(c.models)
                ON CONFLICT (operator_connection_id, user_id, model) DO UPDATE
                SET daily_requests = EXCLUDED.daily_requests
                RETURNING 1
                """,
                connection_id,
                user_id,
                model,
                daily_requests,
            )
        return written is not None

    async def clear_operator_user_limit(
        self, *, connection_id: str, user_id: str, model: str
    ) -> bool:
        """Return one account to the model's default allowance; ``False`` when it had none."""
        async with self._pool.acquire() as conn:
            removed = await conn.fetchval(
                """
                DELETE FROM agent_operator_model_user_limits
                WHERE operator_connection_id = $1 AND user_id = $2 AND model = $3
                RETURNING 1
                """,
                connection_id,
                user_id,
                model,
            )
        return removed is not None

    async def list_operator_users(
        self, *, connection_id: str, cursor: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Accounts with an allowance of their own, or with use today, by account id.

        Each as ``{"user_id", "limits": {model: n|None}, "used_today": {model: n}}``.
        """
        async with self._pool.acquire() as conn:
            ids = [
                str(row["user_id"])
                for row in await conn.fetch(
                    f"""
                    SELECT user_id FROM (
                        SELECT user_id FROM agent_operator_model_user_limits
                        WHERE operator_connection_id = $1
                        UNION
                        SELECT user_id FROM agent_operator_model_usage
                        WHERE operator_connection_id = $1 AND day = {_TODAY} AND requests > 0
                    ) ids
                    WHERE $2::text IS NULL OR user_id > $2
                    ORDER BY user_id LIMIT $3
                    """,
                    connection_id,
                    cursor,
                    limit,
                )
            ]
            users: dict[str, dict[str, Any]] = {
                user_id: {"user_id": user_id, "limits": {}, "used_today": {}} for user_id in ids
            }
            if not ids:
                return []
            for row in await conn.fetch(
                """
                SELECT user_id, model, daily_requests FROM agent_operator_model_user_limits
                WHERE operator_connection_id = $1 AND user_id = ANY($2::text[])
                """,
                connection_id,
                ids,
            ):
                users[str(row["user_id"])]["limits"][str(row["model"])] = row["daily_requests"]
            for row in await conn.fetch(
                f"""
                SELECT user_id, model, requests FROM agent_operator_model_usage
                WHERE operator_connection_id = $1 AND user_id = ANY($2::text[])
                  AND day = {_TODAY} AND requests > 0
                """,
                connection_id,
                ids,
            ):
                users[str(row["user_id"])]["used_today"][str(row["model"])] = int(row["requests"])
        return list(users.values())

    async def operator_usage(
        self, *, connection_id: str, start: date, end: date, limit: int
    ) -> list[dict[str, Any]]:
        """Accepted requests per day, account and model in ``[start, end]``, newest day first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT day, user_id, model, requests FROM agent_operator_model_usage
                WHERE operator_connection_id = $1 AND day BETWEEN $2 AND $3 AND requests > 0
                ORDER BY day DESC, requests DESC, user_id, model
                LIMIT $4
                """,
                connection_id,
                start,
                end,
                limit,
            )
        return [dict(row) for row in rows]

    async def operator_sources_for_user(self, *, user_id: str) -> list[dict[str, Any]]:
        """Every enabled connection, each with ``user_id``'s allowance per listed model.

        A connection row plus ``allowances``: ``{model: Allowance}`` for every
        listed model, the ones withheld from this owner included.
        """
        async with self._pool.acquire() as conn:
            connections = [
                _connection(row)
                for row in await conn.fetch(
                    f"""
                    SELECT {_COLUMNS} FROM agent_operator_model_connections
                    WHERE deleted_at IS NULL AND enabled ORDER BY created_at, id
                    """
                )
            ]
            if not connections:
                return []
            rows = await conn.fetch(
                f"""
                SELECT l.operator_connection_id, l.model, l.user_daily_requests,
                       (o.model IS NOT NULL) AS own, o.daily_requests,
                       COALESCE(u.requests, 0) AS used
                FROM agent_operator_model_limits l
                LEFT JOIN agent_operator_model_user_limits o
                       ON o.operator_connection_id = l.operator_connection_id
                      AND o.model = l.model AND o.user_id = $2
                LEFT JOIN agent_operator_model_usage u
                       ON u.operator_connection_id = l.operator_connection_id
                      AND u.model = l.model AND u.user_id = $2 AND u.day = {_TODAY}
                WHERE l.operator_connection_id = ANY($1::text[])
                """,
                [connection["id"] for connection in connections],
                user_id,
            )
        found: dict[tuple[str, str], Allowance] = {}
        for row in rows:
            found[(str(row["operator_connection_id"]), str(row["model"]))] = effective_allowance(
                {"user_daily_requests": row["user_daily_requests"]},
                {"daily_requests": row["daily_requests"]} if row["own"] else None,
                used=int(row["used"]),
            )
        for connection in connections:
            connection["allowances"] = {
                model: found.get((connection["id"], model), effective_allowance(None, None))
                for model in connection["models"]
            }
        return connections

    # -- residencies -----------------------------------------------------------

    async def release_stale_operator_residencies(
        self, *, connection_id: str
    ) -> list[dict[str, Any]]:
        """Release the open residencies a change to this connection left behind."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(RELEASE_STALE_OPERATOR_RESIDENCIES_MEMBERS_SQL, connection_id)
        return [dict(row) for row in rows]

    # -- the model relay -------------------------------------------------------

    async def resolve_model_relay_caller(
        self, *, thread_id: str, residency_generation: int, model: str | None
    ) -> dict[str, Any] | None:
        """Name the running turn a model relay placeholder speaks for, and what it may use.

        ``None`` unless the residency passes the relays' shared fence
        (:func:`~cloud_agent.storage._sql.fences.live_residency_caller_sql`).
        Otherwise the turn's job, owner and provided connection, with the
        revision this residency's process was bound to (``bound_revision``,
        ``None`` when it was bound to none) and, for ``model``, the owner's
        allowance (``allowance``) and the model's deployment-wide concurrency
        cap. ``connection`` is ``None`` when the turn does not run on a
        provided connection at all.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT j.id AS job_id, a.id AS attempt_id, j.thread_id, j.user_id,
                       j.model_connection_id, b.credential_revision AS bound_revision
                FROM {RESIDENCY_CALLER_FROM}
                LEFT JOIN agent_operator_connection_residencies b
                       ON b.thread_id = m.thread_id
                      AND b.residency_generation = m.residency_generation
                      AND b.operator_connection_id = j.model_connection_id
                WHERE {live_residency_caller_sql("$1", "$2")}
                """,
                thread_id,
                residency_generation,
            )
            if row is None:
                return None
            caller = dict(row)
            connection_id = caller.get("model_connection_id")
            caller["connection"] = (
                await read_operator_connection(conn, str(connection_id)) if connection_id else None
            )
            caller["allowance"] = None
            caller["global_concurrency"] = None
            if caller["connection"] is not None and model is not None:
                caller["allowance"] = await read_operator_allowance(
                    conn,
                    connection_id=str(connection_id),
                    user_id=str(caller["user_id"]),
                    model=model,
                )
                caller["global_concurrency"] = await conn.fetchval(
                    "SELECT global_concurrency FROM agent_operator_model_limits "
                    "WHERE operator_connection_id = $1 AND model = $2",
                    str(connection_id),
                    model,
                )
        return caller

    async def count_operator_request(
        self, *, connection_id: str, user_id: str, model: str
    ) -> dict[str, Any]:
        """Count one request against the owner's allowance, unless it is used up.

        One statement decides and counts, so concurrent requests of one owner
        on one model queue on their own row and cannot pass the allowance
        together. Answers ``{"outcome", "day"}``: ``counted``, ``exhausted``
        (the allowance was already reached; nothing was counted) or
        ``not_offered`` (no allowance, or zero).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                WITH lim AS (
                    SELECT CASE WHEN o.model IS NOT NULL THEN o.daily_requests
                                ELSE l.user_daily_requests END AS daily
                    FROM agent_operator_model_limits l
                    LEFT JOIN agent_operator_model_user_limits o
                           ON o.operator_connection_id = l.operator_connection_id
                          AND o.model = l.model AND o.user_id = $2
                    WHERE l.operator_connection_id = $1 AND l.model = $3
                ), counted AS (
                    INSERT INTO agent_operator_model_usage AS u
                        (operator_connection_id, user_id, model, day, requests)
                    SELECT $1, $2, $3, {_TODAY}, 1 FROM lim
                    WHERE lim.daily IS NULL OR lim.daily > 0
                    ON CONFLICT (operator_connection_id, user_id, model, day) DO UPDATE
                        SET requests = u.requests + 1
                        WHERE (SELECT daily FROM lim) IS NULL
                           OR u.requests < (SELECT daily FROM lim)
                    RETURNING day
                )
                SELECT (SELECT count(*) FROM lim) AS limited,
                       (SELECT daily FROM lim) AS daily,
                       (SELECT day FROM counted) AS day
                """,
                connection_id,
                user_id,
                model,
            )
        assert row is not None
        if not row["limited"] or row["daily"] == 0:
            return {"outcome": "not_offered", "day": None}
        if row["day"] is None:
            return {"outcome": "exhausted", "day": None}
        return {"outcome": "counted", "day": row["day"]}

    async def refund_operator_request(
        self, *, connection_id: str, user_id: str, model: str, day: date
    ) -> None:
        """Take back one counted request the provider never served."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_operator_model_usage SET requests = requests - 1
                WHERE operator_connection_id = $1 AND user_id = $2 AND model = $3
                  AND day = $4 AND requests > 0
                """,
                connection_id,
                user_id,
                model,
                day,
            )


def _unverified_assignments(changes: dict[str, Any], args: list[Any]) -> list[str]:
    """The ``SET`` list of an edit that needs no verification, judged against the stored row."""
    placeholders: dict[str, str] = {}
    assignments: list[str] = []
    for column, value in changes.items():
        args.append(value)
        placeholders[column] = _placeholder(column, len(args))
        assignments.append(f"{column} = {placeholders[column]}")
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
        if "default_model" not in changes:
            assignments.append(
                "default_model = CASE WHEN default_model = "
                f"ANY({placeholders['models']}) THEN default_model END"
            )
    return assignments


async def _forget_removed_models(conn: Any, connection_id: str, models: list[str]) -> None:
    """Drop the allowances of models a connection no longer lists.

    A model listed again later starts with no default allowance, so it is
    offered to nobody until its administrator chooses one.
    """
    for table in ("agent_operator_model_limits", "agent_operator_model_user_limits"):
        await conn.execute(
            f"DELETE FROM {table} WHERE operator_connection_id = $1 AND NOT (model = ANY($2::text[]))",
            connection_id,
            models,
        )


__all__ = [
    "PostgresOperatorModelRepository",
    "bind_operator_residency",
    "read_operator_allowance",
    "read_operator_connection",
]
