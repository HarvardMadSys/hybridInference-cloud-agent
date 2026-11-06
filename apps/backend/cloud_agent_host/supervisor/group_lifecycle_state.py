"""A shared sandbox group's lifecycle state, and the only transitions that change it.

The state has four slots for the operations in progress -- a start, a stop,
the exclusive workspace, and a lifecycle check -- each with at most one
holder, recorded as the :class:`Hold` its ``begin_*`` method returned. Next
to them are records of what happened (the last start failed, the container
was lost), the barriers that wait for unfinished archive work, and the label
of the run the group is in. The A2 spec
(``docs/agents/specs/2026-11-05-group-lifecycle-state.zh-CN.md``) derives
which combinations are reachable and why.

Every method ends in ``_locked``: the caller holds ``SharedSessionState.lock``,
whose condition the state notifies when a transition another caller may wait
for ends. A transition checks its preconditions, writes its fields and, where
needed, notifies; admission stays with ``GroupLifecycle.admit``. Nothing
outside this module assigns these fields.

A precondition that does not hold is a violation: it is logged as
``group_lifecycle_violation`` and the transition then does what the code
before this state did, so a violation changes no behaviour. The invariants
the A2 spec proves (:meth:`GroupLifecycleState.check_invariants`) are logged
with the combination counts (``violation:<name>`` parts), and those it does
not prove (:meth:`GroupLifecycleState.unproven`) as ``unproven:<name>``.

Tests switch on :func:`set_strict_checks`: a violated precondition then raises
``AssertionError``, and every transition asserts that its caller holds the
lock and, once it is done, that the proven invariants hold
(:class:`LifecycleInvariantError`).
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Concatenate, Final, Literal, ParamSpec, TypeVar

from cloud_agent_host.session_lifecycle import StaleStartFence

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.supervisor.shared_session_state import (
        SharedMemberManifest,
        _LifecycleCheck,
    )

logger = logging.getLogger("cloud_agent_host.supervisor.group_lifecycle")


class _Checks:
    """Whether violations raise and every transition is checked (tests and debugging)."""

    strict = False


def set_strict_checks(enabled: bool) -> None:
    """Raise on a violated precondition and check every transition, or only log."""
    _Checks.strict = enabled


class LifecycleInvariantError(AssertionError):
    """A transition left a proven invariant broken (strict checks only)."""


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _transition(
    method: Callable[Concatenate[GroupLifecycleState, _P], _R],
) -> Callable[Concatenate[GroupLifecycleState, _P], _R]:
    """Under strict checks, require the lock before ``method`` and the invariants after it."""

    @functools.wraps(method)
    def checked(self: GroupLifecycleState, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if not _Checks.strict:
            return method(self, *args, **kwargs)
        try:
            # Raises unless this thread holds the condition's lock; wakes no one.
            self.condition.notify(0)
        except RuntimeError:
            raise AssertionError(f"{method.__name__} ran without the state lock") from None
        result = method(self, *args, **kwargs)
        if broken := self.check_invariants():
            raise LifecycleInvariantError(f"{method.__name__} broke {', '.join(broken)}")
        return result

    return checked


class StopHolder(Enum):
    """Who holds a group's stop. Admission treats every holder as the same ``STOPPING``."""

    #: ``stop_if_idle``, under its own lifecycle check.
    STOP = "stop"
    #: Reconciliation's retry, finalization or blocked stop, under its own check.
    FINALIZE = "finalize"
    #: ``stop_all_for_shutdown``'s final archive retry.
    SHUTDOWN_RETRY = "shutdown"
    #: An administrator's archive request, from its begin until its commit or abort.
    ARCHIVE_HOLD = "archive_hold"


class ExclusiveHolder(Enum):
    """Who holds a group's exclusive workspace (the wire's ``snapshotting``)."""

    ENVIRONMENT = "environment"
    INITIAL_ARCHIVE = "initial_archive"


class StartOutcome(Enum):
    """How a start ended, which decides the group's ``last_start_failed`` record."""

    COMPLETED = "completed"
    FAILED = "failed"
    #: Refused before Docker was asked (a stale start fence): nothing failed.
    REFUSED = "refused"


