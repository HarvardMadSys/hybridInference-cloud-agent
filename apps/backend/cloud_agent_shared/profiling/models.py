"""Run descriptors and mutable timing state, without policy or export side effects."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 -- public dataclass type introspection
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

from .clock import clock
from .schema import (
    CLOCK_DOMAIN_ID,
    DEFAULT_PRODUCER,
    MODE_DISABLED,
    PRODUCER_INSTANCE_ID,
    SOURCE_ENVIRONMENT,
)


@dataclass(frozen=True)
class AdmissionInstance(Mapping[str, object]):
    """One producer instance's immutable contribution to a run budget."""

    instance: str
    max_operations: int

    def __getitem__(self, key: str) -> object:
        """Return one field through the legacy mapping interface."""
        if key == "instance":
            return self.instance
        if key == "max_operations":
            return self.max_operations
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        """Iterate fields in their stable wire order."""
        return iter(("instance", "max_operations"))

    def __len__(self) -> int:
        """Return the fixed number of admission fields."""
        return 2

    def to_payload(self) -> dict[str, Any]:
        """Return the catalogue and handoff representation."""
        return {"instance": self.instance, "max_operations": self.max_operations}


@dataclass(frozen=True)
class RunManifest:
    """The canonical immutable identity of one profiling experiment.

    The object owns the normalized vocabulary shared by local selection,
    handoffs, records, and catalogue registration. Its nested values are
    immutable too: callers cannot mutate a target or admission after a
    measurement has retained the manifest.
    """

    run_id: str
    operations: frozenset[str]
    expires_at: datetime | None
    max_operations: int
    continuous: bool = False
    target: Mapping[str, str] = field(default_factory=dict)
    config_version: int = 1
    admission_instances: tuple[AdmissionInstance, ...] = ()
    #: Who rooted this run: a static deployment descriptor or an administrator.
    source: str = SOURCE_ENVIRONMENT

    def __post_init__(self) -> None:
        """Freeze caller-owned collections at the normalization boundary."""
        object.__setattr__(self, "operations", frozenset(self.operations))
        object.__setattr__(
            self,
            "target",
            MappingProxyType(
                dict(sorted((str(key), str(value)) for key, value in self.target.items()))
            ),
        )
        object.__setattr__(self, "admission_instances", tuple(self.admission_instances))

    def _wire_identity(self) -> dict[str, Any]:
        """Return the fields shared by the catalogue and handoff projections."""
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "operations": sorted(self.operations),
            "target": dict(self.target),
            "config_version": self.config_version,
            "max_operations": self.max_operations,
        }
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at.isoformat()
        elif self.continuous:
            payload.update(expires_at=None, continuous=True)
        return payload

    def to_handoff_payload(self, *, control_revision: int) -> dict[str, Any]:
        """Project this identity into the nested process-handoff format."""
        payload = self._wire_identity()
        payload.update(control_revision=control_revision, source=self.source)
        return payload

    def to_catalogue_payload(self) -> dict[str, Any] | None:
        """Project this identity into the trusted storage catalogue format."""
        if self.expires_at is None and not self.continuous:
            return None
        payload = self._wire_identity()
        payload["admission_instances"] = [item.to_payload() for item in self.admission_instances]
        return payload


@dataclass(frozen=True)
class ProfilingRun:
    """A manifest plus the process-local deadline that enforces it.

    ``deadline_mono`` is derived from ``manifest.expires_at`` when the descriptor is
    first read, so a process restart or a wall-clock jump cannot extend a run.

    The manifest's absolute expiry is the configured instant itself, and it is a different
    value from ``deadline_mono`` in every way that matters downstream: it is
    what the experiment *is*, so a stored manifest is identified by it, while
    the monotonic deadline is what this process enforces locally. A consumer
    handed the monotonic form and asked to re-derive a wall clock would be
    reconstructing the configuration from a measurement of it.
    """

    manifest: RunManifest
    deadline_mono: float

    @property
    def run_id(self) -> str:
        """Return the manifest's run id."""
        return self.manifest.run_id

    @property
    def operations(self) -> frozenset[str]:
        """Return the operations selected by the manifest."""
        return self.manifest.operations

    @property
    def expires_at(self) -> str:
        """Return the normalized configured expiry for compatibility."""
        return self.manifest.expires_at.isoformat() if self.manifest.expires_at else ""

    @property
    def expires_at_utc(self) -> datetime | None:
        """Return the configured absolute expiry."""
        return self.manifest.expires_at

    @property
    def max_operations(self) -> int:
        """Return the local admission budget."""
        return self.manifest.max_operations

    @property
    def continuous(self) -> bool:
        """Return whether the run has no time or sample limit."""
        return self.manifest.continuous

    @property
    def target(self) -> Mapping[str, str]:
        """Return the immutable target mapping."""
        return self.manifest.target

    @property
    def config_version(self) -> int:
        """Return the immutable manifest version."""
        return self.manifest.config_version

    @property
    def admission_instances(self) -> tuple[AdmissionInstance, ...]:
        """Return the immutable admission instances."""
        return self.manifest.admission_instances

    @property
    def source(self) -> str:
        """Return the control source that rooted the run."""
        return self.manifest.source


