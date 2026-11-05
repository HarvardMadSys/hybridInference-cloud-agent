"""Bulk immutable-version reads and the environment catalogue's public projection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.environment import EnvironmentScripts

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cloud_agent.environments import EnvironmentCipher


async def versions(conn: Any, rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Load precisely the revisions selected by the authorized catalogue query."""
    if not rows:
        return {}
    return {
        row["environment_id"]: dict(row)
        for row in await conn.fetch(
            "SELECT * FROM agent_environment_versions WHERE (environment_id, revision) IN "
            "(SELECT * FROM unnest($1::text[], $2::bigint[]))",
            [row["id"] for row in rows],
            [row["revision"] for row in rows],
        )
    }


async def variables(conn: Any, version_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Read each requested version's variables once, retaining name ordering."""
    result: dict[str, list[dict[str, Any]]] = {}
    if version_ids:
        for row in await conn.fetch(
            "SELECT * FROM agent_environment_version_variables "
            "WHERE version_id=ANY($1::text[]) ORDER BY version_id, name",
            version_ids,
        ):
            result.setdefault(row["version_id"], []).append(dict(row))
    return result


async def latest_tests(conn: Any, user_id: str, environment_ids: list[str]) -> dict[str, Any]:
    """Read the owner's latest test of each authorized environment in one query."""
    result = {}
    if environment_ids:
        for row in await conn.fetch(
            "SELECT DISTINCT ON (v.environment_id) v.environment_id, "
            "r.id,r.state,r.image_digest,r.base_sha,r.finished_at,v.revision,"
            "COALESCE(g.revision,0) AS global_revision FROM agent_environment_runs r "
            "JOIN agent_environment_bindings b ON b.id=r.binding_id "
            "JOIN agent_environment_versions v ON v.id=b.environment_version_id "
            "LEFT JOIN agent_environment_versions g ON g.id=b.global_version_id "
            "WHERE r.user_id=$1 AND v.environment_id=ANY($2::text[]) AND r.purpose='test' "
            "ORDER BY v.environment_id,r.created_at DESC,r.id DESC",
            user_id,
            environment_ids,
        ):
            view = dict(row)
            result[view.pop("environment_id")] = view
    return result


def public_environment(
    row: Mapping[str, Any],
    version: Mapping[str, Any] | None,
    stored_variables: list[dict[str, Any]],
    cipher: Callable[[], EnvironmentCipher],
) -> dict[str, Any]:
    """Render an already-loaded version, never returning a secret's plaintext."""
    document = version["document"] if version else EnvironmentScripts().model_dump()
    if isinstance(document, str):
        document = json.loads(document)
    team_id = row.get("team_id")
    return {
        "id": row["id"],
        "repo": row["repo"],
        "name": row["name"],
        "team": {"id": team_id, "name": row.get("team_name") or ""} if team_id else None,
        "can_edit": team_id is None or row.get("team_role") in {"owner", "admin"},
        "revision": int(version["revision"] if version else 0),
        **document,
        "archived": row["archived_at"] is not None,
        "updated_at": row["updated_at"],
        "variables": [
            {
                "name": variable["name"],
                "secret": variable["secret"],
                "phase": variable["phase"],
                "value": None
                if variable["secret"]
                else cipher().decrypt(variable["value_ciphertext"]),
                "is_set": True,
                "updated_at": variable["updated_at"],
            }
            for variable in stored_variables
        ],
    }
