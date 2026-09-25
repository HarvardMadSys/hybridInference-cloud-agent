"""Reduce a member's live censuses and exit events into one window summary.

One window is one member activation: the resident agent and its tools, under
one member UID, for exactly as long as the platform runs that turn. Two
independent sources describe it, and neither replaces the other.

* The **live census** reads every task the member still has running. It is the
  only way a long-lived agent process is measured while it works instead of
  when it eventually dies, and its counters are dominated by that resident
  process.
* The **exit event** is the kernel's final word on one task. It is the only
  way a process born and died between two censuses is measured at all.

They describe the same work from different directions, which is what makes
them useful and also what makes them dangerous: adding two totals would count
a process that both ran and exited inside the window twice. So this module
keeps them as two estimates and decides between them, rather than summing.

How the two are combined:

* Each estimate accumulates only *non-negative deltas* of its own source. A
  live total can fall when a task exits, and a fall is not negative work, so
  it is dropped rather than subtracted. An exit total only ever grows, once
  per instance.
* When both produced a value, the larger one is reported. It is the better
  floor: the live estimate can miss a short-lived process that started and
  finished between two censuses, and the exit estimate can miss a process
  still running when the window closes.
* When the two disagree by more than the reconciliation tolerance, the result
  is ``partial`` with ``estimates_diverged``. Two measurements of one quantity
  that differ that much mean one of them is wrong, and reporting the larger
  one as if it were exact would be the overclaim this shape exists to stop.
  The tolerance is generous because the sources are not expected to agree
  exactly: they are sampled at different instants and one of them sees the
  closing moment.

Neither estimate is repaired with zero. A cycle that could not read a counter
does not move its total, a counter that goes backwards is refused rather than
reconciled, and a window with no usable reading reports the metric as
unavailable rather than as nothing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

#: The capability string reported for a window measured this way.
MEMBER_WINDOW_SOURCE = "kata-guest-member-window-v1"
#: The scope every number in this module describes.
MEMBER_WINDOW_SCOPE = "member_execution_window"
#: Stamped into the stored snapshot so a reader only interprets a shape it
#: understands.
MEMBER_WINDOW_SCHEMA_VERSION = 1
#: The planned census period. The wire reports it because a reader comparing
#: two turns has to know how finely each one was sampled.
MEMBER_WINDOW_SAMPLE_INTERVAL_MS = 1000

#: Per-cycle bits mirrored from the guest helper's ``fi_window_counter``.
WINDOW_CPU = 1 << 0
WINDOW_STORAGE_IO = 1 << 1
WINDOW_PSS = 1 << 2
WINDOW_PSS_COMPLETE = 1 << 3
WINDOW_ADDRESS_SPACES_DEDUPED = 1 << 4
WINDOW_TRUNCATED = 1 << 5
WINDOW_READING_COMPLETE = 1 << 6

#: Bits mirrored from ``fi_process_counter``: which exit counters the kernel
#: accounts for that task. A clear bit means absent, never zero.
EXIT_COUNTER_CPU = 1 << 0
EXIT_COUNTER_IO = 1 << 2

#: How far the live and exit estimates may differ before the result is called
#: partial. A tenth of the larger value or ten milliseconds of CPU, whichever
#: is greater: the two are sampled at different instants, so exact agreement
#: is not the standard. Agreement within this is evidence they are measuring
#: the same work; disagreement outside it is evidence that one is wrong.
DIVERGENCE_FRACTION = 0.10
DIVERGENCE_FLOOR_NS = 10_000_000

#: One kernel task start time is reported in clock ticks. USER_HZ is 100 on
#: every configuration this platform deploys; the conversion happens once,
#: here, so a guest-side tick value is never compared against a nanosecond
#: field in a unit nobody wrote down.
USER_HZ = 100
NS_PER_TICK = 1_000_000_000 // USER_HZ

_STATE_COLLECTING = "collecting"
_STATE_FINAL = "final"
STATUS_AVAILABLE = "available"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ThreadReading:
    """One live task's cumulative counters at one census.

    ``instance`` is the task's stable identity: the guest boot, the kernel's
    own start time for it, and its TID. A recycled TID therefore produces a
    different instance and can never inherit the earlier thread's baseline,
    which is precisely the failure a PID-keyed reducer would have.
    """

    instance: str
    tid: int
    tgid: int
    uid: int
    cpu_ns: int | None = None
    read_bytes: int | None = None
    write_bytes: int | None = None
    #: True when the kernel's own start time for this instance falls inside the
    #: window. Such a task began after the window opened, so everything it has
    #: accumulated is this window's work; an instance that predates the window
    #: is baselined instead, because its earlier consumption belongs to
    #: whatever ran before.
    born_in_window: bool = False


@dataclass(frozen=True)
class WindowSample:
    """One census cycle of one member window, as the guest recorded it.

    The cycle carries one entry per live task, not only the member's totals.
    It has to: a total that moves can be moved by a new task starting as
    easily as by work happening, so a reducer given only totals could not tell
    a member's own growth from a child's arrival. It would then charge that
    child's life again when the kernel settled its exit.

    Field names and paths are deliberately absent from a reading. What a
    member runs is its own business; what it consumed is the platform's.
    """

    sample_seq: int
    guest_mono_ns: int
    counters: int
    tasks: tuple[ThreadReading, ...] = ()
    address_spaces: int = 0
    pss_bytes: int | None = None
    pss_failed: int = 0

    @property
    def pss_is_complete(self) -> bool:
        """Whether every member address space was read in this cycle."""
        return (
            self.counters & WINDOW_PSS != 0
            and self.counters & WINDOW_PSS_COMPLETE != 0
            and self.address_spaces > 0
            and self.pss_failed == 0
        )

    @property
    def readings_are_complete(self) -> bool:
        """Whether every task in this cycle answered its scheduler counter.

        A cycle that could not read some task still reports real readings for
        the tasks that answered, but it does not know how many tasks it
        missed, so it can neither open nor close an instance's range.

        It says nothing about the task I/O counters. A kernel that does not
        account them is not a kernel that failed to read CPU, and tying the two
        together left a guest without task I/O accounting measuring no CPU at
        all. A member with no live task at all does set this: a turn that
        finished its work and went quiet is a measured zero.
        """
        return self.counters & WINDOW_READING_COMPLETE != 0


@dataclass(frozen=True)
class ThreadExit:
    """The kernel's final accounting for one task, as the observer settled it.

    ``instance`` is the stable identity of that task, and ``counter_bits``
    says which counters the kernel actually accounts, so an absent counter
    arrives as unavailable rather than as zero.
    """

    instance: str
    guest_mono_ns: int
    cpu_ns: int | None = None
    read_bytes: int | None = None
    write_bytes: int | None = None
    counter_bits: int = 0
    #: True when this window watched the task start. Only then may its settled
    #: figure be charged in full: the kernel reports a lifetime, and a lifetime
    #: that began before the window is not this window's work. The default is
    #: False because an unproven birth must never be assumed.
    born_in_window: bool = False


@dataclass
class _ThreadState:
    """What one task instance has been charged so far.

    ``cpu_ns`` and the I/O fields hold the last trusted cumulative value for
    that instance, which is what makes every contribution a difference rather
    than a total.
    """

    cpu_ns: int | None = None
    read_bytes: int | None = None
    write_bytes: int | None = None
    last_guest_mono_ns: int = 0
    closed: bool = False
    regressed: bool = False
    #: Whether this window saw the instance start, from a census reading whose
    #: start time falls inside the window or from the fork record itself. It is
    #: what separates a task the window may charge in full from one whose
    #: earlier life belongs to whatever ran before.
    born_in_window: bool = False


@dataclass
class MemberWindowReducer:
    """Fold one member window's censuses and exits into its final summary.

    Fed in event order. It is not a pure function of a list because the whole
    point is that the reducer -- not its caller -- remembers what it has
    already counted. The same exit may be delivered twice and the same reading
    arrives once a second, so only a stateful reducer can promise that each
    instance's work is charged exactly once.

    The rule for one instance is simple enough to state and is what the tests
    pin down: it is charged the difference between consecutive trusted
    readings of *its own* counters, and its settled exit closes that range.
    Anything the kernel reports beyond the last reading is added once; a
    duplicate exit, a repeated reading, or a second source reporting the same
    range adds nothing.
    """

    uid: int
    measurement_id: str
    #: The census clock at window open, so later readings can be placed
    #: relative to it. The sentinel is negative rather than zero because zero
    #: is a legitimate guest reading.
    opened_guest_mono_ns: int = -1
    #: Counts a reader may want and no total can express.
    duplicate_exits: int = 0
    regressions: int = 0
    #: Instances the window saw alive but never saw close.
    unclosed_instances: int = 0
    _threads: dict[str, _ThreadState] = field(default_factory=dict)
    _cpu_ns: int = 0
    _read_bytes: int = 0
    _write_bytes: int = 0
    _cpu_measured: bool = False
    _io_measured: bool = False
    _cpu_partial: bool = False
    _io_partial: bool = False
    _cpu_reasons: set[str] = field(default_factory=set)
    _io_reasons: set[str] = field(default_factory=set)
    _memory_reasons: set[str] = field(default_factory=set)
    _pss_peak_bytes: int | None = None
    _valid_samples: int = 0
    _expected_samples: int = 0
    _max_gap_ms: int = 0
    _last_valid_mono_ns: int | None = None
    _last_sample_seq: int | None = None
    _observed_through_ms: int = 0
    #: A completed cycle in which the member had no tasks at all. That is a
    #: real state -- a turn can finish its work and go quiet -- and it is the
    #: one case where a total of zero is a measurement rather than an absence.
    _saw_empty_window: bool = False
    #: False when the window's final census did not arrive: the collection was
    #: cut off, or the journal slice ended early. Everything measured up to
    #: that point is real, but the tail is missing, so no metric may be
    #: reported as complete. Shortening the window instead would present a
    #: truncated turn as a shorter finished one.
    _readings_complete: bool = True

    # -- ingestion ---------------------------------------------------------

    def note_ring_loss(self, lost_events: int) -> None:
        """Record that the kernel dropped lifecycle events for this guest.

        A lost event is a missing exit record. That is a missing part of the
        window, and it is reported rather than left to look like a task that
        simply never finished.
        """
        if lost_events <= 0:
            return
        self._cpu_partial = True
        self._io_partial = True
        self._cpu_reasons.add("ring_loss")
        self._io_reasons.add("ring_loss")

    def observe_sample(self, sample: WindowSample) -> None:
        """Take one census cycle.

        A complete cycle moves every instance's range forward by the growth of
        its own counters. A cycle that could not read everything moves nothing
        but still records that its instances were alive: an unread counter is
        missing, and a missing reading is not a smaller one.
        """
        self._note_sample_sequence(sample.sample_seq)
        self._expected_samples = max(self._expected_samples, sample.sample_seq + 1)
        self._observed_through_ms = max(
            self._observed_through_ms, self._relative_ms(sample.guest_mono_ns)
        )
        if sample.counters & WINDOW_TRUNCATED:
            self._cpu_partial = True
            self._io_partial = True
            self._cpu_reasons.add("sampling_over_budget")
            self._io_reasons.add("sampling_over_budget")
        if (
            sample.counters & WINDOW_PSS != 0
            and sample.counters & WINDOW_ADDRESS_SPACES_DEDUPED == 0
        ):
            self._memory_reasons.add("address_space_share_unverified")
        for reading in sample.tasks:
            state = self._state_for(reading)
            state.last_guest_mono_ns = max(state.last_guest_mono_ns, sample.guest_mono_ns)
            # The proof is kept even when this cycle may not move a total: a
            # cycle that failed to read one task still saw when the others
            # started, and that fact outlives the cycle it arrived in.
            if reading.born_in_window:
                state.born_in_window = True
            if not sample.readings_are_complete or state.closed:
                continue
            for field_name, value in (
                ("cpu_ns", reading.cpu_ns),
                ("read_bytes", reading.read_bytes),
                ("write_bytes", reading.write_bytes),
            ):
                self._advance(
                    state,
                    field_name,
                    value,
                    sample.guest_mono_ns,
                    reading_born_in_window=reading.born_in_window,
                )
        if not sample.readings_are_complete:
            self._cpu_partial = True
            self._io_partial = True
            self._cpu_reasons.add("sampling_over_budget")
            self._io_reasons.add("sampling_over_budget")
        elif not sample.tasks:
            self._saw_empty_window = True
        self._note_memory_cycle(sample)

    def observe_exit(self, exit_event: ThreadExit) -> None:
        """Close one task instance with the kernel's settled figures.

        Each instance closes once. A repeat delivery is dropped rather than
        reconciled: adding it again would bill one process's life twice, which
        is the exact failure this reducer exists to prevent.
        """
        state = self._threads.get(exit_event.instance)
        if state is None:
            state = _ThreadState()
            self._threads[exit_event.instance] = state
        if exit_event.born_in_window:
            state.born_in_window = True
        if state.closed:
            # A closed instance can still be restated: the kernel's exit record
            # carries the settled I/O and reads zero CPU, and the scheduler's
            # final runtime arrives with the done record for the same task. That
            # is growth rather than a duplicate, so only a restatement that adds
            # nothing counts as one.
            if not self._restate(state, exit_event):
                self.duplicate_exits += 1
            return
        self._observed_through_ms = max(
            self._observed_through_ms, self._relative_ms(exit_event.guest_mono_ns)
        )
        state.last_guest_mono_ns = max(state.last_guest_mono_ns, exit_event.guest_mono_ns)
        self._close(
            state,
            "cpu_ns",
            exit_event.cpu_ns,
            exit_event.counter_bits,
            EXIT_COUNTER_CPU,
            self._cpu_reasons,
        )
        self._close(
            state,
            "read_bytes",
            exit_event.read_bytes,
            exit_event.counter_bits,
            EXIT_COUNTER_IO,
            self._io_reasons,
        )
        self._close(
            state,
            "write_bytes",
            exit_event.write_bytes,
            exit_event.counter_bits,
            EXIT_COUNTER_IO,
            self._io_reasons,
        )
        state.closed = True

    # -- internals ---------------------------------------------------------

    def _state_for(self, reading: ThreadReading) -> _ThreadState:
        state = self._threads.get(reading.instance)
        if state is None:
            state = _ThreadState()
            self._threads[reading.instance] = state
        return state

    def _note_sample_sequence(self, sample_seq: int) -> None:
        """Treat a gap in the requester's own counter as a missing cycle."""
        if self._last_sample_seq is not None and sample_seq > self._last_sample_seq + 1:
            self._cpu_reasons.add("sampling_over_budget")
            self._io_reasons.add("sampling_over_budget")
            self._memory_reasons.add("sampling_over_budget")
        if self._last_sample_seq is None or sample_seq > self._last_sample_seq:
            self._last_sample_seq = sample_seq

    def _relative_ms(self, guest_mono_ns: int) -> int:
        if self.opened_guest_mono_ns < 0 or guest_mono_ns <= self.opened_guest_mono_ns:
            return 0
        return (guest_mono_ns - self.opened_guest_mono_ns) // 1_000_000

    def _advance(
        self,
        state: _ThreadState,
        field_name: str,
        value: int | None,
        guest_mono_ns: int,
        *,
        reading_born_in_window: bool = False,
    ) -> None:
        """Add one instance's growth for one counter since its last reading."""
        if value is None:
            self._counter_missing(field_name)
            return
        previous = getattr(state, field_name)
        if previous is None:
            # This is the instance's first reading. A task that started before
            # the window opens a range here: its earlier work belongs to
            # whatever ran before. A task born inside the window owes
            # everything it has already accumulated, because none of it
            # predates the window.
            setattr(state, field_name, value)
            if field_name == "cpu_ns":
                self._cpu_measured = True
            else:
                self._io_measured = True
            if reading_born_in_window:
                self._add(field_name, value)
            return
        if value < previous:
            self._regressed(state, field_name)
            return
        setattr(state, field_name, value)
        self._add(field_name, value - previous)
        state.last_guest_mono_ns = max(state.last_guest_mono_ns, guest_mono_ns)

    def _restate(self, state: _ThreadState, exit_event: ThreadExit) -> bool:
        """Apply growth reported after an instance was already closed.

        Returns whether anything changed. Only increases are taken: a value that
        moved backwards is refused exactly as it is for a live reading, because
        reconciling it would mean inventing time.
        """
        changed = False
        for field_name, value in (
            ("cpu_ns", exit_event.cpu_ns),
            ("read_bytes", exit_event.read_bytes),
            ("write_bytes", exit_event.write_bytes),
        ):
            if value is None:
                continue
            previous = getattr(state, field_name)
            if previous is not None and value < previous:
                self._regressed(state, field_name)
                continue
            if previous is None and not state.born_in_window:
                # Same rule as a first close: an unproven birth means the
                # figure may include work from before the window.
                self._missing_baseline(
                    field_name,
                    self._cpu_reasons if field_name == "cpu_ns" else self._io_reasons,
                )
                continue
            if previous is None or value > previous:
                delta = value if previous is None else value - previous
                setattr(state, field_name, value)
                self._add(field_name, delta)
                changed = True
        if exit_event.guest_mono_ns > state.last_guest_mono_ns:
            state.last_guest_mono_ns = exit_event.guest_mono_ns
            self._observed_through_ms = max(
                self._observed_through_ms, self._relative_ms(exit_event.guest_mono_ns)
            )
        return changed

    def _close(
        self,
        state: _ThreadState,
        field_name: str,
        value: int | None,
        counter_bits: int,
        required_bit: int,
        reasons: set[str],
    ) -> None:
        """Apply a settled exit as the end of one instance's range.

        The exit value is the kernel's final figure for the task, so the
        amount still owed is whatever the readings had not yet reached. When
        no reading ever arrived, the whole figure is owed -- but only when the
        window saw the task start; otherwise the task may have been running
        before the window opened and its earlier work would be billed here.
        """
        if counter_bits & required_bit == 0 or value is None:
            if field_name == "cpu_ns":
                self._cpu_partial = True
            else:
                self._io_partial = True
            reasons.add(
                "missing_thread_baseline"
                if required_bit == EXIT_COUNTER_CPU
                else "filesystem_attribution_unverified"
            )
            return
        previous = getattr(state, field_name)
        if previous is None:
            if not state.born_in_window:
                # No reading ever opened a range for this instance and nothing
                # proved it started here. The kernel's figure is a lifetime,
                # and a lifetime that may predate the window cannot be charged
                # to it: a resident process that has run for an hour and exits
                # during this turn would otherwise bill the hour. So the part
                # is reported missing rather than repaired with a zero
                # baseline, which is the one repair this reducer never makes.
                self._missing_baseline(field_name, reasons)
                return
            # Born and died between two censuses, which is exactly the case
            # the exit record exists to measure.
            setattr(state, field_name, value)
            self._add(field_name, value)
            return
        if value < previous:
            self._regressed(state, field_name)
            return
        setattr(state, field_name, value)
        self._add(field_name, value - previous)

    def _add(self, field_name: str, amount: int) -> None:
        if amount < 0:
            return
        if field_name == "cpu_ns":
            self._cpu_ns += amount
            self._cpu_measured = True
        elif field_name == "read_bytes":
            self._read_bytes += amount
            self._io_measured = True
        else:
            self._write_bytes += amount
            self._io_measured = True

    def _missing_baseline(self, field_name: str, reasons: set[str]) -> None:
        """Mark a metric short because an instance had no range to close.

        The amount is not guessed at from either end: an instance with no
        baseline contributes nothing rather than its whole settled figure, and
        the metric says it is a floor.
        """
        if field_name == "cpu_ns":
            self._cpu_partial = True
        else:
            self._io_partial = True
        reasons.add("missing_thread_baseline")

    def _counter_missing(self, field_name: str) -> None:
        """A counter this kernel does not account is unmeasured, not zero."""
        if field_name == "cpu_ns":
            self._cpu_partial = True
            self._cpu_reasons.add("missing_thread_baseline")
        else:
            self._io_partial = True
            self._io_reasons.add("filesystem_attribution_unverified")

    def _regressed(self, state: _ThreadState, field_name: str) -> None:
        """Refuse to reconcile a counter that moved backwards.

        Reconciling it would mean inventing time, so this instance stops
        contributing and the metric says it is short instead of silently
        becoming a smaller number.
        """
        state.regressed = True
        self.regressions += 1
        if field_name == "cpu_ns":
            self._cpu_partial = True
            self._cpu_reasons.add("counter_regressed")
        else:
            self._io_partial = True
            self._io_reasons.add("counter_regressed")

    def _note_memory_cycle(self, sample: WindowSample) -> None:
        """Let only a complete cycle set the peak.

        An incomplete cycle is not a smaller peak, it is an unknown one: PSS
        summed over the address spaces that happened to answer describes a
        different set of processes, so it is excluded rather than compared.
        """
        if not sample.pss_is_complete:
            self._memory_reasons.add("pss_unreadable")
            return
        self._valid_samples += 1
        value = sample.pss_bytes or 0
        if self._pss_peak_bytes is None or value > self._pss_peak_bytes:
            self._pss_peak_bytes = value
        if self._last_valid_mono_ns is not None:
            gap_ms = (sample.guest_mono_ns - self._last_valid_mono_ns) // 1_000_000
            self._max_gap_ms = max(self._max_gap_ms, gap_ms)
        self._last_valid_mono_ns = sample.guest_mono_ns

    # -- results -----------------------------------------------------------

    def snapshot(self, *, state: str) -> dict[str, Any]:
        """This window's metrics as they stand, with no window geometry.

        A running checkpoint and a finished report describe the same numbers;
        only the window's duration and end differ, and those are facts about
        the turn rather than about the counters. Keeping them separate is what
        lets a checkpoint be posted without pretending the turn has ended.
        """
        self.unclosed_instances = sum(
            1 for state_ in self._threads.values() if not state_.closed and not state_.regressed
        )
        if not self._readings_complete:
            self._cpu_partial = True
            self._io_partial = True
            self._cpu_reasons.add("window_prefix_retained")
            self._io_reasons.add("window_prefix_retained")
            self._memory_reasons.add("window_prefix_retained")
        cpu_status = self._metric_status(
            self._cpu_measured or self._saw_empty_window,
            self._cpu_partial,
            self._cpu_reasons,
        )
        io_status = self._metric_status(
            self._io_measured or self._saw_empty_window,
            self._io_partial,
            self._io_reasons,
        )
        memory_status = self._memory_status()
        return {
            "schema_version": MEMBER_WINDOW_SCHEMA_VERSION,
            "measurement_id": self.measurement_id,
            "state": state,
            "scope": MEMBER_WINDOW_SCOPE,
            "source": MEMBER_WINDOW_SOURCE,
            "cpu": {
                "status": cpu_status,
                "method": "live_and_exit_task_counters",
                "usec": None if cpu_status == STATUS_UNAVAILABLE else self._cpu_ns // 1_000,
                "reason_codes": sorted(self._cpu_reasons),
            },
            "memory": {
                "status": memory_status,
                "method": "sampled_member_pss",
                "peak_bytes": (
                    None if memory_status == STATUS_UNAVAILABLE else self._pss_peak_bytes
                ),
                "sample_interval_ms": MEMBER_WINDOW_SAMPLE_INTERVAL_MS,
                "valid_samples": self._valid_samples,
                "expected_samples": max(self._expected_samples, self._valid_samples),
                "max_gap_ms": self._max_gap_ms,
                "reason_codes": sorted(self._memory_reasons),
            },
            "storage_io": {
                "status": io_status,
                "method": "task_io_accounting",
                "read_bytes": None if io_status == STATUS_UNAVAILABLE else self._read_bytes,
                "write_bytes": None if io_status == STATUS_UNAVAILABLE else self._write_bytes,
                "reason_codes": sorted(self._io_reasons),
            },
        }

    def summary(
        self,
        *,
        state: str,
        duration_ms: int,
        open_scan_ms: int = 0,
        close_scan_ms: int = 0,
    ) -> dict[str, Any]:
        """Freeze this window into the reported snapshot.

        ``state`` comes from the caller because only the caller knows whether
        the turn ended or the collector did. A window whose collection stopped
        early is reported as a prefix with ``observed_through_ms``, never
        rounded up to the window's end.
        """
        observed = max(0, self._observed_through_ms)
        duration = max(0, duration_ms)
        if state == _STATE_FINAL and self.opened_guest_mono_ns >= 0 and observed < duration:
            # The closing census is missing. The window really ran for
            # ``duration_ms``; the data reached ``observed``. Keeping the true
            # end and the true cut-off lets a reader see the gap, which is the
            # whole reason both fields exist, and no metric may then claim to
            # be complete. Without an opening tick there is nothing to compare
            # against, so the window keeps its own length.
            #
            # The state has to change with it. A ``final`` report describing a
            # window prefix is refused by the request schema, so leaving it
            # final would have produced a snapshot the API rejects outright --
            # and the runner swallows that, so the whole turn would have gone
            # unreported. ``collecting`` is the honest state for a window whose
            # collection stopped early: it is read back as ``interrupted`` once
            # the attempt is terminal, which is precisely what happened.
            self._readings_complete = False
            state = _STATE_COLLECTING
        snapshot = self.snapshot(state=state)
        snapshot["window"] = {
            "duration_ms": duration,
            "observed_through_ms": min(observed, duration),
            "open_scan_ms": max(0, open_scan_ms),
            "close_scan_ms": max(0, close_scan_ms),
        }
        return snapshot

    def _metric_status(
        self,
        measured: bool,
        partial: bool,
        reasons: set[str],
    ) -> str:
        """One metric's completeness, judged from its own evidence alone."""
        if not measured:
            return STATUS_UNAVAILABLE
        if partial or reasons:
            return STATUS_PARTIAL
        return STATUS_AVAILABLE

    def _memory_status(self) -> str:
        if self._pss_peak_bytes is None:
            return STATUS_UNAVAILABLE
        if self._memory_reasons:
            return STATUS_PARTIAL
        return STATUS_AVAILABLE


