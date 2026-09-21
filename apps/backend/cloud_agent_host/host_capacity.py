"""Bounded host-capacity observation for the runner's own Docker host.

The admin Servers page has to answer three questions side by side: how loaded
the machine is, how much execution quota is left, and how many sandboxes are
still retained. The last two already have sources — ``slots_total/slots_used``
in the database and the broker's own Docker view — but the first has none, so
an operator sees a host taking work with no idea whether it is anywhere near
its ceiling.

**What this module measures.** The Linux host running the Docker Engine, not
this container: CPU busy share from the host's ``/proc/stat``, memory from the
host's ``/proc/meminfo``, and an entity count of retained chat containers from
one read-only ``docker container ls``. Kata guest activity is inside the host's
load by construction; splitting it out is not this version's job.

**Why the host proc files are bind-mounted rather than the whole of
``/proc``.** A guest kernel's ``/stat`` describes this container, and a
container with ``pid: host`` would hand a runner — a process sitting beside
untrusted repository code — a view of every process on the machine. The compose
file therefore mounts exactly three files read-only under
``/run/host-metrics``, and this module reads exactly those. A missing file is
reported as a missing measurement; it is never replaced by reading an
unscoped ``/proc``.

**Why the inventory is its own Docker call.** The broker's
``ChatSandboxManager.list()`` reconciles: it stops orphan containers, prunes
networks and inspects each container in turn. Running it on a 30-second timer
would make monitoring a lifecycle writer. This module makes one ``container
ls`` with a JSON Go template and no inspection at all — no ``--size``, no
``--all``-plus-inspect, no ``stats``, nothing that writes.

**Why storage is read from this container's own root, and why a file gates it.**
The sandbox writable layers live on the filesystem the Docker storage driver
keeps its upper directories on. This runner is an ordinary container under that
same daemon and driver, so ``overlay``'s ``statfs`` semantics make
``os.statvfs("/")`` that filesystem — no host mount, no Docker query, no child
process. The catch is that a *plausible* number is not a *verified* one: a
project quota on the upper directory returns the quota rather than the disk, and
a container that reads its quota would report a 10 GiB ceiling as though it were
the machine's capacity. So the number is withheld unless the deployment has
attested the topology and written that verdict to a file the runner reads each
round; an absent, stale or mismatched verdict reports ``unverified`` rather than
a value. See ``docs/specs/2026-09-15-admin-server-storage-capacity-design.zh-CN.md``.

**Why a daemon thread.** The heartbeat is this host's liveness signal, and a
Docker daemon under load can take seconds to answer. Sampling on its own thread
and publishing only the latest result keeps a slow Docker out of the heartbeat
path entirely; a sample that is not ready is simply not sent.

**Everything here is bounded.** The Docker child is killed at two seconds, its
stdout is capped at one mebibyte and 4096 lines while being read — not after —
and the proc files have a length cap of their own. A capped or timed-out
inventory reports itself unavailable rather than reporting the smaller count it
managed to read, because a truncated container count looks exactly like a
plausible one.

The module depends on the standard library alone. The control plane imports its
pure helpers (``cpu_busy_percent``, ``memory_usage``) to render the same
percentages the runner reported, and a control-plane module may not drag in the
sandbox stack to do it.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from cloud_agent_host.logging_utils import get_logger
from cloud_agent_shared.host_capacity import (
    CPU_MISSING_COUNTER_RESET,
    CPU_MISSING_INVALID_SAMPLE,
    CPU_MISSING_SOURCE_UNAVAILABLE,
    CPU_MISSING_WARMING_UP,
    MAX_STORAGE_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    MEMORY_MISSING_INVALID_SAMPLE,
    MEMORY_MISSING_SOURCE_UNAVAILABLE,
    OTHER_STATES,
    SCHEMA_VERSION,
    SOURCE,
    STORAGE_MISSING_DISABLED,
    STORAGE_MISSING_SOURCE_UNAVAILABLE,
    STORAGE_MISSING_UNSUPPORTED,
    STORAGE_MISSING_UNVERIFIED,
    MemoryUsage,  # noqa: F401 — re-exported: the identity the host namespace kept.
    StorageUsage,  # noqa: F401 — re-exported: the identity the host namespace kept.
    memory_usage,
    storage_bytes_in_range,
    storage_usage,  # noqa: F401 — re-exported: the identity the host namespace kept.
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from _typeshed import FileDescriptorLike

logger = get_logger(__name__)

#: Set to a falsey value to stop sampling on this host. Absent means enabled:
#: the compose file mounts the proc files, and an operator who did not ask for
#: the feature off should get it.
ENV_CAPACITY_ENABLED = "AGENT_HOST_CAPACITY_ENABLED"

#: Which storage probe this deployment declared. The deployment script knows
#: the topology — which driver keeps the writable layers, and where — and this
#: module never guesses it: a value it does not recognize reports
#: ``unsupported`` rather than silently measuring whatever ``/`` happens to be.
#: Absent means the one probe this version ships, matching the compose file's
#: own default.
ENV_STORAGE_PROBE = "AGENT_HOST_STORAGE_PROBE"
STORAGE_PROBE_OVERLAY_ROOT = "overlay_root"
STORAGE_PROBE_OFF = "off"

#: Identity of the only probe in this version: the runner's root filesystem,
#: which is an overlay whose upper layer sits on the sandbox writable layer's
#: filesystem. A new probe is a new value here, not a new protocol version.
STORAGE_SOURCE_OVERLAY_ROOT: Final = "linux_overlay_root_statvfs"

#: Where the deployment writes the topology verdict it reached, and what the
#: runner reads instead of trusting its own reading. The *directory* is the bind
#: mount, so this name is resolved again on every sample: the deployment may
#: replace, delete or recreate the file underneath a running replica and the
#: next sample sees it. The path is a contract with the host compose file,
#: exactly like ``DEFAULT_PROC_ROOT``.
DEFAULT_STORAGE_VERDICT_PATH = "/run/cloud-agent/storage-probe/verdict.json"
VERDICT_SCHEMA_VERSION: Final = 1
VERDICT_VERIFIED: Final = "verified"

#: The verdict document's ceiling. Small on purpose: it holds one short record
#: per runner replica, and a file larger than this is not the file that was
#: written — it is read on the heartbeat's own thread every cycle.
VERDICT_MAX_BYTES = 16 * 1024

#: The mount table of this container, which is where an overlay's upper layer is
#: named. Read, never parsed as a general mount table: this module looks for the
#: one record whose mount point is ``/``.
MOUNTINFO_PATH = "/proc/self/mountinfo"
CONTAINER_ROOT = "/"
OVERLAY_FSTYPE: Final = "overlay"
UPPERDIR_OPTION: Final = "upperdir="

#: Where the compose file mounts the three host proc files. Not configurable
#: through the environment on purpose: this path is a contract with the host
#: deployment, and a typo in it must fail visibly instead of silently measuring
#: whatever else happens to be there.
DEFAULT_PROC_ROOT = "/run/host-metrics"

#: The three fixed inputs, in the order the host mounts them.
PROC_STAT_FILE = "stat"
PROC_MEMINFO_FILE = "meminfo"
PROC_BOOT_ID_FILE = "boot_id"

#: The immutable ownership label every chat container carries. Mirrored from
#: ``cloud_agent_host.chat_sandbox`` rather than imported, so that the control
#: plane can import this module without importing the sandbox stack; a test
#: asserts the two agree.
CHAT_SANDBOX_LABEL = "org.freeinference.chat-sandbox"
CHAT_NAMESPACE_LABEL = "org.freeinference.sandbox-namespace"
CHAT_SANDBOX_ID_LABEL = "org.freeinference.chat-sandbox-id"

#: Docker's own state vocabulary, partitioned exactly as the design requires.
RUNNING_STATE = "running"
STOPPED_STATE = "exited"

#: Sampling cadence. Matches the heartbeat interval so a fresh observation is
#: available for every beat.
SAMPLE_INTERVAL_S = 30.0

#: A CPU interval longer than this is a gap, not an average: the baseline is
#: rebuilt rather than reporting a number smeared across a stall.
MAX_CPU_WINDOW_S = 90.0

#: Budgets. Small on purpose — this is monitoring, and it runs next to real
#: work.
DOCKER_TIMEOUT_S = 2.0
DOCKER_MAX_STDOUT_BYTES = 1024 * 1024
DOCKER_MAX_LINES = 4096
DOCKER_MAX_STDERR_BYTES = 8192
PROC_MAX_BYTES = 256 * 1024

#: How old a cached observation may be and still be sent. Older than this the
#: runner sends no capacity at all, which reads as stale rather than as news.
MAX_SAMPLE_AGE_MS = 300_000

#: Inventory outcomes.
INVENTORY_AVAILABLE = "available"
INVENTORY_PARTIAL = "partial"
INVENTORY_UNAVAILABLE = "unavailable"

#: Why an inventory is partial or unavailable. Bounded identifiers rather than
#: prose: they are stored, compared and counted.
REASON_NAMESPACE_UNSET = "namespace_unset"
REASON_DOCKER_UNAVAILABLE = "docker_unavailable"
REASON_DOCKER_TIMEOUT = "docker_timeout"
REASON_DOCKER_EXIT_ERROR = "docker_exit_error"
REASON_DOCKER_OUTPUT_LIMIT = "docker_output_limit"
REASON_DOCKER_LINE_LIMIT = "docker_line_limit"
REASON_DOCKER_MALFORMED_OUTPUT = "docker_malformed_output"
REASON_UNKNOWN_STATE = "unknown_state"
REASON_UNSCOPED_LEGACY_CONTAINERS = "unscoped_legacy_containers"

#: ``used = MemTotal - MemAvailable``, in bytes. A kernel page is 1 KiB here
#: regardless of the host's page size, which is what /proc/meminfo documents.
_KIB = 1024

#: The eight accounted columns of /proc/stat's aggregate line. ``guest`` and
#: ``guest_nice`` are deliberately absent: the kernel already counts them
#: inside ``user`` and ``nice``, so adding them would count them twice.
_CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def capacity_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this host should sample capacity at all.

    Args:
        environ: Environment to read; the process environment by default.

    Returns:
        ``True`` unless the value is an explicit falsey spelling. Absent means
        enabled, because the deployment that mounts the proc files wants this
        and a silent opt-out is how a page ends up permanently empty.
    """
    source = os.environ if environ is None else environ
    raw = (source.get(ENV_CAPACITY_ENABLED) or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class CpuCounters:
    """One aggregate CPU sample from ``/proc/stat``, in USER_HZ ticks.

    Attributes:
        fields: The eight accounted columns, in kernel order.
        logical_count: Number of ``cpuN`` lines beside the aggregate.
    """

    fields: tuple[int, ...]
    logical_count: int

    @property
    def total(self) -> int:
        """Every accounted tick: the denominator of the busy share."""
        return sum(self.fields)

    @property
    def idle(self) -> int:
        """Ticks the CPU spent idle or waiting on I/O.

        Both are excluded from busy. A machine whose disks are the bottleneck
        is not a machine whose CPUs are, and reporting it as saturated would
        send an operator to the wrong component.
        """
        return self.fields[3] + self.fields[4]


def cpu_busy_percent(*, delta_total: int, delta_idle: int) -> float | None:
    """Return the non-idle share of one CPU interval, or ``None``.

    The shared formula: both the runner's live value and the control plane's
    historical points come through here, so the number on the table and the
    number on the chart cannot drift apart.

    Args:
        delta_total: Accounted ticks elapsed across the interval.
        delta_idle: Idle plus iowait ticks across the same interval.

    Returns:
        A percentage in ``[0, 100]``, or ``None`` when the interval cannot
        produce one — a non-positive denominator, or an idle delta that does
        not fit inside the total.
    """
    if delta_total <= 0 or delta_idle < 0 or delta_idle > delta_total:
        return None
    return 100.0 * (delta_total - delta_idle) / delta_total


def parse_cpu_stat(text: str) -> CpuCounters:
    """Parse the aggregate line and CPU count out of ``/proc/stat``.

    Args:
        text: The whole file.

    Returns:
        The aggregate counters and how many CPUs the file accounts for.

    Raises:
        ValueError: The aggregate line is missing, short, or not numbers, or
            the file accounts for no CPUs at all.
    """
    aggregate: tuple[int, ...] | None = None
    logical_count = 0
    for line in text.splitlines():
        if line.startswith("cpu ") and aggregate is None:
            parts = line.split()
            if len(parts) < len(_CPU_FIELDS) + 1:
                raise ValueError("aggregate cpu line has too few columns")
            try:
                aggregate = tuple(int(part) for part in parts[1 : len(_CPU_FIELDS) + 1])
            except ValueError as exc:
                raise ValueError("aggregate cpu line is not numeric") from exc
            if any(value < 0 for value in aggregate):
                raise ValueError("aggregate cpu line has a negative counter")
        elif line.startswith("cpu") and line[3:4].isdigit():
            logical_count += 1
    if aggregate is None:
        raise ValueError("no aggregate cpu line")
    if logical_count <= 0:
        raise ValueError("no per-cpu lines")
    return CpuCounters(fields=aggregate, logical_count=logical_count)


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    """Parse ``MemTotal`` and ``MemAvailable`` out of ``/proc/meminfo``.

    Args:
        text: The whole file.

    Returns:
        ``(total_bytes, available_bytes)``, each ``None`` when its field is
        absent or unparseable. Bytes, converted at 1 KiB per documented unit.
    """
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name not in {"MemTotal", "MemAvailable"}:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            values[name] = int(parts[0]) * _KIB
        except ValueError:
            continue
    return values.get("MemTotal"), values.get("MemAvailable")


def parse_boot_id(text: str) -> str | None:
    """Return the host's boot id as a canonical UUID string, or ``None``.

    Args:
        text: The contents of ``/proc/sys/kernel/random/boot_id``.

    Returns:
        The dashed UUID, or ``None`` when the file does not hold one. A value
        that is not a UUID is not a boot id, and guessing that it is would let
        two different hosts be added together.
    """
    try:
        return str(uuid.UUID(text.strip()))
    except (ValueError, AttributeError):
        return None


# ── Storage: the filesystem holding sandbox writable layers ────────────


@dataclass(frozen=True)
class StorageSample:
    """One usable reading of the filesystem that holds writable layers.

    Attributes:
        total_bytes: Filesystem size.
        free_bytes: Free bytes, reserve included.
        available_bytes: Free bytes an unprivileged writer may use.
        target: Human-readable label for what was measured — the host mount
            root the deployment attested. A label for the tooltip, never
            something any logic branches on.
    """

    total_bytes: int
    free_bytes: int
    available_bytes: int
    target: str | None = None


@dataclass(frozen=True)
class StorageMissing:
    """Why this round produced no storage reading."""

    reason: Literal["disabled", "unsupported", "unverified", "source_unavailable"]


class StatvfsReading(Protocol):
    """The four ``statvfs`` fields a block-space reading needs.

    Read-only, which is what makes ``os.statvfs_result`` itself satisfy this:
    its fields are structseq members with no setter, and a protocol asking for
    assignable attributes would exclude the very type every caller passes.
    """

    @property
    def f_frsize(self) -> int:
        """Fragment size, the unit the block counts are in."""

    @property
    def f_blocks(self) -> int:
        """Fragments in the filesystem."""

    @property
    def f_bfree(self) -> int:
        """Fragments free, including the ones reserved for root."""

    @property
    def f_bavail(self) -> int:
        """Fragments an unprivileged writer may still use."""


class StorageProbe(Protocol):
    """What the collector is allowed to know about a storage measurement.

    One identifier and one method. A different host type, driver or storage
    backend is a new implementation of this protocol with a new ``source`` —
    not a new field on the wire, and never a branch in the page.
    """

    source: str

    def sample(self) -> StorageSample | StorageMissing:
        """Return this round's reading, or why there is none."""
        ...


@dataclass(frozen=True)
class RootMount:
    """The mountinfo record for the container's own ``/``.

    Attributes:
        fstype: The filesystem type the record names.
        upperdir: The overlay upper directory, when the record names one.
        mount_point: The record's mount point, always ``/`` here.
    """

    fstype: str
    upperdir: str | None
    mount_point: str


def find_root_mount(text: str) -> RootMount | None:
    r"""Find the mountinfo record whose mount point is ``/``, or ``None``.

    Deliberately not a mount-table parser: one line is selected by mount point,
    and three fields are read off it. Escape sequences (``\\040`` and friends)
    are left exactly as written, because the value is a lookup key shared with
    the deployment script, which extracts the same substring the same way — an
    unescaping difference would make the runner and the verdict disagree about
    a path that is, byte for byte, identical.

    Args:
        text: Contents of a ``mountinfo`` file.

    Returns:
        The root record, or ``None`` when no record has ``/`` as its mount
        point or the record is too short to carry a filesystem type.
    """
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 7 or fields[4] != CONTAINER_ROOT:
            continue
        try:
            separator = fields.index("-")
        except ValueError:
            return None
        # The separator is followed by fstype, source and super options.
        if separator + 3 >= len(fields):
            return None
        upperdir: str | None = None
        for option in fields[separator + 3].split(","):
            if option.startswith(UPPERDIR_OPTION):
                upperdir = option[len(UPPERDIR_OPTION) :] or None
                break
        return RootMount(fstype=fields[separator + 1], upperdir=upperdir, mount_point=fields[4])
    return None


def root_overlay_upperdir(text: str) -> str | None:
    """Return the upper directory behind this container's ``/``, or ``None``.

    The kernel's ``ovl_statfs`` resolves the overlay root to its upper layer
    before asking the underlying filesystem for its statistics, which is the
    whole reason a runner can answer "how full is the sandbox writable layer's
    disk" about itself. The reverse is also true and is why this exists: on a
    container whose root is *not* such an overlay, the same call would describe
    some other filesystem, so the reading is refused instead.

    Args:
        text: Contents of a ``mountinfo`` file.

    Returns:
        The upper directory, or ``None`` when the root is not an overlay or
        carries no upper layer.
    """
    mount = find_root_mount(text)
    if mount is None or mount.fstype != OVERLAY_FSTYPE:
        return None
    return mount.upperdir


@dataclass(frozen=True)
class StorageVerdictEntry:
    """One attested topology, as the deployment's script recorded it.

    Attributes:
        verdict: ``verified``, ``unsupported`` or ``unverified``. An unknown
            value is treated as ``unverified`` by the reader.
        mount_root: Host mount root of the filesystem that was measured.
        size_bytes: The size the host's ``df`` reported for that mount root.
        reason: Human-readable detail for the deployment log.
    """

    verdict: str
    mount_root: str | None = None
    size_bytes: int | None = None
    reason: str | None = None


def parse_storage_verdict(text: str) -> dict[str, StorageVerdictEntry] | None:
    """Parse the deployment's verdict document, or ``None`` when unusable.

    Args:
        text: The file's contents, already length-checked by the caller.

    Returns:
        Entries keyed by the runner's own upper directory. ``None`` means the
        document cannot be trusted at all — not JSON, not an object, an unknown
        schema version, or an ``entries`` member that is not an object. Every
        one of those makes the caller report ``unverified``: an unreadable
        attestation is not an attestation, and the failure direction of a
        monitoring field has to be silence rather than a number. A single
        malformed *entry* is skipped instead, so a corrupt row for a sibling
        replica cannot take this one's verdict away.
    """
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema_version") != VERDICT_SCHEMA_VERSION:
        return None
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, dict):
        return None
    entries: dict[str, StorageVerdictEntry] = {}
    for key, raw in raw_entries.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        verdict = raw.get("verdict")
        if not isinstance(verdict, str):
            continue
        size = raw.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int):
            size = None
        mount_root = raw.get("mount_root")
        reason = raw.get("reason")
        entries[key] = StorageVerdictEntry(
            verdict=verdict,
            mount_root=mount_root if isinstance(mount_root, str) and mount_root else None,
            size_bytes=size,
            reason=reason if isinstance(reason, str) else None,
        )
    return entries


