"""Reading the environment catalogue: environments, fingerprints and versions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.environments.environments import EnvironmentEncryptionError
from cloud_agent.storage._sql import environment_catalogue
from cloud_agent.storage.repositories.environments._common import (
    EnvironmentRepositoryBase,
    _cipher,
    _json,
)
from cloud_agent.storage.repositories.environments.installations import optional_setup_fingerprint
from cloud_agent_shared.environment import EnvironmentScripts, EnvironmentVariable, merge_variables

if TYPE_CHECKING:
    import builtins
    from collections import OrderedDict


#: The environments an account may use: its own, and those of every active team
#: it is an active member of, with its role there. Prefix of a query whose
#: first parameter is the account.
_ACCESSIBLE_ENVIRONMENTS = """
WITH accessible AS (
    SELECT e.*, NULL::text AS team_name, NULL::text AS team_role,
           NULL::bigint AS team_access_generation
    FROM agent_environments e
    WHERE e.team_id IS NULL AND e.user_id = $1
    UNION ALL
    SELECT e.*, t.name AS team_name,
           CASE WHEN t.owner_user_id = $1 THEN 'owner' ELSE m.role END AS team_role,
           m.access_generation AS team_access_generation
    FROM agent_environments e
    JOIN agent_teams t ON t.id = e.team_id AND t.state = 'active'
    JOIN agent_team_members m ON m.team_id = t.id AND m.user_id = $1 AND m.state = 'active'
)
"""


class CatalogueMixin(EnvironmentRepositoryBase):
    """List and read environments and their versions."""

    _fingerprints: OrderedDict[tuple[str, str | None], str]

    async def list_environments(
        self, user_id: str, repo: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """List the account's own active configurations, then its teams', with test facts.

        A team's environments appear while the account is an active member of
        that active team. Defaults are the account's own, whichever kind they name.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                _ACCESSIBLE_ENVIRONMENTS + "SELECT e.*, d.environment_id=e.id AS is_default, "
                "COALESCE(d.revision,0) AS default_revision "
                "FROM accessible e LEFT JOIN agent_repository_environment_defaults d "
                "ON d.user_id=$1 AND d.repo=e.repo "
                "WHERE e.repo IS NOT NULL AND e.archived_at IS NULL "
                "AND ($2::text IS NULL OR e.repo=$2) "
                "ORDER BY e.repo, e.team_id IS NOT NULL, e.team_name, e.name, e.id",
                user_id,
                repo,
            )
            versions = await environment_catalogue.versions(conn, rows)
            variables = await environment_catalogue.variables(
                conn, [v["id"] for v in versions.values()]
            )
            latest = await environment_catalogue.latest_tests(
                conn, user_id, [row["id"] for row in rows]
            )
            return [
                {
                    **environment_catalogue.public_environment(
                        row,
                        versions.get(row["id"]),
                        variables.get(versions.get(row["id"], {}).get("id", ""), []),
                        _cipher,
                    ),
                    "is_default": bool(row["is_default"]),
                    "default_revision": row["default_revision"],
                    "latest_test": latest.get(row["id"]),
                }
                for row in rows
            ]

    async def current_setup_fingerprints(
        self, user_id: str, environment_ids: builtins.list[str]
    ) -> dict[str, str | None]:
        """What a task started now would install, per environment; ``None`` without setup."""
        result: dict[str, str | None] = {}
        async with self._pool.acquire() as conn:
            global_id = await conn.fetchval(
                "SELECT v.id FROM agent_environments e JOIN agent_environment_versions v "
                "ON v.environment_id=e.id AND v.revision=e.revision WHERE e.user_id=$1 AND e.repo IS NULL",
                user_id,
            )
            rows = await conn.fetch(
                _ACCESSIBLE_ENVIRONMENTS
                + "SELECT e.id,e.team_id,v.id AS version_id,v.document FROM accessible e "
                "JOIN agent_environment_versions v ON v.environment_id=e.id AND v.revision=e.revision "
                "WHERE e.id=ANY($2::text[])",
                user_id,
                environment_ids,
            )
            cached = self._fingerprints.copy()
            missing = [
                row
                for row in rows
                if (row["version_id"], None if row["team_id"] else global_id) not in cached
                and _json(row["document"]).get("setup_script")
            ]
            needs_globals = global_id is not None and any(not row["team_id"] for row in missing)
            variables = await environment_catalogue.variables(
                conn,
                [row["version_id"] for row in missing] + ([global_id] if needs_globals else []),
            )
            global_values: list[EnvironmentVariable] | None = []
            if needs_globals:
                try:
                    global_values = self._environment_decode(variables.get(global_id, []))
                except EnvironmentEncryptionError:
                    global_values = None
            for row in rows:
                # A team's installation does not depend on the member's globals.
                scope = None if row["team_id"] else global_id
                key = (row["version_id"], scope)
                if key in cached:
                    result[row["id"]] = cached[key]
                    continue
                if key not in self._fingerprints:
                    setup = _json(row["document"]).get("setup_script") or ""
                    try:
                        fingerprint = (
                            optional_setup_fingerprint(
                                setup,
                                merge_variables(
                                    [] if row["team_id"] else (global_values or []),
                                    self._environment_decode(variables.get(row["version_id"], [])),
                                ),
                            )
                            if setup and (row["team_id"] or global_values is not None)
                            else None
                        )
                    except EnvironmentEncryptionError:
                        fingerprint = None
                    if fingerprint is not None:
                        self._fingerprints[key] = fingerprint
                        while len(self._fingerprints) > 512:
                            self._fingerprints.popitem(last=False)
                result[row["id"]] = self._fingerprints.get(key)
        return result

    async def get_environment(self, user_id: str, environment_id: str) -> dict[str, Any]:
        """Read the current owned configuration with write-only secrets."""
        async with self._pool.acquire() as conn:
            return await self._environment_public(
                conn, await self._environment_row(conn, user_id, environment_id, archived=True)
            )

    async def get_global_environment(self, user_id: str) -> dict[str, Any]:
        """Read the global scope without creating an implicit configuration."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_environments WHERE user_id=$1 AND repo IS NULL", user_id
            )
            return (
                await self._environment_public(conn, row)
                if row
                else {
                    "id": None,
                    "repo": None,
                    "name": "Global",
                    "revision": 0,
                    "variables": [],
                    **EnvironmentScripts().model_dump(),
                }
            )

    async def list_environment_versions(
        self, user_id: str, environment_id: str, before: int | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Read at most fifty immutable history entries with secret-free values."""
        async with self._pool.acquire() as conn:
            row = await self._environment_row(conn, user_id, environment_id, archived=True)
            versions = await conn.fetch(
                "SELECT * FROM agent_environment_versions WHERE environment_id=$1 AND ($2::bigint IS NULL OR revision<$2) ORDER BY revision DESC LIMIT 50",
                environment_id,
                before,
            )
            return [
                {
                    **await self._environment_public(conn, row, v),
                    "changes": _json(v["changes"]),
                    "created_at": v["created_at"],
                }
                for v in versions
            ]