#: The kind of the one start hold.
START: Final = "start"

HoldKind = StopHolder | ExclusiveHolder | Literal["start"]


@dataclass(frozen=True, eq=False)
class Hold:
    """One operation in progress; only the caller that began it may end it."""

    kind: HoldKind
    #: The archive request, for :attr:`StopHolder.ARCHIVE_HOLD` only.
    request_id: str | None = None


@dataclass(frozen=True)
class FinalArchive:
    """The final archive a stop still owes.

    ``members`` set is the wire's ``stop_archive_pending`` and the membership
    the archive is for; ``blocked`` is ``stop_archive_blocked``. Without final
    archives configured, a blocked stop owes no upload and ``members`` is
    ``None``.
    """

    members: tuple[SharedMemberManifest, ...] | None
    blocked: bool = False


@dataclass(frozen=True)
class OrphanFinalization:
    """An orphaned group's final archive is still to be finished."""

    marker_unreadable: bool = False


@dataclass(frozen=True)
class RunLabel:
    """The control plane's start fences of the run the group is in.

    ``start_seq`` is the highest fence of the requests this run admitted, and
    ``unfenced`` says a request without a fence joined it, so the run's
    checkpoint covers nothing. ``sealed_seq`` is the highest label of a run
    that ended, or the checkpoint this host found the group with; no request
    under a fence at or below it may start the group again.
    """

    start_seq: int | None = None
    unfenced: bool = False
    sealed_seq: int | None = None


#: The names the wire, the logs and the deletion diagnostics know the state by.
FLAG_NAMES: Final = (
    "container_lost",
    "snapshotting",
    "starting",
    "stopping",
    "initial_archive_preservation_pending",
    "stop_archive_pending",
    "stop_archive_blocked",
    "orphan_finalization_pending",
    "orphan_marker_unreadable",
    "start_archive_completion_pending",
)