class OverlayRootStorageProbe:
    """Read this runner's root filesystem, and report it only when attested.

    The order of the checks is the design's, and each step fails closed:

    1. ``/`` is not an overlay with an upper layer → ``unsupported``; there is
       no claim to make about this container.
    2. ``statvfs`` raised, or returned a triple outside the wire range →
       ``source_unavailable``.
    3. the verdict file is absent, unreadable, oversized or unusable → the
       number is withheld as ``unverified``.
    4. no entry for *this* upper directory → ``unverified``. A rebuilt
       container gets a new upper directory, so the previous attestation stops
       applying on its own; nothing here expires by time.
    5. the entry says ``unsupported``/``unverified`` → that reason.
    6. the entry's ``size_bytes`` no longer matches what ``statvfs`` reports →
       ``unverified``: the filesystem was resized or replaced since the
       deployment looked at it, and only a new deployment can say what it is
       now.

    Only then is a reading published, labelled with the attested host mount
    root. A mountinfo read that fails is ``source_unavailable`` rather than
    ``unsupported``: the conditions could not be established at all, which is a
    different statement from having established that they do not hold.
    """

    source = STORAGE_SOURCE_OVERLAY_ROOT

    def __init__(
        self,
        *,
        verdict_path: str | Path = DEFAULT_STORAGE_VERDICT_PATH,
        mountinfo_path: str | Path = MOUNTINFO_PATH,
        root: str = CONTAINER_ROOT,
        mountinfo_reader: Callable[[], str | None] | None = None,
        verdict_reader: Callable[[], str | None] | None = None,
        statvfs: Callable[[str], StatvfsReading] = os.statvfs,
    ) -> None:
        """Build the probe.

        Args:
            verdict_path: File the deployment writes its attestation to.
            mountinfo_path: This container's mount table.
            root: The mount point to measure.
            mountinfo_reader: Test seam replacing the mount-table read.
            verdict_reader: Test seam replacing the verdict read.
            statvfs: Test seam replacing the system call.
        """
        self._verdict_path = Path(verdict_path)
        self._mountinfo_path = Path(mountinfo_path)
        self._root = root
        self._mountinfo_reader = mountinfo_reader
        self._verdict_reader = verdict_reader
        self._statvfs = statvfs

    def sample(self) -> StorageSample | StorageMissing:
        """Take one reading, or say why there is none."""
        mountinfo = self._read_mountinfo()
        if mountinfo is None:
            return StorageMissing(reason=STORAGE_MISSING_SOURCE_UNAVAILABLE)
        upperdir = root_overlay_upperdir(mountinfo)
        if upperdir is None:
            return StorageMissing(reason=STORAGE_MISSING_UNSUPPORTED)
        try:
            reading = self._statvfs(self._root)
        except OSError:
            return StorageMissing(reason=STORAGE_MISSING_SOURCE_UNAVAILABLE)
        total_bytes = reading.f_frsize * reading.f_blocks
        free_bytes = reading.f_frsize * reading.f_bfree
        available_bytes = reading.f_frsize * reading.f_bavail
        if not storage_bytes_in_range(
            total_bytes=total_bytes, free_bytes=free_bytes, available_bytes=available_bytes
        ):
            return StorageMissing(reason=STORAGE_MISSING_SOURCE_UNAVAILABLE)
        entry = self._verdict_for(upperdir)
        if entry is None:
            return StorageMissing(reason=STORAGE_MISSING_UNVERIFIED)
        if entry.verdict == STORAGE_MISSING_UNSUPPORTED:
            return StorageMissing(reason=STORAGE_MISSING_UNSUPPORTED)
        if entry.verdict != VERDICT_VERIFIED:
            # Includes an unknown verdict from a newer script: a value this
            # code cannot read is not a value it may publish.
            return StorageMissing(reason=STORAGE_MISSING_UNVERIFIED)
        if entry.size_bytes != total_bytes or entry.mount_root is None:
            return StorageMissing(reason=STORAGE_MISSING_UNVERIFIED)
        return StorageSample(
            total_bytes=total_bytes,
            free_bytes=free_bytes,
            available_bytes=available_bytes,
            target=entry.mount_root,
        )

    def _read_mountinfo(self) -> str | None:
        """Read the container's mount table, or ``None``."""
        if self._mountinfo_reader is not None:
            return self._mountinfo_reader()
        return read_mountinfo_text(self._mountinfo_path)

    def _read_verdict(self) -> str | None:
        """Read the deployment's verdict file, or ``None``."""
        if self._verdict_reader is not None:
            return self._verdict_reader()
        return _read_text_file(
            self._verdict_path.parent, self._verdict_path.name, max_bytes=VERDICT_MAX_BYTES
        )

    def _verdict_for(self, upperdir: str) -> StorageVerdictEntry | None:
        """Look this runner's upper directory up in the verdict document."""
        text = self._read_verdict()
        if text is None:
            return None
        entries = parse_storage_verdict(text)
        if entries is None:
            return None
        return entries.get(upperdir)


