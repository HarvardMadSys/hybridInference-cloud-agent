"""One member execution window, as the shared-session supervisor holds it.

A window belongs to exactly one activation: it opens on the turn's observation
fence and closes with the turn. Everything the supervisor keeps for it lives
here -- the handle and the sampler that drives it, the running accumulator and
how far the journal has been read into it, the census and report sequences, the
checkpoint cadence, and the checkpoint waiting to be forwarded. Dropping the
window is therefore one assignment, and nothing of one window can outlive it
into the next.

The supervisor keeps the lock, the member lookup and the turn boundary; this
module answers only for the window itself.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from cloud_agent_host.member_resources import (
    CheckpointSchedule,
    MemberWindowHandle,
    MemberWindowSampler,
    WindowAccumulator,
    reduce_window,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.guest_observer import GuestObservation, GuestObserverCursor

#: The supervisor's logger, so these records keep the name operators filter on.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


def window_boot_id(manager: Any, spec: Any, cursor: Any) -> str:
    """The boot every instance key in one window is stamped with.

    The cursor carries it when the observer could report one. Falling back
    to asking the manager keeps a window whose cursor predates that field --
    or a guest that could not answer -- from keying every thread under an
    empty boot, which would let two boots' task numbers collide.
    """
    from_cursor = getattr(cursor, "guest_boot_id", "") or ""
    if from_cursor:
        return from_cursor
    read = getattr(manager, "guest_boot_id", None)
    if read is None:
        return ""
    try:
        return read(spec) or ""
    except Exception:
        return ""


@dataclass(eq=False)
class MemberWindow:
    """One activation's open execution window and the census that runs on it."""

    handle: MemberWindowHandle
    sampler: MemberWindowSampler
    #: This window's cumulative measurement, kept across journal reads so a
    #: checkpoint describes the same counters the final report will.
    accumulator: WindowAccumulator
    checkpoints: CheckpointSchedule
    #: How far the window's journal has been read. Only the offset is kept:
    #: the accumulator carries identity between slices, so retaining every byte
    #: read would grow without bound and re-parse it all on each tick.
    journal_offset: int
    #: The number of the last census the sampler took.
    census_seq: int = 0
    #: The last ``report_seq`` this window handed out. It is separate from the
    #: census counter because both keep moving: the sampler ticks while a
    #: closing census is in flight, so a final report that derived its sequence
    #: from the census counter could come out below a checkpoint that was
    #: produced in between -- and the store rejects a lower sequence, which
    #: would leave a normally completed turn stored as still collecting.
    report_seq: int = 0
    #: When this window's next census is due, on the supervisor's clock.
    next_census_mono: float = 0.0
    #: The newest checkpoint the sampler produced and the turn has not
    #: forwarded yet. It goes with the window: a checkpoint that names this
    #: window's measurement must never reach the next turn's attempt, whose
    #: record the first snapshot it accepts would claim for this window.
    pending_checkpoint: dict[str, Any] | None = None

    @classmethod
    def opened(
        cls,
        handle: MemberWindowHandle,
        sampler: MemberWindowSampler,
        *,
        cursor: GuestObserverCursor | None,
        boot_id: str,
        checkpoint_interval_s: float,
    ) -> MemberWindow:
        """The census state of a window admitted with its turn."""
        return cls(
            handle=handle,
            sampler=sampler,
            accumulator=WindowAccumulator(
                uid=handle.uid,
                measurement_id=handle.measurement_id,
                opened_guest_mono_ns=(getattr(cursor, "guest_mono_ns", 0) or 0),
                boot_id=boot_id,
            ),
            checkpoints=CheckpointSchedule(interval_s=checkpoint_interval_s),
            # The journal read fence starts at the window's opening cursor,
            # not at zero and not at where a previous window stopped: the
            # observer may have rotated the journal before this window began,
            # and a stale offset would sit behind that fence and silence every
            # later census read for this member.
            journal_offset=getattr(cursor, "offset", 0) or 0,
        )

    def duration_ms(self) -> int:
        """How long the window has been open, on the host's clock."""
        return max(0, int((time.monotonic() - self.handle.opened_host_mono) * 1000))

    def census_due(self, now: float) -> int | None:
        """The number of the census due at ``now``, or None when none is.

        A window whose opening census never landed has no baseline, and later
        cycles cannot give it one, so it is never censused again.
        """
        if not self.handle.baseline_taken or now < self.next_census_mono:
            return None
        self.next_census_mono = now + self.sampler.interval_s
        self.census_seq += 1
        return self.census_seq

    def consume_journal(
        self,
        read: Callable[[Any, GuestObserverCursor], tuple[bytes, int]],
        spec: Any,
        cursor: GuestObserverCursor,
    ) -> int:
        """Fold this window's journal, read so far, into its accumulator.

        The slice read is the new tail only -- reading the whole journal once a
        second for the length of a turn would be wasteful -- but it is handed
        on top of what was read before, because identity has to be derived from
        the whole history rather than from whichever slice a record landed in.
        """
        scoped = replace(cursor, offset=self.journal_offset)
        payload, new_offset = read(spec, scoped)
        # A journal read can catch the observer mid-write, and the tail of such
        # a read is half a record. Advancing past it would lose that cycle for
        # good and leave nothing in the accounting to say so, so the offset
        # stops at the last complete line and the partial tail is read again
        # once the writer has finished it.
        complete = payload.rfind(b"\n") + 1
        self.journal_offset = new_offset - (len(payload) - complete)
        return self.accumulator.consume(
            payload[:complete],
            boot_id=cursor.guest_boot_id,
            upto_offset=self.journal_offset,
        )

    def checkpoint_seq(self) -> int | None:
        """The report sequence of a checkpoint due now, or None when none is.

        The schedule owns the cadence, so a caller may ask on any poll and only
        the interval's end produces a report.
        """
        if not self.handle.baseline_taken or not self.checkpoints.due():
            return None
        self.checkpoints.mark_sent()
        self.report_seq += 1
        return self.report_seq

    def collecting_snapshot(self, report_seq: int, *, duration_ms: int) -> dict[str, Any]:
        """The counters this window has been credited with so far.

        Built from the running accumulator rather than a fresh reducer, so a
        checkpoint describes the same measurement the final report will, with
        the window still open.
        """
        snapshot = self.accumulator.snapshot(
            state="collecting",
            duration_ms=duration_ms,
            report_seq=report_seq,
            open_scan_ms=self.handle.open_scan_ms,
        )
        snapshot["cpu"]["capacity_cores"] = self.handle.cpu_capacity_cores
        return snapshot

    def freeze(self, observation: GuestObservation, sequence: int) -> dict[str, Any] | None:
        """Close the window and reduce it into the reported snapshot.

        Every failure here degrades to "not measured" rather than raising: a
        turn whose resources could not be counted is still a turn that ran,
        and observation must never be able to fail the user's work.
        """
        if not self.handle.baseline_taken:
            # The opening census never landed, so this window has no baseline.
            # Reducing it would report the member's whole session as this
            # turn's work, which is exactly the attribution this design
            # refuses to make.
            self.close()
            return None
        close_started = time.monotonic()
        self.close()
        close_scan_ms = max(0, int((time.monotonic() - close_started) * 1000))
        cursor = self.handle.cursor
        duration_ms = self.duration_ms()
        try:
            snapshot = reduce_window(
                uid=self.handle.uid,
                measurement_id=self.handle.measurement_id,
                boot_id=getattr(cursor, "guest_boot_id", "") or "",
                samples=observation.window_samples,
                journal_payload=observation.journal_payload,
                opened_guest_mono_ns=getattr(cursor, "guest_mono_ns", 0) or 0,
                lost_events=observation.lost_events,
                state="final",
                duration_ms=duration_ms,
                report_seq=sequence,
                open_scan_ms=self.handle.open_scan_ms,
                close_scan_ms=close_scan_ms,
            )
            snapshot["cpu"]["capacity_cores"] = self.handle.cpu_capacity_cores
            return snapshot
        except Exception:
            logger.warning(
                "member window reduction failed for %s",
                self.handle.measurement_id,
                extra={
                    "event": "shared_member_window_reduce_failed",
                    "measurement_id": self.handle.measurement_id,
                },
                exc_info=True,
            )
            return None

    def close(self) -> None:
        """Stop the observer tracking this window; a helper that cannot answer is ignored."""
        with contextlib.suppress(Exception):
            self.sampler.close(self.handle)
