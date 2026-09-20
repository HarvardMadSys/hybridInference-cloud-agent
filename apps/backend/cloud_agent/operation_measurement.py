"""Server-side selection for platform operations the control plane roots.

The runner roots ``attempt.startup`` and knows its own attempt, so
``operation_profiling.start_operation`` selects there. A file or terminal
request has no attempt at all: it arrives at the control plane, which is the
only place that knows who is asking, which chat owns the workspace, and whether
the experiment targets it. This module is that root.

Three identities, and the difference between them matters:

* **interaction** -- one logical user interaction, such as opening a terminal.
  A browser echoes ``X-Agent-Interaction-Id`` and every request of that
  interaction carries the same id, which is what lets the records be read
  together. It is correlation only: it grants no selection and no access.
* **measurement** -- one local root request. **Every request mints its own.**
  Two requests never share a collector, so one finishing cannot close the
  other's record or consume its answer, and the id is immutable and therefore
  safe to use as a database key later.
* **call** -- one physical downstream call inside one measurement. Each call
  mints its own id and the answer is recorded against the id that call was
  made with, never against a mutable "current call".

What else this module guarantees, and why each one is here:

* **Selection happens after the existing checks, never instead of them.** The
  caller authenticates, resolves ownership and entitlement, and resolves the
  broker exactly as before; only then does :func:`observe` decide whether to
  measure. Profiling adds no permission exemption and no extra probe request.
* **The current request's own facts are the only evidence.** A continuation is
  re-validated against what *this* request resolved -- run, operation, chat,
  host, thread, sandbox -- and a fact that is missing or has changed means the
  request is simply not measured. The previous request's facts are never
  copied forward to fill a gap: a record that claimed the old host would be
  attributing this request's time to a machine that did not serve it.
* **A slot is charged only for a candidate the run selects, and refunded
  otherwise.** A rejected candidate gives its slot back, so traffic from other
  chats cannot spend a chat-scoped experiment.
* **Nothing here can fail a business request.** Every entry point answers
  ``None`` for "not selected"; a malformed run descriptor or an unparseable
  header disables collection and leaves the request to behave as it always did.

The budget is per process. The design is explicit that fleet-wide admission is
a separate slice, so an experiment on more than one control-plane replica must
record each instance's share in its manifest rather than claim a global cap.
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

from cloud_agent_profiling import operation_profiling

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_profiling.operation_profiling import Measurement

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

#: Which local request a record describes. Distinct from the operation because
#: one interaction makes several requests: aggregating them under one operation
#: name would mix a directory listing into a file-open distribution.
REQUEST_KINDS = {
    FILES_LIST: "files.list",
    FILES_OPEN: "files.open",
    TERMINAL_OPEN: "terminal.open",
}
#: The local requests one terminal interaction makes. An operation filter can
#: select ``terminal.open``; a distribution over the requests has to be able to
#: say which of them a sample is, because a list and a stream attach are not
#: the same wait.
TERMINAL_LIST = "terminal.list"
TERMINAL_CREATE = "terminal.create"
TERMINAL_STREAM = "terminal.stream_attach"
for _request in (TERMINAL_LIST, TERMINAL_CREATE, TERMINAL_STREAM):
    REQUEST_KINDS[_request] = _request

#: The operation a request measures before its kind is known. A non-empty path
#: may be a file preview or a directory expansion, and only the read result can
#: say which, so the request starts as a bounded candidate over both.
FILE_CANDIDATES = (FILES_LIST, FILES_OPEN)

#: Where the browser hands back the interaction it is continuing. The id is a
#: correlation handle: it cannot name a host or a workspace, and it only
#: resolves to an interaction this process already authorized.
INTERACTION_HEADER = "X-Agent-Interaction-Id"

#: The id this replaced. Accepted during the migration so a client that already
#: echoes a measurement id keeps correlating; it is resolved through the
#: server-side table and is never reused as a new record's id.
LEGACY_MEASUREMENT_HEADER = "X-Agent-Measurement-Id"

#: Longest id accepted, matching the minted shape with room to spare.
MAX_ID_CHARS = 64

_INTERACTIONS: OrderedDict[str, Interaction] | None = None
_INTERACTION_LOCK = threading.Lock()


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
    #: Operations this request may resolve to before its result is known. A
    #: file path is a candidate over listing and opening; everything else is
    #: the one operation it names. The route states this because it is the
    #: route that knows which outcomes are still possible for its own call.
    candidates: frozenset[str] | None = None

    def offered(self) -> frozenset[str]:
        """Return the operations this request may be filed under."""
        if self.candidates:
            return self.candidates
        return _candidates(self.operation)


@dataclass(frozen=True)
class Interaction:
    """One logical interaction, as far as it can be decided up front.

    Immutable on purpose: it is the part every request of the interaction
    shares, and nothing a later request does may change what the first one
    established. It carries the operation *candidates* rather than the
    operation, because a non-empty path's kind is only known after the read.
    """

    interaction_id: str
    job_id: str
    service: str
    run_id: str
    #: Operations this interaction may resolve to. More than one only for a
    #: file path, whose kind the read decides.
    candidates: frozenset[str]
    #: Operations the run actually named, so a confirmed kind that this
    #: experiment does not select is dropped rather than reported.
    allowed: frozenset[str]
    #: The run's own deadline, so no request of the interaction outlives it.
    deadline_mono: float
    started_mono: float
    last_used_mono: float
    #: The attribution this interaction was established with, including the
    #: chat id. A continuation has to match it even when the run does not
    #: filter by those fields: an interaction is one chat's workspace on one
    #: machine, and a request that resolved a different host or sandbox is a
    #: different interaction, not a continuation of this one. The chat id is
    #: included because it lives on the scope rather than in its facts, which
    #: is what made a job-scoped run reject a legitimate continuation.
    source_facts: Mapping[str, str] = field(default_factory=dict)

    @property
    def operation(self) -> str:
        """Return the single operation, or the first candidate deterministically."""
        return min(self.candidates)

    @property
    def offered(self) -> frozenset[str]:
        """Return the candidate operation names, for the wire context."""
        return frozenset(name for name in self.candidates if name)


@dataclass
class Selection:
    """One selected root request: its own measurement, its own lifetime.

    The object is the request's lifetime: :func:`operation` ends the
    measurement on exit, so every exit path -- a returned response, a raised
    HTTP error, a cancelled request -- reports exactly one outcome. Finishing
    is safe more than once. Nothing here is shared with another request.
    """

    measurement: Measurement
    interaction: Interaction
    outcome: str = "incomplete"
    failure_stage: str | None = None
    #: Answers this request received, keyed by the call they answer, so a
    #: second call cannot overwrite the first call's answer.
    calls: dict[str, bool] = field(default_factory=dict)
    #: Which local request this record describes. Every route states it: an
    #: interaction is several requests, and a sample that cannot say which one
    #: it is turns a latency distribution into a mixture.
    request_kind: str | None = None
    _finished: bool = False

    @property
    def measurement_id(self) -> str:
        """Return this request's immutable record id."""
        return self.measurement.measurement_id

    @property
    def interaction_id(self) -> str:
        """Return the interaction every request of this interaction shares."""
        return self.interaction.interaction_id

    @property
    def operation(self) -> str:
        """Return the operation this request is currently filed under."""
        return self.measurement.operation

    def name_request(self, request_kind: str) -> None:
        """State which local request this record describes."""
        self.request_kind = request_kind
        operation_profiling.set_attributes(self.measurement, request_kind=request_kind)

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

    def begin_call(self, stage: str, *, producer: str = BROKER_PRODUCER) -> str | None:
        """Register one downstream call and return the id that identifies it.

        Always a fresh id. A request that makes two calls -- a terminal open
        that lists and then creates -- owes an answer for each, and a mutable
        "current call" would let the second silently replace the first.
        """
        return operation_profiling.expect_fragment(self.measurement, producer, stage)

    def context(self, call_id: str | None) -> dict[str, Any]:
        """Return what a downstream producer inherits for one call.

        The transport decides how to encode it. It states the interaction, this
        request's measurement, the call, the candidate operations, and an
        absolute wall-clock expiry -- two processes share no monotonic clock.
        """
        context = operation_profiling.measurement_context(self.measurement, call_id) or {}
        if not context:
            return {}
        context["interaction_id"] = self.interaction_id
        # The *candidate operation strings* this request may be filed under,
        # intersected with what the run selects here. Deliberately not a list of
        # measurement objects: the broker is told which operations are in play,
        # not asked to choose one before its result exists.
        context["operations"] = sorted(self.interaction.allowed)
        context.pop("operation", None)
        return context

    def confirm(self, operation: str, *, request_kind: str | None = None) -> bool:
        """File this request under the operation its result actually was.

        A file path starts as a candidate over ``files.list`` and
        ``files.open`` because only the read can say which it was. Once the
        answer names the kind, the request is filed under it -- but only if
        this experiment selects that operation. Returns whether it does; a
        caller that gets ``False`` should :func:`discard` the measurement
        rather than report a sample under a name the run did not ask for.
        """
        if operation not in self.interaction.allowed:
            return False
        self.measurement.operation = operation
        operation_profiling.set_attributes(
            self.measurement,
            operation=operation,
            request_kind=request_kind or REQUEST_KINDS.get(operation, operation),
        )
        self.request_kind = request_kind or REQUEST_KINDS.get(operation, operation)
        return True

    def result_operation(self, response: Any, *, default: str) -> str:
        """Return the operation this request's result turned out to be.

        The operation comes from the broker's answer when it named one, and
        otherwise from the response body's own ``kind`` -- the same evidence,
        read where the answer could not carry it. A request that already had a
        single candidate keeps it. This only answers the question; the caller
        decides with :meth:`confirm` whether that operation is one to report.
        """
        operation = default
        if isinstance(response, Mapping):
            named = str(
                response.get("profiling_operation") or _operation_from_kind(response, default) or ""
            ).strip()
            if named:
                operation = named
        return operation

    def record_answer(self, stage: str, call_id: str | None, response: Any) -> bool:
        """Record what a downstream producer answered about one call.

        Returns whether the response matched the call it claims to answer. The
        answer says the producer finished *collecting*, not how much it
        collected: a call that legitimately did no timed work is still complete.
        """
        recorded = operation_profiling.record_fragment(
            self.measurement,
            producer=BROKER_PRODUCER,
            call_id=call_id,
            stage=stage,
            response=response,
        )
        if call_id:
            self.calls[call_id] = recorded
        return recorded

    def record_answer_from_headers(self, stage: str, call_id: str | None, answer: Any) -> bool:
        """Record an answer that arrived in response headers rather than a body.

        A terminal stream has already started sending the terminal's own bytes
        when it answers, so its answer travels in headers. An empty or
        unmatched value leaves the fragment unanswered, and the record names
        the fragment it owed instead of implying one it never got.
        """
        if not isinstance(answer, Mapping):
            return False
        return self.record_answer(stage, call_id, dict(answer))

    def settle(self) -> None:
        """Freeze the end boundary and completeness, once, before any report.

        Called before a response is built. Everything that reports on this
        request afterwards -- the response block, the SSE headers, the log
        line, the persisted row -- reads the same frozen values, so none of
        them can contradict another about the sample they all describe.

        The freeze happens here rather than at export on purpose: encoding a
        response takes real time, and that time is not collection. A response
        built at 100 and exported at 102 used to disagree -- complete in the
        body, partial in the record -- and the boundary is what removes the
        disagreement rather than hiding it.
        """
        operation_profiling.finalize(self.measurement, self.outcome)

    def frozen_record(self, *, failure_stage: str | None = None) -> dict[str, Any] | None:
        """Return the record this request would persist, or ``None`` if none would.

        The same bytes the exporter would write, so an in-process store and a
        delivered one cannot describe the same measurement differently.
        """
        return operation_profiling.frozen_record(
            self.measurement,
            self.outcome,
            failure_stage or self.failure_stage,
        )

    def metadata(self) -> dict[str, Any] | None:
        """Return the block a response carries, or ``None`` when unselected.

        Two different completeness questions, kept apart on purpose: whether
        each downstream fragment answered, and whether this whole local record
        is complete. A reader must not have to infer either from the other.
        """
        if self._finished:
            # Discarded: the request ran, but it was not a sample this run
            # asked for, so the response carries no measurement block at all.
            return None
        recorded_calls = [call for call, answered in self.calls.items() if answered]
        return {
            "interaction_id": self.interaction_id,
            "measurement_id": self.measurement_id,
            # The call this answer belongs to, most recent first: a request
            # that made several calls reports the one that produced the reply.
            "call_id": recorded_calls[-1] if recorded_calls else None,
            "operation": self.measurement.operation,
            "request_kind": self.request_kind or REQUEST_KINDS.get(self.measurement.operation, ""),
            # At least one downstream call answered. Every call that did not
            # is named in the record's ``missing_fragments``, so per-call
            # completeness is stated there rather than compressed into a
            # single boolean here.
            "fragment_recorded": bool(recorded_calls),
            "fragment_truncated": operation_profiling.fragment_truncated(
                self.measurement, recorded_calls
            ),
            "collection_complete": operation_profiling.collection_complete(
                self.measurement, self.outcome
            ),
        }

    def finish(self, outcome: str | None = None, *, failure_stage: str | None = None) -> None:
        """Emit the record once. Safe to call more than once.

        An outcome passed here may only replace one that was never frozen: a
        route that already built its response has stated what the sample was,
        and a later failure cannot rewrite a reply the client already has.
        """
        if self._finished:
            return
        self._finished = True
        settled = outcome or self.outcome
        if not self.measurement.frozen or outcome is None:
            self.outcome = settled
        operation_profiling.finish(
            self.measurement,
            outcome=self.outcome,
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


def root_scope(
    operation: str,
    job: Mapping[str, Any],
    *,
    service: str = ROUTE_FILES,
    candidates: frozenset[str] | None = None,
) -> Root:
    """Build one root operation's scope from the job its route already resolved."""
    return Root(
        operation=operation,
        job_id=str(job.get("id") or ""),
        facts=scope_facts(job),
        service=service,
        candidates=candidates,
    )


def continuation(request_headers: Mapping[str, str] | None) -> str | None:
    """Return the interaction a browser echoed, if it sent a usable one.

    Header lookup is case-insensitive because HTTP header names are, and a
    value that is not a plausible id is dropped rather than raising: profile
    plumbing never fails a file read.
    """
    if not request_headers:
        return None
    wanted = {INTERACTION_HEADER.lower(), LEGACY_MEASUREMENT_HEADER.lower()}
    for name, value in request_headers.items():
        if str(name).lower() not in wanted:
            continue
        text = str(value).strip()
        if not text or len(text) > MAX_ID_CHARS:
            return None
        return text
    return None


#: What a workspace read reports it found. The broker is the only component
#: that knows, because it performs the read; a path alone cannot say.
FILE_KINDS = {
    "directory": FILES_LIST,
    "file": FILES_OPEN,
    "symlink": FILES_OPEN,
}


def _operation_from_kind(response: Mapping[str, Any], default: str) -> str | None:
    """Return the file operation a response's own ``kind`` implies."""
    if default not in FILE_CANDIDATES:
        return None
    return FILE_KINDS.get(str(response.get("kind") or "").strip())


def _candidates(operation: str) -> frozenset[str]:
    """Return the operations one route may resolve to before its result is in."""
    return frozenset(FILE_CANDIDATES if operation in FILE_CANDIDATES else (operation,))


def _eligible(run: operation_profiling.ProfilingRun, candidates: frozenset[str]) -> frozenset[str]:
    """Return the candidates this run actually selects."""
    return frozenset(name for name in candidates if name in run.operations)


def start(operation: str, scope: Root) -> Selection | None:
    """Root one request, or return ``None`` when the run does not select it.

    The cheap pre-filter -- does the run name any candidate operation, and does
    its host match -- runs before the budget is claimed. The authoritative
    scope check is then ``bind``, which requires every field the run scopes
    itself by to be present and equal, so a workspace this route could not
    resolve is a mismatch rather than a wildcard. A candidate that fails it
    returns its slot.
    """
    return _root(operation, scope, interaction_id=None)


def _root(operation: str, scope: Root, *, interaction_id: str | None) -> Selection | None:
    """Create one request's measurement under an existing or new interaction."""
    run = operation_profiling.active_run()
    candidates = scope.offered()
    if run is None:
        return None
    allowed = _eligible(run, candidates)
    if not allowed:
        return None
    measurement = operation_profiling.start_operation(
        operation=min(allowed),
        producer=PRODUCER,
        host=scope.facts.get("host"),
        # These boundaries start after the route's own authentication and
        # end when the response object is built: a server request, never the
        # user's wait, which is what the record has to say about itself.
        coverage_scope=operation_profiling.COVERAGE_SERVER_REQUEST,
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
    interaction = Interaction(
        interaction_id=interaction_id or measurement.measurement_id,
        job_id=scope.job_id,
        service=scope.service,
        run_id=measurement.run_id,
        candidates=candidates,
        allowed=allowed,
        deadline_mono=measurement.deadline_mono,
        started_mono=now,
        last_used_mono=now,
        source_facts={**scope.facts, "job_id": scope.job_id},
    )
    selection = Selection(measurement=measurement, interaction=interaction)
    operation_profiling.set_attributes(
        measurement,
        interaction_id=interaction.interaction_id,
        request_kind=REQUEST_KINDS.get(interaction.operation, interaction.operation),
    )
    selection.request_kind = REQUEST_KINDS.get(interaction.operation, interaction.operation)
    _remember(interaction)
    return selection


def observe(operation: str, scope: Root, *, continuing: str | None = None) -> Selection | None:
    """Return the selection for one request, continuing an interaction if asked.

    ``continuing`` is the id the browser echoed. It only ever resolves to an
    interaction this process minted for the same operation and the same chat
    *and* whose scope still agrees with what this request just resolved, so a
    guessed or stale id cannot observe another workspace, outlive a change of
    host, or start an experiment.
    """
    if continuing:
        interaction = _resume(continuing, scope=scope)
        if interaction is None:
            # The caller stated which interaction this request belongs to and
            # this process cannot honour it -- evicted, expired, or another
            # replica's. The business request proceeds, but it is not sampled:
            # a fresh interaction here would present one more sample as if the
            # interrupted interaction had continued, which is exactly the
            # continuity the browser is asking about.
            return None
        return _root(operation, scope, interaction_id=interaction.interaction_id)
    # No reference at all: an ordinary new request, selected on its own facts.
    return start(operation, scope)


def _resume(interaction_id: str, *, scope: Root) -> Interaction | None:
    """Return a live interaction this request may continue, if any.

    Everything is checked against *this* request: the candidates must cover the
    operation, the chat must be the one this route just authorized, and every
    fact the run is scoped by must still be present and equal to what this
    request resolved. A host or sandbox that changed, or that could not be
    resolved at all, ends the interaction here rather than recording the new
    request under the old facts.
    """
    wanted = (interaction_id or "").strip()
    if not wanted or len(wanted) > MAX_ID_CHARS:
        return None
    run = operation_profiling.active_run()
    if run is None:
        return None
    now = time.monotonic()
    with _INTERACTION_LOCK:
        if _INTERACTIONS is None:
            return None
        _prune(now)
        interaction = _INTERACTIONS.get(wanted)
        if interaction is None:
            return None
        if interaction.job_id != scope.job_id or interaction.run_id != run.run_id:
            return None
        if not scope.offered() & interaction.candidates:
            return None
        if now - interaction.last_used_mono > operation_profiling.SELECTION_TTL_S:
            del _INTERACTIONS[wanted]
            return None
        # Every fact the interaction was established with has to still hold,
        # whether or not the run filters by it. A host that changed under an
        # unfiltered run is still a different machine, and continuing would
        # report this request's time as having been served by the old one.
        observed = {**scope.facts, "job_id": scope.job_id}
        for key, established in interaction.source_facts.items():
            if observed.get(key) != established:
                return None
        refreshed = Interaction(
            interaction_id=interaction.interaction_id,
            job_id=interaction.job_id,
            service=interaction.service,
            run_id=interaction.run_id,
            candidates=interaction.candidates,
            allowed=_eligible(run, interaction.candidates),
            deadline_mono=interaction.deadline_mono,
            started_mono=interaction.started_mono,
            last_used_mono=now,
            source_facts=interaction.source_facts,
        )
        _INTERACTIONS[wanted] = refreshed
        return refreshed


def _prune(now: float) -> None:
    """Drop expired interactions. The caller holds the lock."""
    assert _INTERACTIONS is not None
    for key in [key for key, item in _INTERACTIONS.items() if item.deadline_mono <= now]:
        del _INTERACTIONS[key]


def _remember(interaction: Interaction) -> None:
    """Remember one interaction under a bounded, TTL'd table."""
    global _INTERACTIONS
    now = time.monotonic()
    with _INTERACTION_LOCK:
        if _INTERACTIONS is None:
            _INTERACTIONS = OrderedDict()
        _prune(now)
        _INTERACTIONS[interaction.interaction_id] = interaction
        while len(_INTERACTIONS) > operation_profiling.MAX_SELECTIONS:
            # Oldest first. Dropping a live interaction only costs coverage: a
            # browser continuing it finds no entry and simply is not measured.
            _INTERACTIONS.popitem(last=False)


@contextmanager
def operation(op: str, scope: Root, *, continuing: str | None = None) -> Iterator[Selection | None]:
    """Run one request inside its own measurement and report the outcome.

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
    """Forget every remembered interaction. Tests call this."""
    global _INTERACTIONS
    with _INTERACTION_LOCK:
        _INTERACTIONS = None


__all__ = [
    "BROKER_PRODUCER",
    "FILES_LIST",
    "FILES_OPEN",
    "FILE_CANDIDATES",
    "FILE_KINDS",
    "INTERACTION_HEADER",
    "LEGACY_MEASUREMENT_HEADER",
    "MAX_ID_CHARS",
    "PRODUCER",
    "REQUEST_KINDS",
    "ROUTE_FILES",
    "ROUTE_TERMINALS",
    "TERMINAL_CREATE",
    "TERMINAL_LIST",
    "TERMINAL_OPEN",
    "TERMINAL_STREAM",
    "Interaction",
    "Root",
    "Selection",
    "continuation",
    "current_run",
    "observe",
    "operation",
    "reset",
    "root_scope",
    "scope_facts",
    "start",
]
