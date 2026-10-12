"""Wake the control plane's own workers when there may be work for them.

The snapshot coordinator, the publisher and the usage collector each looked for
work on a clock. Triggers now notify ``agent_background_work`` with the name of
the worker that has something new (migration ``0032``), and the process's shared
listener (``notifications``) hands it here. Each worker waits for its name or
for the earliest retry it scheduled itself, with a long clock as the backstop
for anything no trigger announces; while nothing listens, it looks on its old
short clock instead.
"""

from __future__ import annotations

import asyncio

CHANNEL = "agent_background_work"
SNAPSHOT = "snapshot"
PUBLISH = "publish"
LEDGER = "ledger"
KINDS = (SNAPSHOT, PUBLISH, LEDGER)


class BackgroundWake:
    """One wake-up per worker, set by a notification or a listener reset."""

    channel = CHANNEL

    def __init__(self) -> None:
        """Start with nothing pending and no connection listening yet."""
        self._woken = {kind: asyncio.Event() for kind in KINDS}
        #: Whether a connection is listening now. Until it is, and after it is
        #: lost, nothing announces new work, so workers look on their old clocks.
        self.listening = False

    def notified(self, payload: str) -> None:
        """Work may be waiting for the worker ``payload`` names."""
        woken = self._woken.get(payload)
        if woken is not None:
            woken.set()

    def reset(self, *, listening: bool) -> None:
        """A notification sent while nothing listened is gone: every worker looks."""
        self.listening = listening
        for woken in self._woken.values():
            woken.set()

    async def wait(self, kind: str, timeout_s: float) -> bool:
        """Wait for ``kind``'s wake-up or the timeout; report which, and consume it."""
        woken = self._woken[kind]
        # Checked first: a zero timeout would otherwise expire before the
        # wait looked at a wake-up that is already there.
        if not woken.is_set():
            try:
                await asyncio.wait_for(woken.wait(), max(timeout_s, 0.0))
            except TimeoutError:
                return False
        woken.clear()
        return True
