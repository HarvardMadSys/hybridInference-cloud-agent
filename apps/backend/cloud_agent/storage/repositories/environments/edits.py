"""Editing environments: save, archive, default, copy and restore."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.environments.environments import (
    EnvironmentConflict,
    EnvironmentForbidden,
    EnvironmentNotFound,
)
from cloud_agent.storage.repositories.environments._common import (
    _TEAM_EDITORS,
    EnvironmentRepositoryBase,
    _id,
    _json,
)
from cloud_agent_shared.environment import (
    MAX_VARIABLES,
    EnvironmentInputError,
    EnvironmentScripts,
    EnvironmentVariable,
    VariableEdit,
    merge_variables,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import Mapping


class EditsMixin(EnvironmentRepositoryBase):
    """Save, archive, copy and restore environments."""

    @staticmethod
    async def _team_environment_audit(
        conn: Any, row: Mapping[str, Any], actor: str, action: str, **details: Any
    ) -> None:
        """Record a change to a team's environment in that team's audit log."""
        if row.get("team_id") is None:
            return
        await conn.execute(
            "INSERT INTO agent_team_audit_events (team_id, actor_user_id, action, target_id, details) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            row["team_id"],
            actor,
            action,
            row["id"],
            json.dumps({"name": row.get("name"), "repo": row.get("repo"), **details}),
        )

    async def _environment_check_merged(
        self,
        conn: Any,
        user_id: str,
        repo: str | None,
        environment_id: str,
        candidate: builtins.list[EnvironmentVariable],
        *,
        team: bool = False,
    ) -> None:
        if len(candidate) > MAX_VARIABLES:
            raise EnvironmentInputError("An environment accepts at most 100 variables")
        if team:
            # Each member's globals join it only when a task binds it, so the
            # combined limits are checked there, for that member.
            merge_variables(candidate)
        elif repo is None:
            merge_variables(candidate)
            rows = await conn.fetch(
                "SELECT e.name,v.id FROM agent_environments e JOIN agent_environment_versions v "
                "ON v.environment_id=e.id AND v.revision=e.revision WHERE e.user_id=$1 "
                "AND e.team_id IS NULL AND e.repo IS NOT NULL AND e.archived_at IS NULL",
                user_id,
            )
            for row in rows:
                try:
                    merge_variables(
                        candidate,
                        self._environment_decode(
                            await self._environment_stored_variables(conn, row["id"])
                        ),
                    )
                except ValueError:
                    raise EnvironmentInputError(
                        f"Combined limits exceeded for environment {row['name']}"
                    ) from None
        else:
            global_id = await conn.fetchval(
                "SELECT v.id FROM agent_environments e JOIN agent_environment_versions v "
                "ON v.environment_id=e.id AND v.revision=e.revision WHERE e.user_id=$1 AND e.repo IS NULL",
                user_id,
            )
            merge_variables(
                self._environment_decode(await self._environment_stored_variables(conn, global_id)),
                candidate,
            )

    async def save_environment(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        self,
        user_id: str,
        *,
        repo: str | None,
        name: str,
        environment_id: str | None,
        revision: int,
        document: dict[str, Any],
        edits: builtins.list[dict[str, Any]],
        expected_global_revision: int | None = None,
        team_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish one atomic validated revision, never storing raw values.

        ``team_id`` creates the environment for that team, which only its owner
        and admins may do; it is ignored when saving an existing environment,
        whose owner never changes. A team's environment is edited by its owner
        and admins, and each change is recorded in the team's audit log.
        """
        scripts = EnvironmentScripts.model_validate(document).model_dump(mode="json")
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise EnvironmentInputError("Environment name must have 1-80 characters")
        if repo is None and any(scripts.values()):
            raise EnvironmentInputError("Global scope accepts only variables")
        parsed = [VariableEdit.model_validate(e) for e in edits]
        if len(parsed) != len({e.name for e in parsed}):
            raise EnvironmentInputError("Duplicate variable edit")
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await self._environment_lock(conn, user_id)
                if expected_global_revision is not None:
                    actual_global = (
                        await conn.fetchval(
                            "SELECT revision FROM agent_environments WHERE user_id=$1 AND repo IS NULL",
                            user_id,
                        )
                        or 0
                    )
                    if expected_global_revision != actual_global:
                        raise EnvironmentConflict(
                            "Global variables changed; preview the import again",
                            current_revision=actual_global,
                        )
                if environment_id is None and repo is None:
                    existing = await conn.fetchrow(
                        "SELECT * FROM agent_environments WHERE user_id=$1 AND repo IS NULL",
                        user_id,
                    )
                    environment_id = existing["id"] if existing else None
                created = not environment_id
                if environment_id:
                    row = await self._environment_row(conn, user_id, environment_id, edit=True)
                    if row["repo"] != repo:
                        raise EnvironmentInputError("Environment repository cannot be changed")
                else:
                    if revision != 0:
                        raise EnvironmentConflict("Environment changed", current_revision=0)
                    team: dict[str, Any] = {"team_role": None, "team_access_generation": None}
                    if team_id is not None:
                        if repo is None:
                            raise EnvironmentInputError(
                                "A team environment belongs to one repository"
                            )
                        access = await self._team_access(conn, user_id, team_id)
                        if access is None:
                            raise EnvironmentNotFound("Team not found")
                        if access[0] not in _TEAM_EDITORS:
                            raise EnvironmentForbidden(
                                "Only the team's owner and admins can add its environments"
                            )
                        team = {
                            "team_role": access[0],
                            "team_access_generation": access[1],
                            "team_name": await conn.fetchval(
                                "SELECT name FROM agent_teams WHERE id=$1", team_id
                            ),
                        }
                    row = {
                        **dict(
                            await conn.fetchrow(
                                "INSERT INTO agent_environments"
                                "(id,user_id,repo,name,normalized_name,team_id) "
                                "VALUES($1,$2,$3,$4,$5,$6) RETURNING *",
                                _id(),
                                user_id,
                                repo,
                                name,
                                name.casefold(),
                                team_id,
                            )
                        ),
                        **team,
                    }
                if row["revision"] != revision:
                    raise EnvironmentConflict(
                        "Environment changed; refresh before saving",
                        current_revision=row["revision"],
                    )
                version = await self._environment_version(conn, row["id"], revision)
                old = await self._environment_stored_variables(
                    conn, version["id"] if version else None
                )
                values = {v["name"]: v for v in old}
                changes: builtins.list[dict[str, Any]] = []
                for edit in parsed:
                    prior = values.get(edit.name)
                    if edit.operation == "remove":
                        values.pop(edit.name, None)
                    elif edit.operation == "keep":
                        if prior is None or (prior["secret"] and not edit.secret):
                            raise EnvironmentInputError(
                                "Secret values must be replaced when changing to ordinary variables"
                            )
                        values[edit.name] = {**prior, "secret": edit.secret, "phase": edit.phase}
                    else:
                        assert edit.value is not None
                        values[edit.name] = self._environment_encode(
                            [
                                EnvironmentVariable(
                                    name=edit.name,
                                    value=edit.value,
                                    secret=edit.secret,
                                    phase=edit.phase,
                                )
                            ]
                        )[0]
                    changes.append(
                        {
                            "name": edit.name,
                            "operation": edit.operation,
                            "secret": edit.secret,
                            "phase": edit.phase,
                        }
                    )
                await self._environment_check_merged(
                    conn,
                    user_id,
                    repo,
                    row["id"],
                    self._environment_decode(list(values.values())),
                    team=row["team_id"] is not None,
                )
                await conn.execute(
                    "UPDATE agent_environments SET name=$2,normalized_name=$3 WHERE id=$1",
                    row["id"],
                    name,
                    name.casefold(),
                )
                await self._team_environment_audit(
                    conn,
                    {**row, "name": name},
                    user_id,
                    "environment_created" if created else "environment_updated",
                    revision=revision + 1,
                )
                # A personal environment's first save becomes its repository's
                # default; a team's never changes its members' own choices.
                if repo is not None and row["team_id"] is None:
                    await conn.execute(
                        "INSERT INTO agent_repository_environment_defaults(user_id,repo,environment_id) "
                        "VALUES($1,$2,$3) ON CONFLICT(user_id,repo) DO NOTHING",
                        user_id,
                        repo,
                        row["id"],
                    )
                return await self._environment_publish(
                    conn, {**row, "name": name}, scripts, list(values.values()), changes
                )
        except asyncpg.UniqueViolationError:
            raise EnvironmentConflict("An environment already uses this name") from None

    async def archive_environment(self, user_id: str, environment_id: str, revision: int) -> None:
        """Retain history and bindings while removing future default selection.

        Every account's default that named it is cleared, a team's members'
        included: none of them can select it any more.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await self._environment_lock(conn, user_id)
            row = await self._environment_row(conn, user_id, environment_id, edit=True)
            if row["revision"] != revision or row["repo"] is None:
                raise EnvironmentConflict(
                    "Environment changed or cannot be archived", current_revision=row["revision"]
                )
            await conn.execute(
                "UPDATE agent_environments SET archived_at=NOW(),updated_at=NOW() WHERE id=$1",
                environment_id,
            )
            await conn.execute(
                "UPDATE agent_repository_environment_defaults SET environment_id=NULL,revision=revision+1 WHERE environment_id=$1",
                environment_id,
            )
            await self._team_environment_audit(conn, row, user_id, "environment_archived")

    async def set_environment_default(
        self, user_id: str, repo: str, environment_id: str | None, revision: int
    ) -> dict[str, Any]:
        """Atomically change only a default the owner has reviewed."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._environment_lock(conn, user_id)
            if environment_id is not None:
                # Any environment the account may use, its teams' included.
                env = await self._environment_row(conn, user_id, environment_id)
                if env["repo"] != repo:
                    raise EnvironmentNotFound("Environment not found for this repository")
            current = await conn.fetchrow(
                "SELECT * FROM agent_repository_environment_defaults WHERE user_id=$1 AND repo=$2",
                user_id,
                repo,
            )
            if (current["revision"] if current else 0) != revision:
                raise EnvironmentConflict(
                    "Default environment changed",
                    current_revision=current["revision"] if current else 0,
                )
            return dict(
                await conn.fetchrow(
                    "INSERT INTO agent_repository_environment_defaults(user_id,repo,environment_id,revision) "
                    "VALUES($1,$2,$3,$4) ON CONFLICT(user_id,repo) DO UPDATE SET environment_id=$3,revision=$4 RETURNING *",
                    user_id,
                    repo,
                    environment_id,
                    revision + 1,
                )
            )

    async def copy_environment(
        self,
        user_id: str,
        environment_id: str,
        *,
        repo: str,
        name: str,
        revision: int,
        idempotency_key: str | None = None,
        team_id: str | None = None,
    ) -> dict[str, Any]:
        """Duplicate encrypted configuration without sharing future revisions.

        The source is any environment the account may use. The copy is the
        account's own, or with ``team_id`` that team's, which only its owner
        and admins may add to.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await self._environment_lock(conn, user_id)
            name = name.strip()
            if not 1 <= len(name) <= 80:
                raise EnvironmentInputError("Environment name must have 1-80 characters")
            fingerprint = (
                __import__("hashlib")
                .sha256(
                    json.dumps(
                        [environment_id, revision, repo, name]
                        + ([team_id] if team_id is not None else []),
                        separators=(",", ":"),
                    ).encode()
                )
                .hexdigest()
            )
            if idempotency_key:
                existing = await conn.fetchrow(
                    "SELECT * FROM agent_environments WHERE user_id=$1 AND copy_request_key=$2",
                    user_id,
                    idempotency_key,
                )
                if existing:
                    if existing["copy_request_fingerprint"] != fingerprint:
                        raise EnvironmentConflict("Idempotency key already used for another copy")
                    if existing["archived_at"] is not None:
                        raise EnvironmentConflict("Previous copy has been archived")
                    return await self._environment_public(
                        conn, await self._environment_row(conn, user_id, existing["id"])
                    )
            source = await self._environment_row(conn, user_id, environment_id)
            target: dict[str, Any] = {"team_role": None, "team_access_generation": None}
            if team_id is not None:
                access = await self._team_access(conn, user_id, team_id)
                if access is None:
                    raise EnvironmentNotFound("Team not found")
                if access[0] not in _TEAM_EDITORS:
                    raise EnvironmentForbidden(
                        "Only the team's owner and admins can add its environments"
                    )
                target = {
                    "team_role": access[0],
                    "team_access_generation": access[1],
                    "team_name": await conn.fetchval(
                        "SELECT name FROM agent_teams WHERE id=$1", team_id
                    ),
                }
            if source["revision"] != revision:
                raise EnvironmentConflict(
                    "Source environment changed", current_revision=source["revision"]
                )
            version = await self._environment_version(conn, environment_id, revision)
            assert version is not None
            values = await self._environment_stored_variables(conn, version["id"])
            row = {
                **dict(
                    await conn.fetchrow(
                        "INSERT INTO agent_environments(id,user_id,repo,name,normalized_name,copy_request_key,copy_request_fingerprint,team_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *",
                        _id(),
                        user_id,
                        repo,
                        name,
                        name.casefold(),
                        idempotency_key,
                        fingerprint if idempotency_key else None,
                        team_id,
                    )
                ),
                **target,
            }
            await self._environment_check_merged(
                conn,
                user_id,
                repo,
                row["id"],
                self._environment_decode(values),
                team=team_id is not None,
            )
            await self._team_environment_audit(
                conn, row, user_id, "environment_created", copied_from=environment_id
            )
            return await self._environment_publish(
                conn, row, _json(version["document"]), values, [{"operation": "copy"}]
            )

    async def restore_environment(
        self, user_id: str, environment_id: str, *, revision: int, source_revision: int
    ) -> dict[str, Any]:
        """Publish a historical snapshot as a new revision, preserving CAS."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._environment_lock(conn, user_id)
            row = await self._environment_row(conn, user_id, environment_id, edit=True)
            if row["revision"] != revision:
                raise EnvironmentConflict("Environment changed", current_revision=row["revision"])
            source = await self._environment_version(conn, environment_id, source_revision)
            if source is None:
                raise EnvironmentNotFound("Version not found")
            values = await self._environment_stored_variables(conn, source["id"])
            await self._environment_check_merged(
                conn,
                user_id,
                row["repo"],
                environment_id,
                self._environment_decode(values),
                team=row["team_id"] is not None,
            )
            await self._team_environment_audit(
                conn, row, user_id, "environment_updated", restored_revision=source_revision
            )
            return await self._environment_publish(
                conn,
                row,
                _json(source["document"]),
                values,
                [{"operation": "restore", "source_revision": source_revision}],
            )
