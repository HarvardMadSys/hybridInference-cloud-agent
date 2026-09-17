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

from typing import TYPE_CHECKING, Any, Literal

from cloud_agent.logging_utils import get_logger
from cloud_agent.measurement_ingest import (
    MeasurementRejected,
    require_record_matches_its_experiment,
    validate_measurement,
    validate_run,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent.measurement_ingest import Owner
    from cloud_agent.ports.operation_measurements import OperationMeasurements

logger = get_logger(__name__)

#: What a persist attempt can report: the repository's own outcomes, plus the
#: one way this service declines before reaching it -- a record it will not
#: store. A store that *failed* is deliberately absent: the endpoint lets it
#: propagate so a producer can be told to try again, and the background caller
#: catches it, because by then nobody is left to tell.
PersistenceResult = Literal["stored", "duplicate", "conflict", "run_conflict", "rejected"]


def manifest_for_run(run: Any) -> dict[str, Any] | None:
    """Return the experiment manifest for a live run descriptor, or ``None``.

    Built from the descriptor this process already validated rather than from
    the environment again: two readings could disagree, and the manifest is an
    experiment's identity.
    """
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "operations": sorted(run.operations),
        "target": dict(run.target),
        "expires_at": run.expires_at_utc.isoformat(),
        "config_version": 1,
        "admission_instances": [],
    }


async def persist_measurement(
    *,
    capability: OperationMeasurements,
    record: Mapping[str, Any],
    owner: Owner,
    run: Mapping[str, Any] | None,
    now: Any,
) -> tuple[PersistenceResult, str]:
    """Validate and store one record, reporting what happened.

    Returns the repository's outcome -- or ``"rejected"`` with the reason it
    was refused, when the record is not one this deployment stores. It never raises for a bad *record* -- that is a
    judgement about the payload, and both callers act on the result -- but a
    failing *store* propagates, so the endpoint can answer 5xx and a delivery
    policy can retry it.
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
        # The reason travels back with the refusal. A producer that is told
        # only "rejected" cannot tell a schema it does not speak from a field
        # it should not have sent, and this is a diagnostic surface.
        return "rejected", reason
    stored = await capability.record_operation_measurement(
        measurement=measured.as_row(), run=validated
    )
    return stored, ""


__all__ = ["PersistenceResult", "manifest_for_run", "persist_measurement"]
