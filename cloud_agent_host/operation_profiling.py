"""Default-off timing for platform operations.

Design and field semantics live in
``docs/specs/2026-09-14-sandbox-startup-measurement-design.zh-CN.md`` and
``docs/specs/2026-09-14-platform-operation-measurement-inventory.zh-CN.md``.
This module implements the first slice: the opt-in switch, the run descriptor,
a pure-timing collector, and one structured record per selected root operation.

The default is **off**, and off has to stay cheap: no collector, no measurement
id, no clock reads, no span objects, no serialization, no background work. The
entry points therefore take a ``Measurement | None`` and answer ``None``/``False``
without allocating. Environment variables are parsed once per process, never per
span, and the export thread exists only once a run has produced a record.

A run is only live when all of the following hold; anything else leaves the
business path untouched and logs one warning:

* ``AGENT_OPERATION_PROFILING_ENABLED`` is truthy,
* ``AGENT_OPERATION_PROFILING_RUN`` is a JSON descriptor with a ``run_id``, a
  non-empty ``operations`` list, a future ``expires_at``, a positive
  ``max_operations``, and a ``target`` that names only known fields.

Three costs are bounded on purpose, because this measures a path users wait on:

* **Nothing is published on the attempt thread.** Both the log line and the
  ``AGENT_OPERATION_PROFILING_SINK`` write go through one bounded queue drained
  by a background thread, so neither a slow filesystem nor a blocking log
  handler can be mistaken for slow startup. A queue that fills drops records
  and counts the drops.
* **The operation budget is refunded, not spent, when nothing was selected.**
  A claim that returned no job, or an attempt outside the run's target, gives
  its reserved slot back, so a small experiment is not consumed by polling.
* **A run that expires stops collecting.** No mark, span or attribute is
  recorded past the deadline, and a measurement that finishes after it is
  emitted as partial rather than as a complete sample.

Known limits of this slice, all deliberate:

* Selection happens at the runner's root operation, not at the control plane.
  ``max_operations`` is therefore a per-process budget, not a fleet-wide one.
* ``target`` accepts ``host`` (known before claim) plus ``job_id``,
  ``thread_id`` and ``sandbox_id`` (known only once an attempt is claimed), so
  a target filter is applied in two steps: the cheap pre-filter at
  :func:`start_operation`, then the authoritative :func:`bind`, which requires
  every configured field to be present and equal. A scoped measurement that was
  never bound is dropped rather than reported.
* Records go to a structured log line and, when the sink is configured, to an
  appended JSONL file. Durable per-attempt storage and the admin reading
  surface are later slices.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Deployment-level master switch. Absent or false means "do not profile".
ENV_PROFILING_ENABLED = "AGENT_OPERATION_PROFILING_ENABLED"

#: JSON descriptor for the current profiling run. See :class:`ProfilingRun`.
ENV_PROFILING_RUN = "AGENT_OPERATION_PROFILING_RUN"

#: Optional JSONL path. Each finished measurement is appended as one line.
ENV_PROFILING_SINK = "AGENT_OPERATION_PROFILING_SINK"

#: Bumped when the emitted record shape changes.
SCHEMA_VERSION = 1

#: Per-measurement span ceiling; the design budgets 128 spans per attempt.
MAX_SPANS = 128

#: Attribute strings are truncated here. The design forbids prompts, tokens,
#: environment variables, command lines and raw error text in a record.
MAX_ATTRIBUTE_CHARS = 200

#: Longest run id a descriptor may declare. The run id is a top-level record
#: field rather than a bounded attribute, so it is capped where the descriptor
#: is read -- long before any request carries it across a process boundary.
#: Violating it disables profiling instead of failing a startup.
MAX_RUN_ID_CHARS = 128

#: How many finished records may wait for the export thread before the sink
#: starts dropping. Reaching this means the sink cannot keep up with the
#: experiment, which is reported with the drops rather than hidden.
SINK_QUEUE_MAX = 256

#: Fields an experiment may scope a run by. A descriptor naming anything else
#: is rejected outright: an unknown filter would silently never match, and a
#: run that measures nothing is worse than a run that refuses to start.
TARGET_KEYS = frozenset({"host", "job_id", "thread_id", "sandbox_id"})

#: Sub-stages another producer may contribute to one local stage. Bounded
#: because the list crosses a process boundary and is not trusted.
MAX_REMOTE_STAGES = 32

_TRUTHY = {"1", "true", "yes", "on"}

_OUTCOMES = {"dispatched", "failed", "cancelled", "lease_lost", "incomplete"}


def _instance_id() -> str:
    """Return a stable identifier for this producer process."""
    try:
        host = socket.gethostname()
    except OSError:  # pragma: no cover - hostname lookup is effectively local
        host = "unknown-host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


#: Every producer has its own monotonic clock domain. Timestamps from two
#: domains are never subtracted from each other; they are correlated by
#: measurement id instead.
CLOCK_DOMAIN_ID = f"host-{uuid.uuid4().hex[:12]}"

#: Identifies one runner process across restarts.
PRODUCER_INSTANCE_ID = _instance_id()


@dataclass(frozen=True)
class ProfilingRun:
    """A bounded, explicitly scoped profiling experiment.

    ``deadline_mono`` is derived from ``expires_at`` when the descriptor is
    first read, so a process restart or a wall-clock jump cannot extend a run.
    """

    run_id: str
    operations: frozenset[str]
    expires_at: str
    deadline_mono: float
    max_operations: int
    target: Mapping[str, str] = field(default_factory=dict)


@dataclass
class _SpanRecord:
    """One timed interval inside a measurement."""

    span_id: str
    name: str
    ordinal: int
    start_offset_us: int
    parent_span_id: str | None = None
    duration_us: int | None = None
    outcome: str = "ok"
    #: The run's deadline landed inside this interval, so its duration is a
    #: floor rather than the time the work really took.
    truncated: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Return the wire form of this span."""
        payload: dict[str, Any] = {
            "span_id": self.span_id,
            "name": self.name,
            "ordinal": self.ordinal,
            "start_offset_us": self.start_offset_us,
            "outcome": self.outcome,
        }
        if self.parent_span_id is not None:
            payload["parent_span_id"] = self.parent_span_id
        if self.duration_us is not None:
            payload["duration_us"] = self.duration_us
        if self.truncated:
            payload["truncated"] = True
        if self.attributes:
            payload["attributes"] = self.attributes
        return payload


