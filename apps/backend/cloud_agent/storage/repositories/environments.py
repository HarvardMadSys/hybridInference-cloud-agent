"""Transactional owner-scoped environment history and execution snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.environments import (
    EnvironmentAccessRevoked,
    EnvironmentBusy,
    EnvironmentCipher,
    EnvironmentConflict,
    EnvironmentEncryptionError,
    EnvironmentForbidden,
    EnvironmentNotFound,
)
from cloud_agent.execution_profiles import default_execution_profile, shared_sandbox_claim_attested
from cloud_agent.storage._sql.compute_admission import host_compute_admission, lock_compute_topology
from cloud_agent_shared.environment import (
    MAX_VARIABLES,
    BoundEnvironment,
    EnvironmentInputError,
    EnvironmentScripts,
    EnvironmentVariable,
    VariableEdit,
    merge_variables,
)
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterable, Mapping


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _id() -> str:
    return str(uuid.uuid4())


def _cipher() -> EnvironmentCipher:
    return EnvironmentCipher()


def setup_fingerprint(setup_script: str | None, variables: list[EnvironmentVariable]) -> str:
    """HMAC of what an installation depends on: the script and setup-time values.

    Equal fingerprints install the same thing, so an installation snapshot made
    for one serves the other. Keyed, so a fingerprint written into a container
    tells its code nothing about the values.
    """
    material = json.dumps(
        {
            "v": 1,
            "setup_script": setup_script or "",
            "variables": sorted([v.name, v.value] for v in variables if v.phase != "runtime"),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _cipher().mac(material)


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


def runtime_only(variables: list[EnvironmentVariable]) -> list[EnvironmentVariable]:
    """A member's personal globals, as a team environment uses them: at run time only.

    Installation is the team's alone, so it is the same for every member and
    their snapshots can be shared; a member's own values still reach the
    agent and the commands it runs. The team's definition of a name wins.
    """
    return [v.model_copy(update={"phase": "runtime"}) for v in variables if v.phase != "setup"]


#: Who may change a team's environments; every other active member may use them.
_TEAM_EDITORS = frozenset({"owner", "admin"})


def optional_setup_fingerprint(
    setup_script: str | None, variables: list[EnvironmentVariable]
) -> str | None:
    """The fingerprint, or ``None`` without setup or without the deployment's key.

    Snapshots need the key; tasks do not, so a deployment without it simply
    never snapshots.
    """
    if not setup_script:
        return None
    try:
        return setup_fingerprint(setup_script, variables)
    except EnvironmentEncryptionError:
        return None


async def lock_environment_snapshots(conn: Any, user_id: str) -> None:
    """Take the owner's snapshot lock, before any of their snapshot or environment-run rows.

    A write that locks more than one of an owner's installation snapshots,
    or a snapshot and its run, takes this first and holds it to the end of
    its transaction, so no two of them take those rows in different orders.
    A receipt takes the snapshot it makes ready and its run, then the older
    builds it supersedes and the snapshots the owner's quota evicts; a
    deletion takes an environment's snapshots in whatever order its one
    statement scans them, then their runs.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('agent-environment-snapshots:' || $1, 0))",
        user_id,
    )


async def lock_team_environment_snapshots(conn: Any, environment_id: str) -> list[str]:
    """Take the snapshot lock of every member with a snapshot of a team environment.

    A team environment's snapshots belong to whichever members built them, so
    a write over several of them takes each of those owners' locks, in one
    order, before any of the rows. Returns the owners; a snapshot queued by
    anyone else afterwards is not the write's to touch.
    """
    owners = [
        row["user_id"]
        for row in await conn.fetch(
            "SELECT DISTINCT user_id FROM agent_environment_snapshots "
            "WHERE environment_id=$1 ORDER BY user_id",
            environment_id,
        )
    ]
    for owner in owners:
        await lock_environment_snapshots(conn, owner)
    return owners


async def retire_changed_snapshots(
    conn: Any, user_id: str, environment_ids: Iterable[str] | None = None
) -> None:
    """Retire the owner's ready snapshots of an installation they have since changed.

    A snapshot holds what its installation wrote, which can include a
    setup-time Secret (an ``.npmrc``, say). Once the owner changes the
    install script or a setup-time value, a global one included, new tasks
    are bound to another fingerprint and never start from it, yet it stayed
    stored until it expired. It is superseded instead: no longer handed out,
    and deleted after the grace a snapshot replaced by a newer build gets.
    Only the current configuration says what is current, and a fingerprint
    needs the deployment's key, so this runs in the transaction that
    publishes a configuration and in the one that records a build.

    A snapshot built for a task that replaced the install script is checked
    against that script with the current values. ``environment_ids`` limits
    the check to those environments; ``None`` checks all of the owner's, as
    a change to their global variables needs. Without the key nothing is
    retired: without it nothing is matched or built either.
    """
    await lock_environment_snapshots(conn, user_id)
    snapshots = await conn.fetch(
        """
        SELECT s.id, s.environment_id, s.setup_fingerprint,
               COALESCE(b.has_setup_override, FALSE) AS has_setup_override, b.setup_override
        FROM agent_environment_snapshots s
        LEFT JOIN agent_environment_runs r ON r.id = s.run_id
        LEFT JOIN agent_environment_bindings b ON b.id = r.binding_id
        WHERE s.user_id = $1 AND s.state = 'ready'
          AND ($2::text[] IS NULL OR s.environment_id = ANY($2::text[]))
        """,
        user_id,
        None if environment_ids is None else list(environment_ids),
    )
    if not snapshots:
        return
    stale = []
    try:
        installations = await PostgresEnvironmentRepository._environment_installations(
            conn, user_id, list({row["environment_id"] for row in snapshots})
        )
        for row in snapshots:
            # An archived environment's snapshots are maintenance's to retire.
            if (installation := installations.get(row["environment_id"])) is None:
                continue
            script, variables = installation
            if row["has_setup_override"]:
                script = row["setup_override"]
            current = setup_fingerprint(script, variables) if script else None
            if current != row["setup_fingerprint"]:
                stale.append(row["id"])
    except EnvironmentEncryptionError:
        return
    if stale:
        await conn.execute(
            "UPDATE agent_environment_snapshots SET state='superseded', "
            "detail='The installation changed', updated_at=NOW() "
            "WHERE id=ANY($1::text[]) AND state='ready'",
            stale,
        )


