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
span.

A run is only live when all of the following hold; anything else leaves the
business path untouched and logs one warning:

* ``AGENT_OPERATION_PROFILING_ENABLED`` is truthy,
* ``AGENT_OPERATION_PROFILING_RUN`` is a JSON descriptor with a ``run_id``, a
  non-empty ``operations`` list, a future ``expires_at``, and a positive
  ``max_operations``.

Known limits of this slice, all deliberate:

* Selection happens at the runner's root operation, not at the control plane.
  ``max_operations`` is therefore a per-process budget, not a fleet-wide one.
* ``target`` accepts ``host`` (known before claim) plus ``job_id``,
  ``thread_id`` and ``sandbox_id`` (known only once an attempt is claimed), so
  a target filter is applied in two steps and a measurement that falls out of
  scope after claim is dropped without emitting a record.
* Records go to a structured log line and, when
  ``AGENT_OPERATION_PROFILING_SINK`` names a path, to an appended JSONL file.
  Durable per-attempt storage and the admin reading surface are later slices.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

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
    facts: dict[str, Any] = field(default_factory=dict)
    spans: list[_SpanRecord] = field(default_factory=list)
    marks: list[tuple[str, int]] = field(default_factory=list)
    ordinals: dict[str, int] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    # Span ids come from a counter, not from the number of closed spans: a
    # nested span closes first, so counting appends handed two live spans the
    # same id and made a child its own parent.
    span_counter: int = 0
    dropped_spans: int = 0
    selected: bool = True
    finished: bool = False

    def offset_us(self, mono_ns: int | None = None) -> int:
        """Return microseconds since this measurement started."""
        now = time.monotonic_ns() if mono_ns is None else mono_ns
        return (now - self.started_mono_ns) // 1000


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


def reset_cache() -> None:
    """Drop the cached switch state. Tests call this after changing the env."""
    global _CACHE, _USED_OPERATIONS
    with _CACHE_LOCK:
        _CACHE = None
    with _BUDGET_LOCK:
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
        ValueError: the descriptor is not an object, or is missing a run id, a
            non-empty operation list, a future expiry, or a positive cap.
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
    target = {
        str(key): str(value)
        for key, value in raw_target.items()
        if isinstance(key, str) and isinstance(value, (str, int)) and str(value).strip()
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


def _deadline(run: ProfilingRun) -> float:
    """Return the run's monotonic deadline."""
    return run.deadline_mono


def _claim_budget(run: ProfilingRun) -> bool:
    """Reserve one root-operation slot, or return ``False`` when used up."""
    global _USED_OPERATIONS
    with _BUDGET_LOCK:
        if run.max_operations <= _USED_OPERATIONS:
            return False
        _USED_OPERATIONS += 1
        return True


def start_operation(
    *,
    operation: str,
    host: str | None = None,
    worker_id: str | None = None,
) -> Measurement | None:
    """Begin timing a root operation, or return ``None`` when not selected.

    Only ``host`` and ``worker_id`` are known this early; the remaining target
    facts arrive with :func:`bind` once an attempt has been claimed.
    """
    loaded = _load()
    run = loaded.run
    if run is None or operation not in run.operations:
        return None
    if time.monotonic() > _deadline(run):
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
        facts={"operation": operation},
    )
    if host:
        measurement.facts["host"] = host
    if worker_id:
        measurement.facts["worker_id"] = worker_id
    return measurement


def bind(measurement: Measurement | None, **facts: Any) -> bool:
    """Record post-claim facts and report whether the measurement is in scope.

    Returns ``False`` when a configured target filter names a fact that does not
    match, and also when the measurement is ``None``. Callers should
    :func:`discard` it in that case, so a mismatched attempt emits nothing.
    """
    if measurement is None or measurement.finished:
        return False
    run = _load().run
    for key, value in facts.items():
        measurement.facts[key] = _bounded(value)
    if run is not None:
        for key, wanted in run.target.items():
            observed = facts.get(key)
            if observed is not None and str(observed) != wanted:
                measurement.selected = False
                return False
    return measurement.selected


