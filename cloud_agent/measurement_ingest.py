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
from datetime import datetime, timezone
from typing import Any, Final

from cloud_agent_host.operation_profiling import (
    MAX_ATTRIBUTE_CHARS,
    MAX_REMOTE_STAGES,
    MAX_RUN_ID_CHARS,
    MAX_SPANS,
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
        "host",
        "worker_id",
        "job_id",
        "thread_id",
        "sandbox_id",
        "interaction_id",
        "request_kind",
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


def _bounded_count(record: Mapping[str, Any], key: str, limit: int) -> int:
    """Return a required non-negative count field, refusing one over its cap."""
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MeasurementRejected(f"{key} must be a non-negative integer")
    if value > limit:
        raise MeasurementRejected(f"{key} exceeds {limit}")
    return value


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
    outcome = _require(record, "outcome")
    if outcome not in KNOWN_OUTCOMES:
        raise MeasurementRejected(f"unknown outcome: {outcome}")
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

    startup_path = record.get("startup_path")
    sanitized = dict(record)
    sanitized["facts"] = sanitize_facts(record.get("facts"))
    resolved_owner = owner or Owner()

    return StoredMeasurement(
        measurement_id=_require(record, "measurement_id", limit=MAX_MEASUREMENT_ID_CHARS),
        # An operation with no interaction of its own is its own interaction,
        # which is what the collector states; a producer that omits the field
        # is taken at that same meaning rather than refused.
        interaction_id=str(
            record.get("interaction_id")
            or _require(record, "measurement_id", limit=MAX_MEASUREMENT_ID_CHARS)
        )[:MAX_MEASUREMENT_ID_CHARS],
        profiling_run_id=_require(record, "profiling_run_id", limit=MAX_RUN_ID_CHARS),
        schema_version=int(schema_version),
        operation=operation,
        request_kind=str(record.get("request_kind") or "")[:MAX_ATTRIBUTE_CHARS],
        boundary_version=int(boundary_version),
        coverage_scope=str(record.get("coverage_scope") or "")[:MAX_ATTRIBUTE_CHARS],
        producer=_require(record, "producer", limit=MAX_ATTRIBUTE_CHARS),
        producer_instance_id=str(record.get("producer_instance_id") or "")[:MAX_ATTRIBUTE_CHARS],
        duration_us=int(duration_us),
        outcome=outcome,
        collection_complete=collection_complete,
        end_to_end_complete=end_to_end_complete,
        startup_path=None if startup_path is None else str(startup_path)[:MAX_ATTRIBUTE_CHARS],
        observed_at=_timestamp(record, "observed_at"),
        record=sanitized,
        # Hashed over the record as it will be stored, so a retry of one record
        # compares equal and a producer that changed it does not.
        payload_hash=canonical_hash(sanitized),
        owner=resolved_owner,
    )


def validate_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an experiment manifest, and return what the catalogue stores.

    The boundaries are the collector's: a manifest this service would not have
    accepted as a descriptor cannot be stored as one either.
    """
    if not isinstance(run, Mapping):
        raise MeasurementRejected("run must be an object")
    run_id = _require(run, "run_id", limit=MAX_RUN_ID_CHARS)
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
    max_operations = run.get("max_operations", 1)
    if not isinstance(max_operations, int) or isinstance(max_operations, bool):
        raise MeasurementRejected("run.max_operations must be an integer")
    if max_operations <= 0:
        raise MeasurementRejected("run.max_operations must be positive")
    instances = run.get("admission_instances") or []
    if not isinstance(instances, list):
        raise MeasurementRejected("run.admission_instances must be a list")
    return {
        "run_id": run_id,
        "operations": operations,
        "target": {str(key): str(value) for key, value in target.items()},
        "expires_at": _timestamp(run, "expires_at"),
        "config_version": int(run.get("config_version") or 1),
        "admission_instances": [dict(item) for item in instances if isinstance(item, Mapping)],
    }


__all__ = [
    "ALLOWED_FACTS",
    "KNOWN_OPERATIONS",
    "KNOWN_OUTCOMES",
    "MAX_RECORD_BYTES",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MeasurementRejected",
    "Owner",
    "StoredMeasurement",
    "canonical_hash",
    "sanitize_facts",
    "validate_measurement",
    "validate_run",
]
