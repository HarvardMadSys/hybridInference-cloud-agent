"""Validate and continue selections and fragment answers across process boundaries."""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .clock import clock
from .measurement import _bounded
from .models import Measurement, ProfilingRun, _SpanRecord
from .policy import _load, profile_mode
from .schema import (
    BOUNDARIES_BY_OPERATION,
    CLOCK_DOMAIN_ID,
    COVERAGE_BY_OPERATION,
    DEFAULT_PRODUCER,
    DEFAULT_ROOT_STAGE,
    MAX_ATTRIBUTE_CHARS,
    MAX_CONTEXT_CHARS,
    MAX_REMOTE_STAGES,
    MAX_RUN_ID_CHARS,
    MAX_SPANS,
    MODE_ADMIN,
    PRODUCER_INSTANCE_ID,
    PROFILING_ANSWER_CALL_ID_HEADER,
    PROFILING_ANSWER_OPERATION_HEADER,
    PROFILING_ANSWER_RECORDED_HEADER,
    PROFILING_ANSWER_STAGES_HEADER,
    PROFILING_ANSWER_TRUNCATED_HEADER,
    RUN_ID_PATTERN,
    SOURCE_ADMIN,
    TARGET_KEYS,
)
from .stages import StageRecorder

logger = logging.getLogger("cloud_agent_shared.operation_profiling")


def measurement_context(
    measurement: Measurement | None, call_id: str | None = None
) -> dict[str, Any] | None:
    """Return the selection a downstream producer needs to inherit.

    The runner decides whether an operation is selected; a sandbox manager cannot make
    that decision for itself, because it sees one call at a time and does not
    know which attempt it belongs to. So the selected measurement travels with
    the request: run id, operation, and an absolute wall-clock expiry, since
    two processes share no monotonic clock.

    Continuous runs carry an explicit null expiry and their immutable manifest.
    For timed runs, the expiry is the experiment's own end. There is no shorter lease to hand
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
        "expires_at": None
        if math.isinf(deadline)
        else clock().time() + max(0.0, deadline - clock().monotonic()),
        # The parent's own start, in nanoseconds. A child on the same host
        # shares this monotonic clock, so a turn it runs can be timed from the
        # claim rather than from whenever the child happened to start --
        # otherwise every sample silently loses the queueing it exists to show.
        "started_mono_ns": measurement.started_mono_ns,
        # The candidate operations this caller says are in play. Deliberately
        # *not* the experiment's manifest: a caller may narrow the candidates it
        # offers, and a sandbox manager must never let that claim override what its own
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
        # that only continues the selection -- a child, a sandbox manager -- reports the
        # same provenance its parent proved instead of inferring one.
        "source": measurement.run_source,
    }
    if measurement.run_expires_at is not None:
        run["expires_at"] = measurement.run_expires_at.isoformat()
    elif measurement.run_continuous:
        run.update(expires_at=None, continuous=True)
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
    continuous = raw.get("continuous") is True
    expires_at = _parse_context_instant(raw.get("expires_at"))
    if continuous:
        if (
            "expires_at" not in raw
            or raw["expires_at"] is not None
            or max_operations != 0
            or raw.get("source") != SOURCE_ADMIN
        ):
            return None
    elif expires_at is None:
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
        "continuous": continuous,
        "control_revision": control_revision,
        "source": str(raw.get("source") or SOURCE_ADMIN),
    }


def _manifest_for_continuation(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the manifest that lets this process continue a selection.

    The continuation arm exists because a selection may outlive the
    configuration that made it -- a child starting after a Sync, a sandbox manager
    answering a stage of an attempt that was chosen before a Disable. It is
    available **only under Admin control**, which is where "the configuration
    may change while work is in flight" is the whole point.

    ``disabled`` is a different statement. It means this deployment forbids
    collection outright, so a process configured that way must not be talked
    into collecting by an upstream context, however well formed -- otherwise a
    staged rollout or a deliberately silenced producer could be made to allocate
    recorders and measurements by whoever holds the sandbox manager token. Explicit
    disabled remains a deployment-level override.

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
    if manifest.get("continuous") is True and expires_at is None:
        return math.inf
    if not isinstance(expires_at, datetime):
        return None
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return None
    return clock().monotonic() + remaining


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
        run_continuous = manifest["continuous"]
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
        run_continuous = local.continuous
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
    if clock().monotonic() > deadline:
        return None
    started = context.get("started_mono_ns")
    try:
        started_mono_ns = int(started) if started is not None else clock().monotonic_ns()
    except (TypeError, ValueError):
        started_mono_ns = clock().monotonic_ns()
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
                or budget < 0
                or (budget == 0 and not run_continuous)
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
    if run_expires_at is None and not run_continuous:
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
        run_continuous=run_continuous,
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
    sandbox manager sees one call at a time and cannot tell which attempt it belongs to,
    so on its own it cannot know whether the operation was selected -- and a
    sandbox manager that kept measuring after the configuration moved on would report
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
      was kept, but the sandbox manager's stages went missing, which is worse than
      either.

    The continuation arm is safe because of the channel rather than a local
    re-check: a manifest-bearing context is produced by a control-plane or
    runner process that had already proved the selection, it reaches the sandbox manager
    over the sandbox manager's own shared-token channel, and the experiment it names must
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
    if clock().monotonic() > deadline:
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
    if "expires_at" in context and context["expires_at"] is None:
        manifest = _inherited_manifest(context)
        return math.inf if manifest is not None and manifest["continuous"] else None
    try:
        expires_at = float(str(context.get("expires_at")))
    except (TypeError, ValueError):
        return None
    remaining = expires_at - clock().time()
    if remaining <= 0:
        return None
    return clock().monotonic() + remaining


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
    if "expires_at" in body and body["expires_at"] is None:
        expires_at = None
    else:
        try:
            expires_at = float(str(body.get("expires_at")))
        except (TypeError, ValueError):
            return None
    parsed: dict[str, Any] = {
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
    # through the header boundary intact: a sandbox manager that received a selection it
    # cannot identify could only refuse it. ``_inherited_manifest`` is what
    # decides whether the object is usable, so this only bounds its size.
    run = body.get("run")
    if isinstance(run, Mapping):
        parsed["run"] = _bounded_context_run(run)
    return parsed


def _bounded_context_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded, deliberately unvalidated form of an inherited run.

    Unvalidated here on purpose: two different readers want different things
    from it -- a sandbox manager needs the operation list and the deadline, a child needs
    the whole identity -- and each validates what it uses. This bounds the
    values so a hostile or truncated value cannot become an unbounded record.
    """
    bounded: dict[str, Any] = {
        "run_id": _bounded_context_str(run.get("run_id"), MAX_RUN_ID_CHARS),
        "config_version": run.get("config_version"),
        "max_operations": run.get("max_operations"),
        "control_revision": run.get("control_revision"),
        "source": _bounded_context_str(run.get("source")),
        "expires_at": None
        if "expires_at" in run and run["expires_at"] is None
        else _bounded_context_str(run.get("expires_at")),
        **({"continuous": True} if run.get("continuous") is True else {}),
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
