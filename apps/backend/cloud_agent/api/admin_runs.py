"""Admin-only observation reads, separate from owner content and controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import AwareDatetime, ValidationError

from cloud_agent.admin_observability import (
    AdminAttempt,
    AdminRun,
    AdminRunDetail,
    AdminRunList,
    RunCursor,
    RunFilters,
    cursor_scope,
    decode_cursor,
    resolve_run_window,
)
from cloud_agent.api._job_views import _attempt_duration_ms, _attempt_resources
from cloud_agent.api.deps import (
    get_storage,
    require_store,
    verify_admin_access,
)
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.ledger import summarize_job_usage
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import AgentAttemptTelemetry, AttemptResources, TurnResourceSummary
from cloud_agent.thread_stats import summarize_session_statistics
from cloud_agent_shared.turn_resources import summarize_turn_resources

if TYPE_CHECKING:
    from cloud_agent.ports import LedgerRepository
    from cloud_agent.storage.admin_queries import AdminRunQueries

logger = get_logger(__name__)


class _AdminRunsStorage(Protocol):
    """The capabilities this module's handlers reach the storage layer for."""

    admin_runs: AdminRunQueries
    ledger: LedgerRepository


def _require_store(store: _AdminRunsStorage | None) -> _AdminRunsStorage:
    """The storage layer, or 503."""
    return require_store(store)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


# The guard is on the router, not only on each route: a route added to this
# surface inherits it, so the next runs read cannot ship without one by
# omission. The routes that also name it as their ``_admin`` parameter do not
# ask the gateway twice: FastAPI runs a shared dependency once per request.
router = APIRouter(
    prefix="/admin/agent/runs",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)


def get_admin_run_queries(
    store: _AdminRunsStorage | None = Depends(get_storage),
) -> AdminRunQueries:
    """Expose only purpose-built projections to the default admin routes."""
    return _require_store(store).admin_runs


async def _run(queries: AdminRunQueries, job_id: str) -> dict[str, Any]:
    row = await queries.get_run(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return row


def _resources_with_turn_summary(telemetry: dict[str, Any]) -> AttemptResources | None:
    """Shape one attempt's resources, adding the derived whole-turn window.

    The stored snapshot and the attempt's terminal state go through the shared
    reducer, so this view and the Runs list derive ``interrupted`` the same way.
    The reducer is total: a malformed or absent snapshot yields no
    ``turn_summary`` and never fails the page, which is what keeps one damaged
    row from taking down the detail view.

    The same policy covers the model itself. The reducer checks the structure
    it must understand, not every literal method string a modified row could
    carry, so a snapshot that still cannot become a response is reported as
    absent rather than as a 500 on the admin page.
    """
    resources = _attempt_resources(telemetry)
    if resources is None:
        return None
    summary = summarize_turn_resources(
        telemetry.get("turn_summary"),
        attempt_terminal=bool(telemetry.get("attempt_terminal")),
        received_at=telemetry.get("turn_summary_received_at"),
    )
    if summary is None:
        return resources
    try:
        turn_summary = TurnResourceSummary.model_validate(summary)
    except ValidationError:
        logger.warning("agent_admin_turn_summary_unreadable")
        return resources
    return resources.model_copy(update={"turn_summary": turn_summary})


@router.get("", response_model=AdminRunList)
async def list_admin_runs(
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    user_id: str | None = Query(None, max_length=256),
    repo: str | None = Query(None, max_length=256),
    state: str | None = Query(None, max_length=64),
    runtime: str | None = Query(None, max_length=128),
    model: str | None = Query(None, max_length=256),
    host: str | None = Query(None, max_length=256),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(50, ge=1, le=200),
    _admin: str = Depends(verify_admin_access),
    queries: AdminRunQueries = Depends(get_admin_run_queries),
) -> AdminRunList:
    """List metadata across owners in a bounded, creation-time window."""
    filters = RunFilters(
        user_id=user_id, repo=repo, state=state, runtime=runtime, model=model, host=host
    )
    try:
        parsed = decode_cursor(cursor, RunCursor, cursor_scope(filters.model_dump()))
        since, until = resolve_run_window(since=since, until=until, cursor=parsed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await queries.list_runs(
        filters=filters, since=since, until=until, cursor=parsed, limit=limit
    )


@router.get("/{job_id}", response_model=AdminRunDetail)
async def get_admin_run(
    job_id: str = Path(max_length=256),
    _admin: str = Depends(verify_admin_access),
    queries: AdminRunQueries = Depends(get_admin_run_queries),
    store: _AdminRunsStorage | None = Depends(get_storage),
) -> AdminRunDetail:
    """Read a turn's metadata and existing accounting without owner impersonation."""
    run = await _run(queries, job_id)
    runtime_inputs = await queries.usage_inputs(job_id)
    usage = summarize_job_usage(
        ledger_rows=await _require_store(store).ledger.ledger_rows_for_job(job_id=job_id),
        runtime_inputs=runtime_inputs,
        runtime=run["runtime"],
        terminal=run["state"] in TERMINAL_STATES,
        # Which connection stays the owner's; the reader learns only that the
        # turn's provider, not the Gateway, bills it.
        external_provider=bool(run.get("model_connection_id")),
    )
    return AdminRunDetail(
        run=AdminRun.model_validate(run),
        attempts=[AdminAttempt.model_validate(row) for row in await queries.attempts(job_id)],
        usage=usage,
        session_statistics=summarize_session_statistics(**runtime_inputs),
    )


@router.get("/{job_id}/attempts/{attempt_id}/resources", response_model=AgentAttemptTelemetry)
async def get_admin_resources(
    job_id: str = Path(max_length=256),
    attempt_id: int = Path(ge=1, le=2**63 - 1),
    _admin: str = Depends(verify_admin_access),
    queries: AdminRunQueries = Depends(get_admin_run_queries),
) -> AgentAttemptTelemetry:
    """Read measured resources for this execution identity, retaining gaps and scope."""
    telemetry = await queries.resources(job_id, attempt_id)
    if telemetry is None:
        raise HTTPException(status_code=404, detail="Run execution not found.")
    return AgentAttemptTelemetry(
        attempt_id=telemetry["attempt_id"],
        attempt_no=telemetry["attempt_no"],
        continuity_mode=telemetry.get("continuity_mode"),
        context_status=telemetry.get("context_status"),
        duration_ms=_attempt_duration_ms(telemetry),
        resources=_resources_with_turn_summary(telemetry),
    )