@dataclass(frozen=True)
class ControlState:
    """The collection configuration a control source is currently stating.

    ``config_version`` is the only ordering this feature has, and it is the
    order a *configuration* moved in: Start, Stop and the next Start each
    advance it, and a snapshot carrying a lower version than the one already
    applied is refused rather than applied. That is the whole of the
    anti-rollback rule -- a delayed or reordered answer from the control plane
    must not put a stopped experiment back into force.

    It is deliberately not the run's own ``config_version`` (that one says which
    immutable manifest this is) and not a lease counter: there is no lease here.
    A host that cannot reach the control plane keeps the last configuration it
    was given and stops selecting when that experiment's own ``expires_at``
    passes. A process whose own read of the control source fails -- the control
    plane's loop, or an installed provider that raises -- withdraws the run
    instead, through :meth:`RuntimePolicyStore.suspend`.
    """

    mode: str = MODE_DISABLED
    config_version: int = 0
    run: ProfilingRun | None = None
    #: Why there is no run, when there is none. Purely informational: it is what
    #: the page shows, never something the collector branches on.
    stop_reason: str = ""


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
    #: The exact immutable experiment snapshot that selected this measurement.
    #: It is retained once, rather than unpacked into parallel ``run_*`` fields.
    run_manifest: RunManifest
    measurement_id: str
    started_mono_ns: int
    #: The experiment's own monotonic deadline, and the only thing that ends
    #: this measurement. There is no lease: a Stop stops *selecting* new
    #: operations, and an operation that already started runs to its own
    #: boundary and is delivered. What bounds a node that never hears the Stop
    #: is this instant, derived from the run's configured expiry.
    deadline_mono: float
    #: The configuration version this measurement was selected under. It travels
    #: with the selection so a child or a sandbox manager can check that the selection it
    #: was handed belongs to the configuration they hold, and so a record says
    #: which version of the switch produced it.
    control_revision: int = 0
    #: Producer that rooted this operation: the runner for an attempt startup,
    #: the control plane for a file or terminal request. It names the record's
    #: owner and the source of its local milestones. Declared among the
    #: defaulted fields because :func:`start_operation` sets it by keyword.
    producer: str = DEFAULT_PRODUCER
    producer_instance_id: str = PRODUCER_INSTANCE_ID
    clock_domain_id: str = CLOCK_DOMAIN_ID
    #: How far these boundaries reach. Empty means the root never stated it,
    #: and the record then claims no coverage rather than assuming the widest.
    coverage_scope: str = ""
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
    #: Producers that sent durations this record accepted. Not the same as
    #: having answered a call: durations attach to a stage, so a producer whose
    #: timings landed on a stage no mark produced contributed nothing to the
    #: timeline even though it collected.
    contributors: set[str] = field(default_factory=set)
    #: Producers that answered for a call on this record, whether or not their
    #: durations reached the timeline.
    answered_by: set[str] = field(default_factory=set)
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
    #: Set by :func:`finalize` when the end boundary and completeness are
    #: frozen. After this, no report about the measurement re-reads the clock.
    frozen: bool = False
    #: The outcome frozen with the boundary, so ``finish`` cannot change it.
    frozen_outcome: str = ""
    #: The completeness frozen with the boundary, so a response and the record
    #: it describes cannot disagree.
    frozen_complete: bool = False
    #: The immutable payload built from the frozen boundary. Log, JSONL, HTTP
    #: delivery, and in-process storage all reuse these exact values.
    frozen_payload: dict[str, Any] | None = None
    frozen_failure_stage: str | None = None
    #: The budget slot is held from selection and returned by `discard`.
    reserved: bool = True
    selected: bool = True
    finished: bool = False

    @property
    def run_id(self) -> str:
        """Return the retained manifest's run id."""
        return self.run_manifest.run_id

    @property
    def run_operations(self) -> frozenset[str]:
        """Compatibility view of the retained manifest's operations."""
        return self.run_manifest.operations

    @property
    def run_target(self) -> Mapping[str, str]:
        """Compatibility view of the retained immutable target."""
        return self.run_manifest.target

    @property
    def run_max_operations(self) -> int:
        """Compatibility view of the retained manifest budget."""
        return self.run_manifest.max_operations

    @property
    def run_continuous(self) -> bool:
        """Compatibility view of the retained continuous flag."""
        return self.run_manifest.continuous

    @property
    def run_config_version(self) -> int:
        """Compatibility view of the retained manifest version."""
        return self.run_manifest.config_version

    @property
    def run_source(self) -> str:
        """Compatibility view of the retained control source."""
        return self.run_manifest.source

    @property
    def run_expires_at(self) -> datetime | None:
        """Compatibility view of the retained configured expiry."""
        return self.run_manifest.expires_at

    @property
    def run_admission_instances(self) -> tuple[AdmissionInstance, ...]:
        """Compatibility view of the retained admission instances."""
        return self.run_manifest.admission_instances

    def offset_us(self, mono_ns: int | None = None) -> int:
        """Return microseconds since this measurement started."""
        now = clock().monotonic_ns() if mono_ns is None else mono_ns
        return (now - self.started_mono_ns) // 1000

    def past_deadline(self) -> bool:
        """Return whether the experiment has ended while this measurement is open.

        A measurement that outlives its run is closed at the deadline rather
        than at the moment somebody noticed, so a partial record never reports
        time the experiment was not collecting for.
        """
        if clock().monotonic() > self.deadline_mono:
            self.expired = True
            return True
        return False

    def deadline_offset_us(self) -> int:
        """Return the offset at which this measurement's run expires."""
        return int(self.deadline_mono * 1_000_000) - self.started_mono_ns // 1000


class _NullSpan:
    """Shared no-op context manager, so the off path allocates nothing."""

    __slots__ = ()

    def __enter__(self) -> None:
        """Enter a span that records nothing."""
        return None

    def __exit__(self, *exc_info: object) -> Literal[False]:
        """Leave without suppressing the exception."""
        return False


_NULL_SPAN = _NullSpan()


_EMPTY_SPAN: AbstractContextManager[None] = nullcontext()