@dataclass
class Measurement:
    """Mutable timing state for one selected root operation."""

    operation: str
    run_id: str
    measurement_id: str
    started_mono_ns: int
    #: The run's monotonic deadline, carried per measurement so expiry is
    #: enforced while collecting and not only when a new measurement starts.
    deadline_mono: float
    facts: dict[str, Any] = field(default_factory=dict)
    spans: list[_SpanRecord] = field(default_factory=list)
    marks: list[tuple[str, int]] = field(default_factory=list)
    ordinals: dict[str, int] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    #: Span ids come from a counter, not from the number of closed spans: a
    #: nested span closes first, so counting appends handed two live spans the
    #: same id and made a child its own parent.
    span_counter: int = 0
    dropped_spans: int = 0
    #: Offset of the closing boundary. Without it the stage that was running
    #: when the measurement ended had no duration at all -- which is precisely
    #: the stage a slow startup is sitting in.
    ended_offset_us: int | None = None
    #: Whether the authoritative target check ran. A scoped run refuses to
    #: report a measurement that never proved it matched.
    bound: bool = False
    #: Sub-stage durations reported by another producer, keyed by the local
    #: stage they belong to. They carry no offset because a different process
    #: has a different monotonic clock domain; only durations travel.
    remote_stages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Producers that contributed to this record, beyond the runner itself.
    producers: set[str] = field(default_factory=set)
    #: (producer, call id, local stage) fragments this path asked for. Keyed by
    #: call rather than by stage so a retry is a second fragment that must be
    #: accounted for, and so an unanswered call cannot be hidden by a later one
    #: to the same stage.
    expected_fragments: set[tuple[str, str, str]] = field(default_factory=set)
    #: (producer, call id) answers this path received. An answer says the
    #: producer finished collecting for that call -- which is not the same as
    #: having timed something: a legitimately skipped stage answers with no
    #: stages at all, and that is a complete fragment, not a missing one.
    answered_fragments: set[tuple[str, str]] = field(default_factory=set)
    #: Mints the call ids that make retries distinct fragments.
    call_counter: int = 0
    #: Set when any interval in this record ended at a deadline rather than
    #: when its work finished -- locally or in another process.
    truncated: bool = False
    #: (producer, call id) fragments that answered but were cut short by their
    #: own deadline, so the record can name what it is missing rather than
    #: only being short.
    truncated_fragments: set[tuple[str, str]] = field(default_factory=set)
    #: Set once the run's deadline passed with collecting still open.
    expired: bool = False
    #: The budget slot is held from selection and returned by `discard`.
    reserved: bool = True
    selected: bool = True
    finished: bool = False

    def offset_us(self, mono_ns: int | None = None) -> int:
        """Return microseconds since this measurement started."""
        now = time.monotonic_ns() if mono_ns is None else mono_ns
        return (now - self.started_mono_ns) // 1000

    def deadline_offset_us(self) -> int:
        """Return the offset at which this measurement's run expires.

        A measurement that outlives its run is closed here rather than at the
        moment someone noticed, so a partial record never reports time the
        experiment was not collecting for.
        """
        return int(self.deadline_mono * 1_000_000) - self.started_mono_ns // 1000

    def past_deadline(self) -> bool:
        """Return whether the run expired while this measurement was open."""
        return time.monotonic() > self.deadline_mono


class _NullSpan:
    """Shared no-op context manager, so the off path allocates nothing."""

    __slots__ = ()

    def __enter__(self) -> None:
        """Enter a span that records nothing."""
        return None

    def __exit__(self, *exc_info: object) -> bool:
        """Leave without suppressing the exception."""
        return False


