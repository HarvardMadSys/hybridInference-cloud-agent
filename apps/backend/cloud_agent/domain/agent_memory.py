"""Who writes a durable memory: the running attempt a memory token names."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryTask:
    """The running attempt an agent writes memory from, as its token names it."""

    user_id: str
    job_id: str
    attempt_id: int
    lease_generation: int
    residency_generation: int | None = None
