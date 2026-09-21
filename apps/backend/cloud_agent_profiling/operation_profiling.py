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

* Selection happens at each producer's root operation. ``max_operations`` is
  therefore a per-process budget, not a fleet-wide one, and a deployment
  experiments with one admission instance until shared admission exists.
* ``target`` accepts ``host`` (known before claim) plus ``job_id``,
  ``thread_id`` and ``sandbox_id`` (known only once an attempt is claimed), so
  a target filter is applied in two steps: the cheap pre-filter at
  :func:`start_operation`, then the authoritative :func:`bind`, which requires
  every configured field to be present and equal. A scoped measurement that was
  never bound is dropped rather than reported.
* Records go to a structured log line and, when configured, an appended JSONL
  file. Root producers can also hand the same frozen payload to bounded
  durable delivery; storage never rebuilds a second version of the record.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import queue
import re
import socket
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Deployment-level master switch. Absent or false means "do not profile".
ENV_PROFILING_ENABLED = "AGENT_OPERATION_PROFILING_ENABLED"

#: JSON descriptor for the current profiling run. See :class:`ProfilingRun`.
ENV_PROFILING_RUN = "AGENT_OPERATION_PROFILING_RUN"

#: Which control source this deployment allows. One of :data:`PROFILING_MODES`.
#: Absent keeps the pre-control semantics exactly (``ENABLED`` + ``RUN``, read
#: once per process); ``admin`` is what makes an Admin Start/Stop take effect
#: without a restart, and the only mode in which a runtime policy replaces the
#: environment. It is read once per process: it is deployment configuration,
#: and the design says the mode is *how* a deployment may be controlled, never
#: the per-experiment switch itself.
ENV_PROFILING_MODE = "AGENT_OPERATION_PROFILING_MODE"

#: Optional JSONL path. Each finished measurement is appended as one line.
ENV_PROFILING_SINK = "AGENT_OPERATION_PROFILING_SINK"

#: The three answers to "who decides whether this deployment collects".
#:
#: ``disabled``  -- nobody does. Admin shows the control as unavailable rather
#:                  than offering a Start that would do nothing.
#: ``environment`` -- only the static ``ENABLED``/``RUN`` pair does, re-read
#:                  only when the process restarts. Admin says the deployment
#:                  is managed by configuration and offers no fake Stop.
#: ``admin``     -- only the persisted control state does. ``ENABLED=false``
#:                  no longer vetoes an Admin Start, and no old ``RUN`` may be
#:                  used as a fallback after an Admin Stop: a Stop has to mean
#:                  stopped, and a leftover descriptor silently re-enabling a
#:                  lapsed experiment is exactly the "off that is not off" the
#:                  design forbids.
MODE_DISABLED = "disabled"
MODE_ENVIRONMENT = "environment"
MODE_ADMIN = "admin"

#: Declared explicitly rather than derived, so a typo in the environment is
#: refused instead of being read as "the default".
PROFILING_MODES = (MODE_DISABLED, MODE_ENVIRONMENT, MODE_ADMIN)

#: Who rooted a run. A record carries this so a reader can tell a static
#: env experiment from one an administrator started, which is what makes the
#: "no control state" compatibility answer possible instead of a guess.
SOURCE_ENVIRONMENT = "environment"
SOURCE_ADMIN = "admin"

#: Major is bumped when a reader of an older record would misread a new one;
#: minor when fields are only added. v2 is additive over v1: ``outcome`` joins
#: ``startup_outcome``, and ``producer`` names whichever producer rooted the
#: operation instead of always the runner. A v1 reader can still consume a v2
#: record for ``attempt.startup`` by ignoring fields it does not know.
SCHEMA_VERSION = 3

#: Version of the *boundaries* this record's operation measured. Bumped when a
#: start or end point moves, so two records with the same operation name are
#: never compared as if they measured the same span.
BOUNDARY_VERSION = 1

#: How far the measured span reaches. A server-side record never covers the
#: user's own wait, and saying so in the data is what stops a reader from
#: presenting it as end to end.
COVERAGE_HOST_STARTUP = "host_startup"
COVERAGE_SERVER_REQUEST = "server_request"
COVERAGE_BROWSER_INTERACTION = "browser_interaction"

#: The coverage each root operation's boundaries actually reach. Filled in
#: below, once the operation names themselves are declared.
COVERAGE_BY_OPERATION: dict[str, str] = {}
BOUNDARIES_BY_OPERATION: dict[str, tuple[str, str]] = {
    "attempt.startup": ("runner.claim.request", "supervisor.prompt.dispatched"),
    "files.list": (
        "control_plane.operation.selected",
        "control_plane.business_result.validated",
    ),
    "files.open": (
        "control_plane.operation.selected",
        "control_plane.business_result.validated",
    ),
    "terminal.open": (
        "control_plane.operation.selected",
        "control_plane.business_result.validated",
    ),
}

#: The runner's root operation. Only this operation keeps the v1
#: ``startup_outcome`` alias, because that name is a claim about a startup.
STARTUP_OPERATION = "attempt.startup"

#: Alias carrying the same value as ``outcome``, written only for
#: ``attempt.startup``. A file read is not a startup and must not say it is.
STARTUP_OUTCOME_ALIAS = "startup_outcome"

COVERAGE_BY_OPERATION[STARTUP_OPERATION] = COVERAGE_HOST_STARTUP

#: Producer name for a record whose root is the runner process.
DEFAULT_PRODUCER = "runner"

#: Header a selected root operation's context travels in. It is measurement
#: metadata, never a credential, and every producer re-validates it
#: against its own switch, run and deadline.
PROFILING_CONTEXT_HEADER = "X-Agent-Operation-Profiling"

#: Headers a producer answers in when its response body cannot carry the
#: answer -- a terminal stream has already started sending the terminal's own
#: bytes. Lower case because that is how an ASGI response exposes headers.
PROFILING_ANSWER_CALL_ID_HEADER = "x-agent-profiling-call-id"
PROFILING_ANSWER_RECORDED_HEADER = "x-agent-profiling-recorded"
PROFILING_ANSWER_TRUNCATED_HEADER = "x-agent-profiling-truncated"
PROFILING_ANSWER_STAGES_HEADER = "x-agent-profiling-stages"
#: Which operation the producer decided this call was, when the context offered
#: candidates. A file path is collected before its kind is known.
PROFILING_ANSWER_OPERATION_HEADER = "x-agent-profiling-operation"

#: Longest context a caller may hand over. The context carries five bounded
#: strings, so anything larger is a malformed or hostile value, not a selection.
MAX_CONTEXT_CHARS = 2048

#: How long a control-plane instance remembers a selection while a browser
#: interaction continues across requests (a terminal open is a list, a create
#: and a stream). Beyond the run's own deadline the selection is gone.
SELECTION_TTL_S = 120.0

#: Ceiling on remembered selections per process, so a client that never
#: finishes an interaction cannot grow the table without bound.
MAX_SELECTIONS = 512

#: Run ids are used in Admin URL path segments as well as database keys.
#: Accepting slashes makes a descriptor valid but its summary route
#: unreachable after URL decoding.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

#: Supplied by the control plane as the fallback root stage name when no
#: operation is inherited. Kept generic deliberately: an unmarked file or
#: terminal measurement is not a startup, and must not be labelled one.
DEFAULT_ROOT_STAGE = "operation"

#: The only reason a record states for collection ending mid-flight: the
#: experiment reached its own deadline. A Stop does not truncate work that has
#: already started, so there is no second reason to declare.
STOP_REASON_EXPIRED = "expired"
#: Why a configuration is not collecting, when it is not. Shown by the page.
STOP_REASON_STOPPED = "stopped"

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

#: Every outcome a root operation may report. The runner's startup outcomes are
#: about an attempt's fate; the others are about the endpoint a platform
#: operation reached and are what a file or terminal request reports. One list,
#: because the schema is one schema -- but no producer borrows the other's
#: words: a startup is never "completed" and a file read is never "dispatched".
OUTCOMES = frozenset(
    {
        # attempt.startup
        "dispatched",
        "failed",
        "cancelled",
        "lease_lost",
        "incomplete",
        # files.list, files.open, terminal.open
        "completed",
        # A newer request replaced this one before its result reached the user.
        "superseded",
    }
)

#: Retained for callers written against the first slice.
_OUTCOMES = OUTCOMES


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

    ``expires_at_utc`` is the configured instant itself, and it is a different
    value from ``deadline_mono`` in every way that matters downstream: it is
    what the experiment *is*, so a stored manifest is identified by it, while
    the monotonic deadline is what this process enforces locally. A consumer
    handed the monotonic form and asked to re-derive a wall clock would be
    reconstructing the configuration from a measurement of it.
    """

    run_id: str
    operations: frozenset[str]
    expires_at: str
    expires_at_utc: datetime
    deadline_mono: float
    max_operations: int
    target: Mapping[str, str] = field(default_factory=dict)
    config_version: int = 1
    admission_instances: tuple[Mapping[str, Any], ...] = ()
    #: Who rooted this run: a static deployment descriptor or an administrator.
    #: It travels into every measurement, so a record says which control source
    #: produced it rather than leaving a reader to infer it from a run id.
    source: str = SOURCE_ENVIRONMENT


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
    A process that loses contact with the control source keeps the last
    configuration it was given and stops selecting when that experiment's own
    ``expires_at`` passes.
    """

    mode: str = MODE_DISABLED
    config_version: int = 0
    run: ProfilingRun | None = None
    #: Why there is no run, when there is none. Purely informational: it is what
    #: the page shows, never something the collector branches on.
    stop_reason: str = ""


