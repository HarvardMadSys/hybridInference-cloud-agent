"""Turning a producer's frozen measurement into a storable row, or refusing it.

The producer sends a record it has already frozen. The control plane's job is
not to re-derive it but to decide whether it may be stored at all, and to state
the parts the producer is not allowed to decide for itself.

Three rules shape everything here:

* **The producer states what it measured; the control plane states who owns
  it.** ``owner_id``, and the ownership of the job and attempt, come from the
  database row this request authenticated against -- never from the payload.
  A record that could name its own owner could be filed under someone else's
  history by anyone holding a valid token for their own job.
* **Bounds are the collector's, not a second set.** The caps on stages, spans,
  fragments and attribute length are imported from the producer that enforced
  them, so a record that passes there cannot be refused here for disagreeing
  with a copy of the same number.
* **Facts are a whitelist, not a blacklist.** A denylist would have to
  anticipate every name a future producer might use for something sensitive;
  a whitelist refuses the unknown by default, which is the only direction that
  fails closed. File contents, paths, prompts, terminal bytes and tokens never
  reach the table because their names are not on the list.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from cloud_agent_shared.operation_profiling import (
    BOUNDARIES_BY_OPERATION,
    COVERAGE_HOST_STARTUP,
    COVERAGE_SERVER_REQUEST,
    MAX_ATTRIBUTE_CHARS,
    MAX_REMOTE_STAGES,
    MAX_RUN_ID_CHARS,
    MAX_SPANS,
    RUN_ID_PATTERN,
    STARTUP_OPERATION,
    TARGET_KEYS,
)

#: Longest record accepted, in bytes. The collector's own ceilings bound the
#: counts; this bounds the whole document, including anything a future field
#: might add that no per-collection cap covers.
MAX_RECORD_BYTES: Final = 64 * 1024

#: Longest a stored measurement id may be. Matches the minted shape with room.
MAX_MEASUREMENT_ID_CHARS: Final = 64

#: Schema versions this ingest understands. An unknown version is refused
#: rather than interpreted as the nearest one: a record whose meaning is not
#: known cannot be summarised honestly.
SUPPORTED_SCHEMA_VERSIONS: Final = frozenset({3})

#: Facts a stored record may keep. Everything here is a name, a count, a
#: classification or an identifier the platform minted. Notably absent:
#: anything holding file content or a path, terminal input or output, a prompt,
#: a credential, or an environment value.
ALLOWED_FACTS: Final = frozenset(
    {
        "operation",
        "outcome",
        "startup_outcome",
        "failure_stage",
        "startup_path",
        "container_action",
        "resident_action",
        "context_status",
        "workspace_action",
        "runtime_id",
        "runtime_version",
        "execution_profile",
        "host",
        "worker_id",
        "job_id",
        "thread_id",
        "sandbox_id",
        "interaction_id",
        "request_kind",
        "coverage_scope",
        "boundary_version",
        "kind",
        "entry_count",
        "content_truncated",
        "binary",
        "terminal_count",
        "profiling_stopped",
        "continued_requests",
    }
)

#: The operations a stored record may name. A record for something else is a
#: producer this deployment does not know, and its samples would land in a
#: distribution that has no boundary version for them.
KNOWN_OPERATIONS: Final = frozenset(
    {
        "attempt.startup",
        "files.list",
        "files.open",
        "terminal.open",
    }
)

#: Outcomes a record may report, mirroring the collector's vocabulary.
KNOWN_OUTCOMES: Final = frozenset(
    {
        "dispatched",
        "failed",
        "cancelled",
        "lease_lost",
        "incomplete",
        "completed",
        "superseded",
    }
)


#: Every top-level field a stored record may carry. A record is reduced to
#: these, so a producer cannot add a field this deployment does not know -- and
#: the reduction is what keeps the promise that no prompt, file body or
#: credential reaches the table, however a future producer names it.
KNOWN_RECORD_KEYS: Final = frozenset(
    {
        "schema_version",
        "measurement_id",
        "interaction_id",
        "profiling_run_id",
        "operation",
        "request_kind",
        "boundary_version",
        "coverage_scope",
        "start_boundary",
        "end_boundary",
        "producer",
        "producer_instance_id",
        "duration_us",
        "outcome",
        "collection_complete",
        "end_to_end_complete",
        "measurement_complete",
        "startup_path",
        "startup_outcome",
        "failure_stage",
        "observed_at",
        "recorded_at",
        "facts",
        "stages",
        "spans",
        "milestones",
        "missing_fragments",
        "missing_producers",
        "truncated_fragments",
        "dropped_spans",
        "truncated",
        "profiling_stopped",
        "producers",
        "timing_sources",
        # Which clock domain the record's offsets are stated in. Identity, not
        # decoration: offsets from two domains must never be compared.
        "clock_domain_id",
        "boundary_frozen",
        "run_expires_at",
        # Which control source rooted this measurement, and -- for a run an
        # administrator started -- the deployment revision that authorized it.
        # Both are identity rather than observation: a reader uses them to tell
        # a static-descriptor experiment from a commanded one, and two
        # experiments that happen to reuse a run id apart.
        "run_source",
        "control_revision",
        "container_action",
        "resident_action",
        "context_status",
        "workspace_action",
        "runtime_id",
        "runtime_version",
        "execution_profile",
    }
)

#: Fields a *stage* or *span* may carry. A producer that puts a credential in
#: a nested object is refused rather than having it silently dropped, because
#: the two are different mistakes: an unknown top-level field is usually a
#: newer producer, while an unknown nested one is usually something that
#: should never have been serialized at all.
KNOWN_SPAN_KEYS: Final = frozenset(
    {
        "span_id",
        "parent_span_id",
        "name",
        "stage",
        "ordinal",
        "start_offset_us",
        "duration_us",
        "outcome",
        "truncated",
        "attributes",
        "children",
        "source",
        "call_id",
    }
)

#: Fields a *milestone* may carry. A milestone is a named instant on the
#: producer's own timeline, so it has neither a duration nor children -- the
#: separate list is what stops a stage-shaped object being accepted here.
KNOWN_MILESTONE_KEYS: Final = frozenset({"name", "offset_us", "source"})

#: Longest nesting a stage list may reach. A record is bounded in bytes too,
#: but a deeply nested one can be small and still expensive to walk.
MAX_STAGE_DEPTH: Final = 4


#: The producer name this process writes when it roots an operation itself.
#: Named here because two places have to agree on it: the ingest, which exempts
#: it from stating a manifest, and the control plane, which writes it.
CONTROL_PLANE_PRODUCER: Final = "control-plane"


class MeasurementRejected(ValueError):
    """A record that may not be stored, with the reason it was refused."""


@dataclass
class Owner:
    """The attribution the control plane resolved, not the payload's claims."""

    owner_id: str | None = None
    job_id: str | None = None
    thread_id: str | None = None
    sandbox_id: str | None = None
    host_id: str | None = None
    attempt_id: int | None = None
    lease_generation: int | None = None


