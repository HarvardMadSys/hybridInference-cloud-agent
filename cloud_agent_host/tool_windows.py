"""Per-tool-call windows, and what the sampler may honestly attribute to them.

The attempt rollup answers "what did this turn cost". This module answers the
next question — "which call cost it" — and, far more often, declines to.

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

Timestamps are taken in the runner's own loop, where the harness line was read.
That is a different clock from the one the turn footer has shown until now,
which is the control plane's insert time for the event and therefore carries
the upload queue inside every duration it displays.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from cloud_agent_host.resources import Sample, _delta, _readable

#: Windows kept per attempt. A turn issuing more than this reports the
#: overflow through ``truncated`` — a cap nobody is told about reads exactly
#: like a turn that made fewer calls.
MAX_TOOL_CALLS = 500

#: Tool names are a bounded vocabulary in principle and arbitrary strings in
#: practice (MCP servers name their own). Bounded here so one hostile name
#: cannot widen every row that carries it.
MAX_TOOL_NAME = 120


#: Where the sandbox's hooks append their boundary marks, relative to the
#: state mount both sides share. Not JSON: three fields, space separated, one
#: line per mark, appended under ``O_APPEND`` so concurrent tool calls cannot
#: interleave a line.
TOOL_MARK_FILE = "tool-marks.log"

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

    Fed from the runner's event loop, so it sees exactly the events the owner
    will later read, in the order they were parsed. It holds no payloads: a
    window is four scalars, and the arguments and results the call carried are
    already stored once.
    """

    def __init__(self, *, limit: int = MAX_TOOL_CALLS) -> None:
        self._limit = limit
        self._open: dict[str, ToolWindow] = {}
        self._closed: list[ToolWindow] = []
        self._dropped = 0

    def observe(self, event_type: str, payload: dict[str, Any] | None, *, mono: float) -> None:
        """Note one driver event. Anything that is not a tool boundary is ignored."""
        fields = payload or {}
        if event_type == "tool_use":
            tool_use_id = _text(fields.get("id"))
            if not tool_use_id:
                # Without an id there is nothing to pair a result against, so
                # the call could only ever be reported unfinished. A row that
                # says "unfinished" about a call that finished is worse than
                # no row.
                return
            self._open[tool_use_id] = ToolWindow(
                tool_use_id=tool_use_id,
                tool=(_text(fields.get("name")) or "tool")[:MAX_TOOL_NAME],
                opened_mono=mono,
            )
        elif event_type == "tool_result":
            tool_use_id = _text(fields.get("tool_use_id"))
            window = self._open.pop(tool_use_id, None) if tool_use_id else None
            if window is None:
                return
            self._append(dataclasses.replace(window, closed_mono=mono))

    def _append(self, window: ToolWindow) -> None:
        if len(self._closed) >= self._limit:
            self._dropped += 1
            return
        self._closed.append(window)

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
        opened: dict[str, float] = {}
        closed: dict[str, float] = {}
        for when, phase, tool_use_id in marks:
            target = opened if phase == "pre" else closed
            # First pre and last post: a call retried inside one attempt would
            # otherwise take the widest possible span rather than its own.
            if phase == "pre" and tool_use_id in target:
                continue
            target[tool_use_id] = when
        upgraded = 0
        for index, window in enumerate(self._closed):
            start, end = opened.get(window.tool_use_id), closed.get(window.tool_use_id)
            if start is None or end is None or end < start:
                continue
            if start < window.opened_mono or end > (window.closed_mono or start):
                continue
            self._closed[index] = dataclasses.replace(
                window, opened_mono=start, closed_mono=end, source="exact"
            )
            upgraded += 1
        return upgraded

    def windows(self) -> list[ToolWindow]:
        """Every call this attempt issued, finished ones first.

        Calls still open when the turn ended are included with no close time.
        A turn that ends mid-call is the shape of a timeout or a cancel, and
        dropping those rows would delete the evidence.
        """
        room = max(0, self._limit - len(self._closed))
        return [*self._closed, *list(self._open.values())[:room]]

    @property
    def truncated(self) -> bool:
        """Whether this attempt issued more calls than are being reported."""
        return self._dropped > 0 or len(self._open) > max(0, self._limit - len(self._closed))


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
        if other is window or other.closed_mono is None:
            continue
        if other.opened_mono <= end and other.closed_mono >= start:
            shared += 1
    return shared


