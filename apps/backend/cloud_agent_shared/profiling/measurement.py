"""Select, collect, freeze, and serialize one local operation measurement."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from typing import Any

from .clock import clock
from .models import _EMPTY_SPAN, _NULL_SPAN, Measurement, _SpanRecord
from .policy import _claim_budget, _load, _release_budget
from .schema import (
    _OUTCOMES,
    BOUNDARIES_BY_OPERATION,
    BOUNDARY_VERSION,
    COVERAGE_BY_OPERATION,
    DEFAULT_PRODUCER,
    MAX_ATTRIBUTE_CHARS,
    MAX_REMOTE_STAGES,
    MAX_SPANS,
    SCHEMA_VERSION,
    STARTUP_OPERATION,
    STARTUP_OUTCOME_ALIAS,
    STOP_REASON_EXPIRED,
)


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
    if clock().monotonic() > run.deadline_mono:
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
        started_mono_ns=clock().monotonic_ns(),
        deadline_mono=run.deadline_mono,
        control_revision=_load().control.config_version,
        run_operations=run.operations,
        run_target=dict(run.target),
        run_max_operations=run.max_operations,
        run_continuous=run.continuous,
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


def _clamp_to_deadline(measurement: Measurement, end_us: int) -> tuple[int, bool]:
    """Return an interval end that never runs past the run's deadline."""
    if not measurement.past_deadline():
        return end_us, False
    measurement.expired = True
    measurement.truncated = True
    return min(end_us, max(0, measurement.deadline_offset_us())), True


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
        # Who actually contributed to this record. A run needs the sandbox manager's
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
    elif measurement.run_continuous:
        record["run_expires_at"] = None
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


def discard(measurement: Measurement | None) -> None:
    """Stop timing without emitting, returning the reserved budget slot."""
    if measurement is None or measurement.finished:
        return
    measurement.finished = True
    measurement.selected = False
    if measurement.reserved:
        measurement.reserved = False
        _release_budget(measurement.run_id)