@dataclass(eq=False)
class GroupLifecycleState:
    """One group's lifecycle. Read it anywhere under the lock; change it only here."""

    #: The condition of the lock every transition runs under.
    condition: threading.Condition = field(repr=False)
    #: Whether this supervisor archives a group's final state.
    archives_finals: bool
    start: Hold | None = None
    stop: Hold | None = None
    exclusive: Hold | None = None
    check: _LifecycleCheck | None = field(default=None, repr=False)
    #: A failed initial archive observed, or could not rule out, its durable
    #: preparation marker, but the exact container could not yet be stopped.
    #: Only a later accepted STOPPED inspection releases it, with the
    #: exclusive workspace (:meth:`accepted_stopped_locked`).
    initial_preservation: bool = False
    last_start_failed: bool = False
    #: Set once and never cleared: a lost group never runs again.
    container_lost: bool = False
    final_archive: FinalArchive | None = None
    orphan: OrphanFinalization | None = None
    #: A proven start's marker completion failed and is retried before admission.
    start_completion_pending: bool = False
    run: RunLabel = field(default_factory=RunLabel)
    #: The fence clock: advanced by every transition that supersedes an
    #: observation made without the lock.
    generation: int = 0

    # Derived views -------------------------------------------------------

    @property
    def archive_request_id(self) -> str | None:
        """The archive request holding the group's stop, if one does."""
        stop = self.stop
        if stop is not None and stop.kind is StopHolder.ARCHIVE_HOLD:
            return stop.request_id
        return None

    @property
    def final_archive_pending(self) -> bool:
        """Whether a final archive upload is owed (the wire's ``stop_archive_pending``)."""
        return self.final_archive is not None and self.final_archive.members is not None

    @property
    def final_archive_blocked(self) -> bool:
        """Whether the owed final archive is blocked (``stop_archive_blocked``)."""
        return self.final_archive is not None and self.final_archive.blocked

    @property
    def orphan_marker_unreadable(self) -> bool:
        """Whether the orphan's final archive marker could not be read."""
        return self.orphan is not None and self.orphan.marker_unreadable

    @property
    def final_archive_members(self) -> tuple[SharedMemberManifest, ...] | None:
        """The membership an owed final archive is for, if one is owed."""
        return None if self.final_archive is None else self.final_archive.members

    def flags(self) -> dict[str, bool]:
        """The state under the names in :data:`FLAG_NAMES`, in that order."""
        return {
            "container_lost": self.container_lost,
            "snapshotting": self.exclusive is not None,
            "starting": self.start is not None,
            "stopping": self.stop is not None,
            "initial_archive_preservation_pending": self.initial_preservation,
            "stop_archive_pending": self.final_archive_pending,
            "stop_archive_blocked": self.final_archive_blocked,
            "orphan_finalization_pending": self.orphan is not None,
            "orphan_marker_unreadable": self.orphan_marker_unreadable,
            "start_archive_completion_pending": self.start_completion_pending,
        }

    def combination(self) -> tuple[str, ...]:
        """The operations in progress with their holders, then the records and barriers.

        Each part is present only while it holds, in a fixed order, so equal
        states give equal tuples.
        """
        parts: list[str] = []
        if self.start is not None:
            parts.append("start")
        if self.stop is not None:
            parts.append(f"stop:{_kind(self.stop)}")
        if self.exclusive is not None:
            parts.append(f"exclusive:{_kind(self.exclusive)}")
        if self.check is not None:
            parts.append("check")
        for name, held in (
            ("start_failed", self.last_start_failed),
            ("container_lost", self.container_lost),
            ("initial_preservation", self.initial_preservation),
            ("final_archive", self.final_archive_pending),
            ("final_archive_blocked", self.final_archive_blocked),
            ("orphan", self.orphan is not None),
            ("orphan_marker_unreadable", self.orphan_marker_unreadable),
            ("start_completion", self.start_completion_pending),
        ):
            if held:
                parts.append(name)
        return tuple(parts)

    # Invariants ------------------------------------------------------------

    def check_invariants(self) -> list[str]:
        """The invariants the A2 spec proves that this state breaks, by name."""
        broken: list[str] = []
        owed = self.final_archive
        if self.archives_finals and owed is not None and owed.blocked and owed.members is None:
            # I1: with final archives, a blocked final archive is still owed.
            broken.append("I1")
        stop = self.stop
        under_check = (StopHolder.STOP, StopHolder.FINALIZE)
        if stop is not None and stop.kind in under_check and self.check is None:
            # A stop or a reconciliation ends its stop before its check.
            broken.append("stop_under_check")
        return broken

    def unproven(self) -> list[str]:
        """The invariants the A2 spec leaves open (section 4.4) that this state breaks."""
        broken: list[str] = []
        if self.start is not None and self.stop is not None:
            broken.append("U1")
        if self.initial_preservation and self.exclusive is None:
            broken.append("I5")
        start_seq, sealed_seq = self.run.start_seq, self.run.sealed_seq
        if start_seq is not None and sealed_seq is not None and start_seq <= sealed_seq:
            broken.append("U3")
        return broken

    def checked_combination(self) -> tuple[str, ...]:
        """:meth:`combination`, followed by the invariants this state breaks."""
        return (
            *self.combination(),
            *(f"violation:{name}" for name in self.check_invariants()),
            *(f"unproven:{name}" for name in self.unproven()),
        )

    # Preconditions ---------------------------------------------------------

    def _expect(self, holds: bool, violation: str) -> bool:
        """Whether a precondition holds; a violation is logged and the caller goes on."""
        if holds:
            return True
        if _Checks.strict:
            raise AssertionError(f"group lifecycle: {violation}")
        logger.warning(
            "group_lifecycle_violation",
            extra={
                "event": "group_lifecycle_violation",
                "violation": violation,
                "combination": render_combination(self.combination()),
            },
        )
        return False

    def _holding_any(self) -> bool:
        return self.start is not None or self.stop is not None or self.check is not None

    # The start -------------------------------------------------------------

    @_transition
    def begin_start_locked(self) -> Hold:
        """Mark a start in progress; the caller waited out ``START_WAIT`` and admitted ``START``."""
        self._expect(self.start is None, "a start began while another was in progress")
        hold = Hold(START)
        self.start = hold
        return hold

    @_transition
    def end_start_locked(self, hold: Hold, outcome: StartOutcome) -> None:
        """End the caller's start, recording whether it failed unless it was refused."""
        self._expect(self.start is hold, "a start ended that is not the one in progress")
        if outcome is not StartOutcome.REFUSED:
            self.last_start_failed = outcome is StartOutcome.FAILED
        self.start = None
        self.condition.notify_all()

    @_transition
    def begin_run_locked(self, start_seq: int | None) -> None:
        """Label the run a stopped group is about to start with its request's fence.

        The run that ended is sealed first: the checkpoint of its stop names
        its label, and may be what the group moves with. A request under a
        fence at or below it was counted in that label without its work being
        in the checkpoint, so it is refused; the control plane fences again.
        """
        self._expect(self.start is not None, "a run began outside a start")
        run = self.run
        sealed = run.sealed_seq
        if run.start_seq is not None and (sealed is None or run.start_seq > sealed):
            sealed = run.start_seq
            self.run = run = replace(run, sealed_seq=sealed)
        if start_seq is not None and sealed is not None and start_seq <= sealed:
            raise StaleStartFence("this request's start fence predates the group's last stop")
        self.run = replace(run, start_seq=start_seq, unfenced=start_seq is None)

    @_transition
    def join_run_locked(self, start_seq: int | None) -> None:
        """Count a request the caller admitted with ``JOIN_RUN`` in the run's label."""
        if start_seq is None:
            self.run = replace(self.run, unfenced=True)
        elif self.run.start_seq is None or start_seq > self.run.start_seq:
            self.run = replace(self.run, start_seq=start_seq)

    @_transition
    def owe_start_completion_locked(self) -> None:
        """Record that the proven start's marker completion is still owed."""
        self._expect(self.start is not None, "a start completion was owed outside a start")
        self.start_completion_pending = True

    @_transition
    def settle_start_completion_locked(self) -> None:
        """Record that the start's marker completion finished."""
        self.start_completion_pending = False

    # The stop --------------------------------------------------------------

    @_transition
    def begin_stop_locked(self, kind: StopHolder, *, check: _LifecycleCheck | None = None) -> Hold:
        """Mark the group stopping for ``kind``.

        A stop or a reconciliation stops under ``check``, the lifecycle check
        it holds. An archive request holds the stop with
        :meth:`hold_archive_locked` instead.
        """
        self._expect(self.stop is None, f"a {kind.value} stop began while the group was stopping")
        if kind in (StopHolder.STOP, StopHolder.FINALIZE):
            self._expect(
                check is not None and self.check is check,
                f"a {kind.value} stop began outside its lifecycle check",
            )
        hold = Hold(kind)
        self.stop = hold
        return hold

    @_transition
    def end_stop_locked(self, hold: Hold | None) -> None:
        """End the caller's stop; ``None`` is a caller that may not have taken one."""
        self._expect(self.stop is hold, "a stop ended that is not the one in progress")
        self.stop = None
        self.condition.notify_all()

    @_transition
    def hold_archive_locked(self, request_id: str) -> bool:
        """Hold the group's stop for ``request_id``; whether it was not held for it already."""
        held = self.stop
        if (
            held is not None
            and held.kind is StopHolder.ARCHIVE_HOLD
            and held.request_id == request_id
        ):
            return False
        self._expect(held is None, "an archive request held a group that was stopping")
        self.stop = Hold(StopHolder.ARCHIVE_HOLD, request_id)
        return True

    @_transition
    def release_archive_locked(self, request_id: str) -> None:
        """Release the stop ``request_id`` holds, and supersede what was observed under it."""
        self._expect(
            self.archive_request_id == request_id,
            "an archive request released a stop it does not hold",
        )
        self.stop = None
        self.generation += 1
        self.condition.notify_all()

    # The exclusive workspace -----------------------------------------------

    @_transition
    def begin_exclusive_locked(self, kind: ExclusiveHolder) -> Hold:
        """Take the exclusive workspace; the caller admitted ``MUTATION``."""
        self._expect(
            self.exclusive is None, f"{kind.value} took the exclusive workspace while it was held"
        )
        hold = Hold(kind)
        self.exclusive = hold
        return hold

    @_transition
    def end_exclusive_locked(self, hold: Hold) -> None:
        """Release the caller's exclusive workspace."""
        self._expect(
            self.exclusive is hold, "an exclusive workspace ended that is not the held one"
        )
        self.exclusive = None
        self.condition.notify_all()

    @_transition
    def expect_exclusive_locked(self, hold: Hold) -> None:
        """Check that the caller still holds the exclusive workspace, as it expects to.

        A caller that lost it keeps it again, as the barrier it rechecks
        under was always put back.
        """
        held = self._expect(
            self.exclusive is hold,
            "an exclusive workspace was rechecked by a caller not holding it",
        )
        if not held and self.exclusive is None:
            self.exclusive = hold

    @_transition
    def preserve_initial_locked(self) -> None:
        """Keep the initial archive's frozen writers until an accepted STOPPED inspection."""
        self.initial_preservation = True

    @_transition
    def release_initial_locked(self, hold: Hold) -> None:
        """Release the initial archive's preservation and its exclusive workspace.

        An accepted STOPPED inspection may already have released both.
        """
        self.initial_preservation = False
        self._expect(
            self.exclusive is hold or self.exclusive is None,
            "an initial archive released an exclusive workspace another caller holds",
        )
        self.exclusive = None
        self.condition.notify_all()

    @_transition
    def accepted_stopped_locked(self) -> None:
        """An accepted inspection says the container stopped: release a preserved initial archive.

        The exclusive workspace is released whoever holds it, as before this
        state existed (A2 spec Q2); a holder other than an initial archive is
        logged as ``group_lifecycle_foreign_exclusive_release``.
        """
        if not self.initial_preservation:
            return
        exclusive = self.exclusive
        if exclusive is not None and exclusive.kind is not ExclusiveHolder.INITIAL_ARCHIVE:
            logger.warning(
                "group_lifecycle_foreign_exclusive_release",
                extra={
                    "event": "group_lifecycle_foreign_exclusive_release",
                    "holder": _kind(exclusive),
                    "combination": render_combination(self.combination()),
                },
            )
        self.initial_preservation = False
        self.exclusive = None
        self.condition.notify_all()

    # The lifecycle check ---------------------------------------------------

    @_transition
    def advance_generation_locked(self) -> int:
        """Supersede every observation made without the lock; the new generation."""
        self.generation += 1
        return self.generation

    @_transition
    def begin_check_locked(self, check: _LifecycleCheck) -> None:
        """Reserve the group for ``check``; the caller admitted ``LIFECYCLE_CHECK``."""
        self._expect(self.check is None, "a lifecycle check began while another held the group")
        self.check = check

    @_transition
    def end_check_locked(self, check: _LifecycleCheck) -> bool:
        """Release ``check`` if it still holds the group; whether it did."""
        if self.check is not check:
            return False
        stop = self.stop
        self._expect(
            stop is None or stop.kind not in (StopHolder.STOP, StopHolder.FINALIZE),
            "a lifecycle check ended while the stop it holds was in progress",
        )
        self.check = None
        self.generation += 1
        self.condition.notify_all()
        return True

    @_transition
    def mark_lost_locked(self) -> None:
        """Record that the group's container identity was lost, for good."""
        if not self.container_lost:
            self.generation += 1
        self.container_lost = True

    # Barriers --------------------------------------------------------------

    @_transition
    def owe_final_locked(
        self, members: tuple[SharedMemberManifest, ...] | None, *, blocked: bool = False
    ) -> None:
        """Record the final archive a stop owes for ``members``, blocked or as it was.

        ``members`` is ``None`` when no upload is owed (no final archive is
        configured): only a block is then recorded.
        """
        self._expect(self._holding_any(), "a final archive was owed outside a lifecycle operation")
        self._expect(
            members is not None or not self.archives_finals,
            "a final archive was owed without its members while final archives are configured",
        )
        was_blocked = self.final_archive is not None and self.final_archive.blocked
        blocked = blocked or was_blocked
        self.final_archive = (
            None if members is None and not blocked else FinalArchive(members, blocked)
        )

    @_transition
    def refresh_final_members_locked(self, members: tuple[SharedMemberManifest, ...]) -> None:
        """Replace the membership an owed final archive is for."""
        owed = self.final_archive
        if self._expect(
            owed is not None and owed.members is not None,
            "final archive members were refreshed with no final archive owed",
        ):
            assert owed is not None
            self.final_archive = replace(owed, members=members)

    @_transition
    def owe_orphan_locked(self, *, marker_unreadable: bool | None = None) -> None:
        """Record that an orphan's final archive is to be finished.

        ``marker_unreadable`` ``None`` keeps what was recorded of its marker.
        """
        self._expect(
            self.check is not None or self.stop is not None,
            "an orphan finalization was owed outside a check or a stop",
        )
        if marker_unreadable is None:
            marker_unreadable = self.orphan_marker_unreadable
        self.orphan = OrphanFinalization(marker_unreadable)

    @_transition
    def marker_read_locked(self) -> None:
        """Record that the orphan's marker was read; the finalization stays owed."""
        if self.orphan is not None:
            self.orphan = OrphanFinalization(marker_unreadable=False)

    @_transition
    def settle_orphan_locked(self) -> None:
        """Record that the orphan owes no finalization."""
        self.orphan = None

    @_transition
    def settle_archive_locked(self) -> None:
        """Clear every archive barrier: the stop's lifecycle is complete."""
        self._expect(self._holding_any(), "archive barriers settled outside a lifecycle operation")
        self.final_archive = None
        self.orphan = None
        self.start_completion_pending = False


