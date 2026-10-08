"""Wake hosts that are waiting for work as soon as there may be some.

A host that finds nothing to claim parks a request on ``/v1/agent/worker/wait``
instead of sleeping. This process answers it the moment Postgres notifies
``agent_work_available``, which triggers on ``agent_jobs`` do when a job is
queued, requeued or ends (migration ``0029``), and the host claims again.

The token a host passes back is this process's own, so it assumes what the
lifespan already does: one main API process. Its reaper and publisher run once
per process, and a second one would renew every grant twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

CHANNEL = "agent_work_available"
#: How often the idle listening connection is checked. One that died without
#: closing would otherwise leave every wait to run out its timeout unnoticed.
_PING_S = 30.0
_PING_TIMEOUT_S = 10.0
_RECONNECT_S = 5.0


class WorkSignal:
    """A token that changes whenever claimable work may have appeared."""

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


async def listen_for_work(dsn: str, signal: WorkSignal) -> None:
    """Keep one connection listening on ``agent_work_available`` until cancelled.

    A dedicated connection, because a pooled one is reset when released and
    would stop listening. Each connect and each loss also wakes every waiting
    host once: a notification sent while nothing listened is gone, so they
    claim to find out, which costs a query where missing it costs a wait.
    """
    import asyncpg

    healthy = True
    while True:
        connection: asyncpg.Connection | None = None
        try:
            connection = await asyncpg.connect(dsn)
            lost = asyncio.Event()
            # Bound now: a late callback from an old connection must not end
            # the next one.
            connection.add_termination_listener(lambda _connection, lost=lost: lost.set())

            def _notified(*_args: Any) -> None:
                signal.notify()

            await connection.add_listener(CHANNEL, _notified)
            signal.notify()
            if not healthy:
                healthy = True
                logger.info(
                    "agent_work_listener_recovered",
                    extra={"event": "agent_work_listener_recovered"},
                )
            while not lost.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(lost.wait(), _PING_S)
                if not lost.is_set():
                    await asyncio.wait_for(connection.fetchval("SELECT 1"), _PING_TIMEOUT_S)
            raise ConnectionError("the listening connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Once per outage: hosts keep claiming on their own timeout meanwhile,
            # so a report every reconnect would say nothing new.
            if healthy:
                healthy = False
                logger.warning(
                    "agent_work_listener_failed",
                    extra={
                        "event": "agent_work_listener_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                    },
                )
        finally:
            if connection is not None and not connection.is_closed():
                # Not close(): that waits on the server, and the server may be
                # what stopped answering.
                connection.terminate()
        signal.notify()
        await asyncio.sleep(_RECONNECT_S)
