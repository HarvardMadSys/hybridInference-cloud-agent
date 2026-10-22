"""Wake hosts that are waiting for work as soon as there may be some.

A host that finds nothing to claim parks a request on ``/v1/agent/worker/wait``
instead of sleeping. This process answers it the moment Postgres notifies
``agent_work_available``, which triggers on ``agent_jobs`` do when a job is
queued, requeued or ends (migration ``0029``), and the host claims again. The
connection that hears it is the process's shared listener (``notifications``).

A notification names the host when only that host can take the work: a
retained conversation's turn runs where its group lives (migration ``0092``).
Then only that host is woken, and every other host keeps waiting instead of
spending a claim on work it cannot take. An empty one wakes every host.

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
    """A token per host that changes whenever work it can claim may have appeared.

    It is also the shared listener's receiver for ``agent_work_available``.
    """

    channel = CHANNEL

    def __init__(self) -> None:
        """Start at a token no earlier process could have handed out."""
        self._instance = secrets.token_hex(6)
        #: Changes any host may be able to take.
        self._generation = 0
        #: Changes only one host can take, by host. ``None`` counts every one of
        #: them, for a wait that names no host.
        self._host_generation: dict[str | None, int] = {}
        self._changed: dict[str | None, asyncio.Event] = {}

    def token_for(self, host: str | None) -> str:
        """The token ``host`` passes back to wait for a newer one."""
        return f"{self._instance}:{self._generation}:{self._host_generation.get(host, 0)}"

    @property
    def token(self) -> str:
        """The current token of a wait that names no host."""
        return self.token_for(None)

    def notify(self, host: str | None = None) -> None:
        """Advance the tokens a change concerns and release the waits on them.

        Without ``host`` the change may be any host's work, and every wait is
        released. With one it is work only that host can take: its token and the
        host-less one advance, and only those waits are released.
        """
        if host is None:
            self._generation += 1
            released = list(self._changed.values())
            self._changed.clear()
        else:
            released = []
            for key in (host, None):
                self._host_generation[key] = self._host_generation.get(key, 0) + 1
                changed = self._changed.pop(key, None)
                if changed is not None:
                    released.append(changed)
        for changed in released:
            changed.set()

    async def wait(self, since: str | None, timeout_s: float, *, host: str | None = None) -> str:
        """Return ``host``'s token once it differs from ``since``, or after ``timeout_s``.

        A host passes the token it held *before* its last claim, so a change
        that claim may have missed has already advanced it and this answers at
        once. So does no token, or one from an earlier process: what changed
        since is unknown, and claiming again costs one query. A change only
        another host can take does neither.
        """
        current = self.token_for(host)
        if since != current:
            return current
        changed = self._changed.setdefault(host, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(changed.wait(), timeout_s)
        return self.token_for(host)

    def notified(self, payload: str) -> None:
        """Work may have appeared, for the host the payload names or for any host."""
        self.notify(payload or None)

    def reset(self, *, listening: bool) -> None:
        """A notification sent while nothing listened is gone: claim to find out.

        That costs every waiting host a query, where missing it costs a wait.
        """
        _ = listening
        self.notify()
