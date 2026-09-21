"""Reading stored operation measurements back, for administrators.

This surface answers four questions and refuses to answer a fifth. It lists the
experiments that were catalogued, pages the measurements of one, expands a
single measurement, and aggregates a whole experiment. It does **not** collect:
browsing history never turns profiling on, never starts a poller, and changes
nothing about a running experiment.

Two honesty rules are built into the shape of the responses rather than left to
the page:

* **No samples is not zero samples.** A run with no stored measurements returns
  an empty page and says so, because the absence has four possible causes --
  switched off, not selected, dropped in delivery, or a producer this deployment
  does not know -- and none of them is "the operation took 0 ms".
* **A server-side record is not the user's wait.** Every row states its
  ``coverage_scope`` and ``end_to_end_complete``, so a consumer cannot present
  a server request as an interaction without contradicting the data it was
  handed.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — FastAPI resolves this at runtime.
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from cloud_agent import measurement_writer
from cloud_agent.api.deps import get_storage, require_store, verify_admin_access
from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

#: Rows one page may return, and the ceiling a caller may ask for.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
#: Experiments one catalogue page may return.
MAX_RUNS = 200

#: The selectors the sample list accepts. Anything else is refused rather than
#: ignored, because FastAPI drops an unknown query parameter silently: a caller
#: that asked for one attempt's startup and mistyped the key would be handed the
#: whole experiment's page and no way to notice. The Admin page has the same
#: rule, so the API and the UI cannot disagree about what was asked for.
_MEASUREMENT_LIST_QUERY = frozenset(
    {
        "run_id",
        "operation",
        "request_kind",
        "coverage_scope",
        "outcome",
        "collection_complete",
        "job_id",
        "thread_id",
        "attempt_id",
        "interaction_id",
        "after",
        "limit",
    }
)


class _MeasurementStorage(Protocol):
    """The one capability this module's handlers reach the storage layer for."""

    operation_measurements: Any


def _require_store(store: _MeasurementStorage | None) -> _MeasurementStorage:
    """The storage layer, or 503."""
    return require_store(store)


def _reject_unknown_query(request: Request, allowed: frozenset[str]) -> None:
    """Refuse a selector this endpoint does not accept, before any work."""
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "unexpected_query_parameter",
                    "message": f"Unsupported query parameter: {unknown[0]}.",
                }
            },
        )


def _no_store(response: Response) -> None:
    """A cached measurement is a measurement read at a time nobody chose."""
    response.headers["Cache-Control"] = "no-store"


#: One router, the shared admin guard, and no caching. The guard is a
#: dependency rather than a check inside each handler: a route added later
#: cannot forget it, because it is not something the handler does.
router = APIRouter(
    prefix="/admin/agent",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


@router.get("/profiling-runs")
async def list_profiling_runs(
    store: _MeasurementStorage | None = Depends(get_storage),
    since: datetime | None = None,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_RUNS),
) -> dict[str, Any]:
    """Return the experiment catalogue, most recent first.

    A run appears here only once a producer has delivered a measurement for it:
    the row is created by that delivery, so an experiment that collected
    nothing has nothing to list. The page says how many runs it returned rather
    than implying it returned all of them.
    """
    runs = await _require_store(store).operation_measurements.list_profiling_runs(
        since=since, limit=limit
    )
    return {"runs": runs, "count": len(runs), "limit": limit}


@router.get("/profiling-runs/{run_id}/summary")
async def summarize_profiling_run(
    run_id: str,
    store: _MeasurementStorage | None = Depends(get_storage),
    group_by: list[str] | None = Query(None),
) -> dict[str, Any]:
    """Return counts and percentiles over a whole experiment.

    Computed in SQL, not over one page: a percentile of the rows a reader
    happened to load is a percentile of the page. The groups carry the
    dimensions that decide comparability, and the counts report what was
    excluded from the distribution alongside what was included.
    """
    try:
        groups = await _require_store(
            store
        ).operation_measurements.summarize_operation_measurements(run_id=run_id, group_by=group_by)
    except ValueError as exc:
        # A caller naming a dimension that does not exist is refused rather
        # than silently handed a coarser grouping than it asked for.
        raise HTTPException(
            status_code=422,
            detail={"error": {"type": "invalid_group_by", "message": str(exc)}},
        ) from exc
    return {"run_id": run_id, "groups": groups}