class UnsupportedStorageProbe:
    """A probe for a name this version does not implement.

    Reports ``unsupported`` rather than falling back to the root filesystem:
    the deployment asked for a measurement this code does not know how to take,
    and measuring something else under that name would be a wrong number
    wearing the right label.
    """

    source = "unconfigured"

    def sample(self) -> StorageMissing:
        """Report that this deployment named a probe this build does not have."""
        return StorageMissing(reason=STORAGE_MISSING_UNSUPPORTED)


def read_mountinfo_text(path: str | Path) -> str | None:
    """Read a mount table, bounded and strictly decoded, or ``None``.

    One function rather than one per caller, because the deployment attests
    the reading the heartbeat will carry. A second reader with its own size
    ceiling, or its own idea of what an undecodable byte means, can call a host
    verified for a reading the runner then refuses to take: the log says the
    disk was measured and the page says it was not, with nothing connecting the
    two. A file this code cannot read whole and decode strictly is not the file
    it asked for, on either path.

    Args:
        path: The mount table to read.

    Returns:
        The decoded contents, or ``None`` when it cannot be used.
    """
    mountinfo = Path(path)
    return _read_text_file(mountinfo.parent, mountinfo.name)


def describe_storage_probe(
    *,
    mountinfo_path: str | Path = MOUNTINFO_PATH,
    root: str = CONTAINER_ROOT,
    statvfs: Callable[[str], StatvfsReading] = os.statvfs,
) -> dict[str, Any]:
    """Describe what this runner's probe currently sees, for the deployment.

    The deployment has to compare the container's own reading against the
    host's ``df``, and it has to be *this* reading: a shell re-implementation
    could agree with itself while both differ from what the runner reports.
    That is also why the mount table goes through :func:`read_mountinfo_text`,
    the reader the sampling path uses, rather than a second one of its own.
    Deliberately independent of the verdict file — this is the reading the
    verdict is about to be derived from, so consulting it here would be
    circular.

    Args:
        mountinfo_path: This container's mount table.
        root: The mount point to measure.
        statvfs: Test seam replacing the system call.

    Returns:
        A JSON-safe description. ``ok`` is false when the topology cannot be
        measured, with ``reason`` naming the missing-measurement vocabulary;
        when the mount table names a filesystem type, it is reported even on
        failure, because that is the fact the operator needs in the log.
    """
    mount: RootMount | None = None
    mountinfo = read_mountinfo_text(mountinfo_path)
    if mountinfo is not None:
        mount = find_root_mount(mountinfo)
    if mountinfo is None:
        return _describe_failure(STORAGE_MISSING_SOURCE_UNAVAILABLE, mount)
    if mount is None or mount.fstype != OVERLAY_FSTYPE or mount.upperdir is None:
        return _describe_failure(STORAGE_MISSING_UNSUPPORTED, mount)
    try:
        reading = statvfs(root)
    except OSError:
        return _describe_failure(STORAGE_MISSING_SOURCE_UNAVAILABLE, mount)
    total_bytes = reading.f_frsize * reading.f_blocks
    free_bytes = reading.f_frsize * reading.f_bfree
    available_bytes = reading.f_frsize * reading.f_bavail
    if not storage_bytes_in_range(
        total_bytes=total_bytes, free_bytes=free_bytes, available_bytes=available_bytes
    ):
        return _describe_failure(STORAGE_MISSING_SOURCE_UNAVAILABLE, mount)
    return {
        "ok": True,
        "reason": None,
        "fstype": mount.fstype,
        "upperdir": mount.upperdir,
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "available_bytes": available_bytes,
    }


