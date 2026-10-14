"""Observers that collect local sub-stages and report their attribution."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .clock import clock
from .measurement import _clamp_to_deadline, _recording
from .models import _NULL_SPAN, Measurement
from .schema import DEFAULT_PRODUCER, DEFAULT_ROOT_STAGE

if TYPE_CHECKING:
    from collections.abc import Iterator


class StageObserver(Protocol):
    """Times named sub-stages for a caller that cannot report its own timings.

    The sandbox manager uses this to expose the work inside one of its calls --
    the container create or start, the guest restart of the agent-network
    transition -- without knowing whether the caller is a runner recording into
    a measurement or a supervisor collecting durations to return over HTTP.
    """

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time one sub-stage."""
        ...


@dataclass
class StageRecorder:
    """Collects sub-stage durations, for reporting back over a process boundary.

    Only durations cross the boundary. A sub-stage measured in the sandbox manager
    process cannot be placed on the runner's monotonic timeline, so it is
    reported as a duration attributed to its producer instead of as an offset
    that would look precise and mean nothing.

    The recorder inherits the selected measurement's deadline, so a run that
    lapses mid-call stops collecting here too rather than measuring past the
    experiment.
    """

    deadline_mono: float = float("inf")
    #: Producer that owns the sub-stages this recorder collects. Defaults to
    #: the runner so a caller written before this producer existed, and a
    #: request that names no producer, cannot silently claim another's work.
    producer: str = DEFAULT_PRODUCER
    #: The operation this call belongs to, when the context named one the run
    #: selects. A producer that learned the result narrows it with
    #: :meth:`set_operation`; the sub-stages already recorded keep their name
    #: and are relabelled with the answer, because the whole call was one
    #: operation and only its kind was in question.
    operation: str = ""
    #: Operations this call may be filed under. More than one means the
    #: producer is expected to narrow it once it knows the result.
    allowed: frozenset[str] = frozenset()
    #: Fallback root stage for a caller that records sub-stages without marking
    #: one first. An operation that never marks is not necessarily a startup.
    root_stage: str = "startup"
    stages: list[dict[str, Any]] = field(default_factory=list)
    #: Sub-stages that were clamped at the deadline.
    truncated: int = 0
    #: Sub-stages that never started because the deadline had already passed.
    #: Counted separately because a skipped stage leaves no trace at all, and
    #: an empty `stages` would otherwise be indistinguishable from a call that
    #: legitimately had no timed work.
    skipped: int = 0

    def expired(self) -> bool:
        """Return whether the inherited experiment has lapsed."""
        return clock().monotonic() > self.deadline_mono

    def stopped_early(self) -> bool:
        """Return whether the deadline cut this collection short."""
        return self.skipped > 0 or self.truncated > 0

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time one sub-stage onto this recorder."""
        if self.expired():
            self.skipped += 1
            return _NULL_SPAN
        return _recorded_stage(self, name)

    def set_operation(self, operation: str) -> bool:
        """File this call, and everything already timed for it, under ``operation``.

        A file path is collected before its kind is known, so the stages are
        recorded first and named afterwards. They belong to that one call
        either way; only its operation was in question.

        Returns whether the operation was accepted. A producer may only report
        one this run selects here: anything else would file a sample under a
        name nobody asked for, and a caller that is refused must report nothing
        rather than leave a stale name standing.
        """
        named = str(operation)
        if self.allowed and named not in self.allowed:
            return False
        self.operation = named
        for item in self.stages:
            item["operation"] = self.operation
        return True

    def names(self) -> list[str]:
        """Return the stage names recorded so far, in order."""
        return [str(item["name"]) for item in self.stages]


@contextmanager
def _recorded_stage(recorder: StageRecorder, name: str) -> Iterator[None]:
    """Record one sub-stage duration, closing it on success and failure alike."""
    started = clock().monotonic_ns()
    outcome = "ok"
    truncate = False
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        end = clock().monotonic_ns()
        if recorder.expired():
            end = min(end, int(recorder.deadline_mono * 1_000_000_000))
            truncate = True
            recorder.truncated += 1
        entry: dict[str, Any] = {
            "name": name,
            "duration_us": max(0, (end - started) // 1000),
            "outcome": outcome,
        }
        if truncate:
            entry["truncated"] = True
        recorder.stages.append(entry)


@dataclass
class _MeasurementObserver:
    """Records sub-stages as children of the measurement's current stage.

    ``producer`` is this observer's attribution, not the measurement's: a
    caller may time work it did itself under its own name while the record
    still belongs to whoever rooted the operation.
    """

    measurement: Measurement
    producer: str | None = None

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time one sub-stage onto the measurement's current stage."""
        if self.producer is None or self.producer == self.measurement.producer:
            attribution = None
        else:
            attribution = self.producer
        return _observed_stage(self.measurement, name, producer=attribution)


