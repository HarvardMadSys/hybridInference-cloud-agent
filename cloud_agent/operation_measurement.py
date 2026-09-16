"""Server-side selection for file and terminal measurements.

The runner roots ``attempt.startup`` and knows its own attempt, so
``operation_profiling.start_operation`` selects there. A file or terminal
request has no attempt at all: it arrives at the control plane, which is the
only place that knows who is asking, which chat owns the workspace, and whether
the experiment targets it. This module is that root.

What it guarantees, and why each one is here:

* **Selection happens after the existing checks, never instead of them.** The
  caller authenticates, resolves ownership and entitlement, and resolves the
  broker exactly as before; only then does :func:`observe` decide whether to
  measure. Profiling adds no permission exemption and no extra probe request.
* **The target filter is applied where the facts are.** Every field a run
  scopes itself by is matched against what this route already knows -- the job,
  its thread, its sandbox, its host -- and a run that scopes itself by a field
  the route cannot prove simply does not select. That is why selection lives
  here rather than being delegated to ``bind``, whose job is to confirm the
  facts of an attempt a runner has already claimed.
* **A slot is charged only for a candidate that survives the filter.** The
  cheap pre-filter runs first; the budget is claimed last, immediately before
  the measurement exists.
* **A continuing interaction reuses one measurement.** A terminal open is a
  list, a create and a stream; the browser echoes the measurement id back and
  this module returns the same selection instead of charging a second slot.
  The cache is bounded and every entry dies with the run's own deadline, so a
  client that never finishes cannot grow it or extend the experiment.
* **Nothing here can fail a business request.** Every entry point answers
  ``None`` for "not selected"; a malformed run descriptor or an unparseable
  header disables collection and leaves the request to behave as it always did.

The budget is per process. The design is explicit that a fleet-wide admission
is a separate slice, so an experiment on more than one control-plane replica
must record each instance's share in its manifest rather than claim a global
cap.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent_host import operation_profiling

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.operation_profiling import Measurement

logger = logging.getLogger(__name__)

#: Producer name this process writes into a record it roots.
PRODUCER = "control-plane"

#: Root operations this module selects. The names are the contract's: a run
#: that names one of these selects it, and nothing else does.
FILES_LIST = "files.list"
FILES_OPEN = "files.open"
TERMINAL_OPEN = "terminal.open"

#: Producer a downstream broker is expected to contribute under.
BROKER_PRODUCER = "broker"

#: Route hints, so a caller inside the same process can name what is being
#: measured without restating the operation string.
ROUTE_FILES = "files"
ROUTE_TERMINALS = "terminals"

#: Where the browser hands back the measurement it is continuing. The browser's
#: id is only a correlation handle: it cannot start a measurement, name a host
#: or claim a workspace, because no selection exists for an id this process
#: never minted.
CONTINUATION_HEADER = "X-Agent-Measurement-Id"

#: Longest continuation id accepted, matching the minted shape with room to
#: spare. A longer value is malformed, not a selection.
MAX_MEASUREMENT_ID_CHARS = 64

_SELECTIONS: OrderedDict[str, Selection] | None = None
_SELECTION_LOCK = threading.Lock()


@dataclass(frozen=True)
class Root:
    """A root operation's scope, as far as the entry point can already prove it.

    ``facts`` carries only values this route has actually resolved. A fact that
    is absent is not a wildcard -- a run scoping itself by it does not select --
    which is what keeps an unresolved workspace out of a scoped experiment
    instead of reporting it as a match.
    """

    operation: str
    job_id: str
    facts: Mapping[str, str] = field(default_factory=dict)
    service: str = ROUTE_FILES


@dataclass
class Selection:
    """One selected platform operation, and everything the caller needs for it.

    The object is also the operation's lifetime: ``with profiling.observe(...)
    as measure:`` ends the measurement on exit, so a route cannot return a
    response that was never reported. Finishing is safe more than once.
    """

    measurement: Measurement
    operation: str
    service: str
    run_id: str
    deadline_mono: float
    started_mono: float
    last_used_mono: float
    call_id: str | None = None
    outcome: str = "incomplete"
    failure_stage: str | None = None
    #: Whether the last registered downstream call came back with an answer
    #: this process could match to that call.
    _answered: bool = False
    _finished: bool = False

    @property
    def measurement_id(self) -> str:
        """Return the id a browser echoes back to continue this interaction."""
        return self.measurement.measurement_id

    def close(self) -> None:
        """End the measurement. The caller's half of the lifetime API.

        :func:`operation` calls this on every exit path; a caller that manages
        its own lifetime uses the same door so a record is emitted once.
        """
        self.finish()

    def mark(self, name: str) -> None:
        """Record one local stage boundary."""
        operation_profiling.mark(self.measurement, name)

    def stage(
        self, parent: str, name: str, *, producer: str | None = None
    ) -> AbstractContextManager[None]:
        """Time one local sub-stage under a named parent."""
        return operation_profiling.attributed_stage(
            self.measurement, parent, name, producer=producer or PRODUCER
        )

    def begin_call(
        self, stage: str, *, producer: str = BROKER_PRODUCER, fresh: bool = False
    ) -> str | None:
        """Register one downstream call and return the id that identifies it.

        Registered before the call, so a call that never answers is a fragment
        this record owed rather than an absence nobody can see. ``fresh`` mints
        a new id for a second physical call within one interaction; without it
        the current id is reused, which is what lets a list/create/stream
        sequence be answered by the call that actually finished.
        """
        if fresh or self.call_id is None:
            self.call_id = operation_profiling.expect_fragment(self.measurement, producer, stage)
        return self.call_id

    def record_answer(self, stage: str, response: Any) -> bool:
        """Record what a downstream producer answered about one call.

        Returns whether the response matched the call it claims to answer. The
        answer says the producer finished *collecting*, not how much it
        collected: a call that legitimately did no timed work is still complete.
        """
        recorded = operation_profiling.record_fragment(
            self.measurement,
            producer=BROKER_PRODUCER,
            call_id=self.call_id,
            stage=stage,
            response=response,
        )
        if recorded:
            self._answered = True
        return recorded

    @property
    def answered(self) -> bool:
        """Return whether the last registered call came back with an answer."""
        return self._answered

    def record_answer_from_headers(self, stage: str, answer: Any) -> bool:
        """Record an answer that arrived in response headers rather than a body.

        A terminal stream has already started sending the terminal's own bytes
        when it answers, so its answer travels in headers. An empty or
        unmatched value leaves the fragment unanswered, and the record names
        the fragment it owed instead of implying one it never got.
        """
        if not isinstance(answer, Mapping):
            return False
        return self.record_answer(stage, dict(answer))

    def context(self) -> dict[str, Any]:
        """Return the selection a downstream producer inherits, as plain data.

        The transport decides how to encode it; this process only states the
        run, the operation, the call, and an absolute wall-clock expiry, since
        two processes share no monotonic clock.
        """
        return operation_profiling.measurement_context(self.measurement, self.call_id) or {}

    def metadata(self, *, recorded: bool, truncated: bool = False) -> dict[str, Any] | None:
        """Return the fragment block for a response, or ``None`` when nothing ran.

        ``recorded`` is the broker's answer about *collecting*, not about how
        much it collected: a stage that legitimately did no timed work still
        counts, and an empty stage list must not be reported as a missing
        producer.
        """
        if self.call_id is None:
            return None
        return {
            "measurement_id": self.measurement_id,
            "call_id": self.call_id,
            "recorded": recorded,
            "truncated": truncated,
        }

    def finish(self, outcome: str | None = None, *, failure_stage: str | None = None) -> None:
        """Emit the record once. Safe to call more than once."""
        if self._finished:
            return
        self._finished = True
        operation_profiling.finish(
            self.measurement,
            outcome=outcome or self.outcome,
            failure_stage=failure_stage or self.failure_stage,
        )

    def discard(self) -> None:
        """Stop without emitting, returning the reserved slot."""
        if self._finished:
            return
        self._finished = True
        operation_profiling.discard(self.measurement)


def current_run() -> operation_profiling.ProfilingRun | None:
    """Return the live run descriptor, or ``None`` when profiling is off."""
    return operation_profiling.active_run()


def scope_facts(job: Mapping[str, Any]) -> dict[str, str]:
    """Return the target facts a route can prove about a chat's workspace.

    Only values already resolved for the business request are returned: the
    chat's thread, its retained sandbox's id and host. A fact that is not known
    stays absent, and a run scoping itself by an absent fact does not select --
    an unresolved workspace is not a match.
    """
    facts: dict[str, str] = {}
    thread_id = job.get("thread_id")
    if thread_id:
        facts["thread_id"] = str(thread_id)
    sandbox = job.get("thread_sandbox")
    if isinstance(sandbox, Mapping):
        for key in ("sandbox_id", "host"):
            value = sandbox.get(key)
            if value:
                facts[key] = str(value)
    return facts


def root_scope(operation: str, job: Mapping[str, Any], *, service: str = ROUTE_FILES) -> Root:
    """Build one root operation's scope from the job its route already resolved."""
    return Root(
        operation=operation,
        job_id=str(job.get("id") or ""),
        facts=scope_facts(job),
        service=service,
    )