@dataclass
class StoredMeasurement:
    """One validated measurement, ready for the repository."""

    measurement_id: str
    interaction_id: str
    profiling_run_id: str
    schema_version: int
    operation: str
    request_kind: str
    boundary_version: int
    coverage_scope: str
    producer: str
    producer_instance_id: str
    duration_us: int
    outcome: str
    collection_complete: bool
    end_to_end_complete: bool
    startup_path: str | None
    runtime_id: str | None
    runtime_version: str | None
    execution_profile: str | None
    observed_at: datetime
    record: dict[str, Any]
    payload_hash: str
    owner: Owner = field(default_factory=Owner)

    def as_row(self) -> dict[str, Any]:
        """Return the column values the repository inserts."""
        return {
            "measurement_id": self.measurement_id,
            "interaction_id": self.interaction_id,
            "profiling_run_id": self.profiling_run_id,
            "schema_version": self.schema_version,
            "owner_id": self.owner.owner_id,
            "job_id": self.owner.job_id,
            "thread_id": self.owner.thread_id,
            "sandbox_id": self.owner.sandbox_id,
            "host_id": self.owner.host_id,
            "attempt_id": self.owner.attempt_id,
            "lease_generation": self.owner.lease_generation,
            "operation": self.operation,
            "request_kind": self.request_kind,
            "boundary_version": self.boundary_version,
            "coverage_scope": self.coverage_scope,
            "producer": self.producer,
            "producer_instance_id": self.producer_instance_id,
            "duration_us": self.duration_us,
            "outcome": self.outcome,
            "collection_complete": self.collection_complete,
            "end_to_end_complete": self.end_to_end_complete,
            "startup_path": self.startup_path,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "execution_profile": self.execution_profile,
            "observed_at": self.observed_at,
            "record": self.record,
            "payload_hash": self.payload_hash,
        }


