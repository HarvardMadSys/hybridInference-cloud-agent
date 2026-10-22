"""Making an in-memory fake reachable the way the real storage layer is.

Production hands every caller the assembly: one object per capability, reached
by name (``storage.host_pool.get_host(...)``). A test fake usually implements
several capabilities in one class, because the scenario it stands in for spans
them -- and it would otherwise have to carry sixteen aliases of itself.

``AllCapabilities`` supplies those aliases. A
capability name resolves to the fake itself, and any other missing attribute
still raises ``AttributeError``, so a typo in a fake is still a failure.

The names come from ``cloud_agent.ports`` at import time rather than from a
list kept here, so a seventeenth capability cannot be reachable in production
and silently unreachable in a fake.
"""

from __future__ import annotations

import pathlib
from typing import Any

from cloud_agent.ports.sandbox import TerminalControlRoute

_PORTS = pathlib.Path(__file__).resolve().parents[2] / "apps" / "backend" / "cloud_agent" / "ports"

#: One name per port module, which is also the assembly's attribute name.
CAPABILITIES = frozenset(p.stem for p in _PORTS.glob("*.py") if p.name != "__init__.py") | {
    "admin_conversations",
    "admin_runs",
    "admin_usage",
}


class AllCapabilities:
    """Mixin: answer every capability name with the fake itself."""

    async def get_job_environment(self, job_id: str):
        """Legacy fixtures have no configured environment unless overridden."""
        return None

    async def maintain_environments(self) -> None:
        """Legacy fixtures have no archived environments or test leases to reap."""
        return None

    async def ssh_group_state(self, *, thread_id: str):
        """Legacy fixtures have no chat that ever selected an SSH server."""
        return None

    async def owns_job(self, *, job_id: str, user_id: str) -> bool:
        """Ownership as a fake's full job read answers it, as the routes once asked it."""
        job = await self.get_job(job_id)  # type: ignore[attr-defined]
        return job is not None and job["user_id"] == user_id and job.get("deleted_at") is None

    async def terminal_control_route(
        self, *, job_id: str, user_id: str
    ) -> TerminalControlRoute | None:
        """A terminal control call's one read, from the reads the route once made.

        The fake's job, readiness and host reads. A shared group that records
        no state reads as running, which is how the fake's start fence treats
        it, and the fence's ``host_revoked`` state is the group's host removed.
        """
        if not await self.owns_job(job_id=job_id, user_id=user_id):
            return None
        job = await self.get_job(job_id)  # type: ignore[attr-defined]
        sandbox = job.get("thread_sandbox")
        thread_id = job.get("thread_id")
        group = isinstance(sandbox, dict) and sandbox.get("shared_live") is True
        state = (sandbox.get("state") or "running") if group else None
        return TerminalControlRoute(
            thread_id=thread_id,
            workspace_ready=sandbox is not None
            and bool(thread_id)
            and await self.thread_workspace_ready(thread_id=str(thread_id)),  # type: ignore[attr-defined]
            terminal_resume_pending=bool(job.get("terminal_resume_pending")),
            group_state=state,
            host_revoked=state == "host_revoked",
            host=await self.host_for_job(job_id=job_id),  # type: ignore[attr-defined]
        )

    def __getattr__(self, name: str) -> Any:
        """Resolve a capability name to this object; anything else is an error."""
        if name in CAPABILITIES:
            return self
        raise AttributeError(name)


class OneFake(AllCapabilities):
    """Wrap one flat fake so that every capability name resolves to it.

    For fakes built inline -- a ``SimpleNamespace`` of ``AsyncMock``s, say --
    where subclassing is not available. Attribute lookup falls through to the
    wrapped object, so the fake's own methods stay reachable under their own
    names as well as under a capability.
    """

    def __init__(self, inner: Any) -> None:
        """Hold the fake every capability will resolve to."""
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        """A capability name is this object; anything else is the fake's own.

        Unless the wrapped fake answers for it itself. That exception is what
        lets a test model a deployment assembled *without* one capability —
        ``goals = None`` on the inner object — instead of being handed this
        wrapper back and quietly getting a fake that pretends to have it.
        """
        try:
            return getattr(self._inner, name)
        except AttributeError:
            pass
        if name in CAPABILITIES:
            return self
        raise AttributeError(name)


class EmptySkillLibrary:
    """Opt-in library for execution fakes whose scenarios do not select skills."""

    async def select_skills(
        self, *, user_id: str, skill_ids: list[int] | None
    ) -> list[dict[str, Any]]:
        """Return no defaults for these execution-only scenarios."""
        return []


class EmptyAttachments:
    """Opt-in upload store for execution fakes whose scenarios attach nothing.

    The claim path asks every turn how many uploads its conversation holds, so
    a fake that stands in for execution needs an answer even when the scenario
    is not about uploads at all.
    """

    async def count_thread_attachments(self, *, thread_id: str) -> int:
        """No conversation in these scenarios carries an upload."""
        return 0
