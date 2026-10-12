"""Admin-only steps: the model requests of a page of turns, read from the gateway.

A step is one model request of a turn. The gateway lists requests one grant
window at a time, so this route reads a page of turns -- the runs list's
cohort, order and cursor -- then each turn's stored references, and merges
what the gateway returns for them, newest request first.

A turn whose requests cannot all be read is still listed, with how much was
read and why, so a short page never reads as "these turns made fewer calls".
Nothing here writes: like the per-turn request page, reading it must not
renew a grant or change what accounting reports.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime  # noqa: TC002 — FastAPI resolves this at runtime.

from cloud_agent.admin_observability import (
    MAX_ADMIN_RESPONSE_BYTES,
    AdminRun,
    AdminStep,
    AdminStepsPage,
    AdminStepTurn,
    RunCursor,
    RunFilters,
    StepCoverage,
    cursor_scope,
    decode_cursor,
    expected_gateway_job_id,
    llm_request_source_view,
    resolve_run_window,
    response_body_bytes,
)
from cloud_agent.api.admin_llm_requests import _refuse
from cloud_agent.api.admin_runs import _no_store, get_admin_run_queries
from cloud_agent.api.deps import (
    get_grant_client,
    verify_admin_access,
)
from cloud_agent.domain.execution import ATTEMPT_ABORTED
from cloud_agent.gateway import GatewayError
from cloud_agent.logging_utils import get_logger
from cloud_agent.storage.admin_queries import (  # noqa: TC001 — FastAPI resolves this at runtime.
    AdminRunQueries,
)

if TYPE_CHECKING:
    from cloud_agent.grants import GrantClient

logger = get_logger(__name__)

# The guard is on the router, not each route: a route added to this surface
# inherits it, so the next steps read cannot ship without one by omission.
router = APIRouter(
    prefix="/admin/agent/steps",
    dependencies=[Depends(_no_store), Depends(verify_admin_access)],
)

#: Turns one page reads. Each costs a reference read and a gateway read per
#: run, so a page stays small and the list pages on.
DEFAULT_STEP_TURNS = 5
MAX_STEP_TURNS = 10
#: Requests read per run. A run with more marks its turn ``partial``; the
#: turn's own page lists every request.
STEP_REQUESTS_PER_RUN = 50
#: Stored references read per turn. A turn restarted more often than this is
#: marked ``partial`` rather than read without bound.
STEP_SOURCES_PER_TURN = 10
#: Gateway reads one page keeps in flight at once.
STEP_READ_CONCURRENCY = 4

#: Why a run's requests could not be read, when the gateway itself failed.
STEP_REASON_GATEWAY_UNAVAILABLE = "gateway_unavailable"

#: Worst first: a turn reports the least complete of its runs.
_COVERAGE_RANK: dict[StepCoverage, int] = {
    "unavailable": 3,
    "partial": 2,
    "complete": 1,
    "not_started": 0,
}


@router.get("", response_model=AdminStepsPage)
async def list_admin_steps(
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    user_id: str | None = Query(None, max_length=256),
    repo: str | None = Query(None, max_length=256),
    state: str | None = Query(None, max_length=64),
    runtime: str | None = Query(None, max_length=128),
    model: str | None = Query(None, max_length=256),
    host: str | None = Query(None, max_length=256),
    cursor: str | None = Query(None, max_length=2048),
    limit: int = Query(DEFAULT_STEP_TURNS, ge=1, le=MAX_STEP_TURNS),
    queries: AdminRunQueries = Depends(get_admin_run_queries),
    grants: GrantClient = Depends(get_grant_client),
) -> AdminStepsPage:
    """List the requests of the next turns in the runs list's selection.

    ``limit`` counts turns, not requests: the continuation is the runs list's
    own, so the turns a steps page covers are exactly the ones the runs list
    shows for the same filters.
    """
    filters = RunFilters(
        user_id=user_id, repo=repo, state=state, runtime=runtime, model=model, host=host
    )
    try:
        parsed = decode_cursor(cursor, RunCursor, cursor_scope(filters.model_dump()))
        since, until = resolve_run_window(since=since, until=until, cursor=parsed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    turns = await queries.list_runs(
        filters=filters, since=since, until=until, cursor=parsed, limit=limit
    )
    gate = asyncio.Semaphore(STEP_READ_CONCURRENCY)
    read = await asyncio.gather(*(_turn_steps(run, queries, grants, gate) for run in turns.runs))
    steps = sorted(
        (step for _, turn_steps in read for step in turn_steps),
        key=lambda step: (step.request_started_at, step.request_id),
        reverse=True,
    )
    page = AdminStepsPage(
        since=since,
        until=until,
        fetched_at=datetime.now(UTC),
        turns=[turn for turn, _ in read],
        steps=steps,
        next_cursor=turns.next_cursor,
    )
    if response_body_bytes(page) > MAX_ADMIN_RESPONSE_BYTES:
        # The same ceiling the per-turn page keeps, measured on the finished
        # body. Dropping rows to fit would present the rest as the whole page.
        raise _refuse(
            502,
            "steps_page_too_large",
            "These turns made more requests than one page may carry; read fewer turns per page.",
        )
    return page


async def _turn_steps(
    run: AdminRun,
    queries: AdminRunQueries,
    grants: GrantClient,
    gate: asyncio.Semaphore,
) -> tuple[AdminStepTurn, list[AdminStep]]:
    """Read one turn's requests from every run of it that began."""
    rows, more = await queries.llm_request_sources(
        run.id, attempt_id=None, cursor=None, limit=STEP_SOURCES_PER_TURN
    )
    # A run the platform gave up on before the agent started made no requests;
    # its missing reference is not a gap in this turn's steps.
    started = [row for row in rows if row.get("attempt_status") != ATTEMPT_ABORTED]
    coverage: StepCoverage = "partial" if more else "complete" if started else "not_started"
    reason: str | None = None
    steps: list[AdminStep] = []
    for row in started:
        read, run_coverage, run_reason = await _run_steps(run, row, grants, gate)
        steps.extend(read)
        if _COVERAGE_RANK[run_coverage] > _COVERAGE_RANK[coverage]:
            coverage, reason = run_coverage, run_reason
    turn = AdminStepTurn(
        job_id=run.id,
        thread_id=run.thread_id,
        turn_no=run.turn_no,
        user_id=run.user_id,
        repo=run.repo,
        runtime=run.runtime,
        state=run.state,
        created_at=run.created_at,
        coverage=coverage,
        reason=reason,
    )
    return turn, steps


