"""Host-side contract for the trusted eBPF observer inside a Kata guest.

Docker statistics are periodic container totals.  They cannot prove that a
35 ms child existed, and a shared retained container cannot assign its totals
to one member anyway.  The observer this module speaks to is different: a
trusted, guest-root process attaches to the *guest kernel's* process lifecycle
tracepoints and records fork/exec/exit events.  A process that begins and ends
between two sampler ticks therefore still produces events.

The agent never receives BPF capabilities.  ``ChatSandboxManager`` starts the
reviewed image helper through a Kata-only privileged exec, while every agent
process remains a capability-dropped member UID.  This module intentionally
contains no Docker calls; it validates the helper's bounded journal and turns
events into the wire rows the control plane may store.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent_host.tool_windows import TOOL_MARK_HELPER, ToolMark

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.tool_windows import ToolWindow

#: The observer protocol this host speaks. It must equal
#: ``FI_OBSERVER_VERSION`` in ``deploy/sandbox/guest-observer.h``: the guest
#: stamps it into its status object and into every journal record, and both
#: readers here refuse anything else. They are two halves of one handshake,
#: and ``tests/unit/test_guest_observer.py`` pins them together.
GUEST_OBSERVER_VERSION = 2
GUEST_OBSERVER_SOURCE = "kata-guest-ebpf-v1"
GUEST_OBSERVER_ROOT = "/run/freeinference-observer"
GUEST_OBSERVER_EVENTS = f"{GUEST_OBSERVER_ROOT}/events.jsonl"
GUEST_OBSERVER_STATUS = f"{GUEST_OBSERVER_ROOT}/status.json"
GUEST_OBSERVER_BINARY = "/usr/local/libexec/freeinference-guest-observer"
#: The kernel's own boot identifier. A task's start time and TID repeat across
#: boots, so this is what makes an instance key unique.
GUEST_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

# One member turn may read at most this much of the group-wide journal.  The
# observer itself has a larger lifetime cap; this one bounds one sandbox manager result.
MAX_OBSERVATION_BYTES = 4 * 1024 * 1024
MAX_OBSERVED_PROCESSES = 4096
MAX_OBSERVED_TOOL_MARKS = 1000
MAX_COMM_BYTES = 64
MAX_EXECUTABLE_BYTES = 512

# Restart an idle retained/shared observer before the C helper's 64 MiB hard
# cap.  The 16 MiB reserve covers ordinary activity while a later turn is
# active; an individual hostile storm is still bounded and reported partial.
GUEST_OBSERVER_ROTATE_BYTES = 48 * 1024 * 1024


class GuestObserverProtocolError(ValueError):
    """The trusted helper returned a malformed or out-of-contract journal."""


class GuestObserverRuntimeError(RuntimeError):
    """The fixed in-guest helper could not be started, fenced, or read."""


@dataclass(frozen=True)
class GuestObserverStatus:
    """One atomic status snapshot written after a ring-buffer drain."""

    pid: int
    poll_seq: int
    event_seq: int
    lost: int
    journal_dropped: int
    journal_bytes: int = 0
    # The observer publishes the identity from its already-open fd. A PID can
    # be reused; this pair proves that a closing fence still names the exact
    # append-only journal that the opening fence named.
    journal_dev: int = 0
    journal_ino: int = 0


@dataclass(frozen=True)
class GuestObserverSnapshot:
    """Status and journal position sampled against the guest monotonic clock."""

    status: GuestObserverStatus
    journal_size: int
    guest_mono_ns: int


@dataclass(frozen=True)
class GuestObserverCursor:
    """The exact journal/loss fence captured before a member prompt is sent."""

    uid: int
    offset: int = 0
    lost: int = 0
    journal_dropped: int = 0
    poll_seq: int = 0
    observer_pid: int = 0
    event_seq: int = 0
    journal_dev: int = 0
    journal_ino: int = 0
    guest_mono_ns: int = 0
    # Add this to a guest monotonic timestamp to place it on the sandbox manager host's
    # monotonic axis.  ``clock_error_ns`` is half the Docker-exec round trip;
    # attribution widens by it rather than claiming false boundary precision.
    mono_offset_ns: int = 0
    clock_error_ns: int = 0
    available: bool = False
    detail: str | None = None
    #: The guest boot this fence belongs to. A thread's identity is only
    #: unique within one boot, so a marker that survives a guest restart must
    #: not be able to make an old instance look like a new one.
    guest_boot_id: str = ""
    #: Set when this cursor also fences one member execution window. The
    #: observation then also carries that window's live census cycles.
    measurement_id: str | None = None

    @classmethod
    def unavailable(cls, uid: int, detail: str) -> GuestObserverCursor:
        """An explicit unavailable fence; an empty event list is not success."""
        return cls(uid=uid, detail=detail[:300])


@dataclass(frozen=True)
class GuestProcess:
    """One process identity reconstructed from loss-aware lifecycle events."""

    process_seq: int
    uid: int
    pid: int
    tgid: int
    ppid: int | None
    comm: str
    executable: str | None
    started_guest_mono_ns: int
    exited_guest_mono_ns: int | None
    started_host_mono: float
    exited_host_mono: float | None
    exit_code: int | None
    observed_fork: bool
    observed_exec: bool
    observed_exit: bool
    # What the process consumed, read by the guest kernel as each of its
    # threads exited and folded into the group leader here. Null until the
    # leader's exit was observed, and null for any counter this kernel does
    # not account, so a process that outlived the turn reports no consumption
    # rather than zero. CPU is the group's own time and excludes children,
    # which have rows of their own; the peak is the group's high-water RSS.
    cpu_usec: int | None = None
    mem_peak_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    io_rchar_bytes: int | None = None
    io_wchar_bytes: int | None = None

    @property
    def duration_ms(self) -> int | None:
        """Observed lifetime, or null when the process outlived the turn."""
        if self.exited_guest_mono_ns is None:
            return None
        elapsed = self.exited_guest_mono_ns - self.started_guest_mono_ns
        return max(0, round(elapsed / 1_000_000))


@dataclass(frozen=True)
class GuestObservation:
    """One member's bounded process-event window."""

    source: str | None
    complete: bool
    lost_events: int
    truncated: bool
    processes: tuple[GuestProcess, ...] = ()
    # Guest-root accepted boundary records, each carrying the guest's own
    # monotonic reading and its bridged position on the sandbox manager's clock. They
    # share the process journal's UID/loss fence.
    tool_marks: tuple[ToolMark, ...] = ()
    origin_guest_mono_ns: int = 0
    clock_error_ns: int = 0
    detail: str | None = None
    #: Live census cycles for the member windows this slice covers, already
    #: filtered to the member's own UID. They ride the observation because the
    #: turn already read this journal slice: a window that read it a second
    #: time would pay twice for the same bytes and could see a different slice.
    window_samples: tuple[Any, ...] = ()
    #: The raw journal this slice was parsed from. ``processes`` below is
    #: folded per thread group, which is the right shape for per-call
    #: attribution and the wrong one for a per-thread resource window: a
    #: group-summed figure cannot be reconciled against per-thread live
    #: readings without charging one thread's work to another. The window
    #: reducer reads the thread-level records from here instead.
    journal_payload: bytes = b""

    @classmethod
    def unavailable(
        cls,
        detail: str | None,
        *,
        source: str | None = None,
    ) -> GuestObservation:
        """Return no rows while preserving why zero rows prove nothing."""
        return cls(
            source=source,
            complete=False,
            lost_events=0,
            truncated=False,
            detail=(detail or "guest observer unavailable")[:300],
        )