_NULL_SPAN = _NullSpan()

_EMPTY_SPAN: AbstractContextManager[None] = nullcontext()


@dataclass(frozen=True)
class _Loaded:
    """Cached result of reading the switch and the run descriptor once."""

    run: ProfilingRun | None
    reason: str


_CACHE: _Loaded | None = None
_CACHE_LOCK = threading.Lock()
_BUDGET_LOCK = threading.Lock()
_USED_OPERATIONS = 0


def _truthy(raw: str | None) -> bool:
    """Return whether an environment value means "on"."""
    return (raw or "").strip().lower() in _TRUTHY


def _parse_expiry(raw: str) -> float:
    """Return the monotonic deadline for an ISO-8601 ``expires_at``.

    A trailing ``Z`` is accepted because the design's run descriptors are
    written in UTC.

    Raises:
        ValueError: the value is missing, unparseable, or not in the future.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("expires_at is required")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"expires_at is not an ISO-8601 timestamp: {text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    remaining = (moment - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise ValueError(f"expires_at is not in the future: {text!r}")
    return time.monotonic() + remaining


def parse_run(raw: str) -> ProfilingRun:
    """Build a run descriptor from its JSON form.

    Raises:
        ValueError: the descriptor is not an object, is missing a run id, a
            non-empty operation list, a future expiry or a positive cap, or
            scopes itself by a field that does not exist.
    """
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run descriptor is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("run descriptor must be a JSON object")

    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run descriptor has no run_id")
    if len(run_id) > MAX_RUN_ID_CHARS:
        raise ValueError(f"run_id is longer than {MAX_RUN_ID_CHARS} characters")

    raw_operations = body.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError("run descriptor has no operations list")
    operations = frozenset(str(item).strip() for item in raw_operations if str(item).strip())
    if not operations:
        raise ValueError("run descriptor selects no operations")

    try:
        max_operations = int(body["max_operations"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run descriptor has no valid max_operations") from exc
    if max_operations <= 0:
        raise ValueError("max_operations must be positive")

    raw_target = body.get("target") or {}
    if not isinstance(raw_target, dict):
        raise ValueError("target must be a JSON object")
    unknown = sorted(str(key) for key in raw_target if str(key) not in TARGET_KEYS)
    if unknown:
        raise ValueError(f"target names unknown field(s): {', '.join(unknown)}")
    target = {
        str(key): str(value)
        for key, value in raw_target.items()
        if isinstance(value, (str, int)) and str(value).strip()
    }

    return ProfilingRun(
        run_id=run_id,
        operations=operations,
        expires_at=str(body.get("expires_at") or "").strip(),
        deadline_mono=_parse_expiry(str(body.get("expires_at") or "")),
        max_operations=max_operations,
        target=target,
    )


def _load() -> _Loaded:
    """Return the cached switch state, reading the environment at most once."""
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE
        if not _truthy(os.environ.get(ENV_PROFILING_ENABLED)):
            _CACHE = _Loaded(None, "not_enabled")
            return _CACHE
        raw = os.environ.get(ENV_PROFILING_RUN) or ""
        try:
            run = parse_run(raw)
        except ValueError as exc:
            # A switch without a usable run is not an experiment. Keep the
            # business path identical and say why once per process.
            logger.warning(
                "operation profiling is enabled but its run descriptor is unusable "
                "(%s); continuing without profiling",
                exc,
            )
            _CACHE = _Loaded(None, "not_enabled")
            return _CACHE
        _CACHE = _Loaded(run, "active")
        return _CACHE


def active_run() -> ProfilingRun | None:
    """Return the live run descriptor, or ``None`` when profiling is off."""
    return _load().run


def reason() -> str:
    """Return ``not_enabled`` or ``active`` for startup diagnostics."""
    return _load().reason


def _claim_budget(run: ProfilingRun) -> bool:
    """Reserve one root-operation slot, or return ``False`` when used up."""
    global _USED_OPERATIONS
    with _BUDGET_LOCK:
        if run.max_operations <= _USED_OPERATIONS:
            return False
        _USED_OPERATIONS += 1
        return True


def _release_budget() -> None:
    """Return a reserved slot for a measurement that produced no record."""
    global _USED_OPERATIONS
    with _BUDGET_LOCK:
        if _USED_OPERATIONS > 0:
            _USED_OPERATIONS -= 1


def _recording(measurement: Measurement) -> bool:
    """Return whether a measurement may still collect.

    Expiry is enforced here rather than only when a measurement starts, so a run
    that lapses mid-attempt stops adding to the record instead of reporting the
    whole attempt as a complete sample.
    """
    if measurement.finished:
        return False
    if measurement.past_deadline():
        measurement.expired = True
        return False
    return True


def start_operation(
    *,
    operation: str,
    host: str | None = None,
    worker_id: str | None = None,
) -> Measurement | None:
    """Begin timing a root operation, or return ``None`` when not selected.

    Only ``host`` and ``worker_id`` are known this early; the remaining target
    facts arrive with :func:`bind` once an attempt has been claimed, and a
    scoped run is not reported until that check has passed.

    The reserved budget slot is committed by :func:`finish` and returned by
    :func:`discard`, so a claim that yielded nothing does not consume the run.
    """
    loaded = _load()
    run = loaded.run
    if run is None or operation not in run.operations:
        return None
    if time.monotonic() > run.deadline_mono:
        return None
    target_host = run.target.get("host")
    if target_host is not None and target_host != host:
        return None
    if not _claim_budget(run):
        return None

    measurement = Measurement(
        operation=operation,
        run_id=run.run_id,
        measurement_id=f"msm_{uuid.uuid4().hex[:16]}",
        started_mono_ns=time.monotonic_ns(),
        deadline_mono=run.deadline_mono,
        facts={"operation": operation},
    )
    if host:
        measurement.facts["host"] = host
    if worker_id:
        measurement.facts["worker_id"] = worker_id
    return measurement


def bind(measurement: Measurement | None, **facts: Any) -> bool:
    """Record post-claim facts and report whether the measurement is in scope.

    This is the authoritative target check: every field the run scopes itself
    by must be present on the measurement and equal to the configured value. A
    missing field is a mismatch, not a pass -- ``target: {sandbox_id: wanted}``
    must not select an attempt whose sandbox id is unknown.

    Returns ``False`` when the measurement is ``None``, when collecting has
    stopped, or when the scope does not match. Callers should :func:`discard`
    it in that case, so an unselected attempt emits nothing.
    """
    if measurement is None or not _recording(measurement):
        return False
    run = _load().run
    for key, value in facts.items():
        measurement.facts[key] = _bounded(value)
    measurement.bound = True
    if run is not None:
        for key, wanted in run.target.items():
            observed = measurement.facts.get(key)
            if observed is None or str(observed) != wanted:
                measurement.selected = False
                return False
    return measurement.selected


def record_fragment(
    measurement: Measurement | None,
    *,
    producer: str,
    call_id: str | None,
    stage: str,
    response: Any,
) -> bool:
    """Record one call's answer and mark that fragment answered.

    An answer is what the producer reports about *collecting*, not about how
    much it collected. A reuse that legitimately does no timed work answers
    with no stages and that fragment is complete; a producer that was not
    collecting answers with nothing and that fragment is missing. Conflating
    the two made every normal reuse look incomplete.

    Returns whether the fragment was answered.
    """
    if measurement is None or not _recording(measurement) or not call_id:
        return False
    if not isinstance(response, Mapping):
        return False
    echoed = str(response.get("profiling_call_id") or "")
    if echoed != call_id:
        # A response that cannot be matched to the call is not an answer.
        return False
    if not response.get("profiling_recorded"):
        return False
    measurement.answered_fragments.add((producer, call_id))
    if response.get("profiling_truncated"):
        # The call answered, but collection stopped at this producer's own
        # deadline: the fragment is present and still a floor, not a duration.
        # Everything it did manage to measure is kept.
        measurement.truncated = True
        measurement.truncated_fragments.add((producer, call_id))
    attach_remote_stages(
        measurement,
        stage,
        response.get("stages"),
        source=producer,
        call_id=call_id,
    )
    return True


def _bounded(value: Any) -> Any:
    """Return an attribute value the record may store."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_ATTRIBUTE_CHARS]


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