@router.get("/measurements")
async def list_measurements(
    request: Request,
    store: _MeasurementStorage | None = Depends(get_storage),
    run_id: str | None = Query(None, max_length=128),
    operation: list[str] | None = Query(None),
    request_kind: list[str] | None = Query(None),
    coverage_scope: list[str] | None = Query(None),
    outcome: str | None = Query(None, max_length=64),
    collection_complete: bool | None = None,
    job_id: str | None = Query(None, max_length=128),
    thread_id: str | None = Query(None, max_length=128),
    attempt_id: int | None = Query(
        None, ge=0, description="One execution's startup samples; startup records carry one."
    ),
    interaction_id: str | None = Query(
        None,
        max_length=128,
        description="Every request of one logical interaction, such as a terminal open.",
    ),
    after: str | None = Query(
        None, description="Opaque cursor: the measurement_id to continue after."
    ),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """Return one page of measurement summaries, newest first.

    Summaries only: the frozen record is the largest thing this surface holds
    and a list needs none of it, so a page of them would be a page of payload.
    The cursor is the ``measurement_id`` of the last row, resolved to its own
    arrival here -- a caller cannot forge an arrival time, and the pair
    ``(received_at, measurement_id)`` is what makes the page stable while new
    samples land.

    ``attempt_id`` and ``interaction_id`` are not two spellings of one filter.
    An attempt selects the startup record of exactly one execution; an
    interaction selects the list, create and stream-attach requests of one
    terminal open, which have no attempt at all. A caller that wants the whole
    of one terminal open asks by interaction, and gets three rows that are
    three measurements rather than one.
    """
    _reject_unknown_query(request, _MEASUREMENT_LIST_QUERY)
    capability = _require_store(store).operation_measurements
    keyset = None
    if after:
        anchor = await capability.get_operation_measurement(after)
        if anchor is None:
            raise HTTPException(
                status_code=404,
                detail={"error": {"type": "unknown_cursor", "message": "No such measurement."}},
            )
        keyset = (anchor["received_at"], anchor["measurement_id"])
    rows = await capability.list_operation_measurements(
        run_id=run_id,
        operations=operation,
        request_kinds=request_kind,
        coverage_scopes=coverage_scope,
        outcome=outcome,
        collection_complete=collection_complete,
        job_id=job_id,
        thread_id=thread_id,
        attempt_id=attempt_id,
        interaction_id=interaction_id,
        after=keyset,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    # An empty page is reported as empty, with what it covered. It is never
    # rendered as zero latency, and a caller cannot tell from the page alone
    # why an experiment has no samples -- so the page does not claim to.
    body: dict[str, Any] = {
        "measurements": rows,
        "count": len(rows),
        "limit": limit,
        "next_cursor": rows[-1]["measurement_id"] if has_more and rows else None,
    }
    if not rows:
        body["empty_reason"] = (
            "no stored samples match this selection; the experiment may be off, "
            "unselected, undelivered, or from a producer version that does not "
            "report measurements"
        )
    return body


@router.get("/measurement-writer")
async def measurement_writer_status() -> dict[str, Any]:
    """Return this replica's bounded diagnostic-writer state and loss counts."""
    local = measurement_writer.writer()
    return {
        "scope": "this_control_plane_process",
        "running": local.running,
        "pending": local.pending,
        "in_flight": local.in_flight,
        "queued_bytes": local.queued_bytes,
        "counts": local.counts.as_dict(),
    }


@router.get("/measurements/{measurement_id}")
async def get_measurement(
    measurement_id: str,
    store: _MeasurementStorage | None = Depends(get_storage),
) -> dict[str, Any]:
    """Return one measurement with the record it was frozen from."""
    row = await _require_store(store).operation_measurements.get_operation_measurement(
        measurement_id
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": "No such measurement."}},
        )
    return row
