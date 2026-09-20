"""Persisting a measurement this process rooted, without calling itself.

The runner delivers its measurements over HTTP, which is right for a producer
in another process. The control plane roots its own file and terminal requests
in-process, and routing those through its own endpoint would mean an HTTP call
from a service to itself: a second failure mode, a second authentication path
and a second serialization of a record it already holds.

So the checks and the write are one function, and both callers use it. What
they do *not* share is the failure handling: the endpoint may answer 4xx to the
producer, while a background write must be silent -- the file was read and the
terminal answered long before the write was attempted, and neither may be
failed by a diagnostic that could not be stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent.logging_utils import get_logger
from cloud_agent.measurement_ingest import (
    MeasurementRejected,
    require_record_matches_its_experiment,
    validate_measurement,
    validate_run,
)
from cloud_agent_profiling.operation_profiling import manifest_for_run as _manifest_for_run

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent.measurement_ingest import Owner
    from cloud_agent.ports.operation_measurements import (
        MeasurementWriteResult,
        OperationMeasurements,
    )

logger = get_logger(__name__)

#: What a persist attempt can report: the repository's own outcomes, plus the
#: one way this service declines before reaching it -- a record it will not
#: store. A store that *failed* is deliberately absent: the endpoint lets it
#: propagate so a producer can be told to try again, and the background caller
#: catches it, because by then nobody is left to tell.
PersistenceResult = Literal["stored", "duplicate", "conflict", "run_conflict", "rejected"]


@dataclass(frozen=True)
class PreparedMeasurement:
    """A validated record, ready for whoever gets to storing it first.

    ``payload`` is the record as it will be sent, kept so the queue can weigh
    it against its byte bound; ``row`` is what the repository writes.
    """

    row: dict[str, Any]
    run: dict[str, Any] | None
    payload: dict[str, Any]


def manifest_for_run(run: Any) -> dict[str, Any] | None:
    """Return the experiment manifest for a live run descriptor, or ``None``.

    Built from the descriptor this process already validated rather than from
    the environment again: two readings could disagree, and the manifest is an
    experiment's identity.
    """
    return _manifest_for_run(run)


def prepare_measurement(
    *,
    record: Mapping[str, Any],
    owner: Owner,
    run: Mapping[str, Any] | None,
    now: Any,
) -> tuple[PreparedMeasurement | None, str]:
    """Validate one record, returning what to store or why it is refused.

    Synchronous and cheap on purpose: a caller on the request path decides
    whether a record is worth queueing without touching the database, and the
    write itself happens later, on the bounded queue.
    """
    try:
        measured = validate_measurement(record, owner=owner)
        validated = validate_run(run) if run is not None else None
        require_record_matches_its_experiment(record=record, run=validated, now=now)
    except MeasurementRejected as exc:
        reason = str(exc)
        logger.warning(
            "operation_measurement_rejected",
            extra={"event": "operation_measurement_rejected", "reason": reason},
        )
        return None, reason
    return PreparedMeasurement(row=measured.as_row(), run=validated, payload=dict(record)), ""


async def store_prepared(
    capability: OperationMeasurements, prepared: PreparedMeasurement
) -> MeasurementWriteResult:
    """Write one prepared record, returning the store's outcome."""
    return await capability.record_operation_measurement(measurement=prepared.row, run=prepared.run)


async def persist_measurement(
    *,
    capability: OperationMeasurements,
    record: Mapping[str, Any],
    owner: Owner,
    run: Mapping[str, Any] | None,
    now: Any,
) -> tuple[PersistenceResult, str]:
    """Validate and store one record, reporting what happened.

    The direct path, for a caller that must know the answer before it replies:
    the delivery endpoint does, because a producer has to be told whether its
    record was kept. The control plane's own producers queue instead, so that
    congestion cannot reach the request that produced the record.
    """
    prepared, reason = prepare_measurement(record=record, owner=owner, run=run, now=now)
    if prepared is None:
        return "rejected", reason
    return await store_prepared(capability, prepared), ""


__all__ = [
    "PersistenceResult",
    "PreparedMeasurement",
    "manifest_for_run",
    "persist_measurement",
    "prepare_measurement",
    "store_prepared",
]