class GuestObserverController:
    """Drive one trusted observer through a container-specific exec transport.

    The lifecycle and parsing contract is shared by throwaway, retained, and
    shared-live Kata containers.  Docker identity checks stay with the caller:
    this class receives only two fixed exec seams and never accepts an argv
    from a job.  Keeping the security-sensitive helper scripts here prevents
    the three lifecycle paths from drifting into subtly different fences.
    """

    def __init__(
        self,
        *,
        capture: Callable[[list[str], float], Any],
        privileged: Callable[[list[str], bool, float], Any],
        lock: threading.Lock | threading.RLock | None = None,
    ) -> None:
        self._capture = capture
        self._privileged = privileged
        self._lock = lock or threading.Lock()
        #: Cached after the first successful read: the boot cannot change while
        #: this controller is talking to the same observer, and a restart
        #: produces a new controller anyway.
        self._boot_id: str | None = None

    @staticmethod
    def _stderr(result: Any) -> str:
        value = getattr(result, "stderr", "")
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value or "")

    @staticmethod
    def _stdout(result: Any) -> bytes:
        value = getattr(result, "stdout", b"")
        if isinstance(value, bytes):
            return value
        return str(value or "").encode()

    def snapshot(
        self,
        *,
        after_poll_seq: int | None = None,
        wait_s: float = 3.0,
    ) -> GuestObserverSnapshot:
        """Read one atomic status/size/guest-clock tuple from the root journal."""
        bounded_wait = min(3.0, max(0.0, wait_s))
        script = (
            "import json, os, sys, time\n"
            "status_path, events_path, expected_binary = sys.argv[1:4]\n"
            "wanted = int(sys.argv[4])\n"
            "deadline = time.monotonic() + float(sys.argv[5])\n"
            "while True:\n"
            "    try:\n"
            "        with open(status_path, encoding='utf-8') as handle:\n"
            "            status = json.load(handle)\n"
            "        pid = int(status.get('pid', 0))\n"
            "        if pid <= 0 or not os.path.exists(f'/proc/{pid}'):\n"
            "            raise RuntimeError('observer process is not alive')\n"
            "        try:\n"
            "            observer_alive = os.path.samefile(f'/proc/{pid}/exe', expected_binary)\n"
            "        except OSError:\n"
            "            observer_alive = False\n"
            "        if not observer_alive:\n"
            "            raise RuntimeError('observer pid does not name the trusted binary')\n"
            "        if wanted < 0 or int(status.get('poll_seq', -1)) > wanted:\n"
            "            break\n"
            "    except (OSError, ValueError, TypeError, json.JSONDecodeError):\n"
            "        status = None\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise RuntimeError('observer status did not advance')\n"
            "    time.sleep(0.01)\n"
            "size = int(status.get('journal_bytes', -1))\n"
            "journal_dev = int(status.get('journal_dev', -1))\n"
            "journal_ino = int(status.get('journal_ino', -1))\n"
            "journal = os.stat(events_path)\n"
            "actual_size = int(journal.st_size)\n"
            "if (size < 0 or actual_size < size or journal_dev < 0 or journal_ino <= 0 "
            "        or int(journal.st_dev) != journal_dev "
            "        or int(journal.st_ino) != journal_ino):\n"
            "    raise RuntimeError('observer journal is behind its published fence')\n"
            "print(json.dumps({'status': status, 'journal_size': size, "
            "'guest_mono_ns': time.monotonic_ns()}, separators=(',', ':')))\n"
        )
        result = self._capture(
            [
                "python3",
                "-c",
                script,
                GUEST_OBSERVER_STATUS,
                GUEST_OBSERVER_EVENTS,
                GUEST_OBSERVER_BINARY,
                str(-1 if after_poll_seq is None else after_poll_seq),
                str(bounded_wait),
            ],
            max(5.0, bounded_wait + 2.0),
        )
        if getattr(result, "returncode", 1) != 0:
            raise GuestObserverRuntimeError(
                "guest observer snapshot failed: " + self._stderr(result)[-300:]
            )
        try:
            return parse_snapshot(self._stdout(result))
        except GuestObserverProtocolError as exc:
            raise GuestObserverRuntimeError(str(exc)) from exc

    def ensure(self) -> None:
        """Start the reviewed eBPF helper and wait until every probe is attached."""
        try:
            self.snapshot(wait_s=0)
            return
        except GuestObserverRuntimeError:
            pass
        with self._lock:
            try:
                self.snapshot(wait_s=0)
                return
            except GuestObserverRuntimeError:
                self.start()

    def start(self) -> None:
        """Replace one stale singleton; the caller holds the shared start lock."""
        cleanup = (
            "import json, os, signal, sys, time\n"
            "status, events, expected_binary = sys.argv[1:4]\n"
            "pid = 0\n"
            "def is_expected_process():\n"
            "    if pid <= 1:\n"
            "        return False\n"
            "    try:\n"
            "        return os.path.samefile(f'/proc/{pid}/exe', expected_binary)\n"
            "    except OSError:\n"
            "        return False\n"
            "try:\n"
            "    with open(status, encoding='utf-8') as handle:\n"
            "        pid = int(json.load(handle).get('pid', 0))\n"
            "    if is_expected_process():\n"
            "        os.kill(pid, signal.SIGTERM)\n"
            "except (OSError, ValueError, TypeError, json.JSONDecodeError):\n"
            "    pass\n"
            "deadline = time.monotonic() + 1.0\n"
            "while is_expected_process() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "if is_expected_process():\n"
            "    os.kill(pid, signal.SIGKILL)\n"
            "    deadline = time.monotonic() + 1.0\n"
            "    while is_expected_process() and time.monotonic() < deadline:\n"
            "        time.sleep(0.01)\n"
            "if is_expected_process():\n"
            "    raise RuntimeError('stale guest observer did not exit')\n"
            "for item in (status, events):\n"
            "    try:\n"
            "        os.unlink(item)\n"
            "    except FileNotFoundError:\n"
            "        pass\n"
        )
        cleaned = self._privileged(
            [
                "python3",
                "-c",
                cleanup,
                GUEST_OBSERVER_STATUS,
                GUEST_OBSERVER_EVENTS,
                GUEST_OBSERVER_BINARY,
            ],
            False,
            30.0,
        )
        if getattr(cleaned, "returncode", 1) != 0:
            raise GuestObserverRuntimeError(
                "stale guest observer cleanup failed: " + self._stderr(cleaned)[-300:]
            )
        started = self._privileged([GUEST_OBSERVER_BINARY], True, 30.0)
        if getattr(started, "returncode", 1) != 0:
            raise GuestObserverRuntimeError(
                "guest observer could not start: " + self._stderr(started)[-300:]
            )
        deadline = time.monotonic() + 5.0
        problem = "guest observer did not become ready"
        while time.monotonic() < deadline:
            try:
                self.snapshot()
                return
            except GuestObserverRuntimeError as exc:
                problem = str(exc)
                time.sleep(0.05)
        raise GuestObserverRuntimeError(problem)

    def rotate_if_needed(self, *, threshold: int = GUEST_OBSERVER_ROTATE_BYTES) -> bool:
        """Reset an idle lifetime journal before its hard cap is exhausted.

        The caller must own the lifecycle guarantee that no cursor is active.
        This method serializes against starts, rechecks the published fence,
        and returns whether it replaced the observer.
        """
        bounded_threshold = max(1, int(threshold))
        with self._lock:
            try:
                snapshot = self.snapshot(wait_s=0)
            except Exception:
                # ``begin`` will perform normal stale/missing recovery. A
                # failed read is not evidence that a live journal is large.
                return False
            if snapshot.journal_size < bounded_threshold:
                return False
            try:
                self.start()
            except Exception:
                # Rotation is telemetry maintenance, never a reason to reject
                # the user's next turn. ``begin`` will retry or return an
                # explicit unavailable cursor.
                return False
            return True

    def guest_boot_id(self) -> str:
        """This guest's boot identifier, or an empty string if unreadable.

        A task's identity is only unique within one boot: after a guest
        restart the same start time and TID can recur. The reader carries the
        boot with every instance key for that reason, and an unreadable
        identifier is reported as empty rather than invented.
        """
        with self._lock:
            if self._boot_id is not None:
                return self._boot_id
        boot_id = ""
        try:
            result = self._capture(["cat", GUEST_BOOT_ID_PATH], 5.0)
            if getattr(result, "returncode", 1) == 0:
                boot_id = self._stdout(result).decode("utf-8", "replace").strip()[:64]
        except Exception:
            boot_id = ""
        with self._lock:
            self._boot_id = boot_id
        return boot_id

    def begin(
        self,
        *,
        user_uid: int,
        restart_if_missing: bool = True,
        measurement_id: str | None = None,
    ) -> GuestObserverCursor:
        """Fence the journal immediately before one prompt is admitted.

        A shared guest can already have observations in flight.  Their caller
        passes ``restart_if_missing=False`` so a transient snapshot failure for
        this new turn cannot restart the singleton and invalidate every older
        cursor.  Once those owners close, the next idle begin may recover it.

        ``measurement_id`` additionally makes this fence the start of a member
        execution window, so the observation also carries that window's live
        census cycles. The fence must be taken before the window's opening
        census, which is why the identifier travels with it here rather than
        being attached afterwards.
        """
        try:
            if restart_if_missing:
                self.ensure()
            opening = self.snapshot()
            # The publication just read may have been written by a poll that
            # started before this opening fence. Cross the following complete
            # poll as well so pre-turn ring-buffer events and loss counters are
            # part of the baseline rather than the new turn.
            host_before = time.monotonic_ns()
            snapshot = self.snapshot(after_poll_seq=opening.status.poll_seq + 1)
            host_after = time.monotonic_ns()
        except Exception as exc:
            return GuestObserverCursor.unavailable(user_uid, str(exc))
        host_midpoint = host_before + (host_after - host_before) // 2
        return GuestObserverCursor(
            uid=user_uid,
            offset=snapshot.journal_size,
            lost=snapshot.status.lost,
            journal_dropped=snapshot.status.journal_dropped,
            poll_seq=snapshot.status.poll_seq,
            observer_pid=snapshot.status.pid,
            event_seq=snapshot.status.event_seq,
            journal_dev=snapshot.status.journal_dev,
            journal_ino=snapshot.status.journal_ino,
            guest_mono_ns=snapshot.guest_mono_ns,
            mono_offset_ns=host_midpoint - snapshot.guest_mono_ns,
            clock_error_ns=max(0, (host_after - host_before) // 2),
            available=True,
            guest_boot_id=self.guest_boot_id(),
            measurement_id=measurement_id,
        )

    def finish(self, cursor: GuestObserverCursor | None) -> GuestObservation:
        """Drain and read exactly one cursor's bounded event slice."""
        if cursor is None:
            return GuestObservation.unavailable("turn had no guest observer cursor")
        if not cursor.available:
            return GuestObservation.unavailable(cursor.detail)
        try:
            closing = self.snapshot()
            if closing.status.pid != cursor.observer_pid:
                return GuestObservation.unavailable("guest observer restarted mid-turn")
            if (
                closing.status.journal_dev != cursor.journal_dev
                or closing.status.journal_ino != cursor.journal_ino
            ):
                return GuestObservation.unavailable("guest observer journal changed mid-turn")
            if closing.status.event_seq < cursor.event_seq:
                return GuestObservation.unavailable("guest observer event sequence reset mid-turn")
            if (
                closing.status.lost < cursor.lost
                or closing.status.journal_dropped < cursor.journal_dropped
            ):
                return GuestObservation.unavailable("guest observer loss counters reset mid-turn")
            # Cross two publications after the closing snapshot. The first may
            # already be in flight after its poll; the second closes that tail.
            snapshot = self.snapshot(after_poll_seq=closing.status.poll_seq + 1)
            if snapshot.status.pid != cursor.observer_pid:
                return GuestObservation.unavailable("guest observer restarted while flushing")
            if (
                snapshot.status.journal_dev != cursor.journal_dev
                or snapshot.status.journal_ino != cursor.journal_ino
            ):
                return GuestObservation.unavailable("guest observer journal changed while flushing")
            if snapshot.status.event_seq < closing.status.event_seq:
                return GuestObservation.unavailable(
                    "guest observer event sequence reset while flushing"
                )
            if (
                snapshot.status.lost < closing.status.lost
                or snapshot.status.journal_dropped < closing.status.journal_dropped
            ):
                return GuestObservation.unavailable(
                    "guest observer loss counters reset while flushing"
                )
            if snapshot.journal_size < cursor.offset:
                return GuestObservation.unavailable("guest observer journal restarted mid-turn")
            available = snapshot.journal_size - cursor.offset
            count = min(available, MAX_OBSERVATION_BYTES)
            reader = (
                "import os, sys\n"
                "filename, offset, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
                "expected_dev, expected_ino = int(sys.argv[4]), int(sys.argv[5])\n"
                "with open(filename, 'rb', buffering=0) as handle:\n"
                "    journal = os.fstat(handle.fileno())\n"
                "    if (int(journal.st_dev) != expected_dev "
                "            or int(journal.st_ino) != expected_ino):\n"
                "        raise RuntimeError('observer journal changed before read')\n"
                "    if int(journal.st_size) < offset + count:\n"
                "        raise RuntimeError('observer journal is behind the read fence')\n"
                "    handle.seek(offset)\n"
                "    sys.stdout.buffer.write(handle.read(count))\n"
            )
            result = self._capture(
                [
                    "python3",
                    "-c",
                    reader,
                    GUEST_OBSERVER_EVENTS,
                    str(cursor.offset),
                    str(count),
                    str(cursor.journal_dev),
                    str(cursor.journal_ino),
                ],
                10.0,
            )
            if getattr(result, "returncode", 1) != 0:
                raise GuestObserverRuntimeError(
                    "guest observer journal read failed: " + self._stderr(result)[-300:]
                )
            payload = self._stdout(result)
            if len(payload) != count:
                raise GuestObserverRuntimeError(
                    "guest observer journal read stopped before its published fence"
                )
            return finish_observation(
                cursor=cursor,
                snapshot=snapshot,
                payload=payload,
                truncated=available > count,
            )
        except Exception as exc:
            return GuestObservation.unavailable(str(exc))


@dataclass
class _MutableProcess:
    process_seq: int
    uid: int
    pid: int
    tgid: int
    ppid: int | None
    comm: str
    executable: str | None
    started_ns: int
    exited_ns: int | None = None
    exit_code: int | None = None
    observed_fork: bool = False
    observed_exec: bool = False
    # The leader's own exit was seen. Linux lets a leader exit while its
    # threads live on, so this is not the process's end; ``dead_seen`` is.
    leader_exited: bool = False
    # An exit record reported the group's live count at zero.
    dead_seen: bool = False
    # Tasks whose exit was seen but whose final deschedule (``done``, where
    # the scheduler runtime is settled) has not. The group's counters are
    # reported only once this is empty and the group is dead.
    pending_done: set[int] = field(default_factory=set)
    cpu_ns: int | None = None
    mem_peak_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    io_rchar_bytes: int | None = None
    io_wchar_bytes: int | None = None

    @property
    def settled(self) -> bool:
        """Whether every counter this group will ever report has arrived."""
        return self.dead_seen and not self.pending_done

    def absorb(self, counters: dict[str, int]) -> None:
        """Fold one task's counters into its thread group.

        Sums for time and bytes, because each task reports only its own
        share. A maximum for the peak, because the kernel settles it once,
        on whichever thread empties the group, and every other thread's
        exit still carries the value it saw.
        """
        for name in (
            "cpu_ns",
            "io_read_bytes",
            "io_write_bytes",
            "io_rchar_bytes",
            "io_wchar_bytes",
        ):
            value = counters.get(name)
            if value is not None:
                setattr(self, name, (getattr(self, name) or 0) + value)
        peak = counters.get("mem_peak_bytes")
        if peak is not None:
            self.mem_peak_bytes = max(self.mem_peak_bytes or 0, peak)


# Bits of the journal's ``counters`` field, matching ``fi_process_counter``
# in the observer's header. A clear bit is a counter this kernel did not
# expose; the loader leaves its keys out of the record entirely.
_COUNTER_CPU = 1
_COUNTER_MAXRSS = 2
_COUNTER_IO = 4
_COUNTER_XACCT = 8
_COUNTER_GROUP = 16


def _counter_bits(event: dict[str, Any]) -> int:
    raw_bits = event.get("counters", 0)
    if isinstance(raw_bits, bool) or not isinstance(raw_bits, int) or raw_bits < 0:
        raise GuestObserverProtocolError("guest process exit counters flag is malformed")
    return raw_bits


def _exit_counters(event: dict[str, Any]) -> dict[str, int]:
    """The accounting one task's record carries, keyed by our names.

    Absent bits are absent keys: a kernel built without task I/O accounting
    produces exits with no ``read_bytes`` at all, which must reach the reader
    as unavailable rather than as a zero that looks like a measurement. CPU
    arrives only on ``done`` records, from the scheduler's own runtime; the
    tick-based time an exit record could carry reads zero for most
    sub-millisecond processes and is not used.
    """
    raw_bits = _counter_bits(event)
    counters: dict[str, int] = {}
    if raw_bits & _COUNTER_CPU and event.get("kind") == "done":
        counters["cpu_ns"] = _integer(event, "cpu_ns")
    if raw_bits & _COUNTER_MAXRSS:
        counters["mem_peak_bytes"] = _integer(event, "maxrss_bytes")
    if raw_bits & _COUNTER_IO:
        counters["io_read_bytes"] = _integer(event, "read_bytes")
        counters["io_write_bytes"] = _integer(event, "write_bytes")
    if raw_bits & _COUNTER_XACCT:
        counters["io_rchar_bytes"] = _integer(event, "rchar")
        counters["io_wchar_bytes"] = _integer(event, "wchar")
    return counters


def _group_dead(event: dict[str, Any]) -> bool:
    """Whether this exit emptied its thread group, per the kernel's live count.

    A kernel that could not expose the count leaves the bit clear; the
    self-test refuses such a kernel at preflight, and here the group is
    simply never closed, which reports no consumption rather than a guess.
    """
    if not _counter_bits(event) & _COUNTER_GROUP:
        return False
    value = event.get("group_dead")
    if not isinstance(value, bool):
        raise GuestObserverProtocolError("guest process group-exit flag is malformed")
    return value


def _close_group(
    group: _MutableProcess,
    mono_ns: int,
    active: dict[int, _MutableProcess],
    finished: list[_MutableProcess],
) -> None:
    """The thread group is dead: the process's lifetime ends here."""
    if group.dead_seen:
        return
    group.dead_seen = True
    group.exited_ns = mono_ns
    if active.get(group.pid) is group:
        active.pop(group.pid)
        finished.append(group)


def _bounded_text(value: Any, limit: int) -> str:
    """A single-line UTF-8-ish field bounded for storage and UI."""
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").replace("\n", " ").replace("\r", " ")[:limit]


def parse_status(value: Any) -> GuestObserverStatus:
    """Validate the observer-owned status object without truthy coercions."""
    if not isinstance(value, dict):
        raise GuestObserverProtocolError("guest observer status is not an object")
    if value.get("version") != GUEST_OBSERVER_VERSION or value.get("ready") is not True:
        raise GuestObserverProtocolError("guest observer is not ready at the expected version")

    def integer(name: str, *, positive: bool = False) -> int:
        item = value.get(name)
        if isinstance(item, bool) or not isinstance(item, int):
            raise GuestObserverProtocolError(f"guest observer {name} is not an integer")
        floor = 1 if positive else 0
        if item < floor:
            raise GuestObserverProtocolError(f"guest observer {name} is outside its range")
        return item

    return GuestObserverStatus(
        pid=integer("pid", positive=True),
        poll_seq=integer("poll_seq"),
        event_seq=integer("event_seq"),
        lost=integer("lost"),
        journal_dropped=integer("journal_dropped"),
        journal_bytes=integer("journal_bytes"),
        journal_dev=integer("journal_dev"),
        journal_ino=integer("journal_ino", positive=True),
    )


def parse_snapshot(payload: bytes) -> GuestObserverSnapshot:
    """Parse one JSON snapshot emitted by the fixed in-guest probe command."""
    if len(payload) > 64 * 1024:
        raise GuestObserverProtocolError("guest observer snapshot is too large")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuestObserverProtocolError("guest observer snapshot is not JSON") from exc
    if not isinstance(value, dict):
        raise GuestObserverProtocolError("guest observer snapshot is not an object")
    size = value.get("journal_size")
    mono = value.get("guest_mono_ns")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or isinstance(mono, bool)
        or not isinstance(mono, int)
        or mono <= 0
    ):
        raise GuestObserverProtocolError("guest observer snapshot counters are malformed")
    status = parse_status(value.get("status"))
    if size != status.journal_bytes:
        raise GuestObserverProtocolError("guest observer journal fence is inconsistent")
    return GuestObserverSnapshot(
        status=status,
        journal_size=size,
        guest_mono_ns=mono,
    )


