"""Owner-scoped reads and refresh requests for a job's inference usage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from cloud_agent.api.deps import (
    get_conversation_queries,
    get_execution_queries,
    get_ledger_repository,
    require_execution_queries,
    require_ledger,
    require_queries,
)
from cloud_agent.api.routes import _owned_job, require_agent_owner
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.ledger import (
    JobUsageResponse,
    summarize_conversation,
    summarize_job_usage,
)

if TYPE_CHECKING:
    from cloud_agent.ports.conversation_queries import ConversationQueries
    from cloud_agent.ports.execution_queries import ExecutionQueries
    from cloud_agent.ports.ledger import LedgerRepository

router = APIRouter(prefix="/v1/agent", tags=["agent"])


async def _usage_response(
    ledger: LedgerRepository,
    execution: ExecutionQueries,
    job: dict[str, Any],
) -> JobUsageResponse:
    job_id = job["id"]
    rows = await ledger.ledger_rows_for_job(job_id=job_id)
    inputs = await execution.job_usage_inputs(job_id=job_id)
    return summarize_job_usage(
        ledger_rows=rows,
        runtime_inputs=inputs,
        runtime=job.get("runtime"),
        terminal=job.get("state") in TERMINAL_STATES,
        conversation=summarize_conversation(
            await ledger.ledger_rows_for_conversation(job_id=job_id, user_id=job["user_id"])
        ),
    )


@router.get("/jobs/{job_id}/usage", response_model=JobUsageResponse)
async def get_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    conversations: ConversationQueries | None = Depends(get_conversation_queries),
    ledger: LedgerRepository | None = Depends(get_ledger_repository),
    execution: ExecutionQueries | None = Depends(get_execution_queries),
) -> JobUsageResponse:
    """Read cached Gateway snapshots and runtime usage for the same job."""
    job = await _owned_job(require_queries(conversations), job_id, user)
    return await _usage_response(require_ledger(ledger), require_execution_queries(execution), job)


@router.post("/jobs/{job_id}/usage/refresh", response_model=JobUsageResponse)
async def refresh_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    conversations: ConversationQueries | None = Depends(get_conversation_queries),
    ledger: LedgerRepository | None = Depends(get_ledger_repository),
    execution: ExecutionQueries | None = Depends(get_execution_queries),
) -> JobUsageResponse:
    """Queue a bounded refresh and return cached data without waiting for HTTP."""
    job = await _owned_job(require_queries(conversations), job_id, user)
    resolved_ledger = require_ledger(ledger)
    await resolved_ledger.queue_job_ledger_refresh(job_id=job_id)
    return await _usage_response(resolved_ledger, require_execution_queries(execution), job)