def canonical_hash(record: Mapping[str, Any]) -> str:
    """Return the hash that makes a retry idempotent.

    Over the canonical serialization -- sorted keys, no insignificant
    whitespace -- so two spellings of one document hash alike, which is what
    lets the repository tell a retry from a conflict.
    """
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require(record: Mapping[str, Any], key: str, *, limit: int | None = None) -> str:
    """Return a required non-empty string field, bounded when a limit is given."""
    value = record.get(key)
    text = "" if value is None else str(value).strip()
    if not text:
        raise MeasurementRejected(f"{key} is required")
    if limit is not None and len(text) > limit:
        raise MeasurementRejected(f"{key} is longer than {limit} characters")
    return text


def _require_run_id(record: Mapping[str, Any], key: str = "run_id") -> str:
    """Return a bounded run id that is safe as an Admin path segment."""
    run_id = _require(record, key, limit=MAX_RUN_ID_CHARS)
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise MeasurementRejected(f"{key} must contain only letters, numbers, '.', '_', ':' or '-'")
    return run_id


def _require_measurement_id(record: Mapping[str, Any]) -> str:
    """Return a measurement id addressable by the Admin detail route."""
    measurement_id = _require(record, "measurement_id", limit=MAX_MEASUREMENT_ID_CHARS)
    if RUN_ID_PATTERN.fullmatch(measurement_id) is None:
        raise MeasurementRejected(
            "measurement_id must contain only letters, numbers, '.', '_', ':' or '-'"
        )
    return measurement_id


def _bounded_count(record: Mapping[str, Any], key: str, limit: int) -> int:
    """Return a required non-negative count field, refusing one over its cap."""
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MeasurementRejected(f"{key} must be a non-negative integer")
    if value > limit:
        raise MeasurementRejected(f"{key} exceeds {limit}")
    return value


def _from_either(record: Mapping[str, Any], key: str) -> Any:
    """Return a field from the top level, or from facts when it is only there.

    The collector states some fields both ways -- a startup path and a request
    kind are facts about the operation *and* the dimensions a distribution is
    grouped by -- and a reader that only looked at the top level would file
    every startup path as null and every terminal request kind as empty. The
    fallback is one-directional on purpose: facts never override a stated
    top-level field.
    """
    value = record.get(key)
    if value is not None and value != "":
        return value
    facts = record.get("facts")
    if isinstance(facts, Mapping):
        return facts.get(key)
    return value


def _strip_unknown(value: Any, allowed: frozenset[str], *, where: str, depth: int = 0) -> Any:
    """Return ``value`` reduced to ``allowed`` keys, refusing unknown ones.

    Refused rather than dropped. A field this deployment does not know is
    either a producer newer than the schema -- which the version check already
    caught -- or content that was never supposed to be serialized, and silently
    discarding the second would leave the producer believing it had been
    stored.
    """
    if not isinstance(value, Mapping):
        return value
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise MeasurementRejected(f"unknown {where} field(s): {', '.join(unknown)}")
    if depth >= MAX_STAGE_DEPTH:
        raise MeasurementRejected(f"{where} nests deeper than {MAX_STAGE_DEPTH}")
    reduced: dict[str, Any] = {}
    for key, item in value.items():
        if key == "children":
            if item is None:
                continue
            if not isinstance(item, list):
                raise MeasurementRejected("children must be a list")
            reduced[key] = [
                _strip_unknown(child, allowed, where=where, depth=depth + 1) for child in item
            ]
        elif key == "attributes":
            # Attributes are free-form by design, but bounded: they are the
            # one place a producer may attach a value whose name this
            # deployment cannot enumerate.
            if isinstance(item, Mapping):
                reduced[key] = {
                    str(name)[:MAX_ATTRIBUTE_CHARS]: _bounded_value(attribute)
                    for name, attribute in item.items()
                }
        else:
            reduced[key] = item
    return reduced