def _integer(event: dict[str, Any], name: str, *, positive: bool = False) -> int:
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GuestObserverProtocolError(f"guest process event {name} is not an integer")
    if value < (1 if positive else 0):
        raise GuestObserverProtocolError(f"guest process event {name} is outside its range")
    return value


def _decoded_exit_code(raw: int) -> int:
    """Turn Linux's wait status into the shell-style code an owner recognizes."""
    signal = raw & 0x7F
    if signal:
        return 128 + signal
    return (raw >> 8) & 0xFF


def _finish_process(item: _MutableProcess, cursor: GuestObserverCursor) -> GuestProcess:
    offset = cursor.mono_offset_ns
    # Counters are reported only once the group is dead and every task whose
    # exit was seen has also left the CPU for the last time: before that a
    # partial thread sum would read as the whole. A group that died but is
    # missing a final deschedule reports no consumption, and the per-call
    # aggregation treats that as a floor.
    settled = item.settled
    return GuestProcess(
        process_seq=item.process_seq,
        uid=item.uid,
        pid=item.pid,
        tgid=item.tgid,
        ppid=item.ppid,
        comm=item.comm,
        executable=item.executable,
        started_guest_mono_ns=item.started_ns,
        exited_guest_mono_ns=item.exited_ns,
        started_host_mono=(item.started_ns + offset) / 1_000_000_000,
        exited_host_mono=(
            None if item.exited_ns is None else (item.exited_ns + offset) / 1_000_000_000
        ),
        exit_code=item.exit_code,
        observed_fork=item.observed_fork,
        observed_exec=item.observed_exec,
        observed_exit=item.dead_seen,
        cpu_usec=(item.cpu_ns // 1000 if settled and item.cpu_ns is not None else None),
        mem_peak_bytes=item.mem_peak_bytes if settled else None,
        io_read_bytes=item.io_read_bytes if settled else None,
        io_write_bytes=item.io_write_bytes if settled else None,
        io_rchar_bytes=item.io_rchar_bytes if settled else None,
        io_wchar_bytes=item.io_wchar_bytes if settled else None,
    )


def parse_events(
    payload: bytes,
    *,
    cursor: GuestObserverCursor,
) -> tuple[tuple[GuestProcess, ...], tuple[ToolMark, ...], bool]:
    """Reconstruct every process of ``cursor.uid`` from one journal slice.

    The return flag is true when input was malformed or exceeded the process
    cap.  That flag makes the containing observation incomplete; malformed
    telemetry never fails the user's agent turn.
    """
    if not cursor.available:
        return (), (), False
    malformed = False
    if payload and not payload.endswith(b"\n"):
        # A snapshot may catch the observer in its one short write.  Ignore the
        # partial tail, but never claim the resulting observation is complete.
        payload, _, _tail = payload.rpartition(b"\n")
        payload += b"\n" if payload else b""
        malformed = True
    active: dict[int, _MutableProcess] = {}
    finished: list[_MutableProcess] = []
    # Every group this slice created, live or exited, keyed by leader pid: a
    # thread can exit after its leader's record was already closed, and its
    # share of the group's CPU still belongs to that row.
    by_tgid: dict[int, _MutableProcess] = {}
    tool_marks: list[ToolMark] = []
    next_process_seq = 1
    # The byte fence and event fence describe the same append-only journal.
    # Re-reading an event from at or before the cursor means those two fences
    # disagreed, so the result must be partial rather than double-charging it.
    previous_event_seq = cursor.event_seq
    for raw_line in payload.splitlines():
        if not raw_line:
            continue
        # The journal carries two record families now: process lifecycle and
        # the member window's live censuses. A census is not a process event,
        # so it is skipped rather than judged malformed, and its own line bound
        # is the census serializer's, not this one's: one member's per-task
        # detail can legitimately exceed the process line cap.
        if _is_window_record(raw_line):
            continue
        if len(raw_line) > 4096:
            malformed = True
            continue
        try:
            event = json.loads(raw_line)
            if not isinstance(event, dict):
                raise GuestObserverProtocolError("guest process event is not an object")
            version = _integer(event, "version", positive=True)
            if version != GUEST_OBSERVER_VERSION:
                raise GuestObserverProtocolError("guest process event version changed")
            event_seq = _integer(event, "seq", positive=True)
            if event_seq <= previous_event_seq:
                raise GuestObserverProtocolError("guest process event sequence went backwards")
            previous_event_seq = event_seq
            uid = _integer(event, "uid")
            if uid != cursor.uid:
                continue
            kind = event.get("kind")
            if kind == "window_sample":
                continue
            if kind not in {"fork", "exec", "exit", "done", "tool_pre", "tool_post"}:
                raise GuestObserverProtocolError("guest process event kind is unknown")
            mono_ns = _integer(event, "mono_ns", positive=True)
            # A kernel event can be timestamped before the opening snapshot yet
            # reach the userspace journal after its byte fence. The shared guest
            # monotonic clock is the authoritative lower bound, so never charge
            # such a late pre-turn event to the newly admitted prompt.
            if mono_ns <= cursor.guest_mono_ns:
                continue
            if kind in {"tool_pre", "tool_post"}:
                tool_use_id = event.get("tool_use_id")
                if (
                    not isinstance(tool_use_id, str)
                    or not tool_use_id
                    or len(tool_use_id) > 200
                    or any(
                        not (character.isascii() and (character.isalnum() or character in "_-"))
                        for character in tool_use_id
                    )
                ):
                    raise GuestObserverProtocolError("guest tool mark id is malformed")
                _integer(event, "pid", positive=True)
                if len(tool_marks) >= MAX_OBSERVED_TOOL_MARKS:
                    # Exact boundaries are an optional precision layer. A
                    # same-UID caller can invoke the fixed helper and exhaust
                    # this cap, but that must not relabel the independent eBPF
                    # process census as incomplete. Missing pairs simply keep
                    # their calls on message-level timing.
                    continue
                tool_marks.append(
                    ToolMark(
                        host_mono=(mono_ns + cursor.mono_offset_ns) / 1_000_000_000,
                        phase="pre" if kind == "tool_pre" else "post",
                        tool_use_id=tool_use_id,
                        guest_mono_ns=mono_ns,
                    )
                )
                continue
            pid = _integer(event, "pid", positive=True)
            tgid = _integer(event, "tgid", positive=True)
            ppid_raw = _integer(event, "ppid")
            if kind == "done":
                # The task left the CPU for the last time and its scheduler
                # runtime is final. Leader or thread, it belongs to the group.
                group = by_tgid.get(tgid)
                if group is not None:
                    group.absorb(_exit_counters(event))
                    group.pending_done.discard(pid)
                continue
            # sched_process_fork also fires for threads.  PR89's contract is
            # process/tool accounting, so retain leaders and omit thread rows.
            # A thread's exit still carries its own share of the group's I/O,
            # and may be the exit that empties the group.
            if pid != tgid:
                if kind == "exit":
                    group = by_tgid.get(tgid)
                    if group is not None:
                        group.absorb(_exit_counters(event))
                        group.pending_done.add(pid)
                        if _group_dead(event):
                            _close_group(group, mono_ns, active, finished)
                continue
            comm = _bounded_text(event.get("comm"), MAX_COMM_BYTES) or "process"
            executable = _bounded_text(event.get("filename"), MAX_EXECUTABLE_BYTES) or None
            current = active.get(pid)
            if kind == "fork":
                if current is not None:
                    # PID reuse without the previous exit means the journal has
                    # a gap. Preserve both identities and mark the window bad.
                    finished.append(current)
                    malformed = True
                current = _MutableProcess(
                    process_seq=next_process_seq,
                    uid=uid,
                    pid=pid,
                    tgid=tgid,
                    ppid=ppid_raw or None,
                    comm=comm,
                    executable=executable,
                    started_ns=mono_ns,
                    observed_fork=True,
                )
                next_process_seq += 1
                active[pid] = current
                by_tgid[pid] = current
            elif kind == "exec":
                if current is None:
                    # The observer may have started after fork but before exec;
                    # exec is still an exact lifecycle event and starts a useful
                    # (conservative) observed span.
                    current = _MutableProcess(
                        process_seq=next_process_seq,
                        uid=uid,
                        pid=pid,
                        tgid=tgid,
                        ppid=ppid_raw or None,
                        comm=comm,
                        executable=executable,
                        started_ns=mono_ns,
                    )
                    next_process_seq += 1
                    active[pid] = current
                    by_tgid[pid] = current
                current.observed_exec = True
                current.comm = comm
                current.executable = executable or current.executable
                current.ppid = ppid_raw or current.ppid
            else:
                # An exit with no fork/exec in this slice belongs to a baseline
                # process (normally the resident CLI), not a process created by
                # this turn.  It is intentionally not manufactured into a row.
                if current is None:
                    continue
                raw_exit = event.get("exit_code")
                if isinstance(raw_exit, bool) or not isinstance(raw_exit, int):
                    raise GuestObserverProtocolError("guest process exit status is not an integer")
                # Counters before the lifecycle flags: a malformed counter
                # leaves the row alive and unexited, and the outer handler
                # marks the slice partial, rather than closing a row whose
                # consumption was half-read.
                counters = _exit_counters(event)
                group_dead = _group_dead(event)
                current.absorb(counters)
                current.pending_done.add(pid)
                current.leader_exited = True
                current.exit_code = _decoded_exit_code(raw_exit)
                current.comm = comm or current.comm
                # A leader whose threads live on has not ended the process;
                # the row stays open until the group's live count hits zero,
                # and reports no consumption if that never happens here.
                if group_dead:
                    _close_group(current, mono_ns, active, finished)
        except (GuestObserverProtocolError, UnicodeDecodeError, json.JSONDecodeError):
            malformed = True
            continue
        if len(finished) + len(active) > MAX_OBSERVED_PROCESSES:
            malformed = True
            break
    finished.extend(active.values())
    finished.sort(key=lambda item: (item.started_ns, item.process_seq))
    bounded = finished[:MAX_OBSERVED_PROCESSES]
    if len(finished) > len(bounded):
        malformed = True
    return (
        tuple(_finish_process(item, cursor) for item in bounded),
        tuple(tool_marks),
        malformed,
    )


def _is_window_record(raw_line: bytes) -> bool:
    """Whether a journal line is a member window census rather than an event.

    The two families share one journal. This test is deliberately on the raw
    bytes so a census line is never handed to the process parser, and so a
    census larger than the process line cap is not mistaken for corruption.
    """
    return b'"kind":"window_sample"' in raw_line


def finish_observation(
    *,
    cursor: GuestObserverCursor,
    snapshot: GuestObserverSnapshot | None,
    payload: bytes,
    truncated: bool,
    detail: str | None = None,
) -> GuestObservation:
    """Close a cursor without ever confusing unavailable with zero processes."""
    if not cursor.available or snapshot is None:
        return GuestObservation.unavailable(detail or cursor.detail)
    lost = max(0, snapshot.status.lost - cursor.lost)
    journal_dropped = max(
        0,
        snapshot.status.journal_dropped - cursor.journal_dropped,
    )
    processes, tool_marks, malformed = parse_events(payload, cursor=cursor)
    was_truncated = truncated or journal_dropped > 0 or malformed
    # Imported here rather than at module scope: the reducer pulls in the
    # window's own vocabulary, and this module is imported by paths that only
    # ever read process events.
    from cloud_agent_host.member_resources import parse_window_samples

    window_samples = tuple(
        parse_window_samples(
            payload,
            measurement_id=cursor.measurement_id,
            boot_id=cursor.guest_boot_id,
            # The window opens on this same fence, immediately after it is
            # taken, so the cursor's guest clock is the origin the census
            # compares a task's start time against. Without it every task
            # looks pre-existing and a child born in the window loses the
            # work it did before its first reading.
            opened_guest_mono_ns=cursor.guest_mono_ns,
        )
        if cursor.measurement_id
        else ()
    )
    return GuestObservation(
        source=GUEST_OBSERVER_SOURCE,
        complete=lost == 0 and not was_truncated,
        lost_events=lost + journal_dropped,
        truncated=was_truncated,
        processes=processes,
        tool_marks=tool_marks,
        origin_guest_mono_ns=cursor.guest_mono_ns,
        clock_error_ns=cursor.clock_error_ns,
        detail=(detail[:300] if detail else None),
        window_samples=window_samples,
        journal_payload=payload,
    )


def attribute_processes(
    observation: GuestObservation,
    windows: list[ToolWindow],
    *,
    clock_error_ns: int | None = None,
    origin_host_mono: float | None = None,
    windows_complete: bool = True,
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Map observed process starts to every plausible tool window.

    A process is attributed by its kernel-observed start, not by a sampler
    tick.  When two tool windows overlap, both call sequence numbers are kept
    on the process row instead of arbitrarily picking one.  A window the
    guest's own hooks bounded is on the process journal's clock already and
    is compared directly.  A message-level window is on the sandbox manager's clock;
    the guest/host bridge has a measured error bound, and widening such a
    window by that bound is conservative and prevents a boundary-adjacent
    process from being assigned with false precision.  If any tool window
    was dropped, no retained window may claim a process: the missing call
    could have overlapped every one of them.  The process census is still
    returned, but its per-call links stay empty and the parent
    ``tool_calls_truncated`` flag carries the loss.
    """
    error_s: float = max(
        0, observation.clock_error_ns if clock_error_ns is None else clock_error_ns
    )
    error_s /= 1_000_000_000
    counts = dict.fromkeys(range(1, len(windows) + 1), 0)
    rows: list[dict[str, Any]] = []
    ordered = sorted(observation.processes, key=lambda item: item.process_seq)
    for process in ordered:
        # Claude's boundary hook is instrumentation, not user workload. The
        # command shell ``exec``s this immutable helper, so a complete eBPF
        # lifecycle always leaves this exact executable identity on the row.
        # Keep the row so the census still says every process existed, but do
        # not charge it to a tool's workload process count.
        platform_process = process.executable == TOOL_MARK_HELPER
        call_seqs: list[int] = []
        if not platform_process and windows_complete:
            for index, window in enumerate(windows, start=1):
                if not _started_inside(process, window, error_s):
                    continue
                call_seqs.append(index)
                counts[index] += 1
        rows.append(
            {
                "process_seq": process.process_seq,
                "uid": process.uid,
                "pid": process.pid,
                "tgid": process.tgid,
                "ppid": process.ppid,
                "comm": process.comm,
                "executable": process.executable,
                "started_offset_ms": max(
                    0,
                    round(
                        (
                            process.started_host_mono - origin_host_mono
                            if origin_host_mono is not None
                            else (process.started_guest_mono_ns - observation.origin_guest_mono_ns)
                            / 1_000_000_000
                        )
                        * 1_000
                    ),
                ),
                "duration_ms": process.duration_ms,
                "exit_code": process.exit_code,
                "observed_fork": process.observed_fork,
                "observed_exec": process.observed_exec,
                "observed_exit": process.observed_exit,
                "tool_call_seqs": call_seqs,
                "cpu_usec": process.cpu_usec,
                "mem_peak_bytes": process.mem_peak_bytes,
                "io_read_bytes": process.io_read_bytes,
                "io_write_bytes": process.io_write_bytes,
                "io_rchar_bytes": process.io_rchar_bytes,
                "io_wchar_bytes": process.io_wchar_bytes,
            }
        )
    return rows, counts


def _started_inside(process: GuestProcess, window: ToolWindow, error_s: float) -> bool:
    """Whether the process began inside the window, on the clock the window is on.

    A window the guest's own hooks bounded shares the process journal's
    clock, so the comparison is direct and carries no error band. A
    message-level window lives on the sandbox manager's clock, and the process start
    is bridged onto it with the measured error, which widens the window by
    that much rather than claiming a boundary the bridge cannot support.
    """
    if window.opened_guest_ns is not None and window.closed_guest_ns is not None:
        return window.opened_guest_ns <= process.started_guest_mono_ns <= window.closed_guest_ns
    if process.started_host_mono + error_s < window.opened_mono:
        return False
    end = window.closed_mono
    return end is None or process.started_host_mono - error_s <= end


def add_process_counts(
    tool_calls: list[dict[str, Any]],
    counts: dict[int, int],
) -> list[dict[str, Any]]:
    """Return per-call rows with event-backed process counts attached."""
    return [
        {**row, "process_count": counts.get(int(row.get("call_seq") or 0), 0)} for row in tool_calls
    ]


def _combined(rows: list[dict[str, Any]], name: str, *, peak: bool) -> int | None:
    """One counter across a call's processes, or null if any lacked it."""
    values = [row.get(name) for row in rows]
    if not values or any(value is None for value in values):
        return None
    # The guard above already rejected a missing counter, so this only states
    # the same fact where the conversion below reads each value.
    present = [value for value in values if value is not None]
    numbers = [int(value) for value in present]
    return max(numbers) if peak else sum(numbers)


def add_process_resources(
    tool_calls: list[dict[str, Any]],
    processes: list[dict[str, Any]],
    *,
    observation_complete: bool = True,
) -> list[dict[str, Any]]:
    """Charge each call with what the processes it alone started consumed.

    This is the per-call CPU, peak memory and I/O the periodic sampler can
    never give a 35 ms call: the guest kernel accounted every process as it
    ended, and a process attributed to exactly one call is charged to it in
    full. CPU and I/O are sums over those processes, each of which reported
    only its own share; the peak is the largest single process, not a sum,
    because their peaks did not necessarily coincide.

    The number is a floor whenever the call had a process it cannot own
    outright: one that overlapping windows attributed to several calls, one
    whose group had not settled when the turn closed (still running, or its
    final deschedule never arrived — CPU is universal, so a settled row
    always carries it), or any process at all when the census lost events,
    since a process the call started may then be missing entirely. Such
    calls are marked partial and the ambiguous process is charged to none of
    them, so a sum shown as exact never contains another call's work or
    hides a gap. Work the agent did inside its own resident process belongs
    to no call and appears nowhere here.
    """
    exclusive: dict[int, list[dict[str, Any]]] = {}
    partial: set[int] = set()
    for row in processes:
        seqs = [int(seq) for seq in row.get("tool_call_seqs") or []]
        if not seqs:
            continue
        settled = bool(row.get("observed_exit")) and row.get("cpu_usec") is not None
        if len(seqs) > 1 or not settled:
            partial.update(seqs)
            continue
        exclusive.setdefault(seqs[0], []).append(row)
    charged: list[dict[str, Any]] = []
    for call in tool_calls:
        seq = int(call.get("call_seq") or 0)
        rows = exclusive.get(seq, [])
        lossy = not observation_complete and int(call.get("process_count") or 0) > 0
        charged.append(
            {
                **call,
                "process_cpu_usec": _combined(rows, "cpu_usec", peak=False),
                "process_mem_peak_bytes": _combined(rows, "mem_peak_bytes", peak=True),
                "process_io_read_bytes": _combined(rows, "io_read_bytes", peak=False),
                "process_io_write_bytes": _combined(rows, "io_write_bytes", peak=False),
                "process_resources_partial": seq in partial or lossy,
            }
        )
    return charged


__all__ = [
    "GUEST_OBSERVER_BINARY",
    "GUEST_OBSERVER_EVENTS",
    "GUEST_OBSERVER_ROOT",
    "GUEST_OBSERVER_ROTATE_BYTES",
    "GUEST_OBSERVER_SOURCE",
    "GUEST_OBSERVER_STATUS",
    "GUEST_OBSERVER_VERSION",
    "MAX_OBSERVATION_BYTES",
    "MAX_OBSERVED_PROCESSES",
    "MAX_OBSERVED_TOOL_MARKS",
    "GuestObservation",
    "GuestObserverController",
    "GuestObserverCursor",
    "GuestObserverProtocolError",
    "GuestObserverRuntimeError",
    "GuestObserverSnapshot",
    "add_process_counts",
    "add_process_resources",
    "attribute_processes",
    "finish_observation",
    "parse_snapshot",
]
