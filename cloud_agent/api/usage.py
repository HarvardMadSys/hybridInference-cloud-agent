"""Owner-scoped reads and refresh requests for a job's inference usage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from cloud_agent.api.deps import get_agent_job_store
from cloud_agent.api.routes import _owned_job, _require_store, require_agent_owner
from cloud_agent.ledger import (
    JobUsageResponse,
    RuntimeUsage,
    summarize_conversation,
    summarize_ledger,
)
from cloud_agent.storage.store import TERMINAL_STATES
from cloud_agent.thread_stats import normalized_runtime_input_tokens, summarize_thread

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

router = APIRouter(prefix="/v1/agent", tags=["agent"])


async def _usage_response(store: AgentJobStore, job: dict[str, Any]) -> JobUsageResponse:
    job_id = job["id"]
    rows = await store.ledger_rows_for_job(job_id=job_id)
    inputs = await store.job_usage_inputs(job_id=job_id)
    stats = summarize_thread(**inputs)
    ledger = summarize_ledger(rows)
    if not rows and job.get("state") in TERMINAL_STATES:
        # Cancellation before the first claim creates no attempt to refresh.
        # Preserve unknown counters without promising a future snapshot.
        ledger.status = "unavailable"
    return JobUsageResponse(
        ledger=ledger,
        runtime=RuntimeUsage(
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            cache_read_tokens=stats.cache_read_tokens,
            normalized_input_tokens=normalized_runtime_input_tokens(
                runtime=job.get("runtime"), events=inputs["events"]
            ),
        ),
        conversation=summarize_conversation(
            await store.ledger_rows_for_conversation(job_id=job_id, user_id=job["user_id"])
        ),
    )


@router.get("/jobs/{job_id}/usage", response_model=JobUsageResponse)
async def get_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> JobUsageResponse:
    """Read cached Gateway snapshots and runtime usage for the same job."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    return await _usage_response(job_store, job)


@router.post("/jobs/{job_id}/usage/refresh", response_model=JobUsageResponse)
async def refresh_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> JobUsageResponse:
    """Queue a bounded refresh and return cached data without waiting for HTTP."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    await job_store.queue_job_ledger_refresh(job_id=job_id)
    return await _usage_response(job_store, job)