def _describe_failure(reason: str, mount: RootMount | None) -> dict[str, Any]:
    """Return the failing shape :func:`describe_storage_probe` promises."""
    return {
        "ok": False,
        "reason": reason,
        "fstype": None if mount is None else mount.fstype,
        "upperdir": None if mount is None else mount.upperdir,
        "total_bytes": None,
        "free_bytes": None,
        "available_bytes": None,
    }


def build_storage_probe(
    environ: Mapping[str, str] | None = None,
    *,
    verdict_path: str | Path = DEFAULT_STORAGE_VERDICT_PATH,
) -> StorageProbe | None:
    """Return the probe this deployment declared, or ``None`` for ``off``.

    Args:
        environ: Environment to read; the process environment by default.
        verdict_path: Where the deployment writes its attestation.

    Returns:
        The probe, or ``None`` when storage measurement is switched off. An
        unrecognized name gets a probe that reports ``unsupported``: the
        deployment named a method this build does not have, and guessing which
        one it meant is how a page ends up describing the wrong filesystem.
    """
    source = os.environ if environ is None else environ
    raw = (source.get(ENV_STORAGE_PROBE) or "").strip().lower()
    if raw in {"", STORAGE_PROBE_OVERLAY_ROOT}:
        return OverlayRootStorageProbe(verdict_path=verdict_path)
    if raw in {STORAGE_PROBE_OFF, "0", "false", "no", "disabled"}:
        return None
    logger.warning(
        "agent_host_storage_probe_unknown",
        extra={"event": "agent_host_storage_probe_unknown", "storage_probe": raw[:64]},
    )
    return UnsupportedStorageProbe()