def _kind(hold: Hold) -> str:
    return hold.kind if isinstance(hold.kind, str) else hold.kind.value


#: How often the per-process combination counts are logged.
COMBINATION_COUNT_INTERVAL_S: Final = 3600.0


class CombinationCounts:
    """Which lifecycle combinations this process has seen, and how often.

    The first time a combination appears it is logged as
    ``group_lifecycle_combination``; the counts of every combination observed
    since the last report are logged as ``group_lifecycle_combination_counts``
    once :data:`COMBINATION_COUNT_INTERVAL_S` has passed. They corroborate the
    reachable combinations in the A2 spec, and decide nothing.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = COMBINATION_COUNT_INTERVAL_S,
    ) -> None:
        self._clock = clock
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._seen: set[tuple[str, ...]] = set()
        self._counts: Counter[tuple[str, ...]] = Counter()
        self._since = clock()

    def observe(self, combination: tuple[str, ...], *, site: str) -> None:
        """Count one observation of ``combination`` at ``site``, logging what is due."""
        with self._lock:
            first = combination not in self._seen
            self._seen.add(combination)
            self._counts[combination] += 1
            now = self._clock()
            due = now - self._since >= self._interval_s
            if due:
                counts = {render_combination(key): count for key, count in self._counts.items()}
                self._counts.clear()
                self._since = now
        if first:
            logger.info(
                "group_lifecycle_combination",
                extra={
                    "event": "group_lifecycle_combination",
                    "combination": render_combination(combination),
                    "site": site,
                },
            )
        if due:
            logger.info(
                "group_lifecycle_combination_counts",
                extra={
                    "event": "group_lifecycle_combination_counts",
                    "counts": counts,
                    "interval_s": self._interval_s,
                },
            )


def render_combination(combination: tuple[str, ...]) -> str:
    """A combination as one log value: its parts joined by ``+``, or ``idle``."""
    return "+".join(combination) or "idle"


#: This process's combination counts.
COMBINATIONS: Final = CombinationCounts()


__all__ = [
    "COMBINATIONS",
    "FLAG_NAMES",
    "START",
    "CombinationCounts",
    "ExclusiveHolder",
    "FinalArchive",
    "GroupLifecycleState",
    "Hold",
    "OrphanFinalization",
    "RunLabel",
    "StartOutcome",
    "StopHolder",
    "render_combination",
]