def attribute(samples: list[Sample], windows: list[ToolWindow]) -> list[dict[str, Any]]:
    """One row per tool call, carrying only what its own readings support.

    The rows are returned for every call, measured or not. A call with no
    readings inside it is the common case and its row is the record of that:
    ``sample_count = 0`` with null counters is the difference between "this
    call was too short to measure" and "this call used nothing", and only the
    first of those is ever true here.
    """
    readings = [sample for sample in samples if _readable(sample)]
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


def tool_time_ms(windows: list[ToolWindow]) -> int:
    """How long the turn spent inside tool calls: the union, never the sum.

    Parallel calls overlap, and adding their durations produces a number that
    can exceed the turn itself — which a reader would rightly report as a bug.
    The union is the honest answer to "how much of this turn was the sandbox
    doing something", and the remainder is what the turn spent waiting on the
    model: measured on production, that remainder is over 98 % of the wall
    clock, and it is the most explanatory number the footer can carry.
    """
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


#: The sed program that lifts the call id out of the hook payload on stdin.
#:
#: Greedy by design, so it takes the *last* occurrence on the line — which is
#: the real one: the CLI builds both payloads with ``tool_use_id`` as the final
#: key, so anything echoing that name inside a tool argument appears earlier.
#: The bounded charset is the other half: an id that does not match is not
#: written at all, so a tool argument cannot forge a second line into the file.
_MARK_SED = r's/.*"tool_use_id": *"\([A-Za-z0-9_-]\{1,200\}\)".*/\1/p'

#: One hook body, with ``PHASE`` and ``MARKS`` substituted before use.
#:
#: A shell rather than the node already in the image: node costs tens of
#: milliseconds to start, and the median tool call is 35 ms, so the observer
#: would have become the largest thing it measured. Two forked shells cost
#: about 0.6 ms of it, measured on the sandbox base image.
#:
#: The timestamp is taken *before* stdin is read, so the mark is the boundary
#: rather than the boundary plus however long the payload took to arrive.
_HOOK_COMMAND = (
    "t=$(date +%s.%N 2>/dev/null); "
    "i=$(sed -n '" + _MARK_SED + "' 2>/dev/null); "
    '[ -n "$t" ] && [ -n "$i" ] && '
    'printf \'%s %s %s\\n\' "$t" PHASE "$i" >> MARKS 2>/dev/null; '
    "exit 0"
)


def hook_settings(mark_path: str) -> dict[str, Any]:
    """The claude-code settings that make a turn's tool boundaries visible.

    Passive by contract, and the shape of the command is the contract: it
    takes its timestamp before it reads anything, extracts the call id with
    one `sed`, writes one short line, and exits 0 whatever happened. It never
    inspects a result, never blocks, and cannot fail a tool — a hook that
    cannot write simply leaves the call message-level.

    The command's shape is the contract; see :data:`_HOOK_COMMAND`.
    """
    command = _HOOK_COMMAND.replace("MARKS", mark_path)
    return {
        "hooks": {
            event: [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command.replace("PHASE", phase),
                            "timeout": 5,
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

    The marks carry a wall clock because that is what a shell can cheaply
    produce; the samples carry a monotonic one. The offset between them is
    taken once, here, and the tracker refuses any mark that lands outside the
    window it is meant to narrow — which is what a clock step would produce.
    """

    def __init__(self, state_dir: str | None) -> None:
        self._path = pathlib.Path(state_dir) / TOOL_MARK_FILE if state_dir else None
        self._offset = self._size()
        self._wall0 = time.time()
        self._mono0 = time.monotonic()

    def _size(self) -> int:
        try:
            return os.path.getsize(self._path) if self._path else 0
        except OSError:
            return 0

    def read(self) -> list[tuple[float, str, str]]:
        """This turn's marks, as (monotonic, phase, tool_use_id).

        Every failure is empty rather than raised: the marks are an upgrade to
        a measurement that already works without them.
        """
        if self._path is None:
            return []
        try:
            with self._path.open("r", errors="replace") as handle:
                handle.seek(self._offset)
                lines = handle.readlines()
        except OSError:
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
    "TOOL_MARK_FILE",
    "ToolMarkReader",
    "ToolWindow",
    "ToolWindowTracker",
    "attribute",
    "hook_settings",
    "tool_time_ms",
]