def _clamp_to_deadline(measurement: Measurement, end_us: int) -> tuple[int, bool]:
    """Return an interval end that never runs past the run's deadline."""
    if not measurement.past_deadline():
        return end_us, False
    measurement.expired = True
    measurement.truncated = True
    return min(end_us, max(0, measurement.deadline_offset_us())), True


@dataclass
class StageRecorder:
    """Collects sub-stage durations, for reporting back over a process boundary.

    Only durations cross the boundary. A sub-stage measured in the broker
    process cannot be placed on the runner's monotonic timeline, so it is
    reported as a duration attributed to its producer instead of as an offset
    that would look precise and mean nothing.

    The recorder inherits the selected measurement's deadline, so a run that
    lapses mid-call stops collecting here too rather than measuring past the
    experiment.
    """

    deadline_mono: float = float("inf")
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
        return time.monotonic() > self.deadline_mono

    def stopped_early(self) -> bool:
        """Return whether the deadline cut this collection short."""
        return self.skipped > 0 or self.truncated > 0

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time one sub-stage onto this recorder."""
        if self.expired():
            self.skipped += 1
            return _NULL_SPAN
        return _recorded_stage(self, name)

    def names(self) -> list[str]:
        """Return the stage names recorded so far, in order."""
        return [str(item["name"]) for item in self.stages]


@contextmanager
def _recorded_stage(recorder: StageRecorder, name: str) -> Iterator[None]:
    """Record one sub-stage duration, closing it on success and failure alike."""
    started = time.monotonic_ns()
    outcome = "ok"
    truncate = False
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        end = time.monotonic_ns()
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
    """Records sub-stages as children of the measurement's current stage."""

    measurement: Measurement

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time one sub-stage onto the measurement's current stage."""
        return _observed_stage(self.measurement, name)


