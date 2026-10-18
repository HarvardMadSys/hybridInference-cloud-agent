"""An owner's own containers and what they have used, for the Usage settings page.

Three reads, each scoped to the signed-in owner:

- **Containers**: every retained container behind a chat the owner can still
  see, with its limits and, while it runs, its latest Docker sample.
- **Resources**: per UTC day, how long the owner's containers ran, read from
  the lifecycle events the database records on every state change, and the
  CPU and memory measured while they ran, read from
  ``agent_sandbox_usage_days``.
- **Tokens**: the per-run Gateway accounting the admin Usage page sums,
  selected for this owner's runs only.

None of them names a host, container ID, image or generation: those describe
the platform, not the owner's work.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.admin_observability import cursor_scope

# Pydantic resolves these annotations when it builds the token usage view.
from cloud_agent.admin_usage import (  # noqa: TC001
    AdminUsageAggregate,
    AdminUsageBreakdown,
    AdminUsageDay,
)

ContainerState = Literal[
    "starting", "running", "stopping", "stopped", "stored", "deleting", "unavailable"
]
#: The order counts are reported in, and the order a page lists containers in.
CONTAINER_STATES: tuple[ContainerState, ...] = (
    "running",
    "starting",
    "stopping",
    "stopped",
    "stored",
    "deleting",
    "unavailable",
)
#: How each stored lifecycle state reads to its owner. ``cold`` is a container
#: kept only as a checkpoint, restored on the next message; ``lost`` is one the
#: host no longer reports.
_STATE_VIEW: dict[str, ContainerState] = {
    "provisioning": "starting",
    "starting": "starting",
    "restoring": "starting",
    "running": "running",
    "stopping": "stopping",
    "stopped": "stopped",
    "stopped_dirty": "stopped",
    "cold": "stored",
    "lost": "unavailable",
}
#: Stored states in which a container process exists and holds its CPU and
#: memory reservation. Running time is the time spent in one of them.
RUNNING_STATES: tuple[str, ...] = ("starting", "running", "stopping")
#: Stored states listed before the rest, newest activity first within each.
ACTIVE_STATES: tuple[str, ...] = ("provisioning", "starting", "restoring", "running", "stopping")

#: Containers on one page, and the most a client may ask for.
DEFAULT_CONTAINER_PAGE = 50
MAX_CONTAINER_PAGE = 100
#: Chats named on one container. A container holds its origin chat and every
#: Fork of it, so the rest are counted rather than listed.
MAX_CONTAINER_CHATS = 20
#: A sample older than this no longer describes a running container's use.
#: The same bound as the admin inventory's.
READING_STALE_SECONDS = 90.0
#: Running containers whose current use is summed. A real owner has a handful;
#: the bound keeps a pathological account from one unbounded read.
MAX_SUMMED_CONTAINERS = 1000

#: Longest gap between two samples of one running container that still counts
#: as continuous use. Hosts report every 30 seconds and sample every 15; a
#: longer gap means reports were lost, and use during it is not assumed.
MAX_SAMPLE_GAP_SECONDS = 120.0
#: Days a history read may span: the admin Usage page's bound.
MAX_HISTORY_DAYS = 90

CONTAINER_CURSOR_SCOPE = cursor_scope(["owner-containers"])

_MEMORY_LIMIT = re.compile(r"(\d+(?:\.\d+)?)([bkmgt]?)b?")
_MEMORY_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def container_state(state: str, deletion_state: str) -> ContainerState:
    """Name a stored lifecycle state the way its owner reads it.

    A deletion is ``pending`` until its archive is uploaded and ``archived``
    until the container is gone; both read as deleting. A completed deletion
    is not listed at all.
    """
    if deletion_state in {"pending", "archived"}:
        return "deleting"
    return _STATE_VIEW.get(state, "unavailable")


def memory_limit_bytes(value: str | None) -> int | None:
    """Read a Docker memory limit such as ``4g`` or ``512m``; None when unreadable."""
    match = _MEMORY_LIMIT.fullmatch((value or "").strip().lower())
    if match is None:
        return None
    amount = float(match.group(1)) * _MEMORY_UNITS[match.group(2)]
    return int(amount) if amount > 0 and math.isfinite(amount) else None


def cpu_limit_cores(value: str | None) -> float | None:
    """Read a Docker CPU limit such as ``2`` or ``1.5``; None when unreadable."""
    try:
        cores = float((value or "").strip())
    except ValueError:
        return None
    return cores if cores > 0 and math.isfinite(cores) else None


def measured_interval(
    *,
    sampled_at: datetime,
    previous_sampled_at: datetime | None,
    cpu_window_seconds: float | None,
) -> float:
    """The seconds of use one new sample of a running container stands for.

    Hosts sample every 15 seconds but report every 30, so the control plane
    sees about every other sample. Each one therefore stands for the time since
    the previous one it accepted, holding its readings over that gap. After a
    start, or a gap too long to be continuous, it stands only for the interval
    its own CPU rate was measured over.
    """
    if previous_sampled_at is not None:
        gap = (sampled_at - previous_sampled_at).total_seconds()
        if 0 < gap <= MAX_SAMPLE_GAP_SECONDS:
            return gap
    if cpu_window_seconds is None or not cpu_window_seconds > 0:
        return 0.0
    return min(cpu_window_seconds, MAX_SAMPLE_GAP_SECONDS)


def utc_day_shares(*, ended_at: datetime, seconds: float) -> list[tuple[date, float]]:
    """How much of ``seconds`` of use ending at ``ended_at`` falls on each UTC day.

    A sample stands for the time before it, which can begin on the day before:
    one taken ten seconds after midnight, standing for thirty, used twenty of
    them the day before. One taken exactly at midnight stands wholly for the
    day before. Earliest day first.
    """
    shares: list[tuple[date, float]] = []
    end = ended_at.astimezone(UTC)
    left = seconds
    while left > 0:
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        if start == end:
            start -= timedelta(days=1)
        share = min(left, (end - start).total_seconds())
        shares.append((start.date(), share))
        left -= share
        end = start
    return shares[::-1]


def history_window(days: int, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The last ``days`` UTC calendar days, today included, up to now."""
    if not 1 <= days <= MAX_HISTORY_DAYS:
        raise ValueError(f"Choose between 1 and {MAX_HISTORY_DAYS} days.")
    until = (now or datetime.now(UTC)).astimezone(UTC)
    today = until.replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days - 1), until