def _bounded(value: Any) -> Any:
    """Return an attribute value the record may store."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_ATTRIBUTE_CHARS]


def set_attributes(measurement: Measurement | None, **values: Any) -> None:
    """Attach bounded attributes to a measurement."""
    if measurement is None or measurement.finished:
        return
    for key, value in values.items():
        measurement.facts[key] = _bounded(value)


def mark(measurement: Measurement | None, name: str) -> None:
    """Record a milestone, typically a stage boundary.

    Consecutive marks become the derived ``stages`` list, so a caller that
    already tracks a phase name gets per-stage durations for one line of code.
    """
    if measurement is None or measurement.finished:
        return
    measurement.marks.append((name, measurement.offset_us()))


def span(measurement: Measurement | None, name: str) -> AbstractContextManager[None]:
    """Time one interval. Returns a shared no-op when nothing is selected."""
    if measurement is None or measurement.finished:
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
        record.duration_us = measurement.offset_us() - record.start_offset_us
        if len(measurement.spans) < MAX_SPANS:
            measurement.spans.append(record)
        else:  # pragma: no cover - guarded by the ceiling in span()
            measurement.dropped_spans += 1


def _stages(measurement: Measurement) -> list[dict[str, Any]]:
    """Derive per-stage durations from consecutive marks."""
    stages: list[dict[str, Any]] = []
    for (name, start), (_, following) in zip(
        measurement.marks, measurement.marks[1:], strict=False
    ):
        stages.append({"stage": name, "start_offset_us": start, "duration_us": following - start})
    return stages


def to_record(
    measurement: Measurement,
    *,
    outcome: str,
    failure_stage: str | None = None,
) -> dict[str, Any]:
    """Build the emitted record for a finished measurement."""
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "measurement_id": measurement.measurement_id,
        "profiling_run_id": measurement.run_id,
        "operation": measurement.operation,
        "producer": "runner",
        "producer_instance_id": PRODUCER_INSTANCE_ID,
        "clock_domain_id": CLOCK_DOMAIN_ID,
        "startup_outcome": outcome,
        "measurement_complete": outcome != "incomplete" and measurement.dropped_spans == 0,
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
    if failure_stage is not None:
        record["failure_stage"] = failure_stage
    return record


def _emit(record: dict[str, Any]) -> None:
    """Write one finished record to the log and the optional JSONL sink."""
    try:
        line = json.dumps(record, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - facts are bounded already
        logger.warning("operation profiling could not serialize a measurement")
        return
    logger.info("agent_operation_profile %s", line)
    sink = (os.environ.get(ENV_PROFILING_SINK) or "").strip()
    if not sink:
        return
    try:
        with _SINK_LOCK, open(sink, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        # Losing the sink must never fail the operation being measured.
        logger.warning("operation profiling sink %s is unavailable (%s)", sink, exc)


_SINK_LOCK = threading.Lock()


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
    if not measurement.selected:
        return
    if outcome not in _OUTCOMES:  # pragma: no cover - guarded by callers
        outcome = "incomplete"
    set_attributes(measurement, startup_outcome=outcome)
    if failure_stage:
        set_attributes(measurement, failure_stage=failure_stage)
    _emit(to_record(measurement, outcome=outcome, failure_stage=failure_stage))


def discard(measurement: Measurement | None) -> None:
    """Stop timing without emitting anything."""
    if measurement is None or measurement.finished:
        return
    measurement.finished = True


__all__ = [
    "CLOCK_DOMAIN_ID",
    "ENV_PROFILING_ENABLED",
    "ENV_PROFILING_RUN",
    "ENV_PROFILING_SINK",
    "MAX_SPANS",
    "PRODUCER_INSTANCE_ID",
    "SCHEMA_VERSION",
    "Measurement",
    "ProfilingRun",
    "active_run",
    "bind",
    "discard",
    "finish",
    "mark",
    "parse_run",
    "reason",
    "reset_cache",
    "set_attributes",
    "span",
    "start_operation",
    "to_record",
]