@contextmanager
def _observed_stage(measurement: Measurement, name: str) -> Iterator[None]:
    """Attach one in-process sub-stage to the stage currently open."""
    if not _recording(measurement):
        yield
        return
    started = measurement.offset_us()
    # The stage the runner last marked is the one this work belongs to.
    parent = measurement.marks[-1][0] if measurement.marks else "startup"
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
            "source": "runner",
        }
        if truncate:
            entry["truncated"] = True
        measurement.remote_stages.setdefault(parent, []).append(entry)


def stage_observer(measurement: Measurement | None) -> StageObserver | None:
    """Return an observer that records sub-stages onto a live measurement."""
    if measurement is None or measurement.finished:
        return None
    return _MeasurementObserver(measurement)


def measurement_context(
    measurement: Measurement | None, call_id: str | None = None
) -> dict[str, Any] | None:
    """Return the selection a downstream producer needs to inherit.

    The runner decides whether an operation is selected; a broker cannot make
    that decision for itself, because it sees one call at a time and does not
    know which attempt it belongs to. So the selected measurement travels with
    the request: run id, operation, and an absolute wall-clock expiry, since
    two processes share no monotonic clock.
    """
    if measurement is None or measurement.finished or measurement.past_deadline():
        return None
    return {
        "run_id": measurement.run_id,
        "measurement_id": measurement.measurement_id,
        "operation": measurement.operation,
        "call_id": call_id or "",
        "expires_at": time.time() + max(0.0, measurement.deadline_mono - time.monotonic()),
    }


def start_stage_recording(context: Any) -> StageRecorder | None:
    """Return a recorder when this process should record for that selection.

    Recording requires an inherited selection, never just a local switch. A
    broker sees one call at a time and cannot tell which attempt it belongs to,
    so on its own it cannot know whether the operation was selected -- and a
    broker that kept measuring after the runner stopped would report time the
    experiment was not collecting for. ``None`` therefore means "not selected"
    and records nothing.

    Beyond the local switch, the inherited selection must agree with this
    process on the run id and on the operation being one this run selects, and
    its deadline must not already have passed.

    The inherited deadline can only ever shorten the local one -- the recorder
    takes the earlier of the two -- so a caller cannot extend an experiment
    past what this process was configured to run.
    """
    run = _load().run
    if run is None or not isinstance(context, Mapping):
        return None
    if str(context.get("run_id") or "") != run.run_id:
        return None
    operation = str(context.get("operation") or "")
    if operation and operation not in run.operations:
        return None
    inherited = _context_deadline(context)
    if inherited is None:
        return None
    deadline = min(run.deadline_mono, inherited)
    if time.monotonic() > deadline:
        return None
    return StageRecorder(deadline_mono=deadline)


def _context_deadline(context: Mapping[str, Any]) -> float | None:
    """Convert a caller's wall-clock expiry into this process's monotonic one."""
    try:
        expires_at = float(context.get("expires_at"))
    except (TypeError, ValueError):
        return None
    remaining = expires_at - time.time()
    if remaining <= 0:
        return None
    return time.monotonic() + remaining


def expect_fragment(measurement: Measurement | None, producer: str, stage: str) -> str | None:
    """Register one call's fragment and return the id that identifies it.

    Register before making the call, so a call that never returned still counts
    as a fragment this record owed and did not get. The returned id travels
    with the request and comes back with the answer, which is what keeps two
    attempts at the same stage -- a failed resident call and its retry --
    separately accountable.
    """
    if measurement is None or measurement.finished:
        return None
    measurement.call_counter += 1
    call_id = f"c{measurement.call_counter}"
    measurement.expected_fragments.add((producer, call_id, stage))
    return call_id


def context_call_id(context: Any) -> str | None:
    """Return the call id a request carried, if it carried one."""
    if not isinstance(context, Mapping):
        return None
    value = str(context.get("call_id") or "").strip()
    return value or None


def substage(observer: StageObserver | None, name: str) -> AbstractContextManager[None]:
    """Return an observer's sub-stage context, or a shared no-op."""
    if observer is None:
        return _NULL_SPAN
    return observer.stage(name)


def _bounded_remote_stages(
    stages: Any, source: str, call_id: str | None = None
) -> list[dict[str, Any]]:
    """Return the reportable sub-stages from an untrusted list.

    The input is sliced to bound the work and the result to bound the record: a
    list padded with malformed entries must not exceed the ceiling by having
    them rejected one at a time.
    """
    if not isinstance(stages, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in stages[:MAX_REMOTE_STAGES]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("stage") or "").strip()
        if not name:
            continue
        try:
            duration = int(item.get("duration_us"))
        except (TypeError, ValueError):
            continue
        entry: dict[str, Any] = {
            "stage": name[:MAX_ATTRIBUTE_CHARS],
            "duration_us": max(0, duration),
            "outcome": str(item.get("outcome") or "ok")[:MAX_ATTRIBUTE_CHARS],
            "source": source,
        }
        if call_id:
            entry["call_id"] = call_id
        if item.get("truncated"):
            # A producer that stopped at its own deadline must not look like a
            # producer that finished, so the flag survives the boundary.
            entry["truncated"] = True
        entries.append(entry)
    return entries[:MAX_REMOTE_STAGES]