def instance_key(*, boot_id: str, starttime_ticks: int, tid: int) -> str:
    """The stable identity of one task for the lifetime of one guest boot.

    A TID alone is reusable and the kernel reuses it aggressively: the same
    number can name a member's agent process in one window and an unrelated
    tool process in the next. Pairing the boot with the kernel's own start
    time for the task makes the key unique, so a reused number produces a new
    instance with no inherited baseline.
    """
    return f"{boot_id}:{starttime_ticks}:{tid}"


def parse_window_sample(
    line: Mapping[str, Any],
    *,
    boot_id: str,
    opened_guest_mono_ns: int = -1,
) -> WindowSample | None:
    """Read one ``window_sample`` journal record, or None if it is not one.

    A record this parser cannot read is skipped rather than guessed at: the
    caller sees a missing cycle, which is visible in the summary, instead of a
    half-read cycle becoming a baseline.
    """
    if line.get("kind") != "window_sample":
        return None
    window = line.get("window")
    if not isinstance(window, dict):
        return None
    counters = _int_or_none(window.get("counters"))
    sample_seq = _int_or_none(window.get("sample_seq"))
    mono_ns = _int_or_none(line.get("mono_ns"))
    if counters is None or sample_seq is None or mono_ns is None:
        return None
    readings: list[ThreadReading] = []
    detail = window.get("task_detail")
    if isinstance(detail, list):
        for entry in detail:
            reading = _parse_thread_reading(
                entry,
                boot_id=boot_id,
                window_opened_ns=opened_guest_mono_ns,
            )
            if reading is not None:
                readings.append(reading)
    return WindowSample(
        sample_seq=sample_seq,
        guest_mono_ns=mono_ns,
        counters=counters,
        tasks=tuple(readings),
        address_spaces=_int_or_none(window.get("address_spaces")) or 0,
        pss_bytes=_int_or_none(window.get("pss_bytes")),
        pss_failed=_int_or_none(window.get("pss_failed")) or 0,
    )