@contextmanager
def _observed_stage(
    measurement: Measurement, name: str, *, producer: str | None = None
) -> Iterator[None]:
    """Attach one in-process sub-stage to the stage currently open.

    ``producer`` overrides the attribution for a caller sharing another
    producer's measurement. Omitted, the measurement's own producer is used --
    which is also what keeps a ``StageObserver`` protocol implementation that
    predates this parameter working unchanged.
    """
    if not _recording(measurement):
        yield
        return
    started = measurement.offset_us()
    # The stage the caller last marked is the one this work belongs to. With no
    # mark it belongs to the same implicit whole-operation stage that
    # ``_stages`` emits; attaching it to the producer's name instead would file
    # it under a stage that never exists and drop the timing without a trace.
    parent = (
        measurement.marks[-1][0]
        if measurement.marks
        else measurement.operation or DEFAULT_ROOT_STAGE
    )
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        end = measurement.offset_us()
        end, truncate = _clamp_to_deadline(measurement, end)
        entry: dict[str, Any] = {
            "stage": name,
            "duration_us": max(0, end - started),
            "outcome": outcome,
            "source": producer or measurement.producer or DEFAULT_PRODUCER,
        }
        if truncate:
            entry["truncated"] = True
        # This is where a producer's timing actually enters the record, so this
        # is where it is counted as a source of it.
        measurement.contributors.add(str(entry["source"]))
        measurement.remote_stages.setdefault(parent, []).append(entry)


def stage_observer(
    measurement: Measurement | None, *, producer: str | None = None
) -> StageObserver | None:
    """Return an observer that records sub-stages onto a live measurement.

    ``producer`` attributes the observed work to a caller sharing someone
    else's measurement, such as a sandbox manager timing its own work under a selection
    the control plane rooted. It changes nothing about who owns the record:
    ``measurement.producer`` stays the producer that rooted the operation.
    """
    if measurement is None or measurement.finished:
        return None
    return _MeasurementObserver(measurement, producer=producer)


def substage(observer: StageObserver | None, name: str) -> AbstractContextManager[None]:
    """Return an observer's sub-stage context, or a shared no-op."""
    if observer is None:
        return _NULL_SPAN
    return observer.stage(name)


@contextmanager
def attributed_stage(
    measurement: Measurement | None,
    parent: str,
    name: str,
    *,
    producer: str | None = None,
) -> Iterator[None]:
    """Time one in-process sub-stage under a named parent.

    This is how a root producer reports its own internal work in the same shape
    the sandbox manager reports its durations: ``{"name", "duration_us", "outcome",
    "source"}`` hung under a stage of the local timeline. Unlike
    :func:`_observed_stage` the parent is named explicitly rather than taken
    from the last mark, so a producer can time the work it does before the first
    mark. An unselected measurement costs one generator and records nothing.
    """
    if measurement is None or not _recording(measurement):
        yield
        return
    started = measurement.offset_us()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        end = measurement.offset_us()
        end, truncate = _clamp_to_deadline(measurement, end)
        entry: dict[str, Any] = {
            "stage": name,
            "duration_us": max(0, end - started),
            "outcome": outcome,
            "source": producer or measurement.producer or DEFAULT_PRODUCER,
        }
        if truncate:
            entry["truncated"] = True
        measurement.contributors.add(str(entry["source"]))
        measurement.remote_stages.setdefault(parent, []).append(entry)
