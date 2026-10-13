"""A ceiling on how many event streams one account holds open on this process.

The change stream and each job's event stream are long-lived, and for as long
as one is open it keeps reading Postgres on every notification and on a
backstop clock. Nothing bounded how many one account could open: a script
doing so in a loop multiplies its share of the connection pool for nothing a
browser needs. A browser needs one change stream per tab and one event stream
per conversation it is watching; the default leaves room for about a dozen
tabs doing both.

In memory and per process, because what it protects is this process's pool
and event loop, and a count that needed shared storage to keep would cost the
very reads it exists to bound. A refusal is a 429 with ``Retry-After``: the web
app backs off and reconnects on one, and reads on its own clock meanwhile.

A slot is released when its stream ends for any reason, from the wrapping
generator's ``finally``. A response whose body never started — its client gone
before the first byte — never runs that, so a slot also releases itself when
it is collected.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, TypeVar

from fastapi import HTTPException

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_T = TypeVar("_T")

#: How many event streams one account may hold open on one process at once.
ENV_MAX_STREAMS_PER_USER = "AGENT_MAX_STREAMS_PER_USER"
DEFAULT_MAX_STREAMS_PER_USER = 24

#: Long enough for one of the account's streams to have closed, short enough
#: that a tab that was merely refused catches up quickly.
_RETRY_AFTER_S = 5


def max_streams_per_user() -> int:
    """Return the configured ceiling; a missing or unusable value is the default."""
    raw = (os.environ.get(ENV_MAX_STREAMS_PER_USER) or "").strip()
    try:
        configured = int(raw) if raw else DEFAULT_MAX_STREAMS_PER_USER
    except ValueError:
        return DEFAULT_MAX_STREAMS_PER_USER
    return configured if configured > 0 else DEFAULT_MAX_STREAMS_PER_USER


class StreamSlot:
    """One open stream's place in its account's count, released exactly once."""

    __slots__ = ("_held", "_owner", "_user_id")

    def __init__(self, owner: StreamSlots, user_id: str) -> None:
        """Record a slot ``owner`` has already counted for ``user_id``."""
        self._owner = owner
        self._user_id = user_id
        self._held = True

    def release(self) -> None:
        """Give the slot back. Idempotent."""
        if self._held:
            self._held = False
            self._owner._release(self._user_id)

    def __del__(self) -> None:
        """Give the slot back if its stream never ran to release it."""
        self.release()

    async def hold(self, stream: AsyncIterator[_T]) -> AsyncIterator[_T]:
        """Yield ``stream``'s items, closing it and releasing this slot at the end."""
        try:
            async for item in stream:
                yield item
        finally:
            try:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
            finally:
                self.release()


class StreamSlots:
    """The open-stream count for every account on this process."""

    def __init__(self) -> None:
        """Start with nothing open."""
        self._open: dict[str, int] = {}

    def open_for(self, user_id: str) -> int:
        """Return how many streams ``user_id`` holds open here."""
        return self._open.get(user_id, 0)

    def acquire(self, user_id: str) -> StreamSlot:
        """Count one more open stream for ``user_id``, or refuse it.

        Raises:
            HTTPException: 429 when the account already holds the maximum.
        """
        limit = max_streams_per_user()
        held = self._open.get(user_id, 0)
        if held >= limit:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": {
                        "type": "too_many_streams",
                        "message": (
                            f"This account already has {limit} live updates open. "
                            "Close a tab or two and try again."
                        ),
                    }
                },
                headers={"Retry-After": str(_RETRY_AFTER_S)},
            )
        self._open[user_id] = held + 1
        return StreamSlot(self, user_id)

    def _release(self, user_id: str) -> None:
        remaining = self._open.get(user_id, 0) - 1
        if remaining > 0:
            self._open[user_id] = remaining
        else:
            self._open.pop(user_id, None)


#: This process's count, shared by every stream route on it.
STREAM_SLOTS = StreamSlots()