def _bounded_value(value: Any) -> Any:
    """Return an attribute value this table has a shape for."""
    if isinstance(value, str):
        return value[:MAX_ATTRIBUTE_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_ATTRIBUTE_CHARS]


def _timestamp(record: Mapping[str, Any], key: str) -> datetime:
    """Return a required ISO-8601 timestamp, normalized to UTC."""
    text = _require(record, key)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MeasurementRejected(f"{key} is not an ISO-8601 timestamp") from exc
    if moment.tzinfo is None:
        raise MeasurementRejected(f"{key} has no timezone")
    return moment.astimezone(timezone.utc)


def sanitize_facts(facts: Any) -> dict[str, Any]:
    """Return the facts a record may keep, and only those.

    An unknown name is dropped rather than stored. That direction matters: a
    future producer adding a field called ``prompt`` would have it silently
    kept by a denylist and silently refused by this, and only one of those
    failures is recoverable.
    """
    if not isinstance(facts, Mapping):
        return {}
    kept: dict[str, Any] = {}
    for key, value in facts.items():
        name = str(key)
        if name not in ALLOWED_FACTS:
            continue
        if isinstance(value, str):
            kept[name] = value[:MAX_ATTRIBUTE_CHARS]
        elif value is None or isinstance(value, (bool, int, float)):
            kept[name] = value
        # Anything else -- a list, a dict, an object -- is not a fact this
        # table has a shape for, so it is dropped rather than stringified into
        # something that looks like data and is not.
    return kept


def _validate_structure(record: Mapping[str, Any]) -> None:
    """Refuse a record whose collections are missing, malformed or oversized."""
    for key, limit in (
        ("stages", MAX_SPANS),
        ("spans", MAX_SPANS),
        ("milestones", MAX_SPANS),
        ("missing_fragments", MAX_REMOTE_STAGES),
        ("missing_producers", MAX_REMOTE_STAGES),
        ("truncated_fragments", MAX_REMOTE_STAGES),
    ):
        value = record.get(key, [])
        if not isinstance(value, list):
            raise MeasurementRejected(f"{key} must be a list")
        if len(value) > limit:
            raise MeasurementRejected(f"{key} exceeds {limit} entries")
    for key in ("stages", "spans"):
        for item in record.get(key) or []:
            if not isinstance(item, Mapping):
                raise MeasurementRejected(f"{key} entries must be objects")
    for item in record.get("stages") or []:
        _validate_stage(item, depth=0)
    for item in record.get("spans") or []:
        _validate_span(item)
    for item in record.get("milestones") or []:
        _validate_milestone(item)
    for key in (
        "missing_fragments",
        "missing_producers",
        "truncated_fragments",
        "producers",
        "timing_sources",
    ):
        value = record.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise MeasurementRejected(f"{key} must be a list of non-empty strings")


def _non_negative_int(value: Any, *, name: str, optional: bool = False) -> None:
    """Validate one integer timing field without accepting booleans."""
    if optional and value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MeasurementRejected(f"{name} must be a non-negative integer")


def _non_empty_text(value: Any, *, name: str, optional: bool = False) -> None:
    """Validate one textual timing identity."""
    if optional and value is None:
        return
    if not isinstance(value, str) or not value:
        raise MeasurementRejected(f"{name} must be a non-empty string")


def _validate_stage(item: Mapping[str, Any], *, depth: int) -> None:
    """Validate one local or attached stage recursively."""
    if depth > MAX_STAGE_DEPTH:
        raise MeasurementRejected(f"stage nests deeper than {MAX_STAGE_DEPTH}")
    _non_empty_text(item.get("stage"), name="stage.stage")
    _non_negative_int(item.get("duration_us"), name="stage.duration_us")
    _non_negative_int(item.get("start_offset_us"), name="stage.start_offset_us", optional=True)
    _non_empty_text(item.get("outcome"), name="stage.outcome", optional=True)
    _non_empty_text(item.get("source"), name="stage.source", optional=True)
    _non_empty_text(item.get("call_id"), name="stage.call_id", optional=True)
    if "truncated" in item and not isinstance(item["truncated"], bool):
        raise MeasurementRejected("stage.truncated must be a boolean")
    children = item.get("children", [])
    if children is None or not isinstance(children, list):
        raise MeasurementRejected("stage.children must be a list")
    if len(children) > MAX_REMOTE_STAGES:
        raise MeasurementRejected(f"stage.children exceeds {MAX_REMOTE_STAGES} entries")
    for child in children:
        if not isinstance(child, Mapping):
            raise MeasurementRejected("stage children must be objects")
        _validate_stage(child, depth=depth + 1)


