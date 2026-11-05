"""Census running members' execution windows on their own clock, and park their checkpoints.

The supervisor's sampler thread calls :meth:`WindowSampler.sample_open_windows`
about once a second; a turn's event stream collects the checkpoint parked on
its window with :meth:`WindowSampler.take_reported_checkpoint`.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.parallel_window_census import take_censuses

if TYPE_CHECKING:
    import builtins

    from cloud_agent_host.drivers import DialectRuntime
    from cloud_agent_host.guest_observer import (
        GuestObserverCursor,
    )
    from cloud_agent_host.member_window_census import MemberWindow
    from cloud_agent_host.parallel_window_census import DueCensus
    from cloud_agent_host.resident_transport import (
        ResidentTurn,
    )
    from cloud_agent_host.shared_session_state import (
        SharedSessionState,
    )

# The supervisor's operational log names are stable: this component logs
# under the supervisor's logger, as it did before it was split out.
logger = logging.getLogger("cloud_agent_host.shared_session_supervisor")


class WindowSampler:
    """Sample member windows and build their running checkpoints."""

    def __init__(self, state: SharedSessionState) -> None:
        self._state = state
        self._manager = state.manager

    def sample_open_windows(self) -> int:
        """Take one census for every member window that is due for one.

        The census has its own clock on purpose. Driving it from the turn's
        event stream meant it ran only when the agent happened to emit
        something, so a turn that spent its time waiting on a model produced
        almost no cycles, and the per-instance deltas and the PSS peak were
        computed from a handful of readings while the summary still advertised
        a one-second period.

        Returns how many windows were sampled. A census that fails is not
        retried here: the next tick is a second away and a late census no
        longer describes the moment it was asked about.

        A census only writes its cycle into the guest journal. Nothing reads
        that journal between checkpoints, so it is folded when a checkpoint is
        built rather than after every census, which would cost each running
        member a second guest exec every second.
        """
        now = time.monotonic()
        # Each due census leaves the lock with its window's handle, sampler and
        # number, grouped by the container it enters, so the guest exec happens
        # outside it.
        due: dict[str, builtins.list[DueCensus]] = {}
        with self._state.lock:
            for sandbox_id, group in self._state.groups.items():
                for member in group.members.values():
                    window = member.window
                    census = window.census_due(now) if window is not None else None
                    if window is not None and census is not None:
                        due.setdefault(sandbox_id, []).append(
                            (window.handle, window.sampler, census)
                        )
        # One container's censuses in order, separate containers at once.
        take_censuses(list(due.values()))
        # Reporting runs on the same clock as the census rather than on the
        # turn's event stream. A turn that spends a minute inside one silent
        # tool call emits nothing for that minute, and an upload that waits for
        # an event would leave nothing behind if the host died in it. It runs
        # after every census of this tick, so a checkpoint folds them in.
        self._report_due_checkpoints()
        return sum(len(censuses) for censuses in due.values())

    def take_reported_checkpoint(
        self, thread_id: str, turn: ResidentTurn[DialectRuntime]
    ) -> dict[str, Any] | None:
        """The newest checkpoint of ``turn``'s window not yet handed out, or None.

        The sampler thread produces checkpoints on its own clock; this is how
        the turn's event stream collects them. Taking is destructive so a
        checkpoint is forwarded once, but the window keeps producing new ones
        whether or not anything ever asks. A checkpoint is only ever handed out
        by the window that produced it: one left behind when its turn ended
        goes with that window. It is handed only to the stream of the turn
        the window measures: an abandon or a group stop can end a turn while
        its stream is stalled, and that stream, once it resumes, must not
        forward the member's next turn's checkpoint under its own attempt.
        """
        with self._state.lock:
            group = self._state.members.get(thread_id)
            member = group.members.get(thread_id) if group is not None else None
            if member is None or not member.lease_token or member.current_turn is not turn:
                return None
            window = member.window
            if window is None:
                return None
            pending = window.pending_checkpoint
            window.pending_checkpoint = None
            return pending

    def _report_due_checkpoints(self) -> None:
        """Produce a checkpoint for every window whose interval has elapsed.

        The checkpoint is parked on the window for its turn's stream to forward.
        Nothing else delivers it: the sandbox manager builds this supervisor
        with no observer of its own, so a cadence that waited for one would
        leave a crashed turn with nothing.
        """
        with self._state.lock:
            due = [
                (member.thread_id, member.lease_token)
                for group in self._state.groups.values()
                for member in group.members.values()
                if member.window is not None
                and member.lease_token is not None
                and member.window.checkpoints.due()
            ]
        for thread_id, token in due:
            with contextlib.suppress(Exception):
                self.checkpoint_running_window(thread_id, token)

    def _consume_window_journal(self, window: MemberWindow, cursor: GuestObserverCursor) -> int:
        """Fold a still-open window's journal, read so far, into its accumulator.

        Only a checkpoint reads it (:meth:`checkpoint_running_window`): nothing
        between two checkpoints reads the fold.

        Read errors are swallowed by the caller: a census the host could not
        read back is a gap in the checkpoint, and the next checkpoint tries
        again from the same offset rather than skipping the bytes it never saw.
        They are still there: the window's own cursor keeps the journal from
        rotating while the window is open.
        """
        read = getattr(self._manager, "member_window_journal_delta", None)
        if read is None:
            return 0
        spec = self._spec_holding(window)
        if spec is None:
            return 0
        return window.consume_journal(read, spec, cursor)

    def _spec_holding(self, window: MemberWindow) -> Any | None:
        """The sandbox whose member holds ``window``, or None once it was dropped.

        At most one member holds a window, so the first match is the only one.
        """
        with self._state.lock:
            for group in self._state.groups.values():
                for member in group.members.values():
                    if member.window is window:
                        return group.spec
        return None

    def checkpoint_running_window(
        self,
        thread_id: str,
        token: str,
    ) -> dict[str, Any] | None:
        """Build one cumulative checkpoint for a member's running turn.

        Returns the snapshot to report, or None when no checkpoint is due or
        the member has no open window. The schedule owns the cadence: a caller
        may invoke this on any poll and only the fifteenth second produces a
        report. The censuses themselves are taken by
        :meth:`sample_open_windows` on their own clock, so this decides when to
        upload, and folds in the cycles they journaled since the last report:
        while the turn runs, nothing else reads the window's journal.
        """
        with self._state.lock:
            try:
                _, member = self._state.leased_member(thread_id, token)
            except Exception:
                return None
            window = member.window
            cursor = member.observer_cursor
            report_seq = window.checkpoint_seq() if window is not None else None
            if window is None or report_seq is None:
                return None
        try:
            duration_ms = window.duration_ms()
            # The same counters the final report will carry, with the window
            # still open: a crash keeps everything measured so far.
            #
            # Without a cursor there is no journal fence to read from, so the
            # checkpoint reports what the accumulator already holds. Calling
            # through anyway raised inside the suppression below, which read as
            # a fold that happened rather than one that was never possible.
            if cursor is not None:
                with contextlib.suppress(Exception):
                    self._consume_window_journal(window, cursor)
            snapshot = window.collecting_snapshot(report_seq, duration_ms=duration_ms)
            with self._state.lock:
                window.pending_checkpoint = snapshot
        except Exception:
            logger.warning(
                "member window checkpoint failed for %s",
                window.handle.measurement_id,
                extra={
                    "event": "shared_member_window_checkpoint_failed",
                    "measurement_id": window.handle.measurement_id,
                },
                exc_info=True,
            )
            return None
        return snapshot


__all__ = [
    "WindowSampler",
]