def _parse_thread_reading(
    entry: Any,
    *,
    boot_id: str,
    window_opened_ns: int = -1,
) -> ThreadReading | None:
    """One per-task entry, or None when it lacks a usable identity.

    The start time is in clock ticks since the guest boot. Comparing it with
    the window's own opening tick is what separates "this task was already
    running" from "this task started while we were measuring": the first is a
    baseline, the second is work the window owes in full. When the opening tick
    is unknown the instance is treated as pre-existing, which under-counts
    rather than inventing work the window may not have caused.
    """
    if not isinstance(entry, dict):
        return None
    tid = _int_or_none(entry.get("tid"))
    tgid = _int_or_none(entry.get("tgid"))
    uid = _int_or_none(entry.get("uid"))
    starttime = _int_or_none(entry.get("starttime"))
    if tid is None or tgid is None or uid is None or starttime is None:
        return None
    born_in_window = False
    if window_opened_ns >= 0:
        started_ns = starttime * NS_PER_TICK
        born_in_window = started_ns >= window_opened_ns
    return ThreadReading(
        instance=instance_key(boot_id=boot_id, starttime_ticks=starttime, tid=tid),
        tid=tid,
        tgid=tgid,
        uid=uid,
        cpu_ns=_int_or_none(entry.get("cpu_ns")),
        read_bytes=_int_or_none(entry.get("read_bytes")),
        write_bytes=_int_or_none(entry.get("write_bytes")),
        born_in_window=born_in_window,
    )