def _validate_span(item: Mapping[str, Any]) -> None:
    """Validate one local span."""
    _non_empty_text(item.get("span_id"), name="span.span_id")
    _non_empty_text(item.get("name"), name="span.name")
    _non_negative_int(item.get("ordinal"), name="span.ordinal")
    _non_negative_int(item.get("start_offset_us"), name="span.start_offset_us")
    _non_negative_int(item.get("duration_us"), name="span.duration_us", optional=True)
    _non_empty_text(item.get("outcome"), name="span.outcome")
    _non_empty_text(item.get("parent_span_id"), name="span.parent_span_id", optional=True)
    if "truncated" in item and not isinstance(item["truncated"], bool):
        raise MeasurementRejected("span.truncated must be a boolean")
    if "attributes" in item and not isinstance(item["attributes"], Mapping):
        raise MeasurementRejected("span.attributes must be an object")


def _validate_milestone(item: Any) -> None:
    """Validate one milestone's name, offset, and source."""
    if not isinstance(item, Mapping):
        raise MeasurementRejected("milestones entries must be objects")
    _non_empty_text(item.get("name"), name="milestone.name")
    _non_negative_int(item.get("offset_us"), name="milestone.offset_us")
    _non_empty_text(item.get("source"), name="milestone.source")


def _contains_truncated(items: Any) -> bool:
    """Return whether a nested stage or span says its duration was truncated."""
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("truncated") is True or _contains_truncated(item.get("children")):
            return True
    return False


def _validate_completeness(
    record: Mapping[str, Any],
    *,
    outcome: str,
    collection_complete: bool,
    end_to_end_complete: bool,
) -> None:
    """Reject records whose summary flag contradicts their own evidence."""
    alias = record.get("measurement_complete")
    if alias is not None:
        if not isinstance(alias, bool):
            raise MeasurementRejected("measurement_complete must be a boolean")
        if alias != collection_complete:
            raise MeasurementRejected("measurement_complete contradicts collection_complete")
    truncated = record.get("truncated", False)
    if not isinstance(truncated, bool):
        raise MeasurementRejected("truncated must be a boolean")
    boundary_frozen = record.get("boundary_frozen")
    if boundary_frozen is not None and not isinstance(boundary_frozen, bool):
        raise MeasurementRejected("boundary_frozen must be a boolean")
    dropped = record.get("dropped_spans", 0)
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
        raise MeasurementRejected("dropped_spans must be a non-negative integer")
    if dropped > MAX_SPANS:
        raise MeasurementRejected(f"dropped_spans exceeds {MAX_SPANS}")
    if end_to_end_complete and not collection_complete:
        raise MeasurementRejected(
            "end_to_end_complete cannot be true when collection is incomplete"
        )
    if not collection_complete:
        return
    contradictory: list[str] = []
    if outcome == "incomplete":
        contradictory.append("incomplete outcome")
    if (
        truncated
        or _contains_truncated(record.get("stages"))
        or _contains_truncated(record.get("spans"))
    ):
        contradictory.append("truncated timing")
    if dropped:
        contradictory.append("dropped spans")
    if boundary_frozen is False:
        contradictory.append("unfrozen boundary")
    for key in ("missing_fragments", "missing_producers", "truncated_fragments"):
        if record.get(key):
            contradictory.append(key)
    if _from_either(record, "profiling_stopped"):
        contradictory.append("profiling_stopped")
    if contradictory:
        raise MeasurementRejected("collection_complete contradicts " + ", ".join(contradictory))


