"""The workspace and terminal fences of a chat's sandbox.

Workspace readiness and the start fence a workspace request takes, the
terminal control route and readiness fence, terminal resume recovery, and the
host's terminal count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.storage._sql.fences import (
    LockMode,
    attempt_lease_is_live,
    leased_attempt_sql,
    lock_running_attempt,
)
from cloud_agent.storage._sql.sandbox import (
    WORKSPACE_READY_SQL,
    fence_sandbox_start,
    read_terminal_control_route,
)
from cloud_agent.storage._sql.sandbox_transitions import RECORD_TERMINAL_OBSERVATION_GROUP_SQL
from cloud_agent.storage._sql.terminal_replay import (
    page as terminal_replay_page,
    pending_ids as terminal_replay_ids,
)
from cloud_agent.storage.repositories.sandbox._common import SandboxRepositoryBase

if TYPE_CHECKING:
    from datetime import datetime

    from cloud_agent.ports.sandbox import TerminalControlRoute


class WorkspaceMixin(SandboxRepositoryBase):
    """Fence workspace and terminal access to a chat's sandbox."""

    async def thread_workspace_ready(self, *, thread_id: str) -> bool:
        """Return whether a retained thread permits owner workspace mutation.

        The sandbox manager's in-memory suspension is the immediate barrier. This
        durable copy closes sandbox-manager-restart and old-turn-URL gaps: an active
        attempt must have published ``workspace_ready``.
        """
        async with self._pool.acquire() as conn:
            ready = await conn.fetchval(
                f"""
                SELECT {WORKSPACE_READY_SQL}
                FROM agent_thread_sandbox_memberships m
                JOIN agent_sandboxes g ON g.id = m.sandbox_id
                WHERE m.thread_id = $1
                """,
                thread_id,
            )
        return bool(ready)

    async def terminal_control_route(
        self, *, job_id: str, user_id: str
    ) -> TerminalControlRoute | None:
        """Read what an attached terminal's control call needs, in one statement."""
        async with self._pool.acquire() as conn:
            return await read_terminal_control_route(conn, job_id=job_id, user_id=user_id)

    async def fence_terminal_workspace(
        self,
        *,
        attempt_id: int,
        lease_generation: int,
    ) -> bool:
        """Revoke readiness only for the still-live fenced attempt."""
        async with self._pool.acquire() as conn, conn.transaction():
            if not await lock_running_attempt(
                conn, attempt_id, lease_generation, LockMode.UPDATE
            ) or not await attempt_lease_is_live(conn, attempt_id):
                return False
            result = await conn.execute(
                f"""
                UPDATE agent_attempts
                SET terminal_ready = FALSE
                WHERE {leased_attempt_sql("$1", "$2", alias="")}
                """,
                attempt_id,
                lease_generation,
            )
        return result == "UPDATE 1"

    async def list_terminal_resumes_pending(self, *, limit: int = 100) -> list[str]:
        """List settled workspaces whose sandbox manager resume still needs confirmation."""
        async with self._pool.acquire() as conn:
            return await terminal_replay_ids(conn, limit=limit)

    async def terminal_resume_page(
        self, *, after: tuple[datetime, str] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Page settled replay routing without job prompts, details or metadata."""
        async with self._pool.acquire() as conn:
            return await terminal_replay_page(conn, after=after, limit=limit)

    async def mark_terminal_resume_complete(self, *, job_id: str) -> bool:
        """Clear durable recovery state after the sandbox manager confirms a resume."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agent_jobs
                SET terminal_resume_pending = FALSE
                WHERE id = $1
                  AND terminal_resume_pending = TRUE
                  AND state = ANY($2::text[])
                """,
                job_id,
                list(TERMINAL_STATES),
            )
        return result == "UPDATE 1"

    async def fence_workspace_start(self, *, thread_id: str) -> tuple[str, int | None] | None:
        """Count a start a workspace request may cause; return state and fence.

        Opening a file, a terminal or a Git view of a stopped group starts its
        container through the sandbox manager, which records nothing here. The
        fence is taken before the request is forwarded, so the checkpoint of
        the last stop stops covering the group from that moment on, and the
        request carries the new ``start_seq`` so the next one does. ``None``
        when the chat has no group, and ``host_revoked`` with no fence when
        its host has left the pool: nothing may start it there.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            sandbox_id = await conn.fetchval(
                "SELECT sandbox_id FROM agent_thread_sandbox_memberships WHERE thread_id = $1",
                thread_id,
            )
            if sandbox_id is None:
                return None
            return await fence_sandbox_start(conn, str(sandbox_id))

    async def record_terminal_observation(
        self,
        *,
        sandbox_id: str,
        host: str,
        generation: int,
        container_id: str,
        terminal_count: int,
    ) -> bool:
        """Refresh terminal activity only for the exact running container."""
        if (
            isinstance(terminal_count, bool)
            or not isinstance(terminal_count, int)
            or not 0 <= terminal_count <= 4096
        ):
            raise ValueError("Invalid terminal count")
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    RECORD_TERMINAL_OBSERVATION_GROUP_SQL,
                    sandbox_id,
                    host,
                    generation,
                    container_id,
                    terminal_count,
                )
            )
