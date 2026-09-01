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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cloud_agent_host.tool_windows import TOOL_MARK_HELPER

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.tool_windows import ToolWindow

GUEST_OBSERVER_VERSION = 1
GUEST_OBSERVER_SOURCE = "kata-guest-ebpf-v1"
GUEST_OBSERVER_ROOT = "/run/freeinference-observer"
GUEST_OBSERVER_EVENTS = f"{GUEST_OBSERVER_ROOT}/events.jsonl"
GUEST_OBSERVER_STATUS = f"{GUEST_OBSERVER_ROOT}/status.json"
GUEST_OBSERVER_BINARY = "/usr/local/libexec/freeinference-guest-observer"

# One member turn may read at most this much of the group-wide journal.  The
# observer itself has a larger lifetime cap; this one bounds one broker result.
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
    guest_mono_ns: int = 0
    # Add this to a guest monotonic timestamp to place it on the broker host's
    # monotonic axis.  ``clock_error_ns`` is half the Docker-exec round trip;
    # attribution widens by it rather than claiming false boundary precision.
    mono_offset_ns: int = 0
    clock_error_ns: int = 0
    available: bool = False
    detail: str | None = None

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
    # Guest-root accepted boundary records, already bridged onto the broker's
    # monotonic clock.  They share the process journal's UID/loss fence.
    tool_marks: tuple[tuple[float, str, str], ...] = ()
    origin_guest_mono_ns: int = 0
    clock_error_ns: int = 0
    detail: str | None = None

    @classmethod
    def unavailable(cls, detail: str | None) -> GuestObservation:
        """Return no rows while preserving why zero rows prove nothing."""
        return cls(
            source=None,
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
        lock: threading.Lock | None = None,
    ) -> None:
        self._capture = capture
        self._privileged = privileged
        self._lock = lock or threading.Lock()

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
            "actual_size = os.path.getsize(events_path) if os.path.exists(events_path) else 0\n"
            "if size < 0 or actual_size < size:\n"
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

    def begin(self, *, user_uid: int) -> GuestObserverCursor:
        """Fence the journal immediately before one prompt is admitted."""
        try:
            self.ensure()
            host_before = time.monotonic_ns()
            snapshot = self.snapshot()
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
            guest_mono_ns=snapshot.guest_mono_ns,
            mono_offset_ns=host_midpoint - snapshot.guest_mono_ns,
            clock_error_ns=max(0, (host_after - host_before) // 2),
            available=True,
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
            if closing.status.event_seq < cursor.event_seq:
                return GuestObservation.unavailable("guest observer event sequence reset mid-turn")
            # Cross two publications after the closing snapshot. The first may
            # already be in flight after its poll; the second closes that tail.
            snapshot = self.snapshot(after_poll_seq=closing.status.poll_seq + 1)
            if snapshot.status.pid != cursor.observer_pid:
                return GuestObservation.unavailable("guest observer restarted while flushing")
            if snapshot.status.event_seq < closing.status.event_seq:
                return GuestObservation.unavailable(
                    "guest observer event sequence reset while flushing"
                )
            if snapshot.journal_size < cursor.offset:
                return GuestObservation.unavailable("guest observer journal restarted mid-turn")
            available = snapshot.journal_size - cursor.offset
            count = min(available, MAX_OBSERVATION_BYTES)
            reader = (
                "import os, sys\n"
                "filename, offset, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
                "with open(filename, 'rb', buffering=0) as handle:\n"
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
    observed_exit: bool = False


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
        observed_exit=item.observed_exit,
    )


def parse_events(
    payload: bytes,
    *,
    cursor: GuestObserverCursor,
) -> tuple[tuple[GuestProcess, ...], tuple[tuple[float, str, str], ...], bool]:
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
    tool_marks: list[tuple[float, str, str]] = []
    next_process_seq = 1
    previous_event_seq = 0
    for raw_line in payload.splitlines():
        if not raw_line:
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
            if kind not in {"fork", "exec", "exit", "tool_pre", "tool_post"}:
                raise GuestObserverProtocolError("guest process event kind is unknown")
            mono_ns = _integer(event, "mono_ns", positive=True)
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
                    malformed = True
                    continue
                tool_marks.append(
                    (
                        (mono_ns + cursor.mono_offset_ns) / 1_000_000_000,
                        "pre" if kind == "tool_pre" else "post",
                        tool_use_id,
                    )
                )
                continue
            pid = _integer(event, "pid", positive=True)
            tgid = _integer(event, "tgid", positive=True)
            ppid_raw = _integer(event, "ppid")
            # sched_process_fork also fires for threads.  PR89's contract is
            # process/tool accounting, so retain leaders and omit thread rows.
            if pid != tgid:
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
                current.exited_ns = mono_ns
                current.exit_code = _decoded_exit_code(raw_exit)
                current.observed_exit = True
                current.comm = comm or current.comm
                finished.append(current)
                active.pop(pid, None)
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
    )


def attribute_processes(
    observation: GuestObservation,
    windows: list[ToolWindow],
    *,
    clock_error_ns: int | None = None,
    origin_host_mono: float | None = None,
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Map observed process starts to every plausible tool window.

    A process is attributed by its kernel-observed start, not by a sampler
    tick.  When two tool windows overlap, both call sequence numbers are kept
    on the process row instead of arbitrarily picking one.  The guest/host
    clock bridge has a measured error bound; widening windows by that bound is
    conservative and prevents a boundary-adjacent process from being assigned
    with false precision.
    """
    error_s = max(0, observation.clock_error_ns if clock_error_ns is None else clock_error_ns)
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
        if not platform_process:
            for index, window in enumerate(windows, start=1):
                end = window.closed_mono
                if process.started_host_mono + error_s < window.opened_mono:
                    continue
                if end is not None and process.started_host_mono - error_s > end:
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
            }
        )
    return rows, counts


def add_process_counts(
    tool_calls: list[dict[str, Any]],
    counts: dict[int, int],
) -> list[dict[str, Any]]:
    """Return per-call rows with event-backed process counts attached."""
    return [
        {**row, "process_count": counts.get(int(row.get("call_seq") or 0), 0)} for row in tool_calls
    ]


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
    "attribute_processes",
    "finish_observation",
    "parse_snapshot",
]