def attach_remote_stages(
    measurement: Measurement | None,
    stage_name: str,
    stages: Any,
    *,
    source: str,
    call_id: str | None = None,
) -> bool:
    """Attach durations another producer measured to one local stage.

    Returns whether anything was attached, so a caller can report a partial
    timeline instead of claiming a producer that never contributed.
    """
    if measurement is None or not _recording(measurement):
        return False
    entries = _bounded_remote_stages(stages, source, call_id)
    if not entries:
        return False
    measurement.remote_stages.setdefault(stage_name, []).extend(entries)
    measurement.producers.add(source)
    if any(entry.get("truncated") for entry in entries):
        measurement.truncated = True
    return True


def set_attributes(measurement: Measurement | None, **values: Any) -> None:
    """Attach bounded attributes to a measurement."""
    if measurement is None or not _recording(measurement):
        return
    for key, value in values.items():
        measurement.facts[key] = _bounded(value)


def mark(measurement: Measurement | None, name: str) -> None:
    """Record a milestone, typically a stage boundary.

    Consecutive marks become the derived ``stages`` list, and the closing
    boundary that :func:`finish` appends gives the final stage its duration --
    the stage a failed or slow startup actually stopped in.
    """
    if measurement is None or not _recording(measurement):
        return
    measurement.marks.append((name, measurement.offset_us()))


def span(measurement: Measurement | None, name: str) -> AbstractContextManager[None]:
    """Time one interval. Returns a shared no-op when nothing is selected."""
    if measurement is None or not _recording(measurement):
        return _NULL_SPAN
    if len(measurement.spans) >= MAX_SPANS:
        measurement.dropped_spans += 1
        return _EMPTY_SPAN
    return _timed_span(measurement, name)


@contextmanager
def _timed_span(measurement: Measurement, name: str) -> Iterator[None]:
    """Record one span, closing it on success and on failure alike."""
    ordinal = measurement.ordinals.get(name, 0) + 1
    measurement.ordinals[name] = ordinal
    measurement.span_counter += 1
    record = _SpanRecord(
        span_id=f"s{measurement.span_counter}",
        name=name,
        ordinal=ordinal,
        start_offset_us=measurement.offset_us(),
        parent_span_id=measurement.stack[-1] if measurement.stack else None,
    )
    measurement.stack.append(record.span_id)
    try:
        yield
    except BaseException:
        record.outcome = "error"
        raise
    finally:
        measurement.stack.pop()
        end, truncate = _clamp_to_deadline(measurement, measurement.offset_us())
        record.duration_us = max(0, end - record.start_offset_us)
        record.truncated = truncate
        if len(measurement.spans) < MAX_SPANS:
            measurement.spans.append(record)
        else:  # pragma: no cover - guarded by the ceiling in span()
            measurement.dropped_spans += 1


def _stages(measurement: Measurement) -> list[dict[str, Any]]:
    """Derive per-stage durations, closing the final stage at the measurement end.

    Without the closing boundary the stage that was running when the
    measurement ended had no duration at all, which is exactly the stage a slow
    or failed startup is sitting in.
    """
    if not measurement.marks:
        return []
    ended = measurement.ended_offset_us
    if ended is None:
        ended = measurement.offset_us()
    following = [offset for _, offset in measurement.marks[1:]] + [ended]
    stages: list[dict[str, Any]] = []
    for (name, start), stop in zip(measurement.marks, following, strict=False):
        entry: dict[str, Any] = {
            "stage": name,
            "start_offset_us": start,
            "duration_us": max(0, stop - start),
        }
        children = measurement.remote_stages.get(name)
        if children:
            # Durations from another producer, not offsets on this timeline.
            entry["children"] = children
        stages.append(entry)
    return stages