@dataclass(frozen=True)
class _Published:
    """One immutable snapshot, swapped whole."""

    state: ControlState
    run: ProfilingRun | None
    #: Monotonic order of publication in this process, so a reader can tell
    #: whether the configuration it is acting on is still the current one
    #: without comparing any remote clock.
    generation: int


class RuntimePolicyStore:
    """Inside-process, thread-safe holder for the current collection config.

    Two properties, and both are about the same failure -- a delayed answer
    putting a stopped experiment back into force:

    * **A snapshot is replaced whole.** A reader never sees the new run id
      beside the previous run's operations, target or deadline, because there
      is no moment at which the two are separate objects.
    * **A version never goes backwards.** ``publish`` refuses a snapshot older
      than the one already applied, so a reply that overtook a newer one on the
      wire cannot undo it.

    There is no lease and no renewal. The configuration is what it is until the
    control source says otherwise, and the experiment's own absolute deadline is
    what ends it -- including for a node that never hears anything again.
    """

    def __init__(self) -> None:
        """Start with no configuration: nothing may collect until one arrives."""
        self._lock = threading.Lock()
        self._state: _Published | None = None
        self._generation = 0
        self._version = -1

    def publish(self, state: ControlState, *, run: ProfilingRun | None = None) -> _Published:
        """Replace the snapshot, unless it is older than what is already applied."""
        with self._lock:
            if state.config_version < self._version and self._state is not None:
                return self._state
            parsed = run if run is not None else state.run
            if state.run is not None and parsed is not None and parsed.run_id != state.run.run_id:
                # A caller may hand over an already-parsed descriptor, but it
                # must describe the run the state names. Publishing a mismatch
                # would create exactly the torn snapshot this class prevents.
                raise ValueError("run descriptor does not describe the state's run")
            self._generation += 1
            self._version = state.config_version
            self._state = _Published(state=state, run=parsed, generation=self._generation)
            return self._state

    def clear(self) -> None:
        """Forget the configuration entirely. Tests call this."""
        with self._lock:
            self._state = None
            self._generation = 0
            self._version = -1

    def published(self) -> _Published | None:
        """Return the current snapshot, or ``None`` when none was installed."""
        with self._lock:
            return self._state

    def run(self) -> ProfilingRun | None:
        """Return the experiment this process may collect for, if any."""
        with self._lock:
            return self._state.run if self._state is not None else None

    def control(self) -> ControlState:
        """Return the configuration this process is acting on."""
        with self._lock:
            if self._state is None:
                return ControlState()
            return self._state.state

    def admits(self, run_id: str, *, operations: frozenset[str] | None = None) -> bool:
        """Whether an inherited selection is still one this process may collect for.

        This is how a downstream producer answers "may I collect for this
        selection". The caller names the run it inherited; this process answers
        only if that is the run the configuration still names and that run still
        selects one of the operations in play. A selection from a previous
        experiment is refused rather than reported as if this experiment had
        made it.

        A running measurement is deliberately *not* checked against this: an
        operation that already started finishes and delivers under the
        configuration that selected it. This gate is only about *new* work.
        """
        with self._lock:
            state = self._state
        if state is None or state.run is None or state.run.run_id != run_id:
            return False
        return not operations or bool(state.run.operations & operations)


class PolicyProvider(Protocol):
    """Supplies the collection configuration for whichever source this process uses.

    The control plane implements it over the database and the host agent over
    the internal configuration exchange; the collector only ever calls it. A
    provider that cannot answer publishes nothing, which reads as "no run" --
    the safe direction, because a control source that is unreachable must never
    be mistaken for one that said "collect".
    """

    def snapshot(self) -> ControlState:
        """Return the configuration this process should act on."""
        ...


#: The installed store, and the provider that fills it. Both process-global for
#: the same reason the collector's other state is: one process has one control
#: source, and a second one would make "is collection on" depend on which thread
#: asked.
_STORE = RuntimePolicyStore()
_STORE_LOCK = threading.Lock()
_PROVIDER: PolicyProvider | None = None
#: How many times the installed provider has been read. A control-plane route
#: that answers before its background loop's first pass must not report
#: "unavailable" just because nobody has read the database yet, so the first
#: question forces one read. One integer, checked only when the store is empty.
_REFRESHED = 0


def install_policy_provider(provider: PolicyProvider | None) -> None:
    """Install the process's control provider and drop the cached config.

    Replacing a provider also drops the load cache, so the very next call
    re-reads the control source rather than answering from the previous one's
    answer. A caller that installs a provider and then collects without
    publishing anything collects nothing -- there is no "previous run" to fall
    back to, which is what makes a Stop survive a restart.
    """
    global _PROVIDER, _REFRESHED
    with _STORE_LOCK:
        _PROVIDER = provider
        _REFRESHED = 0
    reset_policy()
    if provider is not None:
        refresh_policy()


def policy_provider() -> PolicyProvider | None:
    """Return the installed provider, for the process that owns it."""
    with _STORE_LOCK:
        return _PROVIDER


def policy_store() -> RuntimePolicyStore:
    """Return the process's policy store.

    Callers that own the control source (the control plane's sync loop, the
    host's configuration client) publish into it; the collector only reads it.
    """
    return _STORE


def refresh_policy() -> _Published:
    """Read the installed provider once and publish what it answered.

    Provider failures are published as "no run" rather than raised: a control
    source that is unreachable must not be read as an experiment, and it must
    not fail the business request that was merely looking for one.
    """
    global _REFRESHED
    with _STORE_LOCK:
        provider = _PROVIDER
        _REFRESHED += 1
    if provider is None:
        return _STORE.publish(ControlState())
    try:
        state = provider.snapshot()
    except Exception:
        logger.warning("operation profiling control source is unavailable", exc_info=True)
        state = ControlState()
    return _STORE.publish(state)


def reset_policy() -> None:
    """Clear the published configuration and the load cache. Tests call this."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None
    _STORE.clear()


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
    #: The experiment's own monotonic deadline, and the only thing that ends
    #: this measurement. There is no lease: a Stop stops *selecting* new
    #: operations, and an operation that already started runs to its own
    #: boundary and is delivered. What bounds a node that never hears the Stop
    #: is this instant, derived from the run's configured expiry.
    deadline_mono: float
    #: The configuration version this measurement was selected under. It travels
    #: with the selection so a child or a broker can check that the selection it
    #: was handed belongs to the configuration they hold, and so a record says
    #: which version of the switch produced it.
    control_revision: int = 0
    #: The immutable experiment this measurement belongs to. Copied at
    #: selection rather than re-read later, because "later" may be another
    #: experiment: an in-flight record, its manifest and its delivery must keep
    #: naming the run that selected them no matter what the deployment is
    #: configured to do now.
    run_operations: frozenset[str] = frozenset()
    run_target: Mapping[str, str] = field(default_factory=dict)
    run_max_operations: int = 0
    run_config_version: int = 1
    #: Which control source selected this measurement. A reader can then tell a
    #: legacy static-descriptor experiment from an administrator's, which is
    #: what makes "this old run has no control state" an answer rather than a
    #: guess.
    run_source: str = SOURCE_ENVIRONMENT
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
    #: The run's configured absolute expiry. This is the experiment's identity
    #: for storage, not a measurement of how long is left: a producer that
    #: derived it from its own clock would be stating a different experiment.
    run_expires_at: datetime | None = None
    #: Admission instances that actually enforced this root selection. A
    #: child inherits the parent's entry rather than claiming its own budget.
    run_admission_instances: tuple[Mapping[str, Any], ...] = ()
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

    def offset_us(self, mono_ns: int | None = None) -> int:
        """Return microseconds since this measurement started."""
        now = time.monotonic_ns() if mono_ns is None else mono_ns
        return (now - self.started_mono_ns) // 1000

    def past_deadline(self) -> bool:
        """Return whether the experiment has ended while this measurement is open.

        A measurement that outlives its run is closed at the deadline rather
        than at the moment somebody noticed, so a partial record never reports
        time the experiment was not collecting for.
        """
        if time.monotonic() > self.deadline_mono:
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


@dataclass(frozen=True)
class _Loaded:
    """Cached answer to "may this process collect, and for which run".

    ``mode`` is the deployment's control source rather than an experiment's
    switch, so it is answered even when there is no run -- that is what lets the
    Admin surface say "this deployment is managed by configuration" instead of
    the false "off" a boolean would produce.

    ``generation`` is the policy publication this answer was derived from. An
    admin-mode process re-reads on every load and compares one integer; every
    other process never re-reads at all.
    """

    run: ProfilingRun | None
    reason: str
    mode: str = MODE_DISABLED
    control: ControlState = field(default_factory=ControlState)
    generation: int = 0


_CACHE: _Loaded | None = None
#: Guards the derived read cache. Never held while parsing or logging.
_CACHE_LOCK = threading.Lock()
#: Guards the parse-once state below. Parsing happens outside it, because the
#: parse itself reads the environment and may log.
_MODE_LOCK = threading.Lock()
#: Parsed once per process: the deployment's mode, and the static descriptor it
#: names. Both are deployment configuration, which a restart is what changes.
_MODE: str | None = None
_INVALID_MODE = ""
_ENV_RUN: ProfilingRun | None = None
_ENV_RUN_READ = False
_BUDGET_LOCK = threading.Lock()


@dataclass
class _RunBudget:
    """One experiment's per-process root-operation allowance.

    Per run rather than per process. Two syncs of the *same* configuration must
    not hand back slots that experiment already spent, while an administrator's
    *next* experiment must start from a full allowance rather than inheriting
    the previous one's exhaustion.
    """

    max_operations: int
    used: int = 0


#: Keyed by run id and pruned when a run is dropped, so the table is bounded by
#: the experiments a process actually selected rather than by its uptime.
_BUDGETS: dict[str, _RunBudget] = {}


def _truthy(raw: str | None) -> bool:
    """Return whether an environment value means "on"."""
    return (raw or "").strip().lower() in _TRUTHY


def _parse_absolute_expiry(raw: str) -> datetime:
    """Return the configured absolute expiry of a run, normalized to UTC.

    A trailing ``Z`` is accepted because the design's run descriptors are
    written in UTC, and any other ISO-8601 offset is normalized to UTC so two
    spellings of one instant compare equal.

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
    moment = moment.astimezone(timezone.utc)
    if (moment - datetime.now(timezone.utc)).total_seconds() <= 0:
        raise ValueError(f"expires_at is not in the future: {text!r}")
    return moment