def parse_window_samples(
    payload: bytes,
    *,
    measurement_id: str,
    boot_id: str,
    opened_guest_mono_ns: int = -1,
) -> list[WindowSample]:
    """Read one measurement's cycles out of a journal slice, in order.

    Damaged lines are skipped, not repaired: treating a truncated record as an
    empty cycle would make a lost sample look like a moment when the member
    used nothing. Cycles belonging to another measurement on the same guest
    are ignored, so a fork sibling's window can never contribute here.
    """
    samples: list[WindowSample] = []
    for raw in payload.splitlines():
        if not raw.strip():
            continue
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(decoded, dict) or decoded.get("measurement_id") != measurement_id:
            continue
        sample = parse_window_sample(
            decoded,
            boot_id=boot_id,
            opened_guest_mono_ns=opened_guest_mono_ns,
        )
        if sample is not None:
            samples.append(sample)
    samples.sort(key=lambda item: item.sample_seq)
    return samples


class ExitTracker:
    """Per-thread exits for one window, fed the journal one slice at a time.

    The identity state lives here rather than in a single call because a
    census and the exit it pairs with can fall either side of a read boundary.
    A per-slice parse would then name one thread twice -- its authoritative
    start time arrives with the census in one slice, the exit lands in the
    next -- and the window would charge its live reading and its settled exit
    as two tasks.

    An earlier version solved that by re-parsing everything read so far on
    every tick, which is correct but costs O(cycles^2): at 20 tasks a cycle the
    per-tick parse grew from 1.5 ms to 22 ms across 400 ticks. Keeping the
    state here gives the same identity continuity while parsing each byte once.

    Identity is per *lifecycle*, not per task number. The kernel reuses a TID
    the moment its owner is reaped, so one number can name two unrelated tasks
    inside one window; each number tracks the life it currently owns and
    whether that life has ended.
    """

    def __init__(self, *, boot_id: str, measurement_id: str, uid: int) -> None:
        self._boot_id = boot_id
        self._measurement_id = measurement_id
        self._uid = uid
        self._births: dict[tuple[int, int], int] = {}
        self._current: dict[tuple[int, int], str] = {}
        self._event_index: dict[str, int] = {}
        self._ended: dict[tuple[int, int], bool] = {}
        self._seen_exit: set[str] = set()
        #: Instances whose ``done`` record has arrived. The exit record carries
        #: the settled I/O but reads zero CPU -- the scheduler's runtime is only
        #: final once the task leaves the CPU for the last time -- so emitting
        #: an instance before its done record would report a thread's CPU as
        #: nothing. A done record can be lost, so an instance is also released
        #: on the slice after its exit rather than waited on forever.
        self._settled: set[str] = set()
        self._events: list[ThreadExit] = []
        self._emitted: set[str] = set()
        self._slices = 0
        #: The slice each instance was first seen in, so one that never gets a
        #: done record is still released rather than waited on.
        self._pending_since: dict[str, int] = {}
        #: What was last handed over per instance, so a restated exit -- the
        #: exit record reads zero CPU and the done record carries the
        #: scheduler's final runtime -- is reported again rather than dropped.
        self._last_emitted: dict[str, ThreadExit] = {}
        #: Instances this window watched start, which is what lets the reducer
        #: charge a settled exit in full. Only a fork record inside the window
        #: proves it here; a task first seen at its own exit is not proven, and
        #: is left for the reducer to refuse rather than guessed at.
        self._born: dict[str, bool] = {}

    def _key_for(self, tid: int, starttime_ticks: int) -> str:
        return instance_key(boot_id=self._boot_id, starttime_ticks=starttime_ticks, tid=tid)

    def _start_life(
        self,
        tid: int,
        tgid: int,
        starttime_ticks: int,
        *,
        born: bool = False,
    ) -> str:
        """Begin a new lifecycle for one task number.

        ``born`` records that this window saw the life begin. It defaults to
        False because most lives are discovered rather than witnessed: a census
        that lists a task, or an exit record for one nobody had seen, says
        nothing about whether it started before the window opened.
        """
        identifier = self._key_for(tid, starttime_ticks)
        self._births[(tid, tgid)] = starttime_ticks
        self._current[(tid, tgid)] = identifier
        self._ended[(tid, tgid)] = False
        self._event_index.pop(identifier, None)
        self._seen_exit.discard(identifier)
        if born:
            self._born[identifier] = True
        return identifier

    def consume(self, payload: bytes) -> list[ThreadExit]:
        """Fold one slice in and return the exits it finished or completed.

        Only records this call had not already reported are returned, so a
        caller that re-reads a slice cannot be handed the same exit twice.
        """
        for raw in payload.splitlines():
            if not raw.strip():
                continue
            try:
                decoded = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(decoded, dict):
                continue
            self._observe(decoded)
        self._slices += 1
        return self._drain()

    def _drain(self) -> list[ThreadExit]:
        """Hand over every instance whose counters are as settled as they get.

        An instance is ready when its ``done`` record has arrived, because that
        is where the scheduler's final runtime is read. One whose done record
        has not arrived by the end of the following slice is handed over anyway:
        a lost record would otherwise hold that thread's CPU back forever, and a
        floor reported late is better than a total that never appears.
        """
        fresh: list[ThreadExit] = []
        for identifier, index in self._event_index.items():
            event = self._events[index]
            if event == self._last_emitted.get(identifier):
                continue
            if identifier not in self._emitted:
                ready = identifier in self._settled or self._slices > self._pending_since.get(
                    identifier, self._slices
                )
                if not ready:
                    continue
            self._emitted.add(identifier)
            self._last_emitted[identifier] = event
            fresh.append(event)
        return fresh

    def _observe(self, decoded: dict[str, Any]) -> None:
        kind = decoded.get("kind")
        mono_ns = _int_or_none(decoded.get("mono_ns"))
        if kind == "window_sample":
            if decoded.get("measurement_id") != self._measurement_id:
                return
            window = decoded.get("window")
            detail = window.get("task_detail") if isinstance(window, dict) else None
            if not isinstance(detail, list):
                return
            for entry in detail:
                if not isinstance(entry, dict):
                    continue
                tid = _int_or_none(entry.get("tid"))
                tgid = _int_or_none(entry.get("tgid"))
                starttime = _int_or_none(entry.get("starttime"))
                if tid is None or tgid is None or starttime is None:
                    continue
                task = (tid, tgid)
                known = self._births.get(task)
                if known is None:
                    self._start_life(tid, tgid, starttime)
                    continue
                if known == starttime:
                    continue
                # The kernel's own start time disagrees with what this number
                # was carrying: the old life is over either way, so a reused
                # number becomes a second instance and this becomes the
                # authoritative stamp for a life still in progress.
                carried = False
                if not self._ended.get(task):
                    previous = self._current.get(task)
                    if previous is not None:
                        self._event_index.pop(previous, None)
                        self._emitted.discard(previous)
                        # The same life under a better stamp, so what was
                        # proved about its start is proved about this key too.
                        carried = self._born.get(previous, False)
                self._start_life(tid, tgid, starttime, born=carried)
            return
        tid = _int_or_none(decoded.get("pid"))
        tgid = _int_or_none(decoded.get("tgid"))
        if tid is None or tgid is None or mono_ns is None:
            return
        task = (tid, tgid)
        if kind == "fork":
            if self._ended.get(task, True) or task not in self._births:
                self._start_life(tid, tgid, mono_ns // NS_PER_TICK, born=True)
            else:
                # The life is already being followed, and this record is what
                # proves it began inside the window.
                current = self._current.get(task)
                if current is not None:
                    self._born[current] = True
            return
        if kind not in ("exit", "done"):
            return
        if _int_or_none(decoded.get("uid")) != self._uid:
            return
        closed_life = self._seen_exit.intersection({self._current.get(task, "")})
        if self._ended.get(task) and kind == "done" and closed_life:
            # The life ended at sched_process_exit; this is the scheduler's
            # final runtime for it and belongs to that instance.
            identifier = self._current[task]
        else:
            if self._ended.get(task) or task not in self._current:
                self._start_life(tid, tgid, mono_ns // NS_PER_TICK)
            identifier = self._current[task]
        counters = _int_or_none(decoded.get("counters")) or 0
        cpu_ns = _int_or_none(decoded.get("cpu_ns"))
        read_bytes = _int_or_none(decoded.get("read_bytes"))
        write_bytes = _int_or_none(decoded.get("write_bytes"))
        if kind == "done":
            self._settled.add(identifier)
        index = self._event_index.get(identifier)
        if index is None:
            self._event_index[identifier] = len(self._events)
            self._pending_since[identifier] = self._slices
            self._events.append(
                ThreadExit(
                    instance=identifier,
                    guest_mono_ns=mono_ns,
                    cpu_ns=cpu_ns if counters & EXIT_COUNTER_CPU else None,
                    read_bytes=read_bytes if counters & EXIT_COUNTER_IO else None,
                    write_bytes=write_bytes if counters & EXIT_COUNTER_IO else None,
                    counter_bits=counters & (EXIT_COUNTER_CPU | EXIT_COUNTER_IO),
                    born_in_window=self._born.get(identifier, False),
                )
            )
        else:
            merged = self._events[index]
            self._events[index] = replace(
                merged,
                guest_mono_ns=max(merged.guest_mono_ns, mono_ns),
                cpu_ns=cpu_ns if cpu_ns is not None else merged.cpu_ns,
                read_bytes=read_bytes if read_bytes is not None else merged.read_bytes,
                write_bytes=write_bytes if write_bytes is not None else merged.write_bytes,
                counter_bits=merged.counter_bits
                | (counters & (EXIT_COUNTER_CPU | EXIT_COUNTER_IO)),
                # Proof can arrive after the first record of an exit did.
                born_in_window=merged.born_in_window or self._born.get(identifier, False),
            )
        if kind == "exit":
            self._ended[task] = True
            self._seen_exit.add(identifier)


def exit_events_from_journal(
    payload: bytes,
    *,
    boot_id: str,
    measurement_id: str,
    uid: int,
) -> list[ThreadExit]:
    """Per-thread exits from one journal slice, with no state carried over.

    A convenience for callers that hold the whole journal at once, such as the
    closing reduction. A caller consuming the journal incrementally uses
    :class:`ExitTracker` instead, which is the same logic with identity kept
    between slices.
    """
    tracker = ExitTracker(boot_id=boot_id, measurement_id=measurement_id, uid=uid)
    return tracker.consume(payload)


def exit_events_from_processes(
    processes: Iterable[Any],
    *,
    boot_id: str,
    counter_bits: int = EXIT_COUNTER_CPU | EXIT_COUNTER_IO,
) -> list[ThreadExit]:
    """Turn observed exit rows into window exit events.

    Only rows the kernel actually settled are used. A row for a process that
    outlived the turn carries no consumption by construction, and a row whose
    start time the observer never learned cannot become a stable instance key,
    so it is skipped rather than keyed by PID.
    """
    events: list[ThreadExit] = []
    for process in processes:
        if not getattr(process, "observed_exit", False):
            continue
        started = getattr(process, "started_guest_mono_ns", None)
        pid = getattr(process, "pid", None)
        if not isinstance(started, int) or not isinstance(pid, int) or started <= 0:
            continue
        cpu_usec = getattr(process, "cpu_usec", None)
        events.append(
            ThreadExit(
                instance=instance_key(
                    boot_id=boot_id,
                    starttime_ticks=started // NS_PER_TICK,
                    tid=pid,
                ),
                guest_mono_ns=getattr(process, "exited_guest_mono_ns", 0) or 0,
                cpu_ns=None if cpu_usec is None else int(cpu_usec) * 1_000,
                read_bytes=getattr(process, "io_read_bytes", None),
                write_bytes=getattr(process, "io_write_bytes", None),
                counter_bits=counter_bits,
            )
        )
    return events


def _int_or_none(value: Any) -> int | None:
    """Coerce a JSON number to a non-negative int, or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


#: How often a running turn may post another cumulative checkpoint. The
#: running report exists so a crash keeps the work measured up to it; posting
#: once a second would spend more on reports than on the measurement, and the
#: snapshot is cumulative, so a lost checkpoint is superseded rather than
#: missing.
RUNNING_CHECKPOINT_INTERVAL_S = 15.0


class CheckpointSchedule:
    """Decide when a running turn may post another cumulative checkpoint.

    The first call always reports due: a turn that lasts twenty seconds should
    leave one checkpoint behind even if it never crosses an exact interval
    boundary. After that it fires at most once per interval, so a caller that
    polls it every second cannot flood the control plane with reports that
    differ only in their last second.
    """

    def __init__(
        self,
        *,
        interval_s: float = RUNNING_CHECKPOINT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval_s = max(0.0, interval_s)
        self._clock = clock
        self._last_sent: float | None = None

    @property
    def interval_s(self) -> float:
        """The least time between two checkpoints."""
        return self._interval_s

    def due(self) -> bool:
        """Whether another checkpoint may be posted right now."""
        if self._last_sent is None:
            return True
        return (self._clock() - self._last_sent) >= self._interval_s

    def mark_sent(self) -> None:
        """Record that a checkpoint was posted."""
        self._last_sent = self._clock()


#: The helper that asks the observer for a census. It is a separate reviewed
#: binary from the tool marker, and the observer accepts a window request only
#: from it, so this name is a security boundary rather than a convenience.
MEMBER_WINDOW_DRIVER = "/usr/local/libexec/freeinference-member-window"


@dataclass(frozen=True)
class MemberWindowHandle:
    """One open member window, and where its journal slice begins.

    ``cursor`` is the observation fence taken *before* the opening census, so
    the slice read at close contains the baseline reading as well as every
    cycle after it. Taking the fence afterwards would open a window after its
    own baseline and silently lose the first second of every turn.
    """

    measurement_id: str
    uid: int
    cursor: Any
    opened_host_mono: float
    open_scan_ms: int
    baseline_taken: bool = True
    #: CPU quota of this sandbox at window open; never inferred for old records.
    cpu_capacity_cores: float | None = None


class MemberWindowSampler:
    """Drive one member window through the trusted guest helper.

    Three operations, each a bounded request over the same reviewed transport
    the rest of the guest telemetry uses: open (register and take the opening
    census), sample (one more census), close (stop tracking). ``sample`` never
    raises, because a census that cannot be taken is a missing cycle -- which
    the summary reports -- and never a reason to fail the user's turn.

    The caller owns the member's turn lock, so windows for one member cannot
    overlap. Fork siblings use their own UID and their own handle and may
    therefore sample at the same time.
    """

    def __init__(
        self,
        *,
        run: Callable[[list[str], float], Any],
        interval_s: float = MEMBER_WINDOW_SAMPLE_INTERVAL_MS / 1000,
    ) -> None:
        self._run = run
        self._interval_s = max(0.1, interval_s)

    @property
    def interval_s(self) -> float:
        """The planned period between censuses."""
        return self._interval_s

    def _request(self, argv: list[str]) -> bool:
        """Send one fixed request; True only when it was acknowledged."""
        try:
            result = self._run(argv, self.request_timeout_s())
        except Exception:
            return False
        return getattr(result, "returncode", 1) == 0

    def request_timeout_s(self) -> float:
        """One bounded request, independent of the execution transport's own.

        A census has to finish inside a cycle or the next one overlaps it, so
        waiting longer would only queue work that no longer describes the
        window it was asked about.
        """
        return max(0.5, min(2.0, self._interval_s * 2))

    def open(self, *, uid: int, measurement_id: str, cursor: Any) -> MemberWindowHandle:
        """Register the member and take the window's opening census.

        The handle is returned even when the baseline request failed: the
        window is then unusable, but the caller still has to close it so the
        observer does not keep an entry for a turn that has already ended.
        """
        started = time.monotonic()
        acknowledged = self._request([MEMBER_WINDOW_DRIVER, "register", str(uid), measurement_id])
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        return MemberWindowHandle(
            measurement_id=measurement_id,
            uid=uid,
            cursor=cursor,
            opened_host_mono=started,
            open_scan_ms=elapsed_ms if acknowledged else 0,
            baseline_taken=acknowledged,
        )

    def sample(self, handle: MemberWindowHandle, sample_seq: int) -> bool:
        """Take one more census. A failure is a missing cycle, not an error."""
        return self._request(
            [
                MEMBER_WINDOW_DRIVER,
                "sample",
                str(handle.uid),
                handle.measurement_id,
                str(sample_seq),
            ]
        )

    def close(self, handle: MemberWindowHandle) -> tuple[bool, int]:
        """Stop tracking the window, returning whether it was acknowledged.

        Closing is separate from the last census on purpose: the caller takes
        the closing reading first, so the window ends where the turn ended and
        not wherever the helper happened to stop listening.
        """
        started = time.monotonic()
        acknowledged = self._request(
            [MEMBER_WINDOW_DRIVER, "close", str(handle.uid), handle.measurement_id]
        )
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        return acknowledged, elapsed_ms


class WindowAccumulator:
    """One window's cumulative measurement, kept across journal reads.

    A checkpoint exists so a crash keeps the work already done, which means it
    has to describe the same counters the final report will -- not a fresh
    reducer that has seen nothing. This holds one reducer for the window's
    whole life and consumes the journal in order, so a cycle is credited once
    however many times a checkpoint happens.

    The offset is the contract that makes replay safe: only bytes past it are
    parsed, and it only moves once they have been folded in. Consumption is
    serialized because the census ticks on its own thread while a checkpoint
    may be requested from the turn's.
    """

    def __init__(
        self,
        *,
        uid: int,
        measurement_id: str,
        opened_guest_mono_ns: int,
        boot_id: str,
    ) -> None:
        self._lock = threading.Lock()
        self._reducer = MemberWindowReducer(
            uid=uid,
            measurement_id=measurement_id,
            opened_guest_mono_ns=opened_guest_mono_ns,
        )
        #: Identity state lives here, not in a per-call parse. A census and the
        #: exit it pairs with can fall either side of a read boundary, and a
        #: slice-local parse would name one thread twice.
        self._exits = ExitTracker(
            boot_id=boot_id,
            measurement_id=measurement_id,
            uid=uid,
        )
        self._offset = 0
        self._cycles = 0
        self.measurement_id = measurement_id
        self.uid = uid
        self.opened_guest_mono_ns = opened_guest_mono_ns

    @property
    def offset(self) -> int:
        """How far into the journal this window has consumed."""
        with self._lock:
            return self._offset

    @property
    def cycles(self) -> int:
        """How many census cycles have been folded in."""
        with self._lock:
            return self._cycles

    def at(self, offset: int) -> None:
        """Start consuming from a known fence, for a window opened elsewhere."""
        with self._lock:
            self._offset = max(0, offset)

    def consume(self, payload: bytes, *, boot_id: str, upto_offset: int) -> int:
        """Fold one new slice in, and return how many cycles it held.

        ``boot_id`` is documentation of the caller's fence rather than the
        tracker's input: identity is stamped with the boot the window opened
        on, which cannot change without a new window. Passing a different one
        here would key the same thread under two names, so the constructor is
        where it is fixed.

        The slice is everything read since the last call, so each byte is
        parsed once. Identity still spans the whole window because the tracker
        carries it between calls; deriving it from one slice alone would name a
        thread twice whenever a census and its exit fell either side of a read
        boundary.

        The offset advances only once the slice has been folded in, so a caller
        that re-reads after a failure cannot lose a cycle.
        """
        if not payload:
            return 0
        samples = parse_window_samples(
            payload,
            measurement_id=self.measurement_id,
            boot_id=boot_id,
            opened_guest_mono_ns=self.opened_guest_mono_ns,
        )
        exits = self._exits.consume(payload)
        with self._lock:
            for sample in samples:
                self._reducer.observe_sample(sample)
            for event in exits:
                self._reducer.observe_exit(event)
            self._cycles += len(samples)
            self._offset = max(self._offset, upto_offset)
            return len(samples)

    def snapshot(
        self,
        *,
        state: str,
        duration_ms: int,
        report_seq: int,
        open_scan_ms: int = 0,
        close_scan_ms: int = 0,
    ) -> dict[str, Any]:
        """The window's counters as they stand, with the window still open."""
        with self._lock:
            summary = self._reducer.summary(
                state=state,
                duration_ms=duration_ms,
                open_scan_ms=open_scan_ms,
                close_scan_ms=close_scan_ms,
            )
        # Storage I/O is withheld for the reason `reduce_window` records.
        if True:
            summary["storage_io"] = {
                "status": STATUS_UNAVAILABLE,
                "method": "task_io_accounting",
                "read_bytes": None,
                "write_bytes": None,
                "reason_codes": ["filesystem_attribution_unverified"],
            }
        summary["report_seq"] = max(1, report_seq)
        return summary


def reduce_window(
    *,
    uid: int,
    measurement_id: str,
    boot_id: str,
    samples: Iterable[WindowSample],
    journal_payload: bytes = b"",
    opened_guest_mono_ns: int = -1,
    lost_events: int = 0,
    state: str = _STATE_FINAL,
    duration_ms: int,
    report_seq: int = 1,
    open_scan_ms: int = 0,
    close_scan_ms: int = 0,
) -> dict[str, Any]:
    """Reduce one closed window into its reported snapshot.

    The single entry point the supervisor uses, so the ordering the reducer
    depends on -- every census first, then the settled exits -- is enforced
    here instead of at each call site. The samples come from the observation
    the turn already read, so a window never pays for a second journal read.

    Exit records are the member's own by construction: the observation was
    filtered by UID at its cursor. A shared guest has other members' processes
    in the same journal, and charging them here would bill one member for a
    sibling's work.
    """
    reducer = MemberWindowReducer(
        uid=uid,
        measurement_id=measurement_id,
        opened_guest_mono_ns=opened_guest_mono_ns,
    )
    reducer.note_ring_loss(lost_events)
    for sample in samples:
        reducer.observe_sample(sample)
    for event in exit_events_from_journal(
        journal_payload,
        boot_id=boot_id,
        measurement_id=measurement_id,
        uid=uid,
    ):
        reducer.observe_exit(event)
    summary = reducer.summary(
        state=state,
        duration_ms=duration_ms,
        open_scan_ms=open_scan_ms,
        close_scan_ms=close_scan_ms,
    )
    # Task I/O accounting answers with whatever the filesystem chose to record.
    # Two zeroes are indistinguishable between "the member wrote nothing" and
    # "this filesystem never charged the member at all", and the counter
    # existing is not evidence about which one it is. Until a deployment proves
    # attribution on the filesystem it actually serves, the metric is reported
    # as unmeasured rather than as nothing used. This is unconditional on
    # purpose: a flag nobody can set truthfully is a switch that only ever
    # reads as "not yet".
    if True:
        summary["storage_io"] = {
            "status": STATUS_UNAVAILABLE,
            "method": "task_io_accounting",
            "read_bytes": None,
            "write_bytes": None,
            "reason_codes": ["filesystem_attribution_unverified"],
        }
    summary["report_seq"] = max(1, report_seq)
    return summary