def to_record(
    measurement: Measurement,
    *,
    outcome: str,
    failure_stage: str | None = None,
) -> dict[str, Any]:
    """Build the emitted record for a finished measurement."""
    ended = measurement.ended_offset_us
    if ended is None:
        ended = measurement.offset_us()
    # Completeness is judged per answered call, not by how much a producer
    # sent: a call that legitimately did no timed work still answered.
    missing_fragments = sorted(
        f"{producer}:{call_id}:{stage}"
        for producer, call_id, stage in measurement.expected_fragments
        if (producer, call_id) not in measurement.answered_fragments
    )
    missing_producers = sorted({item.split(":", 1)[0] for item in missing_fragments})
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "measurement_id": measurement.measurement_id,
        "profiling_run_id": measurement.run_id,
        "operation": measurement.operation,
        "producer": "runner",
        "producer_instance_id": PRODUCER_INSTANCE_ID,
        "clock_domain_id": CLOCK_DOMAIN_ID,
        # Who actually contributed to this record. A run needs the broker's
        # switch as well as the runner's for the container sub-stages, and a
        # missing producer is reported here rather than silently absent.
        "producers": ["runner", *sorted(measurement.producers)],
        "startup_outcome": outcome,
        "duration_us": ended,
        "measurement_complete": (
            outcome != "incomplete"
            and measurement.dropped_spans == 0
            and not measurement.expired
            and not measurement.truncated
            and not missing_fragments
        ),
        "dropped_spans": measurement.dropped_spans,
        # Completion order is not timeline order: a nested span closes first.
        "spans": [
            item.to_payload()
            for item in sorted(measurement.spans, key=lambda span: span.start_offset_us)
        ],
        "milestones": [
            {"name": name, "offset_us": offset, "source": "runner"}
            for name, offset in measurement.marks
        ],
        "stages": _stages(measurement),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "facts": measurement.facts,
    }
    if measurement.truncated:
        record["truncated"] = True
    if measurement.truncated_fragments:
        record["truncated_fragments"] = sorted(
            f"{producer}:{call_id}" for producer, call_id in measurement.truncated_fragments
        )
    if missing_fragments:
        record["missing_fragments"] = missing_fragments
        record["missing_producers"] = missing_producers
    if failure_stage is not None:
        record["failure_stage"] = failure_stage
    return record


class _Exporter:
    """Publish records from a bounded background queue.

    Both destinations move off the attempt thread, not just the file. The
    record is produced while the sandbox is starting, so anything synchronous
    here -- a JSONL write, or a log handler that blocks on a slow socket, a
    full disk or a synchronous shipper -- is charged to the startup latency the
    profile exists to explain. Logging is not exempt just because it is the
    established path.

    One daemon thread drains the queue, and it starts only once a live run has
    a record to export. The queue is bounded: an exporter that cannot keep up
    drops records and counts them rather than growing without limit or
    blocking the caller.
    """

    def __init__(self, *, maxsize: int = SINK_QUEUE_MAX) -> None:
        self.dropped = 0
        #: A process that cannot start the thread can still serve traffic. It
        #: keeps running with the measurement dropped rather than failing the
        #: startup that asked for it.
        self.disabled = False
        self._queue: queue.Queue[tuple[str, str | None] | None] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._pending = 0
        self._closing = False
        self._warned = False
        self._drops_reported = False
        self._thread = threading.Thread(
            target=self._run, name="operation-profile-sink", daemon=True
        )
        try:
            self._thread.start()
        except Exception as exc:
            # A thread limit, a fork in a bad state, a sandbox refusing
            # clone(): none of that is a reason to fail the business call this
            # measurement exists to observe, and this runs while a prompt has
            # already been sent.
            self.disabled = True
            self._closing = True
            logger.warning(
                "operation profiling could not start its exporter (%s); "
                "measurements will be dropped",
                exc,
            )
            return
        atexit.register(self._flush_before_exit)

    def offer(self, line: str, sink: str | None) -> None:
        """Queue one record. Never blocks and never raises."""
        with self._lock:
            if self._closing:
                if self.disabled:
                    self.dropped += 1
                return
            # Counted before the item is visible, so the drain thread can never
            # observe a decrement it did not earn.
            self._pending += 1
        try:
            self._queue.put_nowait((line, sink))
        except queue.Full:
            with self._lock:
                self._pending -= 1
                self.dropped += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                line, sink = item
                self._publish(line, sink)
            finally:
                with self._lock:
                    self._pending -= 1

    def _publish(self, line: str, sink: str | None) -> None:
        """Log one record and, when configured, append it to the sink."""
        logger.info("agent_operation_profile %s", line)
        if not sink:
            return
        try:
            with open(sink, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            # One warning per exporter: a broken path would otherwise log on
            # every record for the length of the experiment.
            with self._lock:
                first = not self._warned
                self._warned = True
            if first:
                logger.warning("operation profiling sink %s is unavailable (%s)", sink, exc)

    def flush(self, timeout: float = 2.0) -> None:
        """Wait until queued records are published, or the timeout expires."""
        if self.disabled:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending <= 0:
                    return
            time.sleep(0.005)

    def close(self, timeout: float = 2.0) -> None:
        """Drain, stop the exporter thread, and report anything it dropped."""
        with self._lock:
            self._closing = True
        self.flush(timeout)
        if self.disabled:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)
        with self._lock:
            report, self._drops_reported = self.dropped > 0 and not self._drops_reported, True
            dropped = self.dropped
        if report:
            logger.warning("operation profiling exporter dropped %d record(s)", dropped)

    def _flush_before_exit(self) -> None:
        """Best-effort drain at interpreter shutdown, bounded by the timeout."""
        self.close(timeout=2.0)