def main(argv: Sequence[str] | None = None) -> int:
    """Run this module's small command line — currently only ``describe``.

    The deployment script runs it inside each runner to attest the topology, so
    it prints JSON and never raises: a traceback would read as "the runner is
    broken" when the honest answer is "this host is not a supported topology".

    Args:
        argv: Argument vector without the program name; ``sys.argv[1:]`` when
            omitted.

    Returns:
        A process exit status. Only a usage error is non-zero; a probe that
        cannot measure reports ``ok: false`` in its document instead, because
        the deployment's diagnosis is the JSON.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["describe"]:
        print("usage: python -m cloud_agent_host.host_capacity describe", file=sys.stderr)
        return 2
    print(json.dumps(describe_storage_probe(), sort_keys=True))
    return 0


class CommandStatus(StrEnum):
    """How a bounded child process ended."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    LINE_LIMIT = "line_limit"


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one bounded child process.

    Attributes:
        status: Why it ended.
        stdout: Captured output, empty unless ``status`` is ``OK``. A capped
            run returns nothing rather than its prefix.
        stderr: A bounded prefix of stderr, for the log line.
    """

    status: CommandStatus
    stdout: str = ""
    stderr: str = ""


def run_bounded_command(
    argv: Sequence[str],
    *,
    timeout_s: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    max_lines: int,
    binary_check: Callable[[str], str | None] | None = None,
) -> CommandResult:
    """Run one child process with output read under a hard ceiling.

    The ceiling is enforced while reading, not after. ``communicate()`` would
    buffer whatever the child writes and only then let the caller decide it was
    too much, which is not a bound at all — a Docker daemon answering with a
    gigabyte of diagnostics would be fully resident before the first check.

    Args:
        argv: The exact argument vector. Never a shell string.
        timeout_s: Wall-clock budget for the whole child.
        max_stdout_bytes: Ceiling on captured stdout.
        max_stderr_bytes: Ceiling on captured stderr.
        max_lines: Ceiling on newline count in stdout.
        binary_check: Optional resolver; ``None`` means the binary is missing.

    Returns:
        The outcome. Over-budget and timed-out runs have had their process
        group killed and reaped before this returns.
    """
    if not argv:
        return CommandResult(status=CommandStatus.UNAVAILABLE)
    resolver = binary_check or shutil.which
    if resolver(argv[0]) is None:
        return CommandResult(status=CommandStatus.UNAVAILABLE)
    try:
        child = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return CommandResult(status=CommandStatus.UNAVAILABLE)
    return _drain_child(
        child,
        timeout_s=timeout_s,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        max_lines=max_lines,
    )


def _drain_child(
    child: subprocess.Popen[bytes],
    *,
    timeout_s: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    max_lines: int,
) -> CommandResult:
    """Read a running child's pipes until EOF, a ceiling, or the deadline."""
    deadline = time.monotonic() + timeout_s
    stdout = bytearray()
    stderr = bytearray()
    status: CommandStatus | None = None
    lines = 0
    selector = selectors.DefaultSelector()
    # The selector reports each registered stream back as whatever
    # ``register`` accepted (``FileDescriptorLike``), so the table it is read
    # through is keyed by that type rather than by the concrete pipes.
    streams: dict[FileDescriptorLike | None, bytearray] = {
        child.stdout: stdout,
        child.stderr: stderr,
    }
    try:
        for stream in streams:
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = CommandStatus.TIMEOUT
                break
            for key, _ in selector.select(timeout=remaining):
                sink = streams[key.fileobj]
                limit = max_stdout_bytes if sink is stdout else max_stderr_bytes
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(sink) + len(chunk) > limit:
                    status = (
                        CommandStatus.OUTPUT_LIMIT if sink is stdout else CommandStatus.UNAVAILABLE
                    )
                    break
                sink.extend(chunk)
                if sink is stdout:
                    lines += chunk.count(b"\n")
                    if lines > max_lines:
                        status = CommandStatus.LINE_LIMIT
                        break
            if status is not None:
                break
    finally:
        selector.close()
    if status is None:
        try:
            code = child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            status = CommandStatus.TIMEOUT
        else:
            status = CommandStatus.OK if code == 0 else CommandStatus.UNAVAILABLE
    if status is not CommandStatus.OK or child.poll() is None:
        _kill_child(child)
    if status is not CommandStatus.OK:
        return CommandResult(
            status=status,
            stderr=(bytes(stderr[:max_stderr_bytes]).decode("utf-8", "replace")).strip(),
        )
    return CommandResult(
        status=CommandStatus.OK,
        stdout=bytes(stdout).decode("utf-8", "replace"),
        stderr=bytes(stderr[:max_stderr_bytes]).decode("utf-8", "replace").strip(),
    )