async def _run_steps(
    run: AdminRun,
    row: dict[str, Any],
    grants: GrantClient,
    gate: asyncio.Semaphore,
) -> tuple[list[AdminStep], StepCoverage, str | None]:
    """Read one run's first requests, or say why they cannot be read."""
    expected_external_job_id = expected_gateway_job_id(thread_id=row["thread_id"])
    source = llm_request_source_view(
        row,
        current_binding_id=grants.binding_id,
        expected_external_job_id=expected_external_job_id,
    )
    if source.availability != "queryable" or expected_external_job_id is None:
        return [], "unavailable", source.reason
    assert source.window_since is not None and source.window_until is not None
    started = time.monotonic()
    try:
        async with gate:
            page = await grants.usage_requests(
                row["grant_id"],
                since=source.window_since,
                until=source.window_until,
                limit=STEP_REQUESTS_PER_RUN,
                external_job_id=expected_external_job_id,
            )
    except GatewayError as exc:
        # As on the per-turn page: the machine reason, the correlation ids and
        # the time it took, never the gateway's own text.
        logger.warning(
            "agent_admin_steps_read_failed",
            extra={
                "event": "agent_admin_steps_read_failed",
                "job_id": run.id,
                "source_id": row["ledger_id"],
                "attempt_id": row["attempt_id"],
                "error_type": exc.error_type,
                "status_code": exc.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return [], "unavailable", STEP_REASON_GATEWAY_UNAVAILABLE
    steps = [
        AdminStep.model_validate(
            {**item.model_dump(), "job_id": run.id, "attempt_no": row["attempt_no"]}
        )
        for item in page.requests
    ]
    return steps, "partial" if page.next_cursor is not None else "complete", None
