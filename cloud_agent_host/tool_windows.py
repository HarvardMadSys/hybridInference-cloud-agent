"""Per-tool-call windows and the evidence honestly attributable to them.

The current shared-retained topology records timing for every bounded call and
may attach UID-filtered guest process accounting. The whole-container sampler
arithmetic remains here for historical rows and lower-level validation, but a
shared member never receives its group's Docker totals.

Measured on production over 278 calls: the median tool call is 35 ms, 97 %
finish inside a second, and the sampler reads once a second. Most calls begin
and end between two readings, so there is nothing to hand them. Crediting such
a call with the whole second's delta because it happened to fall inside that
second would invent a number, and an invented number here is worse than a
blank: the entire point of this signal is finding the one call that ate the
machine, and noise at that scale hides it.

Two rules follow, and the rest of this module is their implementation.

**A call is measured only from readings taken inside it.** Fewer than two, and
the call reports no numbers at all. Two or more, and the delta between the
first and last of them is what the call consumed — a *lower* bound, since the
head and tail of the window lie outside those readings, which is why each row
also carries the span its readings actually covered.

**A reading inside two calls at once belongs to neither exclusively.** Runtimes
issue tool calls in parallel and the sandbox counters do not separate them. A
call that shared its readings carries them in the ``shared`` fields, with the
number of calls it shared with, and never in the ``exclusive`` ones — so a
reader that only knows about exclusive numbers shows nothing rather than
showing an overstatement.

Timestamps are taken in the workspace broker's turn loop, where the harness line was read.
That is a different clock from the one the turn footer has shown until now,
which is the control plane's insert time for the event and therefore carries
the upload queue inside every duration it displays.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

from cloud_agent_host.resources import Sample, _delta, _readable

#: Windows kept per attempt. A turn issuing more than this reports the
#: overflow through ``truncated`` — a cap nobody is told about reads exactly
#: like a turn that made fewer calls.
MAX_TOOL_CALLS = 500

#: Tool names are a bounded vocabulary in principle and arbitrary strings in
#: practice (MCP servers name their own). Bounded here so one hostile name
#: cannot widen every row that carries it.
MAX_TOOL_NAME = 120

#: The worker request and database row both bound this identifier. Enforce the
#: same limit while tracking so a malformed runtime event cannot retain an
#: unbounded dictionary key and then make the entire resource report fail
#: validation at the end of an otherwise successful turn.
MAX_TOOL_USE_ID = 200

# Exact marks are an optional precision upgrade, never an unbounded telemetry
# channel. A turn that exceeds this simply stays message-timed.
MAX_TOOL_MARK_BYTES = 1024 * 1024


#: Legacy non-Kata fallback for boundary marks, relative to the state mount.
#: Kata sends the same bounded fields to the guest-root observer instead; the
#: agent-writable fallback is never trusted there.
TOOL_MARK_FILE = "tool-marks.log"
TOOL_MARK_SANDBOX_PATH = f"/agent-state/{TOOL_MARK_FILE}"

#: The immutable image helper used by Claude's command hooks.  ``exec``-ing
#: this binary gives the eBPF census one recognizable instrumentation process
#: instead of the former shell + date + sed process tree.
TOOL_MARK_HELPER = "/usr/local/libexec/freeinference-tool-mark"

#: What the hook is allowed to have extracted. Bounding the charset is what
#: keeps a tool argument from writing a second line into the mark file: an id
#: that does not match is simply not written, and the call stays message-level.
_MARK_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


@dataclass(frozen=True)
class ToolWindow:
    """When one tool call started and, if it returned, when it finished."""

    tool_use_id: str
    tool: str
    opened_mono: float
    closed_mono: float | None = None
    #: ``message`` — the window is when the event was parsed, which brackets
    #: the call from outside. ``exact`` — the sandbox's own hooks marked the
    #: boundaries, so the window is the execution itself.
    source: Literal["message", "exact"] = "message"

    @property
    def duration_ms(self) -> int | None:
        """Wall time from the call being issued to its result arriving.

        ``None`` for a call that never returned — which is a real outcome
        worth showing, not a missing measurement.
        """
        if self.closed_mono is None:
            return None
        return max(0, round((self.closed_mono - self.opened_mono) * 1000))


def _text(value: Any) -> str:
    """A payload field as a plain string, or empty when it is not one."""
    return value.strip() if isinstance(value, str) else ""


class ToolWindowTracker:
    """Turns one attempt's driver event stream into one window per call.

    Fed from the workspace broker's turn loop, so it sees exactly the events
    the runner will later read, in the order they were parsed. It holds no
    payloads: a window is four scalars, and the arguments and results the call
    carried are already stored once.
    """

    def __init__(self, *, limit: int = MAX_TOOL_CALLS) -> None:
        self._limit = max(0, int(limit))
        # The public contract says call_seq is issuance order. Keep one bounded
        # list in that order and let open ids point into it; moving completed
        # calls into a separate list would silently reorder parallel calls by
        # whichever result happened to arrive first.
        self._windows: list[ToolWindow | None] = []
        self._open: dict[str, int] = {}
        # Once two concurrent calls reuse one id, no later result can say
        # which call it closes. Tombstone the retained row and keep the id
        # invalid for the rest of this attempt instead of pairing the first
        # result with whichever call happened to arrive first.
        self._ambiguous_ids: set[str] = set()
        self._dropped = 0

    def observe(self, event_type: str, payload: dict[str, Any] | None, *, mono: float) -> None:
        """Note one driver event. Anything that is not a tool boundary is ignored."""
        fields = payload or {}
        if event_type == "tool_use":
            tool_use_id = _text(fields.get("id"))
            if not tool_use_id or len(tool_use_id) > MAX_TOOL_USE_ID:
                # Without an id there is nothing to pair a result against, so
                # the call could only ever be reported unfinished. A row that
                # says "unfinished" about a call that finished is worse than
                # no row. Say explicitly that the resulting list is partial.
                self._dropped += 1
                return
            if tool_use_id in self._ambiguous_ids:
                self._dropped += 1
                return
            if tool_use_id in self._open:
                # Neither duplicate can be retained honestly. Leaving the
                # original row in place would let the first result close it,
                # even though that result could belong to the second call.
                index = self._open.pop(tool_use_id)
                self._windows[index] = None
                self._ambiguous_ids.add(tool_use_id)
                self._dropped += 2
                return
            if len(self._windows) >= self._limit:
                # An open-call storm must not grow memory past the wire cap.
                self._dropped += 1
                return
            self._open[tool_use_id] = len(self._windows)
            self._windows.append(
                ToolWindow(
                    tool_use_id=tool_use_id,
                    tool=(_text(fields.get("name")) or "tool")[:MAX_TOOL_NAME],
                    opened_mono=mono,
                )
            )
        elif event_type == "tool_result":
            tool_use_id = _text(fields.get("tool_use_id"))
            if tool_use_id in self._ambiguous_ids:
                return
            index = self._open.pop(tool_use_id, None) if tool_use_id else None
            if index is None:
                return
            window = self._windows[index]
            if window is None:  # Defensive; ambiguous ids are handled above.
                return
            self._windows[index] = dataclasses.replace(
                window,
                closed_mono=mono,
            )

    def apply_marks(self, marks: list[tuple[float, str, str]]) -> int:
        """Narrow windows to the boundaries the sandbox's own hooks recorded.

        A window is upgraded only when both of its marks are present and land
        inside the message-level window that already brackets it. That last
        check is the one that matters: the marks come from a wall clock
        converted onto the sampler's monotonic axis, and a clock that stepped
        mid-turn would otherwise move a call somewhere it never ran. A mark
        that fails it is dropped and the call stays message-level, which is
        the outcome this whole path degrades to anyway.

        Returns how many windows were upgraded, for the caller to log.
        """
        by_id: dict[str, list[tuple[float, str]]] = {}
        for when, phase, tool_use_id in marks:
            if phase not in {"pre", "post"}:
                continue
            by_id.setdefault(tool_use_id, []).append((when, phase))
        upgraded = 0
        for index, window in enumerate(self._windows):
            if window is None:
                continue
            if window.closed_mono is None:
                continue
            candidates = [
                (when, phase)
                for when, phase in by_id.get(window.tool_use_id, [])
                if window.opened_mono <= when <= window.closed_mono
            ]
            opened = [when for when, phase in candidates if phase == "pre"]
            closed = [when for when, phase in candidates if phase == "post"]
            # More than one possible boundary is ambiguity, not precision.
            # This also keeps a reused id from cross-pairing two calls.
            if len(opened) != 1 or len(closed) != 1 or closed[0] < opened[0]:
                continue
            self._windows[index] = dataclasses.replace(
                window,
                opened_mono=opened[0],
                closed_mono=closed[0],
                source="exact",
            )
            upgraded += 1
        return upgraded

    def windows(self) -> list[ToolWindow]:
        """Every call this attempt issued, in the order it was issued.

        Calls still open when the turn ended are included in issuance order
        with no close time. A turn that ends mid-call is the shape of a timeout
        or a cancel, and dropping those rows would delete the evidence.
        """
        return [window for window in self._windows if window is not None]

    @property
    def truncated(self) -> bool:
        """Whether this attempt issued more calls than are being reported."""
        return self._dropped > 0


def _row(window: ToolWindow, call_seq: int) -> dict[str, Any]:
    """The unmeasured row: what every call reports before any reading is found."""
    return {
        "call_seq": call_seq,
        "tool_use_id": window.tool_use_id,
        "tool_name": window.tool,
        "timing_source": window.source,
        "duration_ms": window.duration_ms,
        "sample_count": 0,
        "overlap_n": 1,
        "measured_span_ms": None,
        "cpu_usec_exclusive": None,
        "cpu_usec_shared": None,
        "mem_peak_exclusive_bytes": None,
        "mem_peak_shared_bytes": None,
    }


def _overlap_count(window: ToolWindow, windows: list[ToolWindow], start: float, end: float) -> int:
    """How many calls were open across the span these readings cover.

    Deliberately generous: a call whose window touches the span at all counts.
    Over-reporting an overlap costs a number its ``exclusive`` label, which is
    the safe direction — under-reporting one would present a delta two calls
    produced as the work of one.
    """
    shared = 1
    for other in windows:
        if other is window:
            continue
        if other.opened_mono <= end and (other.closed_mono is None or other.closed_mono >= start):
            shared += 1
    return shared


def attribute(
    samples: list[Sample],
    windows: list[ToolWindow],
    *,
    windows_complete: bool = True,
) -> list[dict[str, Any]]:
    """One row per tool call, carrying only what its own readings support.

    The rows are returned for every call, measured or not. A call with no
    readings inside it is the common case and its row is the record of that:
    ``sample_count = 0`` with null counters is the difference between "this
    call was too short to measure" and "this call used nothing", and only the
    first of those is ever true here.
    """
    # A dropped window may overlap every retained call. Without its boundary,
    # no retained row can honestly call a sandbox-wide delta exclusive.
    readings = [sample for sample in samples if _readable(sample)] if windows_complete else []
    rows: list[dict[str, Any]] = []
    for index, window in enumerate(windows, start=1):
        row = _row(window, index)
        rows.append(row)
        if window.closed_mono is None:
            continue
        inside = [s for s in readings if window.opened_mono <= s.mono <= window.closed_mono]
        row["sample_count"] = len(inside)
        if len(inside) < 2:
            continue
        start, end = inside[0].mono, inside[-1].mono
        overlap = _overlap_count(window, windows, start, end)
        memory = [s.mem_bytes for s in inside if s.mem_bytes is not None]
        cpu = _delta(inside, "cpu_usec")
        peak = max(memory) if memory else None
        row["overlap_n"] = overlap
        row["measured_span_ms"] = max(0, round((end - start) * 1000))
        if overlap == 1:
            row["cpu_usec_exclusive"] = cpu
            row["mem_peak_exclusive_bytes"] = peak
        else:
            row["cpu_usec_shared"] = cpu
            row["mem_peak_shared_bytes"] = peak
    return rows


def tool_time_ms(windows: list[ToolWindow]) -> int | None:
    """How long the turn spent inside tool calls: the union, never the sum.

    Parallel calls overlap, and adding their durations produces a number that
    can exceed the turn itself — which a reader would rightly report as a bug.
    The union is the honest answer to "how much of this turn was bracketed by
    tool-call events". The remainder is non-tool time: model work plus harness
    and platform overhead, which this measurement cannot separate. An open
    call makes even that two-way split incomplete, so return no number rather
    than silently counting the unfinished call as non-tool time.
    """
    if any(window.closed_mono is None for window in windows):
        return None
    spans = sorted((w.opened_mono, w.closed_mono) for w in windows if w.closed_mono is not None)
    total = 0.0
    current_start: float | None = None
    current_end = 0.0
    for start, end in spans:
        if current_start is None:
            current_start, current_end = start, end
            continue
        if start > current_end:
            total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_start is not None:
        total += current_end - current_start
    return max(0, round(total * 1000))


def hook_settings(mark_path: str) -> dict[str, Any]:
    """The claude-code settings that make a turn's tool boundaries visible.

    Passive by contract. The shell ``exec`` replaces itself with one immutable
    image helper, which timestamps before reading stdin and always exits zero.
    In Kata that helper sends the mark to the guest-root observer; trusted
    non-Kata development falls back to the bounded legacy state file. If an
    older image lacks the helper the shell exits zero and the call simply stays
    message-level.
    """
    if mark_path != TOOL_MARK_SANDBOX_PATH:
        raise ValueError("tool mark path must use the fixed sandbox state location")
    helper = shlex.quote(TOOL_MARK_HELPER)
    marks = shlex.quote(mark_path)

    def command(phase: str) -> str:
        return f"if [ -x {helper} ]; then exec {helper} {phase} {marks}; fi; exit 0"

    return {
        "hooks": {
            event: [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command(phase),
                            "timeout": 1,
                        }
                    ],
                }
            ]
            for event, phase in (("PreToolUse", "pre"), ("PostToolUse", "post"))
        }
    }


class ToolMarkReader:
    """Reads the sandbox's boundary marks onto the sampler's clock.

    Constructed at turn start so it can remember where the file already ended:
    a resident session writes every turn's marks to the same file, and reading
    from the top would give turn two the boundaries of turn one.

    Legacy marks carry a wall clock while samples carry a monotonic one. The
    offset between them is taken once, here, and the tracker refuses any mark
    that lands outside the window it is meant to narrow. Kata does not use
    this reader: its guest-root journal carries monotonic marks directly.
    """

    def __init__(
        self,
        state_dir: str | None,
        *,
        remote_size: Callable[[], int] | None = None,
        remote_read: Callable[[int, int], bytes] | None = None,
    ) -> None:
        self._path = pathlib.Path(state_dir) / TOOL_MARK_FILE if state_dir else None
        self._remote_size = remote_size
        self._remote_read = remote_read
        self._remote_fenced = True
        self._offset = self._size()
        self._wall0 = time.time()
        self._mono0 = time.monotonic()

    def _size(self) -> int:
        if self._remote_size is not None:
            try:
                return max(0, int(self._remote_size()))
            except Exception:
                # Reading from zero after a failed retained-state fence could
                # replay marks from an older turn. Disable the optional exact
                # path instead; message-level windows remain valid.
                self._remote_fenced = False
                return 0
        try:
            return os.path.getsize(self._path) if self._path else 0
        except OSError:
            return 0

    def read(self) -> list[tuple[float, str, str]]:
        """This turn's marks, as (monotonic, phase, tool_use_id).

        Every failure is empty rather than raised: the marks are an upgrade to
        a measurement that already works without them.
        """
        if (self._path is None and self._remote_read is None) or not self._remote_fenced:
            return []
        try:
            if self._remote_read is not None:
                payload = self._remote_read(self._offset, MAX_TOOL_MARK_BYTES)
                lines = payload.decode("utf-8", "replace").splitlines()
            else:
                assert self._path is not None
                with self._path.open("r", errors="replace") as handle:
                    handle.seek(self._offset)
                    lines = handle.read(MAX_TOOL_MARK_BYTES).splitlines()
        except Exception:
            return []
        marks: list[tuple[float, str, str]] = []
        for line in lines:
            fields = line.split()
            if len(fields) != 3 or fields[1] not in ("pre", "post"):
                continue
            if not _MARK_ID.match(fields[2]):
                continue
            try:
                wall = float(fields[0])
            except ValueError:
                continue
            marks.append((self._mono0 + (wall - self._wall0), fields[1], fields[2]))
        return marks


__all__ = [
    "MAX_TOOL_CALLS",
    "MAX_TOOL_MARK_BYTES",
    "TOOL_MARK_FILE",
    "TOOL_MARK_HELPER",
    "TOOL_MARK_SANDBOX_PATH",
    "ToolMarkReader",
    "ToolWindow",
    "ToolWindowTracker",
    "attribute",
    "hook_settings",
    "tool_time_ms",
]
