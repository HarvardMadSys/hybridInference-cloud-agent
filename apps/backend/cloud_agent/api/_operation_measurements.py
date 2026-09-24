"""Shared HTTP helpers for opt-in file and terminal measurements.

The owner routes root these measurements in the control-plane process.  This
module keeps their timing, response metadata, and bounded persistence plumbing
beside the workspace surface without putting those concerns back into the
top-level route aggregator.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi.responses import JSONResponse

from cloud_agent import measurement_writer
from cloud_agent.logging_utils import get_logger
from cloud_agent.measurement_ingest import Owner
from cloud_agent.measurement_persistence import prepare_measurement, store_prepared
from cloud_agent.schemas import AgentOperationProfilingFragment
from cloud_agent_shared.operation_profiling import manifest_for_measurement

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Response

    from cloud_agent.operation_measurement import Selection
    from cloud_agent.ports.operation_measurements import OperationMeasurements

logger = get_logger(__name__)

_T = TypeVar("_T")

#: The local stage enclosing one workspace read.  The result later classifies
#: the operation as a directory listing or file open, but the clock starts
#: before that result exists.
FILE_READ_STAGE = "read"


def _owner(job: Mapping[str, Any]) -> Owner:
    """Return trusted attribution from the job already authorized by the route."""
    sandbox = job.get("thread_sandbox")
    sandbox = sandbox if isinstance(sandbox, Mapping) else {}
    return Owner(
        owner_id=str(job.get("user_id") or "") or None,
        job_id=str(job.get("id") or "") or None,
        thread_id=str(job.get("thread_id") or "") or None,
        sandbox_id=str(sandbox.get("sandbox_id") or "") or None,
        host_id=str(sandbox.get("host") or "") or None,
    )


def offer_measurement(
    *,
    capability: OperationMeasurements,
    job: Mapping[str, Any],
    measure: Selection | None,
) -> None:
    """Offer one frozen record without letting diagnostics fail its request."""
    if measure is None or not measure.measurement.selected:
        return
    try:
        record = measure.frozen_record()
        if not record:
            return
        prepared, reason = prepare_measurement(
            record=record,
            owner=_owner(job),
            run=manifest_for_measurement(measure.measurement),
            now=datetime.now(UTC),
        )
        writer = measurement_writer.writer()
        if prepared is None:
            writer.counts.rejected += 1
            writer.counts.note(f"rejected:{reason[:60]}")
            return
        # Lazy on purpose: a process with profiling disabled owns no writer
        # tasks.  The descriptor is process-static, so the first selected
        # request is the first time workers can be useful.
        writer.start()
        writer.offer(
            persist=lambda: store_prepared(capability, prepared),
            payload=prepared.payload,
        )
    except Exception:
        # A profiler is never allowed to change the business response.
        logger.warning(
            "operation_measurement_offer_failed",
            exc_info=True,
            extra={"event": "operation_measurement_offer_failed"},
        )


def record_measurement(
    *,
    store: Any,
    job: Mapping[str, Any],
    measure: Selection | None,
    failure: str | None = None,
) -> None:
    """Freeze and queue a successful or failed measured request."""
    if measure is None or not measure.measurement.selected:
        return
    if failure is not None and not measure.measurement.frozen:
        measure.outcome = failure
        if measure.failure_stage is None:
            measure.failure_stage = (
                measure.measurement.marks[-1][0] if measure.measurement.marks else "route"
            )
    offer_measurement(capability=store.operation_measurements, job=job, measure=measure)


def render_fragment(measure: Selection | None) -> dict[str, Any] | None:
    """Return response metadata frozen at the same boundary as storage."""
    if measure is None:
        return None
    measure.settle()
    block = measure.metadata()
    if block is None:
        return None
    return AgentOperationProfilingFragment(**block).model_dump(exclude_none=True)


def measured_response(model: Any, measure: Selection | None, payload: dict[str, Any]) -> Response:
    """Serialize a response without changing the profiling-off wire shape."""
    body = model(**payload).model_dump()
    body.pop("profiling", None)
    block = render_fragment(measure)
    if block is not None:
        body["profiling"] = block
    return JSONResponse(body)


def profiling_answer_headers(measure: Selection | None) -> dict[str, str]:
    """Encode a terminal stream's profiling answer in response headers."""
    rendered = render_fragment(measure)
    if rendered is None:
        return {}
    headers = {
        "X-Agent-Profiling-Interaction-Id": str(rendered["interaction_id"]),
        "X-Agent-Profiling-Measurement-Id": str(rendered["measurement_id"]),
        "X-Agent-Profiling-Collection-Complete": (
            "1" if rendered.get("collection_complete") else "0"
        ),
    }
    if rendered.get("call_id"):
        headers["X-Agent-Profiling-Call-Id"] = str(rendered["call_id"])
    if rendered.get("fragment_truncated"):
        headers["X-Agent-Profiling-Fragment-Truncated"] = "1"
    return headers


async def measured_call(
    measure: Selection | None,
    stage: str,
    *,
    call: Callable[[dict[str, Any] | None], Awaitable[_T]],
    record: bool = True,
    call_id: str | None = None,
    timing_parent: str | None = None,
) -> _T:
    """Time one broker call and attach the answer to its unique call id."""
    if measure is None:
        return await call(None)
    if call_id is None:
        call_id = measure.begin_call(stage)
    with measure.stage(timing_parent or stage, "broker_call"):
        response = await call(measure.context(call_id))
    if record and isinstance(response, dict):
        measure.record_answer(stage, call_id, response)
    return response


__all__ = [
    "FILE_READ_STAGE",
    "measured_call",
    "measured_response",
    "profiling_answer_headers",
    "record_measurement",
    "render_fragment",
]