def _kill_child(child: subprocess.Popen[bytes]) -> None:
    """Kill a child and everything it started, then reap it."""
    try:
        os.killpg(os.getpgid(child.pid), 9)
    except OSError:
        # The group is gone, or the pid was reaped between the two calls. Fall
        # back to the child alone rather than leaving it running.
        with contextlib.suppress(OSError):
            child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5.0)
    for stream in (child.stdout, child.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


@dataclass(frozen=True)
class SandboxInventory:
    """Entity counts of retained chat containers on one Docker host.

    Counts are of **containers**, not of memberships: a Fork adds a member to a
    group that already has one container, so counting memberships would report
    a machine as busier every time a user branched a chat.

    Attributes:
        namespace: The deployment namespace the counts are scoped to.
        status: ``available``, ``partial`` or ``unavailable``.
        running: Containers in ``running``.
        stopped: Containers in ``exited``.
        other_states: Fixed-key breakdown of every other Docker state, with
            ``None`` where the count could not be established.
        reason: Bounded identifier explaining a non-available status.
        unscoped_legacy_containers: Chat containers carrying no namespace
            label. They cannot be attributed safely and are therefore not in
            any count above.
        duplicate_sandbox_id: A sandbox id observed on more than one
            container. Both containers are counted, because both hold
            resources, but the condition is worth surfacing.
    """

    namespace: str | None
    status: str
    running: int | None
    stopped: int | None
    other_states: dict[str, int | None] = field(default_factory=dict)
    reason: str | None = None
    unscoped_legacy_containers: int | None = None
    duplicate_sandbox_id: bool = False

    def as_payload(self) -> dict[str, Any]:
        """Return the fixed wire shape of this inventory."""
        return {
            "namespace": self.namespace,
            "status": self.status,
            "running": self.running,
            "stopped": self.stopped,
            "other_states": {name: self.other_states.get(name) for name in OTHER_STATES},
            "reason": self.reason,
            "unscoped_legacy_containers": self.unscoped_legacy_containers,
            "duplicate_sandbox_id": self.duplicate_sandbox_id,
        }


def unavailable_inventory(*, namespace: str | None, reason: str) -> SandboxInventory:
    """Return an inventory that could not be established at all.

    Args:
        namespace: The namespace the attempt was scoped to, if any.
        reason: Bounded identifier for the failure.

    Returns:
        An inventory whose counts are ``None``. Never zero: a failed listing
        and an empty host are different facts, and the page says so.
    """
    return SandboxInventory(
        namespace=namespace,
        status=INVENTORY_UNAVAILABLE,
        running=None,
        stopped=None,
        other_states=dict.fromkeys(OTHER_STATES),
        reason=reason,
    )


def docker_list_argv(*, docker_binary: str) -> list[str]:
    """Return the exact read-only listing command this module runs.

    Args:
        docker_binary: The Docker CLI to invoke.

    Returns:
        An argument vector. No shell, no ``--size``, no inspection, no state
        change: the four fields are read straight off the list, because a
        per-container inspect is what makes inventory collection expensive
        enough to be a lifecycle hazard.
    """
    template = (
        '{"id":{{json .ID}}'
        f',"namespace":{{{{json (.Label "{CHAT_NAMESPACE_LABEL}")}}}}'
        f',"sandbox_id":{{{{json (.Label "{CHAT_SANDBOX_ID_LABEL}")}}}}'
        ',"state":{{json .State}}}'
    )
    return [
        docker_binary,
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        f"label={CHAT_SANDBOX_LABEL}=true",
        "--format",
        template,
    ]


def parse_docker_rows(stdout: str) -> list[dict[str, Any]]:
    """Parse the Go-template JSON lines the listing emits.

    Args:
        stdout: Raw child output.

    Returns:
        One mapping per listed container.

    Raises:
        ValueError: Any line is blank or not a JSON object with the four
            expected keys. A malformed listing is reported as unavailable
            rather than as the smaller count that happened to parse.
    """
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("listing line is not JSON") from exc
        if not isinstance(row, dict):
            raise ValueError("listing line is not an object")
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("listing line has no container id")
        if row.get("namespace") is not None and not isinstance(row["namespace"], str):
            raise ValueError("listing line has a non-string namespace")
        rows.append(row)
    return rows


def build_inventory(rows: Sequence[Mapping[str, Any]], *, namespace: str) -> SandboxInventory:
    """Count retained containers from already-parsed listing rows.

    Args:
        rows: Parsed listing rows.
        namespace: This deployment's namespace. Only exact matches count.

    Returns:
        The inventory. Foreign namespaces are excluded silently — they belong
        to a sibling stack on the same daemon and are not this deployment's
        business — while containers carrying *no* namespace cannot be
        attributed at all and mark the result partial.
    """
    if not namespace:
        return unavailable_inventory(namespace=None, reason=REASON_NAMESPACE_UNSET)
    counts: dict[str, int] = {RUNNING_STATE: 0, STOPPED_STATE: 0}
    counts.update(dict.fromkeys(OTHER_STATES, 0))
    unscoped = 0
    unknown = False
    seen_sandbox_ids: set[str] = set()
    duplicate = False
    for row in rows:
        reported = row.get("namespace")
        if isinstance(reported, str) and reported:
            if reported != namespace:
                continue
        elif reported == "" or reported is None:
            # No namespace label: either a container from before the label
            # existed, or one a sibling stack created. Counting it into this
            # deployment would be a guess, and deleting it to remove the
            # ambiguity is not monitoring's call to make.
            unscoped += 1
            continue
        else:  # pragma: no cover - ``parse_docker_rows`` rejects these
            continue
        sandbox_id = row.get("sandbox_id")
        if isinstance(sandbox_id, str) and sandbox_id:
            if sandbox_id in seen_sandbox_ids:
                duplicate = True
            seen_sandbox_ids.add(sandbox_id)
        state = row.get("state")
        if isinstance(state, str) and state in counts:
            counts[state] += 1
        else:
            unknown = True
    if unknown:
        return SandboxInventory(
            namespace=namespace,
            status=INVENTORY_PARTIAL,
            running=counts[RUNNING_STATE],
            stopped=counts[STOPPED_STATE],
            other_states={name: counts[name] for name in OTHER_STATES},
            reason=REASON_UNKNOWN_STATE,
            unscoped_legacy_containers=unscoped,
            duplicate_sandbox_id=duplicate,
        )
    if unscoped:
        return SandboxInventory(
            namespace=namespace,
            status=INVENTORY_PARTIAL,
            running=counts[RUNNING_STATE],
            stopped=counts[STOPPED_STATE],
            other_states={name: counts[name] for name in OTHER_STATES},
            reason=REASON_UNSCOPED_LEGACY_CONTAINERS,
            unscoped_legacy_containers=unscoped,
            duplicate_sandbox_id=duplicate,
        )
    return SandboxInventory(
        namespace=namespace,
        status=INVENTORY_AVAILABLE,
        running=counts[RUNNING_STATE],
        stopped=counts[STOPPED_STATE],
        other_states={name: counts[name] for name in OTHER_STATES},
        unscoped_legacy_containers=0,
        duplicate_sandbox_id=duplicate,
    )


@dataclass(frozen=True)
class HostMeasurement:
    """One observation of the host, before it is shaped for the wire.

    Attributes:
        sample_id: Identity of this observation, used to reject replays.
        linux_boot_id: The Docker host's boot id, or ``None`` when it could
            not be read.
        cpu_percent: Non-idle share of the interval, or ``None``.
        cpu_logical_count: CPUs the host accounted for.
        cpu_window_seconds: Length of the interval the CPU figure averages.
        cpu_missing_reason: Why the CPU figure is absent.
        memory_total_bytes: ``MemTotal``.
        memory_available_bytes: ``MemAvailable``.
        memory_missing_reason: Why the memory figures are absent.
        storage_total_bytes: Size of the filesystem holding writable layers.
        storage_free_bytes: Free bytes there, reserve included.
        storage_available_bytes: Free bytes an unprivileged writer may use.
        storage_source: Which probe produced the storage reading.
        storage_target: Attested host mount root of that filesystem.
        storage_missing_reason: Why the storage figures are absent.
        inventory: The container count.
    """

    sample_id: str
    linux_boot_id: str | None
    cpu_percent: float | None
    cpu_logical_count: int | None
    cpu_window_seconds: float | None
    cpu_missing_reason: str | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    memory_missing_reason: str | None
    storage_total_bytes: int | None
    storage_free_bytes: int | None
    storage_available_bytes: int | None
    storage_source: str | None
    storage_target: str | None
    storage_missing_reason: str | None
    inventory: SandboxInventory

    def as_payload(self) -> dict[str, Any]:
        """Return the ``capacity`` block exactly as the heartbeat sends it.

        The storage sub-block is present only when this round produced a
        reading. ``None`` plus a reason is the honest shape for "there is no
        number" — copying the previous round's value forward would make a
        stopped collector look like a live one.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "sample_id": self.sample_id,
            "linux_boot_id": self.linux_boot_id,
            "source": SOURCE,
            "cpu_percent": self.cpu_percent,
            "cpu_logical_count": self.cpu_logical_count,
            "cpu_window_seconds": self.cpu_window_seconds,
            "memory_total_bytes": self.memory_total_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "storage": self._storage_payload(),
            "inventory": self.inventory.as_payload(),
            "missing": {
                "cpu": self.cpu_missing_reason,
                "memory": self.memory_missing_reason,
                "storage": self.storage_missing_reason,
            },
        }

    def _storage_payload(self) -> dict[str, Any] | None:
        """Return the storage sub-block, or ``None`` when there is no reading."""
        if self.storage_missing_reason is not None:
            return None
        if self.storage_source is None:
            # A sample without its source cannot be labelled on the wire, and
            # an unlabelled reading is not one the page may attribute.
            return None
        return {
            "source": self.storage_source,
            "target": self.storage_target,
            "total_bytes": self.storage_total_bytes,
            "free_bytes": self.storage_free_bytes,
            "available_bytes": self.storage_available_bytes,
        }


def _read_text_file(root: Path, name: str, *, max_bytes: int = PROC_MAX_BYTES) -> str | None:
    """Read one fixed file, or ``None`` if it cannot be used.

    Args:
        root: Directory holding the mounted host files.
        name: File name inside it.
        max_bytes: Refuse anything longer; a proc file is a few kilobytes and
            an enormous one is not the file this code asked for.

    Returns:
        The decoded contents, or ``None`` on any error.
    """
    try:
        data = (root / name).read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class HostCapacityCollector:
    """Samples this host's CPU, memory and container count on a daemon thread.

    One instance per runner process. Each replica keeps its own CPU baseline,
    because the counters are per-boot monotonic and two processes cannot share
    a delta they did not both observe. The control plane stores one latest
    report per host; it never adds replicas' raw counters together.
    """

    def __init__(
        self,
        *,
        namespace: str,
        proc_root: str | Path = DEFAULT_PROC_ROOT,
        docker_binary: str = "docker",
        interval_s: float = SAMPLE_INTERVAL_S,
        reader: Callable[[str], str | None] | None = None,
        command_runner: Callable[[Sequence[str]], CommandResult] | None = None,
        storage_probe: StorageProbe | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build a collector.

        Args:
            namespace: This deployment's sandbox namespace; the only
                containers counted are the ones labelled with it.
            proc_root: Directory the host's proc files are mounted under.
            docker_binary: Docker CLI to invoke.
            interval_s: Seconds between samples on the background thread.
            reader: Test seam replacing the proc-file read.
            command_runner: Test seam replacing the bounded child process.
            storage_probe: The declared storage probe, or ``None`` when this
                deployment turned storage measurement off. ``None`` is
                reported as ``disabled`` rather than omitted, so the page can
                tell a switched-off metric from an old runner's silence.
            clock: Monotonic clock, for sample ages.
        """
        self._namespace = namespace
        self._proc_root = Path(proc_root)
        self._docker_binary = docker_binary
        self._interval_s = interval_s if interval_s > 0 else SAMPLE_INTERVAL_S
        self._reader = reader or self._read_proc
        self._command_runner = command_runner or self._run_docker
        self._storage_probe = storage_probe
        self._clock = clock
        self._lock = threading.Lock()
        self._measurement: HostMeasurement | None = None
        self._sampled_monotonic: float | None = None
        self._cpu_baseline: tuple[str | None, int, tuple[int, ...], float] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read_proc(self, name: str) -> str | None:
        """Read one host proc file from the configured root."""
        return _read_text_file(self._proc_root, name)

    def _run_docker(self, argv: Sequence[str]) -> CommandResult:
        """Run one bounded Docker CLI call."""
        return run_bounded_command(
            argv,
            timeout_s=DOCKER_TIMEOUT_S,
            max_stdout_bytes=DOCKER_MAX_STDOUT_BYTES,
            max_stderr_bytes=DOCKER_MAX_STDERR_BYTES,
            max_lines=DOCKER_MAX_LINES,
        )

    def start(self) -> None:
        """Begin sampling in the background."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-host-capacity")
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and join the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self) -> None:
        """Sample until stopped, never letting one failure end the loop."""
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                # Monitoring must not be able to kill its own thread: a thread
                # that dies is never restarted, and the page would then show
                # "no data" for the life of the process.
                logger.warning(
                    "agent_host_capacity_sample_failed",
                    exc_info=True,
                    extra={"event": "agent_host_capacity_sample_failed"},
                )
            self._stop.wait(self._interval_s)

    def latest(self) -> tuple[dict[str, Any] | None, int | None]:
        """Return the newest observation and its age.

        Returns:
            ``(payload, age_ms)`` where the payload carries a freshly computed
            ``sample_age_ms``. ``(None, None)`` before the first sample, and
            also once the cached observation is older than the protocol
            allows — an old sample is not re-stamped with the current time,
            which is what makes a stalled collector visible as staleness
            rather than as a live reading.
        """
        with self._lock:
            measurement = self._measurement
            sampled_at = self._sampled_monotonic
        if measurement is None or sampled_at is None:
            return None, None
        age_ms = max(0, int((self._clock() - sampled_at) * 1000))
        if age_ms > MAX_SAMPLE_AGE_MS:
            return None, None
        payload = measurement.as_payload()
        payload["sample_age_ms"] = age_ms
        return payload, age_ms

    def sample(self) -> HostMeasurement:
        """Take one observation and publish it as the latest.

        Returns:
            The observation, which is also what :meth:`latest` will hand out.
        """
        sampled_at = self._clock()
        boot_id = self._sample_boot_id()
        cpu_percent, cpu_count, cpu_window, cpu_reason = self._sample_cpu(
            boot_id=boot_id, sampled_at=sampled_at
        )
        total_bytes, available_bytes, memory_reason = self._sample_memory(boot_id=boot_id)
        storage = self._sample_storage()
        storage_source = None
        if isinstance(storage, StorageSample) and self._storage_probe is not None:
            storage_source = self._storage_probe.source
        inventory = self._sample_inventory()
        measurement = HostMeasurement(
            sample_id=str(uuid.uuid4()),
            linux_boot_id=boot_id,
            cpu_percent=cpu_percent,
            cpu_logical_count=cpu_count,
            cpu_window_seconds=cpu_window,
            cpu_missing_reason=cpu_reason,
            memory_total_bytes=total_bytes,
            memory_available_bytes=available_bytes,
            memory_missing_reason=memory_reason,
            storage_total_bytes=None
            if isinstance(storage, StorageMissing)
            else storage.total_bytes,
            storage_free_bytes=None if isinstance(storage, StorageMissing) else storage.free_bytes,
            storage_available_bytes=(
                None if isinstance(storage, StorageMissing) else storage.available_bytes
            ),
            storage_source=storage_source,
            storage_target=None if isinstance(storage, StorageMissing) else storage.target,
            storage_missing_reason=storage.reason if isinstance(storage, StorageMissing) else None,
            inventory=inventory,
        )
        with self._lock:
            self._measurement = measurement
            self._sampled_monotonic = sampled_at
        return measurement

    def _sample_boot_id(self) -> str | None:
        """Read the host's boot id, the identity every other field depends on."""
        text = self._reader(PROC_BOOT_ID_FILE)
        return None if text is None else parse_boot_id(text)

    def _sample_cpu(
        self, *, boot_id: str | None, sampled_at: float
    ) -> tuple[float | None, int | None, float | None, str | None]:
        """Compute the CPU figure from the previous sample's baseline.

        Returns:
            ``(percent, logical_count, window_seconds, missing_reason)``. Every
            rejection path clears the baseline, so the next sample starts over
            instead of averaging across the discontinuity.
        """
        if boot_id is None:
            # Without a boot id there is no way to know whose counters these
            # are. Reporting them anyway would let two hosts' numbers be added
            # together by a page that cannot tell them apart.
            self._cpu_baseline = None
            return None, None, None, CPU_MISSING_SOURCE_UNAVAILABLE
        text = self._reader(PROC_STAT_FILE)
        if text is None:
            self._cpu_baseline = None
            return None, None, None, CPU_MISSING_SOURCE_UNAVAILABLE
        try:
            counters = parse_cpu_stat(text)
        except ValueError:
            self._cpu_baseline = None
            return None, None, None, CPU_MISSING_INVALID_SAMPLE
        previous = self._cpu_baseline
        self._cpu_baseline = (boot_id, counters.logical_count, counters.fields, sampled_at)
        if previous is None:
            return None, counters.logical_count, None, CPU_MISSING_WARMING_UP
        previous_boot, previous_count, previous_fields, previous_at = previous
        window = sampled_at - previous_at
        if previous_boot != boot_id or previous_count != counters.logical_count:
            # The machine rebooted, or CPUs were added or removed under us: the
            # counters did not continue, so a delta across this point is
            # meaningless.
            return None, counters.logical_count, None, CPU_MISSING_COUNTER_RESET
        if window <= 0 or window > MAX_CPU_WINDOW_S:
            return None, counters.logical_count, None, CPU_MISSING_COUNTER_RESET
        if any(
            current < before
            for current, before in zip(counters.fields, previous_fields, strict=True)
        ):
            return None, counters.logical_count, None, CPU_MISSING_COUNTER_RESET
        percent = cpu_busy_percent(
            delta_total=counters.total - sum(previous_fields),
            delta_idle=counters.idle - (previous_fields[3] + previous_fields[4]),
        )
        if percent is None:
            return None, counters.logical_count, None, CPU_MISSING_INVALID_SAMPLE
        return percent, counters.logical_count, window, None

    def _sample_memory(self, *, boot_id: str | None) -> tuple[int | None, int | None, str | None]:
        """Compute used memory from the host's meminfo.

        Returns:
            ``(total_bytes, available_bytes, missing_reason)``.
        """
        if boot_id is None:
            return None, None, MEMORY_MISSING_SOURCE_UNAVAILABLE
        text = self._reader(PROC_MEMINFO_FILE)
        if text is None:
            return None, None, MEMORY_MISSING_SOURCE_UNAVAILABLE
        total_bytes, available_bytes = parse_meminfo(text)
        if total_bytes is None:
            return None, None, MEMORY_MISSING_INVALID_SAMPLE
        if available_bytes is None:
            # No fallback to MemFree: it answers a different question, and the
            # number it produces is wrong on every healthy machine.
            return None, None, MEMORY_MISSING_SOURCE_UNAVAILABLE
        if memory_usage(total_bytes=total_bytes, available_bytes=available_bytes) is None:
            return None, None, MEMORY_MISSING_INVALID_SAMPLE
        return total_bytes, available_bytes, None

    def _sample_storage(self) -> StorageSample | StorageMissing:
        """Take one storage reading, or say why there is none.

        Every failure mode is contained here: a probe that raises costs this
        round its storage figures and nothing else — not the CPU, not the
        memory, not the container count, and not the heartbeat they ride. The
        call is a local filesystem's ``statvfs`` plus one small file read, so
        it runs in-process rather than in a child, and the collector's own
        per-field reasons are what keeps its honesty boundary visible.
        """
        if self._storage_probe is None:
            return StorageMissing(reason=STORAGE_MISSING_DISABLED)
        try:
            return self._storage_probe.sample()
        except Exception:
            logger.warning(
                "agent_host_storage_probe_failed",
                exc_info=True,
                extra={"event": "agent_host_storage_probe_failed"},
            )
            return StorageMissing(reason=STORAGE_MISSING_SOURCE_UNAVAILABLE)

    def _sample_inventory(self) -> SandboxInventory:
        """Count retained chat containers with one read-only listing."""
        if not self._namespace:
            return unavailable_inventory(namespace=None, reason=REASON_NAMESPACE_UNSET)
        result = self._command_runner(docker_list_argv(docker_binary=self._docker_binary))
        if result.status is not CommandStatus.OK:
            return unavailable_inventory(
                namespace=self._namespace, reason=_reason_for(result.status)
            )
        try:
            rows = parse_docker_rows(result.stdout)
        except ValueError:
            return unavailable_inventory(
                namespace=self._namespace, reason=REASON_DOCKER_MALFORMED_OUTPUT
            )
        return build_inventory(rows, namespace=self._namespace)


def _reason_for(status: CommandStatus) -> str:
    """Map a child-process outcome onto the inventory reason vocabulary."""
    if status is CommandStatus.TIMEOUT:
        return REASON_DOCKER_TIMEOUT
    if status is CommandStatus.OUTPUT_LIMIT:
        return REASON_DOCKER_OUTPUT_LIMIT
    if status is CommandStatus.LINE_LIMIT:
        return REASON_DOCKER_LINE_LIMIT
    return REASON_DOCKER_UNAVAILABLE


# Last in the file, not beside :func:`main`: this runs on import under
# ``python -m``, so module execution stops here. Anything defined below it
# would not exist yet when the command it starts goes looking for it.
if __name__ == "__main__":  # pragma: no cover - reached through the module CLI
    raise SystemExit(main())