def continuation(request_headers: Mapping[str, str] | None) -> str | None:
    """Return the measurement id a browser echoed, if it sent a usable one.

    Header lookup is case-insensitive because HTTP header names are, and a
    value that is not a plausible id is dropped rather than raising: profile
    plumbing never fails a file read.
    """
    if not request_headers:
        return None
    for name, value in request_headers.items():
        if str(name).lower() != CONTINUATION_HEADER.lower():
            continue
        text = str(value).strip()
        if not text or len(text) > MAX_MEASUREMENT_ID_CHARS:
            return None
        return text
    return None


def fragment(selection: Selection | None, **states: bool) -> dict[str, Any] | None:
    """Return the response block for one selection, or ``None`` when unselected."""
    if selection is None:
        return None
    return selection.metadata(
        recorded=bool(states.get("recorded")), truncated=bool(states.get("truncated"))
    )


def start(operation: str, scope: Root) -> Selection | None:
    """Select one root operation, or return ``None`` when it is not selected.

    The cheap pre-filter -- is this operation named by the run at all, and does
    its host match -- runs before the budget is claimed. The authoritative scope
    check is then ``bind``, which requires every field the run scopes itself by
    to be present on the measurement and equal to the configured value, so a
    workspace the route could not resolve is a mismatch rather than a wildcard.
    A candidate that fails it returns its slot: a run scoped to one chat must
    not be spent by traffic from every other chat.
    """
    run = operation_profiling.active_run()
    if run is None or operation not in run.operations:
        return None
    measurement = operation_profiling.start_operation(
        operation=operation,
        producer=PRODUCER,
        # Known before the request and part of the run's scope, so it is the
        # one fact the cheap pre-filter can use without the rest of the scope.
        host=scope.facts.get("host"),
    )
    if measurement is None:
        return None
    if not operation_profiling.bind(measurement, job_id=scope.job_id, **dict(scope.facts)):
        # The authoritative scope check refused a candidate whose slot was
        # already claimed: give the slot back, so a run scoped to one chat is
        # not spent by traffic from every other chat.
        operation_profiling.discard(measurement)
        return None
    now = time.monotonic()
    selection = Selection(
        measurement=measurement,
        operation=operation,
        service=scope.service,
        run_id=measurement.run_id,
        deadline_mono=measurement.deadline_mono,
        started_mono=now,
        last_used_mono=now,
    )
    _remember(selection)
    return selection