def validate_measurement(
    record: Mapping[str, Any], *, owner: Owner | None = None
) -> StoredMeasurement:
    """Validate one producer record and return the row it becomes.

    Args:
        record: The frozen record, as emitted.
        owner: The attribution resolved from the authenticated request. Keys
            the payload states for itself are ignored: they are what the
            producer *thinks* it belongs to, and only the database knows.

    Raises:
        MeasurementRejected: the record is not one this deployment stores.
    """
    if not isinstance(record, Mapping):
        raise MeasurementRejected("record must be an object")
    encoded = json.dumps(record, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise MeasurementRejected(f"record exceeds {MAX_RECORD_BYTES} bytes")

    schema_version = record.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        # Named explicitly, because "we do not know what this means" and "this
        # is malformed" call for different responses from the producer.
        raise MeasurementRejected(f"unsupported schema_version: {schema_version!r}")

    operation = _require(record, "operation")
    if operation not in KNOWN_OPERATIONS:
        raise MeasurementRejected(f"unknown operation: {operation}")
    producer = _require(record, "producer", limit=MAX_ATTRIBUTE_CHARS)
    coverage_scope = str(_from_either(record, "coverage_scope") or "")
    expected_producer = "runner" if operation == STARTUP_OPERATION else CONTROL_PLANE_PRODUCER
    expected_scope = (
        COVERAGE_HOST_STARTUP if operation == STARTUP_OPERATION else COVERAGE_SERVER_REQUEST
    )
    if producer != expected_producer:
        raise MeasurementRejected(
            f"operation {operation!r} must be rooted by {expected_producer!r}"
        )
    if coverage_scope != expected_scope:
        raise MeasurementRejected(
            f"operation {operation!r} must use coverage_scope {expected_scope!r}"
        )
    request_kind = str(_from_either(record, "request_kind") or "")
    allowed_request_kinds = {
        "attempt.startup": {"attempt.startup"},
        "files.list": {"files.list"},
        "files.open": {"files.open"},
        "terminal.open": {
            "terminal.list",
            "terminal.create",
            "terminal.stream_attach",
        },
    }
    if request_kind not in allowed_request_kinds[operation]:
        raise MeasurementRejected(
            f"operation {operation!r} cannot use request_kind {request_kind!r}"
        )
    expected_boundaries = BOUNDARIES_BY_OPERATION.get(operation)
    if expected_boundaries is not None:
        start_boundary = record.get("start_boundary")
        end_boundary = record.get("end_boundary")
        if start_boundary != expected_boundaries[0]:
            raise MeasurementRejected("start_boundary does not match the operation")
        if end_boundary != expected_boundaries[1]:
            raise MeasurementRejected("end_boundary does not match the operation")
    outcome = _require(record, "outcome")
    if outcome not in KNOWN_OUTCOMES:
        raise MeasurementRejected(f"unknown outcome: {outcome}")
    allowed_outcomes = (
        {"dispatched", "failed", "cancelled", "lease_lost", "incomplete"}
        if operation == STARTUP_OPERATION
        else {"completed", "failed", "cancelled", "incomplete", "superseded"}
    )
    if outcome not in allowed_outcomes:
        raise MeasurementRejected(f"operation {operation!r} cannot use outcome {outcome!r}")
    _validate_structure(record)

    duration_us = record.get("duration_us")
    if not isinstance(duration_us, int) or isinstance(duration_us, bool):
        raise MeasurementRejected("duration_us must be an integer")
    if duration_us < 0:
        raise MeasurementRejected("duration_us must not be negative")

    boundary_version = record.get("boundary_version")
    if not isinstance(boundary_version, int) or isinstance(boundary_version, bool):
        raise MeasurementRejected("boundary_version must be an integer")

    collection_complete = record.get("collection_complete")
    if not isinstance(collection_complete, bool):
        raise MeasurementRejected("collection_complete must be a boolean")
    end_to_end_complete = record.get("end_to_end_complete", False)
    if not isinstance(end_to_end_complete, bool):
        raise MeasurementRejected("end_to_end_complete must be a boolean")
    if end_to_end_complete:
        raise MeasurementRejected("server-side schema v3 records cannot claim end-to-end coverage")
    _validate_completeness(
        record,
        outcome=outcome,
        collection_complete=collection_complete,
        end_to_end_complete=end_to_end_complete,
    )

    startup_path = _from_either(record, "startup_path")
    runtime_id = _from_either(record, "runtime_id")
    runtime_version = _from_either(record, "runtime_version")
    execution_profile = _from_either(record, "execution_profile")
    # Unknown top-level fields are refused, not dropped. Silently discarding
    # one would leave the producer believing it had been stored, and the field
    # it chose might be exactly the content this promise is about.
    unknown = sorted(str(key) for key in record if str(key) not in KNOWN_RECORD_KEYS)
    if unknown:
        raise MeasurementRejected(f"unknown record field(s): {', '.join(unknown)}")
    sanitized = dict(record)
    sanitized["facts"] = sanitize_facts(record.get("facts"))
    for key, allowed in (
        ("stages", KNOWN_SPAN_KEYS),
        ("spans", KNOWN_SPAN_KEYS),
        ("milestones", KNOWN_MILESTONE_KEYS),
    ):
        if key in sanitized and isinstance(sanitized[key], list):
            sanitized[key] = [
                _strip_unknown(item, allowed, where=f"{key} entry") for item in sanitized[key]
            ]
    resolved_owner = owner or Owner()

    return StoredMeasurement(
        measurement_id=_require_measurement_id(record),
        # An operation with no interaction of its own is its own interaction,
        # which is what the collector states; a producer that omits the field
        # is taken at that same meaning rather than refused.
        interaction_id=str(record.get("interaction_id") or _require_measurement_id(record))[
            :MAX_MEASUREMENT_ID_CHARS
        ],
        profiling_run_id=_require_run_id(record, "profiling_run_id"),
        schema_version=int(schema_version),
        operation=operation,
        request_kind=request_kind[:MAX_ATTRIBUTE_CHARS],
        boundary_version=int(boundary_version),
        coverage_scope=coverage_scope[:MAX_ATTRIBUTE_CHARS],
        producer=producer,
        producer_instance_id=str(record.get("producer_instance_id") or "")[:MAX_ATTRIBUTE_CHARS],
        duration_us=int(duration_us),
        outcome=outcome,
        collection_complete=collection_complete,
        end_to_end_complete=end_to_end_complete,
        startup_path=None if startup_path is None else str(startup_path)[:MAX_ATTRIBUTE_CHARS],
        runtime_id=None if runtime_id is None else str(runtime_id)[:MAX_ATTRIBUTE_CHARS],
        runtime_version=(
            None if runtime_version is None else str(runtime_version)[:MAX_ATTRIBUTE_CHARS]
        ),
        execution_profile=(
            None if execution_profile is None else str(execution_profile)[:MAX_ATTRIBUTE_CHARS]
        ),
        observed_at=_timestamp(record, "observed_at"),
        record=sanitized,
        # Hashed over the record as it will be stored, so a retry of one record
        # compares equal and a producer that changed it does not.
        payload_hash=canonical_hash(sanitized),
        owner=resolved_owner,
    )


#: How long after an experiment ends a producer may still deliver what it
#: collected. Delivery is asynchronous, and the samples worth having are often
#: from attempts that failed immediately -- so a short grace period is part of
#: the contract rather than a loosening of it. It also bounds how long a
#: stopped experiment can keep growing: past this, the record is refused.
DELIVERY_GRACE_S: Final = 600


def require_record_matches_its_experiment(
    *, record: Mapping[str, Any], run: Mapping[str, Any] | None, now: datetime
) -> None:
    """Refuse a record that does not belong to the experiment it arrived with.

    Two checks, both of which have to hold for a stored row to be readable
    later:

    * the record names the run it is being filed under, so a sample cannot
      land in another experiment's distribution; and
    * the operation is one that run selected, so a sample cannot appear in a
      series the experiment never asked to measure.

    The deadline is checked here rather than at the repository because it is a
    property of the arrival, not of the row: a retry that arrives in time is
    fine, and one that arrives after the grace period is refused however
    often it is sent.
    """
    if run is None:
        # A producer that sent no manifest is contributing to a run this
        # deployment has no catalogue entry for; the repository stores it and
        # the catalogue reports it as missing rather than inventing one.
        return
    named = str(record.get("profiling_run_id") or "").strip()
    if named != str(run["run_id"]):
        raise MeasurementRejected(
            f"record belongs to run {named!r}, not the manifest's {run['run_id']!r}"
        )
    operation = str(_from_either(record, "operation") or "").strip()
    if operation and operation not in {str(item) for item in run["operations"]}:
        raise MeasurementRejected(f"run does not select operation {operation!r}")
    expires_at = run["expires_at"]
    if expires_at is not None and now > expires_at + timedelta(seconds=DELIVERY_GRACE_S):
        raise MeasurementRejected(
            f"experiment ended more than {DELIVERY_GRACE_S // 60} minutes before this delivery"
        )


def validate_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an experiment manifest, and return what the catalogue stores.

    The boundaries are the collector's: a manifest this service would not have
    accepted as a descriptor cannot be stored as one either.
    """
    if not isinstance(run, Mapping):
        raise MeasurementRejected("run must be an object")
    run_id = _require_run_id(run)
    raw_operations = run.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise MeasurementRejected("run.operations must be a non-empty list")
    operations = [str(item) for item in raw_operations]
    unknown = sorted(name for name in operations if name not in KNOWN_OPERATIONS)
    if unknown:
        raise MeasurementRejected(f"run names unknown operations: {', '.join(unknown)}")
    target = run.get("target") or {}
    if not isinstance(target, Mapping):
        raise MeasurementRejected("run.target must be an object")
    unknown_targets = sorted(str(key) for key in target if str(key) not in TARGET_KEYS)
    if unknown_targets:
        raise MeasurementRejected(
            f"run.target names unknown field(s): {', '.join(unknown_targets)}"
        )
    normalized_target: dict[str, str] = {}
    for key, value in target.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            raise MeasurementRejected(f"run.target.{key} must be a non-empty string or integer")
        normalized_target[str(key)] = str(value)
    continuous = run.get("continuous") is True
    if continuous and ("expires_at" not in run or run["expires_at"] is not None):
        raise MeasurementRejected("continuous runs require explicit null expires_at")
    max_operations = run.get("max_operations", 1)
    if not isinstance(max_operations, int) or isinstance(max_operations, bool):
        raise MeasurementRejected("run.max_operations must be an integer")
    if (continuous and max_operations != 0) or (not continuous and max_operations <= 0):
        raise MeasurementRejected(
            "run.max_operations must be zero if continuous, positive otherwise"
        )
    instances = run.get("admission_instances") or []
    if not isinstance(instances, list):
        raise MeasurementRejected("run.admission_instances must be a list")
    if len(instances) > MAX_REMOTE_STAGES:
        raise MeasurementRejected(f"run.admission_instances exceeds {MAX_REMOTE_STAGES} entries")
    normalized_instances: list[dict[str, Any]] = []
    for item in instances:
        if not isinstance(item, Mapping):
            raise MeasurementRejected("run.admission_instances entries must be objects")
        instance = item.get("instance")
        budget = item.get("max_operations")
        if not isinstance(instance, str) or not instance or len(instance) > MAX_ATTRIBUTE_CHARS:
            raise MeasurementRejected("run admission instance must be a bounded string")
        if (
            not isinstance(budget, int)
            or isinstance(budget, bool)
            or budget < 0
            or (budget == 0 and not continuous)
        ):
            raise MeasurementRejected("run admission max_operations must be a positive integer")
        normalized_instances.append({"instance": instance, "max_operations": budget})
    config_version = run.get("config_version", 1)
    if (
        not isinstance(config_version, int)
        or isinstance(config_version, bool)
        or config_version <= 0
    ):
        raise MeasurementRejected("run.config_version must be a positive integer")
    return {
        "run_id": run_id,
        "operations": operations,
        "target": normalized_target,
        "expires_at": None if continuous else _timestamp(run, "expires_at"),
        "config_version": config_version,
        "admission_instances": normalized_instances,
    }


__all__ = [
    "ALLOWED_FACTS",
    "CONTROL_PLANE_PRODUCER",
    "DELIVERY_GRACE_S",
    "KNOWN_MILESTONE_KEYS",
    "KNOWN_OPERATIONS",
    "KNOWN_OUTCOMES",
    "KNOWN_RECORD_KEYS",
    "KNOWN_SPAN_KEYS",
    "MAX_RECORD_BYTES",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MeasurementRejected",
    "Owner",
    "StoredMeasurement",
    "canonical_hash",
    "require_record_matches_its_experiment",
    "sanitize_facts",
    "validate_measurement",
    "validate_run",
]