def _parse_expiry(raw: str) -> float:
    """Return the monotonic deadline for an ISO-8601 ``expires_at``.

    Derived from :func:`_parse_absolute_expiry`, so the configured instant is
    read in exactly one place.
    """
    moment = _parse_absolute_expiry(raw)
    return time.monotonic() + (moment - datetime.now(timezone.utc)).total_seconds()


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
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run_id must contain only letters, numbers, '.', '_', ':' or '-'")

    raw_operations = body.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError("run descriptor has no operations list")
    operations = frozenset(str(item).strip() for item in raw_operations if str(item).strip())
    if not operations:
        raise ValueError("run descriptor selects no operations")

    try:
        if isinstance(body.get("max_operations"), bool):
            raise TypeError
        max_operations = int(body["max_operations"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run descriptor has no valid max_operations") from exc
    if max_operations <= 0:
        raise ValueError("max_operations must be positive")

    try:
        if isinstance(body.get("config_version"), bool):
            raise TypeError
        config_version = int(body.get("config_version") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("config_version must be an integer") from exc
    if config_version <= 0:
        raise ValueError("config_version must be positive")

    raw_admissions = body.get("admission_instances") or []
    if not isinstance(raw_admissions, list):
        raise ValueError("admission_instances must be a list")
    admissions: list[Mapping[str, Any]] = []
    for raw_admission in raw_admissions:
        if not isinstance(raw_admission, Mapping):
            raise ValueError("admission_instances entries must be objects")
        instance = str(raw_admission.get("instance") or "").strip()
        if not instance:
            raise ValueError("admission_instances entries need an instance")
        raw_budget = raw_admission.get("max_operations")
        if (
            not isinstance(raw_budget, (str, int))
            or isinstance(raw_budget, bool)
            or not str(raw_budget).strip()
        ):
            raise ValueError("admission_instances entries need max_operations")
        try:
            budget = int(raw_budget)
        except ValueError as exc:
            raise ValueError("admission_instances entries need max_operations") from exc
        if budget <= 0:
            raise ValueError("admission instance max_operations must be positive")
        admissions.append({"instance": instance, "max_operations": budget})
    if not any(item["instance"] == PRODUCER_INSTANCE_ID for item in admissions):
        admissions.append({"instance": PRODUCER_INSTANCE_ID, "max_operations": max_operations})

    raw_target = body.get("target") or {}
    if not isinstance(raw_target, dict):
        raise ValueError("target must be a JSON object")
    unknown = sorted(str(key) for key in raw_target if str(key) not in TARGET_KEYS)
    if unknown:
        raise ValueError(f"target names unknown field(s): {', '.join(unknown)}")
    target: dict[str, str] = {}
    for key, value in raw_target.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            raise ValueError(f"target.{key} must be a non-empty string or integer")
        target[str(key)] = str(value)

    raw_expiry = str(body.get("expires_at") or "")
    return ProfilingRun(
        run_id=run_id,
        operations=operations,
        expires_at=raw_expiry.strip(),
        expires_at_utc=_parse_absolute_expiry(raw_expiry),
        deadline_mono=_parse_expiry(raw_expiry),
        max_operations=max_operations,
        target=target,
        config_version=config_version,
        admission_instances=tuple(admissions),
    )


def manifest_for_run(run: ProfilingRun | None) -> dict[str, Any] | None:
    """Return the immutable catalogue manifest for one parsed run."""
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "operations": sorted(run.operations),
        "target": dict(run.target),
        "expires_at": run.expires_at_utc.isoformat(),
        "config_version": run.config_version,
        "max_operations": run.max_operations,
        "admission_instances": [dict(item) for item in run.admission_instances],
    }


def manifest_for_measurement(measurement: Measurement | None) -> dict[str, Any] | None:
    """Return the run manifest this measurement was selected under.

    Built from the measurement's *own* copy of the experiment rather than from
    whatever run is configured now. That is what stops a delivery that happens
    after a new experiment started from describing the old record as a sample
    of the new one -- a mix-up the writer would reject as ``run_conflict`` and
    an operator would have to debug from two disagreeing manifests.
    """
    if measurement is None:
        return None
    if not measurement.run_id:
        return None
    manifest: dict[str, Any] = {
        "run_id": measurement.run_id,
        "operations": sorted(measurement.run_operations),
        "target": dict(measurement.run_target),
        "config_version": measurement.run_config_version,
        "max_operations": measurement.run_max_operations,
        "admission_instances": [dict(item) for item in measurement.run_admission_instances],
    }
    # The configured expiry travels with the record, so a measurement that was
    # handed over rather than selected here still states the experiment's
    # identity instead of deriving a different instant from its own clock.
    if measurement.run_expires_at is None:
        return None
    manifest["expires_at"] = measurement.run_expires_at.isoformat()
    return manifest


def profile_mode() -> str:
    """Return which control source this deployment allows.

    Read once per process. The value is deployment configuration: changing it is
    a redeploy by definition, and the whole point of the feature is that
    changing it is *not* how an experiment is started or stopped.

    An unrecognized value is reported as ``disabled`` with a warning rather
    than guessed at. "Refuse to collect until somebody fixes the spelling" is
    the safe reading of a typo; treating it as ``environment`` could silently
    enable collection from a stale descriptor.
    """
    global _MODE
    with _MODE_LOCK:
        if _MODE is None:
            _MODE = _read_mode()
        mode = _MODE
        invalid = _INVALID_MODE
    if invalid:
        # Reported outside the lock: a log handler that itself asked about
        # profiling would deadlock the process it is reporting on.
        logger.warning(
            "%s=%r is not a profiling mode (%s); profiling stays off",
            ENV_PROFILING_MODE,
            invalid,
            ", ".join(PROFILING_MODES),
        )
    return mode


def _read_mode() -> str:
    """Parse the mode out of the environment exactly once.

    Called with :data:`_MODE_LOCK` held, so it must not log: a logging handler
    that asked about profiling would otherwise deadlock. The refusal is
    therefore returned and reported by the caller.
    """
    raw = (os.environ.get(ENV_PROFILING_MODE) or "").strip().lower()
    if not raw:
        # Absent means the old semantics, unchanged: enabled + run descriptor,
        # which is what a deployment that never sets the new variable expects.
        return MODE_ENVIRONMENT if _truthy(os.environ.get(ENV_PROFILING_ENABLED)) else MODE_DISABLED
    global _INVALID_MODE
    if raw not in PROFILING_MODES:
        # Remembered rather than logged here: the caller reports it outside the
        # lock, because a log handler that itself asked about profiling would
        # deadlock the process it is reporting on.
        _INVALID_MODE = raw
        return MODE_DISABLED
    return raw


def _environment_run() -> ProfilingRun | None:
    """Return the run the static deployment descriptor names, if any.

    Parsed once per process, exactly as before this module grew a control
    source: the environment is deployment configuration and cannot change
    without a restart, so re-reading it would only add a JSON parse to every
    request that asks whether profiling is on.

    The descriptor is parsed *outside* the cache lock. Parsing reads the
    environment and logs, and holding a lock that the collector's read path also
    takes while doing either is how a warning handler that itself asks about
    profiling deadlocks the process it is reporting on.
    """
    global _ENV_RUN, _ENV_RUN_READ
    with _MODE_LOCK:
        if _ENV_RUN_READ:
            return _ENV_RUN
    run: ProfilingRun | None = None
    if _truthy(os.environ.get(ENV_PROFILING_ENABLED)):
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
            run = None
    with _MODE_LOCK:
        _ENV_RUN = run
        _ENV_RUN_READ = True
    return run


def _load() -> _Loaded:
    """Return the current control answer, re-deriving it when policy changed.

    Cached per *publication* rather than once per process. A process whose
    policy cannot change -- every static deployment, and every environment-mode
    process -- therefore parses its descriptor exactly once, while a process
    under Admin control answers from the snapshot the database just delivered.
    The generation is a single integer read, which is what keeps the off path
    as cheap as it was.
    """
    global _CACHE
    mode = profile_mode()
    if mode != MODE_ADMIN:
        with _CACHE_LOCK:
            if _CACHE is not None:
                return _CACHE
            run = _environment_run() if mode == MODE_ENVIRONMENT else None
            _CACHE = _Loaded(
                run,
                "active" if run is not None else "not_enabled",
                mode=mode,
            )
            return _CACHE

    published = _STORE.published()
    if published is None and policy_provider() is not None:
        # Nobody has read the control source in this process yet -- a route
        # answering before the background loop's first pass. Read it once, now,
        # rather than reporting "no control row" for a deployment that has one.
        with _STORE_LOCK:
            refreshed = _REFRESHED
        if refreshed == 0:
            refresh_policy()
            published = _STORE.published()
    generation = published.generation if published is not None else 0
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE.generation == generation:
            return _CACHE
    state = published.state if published is not None else ControlState(mode=MODE_ADMIN)
    run = published.run if published is not None else None
    loaded = _Loaded(
        run,
        "active" if run is not None else "not_enabled",
        mode=MODE_ADMIN,
        control=state,
    )
    with _CACHE_LOCK:
        _CACHE = replace(loaded, generation=generation)
        return _CACHE


def active_run() -> ProfilingRun | None:
    """Return the live run descriptor, or ``None`` when profiling is off.

    This answers "is an experiment in force", which is what a producer checks
    before deciding to look more closely. A Stop is observed here -- on the next
    selection -- and never as a withdrawal of work already selected.
    """
    return _load().run


def control_state() -> ControlState:
    """Return the control state this process is acting on.

    Bounded and non-secret: mode, control revision, run id and the authority
    instant. It is what a host reports back as its applied policy and what the
    Admin surface reads to tell "starting" from "collecting".
    """
    return _load().control


def reason() -> str:
    """Return ``not_enabled`` or ``active`` for startup diagnostics."""
    return _load().reason


def _claim_budget(run: ProfilingRun) -> bool:
    """Reserve one root-operation slot for this run, or refuse when used up."""
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(run.run_id)
        if budget is None:
            budget = _RunBudget(max_operations=run.max_operations)
            _BUDGETS[run.run_id] = budget
        if budget.max_operations <= budget.used:
            return False
        budget.used += 1
        return True


def _release_budget(run_id: str) -> None:
    """Return a reserved slot for a measurement that produced no record."""
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(run_id)
        if budget is not None and budget.used > 0:
            budget.used -= 1


def budget_state(run_id: str) -> dict[str, Any]:
    """Return this process's allowance and usage for one run.

    Reported to the Admin surface so "the experiment collected nothing" can be
    told apart from "this instance spent its share", which the design requires
    to be distinguishable. The scope is stated in the answer because the number
    is a per-producer-instance count and a page that presented it as a
    deployment-wide cap would be describing a quota nobody enforces.
    """
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(run_id)
        if budget is None:
            return {
                "scope": "producer_instance",
                "instance": PRODUCER_INSTANCE_ID,
                "max_operations": None,
                "used": 0,
                "exhausted": False,
            }
        return {
            "scope": "producer_instance",
            "instance": PRODUCER_INSTANCE_ID,
            "max_operations": budget.max_operations,
            "used": budget.used,
            "exhausted": budget.used >= budget.max_operations,
        }


def _recording(measurement: Measurement) -> bool:
    """Return whether a measurement may still collect.

    Expiry is enforced here rather than only when a measurement starts, so a run
    that lapses mid-attempt stops adding to the record instead of reporting the
    whole attempt as a complete sample.
    """
    if measurement.finished or measurement.frozen:
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
    producer: str = DEFAULT_PRODUCER,
    coverage_scope: str = "",
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
        control_revision=_load().control.config_version,
        run_operations=run.operations,
        run_target=dict(run.target),
        run_max_operations=run.max_operations,
        run_config_version=run.config_version,
        run_source=run.source,
        producer=producer,
        coverage_scope=coverage_scope or COVERAGE_BY_OPERATION.get(operation, ""),
        run_expires_at=run.expires_at_utc,
        run_admission_instances=run.admission_instances,
        facts={"operation": operation, "request_kind": operation},
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

    Returns ``False`` when the measurement is absent/finalized or the scope
    does not match. A claim that crossed the deadline is still bound and later
    emitted as partial; erasing it would hide the slow claim being measured.
    """
    if measurement is None or measurement.finished or measurement.frozen:
        return False
    if measurement.past_deadline():
        # The claim happened while the experiment was live. Preserve its
        # attribution so it can close as a partial sample at the deadline;
        # merely noticing expiry after the claim is not grounds to erase it.
        measurement.expired = True
    for key, value in facts.items():
        measurement.facts[key] = _bounded(value)
    measurement.bound = True
    # The measurement's own scope, not the deployment's current one: a record
    # handed over from a parent must be judged by the experiment that selected
    # it, and re-reading here would let a later experiment widen the filter.
    for key, wanted in measurement.run_target.items():
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
    measurement.answered_by.add(producer)
    resolved = str(response.get("profiling_operation") or "").strip()
    if resolved:
        run = _load().run
        if run is not None and resolved in run.operations:
            # The producer named the operation its result turned out to be.
            # Only a name this run selects is adopted; anything else keeps the
            # operation the caller filed the measurement under.
            measurement.operation = resolved
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
    else's measurement, such as a broker timing its own work under a selection
    the control plane rooted. It changes nothing about who owns the record:
    ``measurement.producer`` stays the producer that rooted the operation.
    """
    if measurement is None or measurement.finished:
        return None
    return _MeasurementObserver(measurement, producer=producer)


def measurement_context(
    measurement: Measurement | None, call_id: str | None = None
) -> dict[str, Any] | None:
    """Return the selection a downstream producer needs to inherit.

    The runner decides whether an operation is selected; a broker cannot make
    that decision for itself, because it sees one call at a time and does not
    know which attempt it belongs to. So the selected measurement travels with
    the request: run id, operation, and an absolute wall-clock expiry, since
    two processes share no monotonic clock.

    The expiry is the experiment's own end. There is no shorter lease to hand
    over, so a process that inherits this selection may keep collecting until
    the experiment was always going to end -- which is exactly what a Stop
    means here: stop selecting new operations, and let the ones already running
    finish under the configuration that selected them.

    The immutable manifest travels too, so the inheriting process binds to the
    experiment that selected this measurement rather than to whatever is
    configured where it happens to be running.
    """
    if measurement is None or measurement.finished or measurement.past_deadline():
        return None
    deadline = measurement.deadline_mono
    context: dict[str, Any] = {
        "run_id": measurement.run_id,
        "measurement_id": measurement.measurement_id,
        "operation": measurement.operation,
        "producer": measurement.producer,
        "producer_instance_id": measurement.producer_instance_id,
        "clock_domain_id": measurement.clock_domain_id,
        "coverage_scope": measurement.coverage_scope,
        "start_boundary": BOUNDARIES_BY_OPERATION.get(
            measurement.operation, ("unknown", "unknown")
        )[0],
        "end_boundary": BOUNDARIES_BY_OPERATION.get(measurement.operation, ("unknown", "unknown"))[
            1
        ],
        # The facts travel so a process that continues this selection reports
        # the same scope its parent proved. A child that re-derived them would
        # be stating attribution it never authenticated.
        "facts": dict(measurement.facts),
        "call_id": call_id or "",
        "expires_at": time.time() + max(0.0, deadline - time.monotonic()),
        # The parent's own start, in nanoseconds. A child on the same host
        # shares this monotonic clock, so a turn it runs can be timed from the
        # claim rather than from whenever the child happened to start --
        # otherwise every sample silently loses the queueing it exists to show.
        "started_mono_ns": measurement.started_mono_ns,
        # The candidate operations this caller says are in play. Deliberately
        # *not* the experiment's manifest: a caller may narrow the candidates it
        # offers, and a broker must never let that claim override what its own
        # configuration selects. The manifest is the nested object below.
        "operations": sorted(measurement.run_operations),
        # The immutable experiment, as one object. Its presence is what tells a
        # downstream process that this selection was already proved by a
        # producer that held the configuration, which is the only thing that
        # lets a continuation outlive a Disable.
        "run": _context_run(measurement),
    }
    return context


def _context_run(measurement: Measurement) -> dict[str, Any]:
    """Return the immutable experiment a selection was made under."""
    run: dict[str, Any] = {
        "run_id": measurement.run_id,
        "operations": sorted(measurement.run_operations),
        "target": dict(measurement.run_target),
        "config_version": measurement.run_config_version,
        "max_operations": measurement.run_max_operations,
        "control_revision": measurement.control_revision,
        # Which control source rooted the experiment. It travels so a process
        # that only continues the selection -- a child, a broker -- reports the
        # same provenance its parent proved instead of inferring one.
        "source": measurement.run_source,
    }
    if measurement.run_expires_at is not None:
        run["expires_at"] = measurement.run_expires_at.isoformat()
    return run


def handoff_context(measurement: Measurement | None) -> dict[str, Any] | None:
    """Return the richer same-host context used for a runner child process."""
    context = measurement_context(measurement)
    if context is None or measurement is None:
        return None
    context["admission_instances"] = [dict(item) for item in measurement.run_admission_instances]
    context["inherited_spans"] = [span.to_payload() for span in measurement.spans]
    context["inherited_marks"] = [
        {"name": name, "offset_us": offset} for name, offset in measurement.marks
    ]
    return context


def _restore_handoff_spans(value: Any) -> list[_SpanRecord] | None:
    """Validate and rebuild the bounded spans a parent already completed."""
    if not isinstance(value, list) or len(value) > MAX_SPANS:
        return None
    restored: list[_SpanRecord] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        try:
            span_id = str(item["span_id"])
            name = str(item["name"])
            ordinal = int(item["ordinal"])
            start = int(item["start_offset_us"])
            duration = int(item["duration_us"])
        except (KeyError, TypeError, ValueError):
            return None
        if not span_id or not name or min(ordinal, start, duration) < 0:
            return None
        restored.append(
            _SpanRecord(
                span_id=span_id,
                name=name,
                ordinal=ordinal,
                start_offset_us=start,
                parent_span_id=(
                    str(item["parent_span_id"]) if item.get("parent_span_id") else None
                ),
                duration_us=duration,
                outcome=str(item.get("outcome") or "ok"),
                truncated=bool(item.get("truncated")),
                attributes=(
                    dict(item["attributes"]) if isinstance(item.get("attributes"), Mapping) else {}
                ),
            )
        )
    return restored


def _restore_handoff_marks(value: Any) -> list[tuple[str, int]] | None:
    """Validate the bounded milestones a parent already recorded."""
    if not isinstance(value, list) or len(value) > MAX_SPANS:
        return None
    restored: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        name = str(item.get("name") or "")
        offset = item.get("offset_us")
        if not name or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None
        restored.append((name, offset))
    return restored


def _inherited_manifest(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the immutable experiment a context states, if it states one.

    Bounded and validated like every other value that crosses a process
    boundary. ``None`` means the context did not state one, which is the case
    for a caller written before this field existed -- and for a caller that
    offers candidates without holding the experiment.

    A context that states a *malformed* manifest is refused outright rather than
    read leniently: half a manifest is how an attempt gets filed under an
    experiment it does not belong to.
    """
    raw = context.get("run")
    if not isinstance(raw, Mapping):
        return None
    run_id = str(raw.get("run_id") or "").strip()
    if not run_id or len(run_id) > MAX_RUN_ID_CHARS or RUN_ID_PATTERN.fullmatch(run_id) is None:
        return None
    raw_operations = raw.get("operations")
    if not isinstance(raw_operations, (list, tuple, set, frozenset)):
        return None
    operations = frozenset(str(item).strip() for item in raw_operations if str(item).strip())
    if not operations:
        return None
    raw_target = raw.get("target")
    target: dict[str, str] = {}
    if isinstance(raw_target, Mapping):
        for key, value in raw_target.items():
            if str(key) not in TARGET_KEYS:
                return None
            target[str(key)] = str(value)
    try:
        config_version = int(raw.get("config_version") or 1)
        max_operations = int(raw.get("max_operations") or 0)
    except (TypeError, ValueError):
        return None
    if config_version <= 0 or max_operations < 0:
        return None
    expires_at = _parse_context_instant(raw.get("expires_at"))
    if expires_at is None:
        return None
    try:
        control_revision = max(0, int(raw.get("control_revision") or 0))
    except (TypeError, ValueError):
        control_revision = 0
    return {
        "run_id": run_id,
        "operations": operations,
        "target": target,
        "config_version": config_version,
        "max_operations": max_operations,
        "expires_at": expires_at,
        "control_revision": control_revision,
        "source": str(raw.get("source") or SOURCE_ADMIN),
    }


def _manifest_for_continuation(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the manifest that lets this process continue a selection.

    The continuation arm exists because a selection may outlive the
    configuration that made it -- a child starting after a Sync, a broker
    answering a stage of an attempt that was chosen before a Disable. It is
    available **only under Admin control**, which is where "the configuration
    may change while work is in flight" is the whole point.

    ``disabled`` is a different statement. It means this deployment forbids
    collection outright, so a process configured that way must not be talked
    into collecting by an upstream context, however well formed -- otherwise a
    staged rollout or a deliberately silenced producer could be made to allocate
    recorders and measurements by whoever holds the broker token. The default
    for a deployment that configures nothing is ``disabled``, so this is also
    what keeps default-off meaning off.

    ``environment`` follows its own static descriptor, and it is the same
    descriptor in parent and child (the environment is inherited), so the
    ordinary ``_load()`` path already covers it and the manifest arm is not
    needed.
    """
    if profile_mode() != MODE_ADMIN:
        return None
    return _inherited_manifest(context)


def _manifest_deadline(manifest: Mapping[str, Any]) -> float | None:
    """Return an inherited experiment's own end as a local monotonic deadline.

    ``None`` means the experiment is already over. The manifest states an
    absolute instant, so it is converted here -- once, against this process's
    monotonic clock -- exactly as a locally read descriptor is. A context can
    outlive the experiment it names, and the earlier of the two always wins: a
    selection never buys time by travelling.
    """
    expires_at = manifest.get("expires_at")
    if not isinstance(expires_at, datetime):
        return None
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return None
    return time.monotonic() + remaining


def _parse_context_instant(value: Any) -> datetime | None:
    """Parse an ISO-8601 instant from an untrusted context, if it is one."""
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _context_control_revision(context: Mapping[str, Any]) -> int:
    """Return the configuration version an inherited selection was made under.

    Stated in the record rather than used to decide whether to collect: the
    selection already carries the manifest it belongs to, and this number is
    only how a reader tells which version of the switch produced a sample. A
    context that does not state one is a caller written before the field
    existed, and reads as version 0.
    """
    try:
        return max(0, int(context.get("control_revision") or 0))
    except (TypeError, ValueError):
        return 0


def _context_expiry(context: Mapping[str, Any]) -> datetime | None:
    """Return the configured expiry an inherited context states, if it parses.

    Distinct from the context's own ``expires_at``, which is the instant the
    run ends by this process's clock. This is the experiment's identity -- the
    instant the administrator asked for -- and a record that stated the local
    deadline instead would describe a different experiment on every hop.
    """
    raw = str(context.get("run_expires_at") or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def attach_selection(context: Any, *, producer: str = DEFAULT_PRODUCER) -> Measurement | None:
    """Continue measuring a selection another process already made.

    This is for the one case where a root operation outlives the process that
    selected it: the host service claims a turn, then starts a child to run it,
    and everything worth measuring happens in the child. The claim decided
    *whether* to measure and paid for the slot; the child only times.

    It therefore does **not** claim a budget slot -- the selection carries the
    slot its parent already holds -- and it does **not** take a fresh run
    reading, so a run that lapsed between the claim and the child's start is
    refused rather than quietly re-sampled. Both of those are why this is not
    simply :func:`start_operation`: called there, the launch would spend a
    second slot for one attempt and could extend an experiment past its
    deadline.

    The inherited deadline can only shorten the local one, exactly as it does
    for a downstream producer.

    **The context's own manifest is what this process binds to**, and it does
    not require the child to have selected the experiment for itself. That is
    not a relaxation: the deployment path runs the child in a *new Python
    process*, which under Admin control has no experiment of its own -- its
    policy store is empty until the sync loop fills it, and it never reads a
    static descriptor. Requiring it to agree with a local run made the handoff
    work only in the process that made it, so a real turn child silently
    collected nothing.

    What makes this safe is not a local re-check but the channel: the context
    arrives over the parent's own pipe, produced by a process that had already
    proved the selection against its own configuration. The child restates that
    proof rather than repeating it, and three things are still enforced here --
    the experiment has not expired, its operation list is well formed, and the
    child is not given a budget slot (the parent holds it).
    """
    if not isinstance(context, Mapping):
        return None
    manifest = _manifest_for_continuation(context)
    local = _load().run
    if manifest is not None:
        # A child of a selection that states its experiment. The manifest is
        # that experiment's identity, so it is what this process reports --
        # there is no local configuration to consult, because a new process
        # under Admin control has none until its own sync loop runs.
        run_id = manifest["run_id"]
        allowed_operations = manifest["operations"]
        source = manifest["source"]
        run_config_version = manifest["config_version"]
        run_target = dict(manifest["target"])
        run_max_operations = manifest["max_operations"]
        run_admission_instances: tuple[Mapping[str, Any], ...] = ()
        run_expires_at: datetime | None = manifest["expires_at"]
    elif local is not None:
        # A caller written before the manifest travelled -- and the environment
        # path, where the descriptor really is this process's own
        # configuration. Nothing has been handed over, so the local run decides.
        run_id = str(context.get("run_id") or "")
        if run_id != local.run_id:
            return None
        allowed_operations = local.operations
        source = local.source
        run_config_version = local.config_version
        run_target = dict(local.target)
        run_max_operations = local.max_operations
        run_admission_instances = local.admission_instances
        run_expires_at = _context_expiry(context) or local.expires_at_utc
    else:
        return None
    operation = str(context.get("operation") or "").strip()
    if not operation or operation not in allowed_operations:
        return None
    inherited = _context_deadline(context)
    if inherited is None:
        return None
    # Every ceiling applies and the earliest wins. The transport's own bound,
    # the experiment's *configured* end from the manifest, and -- when this
    # process happens to hold the same experiment -- its local deadline. A
    # context whose stated bound is later than the experiment it names cannot
    # extend that experiment, and one that names an experiment already over is
    # refused outright rather than given the transport's remaining time.
    deadline = inherited
    if manifest is not None:
        manifest_deadline = _manifest_deadline(manifest)
        if manifest_deadline is None:
            return None
        deadline = min(deadline, manifest_deadline)
    if local is not None and local.run_id == run_id:
        deadline = min(deadline, local.deadline_mono)
    if time.monotonic() > deadline:
        return None
    started = context.get("started_mono_ns")
    try:
        started_mono_ns = int(started) if started is not None else time.monotonic_ns()
    except (TypeError, ValueError):
        started_mono_ns = time.monotonic_ns()
    admissions = context.get("admission_instances")
    inherited_admissions: tuple[Mapping[str, Any], ...] = ()
    if admissions is not None:
        if not isinstance(admissions, list) or len(admissions) > MAX_REMOTE_STAGES:
            return None
        parsed_admissions: list[Mapping[str, Any]] = []
        for item in admissions:
            if not isinstance(item, Mapping):
                return None
            instance = str(item.get("instance") or "")
            budget = item.get("max_operations")
            if (
                not instance
                or not isinstance(budget, int)
                or isinstance(budget, bool)
                or budget <= 0
            ):
                return None
            parsed_admissions.append({"instance": instance, "max_operations": budget})
        inherited_admissions = tuple(parsed_admissions)
    spans = _restore_handoff_spans(context.get("inherited_spans", []))
    marks = _restore_handoff_marks(context.get("inherited_marks", []))
    if spans is None or marks is None:
        return None
    # The configured expiry travels with the record, so a manifest rebuilt in
    # this process states the experiment's identity rather than an instant
    # re-derived from this process's own clock. A context from an Admin
    # experiment that omits it cannot be filed under a run, so it is refused
    # rather than given a deadline this process invented.
    if run_expires_at is None:
        return None
    measurement = Measurement(
        operation=operation,
        run_id=run_id,
        # The same id the parent minted, so the child's record and the parent's
        # selection are one measurement rather than two that look related.
        measurement_id=str(context.get("measurement_id") or f"msm_{uuid.uuid4().hex[:16]}"),
        # The parent's start when it was handed over -- the claim it already
        # paid for is part of the startup a user waits through.
        started_mono_ns=started_mono_ns,
        deadline_mono=deadline,
        control_revision=(
            manifest["control_revision"]
            if manifest is not None
            else _context_control_revision(context)
        ),
        run_operations=allowed_operations,
        run_target=run_target,
        run_max_operations=run_max_operations,
        run_config_version=run_config_version,
        run_source=source,
        producer=str(context.get("producer") or producer),
        producer_instance_id=str(context.get("producer_instance_id") or PRODUCER_INSTANCE_ID),
        clock_domain_id=str(context.get("clock_domain_id") or CLOCK_DOMAIN_ID),
        coverage_scope=str(
            context.get("coverage_scope") or COVERAGE_BY_OPERATION.get(operation, "")
        ),
        run_expires_at=run_expires_at,
        run_admission_instances=inherited_admissions or run_admission_instances,
        facts={"operation": operation, "request_kind": operation},
        spans=spans,
        marks=marks,
        # The slot belongs to the parent, so discarding this must not return
        # one this process never took.
        reserved=False,
    )
    for key, value in (context.get("facts") or {}).items():
        measurement.facts[str(key)] = _bounded(value)
    measurement.span_counter = len(spans)
    for span in spans:
        measurement.ordinals[span.name] = max(measurement.ordinals.get(span.name, 0), span.ordinal)
    measurement.bound = True
    return measurement


def _candidate_operations(context: Mapping[str, Any]) -> frozenset[str]:
    """Return the operations a producer may file this call under.

    A file path is a candidate over listing and opening until the read says
    which it was, so the context may name more than one. A context that names
    only ``operation`` -- every caller written before candidates existed -- is
    the single-operation case, and an empty set means "not stated".
    """
    raw = context.get("operations")
    if isinstance(raw, (list, tuple, set, frozenset)):
        named = frozenset(
            str(item.get("operation") if isinstance(item, Mapping) else item).strip()
            for item in raw
        )
        named = frozenset(item for item in named if item)
        if named:
            return named
    single = str(context.get("operation") or "").strip()
    return frozenset({single}) if single else frozenset()


def start_stage_recording(
    context: Any, *, producer: str = DEFAULT_PRODUCER
) -> StageRecorder | None:
    """Return a recorder when this process should record for that selection.

    Recording requires an inherited selection, never just a local switch. A
    broker sees one call at a time and cannot tell which attempt it belongs to,
    so on its own it cannot know whether the operation was selected -- and a
    broker that kept measuring after the configuration moved on would report
    time the experiment was not collecting for. ``None`` therefore means "not
    selected" and records nothing.

    **Two things can make a selection valid, and they are not the same thing.**

    * A context that names an operation and no manifest is a *new* selection, and
      the only way to honour it is for this process's configuration to name that
      run and select that operation. This is the path a producer that read the
      configuration for itself takes.
    * A context that carries the immutable manifest of an experiment is a
      *continuation*: the operation was chosen before an administrator switched
      collection off (or moved to another experiment), and its later stages
      still belong to it. Refusing those is what made a root operation that was
      allowed to run to completion deliver an incomplete record -- the record
      was kept, but the broker's stages went missing, which is worse than
      either.

    The continuation arm is safe because of the channel rather than a local
    re-check: a manifest-bearing context is produced by a control-plane or
    runner process that had already proved the selection, it reaches the broker
    over the broker's own shared-token channel, and the experiment it names must
    still be unexpired. A caller that cannot reach that channel cannot assert
    one.

    The inherited deadline can only ever shorten the local one -- the recorder
    takes the earlier of the two -- so a caller cannot extend an experiment past
    what this process was configured to run.
    """
    if not isinstance(context, Mapping):
        return None
    inherited = _context_deadline(context)
    if inherited is None:
        return None
    local = _load().run
    run_id = str(context.get("run_id") or "")
    manifest = _manifest_for_continuation(context)
    if manifest is not None:
        run_id = manifest["run_id"]
        candidates = _candidate_operations(context)
        allowed = (
            frozenset(name for name in candidates if name in manifest["operations"])
            if candidates
            else manifest["operations"]
        )
        # The experiment's configured end is a ceiling of its own: a context
        # that states a later bound cannot extend it, and one that names an
        # experiment already over is refused.
        manifest_deadline = _manifest_deadline(manifest)
        if manifest_deadline is None:
            return None
        deadline = min(inherited, manifest_deadline)
        if local is not None and local.run_id == run_id:
            # Still the experiment in force: its own end applies too.
            deadline = min(deadline, local.deadline_mono)
        elif local is not None:
            # The deployment moved on. The continuation is honoured anyway --
            # that is the point -- bounded by its own stated end.
            logger.debug("operation profiling continues a selection from a previous configuration")
    elif local is not None and run_id == local.run_id:
        allowed = _resolved_operations(local, context)
        deadline = min(local.deadline_mono, inherited)
    else:
        return None
    if not allowed:
        return None
    if time.monotonic() > deadline:
        return None
    # One candidate is already decided; several are narrowed after the call.
    settled = next(iter(allowed)) if len(allowed) == 1 else ""
    return StageRecorder(
        deadline_mono=deadline,
        producer=producer,
        root_stage=settled or DEFAULT_ROOT_STAGE,
        operation=settled,
        allowed=allowed,
    )


def _resolved_operations(run: ProfilingRun, context: Mapping[str, Any]) -> frozenset[str]:
    """Return the operations this context may collect under.

    Empty means "do not collect": the context names operations, but this run
    selects none of them. A context that names no operation at all is a caller
    that measures whatever the run does, and is reported as the empty-string
    operation so it can still be recorded.

    More than one result is not a refusal. A file path is collected before its
    kind is known, so several candidates are exactly the case where the
    producer learns the answer and narrows it afterwards with
    :meth:`StageRecorder.set_operation`.
    """
    candidates = _candidate_operations(context)
    if not candidates:
        return frozenset({""})
    return frozenset(name for name in candidates if name in run.operations)


def _context_deadline(context: Mapping[str, Any]) -> float | None:
    """Convert a caller's wall-clock expiry into this process's monotonic one."""
    try:
        expires_at = float(str(context.get("expires_at")))
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


def _bounded_context_str(value: Any, limit: int = MAX_ATTRIBUTE_CHARS) -> str:
    """Return a bounded string from an untrusted context value."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()[:limit]


def parse_context(raw: Any) -> dict[str, Any] | None:
    """Decode the profiling header one producer inherited from another.

    The value crosses a process boundary and is not trusted: it is bounded,
    parsed defensively, and reduced to the fields the contract names. Every
    failure returns ``None`` -- measurement metadata must never turn a malformed
    experiment setting into a failed file read or terminal keystroke.
    """
    if isinstance(raw, Mapping):
        body = dict(raw)
    else:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text or len(text) > MAX_CONTEXT_CHARS:
            return None
        try:
            body = json.loads(text)
        except (TypeError, ValueError):
            return None
    if not isinstance(body, dict):
        return None
    try:
        expires_at = float(str(body.get("expires_at")))
    except (TypeError, ValueError):
        return None
    parsed = {
        "run_id": _bounded_context_str(body.get("run_id"), MAX_RUN_ID_CHARS),
        "measurement_id": _bounded_context_str(body.get("measurement_id")),
        "operation": _bounded_context_str(body.get("operation")),
        "call_id": _bounded_context_str(body.get("call_id")),
        "expires_at": expires_at,
    }
    operations = body.get("operations")
    if isinstance(operations, list):
        parsed["operations"] = [
            _bounded_context_str(item) for item in operations[:8] if _bounded_context_str(item)
        ]
    # The immutable experiment travels as one nested object, and it is carried
    # through the header boundary intact: a broker that received a selection it
    # cannot identify could only refuse it. ``_inherited_manifest`` is what
    # decides whether the object is usable, so this only bounds its size.
    run = body.get("run")
    if isinstance(run, Mapping):
        parsed["run"] = _bounded_context_run(run)
    return parsed


def _bounded_context_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded, deliberately unvalidated form of an inherited run.

    Unvalidated here on purpose: two different readers want different things
    from it -- a broker needs the operation list and the deadline, a child needs
    the whole identity -- and each validates what it uses. This bounds the
    values so a hostile or truncated value cannot become an unbounded record.
    """
    bounded: dict[str, Any] = {
        "run_id": _bounded_context_str(run.get("run_id"), MAX_RUN_ID_CHARS),
        "config_version": run.get("config_version"),
        "max_operations": run.get("max_operations"),
        "control_revision": run.get("control_revision"),
        "source": _bounded_context_str(run.get("source")),
        "expires_at": _bounded_context_str(run.get("expires_at")),
    }
    operations = run.get("operations")
    if isinstance(operations, list):
        bounded["operations"] = [
            _bounded_context_str(item) for item in operations[:MAX_REMOTE_STAGES]
        ]
    target = run.get("target")
    if isinstance(target, Mapping):
        bounded["target"] = {
            _bounded_context_str(key): _bounded_context_str(value)
            for key, value in list(target.items())[:8]
        }
    return bounded


def context_call_id(context: Any) -> str | None:
    """Return the call id a request carried, if it carried one."""
    if not isinstance(context, Mapping):
        return None
    value = str(context.get("call_id") or "").strip()
    return value or None


def answer_from_headers(headers: Any) -> dict[str, Any] | None:
    """Read a streamed call's answer out of response headers.

    Returns ``None`` when the response does not answer a call, so a caller can
    leave its fragment unanswered and report it as missing rather than inventing
    an answer from a response that never carried one.
    """
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    call_id = str(getter(PROFILING_ANSWER_CALL_ID_HEADER) or "").strip()
    if not call_id or str(getter(PROFILING_ANSWER_RECORDED_HEADER) or "") not in {"1", "true"}:
        return None
    answer: dict[str, Any] = {"profiling_call_id": call_id, "profiling_recorded": True}
    operation = str(getter(PROFILING_ANSWER_OPERATION_HEADER) or "").strip()
    if operation:
        answer["profiling_operation"] = operation
    if str(getter(PROFILING_ANSWER_TRUNCATED_HEADER) or "") in {"1", "true"}:
        answer["profiling_truncated"] = True
    raw = str(getter(PROFILING_ANSWER_STAGES_HEADER) or "").strip()
    if raw:
        try:
            stages = json.loads(raw)
        except (TypeError, ValueError):
            stages = None
        if isinstance(stages, list):
            answer["stages"] = stages
    return answer


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
            duration = int(str(item.get("duration_us")))
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
    measurement.contributors.add(source)
    measurement.answered_by.add(source)
    if any(entry.get("truncated") for entry in entries):
        measurement.truncated = True
    return True


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
    the broker reports its durations: ``{"name", "duration_us", "outcome",
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

    A measurement that placed no mark still gets one stage covering its whole
    span, named for its own operation. Sub-stage timing attaches to a stage, so
    without this the work a producer timed would have nowhere to land and would
    be dropped without a trace -- and a record that collected nothing would be
    indistinguishable from one whose timings went missing.
    """
    marks = measurement.marks
    if not marks:
        marks = [(measurement.operation or "operation", 0)]
    ended = measurement.ended_offset_us
    if ended is None:
        ended = measurement.offset_us()
    following = [offset for _, offset in marks[1:]] + [ended]
    stages: list[dict[str, Any]] = []
    for (name, start), stop in zip(marks, following, strict=False):
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


def frozen_record(
    measurement: Measurement | None, outcome: str, failure_stage: str | None = None
) -> dict[str, Any] | None:
    """Return the record this measurement would emit, without emitting it.

    For a root that persists in-process rather than delivering: it needs the
    same bytes the exporter would write, and building a second version of them
    beside the exporter is how two descriptions of one measurement start to
    disagree. Returns ``None`` when nothing would be emitted, so a caller can
    treat "not selected" and "nothing to store" the same way.
    """
    if measurement is None or not measurement.selected:
        return None
    # The boundary has to be frozen first, or this would report a completeness
    # that ``finish`` is about to re-decide from a later clock.
    finalize(measurement, outcome)
    frozen_outcome = measurement.frozen_outcome
    if frozen_outcome not in _OUTCOMES:  # pragma: no cover - guarded by callers
        frozen_outcome = "incomplete"
    if measurement.run_target and not measurement.bound:
        return None
    if measurement.frozen_payload is None:
        measurement.frozen_failure_stage = failure_stage
        measurement.facts["outcome"] = frozen_outcome
        if failure_stage:
            measurement.facts["failure_stage"] = failure_stage
        if measurement.expired:
            # The only way collection ends mid-flight is the experiment's own
            # deadline; a Stop never truncates work that already started, so
            # there is exactly one answer to state here.
            measurement.facts["profiling_stopped"] = STOP_REASON_EXPIRED
        measurement.frozen_payload = to_record(
            measurement,
            outcome=frozen_outcome,
            failure_stage=failure_stage,
        )
    return measurement.frozen_payload


def finalize(measurement: Measurement | None, outcome: str) -> None:
    """Freeze this measurement's end boundary and completeness, once.

    Called once, at the end of the work being measured and before any response
    is built. Everything that reports on the sample afterwards -- the response
    metadata, the log line, the JSONL file, the persisted row -- reads these
    frozen values, so none of them can disagree with another about the same
    measurement.

    Freezing here rather than at export is what closes the race the review
    found: a response built at 100 could claim completeness while ``finish`` at
    102 re-read the clock, saw the deadline pass, and emitted the same
    measurement as partial. Encoding a response takes real time, and that time
    is not collection -- so the boundary is the end of collection, and the
    serialization that follows is deliberately outside it.

    Idempotent, and safe on ``None``.
    """
    if measurement is None or measurement.frozen:
        return
    measurement.frozen_outcome = outcome
    measurement.expired = measurement.expired or measurement.past_deadline()
    ended = measurement.offset_us()
    if measurement.expired:
        # Collecting stopped at the deadline, so the record ends there too.
        ended = min(ended, max(0, measurement.deadline_offset_us()))
    measurement.ended_offset_us = ended
    # Computed *before* the flag is set: ``collection_complete`` reads the
    # freeze when there is one, so marking first would have it read the answer
    # it is being asked to produce -- which is always False.
    complete = collection_complete(measurement, outcome)
    measurement.frozen = True
    measurement.frozen_complete = complete


def settle(measurement: Measurement | None) -> None:
    """Deprecated alias for :func:`finalize`, kept for callers mid-migration.

    Prefer ``finalize(measurement, outcome)``: it also freezes the outcome and
    the completeness, which is what stops a later export from re-deciding them.
    """
    if measurement is None or measurement.frozen:
        return
    finalize(measurement, measurement.frozen_outcome or "incomplete")


def collection_complete(measurement: Measurement | None, outcome: str) -> bool:
    """Return whether this local record collected everything it set out to.

    Not the same as the operation succeeding: a failed startup can be a
    complete sample of a failure. A record is incomplete when a boundary never
    closed, a fragment it owed never answered, a span was dropped, or the
    deadline cut collection short. Computed from the same fields ``to_record``
    reports, so a response cannot disagree with the record it describes.
    """
    if measurement is None:
        return False
    if measurement.frozen:
        # The boundary was closed at a stated moment. Recomputing from a later
        # clock is exactly the disagreement this freeze exists to prevent.
        return measurement.frozen_complete
    missing = any(
        (producer, call_id) not in measurement.answered_fragments
        for producer, call_id, _stage in measurement.expected_fragments
    )
    return not (
        outcome == "incomplete"
        or measurement.dropped_spans
        or measurement.expired
        or measurement.truncated
        or missing
    )


def fragment_truncated(measurement: Measurement | None, calls: Iterable[Any]) -> bool:
    """Return whether any of these calls answered but was cut short.

    A fragment can be answered and still be a floor rather than a duration,
    and a reader has to be able to tell that from the answer alone.
    """
    if measurement is None:
        return False
    if measurement.truncated_fragments:
        wanted = {str(call) for call in calls}
        return any(call_id in wanted for _producer, call_id in measurement.truncated_fragments)
    return False


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
    root = measurement.producer or DEFAULT_PRODUCER
    # Who owns this record, then who answered a call on it, then whose timings
    # actually reached the timeline. The root producer leads and is never
    # dropped: a record whose only timing came from downstream still has to say
    # who owned the operation.
    contributors = list(
        dict.fromkeys([root, *sorted(measurement.answered_by | measurement.contributors | {root})])
    )
    sources = list(dict.fromkeys([root, *sorted(measurement.contributors | {root})]))
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "measurement_id": measurement.measurement_id,
        "profiling_run_id": measurement.run_id,
        "operation": measurement.operation,
        "boundary_version": BOUNDARY_VERSION,
        "coverage_scope": measurement.coverage_scope,
        "start_boundary": BOUNDARIES_BY_OPERATION.get(
            measurement.operation, ("unknown", "unknown")
        )[0],
        "end_boundary": BOUNDARIES_BY_OPERATION.get(measurement.operation, ("unknown", "unknown"))[
            1
        ],
        # A server-side record never covers the user's own wait. It becomes true
        # only when a browser interaction is joined to it, which this slice does
        # not do -- so it is stated, not left to a reader to infer.
        "end_to_end_complete": False,
        "producer": root,
        "producer_instance_id": measurement.producer_instance_id,
        "clock_domain_id": measurement.clock_domain_id,
        # Who actually contributed to this record. A run needs the broker's
        # switch as well as the runner's for the container sub-stages, and a
        # missing producer is reported here rather than silently absent.
        "producers": contributors,
        # The same list again, minus anyone whose durations never reached a
        # stage: a reader asking "who timed this" gets an answer it can trust.
        "timing_sources": sources,
        "outcome": outcome,
        "duration_us": ended,
        "collection_complete": collection_complete(measurement, outcome),
        # The v2 name for the same judgement, kept while readers migrate.
        "measurement_complete": collection_complete(measurement, outcome),
        # The outcome and completeness that were frozen with the end boundary.
        # Stated explicitly because they are what every other report of this
        # measurement reads, and a reader has to be able to see that they are
        # one decision rather than two.
        "boundary_frozen": measurement.frozen,
        "dropped_spans": measurement.dropped_spans,
        # Completion order is not timeline order: a nested span closes first.
        "spans": [
            item.to_payload()
            for item in sorted(measurement.spans, key=lambda span: span.start_offset_us)
        ],
        "milestones": [
            {"name": name, "offset_us": offset, "source": root}
            for name, offset in measurement.marks
        ],
        "stages": _stages(measurement),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "facts": measurement.facts,
    }
    # Promoted from facts because it is identity, not an observation: a stored
    # measurement is correlated with the rest of its interaction by this value,
    # and a reader should not have to know which dictionary it lives in. An
    # operation with no interaction -- an attempt startup -- is its own.
    record["interaction_id"] = str(
        measurement.facts.get("interaction_id") or measurement.measurement_id
    )
    # The run's configured expiry travels with the record because the producer
    # that delivers it also states the manifest, and a manifest built without
    # this would have to re-derive an instant that is the experiment's
    # identity -- the one thing it must not guess at.
    if measurement.run_expires_at is not None:
        record["run_expires_at"] = measurement.run_expires_at.isoformat()
    if measurement.operation == STARTUP_OPERATION:
        # A v1 consumer reads this field by name. It is written only where the
        # name is true -- for the runner's own startup operation -- so a file
        # or terminal record cannot be mistaken for a startup by that reader.
        record[STARTUP_OUTCOME_ALIAS] = outcome
    # Which control source rooted this measurement, and -- for a run an
    # administrator started -- the deployment revision that authorized it. A
    # reader that has the revision can tell two experiments that reused a run
    # id apart, and can tell an old sample from one taken after a Stop.
    record["run_source"] = measurement.run_source
    record["control_revision"] = measurement.control_revision
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
    """Emit the measurement. Safe to call twice and safe on ``None``.

    Idempotent in both directions: a measurement already finalized at its end
    boundary is emitted from those frozen values, and one that never called
    :func:`finalize` is finalized here -- so a caller that forgets cannot make
    a record and a response disagree, it can only move the boundary earlier.
    """
    if measurement is None or measurement.finished:
        return
    finalize(measurement, outcome)
    outcome = measurement.frozen_outcome
    measurement.finished = True
    # The record about to be emitted is what the reserved slot was for, so
    # `discard` can no longer return it.
    measurement.reserved = False
    if not measurement.selected:
        return
    if measurement.run_target and not measurement.bound:
        # A scoped run reports only attempts whose scope was proved; emitting
        # an unbound measurement would claim a sample the run never selected.
        logger.warning(
            "operation profiling dropped an unbound measurement (run %s, operation %s)",
            measurement.run_id,
            measurement.operation,
        )
        return
    record = frozen_record(measurement, outcome=outcome, failure_stage=failure_stage)
    if record is not None:
        _emit(record)


def discard(measurement: Measurement | None) -> None:
    """Stop timing without emitting, returning the reserved budget slot."""
    if measurement is None or measurement.finished:
        return
    measurement.finished = True
    measurement.selected = False
    if measurement.reserved:
        measurement.reserved = False
        _release_budget(measurement.run_id)


def reset_cache() -> None:
    """Drop every cached policy answer and stop the sink. Tests call this."""
    global _CACHE, _MODE, _ENV_RUN, _ENV_RUN_READ, _INVALID_MODE, _REFRESHED
    with _CACHE_LOCK:
        _CACHE = None
    with _MODE_LOCK:
        _MODE = None
        _ENV_RUN = None
        _ENV_RUN_READ = False
        _INVALID_MODE = ""
    with _STORE_LOCK:
        _REFRESHED = 0
    with _BUDGET_LOCK:
        _BUDGETS.clear()
    close_sink()


__all__ = [
    "BOUNDARIES_BY_OPERATION",
    "BOUNDARY_VERSION",
    "CLOCK_DOMAIN_ID",
    "COVERAGE_BROWSER_INTERACTION",
    "COVERAGE_BY_OPERATION",
    "COVERAGE_HOST_STARTUP",
    "COVERAGE_SERVER_REQUEST",
    "DEFAULT_PRODUCER",
    "DEFAULT_ROOT_STAGE",
    "ENV_PROFILING_ENABLED",
    "ENV_PROFILING_MODE",
    "ENV_PROFILING_RUN",
    "ENV_PROFILING_SINK",
    "MAX_CONTEXT_CHARS",
    "MAX_REMOTE_STAGES",
    "MAX_RUN_ID_CHARS",
    "MAX_SELECTIONS",
    "MAX_SPANS",
    "MODE_ADMIN",
    "MODE_DISABLED",
    "MODE_ENVIRONMENT",
    "OUTCOMES",
    "PRODUCER_INSTANCE_ID",
    "PROFILING_ANSWER_CALL_ID_HEADER",
    "PROFILING_ANSWER_OPERATION_HEADER",
    "PROFILING_ANSWER_RECORDED_HEADER",
    "PROFILING_ANSWER_STAGES_HEADER",
    "PROFILING_ANSWER_TRUNCATED_HEADER",
    "PROFILING_CONTEXT_HEADER",
    "PROFILING_MODES",
    "RUN_ID_PATTERN",
    "SCHEMA_VERSION",
    "SELECTION_TTL_S",
    "SINK_QUEUE_MAX",
    "SOURCE_ADMIN",
    "SOURCE_ENVIRONMENT",
    "STARTUP_OPERATION",
    "STARTUP_OUTCOME_ALIAS",
    "STOP_REASON_EXPIRED",
    "STOP_REASON_STOPPED",
    "TARGET_KEYS",
    "ControlState",
    "Measurement",
    "PolicyProvider",
    "ProfilingRun",
    "RuntimePolicyStore",
    "StageObserver",
    "StageRecorder",
    "active_run",
    "answer_from_headers",
    "attach_remote_stages",
    "attach_selection",
    "attributed_stage",
    "bind",
    "budget_state",
    "close_sink",
    "collection_complete",
    "context_call_id",
    "control_state",
    "discard",
    "expect_fragment",
    "finalize",
    "finish",
    "flush_sink",
    "fragment_truncated",
    "frozen_record",
    "handoff_context",
    "install_policy_provider",
    "manifest_for_measurement",
    "manifest_for_run",
    "mark",
    "measurement_context",
    "parse_context",
    "parse_run",
    "policy_provider",
    "policy_store",
    "profile_mode",
    "reason",
    "record_fragment",
    "refresh_policy",
    "reset_cache",
    "reset_policy",
    "set_attributes",
    "settle",
    "span",
    "stage_observer",
    "start_operation",
    "start_stage_recording",
    "substage",
    "to_record",
]
