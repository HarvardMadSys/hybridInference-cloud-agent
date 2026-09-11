"""Owner-scoped reads and refresh requests for a job's inference usage."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Depends

from cloud_agent.api.deps import get_agent_job_store, require_store
from cloud_agent.api.routes import _owned_job, require_agent_owner
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.ledger import (
    JobUsageResponse,
    summarize_conversation,
    summarize_job_usage,
)
from cloud_agent.ports import conversation_queries, execution_queries, ledger


class _UsageStorage(
    conversation_queries.ConversationQueries,
    execution_queries.ExecutionQueries,
    ledger.LedgerRepository,
    Protocol,
):
    """The capabilities this module's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """


def _require_store(store: _UsageStorage | None) -> _UsageStorage:
    """This surface's capabilities, or 503 when no database is configured."""
    return require_store(store)


router = APIRouter(prefix="/v1/agent", tags=["agent"])


async def _usage_response(store: _UsageStorage, job: dict[str, Any]) -> JobUsageResponse:
    job_id = job["id"]
    rows = await store.ledger_rows_for_job(job_id=job_id)
    inputs = await store.job_usage_inputs(job_id=job_id)
    return summarize_job_usage(
        ledger_rows=rows,
        runtime_inputs=inputs,
        runtime=job.get("runtime"),
        terminal=job.get("state") in TERMINAL_STATES,
        conversation=summarize_conversation(
            await store.ledger_rows_for_conversation(job_id=job_id, user_id=job["user_id"])
        ),
    )


@router.get("/jobs/{job_id}/usage", response_model=JobUsageResponse)
async def get_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UsageStorage | None = Depends(get_agent_job_store),
) -> JobUsageResponse:
    """Read cached Gateway snapshots and runtime usage for the same job."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    return await _usage_response(job_store, job)


@router.post("/jobs/{job_id}/usage/refresh", response_model=JobUsageResponse)
async def refresh_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UsageStorage | None = Depends(get_agent_job_store),
) -> JobUsageResponse:
    """Queue a bounded refresh and return cached data without waiting for HTTP."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    await job_store.queue_job_ledger_refresh(job_id=job_id)
    return await _usage_response(job_store, job)