class OwnerContainerChat(BaseModel):
    """A chat whose work runs in this container."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    #: The chat's latest turn, which is what a chat link opens.
    job_id: str | None = None
    title: str | None = None
    archived: bool = False


class OwnerContainerReading(BaseModel):
    """The latest Docker sample of a running container, with its age."""

    model_config = ConfigDict(extra="forbid")

    cpu_cores: float | None = Field(None, ge=0)
    memory_bytes: int | None = Field(None, ge=0)
    sampled_at: datetime
    stale: bool


class OwnerContainer(BaseModel):
    """One container and the chats that share it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    state: ContainerState
    repo: str | None = None
    chats: list[OwnerContainerChat] = Field(max_length=MAX_CONTAINER_CHATS)
    chat_count: int = Field(ge=0)
    cpu_limit_cores: float | None = None
    memory_limit_bytes: int | None = None
    #: None when the container has no disk limit.
    disk_limit_bytes: int | None = None
    reading: OwnerContainerReading | None = None
    created_at: datetime
    last_activity_at: datetime
    #: When an idle running container stops by itself; None otherwise.
    idle_deadline: datetime | None = None


class OwnerCurrentUse(BaseModel):
    """What the owner's running containers use now, from their fresh samples."""

    model_config = ConfigDict(extra="forbid")

    cpu_cores: float = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    #: Running containers whose fresh sample is in the sums.
    measured: int = Field(ge=0)
    #: Running containers without one, left out of the sums.
    unmeasured: int = Field(ge=0)


class OwnerContainerList(BaseModel):
    """A page of the owner's containers, with counts and sums over all of them."""

    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    counts: dict[ContainerState, int]
    total_count: int = Field(ge=0)
    current: OwnerCurrentUse
    containers: list[OwnerContainer]
    next_cursor: str | None = None


class OwnerContainerCursor(BaseModel):
    """Continue a container page after the last row of the previous one."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    rank: int = Field(ge=0, le=1)
    last_activity_at: datetime
    id: str = Field(min_length=1, max_length=256)


class OwnerResourceTotals(BaseModel):
    """Running and measured time, and the CPU and memory used over it."""

    model_config = ConfigDict(extra="forbid")

    running_seconds: float = Field(ge=0)
    #: Running time covered by samples. CPU and memory describe only this.
    measured_seconds: float = Field(ge=0)
    cpu_core_seconds: float = Field(ge=0)
    memory_byte_seconds: float = Field(ge=0)


class OwnerResourceDay(OwnerResourceTotals):
    """One UTC day of the window; the last one ends now."""

    date: str
    since: datetime
    until: datetime


class OwnerResourceHistory(BaseModel):
    """The owner's container use per UTC day, summed over every container."""

    model_config = ConfigDict(extra="forbid")

    since: datetime
    until: datetime
    day_timezone: Literal["UTC"] = "UTC"
    generated_at: datetime
    totals: OwnerResourceTotals
    days: list[OwnerResourceDay]


class OwnerTokenUsage(BaseModel):
    """The owner's model usage for runs created in the window.

    Every number is the admin Usage page's for the same runs: a run counts on
    the UTC day it was created, in full, and an unknown amount stays unknown.
    """

    model_config = ConfigDict(extra="forbid")

    since: datetime
    until: datetime
    day_timezone: Literal["UTC"] = "UTC"
    time_basis: Literal["job_created_at"] = "job_created_at"
    generated_at: datetime
    summary: AdminUsageAggregate
    days: list[AdminUsageDay]
    by_model: AdminUsageBreakdown