_EXPORT_LOCK = threading.Lock()
_EXPORTER: _Exporter | None = None


def _exporter() -> _Exporter:
    """Return the process exporter, starting it on the first record.

    A process that cannot start the thread gets one permanently disabled
    exporter rather than an exception per record: retrying a thread start on
    every measurement would be its own cost, and the point is that the business
    path never learns this failed.
    """
    global _EXPORTER
    with _EXPORT_LOCK:
        if _EXPORTER is None:
            _EXPORTER = _Exporter()
        return _EXPORTER


def flush_sink(timeout: float = 2.0) -> None:
    """Wait for queued records to be logged and written."""
    with _EXPORT_LOCK:
        exporter = _EXPORTER
    if exporter is not None:
        exporter.flush(timeout)


def close_sink(timeout: float = 2.0) -> None:
    """Flush and stop the export thread, if one was ever started."""
    global _EXPORTER
    with _EXPORT_LOCK:
        exporter, _EXPORTER = _EXPORTER, None
    if exporter is not None:
        exporter.close(timeout)


def _emit(record: dict[str, Any]) -> None:
    """Hand one finished record to the export thread.

    Nothing here touches a destination: logging blocks just as a file write
    does, and both would otherwise land inside the startup being measured.
    """
    try:
        line = json.dumps(record, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - facts are bounded already
        logger.warning("operation profiling could not serialize a measurement")
        return
    sink = (os.environ.get(ENV_PROFILING_SINK) or "").strip() or None
    # Never on the attempt thread: see _Exporter. A measurement that cannot be
    # exported is dropped; it is never allowed to fail the work it observed.
    try:
        _exporter().offer(line, sink)
    except Exception as exc:  # pragma: no cover - guarded in _Exporter too
        logger.warning("operation profiling could not publish a measurement (%s)", exc)


def finish(
    measurement: Measurement | None,
    *,
    outcome: str,
    failure_stage: str | None = None,
) -> None:
    """Emit the measurement. Safe to call twice and safe on ``None``."""
    if measurement is None or measurement.finished:
        return
    measurement.finished = True
    # The record about to be emitted is what the reserved slot was for, so
    # `discard` can no longer return it.
    measurement.reserved = False
    measurement.expired = measurement.expired or measurement.past_deadline()
    ended = measurement.offset_us()
    if measurement.expired:
        # Collecting stopped at the deadline, so the record ends there too.
        ended = min(ended, max(0, measurement.deadline_offset_us()))
    measurement.ended_offset_us = ended
    if not measurement.selected:
        return
    if outcome not in _OUTCOMES:  # pragma: no cover - guarded by callers
        outcome = "incomplete"
    run = _load().run
    if run is not None and run.target and not measurement.bound:
        # A scoped run reports only attempts whose scope was proved; emitting
        # an unbound measurement would claim a sample the run never selected.
        logger.warning(
            "operation profiling dropped an unbound measurement (run %s, operation %s)",
            run.run_id,
            measurement.operation,
        )
        return
    # Written directly rather than through set_attributes, which stops
    # collecting, and the closing facts have to survive that boundary.
    measurement.facts["startup_outcome"] = outcome
    if failure_stage:
        measurement.facts["failure_stage"] = failure_stage
    if measurement.expired:
        measurement.facts["profiling_stopped"] = "expired"
    _emit(to_record(measurement, outcome=outcome, failure_stage=failure_stage))


def discard(measurement: Measurement | None) -> None:
    """Stop timing without emitting, returning the reserved budget slot."""
    if measurement is None or measurement.finished:
        return
    measurement.finished = True
    if measurement.reserved:
        measurement.reserved = False
        _release_budget()


def reset_cache() -> None:
    """Drop the cached switch state and stop the sink. Tests call this."""
    global _CACHE, _USED_OPERATIONS
    with _CACHE_LOCK:
        _CACHE = None
    with _BUDGET_LOCK:
        _USED_OPERATIONS = 0
    close_sink()


__all__ = [
    "CLOCK_DOMAIN_ID",
    "ENV_PROFILING_ENABLED",
    "ENV_PROFILING_RUN",
    "ENV_PROFILING_SINK",
    "MAX_REMOTE_STAGES",
    "MAX_RUN_ID_CHARS",
    "MAX_SPANS",
    "PRODUCER_INSTANCE_ID",
    "SCHEMA_VERSION",
    "SINK_QUEUE_MAX",
    "TARGET_KEYS",
    "Measurement",
    "ProfilingRun",
    "StageObserver",
    "StageRecorder",
    "active_run",
    "attach_remote_stages",
    "bind",
    "close_sink",
    "context_call_id",
    "discard",
    "expect_fragment",
    "finish",
    "flush_sink",
    "mark",
    "measurement_context",
    "parse_run",
    "reason",
    "record_fragment",
    "reset_cache",
    "set_attributes",
    "span",
    "stage_observer",
    "start_operation",
    "start_stage_recording",
    "substage",
    "to_record",
]
