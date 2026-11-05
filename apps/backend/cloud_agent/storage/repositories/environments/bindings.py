"""Environment bindings: what a turn runs with, and its redaction."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from cloud_agent.environments.environments import (
    EnvironmentAccessRevoked,
    EnvironmentConflict,
    EnvironmentNotFound,
)
from cloud_agent.storage.repositories.environments._common import (
    EnvironmentRepositoryBase,
    _cipher,
    _json,
)
from cloud_agent.storage.repositories.environments.installations import runtime_only
from cloud_agent_shared.environment import (
    BoundEnvironment,
    EnvironmentInputError,
    EnvironmentScripts,
    merge_variables,
)
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    import builtins
    from collections import OrderedDict
    from collections.abc import Mapping


class BindingsMixin(EnvironmentRepositoryBase):
    """Read, refresh and redact an environment binding."""

    _redactors: OrderedDict[tuple[str, str, str], tuple[float, SecretRedactor]]

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
        redaction that must keep finding them after the owner leaves; such a
        caller never passes them on.
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
        from cloud_agent.source_control.source_control import ENV_ENCRYPTION_KEY

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
        """Resolve a turn's immutable owned configuration, for masking."""
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