def observe(operation: str, scope: Root, *, continuing: str | None = None) -> Selection | None:
    """Return the selection for one request, continuing an earlier one if asked.

    ``continuing`` is the id the browser echoed. It only ever resolves to a
    selection this process minted for the same operation and the same chat, so
    a guessed id cannot observe another workspace or start an experiment.
    """
    if continuing:
        selection = _continue(continuing, operation=operation, job_id=scope.job_id)
        if selection is not None:
            return selection
        # An id this process no longer holds is not a reason to fail the
        # request: it is a new interaction, and the run may still select it.
    return start(operation, scope)


def _prune(now: float) -> None:
    """Drop expired selections. The caller holds the lock."""
    assert _SELECTIONS is not None
    for key in [key for key, item in _SELECTIONS.items() if item.deadline_mono <= now]:
        del _SELECTIONS[key]


def _remember(selection: Selection) -> None:
    """Remember one selection under a bounded, TTL'd table."""
    global _SELECTIONS
    now = time.monotonic()
    with _SELECTION_LOCK:
        if _SELECTIONS is None:
            _SELECTIONS = OrderedDict()
        _prune(now)
        _SELECTIONS[selection.measurement_id] = selection
        while len(_SELECTIONS) > operation_profiling.MAX_SELECTIONS:
            # Oldest first. Dropping a live selection only costs coverage: the
            # client's next request finds no entry and simply is not measured.
            _SELECTIONS.popitem(last=False)


