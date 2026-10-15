"""Postgres persistence for deployment-wide administrator settings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent import settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    import asyncpg


def _snapshot(row: Any) -> tuple[int, dict[str, Any]]:
    if row is None:
        return -1, {}
    values = row["values"]
    if isinstance(values, str):
        values = json.loads(values)
    return int(row["revision"]), dict(values)


class PostgresSettingsRepository:
    """The singleton settings document, with row locking for partial updates."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Use the control plane's existing connection pool."""
        self._pool = pool

    async def read_settings(self) -> tuple[int, dict[str, Any]]:
        """Return the revision and encoded values, or revision minus one before initialization."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT revision, "values" FROM agent_settings WHERE id = TRUE'
            )
        return _snapshot(row)

    async def seed_settings(self, values: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Initialize or import new fields once, preserving every saved value."""
        async with self._pool.acquire() as conn, conn.transaction():
            imported = dict(values)
            if "gateway_issuer" in imported:
                from cloud_agent.identity_namespace import IdentityNamespaceError

                issuer = await conn.fetchval("SELECT gateway_issuer FROM agent_identity_namespace")
                if issuer:
                    if imported["gateway_issuer"] and imported["gateway_issuer"] != issuer:
                        raise IdentityNamespaceError(
                            "GATEWAY_ISSUER does not match this Agent database's identity issuer."
                        )
                    imported["gateway_issuer"] = issuer
            await conn.execute(
                """
                INSERT INTO agent_settings (id, revision, "values")
                VALUES (TRUE, 1, $1::jsonb)
                ON CONFLICT (id) DO UPDATE
                SET "values" = EXCLUDED."values" || agent_settings."values",
                    revision = agent_settings.revision + 1, updated_at = NOW()
                WHERE agent_settings."values" IS DISTINCT FROM
                    (EXCLUDED."values" || agent_settings."values")
                """,
                json.dumps(imported),
            )
            row = await conn.fetchrow(
                'SELECT revision, "values" FROM agent_settings WHERE id = TRUE'
            )
        return _snapshot(row)

    async def update_settings(
        self, changes: Mapping[str, Any], *, expected_revision: int | None = None
    ) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Atomically merge encoded changes and return revision, current and previous values.

        With ``expected_revision``, the merge applies only if no other update
        was saved since that revision; the check holds the row lock, so two
        updates reviewed against one revision cannot both apply.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                'SELECT revision, "values" FROM agent_settings WHERE id = TRUE FOR UPDATE'
            )
            if row is None:
                raise RuntimeError("Agent settings have not been initialized.")
            revision, previous = _snapshot(row)
            if expected_revision is not None and revision != expected_revision:
                raise settings.SettingsRevisionConflict()
            values = {**previous, **changes}
            settings.validate_settings_update(previous, values)
            if values == previous:
                return revision, values, previous
            if "gateway_issuer" in changes:
                from cloud_agent.identity_namespace import check_gateway_namespace

                await check_gateway_namespace(conn, str(values["gateway_issuer"]) or None)
            updated = await conn.fetchrow(
                """
                UPDATE agent_settings
                SET revision = revision + 1, "values" = $1::jsonb, updated_at = NOW()
                WHERE id = TRUE
                RETURNING revision, "values"
                """,
                json.dumps(values),
            )
            revision, values = _snapshot(updated)
        return revision, values, previous
