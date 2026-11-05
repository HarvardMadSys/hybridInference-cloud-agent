"""Transactional owner-scoped environment history and execution snapshots.

The repository is composed of one mixin per concern: reading the catalogue,
editing environments, bindings and the environment runs they start. Four
methods stay in the class itself, with the two functions beside it:
``retire_changed_snapshots`` and ``bind_environment`` name the class, and
publishing a version and binding an environment call them.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import asyncpg

from cloud_agent.domain.execution_profiles import default_execution_profile
from cloud_agent.environments.environments import (
    EnvironmentBusy,
    EnvironmentConflict,
    EnvironmentEncryptionError,
    EnvironmentNotFound,
)
from cloud_agent.storage.repositories.environments._common import _id, _json
from cloud_agent.storage.repositories.environments.bindings import BindingsMixin
from cloud_agent.storage.repositories.environments.catalogue import CatalogueMixin
from cloud_agent.storage.repositories.environments.edits import EditsMixin
from cloud_agent.storage.repositories.environments.installations import (
    lock_environment_snapshots,
    lock_team_environment_snapshots,
    optional_setup_fingerprint,
    runtime_only,
    setup_fingerprint,
)
from cloud_agent.storage.repositories.environments.runs import RunsMixin
from cloud_agent_shared.environment import EnvironmentVariable, merge_variables

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterable

    from cloud_agent_shared.secret_redaction import SecretRedactor


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
    Maintenance also runs it for each owner in turn, for a snapshot that went
    stale with neither to notice.

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


class PostgresEnvironmentRepository(CatalogueMixin, EditsMixin, RunsMixin, BindingsMixin):
    """Serialize configuration changes and bindings per owner over one pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Reuse the assembled persistence pool."""
        self._pool = pool
        self._next_gc = 0.0
        self._redactors: OrderedDict[tuple[str, str, str], tuple[float, SecretRedactor]] = (
            OrderedDict()
        )
        # Versions are immutable, so a pair's fingerprint never changes.
        self._fingerprints: OrderedDict[tuple[str, str | None], str] = OrderedDict()

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
                binding_id, _ = await bind_environment(conn, user_id, repo, selection)
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

    async def bind_current_environment(self, user_id: str, environment_id: str) -> tuple[str, str]:
        """Freeze an environment's current configuration, as a task would, for a rebuild."""
        async with self._pool.acquire() as conn, conn.transaction():
            env = await self._environment_row(conn, user_id, environment_id)
            if env["repo"] is None:
                raise EnvironmentNotFound("Global variables have no installation")
            binding_id, _ = await bind_environment(
                conn,
                user_id,
                env["repo"],
                {"environment_id": environment_id, "environment_id_provided": True},
            )
            return binding_id, env["repo"]


async def bind_environment(
    conn: Any, user_id: str, repo: str | None, selection: dict[str, Any]
) -> tuple[str, str | None]:
    """Freeze an environment snapshot in the caller's work-item transaction.

    Temporary workspaces bind globals only. Team bindings use member globals at
    runtime and record the access generation checked by later turns.
    """
    ops = PostgresEnvironmentRepository
    await ops._environment_lock(conn, user_id)
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
                await ops._environment_row(conn, user_id, environment_id)
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
    env = await ops._environment_row(conn, user_id, environment_id) if environment_id else None
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
    version = await ops._environment_version(conn, env["id"], env["revision"]) if env else None
    global_version = (
        await ops._environment_version(conn, global_row["id"], global_row["revision"])
        if global_row
        else None
    )
    team_id = env["team_id"] if env else None
    personal = ops._environment_decode(
        await ops._environment_stored_variables(
            conn, global_version["id"] if global_version else None
        )
    )
    merged = merge_variables(
        runtime_only(personal) if team_id else personal,
        ops._environment_decode(
            await ops._environment_stored_variables(conn, version["id"] if version else None)
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