class PostgresEnvironmentRepository:
    """Serialize configuration changes and bindings per owner over one pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool
        self._next_gc = 0.0
        self._redactors: OrderedDict[tuple[str, str, str], tuple[float, SecretRedactor]] = (
            OrderedDict()
        )
        # Versions are immutable, so a pair's fingerprint never changes.
        self._fingerprints: OrderedDict[tuple[str, str | None], str | None] = OrderedDict()

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
    async def _environment_installations(
        conn: Any, user_id: str, environment_ids: builtins.list[str]
    ) -> dict[str, tuple[str, builtins.list[EnvironmentVariable]]]:
        """What a task bound now would install with: each active environment's setup and values.

        The values are the global and environment variables merged, as a
        binding merges them; an environment whose configuration no longer
        merges binds no task and is left out. A team environment installs with
        the team's values alone (:func:`runtime_only`), whoever ``user_id`` is.
        """
        rows = await conn.fetch(
            "SELECT e.id,e.team_id,v.id AS version_id,v.document FROM agent_environments e "
            "JOIN agent_environment_versions v ON v.environment_id=e.id AND v.revision=e.revision "
            "WHERE (e.user_id=$1 OR e.team_id IS NOT NULL) AND e.id=ANY($2::text[]) "
            "AND e.repo IS NOT NULL AND e.archived_at IS NULL",
            user_id,
            environment_ids,
        )
        if not rows:
            return {}
        global_id = await conn.fetchval(
            "SELECT v.id FROM agent_environments e JOIN agent_environment_versions v "
            "ON v.environment_id=e.id AND v.revision=e.revision WHERE e.user_id=$1 AND e.repo IS NULL",
            user_id,
        )
        decode = PostgresEnvironmentRepository._environment_decode
        stored = PostgresEnvironmentRepository._environment_stored_variables
        global_values = decode(await stored(conn, global_id))
        result = {}
        for row in rows:
            try:
                merged = merge_variables(
                    [] if row["team_id"] else global_values,
                    decode(await stored(conn, row["version_id"])),
                )
            except ValueError:
                continue
            result[row["id"]] = (_json(row["document"]).get("setup_script") or "", merged)
        return result

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
        document = _json(version["document"]) if version else EnvironmentScripts().model_dump()
        team_id = row.get("team_id")
        return {
            "id": row["id"],
            "repo": row["repo"],
            "name": row["name"],
            "team": {"id": team_id, "name": row.get("team_name") or ""} if team_id else None,
            # A personal environment is its owner's; a team's, its owner's and admins'.
            "can_edit": team_id is None or row.get("team_role") in _TEAM_EDITORS,
            "revision": int(version["revision"] if version else 0),
            **document,
            "archived": row["archived_at"] is not None,
            "updated_at": row["updated_at"],
            "variables": [
                {
                    "name": v["name"],
                    "secret": v["secret"],
                    "phase": v["phase"],
                    "value": None if v["secret"] else _cipher().decrypt(v["value_ciphertext"]),
                    "is_set": True,
                    "updated_at": v["updated_at"],
                }
                for v in variables
            ],
        }

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
            result = []
            for row in rows:
                view = await self._environment_public(conn, row)
                latest = await conn.fetchrow(
                    "SELECT r.id,r.state,r.image_digest,r.base_sha,r.finished_at,v.revision,"
                    "COALESCE(g.revision,0) AS global_revision FROM agent_environment_runs r "
                    "JOIN agent_environment_bindings b ON b.id=r.binding_id "
                    "JOIN agent_environment_versions v ON v.id=b.environment_version_id "
                    "LEFT JOIN agent_environment_versions g ON g.id=b.global_version_id "
                    "WHERE r.user_id=$1 AND v.environment_id=$2 AND r.purpose='test' ORDER BY r.created_at DESC LIMIT 1",
                    user_id,
                    row["id"],
                )
                result.append(
                    {
                        **view,
                        "is_default": bool(row["is_default"]),
                        "default_revision": row["default_revision"],
                        "latest_test": dict(latest) if latest else None,
                    }
                )
            return result

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
            for row in rows:
                # A team's installation does not depend on the member's globals.
                scope = None if row["team_id"] else global_id
                key = (row["version_id"], scope)
                if key not in self._fingerprints:
                    setup = _json(row["document"]).get("setup_script") or ""
                    try:
                        self._fingerprints[key] = (
                            optional_setup_fingerprint(
                                setup,
                                merge_variables(
                                    self._environment_decode(
                                        await self._environment_stored_variables(conn, scope)
                                    ),
                                    self._environment_decode(
                                        await self._environment_stored_variables(
                                            conn, row["version_id"]
                                        )
                                    ),
                                ),
                            )
                            if setup
                            else None
                        )
                    except EnvironmentEncryptionError:
                        self._fingerprints[key] = None
                    while len(self._fingerprints) > 512:
                        self._fingerprints.popitem(last=False)
                result[row["id"]] = self._fingerprints[key]
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

    async def _environment_publish(
        self,
        conn: Any,
        row: dict[str, Any],
        document: dict[str, Any],
        variables: builtins.list[dict[str, Any]],
        changes: builtins.list[dict[str, Any]],
    ) -> dict[str, Any]:
        revision = int(row["revision"]) + 1
        version_id = _id()
        await conn.execute(
            "INSERT INTO agent_environment_versions(id,environment_id,revision,document,changes) "
            "VALUES($1,$2,$3,$4::jsonb,$5::jsonb)",
            version_id,
            row["id"],
            revision,
            json.dumps(document),
            json.dumps(changes),
        )
        for v in variables:
            await conn.execute(
                "INSERT INTO agent_environment_version_variables "
                "(version_id,name,value_ciphertext,secret,phase,value_bytes) VALUES($1,$2,$3,$4,$5,$6)",
                version_id,
                v["name"],
                v["value_ciphertext"],
                v["secret"],
                v["phase"],
                v["value_bytes"],
            )
        await conn.execute(
            "UPDATE agent_environments SET revision=$2,updated_at=NOW() WHERE id=$1",
            row["id"],
            revision,
        )
        # This revision may install something other than its snapshots hold;
        # global variables feed every environment of their owner, and a team
        # environment's snapshots are every member's.
        if row.get("team_id"):
            for owner in await lock_team_environment_snapshots(conn, row["id"]):
                await retire_changed_snapshots(conn, owner, [row["id"]])
        else:
            await retire_changed_snapshots(
                conn, row["user_id"], None if row["repo"] is None else [row["id"]]
            )
        row = {**row, "revision": revision}
        return await self._environment_public(conn, row)

    async def save_environment(
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

    async def _bind_environment(
        self, conn: Any, user_id: str, repo: str | None, selection: dict[str, Any]
    ) -> tuple[str, str | None]:
        """Freeze a snapshot inside the transaction that creates its work item.

        A null ``repo`` is a temporary workspace. Environments belong to one
        repository, so it binds only the owner's global variables.

        A team's environment binds the member's globals for run time only
        (:func:`runtime_only`) and records the team and the member's access
        generation, which every later turn is checked against.
        """
        await self._environment_lock(conn, user_id)
        environment_id = selection.get("environment_id")
        if repo is None:
            if environment_id:
                raise EnvironmentNotFound("A temporary workspace has no repository environment")
        elif not selection.get("environment_id_provided", False):
            environment_id = await conn.fetchval(
                "SELECT environment_id FROM agent_repository_environment_defaults WHERE user_id=$1 AND repo=$2",
                user_id,
                repo,
            )
            if environment_id is not None:
                try:
                    await self._environment_row(conn, user_id, environment_id)
                except EnvironmentNotFound:
                    # A team default from a team the account has since left:
                    # it can no longer select it, as if the team had archived it.
                    await conn.execute(
                        "UPDATE agent_repository_environment_defaults "
                        "SET environment_id=NULL,revision=revision+1 WHERE user_id=$1 AND repo=$2",
                        user_id,
                        repo,
                    )
                    environment_id = None
        env = await self._environment_row(conn, user_id, environment_id) if environment_id else None
        if env and env["repo"] != repo:
            raise EnvironmentNotFound("Environment not found for this repository")
        global_row = await conn.fetchrow(
            "SELECT * FROM agent_environments WHERE user_id=$1 AND repo IS NULL", user_id
        )
        for key, row in (("environment_revision", env), ("global_revision", global_row)):
            if selection.get(key) is not None and selection[key] != (row["revision"] if row else 0):
                raise EnvironmentConflict(
                    "Environment version changed; refresh before creating",
                    current_revision=row["revision"] if row else 0,
                )
        version = await self._environment_version(conn, env["id"], env["revision"]) if env else None
        global_version = (
            await self._environment_version(conn, global_row["id"], global_row["revision"])
            if global_row
            else None
        )
        team_id = env["team_id"] if env else None
        personal = self._environment_decode(
            await self._environment_stored_variables(
                conn, global_version["id"] if global_version else None
            )
        )
        merged = merge_variables(
            runtime_only(personal) if team_id else personal,
            self._environment_decode(
                await self._environment_stored_variables(conn, version["id"] if version else None)
            ),
        )
        has_override = bool(selection.get("has_setup_override", False))
        setup = (
            selection.get("setup_override")
            if has_override
            else (_json(version["document"])["setup_script"] if version else None)
        )
        document = _json(version["document"]) if version else {}
        required = bool(merged or setup or document.get("start_script") or document.get("actions"))
        binding_id = _id()
        await conn.execute(
            "INSERT INTO agent_environment_bindings(id,user_id,repo,environment_version_id,global_version_id,setup_override,has_setup_override,required,setup_fingerprint,team_id,team_access_generation) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            binding_id,
            user_id,
            repo,
            version["id"] if version else None,
            global_version["id"] if global_version else None,
            selection.get("setup_override"),
            has_override,
            required,
            # Only a saved environment's installation can be snapshotted.
            optional_setup_fingerprint(setup, merged) if version else None,
            team_id,
            env["team_access_generation"] if env and team_id else None,
        )
        return binding_id, setup

    @classmethod
    async def _require_team_binding_access(cls, conn: Any, binding: Mapping[str, Any]) -> None:
        """Refuse a team binding whose owner left, or left and rejoined, that team."""
        if binding["team_id"] is None:
            return
        access = await cls._team_access(conn, binding["user_id"], binding["team_id"])
        if access is None or access[1] != binding["team_access_generation"]:
            raise EnvironmentAccessRevoked(
                "This chat uses an environment of a team you are no longer a member of"
            )

    async def get_environment_binding(
        self,
        user_id: str,
        binding_id: str,
        *,
        include_secrets: bool = True,
        for_masking: bool = False,
    ) -> BoundEnvironment:
        """Decrypt the exact immutable binding after proving ownership.

        Values bound from a team's environment are handed out only while the
        owner is still the member who bound them: every path that gives them
        to a container (a turn, a terminal, an action, a Fork, preparation, a
        test) reads them here. ``for_masking`` reads them anyway, for the
        redaction and publication scanning that must keep finding them after
        the owner leaves; such a caller never passes them on.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_environment_bindings WHERE id=$1 AND user_id=$2",
                binding_id,
                user_id,
            )
            if row is None:
                raise EnvironmentNotFound("Binding not found")
            if include_secrets and not for_masking:
                await self._require_team_binding_access(conn, row)
            version = (
                await conn.fetchrow(
                    "SELECT * FROM agent_environment_versions WHERE id=$1",
                    row["environment_version_id"],
                )
                if row["environment_version_id"]
                else None
            )
            global_version = (
                await conn.fetchrow(
                    "SELECT * FROM agent_environment_versions WHERE id=$1", row["global_version_id"]
                )
                if row["global_version_id"]
                else None
            )
            personal = self._environment_decode(
                await self._environment_stored_variables(conn, row["global_version_id"]),
                include_secrets=include_secrets,
            )
            values = merge_variables(
                runtime_only(personal) if row["team_id"] else personal,
                self._environment_decode(
                    await self._environment_stored_variables(conn, row["environment_version_id"]),
                    include_secrets=include_secrets,
                ),
                # Task-only variables are no longer accepted, but a workspace
                # bound before their removal keeps the ones it was given.
                self._environment_decode(_json(row["overrides"]), include_secrets=include_secrets),
            )
            document = _json(version["document"]) if version else EnvironmentScripts().model_dump()
            if row["has_setup_override"]:
                document = {**document, "setup_script": row["setup_override"] or ""}
            return BoundEnvironment(
                binding_id=binding_id,
                environment_id=version["environment_id"] if version else None,
                revision=version["revision"] if version else 0,
                global_revision=global_version["revision"] if global_version else 0,
                variables=values,
                runtime_revision=row["runtime_revision"],
                runtime_variables=self._environment_decode(
                    _json(row["runtime_variables"]), include_secrets=include_secrets
                )
                if row["runtime_variables"] is not None
                else None,
                setup_fingerprint=row["setup_fingerprint"] or "",
                previous_secrets={
                    f"{v.name}@history{index}": v.value
                    for index, v in enumerate(
                        self._environment_decode(
                            _json(row["runtime_secret_history"]), include_secrets=include_secrets
                        )
                    )
                    if v.secret and v.value
                },
                **document,
            )

    async def refresh_runtime_environment(
        self, user_id: str, binding_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Select latest runtime variables, preserving original setup inputs and scripts."""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._environment_lock(conn, user_id)
            binding = await conn.fetchrow(
                "SELECT * FROM agent_environment_bindings WHERE id=$1 AND user_id=$2 FOR UPDATE",
                binding_id,
                user_id,
            )
            if binding is None:
                raise EnvironmentNotFound("Binding not found")
            if binding["runtime_revision"] != expected_revision:
                raise EnvironmentConflict(
                    "Runtime variables changed", current_revision=binding["runtime_revision"]
                )
            await self._require_team_binding_access(conn, binding)
            env = (
                await conn.fetchrow(
                    "SELECT e.* FROM agent_environments e JOIN agent_environment_versions v ON v.environment_id=e.id WHERE v.id=$1",
                    binding["environment_version_id"],
                )
                if binding["environment_version_id"]
                else None
            )
            if env and env["archived_at"] is not None:
                raise EnvironmentConflict("Environment is archived")
            global_row = await conn.fetchrow(
                "SELECT * FROM agent_environments WHERE user_id=$1 AND repo IS NULL", user_id
            )
            latest = (
                await self._environment_version(conn, env["id"], env["revision"]) if env else None
            )
            globals_latest = (
                await self._environment_version(conn, global_row["id"], global_row["revision"])
                if global_row
                else None
            )
            personal = self._environment_decode(
                await self._environment_stored_variables(
                    conn, globals_latest["id"] if globals_latest else None
                )
            )
            merged = merge_variables(
                runtime_only(personal) if binding["team_id"] else personal,
                self._environment_decode(
                    await self._environment_stored_variables(conn, latest["id"] if latest else None)
                ),
                # Empty for every binding made since task-only variables were removed.
                self._environment_decode(_json(binding["overrides"])),
            )
            runtime = [v for v in merged if v.phase in ("runtime", "both")]
            # Original installation secrets remain in the base snapshot. Retain prior
            # runtime values as scan material for files produced by earlier turns:
            # each value once, and none still current, which the new runtime
            # variables carry. Otherwise every refresh re-adds the unchanged
            # Secrets, and a few refreshes exhaust the history limit.
            prior = _json(binding["runtime_secret_history"])
            old = [
                v
                for v in (
                    _json(binding["runtime_variables"])
                    if binding["runtime_variables"] is not None
                    else []
                )
                if v["secret"]
            ]
            if old:
                cipher = _cipher()
                kept = {cipher.decrypt(v["value_ciphertext"]) for v in prior}
                kept.update(v.value for v in runtime if v.secret)
                for v in old:
                    value = cipher.decrypt(v["value_ciphertext"])
                    if value not in kept:
                        prior.append(v)
                        kept.add(value)
            if len(prior) > 512 or sum(v["value_bytes"] for v in prior) > 262144:
                raise EnvironmentInputError(
                    "Runtime Secret history limit reached; create a new workspace"
                )
            await conn.execute(
                "UPDATE agent_environment_bindings SET runtime_variables=$3::jsonb,runtime_secret_history=$4::jsonb,runtime_revision=runtime_revision+1,required=required OR $5 WHERE id=$1 AND user_id=$2",
                binding_id,
                user_id,
                json.dumps(self._environment_encode(runtime)),
                json.dumps(prior),
                bool(runtime),
            )
        return {"runtime_revision": expected_revision + 1}

    async def request_skip_environment_start(
        self, user_id: str, binding_id: str, job_id: str
    ) -> None:
        """Arm startup recovery for the next turn of this owned chat."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT j.state,j.thread_id FROM agent_jobs j JOIN agent_threads t ON t.id=j.thread_id WHERE j.id=$1 AND t.user_id=$2 AND t.environment_binding_id=$3 FOR UPDATE OF t",
                job_id,
                user_id,
                binding_id,
            )
            if row is None:
                raise EnvironmentNotFound("Workspace not found")
            if row["state"] not in ("succeeded", "failed", "cancelled"):
                raise EnvironmentConflict("Wait until this turn finishes")
            later = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_jobs WHERE thread_id=$1 AND created_at>(SELECT created_at FROM agent_jobs WHERE id=$2))",
                row["thread_id"],
                job_id,
            )
            if later:
                raise EnvironmentConflict("Open the latest turn before skipping startup")
            await conn.execute(
                "UPDATE agent_environment_bindings SET skip_start_after_job_id=$3,skip_start_job_id=NULL WHERE id=$1 AND user_id=$2",
                binding_id,
                user_id,
                job_id,
            )

    async def environment_for_claim(
        self, user_id: str, binding_id: str, job_id: str
    ) -> BoundEnvironment:
        """Consume explicit startup recovery for one turn, including its retries."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM agent_environment_bindings WHERE id=$1 AND user_id=$2 FOR UPDATE",
                binding_id,
                user_id,
            )
            if row is None:
                raise EnvironmentNotFound("Binding not found")
            await self._require_team_binding_access(conn, row)
            skip = row["skip_start_job_id"] == job_id
            if row["skip_start_after_job_id"] and row["skip_start_after_job_id"] != job_id:
                matches = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM agent_jobs j JOIN agent_jobs source ON source.thread_id=j.thread_id JOIN agent_threads t ON t.id=j.thread_id WHERE j.id=$1 AND source.id=$2 AND j.turn_source='human' AND j.created_at>source.created_at AND t.user_id=$3 AND t.environment_binding_id=$4)",
                    job_id,
                    row["skip_start_after_job_id"],
                    user_id,
                    binding_id,
                )
                if matches:
                    await conn.execute(
                        "UPDATE agent_environment_bindings SET skip_start_job_id=$2,skip_start_after_job_id=NULL WHERE id=$1",
                        binding_id,
                        job_id,
                    )
                    skip = True
        return (await self.get_environment_binding(user_id, binding_id)).model_copy(
            update={"skip_start": skip}
        )

    async def _environment_redactor(self, user_id: str, binding_id: str) -> SecretRedactor:
        from cloud_agent.settings import setting_env
        from cloud_agent.source_control import ENV_ENCRYPTION_KEY

        epoch = hashlib.sha256((setting_env(ENV_ENCRYPTION_KEY, "") or "").encode()).hexdigest()
        async with self._pool.acquire() as conn:
            revision = await conn.fetchval(
                "SELECT runtime_revision FROM agent_environment_bindings WHERE id=$1 AND user_id=$2",
                binding_id,
                user_id,
            )
        key = (user_id, binding_id, epoch + ":" + str(revision))
        cached = self._redactors.get(key)
        if cached is not None and cached[0] > time.monotonic():
            self._redactors.move_to_end(key)
            return cached[1]
        bound = await self.get_environment_binding(user_id, binding_id, for_masking=True)
        redactor = SecretRedactor(bound.secrets())
        self._redactors[key] = (time.monotonic() + 60, redactor)
        self._redactors.move_to_end(key)
        while len(self._redactors) > 256:
            self._redactors.popitem(last=False)
        return redactor

    async def redact_environment_steps(
        self, user_id: str, binding_id: str, steps: builtins.list[dict[str, Any]]
    ) -> builtins.list[dict[str, Any]]:
        """Mask host output with bounded, key-aware immutable-binding material."""
        return (await self._environment_redactor(user_id, binding_id)).payload(steps)

    async def get_job_environment(self, job_id: str) -> BoundEnvironment | None:
        """Resolve immutable owned configuration for trusted publication paths."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT t.user_id,t.environment_binding_id FROM agent_threads t JOIN agent_jobs j ON j.thread_id=t.id WHERE j.id=$1",
                job_id,
            )
        return (
            await self.get_environment_binding(
                row["user_id"], row["environment_binding_id"], for_masking=True
            )
            if row and row["environment_binding_id"]
            else None
        )

    async def workspace_environment_view(self, user_id: str, thread_id: str) -> dict[str, Any]:
        """Expose fixed versions and masked variables for one owned workspace."""
        async with self._pool.acquire() as conn:
            thread = await conn.fetchrow(
                "SELECT environment_binding_id,setup_script FROM agent_threads WHERE id=$1 AND user_id=$2",
                thread_id,
                user_id,
            )
            if thread is None:
                raise EnvironmentNotFound("Thread not found")
            binding_id = thread["environment_binding_id"]
            steps = await conn.fetchval(
                "SELECT p.steps FROM agent_environment_preparation p JOIN agent_thread_sandbox_memberships m ON m.sandbox_id=p.sandbox_id WHERE m.thread_id=$1",
                thread_id,
            )
        if not binding_id:
            return {
                "legacy": True,
                "binding_id": None,
                "variables": [],
                "setup_script": thread["setup_script"],
                "start_script": "",
                "actions": [],
                "action_runs": [],
                "steps": _json(steps) or [],
            }
        bound = await self.get_environment_binding(user_id, binding_id, include_secrets=False)
        view = bound.model_dump(
            mode="json", exclude={"variables", "runtime_variables", "previous_secrets"}
        )
        # Each saved command's latest run, so a panel opened again shows what
        # is still running and offers Stop rather than a second Run. Without
        # output: the panel reads that from the run itself, and this view is
        # polled.
        async with self._pool.acquire() as conn:
            action_runs = await conn.fetch(
                "SELECT DISTINCT ON (action_id) id,action_id,state,terminal_id,exit_code "
                "FROM agent_environment_action_runs WHERE user_id=$1 AND thread_id=$2 "
                "AND binding_id=$3 ORDER BY action_id, created_at DESC, id DESC",
                user_id,
                thread_id,
                binding_id,
            )
        current = None
        if bound.environment_id:
            # Read by the binding, which is the owner's: a team environment
            # stays named here after its owner leaves the team.
            async with self._pool.acquire() as conn:
                found = await conn.fetchrow(
                    "SELECT e.*, t.name AS team_name FROM agent_environments e "
                    "LEFT JOIN agent_teams t ON t.id = e.team_id WHERE e.id = $1",
                    bound.environment_id,
                )
                current = dict(found) if found else None
        async with self._pool.acquire() as conn:
            global_revision = (
                await conn.fetchval(
                    "SELECT revision FROM agent_environments WHERE user_id=$1 AND repo IS NULL",
                    user_id,
                )
                or 0
            )
        return {
            **view,
            "legacy": False,
            "required": bound.required,
            "environment_name": current["name"] if current else None,
            "team": (
                {"id": current["team_id"], "name": current["team_name"] or ""}
                if current and current["team_id"]
                else None
            ),
            "environment_archived": bool(current and current["archived_at"] is not None),
            "has_new_version": bool(
                current and current["archived_at"] is None and current["revision"] != bound.revision
            )
            or global_revision != bound.global_revision,
            "runtime_variables": [
                {
                    "name": v.name,
                    "secret": v.secret,
                    "phase": v.phase,
                    "source": v.source,
                    "value": None if v.secret else v.value,
                }
                for v in bound.runtime_variables
            ]
            if bound.runtime_variables is not None
            else None,
            "variables": [
                {
                    "name": v.name,
                    "secret": v.secret,
                    "phase": v.phase,
                    "source": v.source,
                    "value": None if v.secret else v.value,
                    "is_set": True,
                }
                for v in bound.variables
            ],
            "action_runs": [{**dict(run), "output": ""} for run in action_runs],
            "steps": _json(steps) or [],
        }

    async def find_environment_run(
        self, user_id: str, key: str, selection: dict[str, Any], ref: str | None
    ) -> dict[str, Any] | None:
        """Find the original logical request before any GitHub branch lookup."""
        fingerprint = hashlib.sha256(
            json.dumps([selection, ref], sort_keys=True).encode()
        ).hexdigest()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_environment_runs WHERE user_id=$1 AND idempotency_key=$2",
                user_id,
                key,
            )
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise EnvironmentConflict("Idempotency key already used for another test")
        return self._environment_run_view(row)

    async def fail_environment_preflight(
        self,
        run_id: str,
        host: str,
        generation: int,
        detail: str = "Environment credentials or repository access are unavailable",
    ) -> bool:
        """Release a failed claim that has never been handed to a host for execution."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE agent_environment_runs SET state='failed',cleanup_complete=TRUE,detail=$4,"
                "finished_at=NOW(),updated_at=NOW() WHERE id=$1 AND host=$2 AND lease_generation=$3 AND state='running'",
                run_id,
                host,
                generation,
                detail,
            )
        return result == "UPDATE 1"

    async def create_environment_run(
        self,
        user_id: str,
        *,
        repo: str,
        base_sha: str,
        selection: dict[str, Any],
        key: str | None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        """Queue one isolated test per owner with a frozen input snapshot."""
        fingerprint = hashlib.sha256(
            json.dumps([selection, ref], sort_keys=True).encode()
        ).hexdigest()
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await self._environment_lock(conn, user_id)
                if key:
                    old = await conn.fetchrow(
                        "SELECT * FROM agent_environment_runs WHERE user_id=$1 AND idempotency_key=$2",
                        user_id,
                        key,
                    )
                    if old:
                        if old["fingerprint"] != fingerprint:
                            raise EnvironmentConflict(
                                "Idempotency key already used for another test"
                            )
                        return self._environment_run_view(old)
                if await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM agent_environment_runs WHERE user_id=$1 AND purpose='test' AND state IN ('queued','running'))",
                    user_id,
                ):
                    raise EnvironmentBusy("An environment test is already queued or running")
                binding_id, _ = await self._bind_environment(conn, user_id, repo, selection)
                row = await conn.fetchrow(
                    "INSERT INTO agent_environment_runs(id,user_id,repo,base_sha,binding_id,idempotency_key,fingerprint,execution_profile) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *",
                    _id(),
                    user_id,
                    repo,
                    base_sha,
                    binding_id,
                    key,
                    fingerprint,
                    default_execution_profile().value,
                )
                return self._environment_run_view(row)
        except asyncpg.UniqueViolationError:
            raise EnvironmentBusy("An environment test is already queued or running") from None

    @staticmethod
    def _environment_run_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            k: (_json(v) if k == "steps" else v)
            for k, v in dict(row).items()
            if k not in {"fingerprint", "idempotency_key"}
        }

    async def bind_current_environment(self, user_id: str, environment_id: str) -> tuple[str, str]:
        """Freeze an environment's current configuration, as a task would, for a rebuild."""
        async with self._pool.acquire() as conn, conn.transaction():
            env = await self._environment_row(conn, user_id, environment_id)
            if env["repo"] is None:
                raise EnvironmentNotFound("Global variables have no installation")
            binding_id, _ = await self._bind_environment(
                conn,
                user_id,
                env["repo"],
                {"environment_id": environment_id, "environment_id_provided": True},
            )
            return binding_id, env["repo"]

    async def get_environment_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        """Read owned test state and masked output."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_environment_runs WHERE id=$1 AND user_id=$2", run_id, user_id
            )
            if row is None:
                raise EnvironmentNotFound("Environment test not found")
            return self._environment_run_view(row)

    async def cancel_environment_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        """Cancel immediately and fence any late execution result."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_environment_runs SET cancel_requested=TRUE,cleanup_complete=(host IS NULL),state='cancelled',finished_at=NOW(),updated_at=NOW() WHERE id=$1 AND user_id=$2 AND state IN ('queued','running')",
                run_id,
                user_id,
            )
        return await self.get_environment_run(user_id, run_id)

    async def list_environment_orphans(self, host: str) -> builtins.list[dict[str, Any]]:
        """Read only this enrolled host's tests already fenced by the reaper."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_environment_runs WHERE host=$1 AND NOT cleanup_complete AND state IN ('failed','cancelled')",
                host,
            )
            return [self._environment_run_view(row) for row in rows]

    async def maintain_environments(self) -> None:
        """Expire execution leases and collect only unreferenced archived values."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_environment_runs SET state='failed',detail='Environment test host lease expired',finished_at=NOW(),updated_at=NOW() WHERE state='running' AND (lease_expires_at<NOW() OR started_at<NOW()-INTERVAL '30 minutes')"
            )
            if time.monotonic() < self._next_gc:
                return
            self._next_gc = time.monotonic() + 3600
            owners = await conn.fetch(
                "SELECT DISTINCT user_id FROM agent_environments WHERE archived_at<NOW()-INTERVAL '30 days'"
            )
        for owner in owners:
            async with self._pool.acquire() as conn, conn.transaction():
                await self._environment_lock(conn, owner["user_id"])
                await conn.execute(
                    """DELETE FROM agent_environment_bindings b WHERE b.user_id=$1 AND b.created_at<NOW()-INTERVAL '30 days'
                    AND NOT EXISTS(SELECT 1 FROM agent_threads t WHERE t.environment_binding_id=b.id)
                    AND NOT EXISTS(SELECT 1 FROM agent_environment_runs r WHERE r.binding_id=b.id)
                    AND NOT EXISTS(SELECT 1 FROM agent_environment_action_runs a WHERE a.binding_id=b.id)
                    AND NOT EXISTS(SELECT 1 FROM agent_environment_preparation p WHERE p.binding_id=b.id)""",
                    owner["user_id"],
                )
                await conn.execute(
                    """DELETE FROM agent_environment_versions v USING agent_environments e WHERE e.id=v.environment_id AND e.user_id=$1 AND e.archived_at<NOW()-INTERVAL '30 days'
                    AND NOT EXISTS(SELECT 1 FROM agent_environment_bindings b WHERE b.environment_version_id=v.id OR b.global_version_id=v.id)""",
                    owner["user_id"],
                )

    async def claim_environment_run(
        self,
        host: str,
        credential_hash: str,
        attestation: dict[str, Any],
        *,
        snapshot_capable: bool = False,
    ) -> dict[str, Any] | None:
        """Lease a test, or a prebuild to a snapshot-capable host, within slot capacity."""
        if not shared_sandbox_claim_attested(
            attestation, expected_deployment_sha=os.environ.get("AGENT_DEPLOYMENT_SHA")
        ):
            return None
        kata = (
            attestation.get("sandbox_runtime") == "io.containerd.kata.v2"
            and bool(attestation.get("guest_kernel"))
            and attestation.get("guest_kernel") != attestation.get("host_kernel")
        )
        async with self._pool.acquire() as conn, conn.transaction():
            policy = await conn.fetchrow(
                "SELECT active_host FROM agent_runner_policy WHERE id FOR SHARE"
            )
            await lock_compute_topology(conn)
            resource_new, resource_claims = await host_compute_admission(conn, host)
            if (
                not resource_new
                or not resource_claims
                or (policy and policy["active_host"] not in (None, host))
            ):
                return None
            host_row = await conn.fetchrow(
                "SELECT * FROM agent_runner_hosts WHERE host=$1 FOR UPDATE", host
            )
            if (
                host_row is None
                or host_row["status"] != "online"
                or host_row["credential_hash"] != credential_hash
            ):
                return None
            if await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_environment_runs WHERE host=$1 AND NOT cleanup_complete "
                "AND (lease_expires_at<=NOW() OR started_at<=NOW()-INTERVAL '30 minutes'))",
                host,
            ):
                return None
            tests = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_environment_runs WHERE host=$1 AND NOT cleanup_complete AND lease_expires_at>NOW() AND started_at>NOW()-INTERVAL '30 minutes'",
                    host,
                )
            )
            used = int(host_row["slots_used"]) + tests
            if any(
                host_row.get(k) is not None and used >= int(host_row[k])
                for k in ("slots_total", "slots_desired")
            ):
                return None
            # A prebuild whose snapshot was deleted since would install for nothing.
            row = await conn.fetchrow(
                "SELECT * FROM agent_environment_runs WHERE state='queued' AND (execution_profile='trusted-v1' OR $1) "
                "AND (purpose='test' OR ($2 AND EXISTS (SELECT 1 FROM agent_environment_snapshots s "
                "WHERE s.id=agent_environment_runs.snapshot_id AND s.state IN ('queued','building')))) "
                "ORDER BY purpose='prebuild', created_at FOR UPDATE SKIP LOCKED LIMIT 1",
                kata,
                snapshot_capable,
            )
            if row is None:
                return None
            claimed = await conn.fetchrow(
                "UPDATE agent_environment_runs SET state='running',host=$2,lease_generation=lease_generation+1,lease_expires_at=NOW()+INTERVAL '120 seconds',started_at=NOW(),updated_at=NOW() WHERE id=$1 RETURNING *",
                row["id"],
                host,
            )
            return self._environment_run_view(claimed)

    async def update_environment_run(
        self, run_id: str, host: str, generation: int, update: dict[str, Any]
    ) -> bool:
        """Accept bounded masked output only under the live execution lease."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_environment_runs WHERE id=$1", run_id)
        if row is None or row["host"] != host or row["lease_generation"] != generation:
            return False
        if (
            update.get("cleanup_complete")
            and row["state"] in {"failed", "cancelled"}
            and not update.get("steps")
        ):
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE agent_environment_runs SET cleanup_complete=TRUE,updated_at=NOW() "
                    "WHERE id=$1 AND host=$2 AND lease_generation=$3 AND state IN ('failed','cancelled') AND NOT cleanup_complete",
                    run_id,
                    host,
                    generation,
                )
            return result == "UPDATE 1"
        redactor = await self._environment_redactor(row["user_id"], row["binding_id"])
        steps = redactor.payload(update.get("steps", _json(row["steps"])))
        for step in steps:
            output = str(step.get("output") or "")
            if len(output) > 10000:
                step["output"] = "[output truncated]\n" + output[-10000:]
        if len(json.dumps(steps).encode()) > 262144:
            raise EnvironmentInputError("Environment output exceeds limit")
        state = (
            row["state"]
            if row["state"] in {"failed", "cancelled"}
            else update.get("state", "running")
        )
        if state not in {"running", "succeeded", "failed", "cancelled"}:
            raise EnvironmentInputError("Invalid environment run state")
        cleanup = bool(update.get("cleanup_complete", False))
        if state != "running" and not cleanup:
            state = "running"
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE agent_environment_runs SET steps=$4::jsonb,state=$5,detail=$6,image_digest=COALESCE($7,image_digest),"
                "lease_expires_at=NOW()+INTERVAL '120 seconds',updated_at=NOW(),cleanup_complete=$8,"
                "finished_at=CASE WHEN $5='running' THEN NULL ELSE NOW() END "
                "WHERE id=$1 AND host=$2 AND lease_generation=$3 AND NOT cleanup_complete "
                "AND (($8 AND $5 IN ('failed','cancelled')) OR (state='running' AND NOT cancel_requested AND lease_expires_at>NOW() AND started_at>NOW()-INTERVAL '30 minutes'))",
                run_id,
                host,
                generation,
                json.dumps(steps),
                state,
                redactor.text(str(update.get("detail") or ""))[-1000:],
                update.get("image_digest"),
                cleanup,
            )
            return result == "UPDATE 1"

    async def create_environment_action_run(
        self, user_id: str, thread_id: str, binding_id: str, action_id: str, key: str
    ) -> dict[str, Any]:
        """Reserve one idempotent operation before requesting a terminal."""
        bound = await self.get_environment_binding(user_id, binding_id)
        if not any(a.id == action_id for a in bound.actions):
            raise EnvironmentNotFound("Action not found in this workspace version")
        async with self._pool.acquire() as conn, conn.transaction():
            await self._environment_lock(conn, user_id)
            row = await conn.fetchrow(
                "SELECT * FROM agent_environment_action_runs WHERE user_id=$1 AND thread_id=$2 AND idempotency_key=$3",
                user_id,
                thread_id,
                key,
            )
            if row:
                if row["action_id"] != action_id or row["binding_id"] != binding_id:
                    raise EnvironmentConflict("Idempotency key already used for another action")
                return dict(row)
            if not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM agent_threads WHERE id=$1 AND user_id=$2 AND environment_binding_id=$3)",
                thread_id,
                user_id,
                binding_id,
            ):
                raise EnvironmentNotFound("Workspace binding not found")
            return dict(
                await conn.fetchrow(
                    "INSERT INTO agent_environment_action_runs(id,user_id,thread_id,binding_id,action_id,idempotency_key) VALUES($1,$2,$3,$4,$5,$6) RETURNING *",
                    _id(),
                    user_id,
                    thread_id,
                    binding_id,
                    action_id,
                    key,
                )
            )

    async def get_environment_action_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        """Read a manual operation owned by the requesting user."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT a.*, (SELECT j.id FROM agent_jobs j WHERE j.thread_id=a.thread_id AND j.user_id=a.user_id ORDER BY j.turn_no DESC LIMIT 1) AS job_id FROM agent_environment_action_runs a WHERE a.id=$1 AND a.user_id=$2",
                run_id,
                user_id,
            )
            if row is None:
                raise EnvironmentNotFound("Action run not found")
            return dict(row)

    async def update_environment_action_run(
        self, user_id: str, run_id: str, update: dict[str, Any]
    ) -> dict[str, Any]:
        """Save only masked terminal summaries and execution state."""
        row = await self.get_environment_action_run(user_id, run_id)
        redactor = await self._environment_redactor(user_id, row["binding_id"])
        output = redactor.text(str(update.get("output", row["output"])))
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_environment_action_runs SET terminal_id=$3,state=$4,exit_code=$5,output=$6,updated_at=NOW() WHERE id=$1 AND user_id=$2 AND state NOT IN ('succeeded','failed','closed','cancelled','stopped','unknown')",
                run_id,
                user_id,
                update.get("terminal_id", row["terminal_id"]),
                update.get("state", row["state"]),
                update.get("exit_code", row["exit_code"]),
                output[-65536:],
            )
        return await self.get_environment_action_run(user_id, run_id)

    async def record_environment_preparation(
        self, user_id: str, thread_id: str, receipt: dict[str, Any]
    ) -> None:
        """Persist masked host facts only for the matching owned group binding."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT m.sandbox_id,t.environment_binding_id FROM agent_threads t JOIN agent_thread_sandbox_memberships m ON m.thread_id=t.id WHERE t.id=$1 AND t.user_id=$2",
                thread_id,
                user_id,
            )
        if (
            row is None
            or row["environment_binding_id"] != receipt.get("binding_id")
            or not receipt.get("container_id")
            or not receipt.get("boot_id")
        ):
            return
        redactor = await self._environment_redactor(user_id, row["environment_binding_id"])
        await self.save_environment_preparation(
            row["sandbox_id"],
            row["environment_binding_id"],
            receipt["container_id"],
            receipt["boot_id"],
            redactor.payload(receipt.get("steps", [])),
        )

    async def record_worker_environment_preparation(
        self, job_id: str, attempt_id: int, generation: int, receipt: dict[str, Any]
    ) -> bool:
        """Persist one lifecycle receipt only while its exact attempt still owns the lease."""
        async with self._pool.acquire() as conn:
            job = await conn.fetchrow(
                "SELECT t.user_id,t.environment_binding_id FROM agent_jobs j JOIN agent_threads t ON t.id=j.thread_id WHERE j.id=$1",
                job_id,
            )
        if job is None or job["environment_binding_id"] != receipt.get("binding_id"):
            return False
        if not receipt.get("container_id") or not receipt.get("boot_id"):
            return False
        redactor = await self._environment_redactor(job["user_id"], job["environment_binding_id"])
        steps = redactor.payload(receipt.get("steps", []))
        async with self._pool.acquire() as conn, conn.transaction():
            sandbox_id = await conn.fetchval(
                "SELECT m.sandbox_id FROM agent_jobs j JOIN agent_attempts a ON a.job_id=j.id JOIN agent_thread_sandbox_memberships m ON m.thread_id=j.thread_id WHERE j.id=$1 AND a.id=$2 AND a.lease_generation=$3 AND j.current_attempt_id=a.id AND j.state='running' AND a.lease_expires_at>clock_timestamp() FOR UPDATE OF j,a",
                job_id,
                attempt_id,
                generation,
            )
            if sandbox_id is None:
                return False
            await self._write_preparation(
                conn,
                sandbox_id,
                job["environment_binding_id"],
                receipt["container_id"],
                receipt["boot_id"],
                steps,
            )
            return True

    async def save_environment_preparation(
        self,
        sandbox_id: str,
        binding_id: str,
        container_id: str,
        boot_id: str,
        steps: builtins.list[dict[str, Any]],
    ) -> None:
        """Keep preparation facts attributable to one fixed container binding."""
        async with self._pool.acquire() as conn:
            await self._write_preparation(
                conn, sandbox_id, binding_id, container_id, boot_id, steps
            )

    @staticmethod
    async def _write_preparation(
        conn: Any,
        sandbox_id: str,
        binding_id: str,
        container_id: str,
        boot_id: str,
        steps: builtins.list[dict[str, Any]],
    ) -> None:
        """Write the same bounded summary for owner and fenced-worker mutations."""
        await conn.execute(
            "INSERT INTO agent_environment_preparation(sandbox_id,binding_id,container_id,boot_id,setup_done,start_done,steps) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT(sandbox_id) DO UPDATE SET container_id=$3,boot_id=$4,setup_done=$5,start_done=$6,steps=$7::jsonb,updated_at=NOW() WHERE agent_environment_preparation.binding_id=$2",
            sandbox_id,
            binding_id,
            container_id,
            boot_id,
            any(s.get("phase") == "setup" and s.get("state") == "succeeded" for s in steps),
            any(s.get("phase") == "start" and s.get("state") == "succeeded" for s in steps),
            json.dumps(steps),
        )