def _continue(measurement_id: str, *, operation: str, job_id: str) -> Selection | None:
    """Return a selection this process already made, or re-root it under its id.

    One interaction is a list, a create and a stream -- separate platform
    operations, each with a duration worth stating. So a request that arrives
    after the previous one already emitted its record is rooted afresh *under
    the same id*: the browser, the broker fragments and every record of the
    interaction stay correlatable, while each request reports its own time. The
    interaction remains bounded because the original deadline and TTL still
    govern it, and each request claims its own slot -- a slot that paid for
    unbounded requests would make ``max_operations`` meaningless.
    """
    wanted = (measurement_id or "").strip()
    if not wanted or len(wanted) > MAX_MEASUREMENT_ID_CHARS:
        return None
    now = time.monotonic()
    with _SELECTION_LOCK:
        if _SELECTIONS is None:
            return None
        _prune(now)
        selection = _SELECTIONS.get(wanted)
        if selection is None:
            return None
        if selection.operation != operation or selection.measurement.facts.get("job_id") != job_id:
            # Same id, different operation or chat: not a continuation of this
            # interaction, and not something to measure under that id.
            return None
        if now - selection.last_used_mono > operation_profiling.SELECTION_TTL_S:
            del _SELECTIONS[wanted]
            return None
        if not selection._finished:
            selection.last_used_mono = now
            selection.measurement.facts["continued_requests"] = (
                int(selection.measurement.facts.get("continued_requests") or 0) + 1
            )
            return selection
        facts = dict(selection.measurement.facts)
        deadline_mono = selection.deadline_mono
        service = selection.service
    # Re-rooted outside the lock: claiming a slot and remembering the new
    # selection must not happen while this table is held.
    return _restart(
        measurement_id=wanted,
        operation=operation,
        job_id=job_id,
        service=service,
        facts=facts,
        host=str(facts.get("host") or "") or None,
        deadline_mono=deadline_mono,
    )


def _restart(
    *,
    measurement_id: str,
    operation: str,
    job_id: str,
    service: str,
    facts: Mapping[str, Any],
    host: str | None,
    deadline_mono: float,
) -> Selection | None:
    """Root another request of an interaction under the interaction's own id."""
    run = operation_profiling.active_run()
    if run is None or operation not in run.operations:
        return None
    measurement = operation_profiling.start_operation(
        operation=operation, producer=PRODUCER, host=host
    )
    if measurement is None:
        return None
    if not operation_profiling.bind(measurement, **dict(facts)):
        operation_profiling.discard(measurement)
        return None
    # The id stays the interaction's, and the deadline stays the experiment's:
    # a later request can neither rename the interaction nor extend the run.
    measurement.measurement_id = measurement_id
    measurement.deadline_mono = min(measurement.deadline_mono, deadline_mono)
    selection = Selection(
        measurement=measurement,
        operation=operation,
        service=service,
        run_id=measurement.run_id,
        deadline_mono=measurement.deadline_mono,
        started_mono=time.monotonic(),
        last_used_mono=time.monotonic(),
    )
    operation_profiling.set_attributes(
        measurement,
        continued_requests=int(facts.get("continued_requests") or 0) + 1,
    )
    _remember(selection)
    return selection


@contextmanager
def operation(op: str, scope: Root, *, continuing: str | None = None) -> Iterator[Selection | None]:
    """Run one request inside its measurement and report the outcome.

    The measurement ends when the block does, so every exit path -- a returned
    response, a raised HTTP error, a cancelled request -- reports exactly one
    outcome. An exception is reported as ``failed`` and re-raised unchanged:
    profiling observes the request, it does not handle it.
    """
    selection = observe(op, scope, continuing=continuing)
    if selection is None:
        yield None
        return
    try:
        yield selection
    finally:
        # One exit path, so a returned response, a raised HTTP error and a
        # cancelled request all report exactly one outcome and emit at most
        # one record. The exception is not handled here, only observed.
        if not selection._finished and sys.exc_info()[0] is not None:
            selection.outcome = "failed"
            selection.failure_stage = selection.failure_stage or "route"
        selection.finish()


def reset() -> None:
    """Forget every remembered selection. Tests call this."""
    global _SELECTIONS
    with _SELECTION_LOCK:
        _SELECTIONS = None


__all__ = [
    "BROKER_PRODUCER",
    "CONTINUATION_HEADER",
    "FILES_LIST",
    "FILES_OPEN",
    "MAX_MEASUREMENT_ID_CHARS",
    "PRODUCER",
    "ROUTE_FILES",
    "ROUTE_TERMINALS",
    "TERMINAL_OPEN",
    "Root",
    "Selection",
    "continuation",
    "current_run",
    "fragment",
    "observe",
    "operation",
    "reset",
    "root_scope",
    "scope_facts",
    "start",
]
