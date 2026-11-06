"""Environment runs and action runs, their claims, updates and preparation records."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from cloud_agent.environments.environments import EnvironmentConflict, EnvironmentNotFound
from cloud_agent.storage._sql import environment_runs
from cloud_agent.storage._sql.environment_claims import claim_environment_run as claim_queued_run
from cloud_agent.storage.repositories.environments._common import _id, _json
from cloud_agent.storage.repositories.environments.bindings import BindingsMixin
from cloud_agent_shared.environment import EnvironmentInputError

if TYPE_CHECKING:
    import builtins


class RunsMixin(BindingsMixin):
    """Find, claim, update and maintain environment runs."""

    _next_gc: float

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
                f"UPDATE agent_environment_runs SET state='failed',detail='Environment test host lease expired',finished_at=NOW(),updated_at=NOW() WHERE state='running' AND {environment_runs.env_run_stale()}"
            )
            if time.monotonic() < self._next_gc:
                return
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
        # A cancelled or failed pass must retry incomplete owner cleanup on
        # the next maintenance interval.
        self._next_gc = time.monotonic() + 3600

    async def claim_environment_run(
        self,
        host: str,
        credential_hash: str,
        attestation: dict[str, Any],
        *,
        snapshot_capable: bool = False,
    ) -> dict[str, Any] | None:
        """Lease a test, or a prebuild to a snapshot-capable host, within slot capacity."""
        return await claim_queued_run(
            self._pool,
            host,
            credential_hash,
            attestation,
            snapshot_capable=snapshot_capable,
            run_view=self._environment_run_view,
        )

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
                f"AND (($8 AND $5 IN ('failed','cancelled')) OR (state='running' AND NOT cancel_requested AND {environment_runs.env_run_live()}))",
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
        async with self._pool.acquire() as conn:
            await self._write_preparation(
                conn,
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
