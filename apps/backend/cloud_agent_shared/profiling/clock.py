"""Clock access shared by the profiler's concern modules.

The public module retains its replaceable ``time`` object so deterministic
consumers can replace one clock for the entire collector. Resolve it only when
collecting; the default-off selection path performs no clock reads.
"""

from __future__ import annotations

from typing import Protocol


class Clock(Protocol):
    """The timing capabilities needed by collection and background export."""

    def monotonic(self) -> float:
        """Return elapsed seconds from the process clock domain."""
        ...

    def monotonic_ns(self) -> int:
        """Return elapsed nanoseconds from the process clock domain."""
        ...

    def time(self) -> float:
        """Return seconds from the wall clock epoch."""
        ...

    def sleep(self, seconds: float) -> None:
        """Yield execution for the requested interval."""
        ...


def clock() -> Clock:
    """Return the clock currently installed on the stable producer API."""
    from .. import operation_profiling

    return operation_profiling.time
