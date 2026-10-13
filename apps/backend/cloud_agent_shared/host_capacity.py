"""Capacity values the control plane and the host must agree on.

The admin Servers page has to answer three questions side by side: how loaded
the machine is, how much execution quota is left, and how many sandboxes are
still retained. The last two already have sources — ``slots_total/slots_used``
in the database and the sandbox manager's own Docker view — but the first has none, so
an operator sees a host taking work with no idea whether it is anywhere near
its ceiling.

**What lives here.** The wire version of the heartbeat's ``capacity`` block,
the name of the observation method, the closed vocabulary for a missing CPU or
memory measurement, the fixed partition of Docker's container states, and the
pure ``MemTotal``/``MemAvailable`` derivation both sides render. Everything
that touches the machine — the ``/proc`` readers, the bounded Docker listing,
the sampler thread — stays in :mod:`cloud_agent_host.host_capacity`, which
re-exports these names so existing importers are unaffected.

The module depends on the standard library alone, deliberately: the control
plane imports these values to render the same percentages the runner reported,
and a control-plane module may not drag in the sandbox stack to do it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The wire version of the ``capacity`` block in a host heartbeat. A control
#: plane that does not know a version drops the block and keeps the heartbeat.
SCHEMA_VERSION: Final = 1

#: Names the observation method, so a future remote-Docker or rootless adapter
#: is distinguishable from this one in stored history.
SOURCE: Final = "linux_host_proc_and_docker_list"

#: The remaining states, at fixed keys so the shape never varies. ``paused``
#: included deliberately: a paused container has not released its memory, so it
#: must not be folded into "stopped".
OTHER_STATES: tuple[str, ...] = ("created", "paused", "restarting", "removing", "dead")

#: Missing-measurement vocabulary for the CPU and memory halves. Closed sets:
#: the control plane stores the string and the page explains it, so a new value
#: is a protocol change rather than a log line.
CPU_MISSING_WARMING_UP: Final = "warming_up"
CPU_MISSING_COUNTER_RESET: Final = "counter_reset"
CPU_MISSING_SOURCE_UNAVAILABLE: Final = "source_unavailable"
CPU_MISSING_INVALID_SAMPLE: Final = "invalid_sample"
MEMORY_MISSING_SOURCE_UNAVAILABLE: Final = "source_unavailable"
MEMORY_MISSING_INVALID_SAMPLE: Final = "invalid_sample"


@dataclass(frozen=True)
class MemoryUsage:
    """Used memory derived from a MemTotal/MemAvailable pair.

    Attributes:
        used_bytes: ``MemTotal - MemAvailable``.
        used_percent: ``used_bytes`` as a percentage of ``MemTotal``.
    """

    used_bytes: int
    used_percent: float


def memory_usage(*, total_bytes: int | None, available_bytes: int | None) -> MemoryUsage | None:
    """Return used memory from a total and an available figure, or ``None``.

    ``MemAvailable`` is the kernel's own estimate of what a new workload can
    have without swapping. ``MemFree`` is not a substitute for it — a machine
    with a warm page cache has almost no free memory and plenty available — so
    a missing ``MemAvailable`` yields no measurement at all.

    Args:
        total_bytes: ``MemTotal`` in bytes.
        available_bytes: ``MemAvailable`` in bytes.

    Returns:
        The derived usage, or ``None`` when either figure is absent or the
        pair is not physically possible.
    """
    if total_bytes is None or available_bytes is None:
        return None
    if total_bytes <= 0 or available_bytes < 0 or available_bytes > total_bytes:
        return None
    used_bytes = total_bytes - available_bytes
    return MemoryUsage(used_bytes=used_bytes, used_percent=100.0 * used_bytes / total_bytes)


#: Missing-measurement vocabulary for storage. The first four are the runner's
#: to produce; ``invalid_sample`` is the control plane's verdict on a sub-block
#: that failed validation, and ``not_reported`` is what an older runner's
#: silence reads as. Every member is explained on the page, so this is closed.
STORAGE_MISSING_DISABLED: Final = "disabled"
STORAGE_MISSING_UNSUPPORTED: Final = "unsupported"
STORAGE_MISSING_UNVERIFIED: Final = "unverified"
STORAGE_MISSING_SOURCE_UNAVAILABLE: Final = "source_unavailable"


@dataclass(frozen=True)
class StorageUsage:
    """Used block space derived from a total/free/available triple.

    Attributes:
        used_bytes: ``total - free``, the figure GNU ``df`` prints.
        used_percent: ``used_bytes`` over ``used_bytes + available_bytes``.
    """

    used_bytes: int
    used_percent: float


#: The wire range of the three block-space figures, stated once. Anything
#: outside it — a negative, an ordering that cannot happen, a value a
#: JavaScript client cannot hold exactly — is not a reading to be forwarded.
MAX_STORAGE_BYTES: Final = 2**53 - 1


def storage_bytes_in_range(*, total_bytes: int, free_bytes: int, available_bytes: int) -> bool:
    """Whether a block-space triple is transmissible at all.

    ``available <= free <= total`` is a fact about filesystems rather than a
    sanity margin: ``free`` counts the blocks reserved for root and
    ``available`` does not, so a triple that breaks the relation came from
    something this code cannot describe. A zero total is inside the wire range
    — an empty pseudo-filesystem is a real answer — but it yields no usage,
    because there is no denominator to take a percentage of.

    Args:
        total_bytes: Filesystem size in bytes.
        free_bytes: Free bytes, reserve included.
        available_bytes: Free bytes an unprivileged writer may use.

    Returns:
        ``True`` when the triple fits the protocol's range and ordering.
    """
    return 0 <= available_bytes <= free_bytes <= total_bytes <= MAX_STORAGE_BYTES


def storage_usage(
    *, total_bytes: int | None, free_bytes: int | None, available_bytes: int | None
) -> StorageUsage | None:
    """Return used block space in the ``df`` sense, or ``None``.

    The shared formula, mirroring :func:`memory_usage`: the runner's live value
    and the admin page's stored value come through here, so the two cannot
    disagree. The three inputs are not interchangeable, and used is *not*
    ``total - available``: ``available`` excludes the reserve, so that
    subtraction would report every healthy filesystem as partly full with
    blocks nobody wrote.

    Args:
        total_bytes: Filesystem size in bytes.
        free_bytes: Free bytes, reserve included.
        available_bytes: Free bytes an unprivileged writer may use.

    Returns:
        The derived usage, or ``None`` when the triple is absent, outside the
        wire range, or has no denominator — a filesystem whose every block is
        reserved and none of them written. Callers render ``None`` as
        ``invalid_sample``, never as zero.
    """
    if total_bytes is None or free_bytes is None or available_bytes is None:
        return None
    if not storage_bytes_in_range(
        total_bytes=total_bytes, free_bytes=free_bytes, available_bytes=available_bytes
    ):
        return None
    if total_bytes <= 0:
        return None
    used_bytes = total_bytes - free_bytes
    denominator = used_bytes + available_bytes
    if denominator <= 0:
        return None
    return StorageUsage(used_bytes=used_bytes, used_percent=100.0 * used_bytes / denominator)
