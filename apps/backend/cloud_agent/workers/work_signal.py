"""Wake hosts that are waiting for work as soon as there may be some.

A host that finds nothing to claim parks a request on ``/v1/agent/worker/wait``
instead of sleeping. This process answers it the moment Postgres notifies
``agent_work_available``, which triggers on ``agent_jobs`` do when a job is
queued, requeued or ends (migration ``0029``), and the host claims again. The
connection that hears it is the process's shared listener (``notifications``).

The token a host passes back is this process's own, so it assumes what the
lifespan already does: one main API process. Its reaper and publisher run once
per process, and a second one would renew every grant twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets

CHANNEL = "agent_work_available"


class WorkSignal:
    """A token that changes whenever claimable work may have appeared.

    It is also the shared listener's receiver for ``agent_work_available``.
    """

    channel = CHANNEL

    def __init__(self) -> None:
        """Start at a token no earlier process could have handed out."""
        self._instance = secrets.token_hex(6)
        self._generation = 0
        self._changed = asyncio.Event()

    @property
    def token(self) -> str:
        """The current token; a host passes it back to wait for a newer one."""
        return f"{self._instance}:{self._generation}"

    def notify(self) -> None:
        """Advance the token and release everyone waiting on the current one."""
        self._generation += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait(self, since: str | None, timeout_s: float) -> str:
        """Return the token once it differs from ``since``, or after ``timeout_s``.

        A host passes the token it held *before* its last claim, so a change
        that claim may have missed has already advanced it and this answers at
        once. So does no token, or one from an earlier process: what changed
        since is unknown, and claiming again costs one query.
        """
        if since != self.token:
            return self.token
        changed = self._changed
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout_s)
        return self.token

    def notified(self, payload: str) -> None:
        """Work may have appeared; the notification carries nothing else."""
        _ = payload
        self.notify()

    def reset(self, *, listening: bool) -> None:
        """A notification sent while nothing listened is gone: claim to find out.

        That costs every waiting host a query, where missing it costs a wait.
        """
        _ = listening
        self.notify()
