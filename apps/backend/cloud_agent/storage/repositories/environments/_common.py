"""State and private helpers shared by the environment repository's mixins."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from cloud_agent.environments.environments import (
    EnvironmentCipher,
    EnvironmentForbidden,
    EnvironmentNotFound,
)
from cloud_agent.storage._sql import environment_catalogue
from cloud_agent_shared.environment import EnvironmentVariable

if TYPE_CHECKING:
    import builtins
    from collections.abc import Mapping

    import asyncpg


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _id() -> str:
    return str(uuid.uuid4())


def _cipher() -> EnvironmentCipher:
    return EnvironmentCipher()


#: Who may change a team's environments; every other active member may use them.
_TEAM_EDITORS = frozenset({"owner", "admin"})


class EnvironmentRepositoryBase:
    """The pool, and the row, version and variable helpers every mixin shares."""

    _pool: asyncpg.Pool

    @staticmethod
    async def _environment_lock(conn: Any, user_id: str) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('agent-environments:' || $1, 0))",
            user_id,
        )

    @staticmethod
    async def _team_access(conn: Any, user_id: str, team_id: str) -> tuple[str, int] | None:
        """The account's role in an active team and its access generation, or ``None``."""
        row = await conn.fetchrow(
            "SELECT t.owner_user_id, m.role, m.access_generation FROM agent_teams t "
            "JOIN agent_team_members m ON m.team_id = t.id AND m.user_id = $2 "
            "AND m.state = 'active' WHERE t.id = $1 AND t.state = 'active'",
            team_id,
            user_id,
        )
        if row is None:
            return None
        role = "owner" if row["owner_user_id"] == user_id else row["role"]
        return role, int(row["access_generation"])

    @classmethod
    async def _environment_row(
        cls,
        conn: Any,
        user_id: str,
        environment_id: str,
        *,
        archived: bool = False,
        edit: bool = False,
    ) -> dict[str, Any]:
        """An environment the account may use, or with ``edit`` may change.

        A personal environment is its owner's alone. A team's is used by every
        active member and changed only by the team's owner and admins; anyone
        else is told it does not exist. An edit locks the row, so two admins
        saving at once take turns rather than both claiming one revision.
        """
        row = await conn.fetchrow(
            "SELECT e.*, t.name AS team_name FROM agent_environments e "
            "LEFT JOIN agent_teams t ON t.id = e.team_id "
            "WHERE e.id=$1 AND ($2 OR e.archived_at IS NULL)"
            + (" FOR UPDATE OF e" if edit else ""),
            environment_id,
            archived,
        )
        if row is None:
            raise EnvironmentNotFound("Environment not found")
        found = dict(row)
        if found["team_id"] is None:
            if found["user_id"] != user_id:
                raise EnvironmentNotFound("Environment not found")
            return {**found, "team_role": None, "team_access_generation": None}
        access = await cls._team_access(conn, user_id, found["team_id"])
        if access is None:
            raise EnvironmentNotFound("Environment not found")
        role, generation = access
        if edit and role not in _TEAM_EDITORS:
            raise EnvironmentForbidden(
                "Only the team's owner and admins can change its environments"
            )
        return {**found, "team_role": role, "team_access_generation": generation}

    @staticmethod
    async def _environment_version(
        conn: Any, environment_id: str, revision: int
    ) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            "SELECT * FROM agent_environment_versions WHERE environment_id=$1 AND revision=$2",
            environment_id,
            revision,
        )
        return dict(row) if row is not None else None

    @staticmethod
    async def _environment_stored_variables(
        conn: Any, version_id: str | None
    ) -> builtins.list[dict[str, Any]]:
        if version_id is None:
            return []
        return [
            dict(v)
            for v in await conn.fetch(
                "SELECT * FROM agent_environment_version_variables WHERE version_id=$1 ORDER BY name",
                version_id,
            )
        ]

    @staticmethod
    def _environment_decode(
        variables: builtins.list[dict[str, Any]],
        *,
        include_secrets: bool = True,
    ) -> builtins.list[EnvironmentVariable]:
        if not variables:
            return []
        cipher = _cipher() if any(include_secrets or not v["secret"] for v in variables) else None
        return [
            EnvironmentVariable(
                name=v["name"],
                value=cipher.decrypt(v["value_ciphertext"])
                if cipher and (include_secrets or not v["secret"])
                else "",
                secret=v["secret"],
                phase=v["phase"],
            )
            for v in variables
        ]

    @staticmethod
    def _environment_encode(
        variables: builtins.list[EnvironmentVariable],
    ) -> builtins.list[dict[str, Any]]:
        if not variables:
            return []
        cipher = _cipher()
        return [
            {
                "name": v.name,
                "value_ciphertext": cipher.encrypt(v.value),
                "secret": v.secret,
                "phase": v.phase,
                "value_bytes": len(v.value.encode()),
            }
            for v in variables
        ]

    async def _environment_public(
        self, conn: Any, row: Mapping[str, Any], version: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        version = version or await self._environment_version(conn, row["id"], row["revision"])
        variables = await self._environment_stored_variables(
            conn, version["id"] if version else None
        )
        return environment_catalogue.public_environment(row, version, variables, _cipher)

    @staticmethod
    def _environment_run_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            k: (_json(v) if k == "steps" else v)
            for k, v in dict(row).items()
            if k not in {"fingerprint", "idempotency_key"}
        }

    if TYPE_CHECKING:
        # Defined on PostgresEnvironmentRepository itself, beside the
        # ``retire_changed_snapshots`` it calls; declared here for the edits.
        async def _environment_publish(
            self,
            conn: Any,
            row: dict[str, Any],
            document: dict[str, Any],
            variables: builtins.list[dict[str, Any]],
            changes: builtins.list[dict[str, Any]],
        ) -> dict[str, Any]:
            raise NotImplementedError
