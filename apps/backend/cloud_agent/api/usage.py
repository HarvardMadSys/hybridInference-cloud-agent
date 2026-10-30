"""Owner-scoped reads of a job's usage: what its runtime reported, and its conversation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_storage, require_store
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.ledger import (
    JobUsageResponse,
    summarize_conversation,
    summarize_job_usage,
)

if TYPE_CHECKING:
    from cloud_agent.ports import ConversationQueries, LedgerRepository


class _UsageStorage(Protocol):
    """The capabilities this module's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """

    conversation_queries: ConversationQueries
    ledger: LedgerRepository


def _require_store(store: _UsageStorage | None) -> _UsageStorage:
    """This surface's capabilities, or 503 when no database is configured."""
    return require_store(store)


router = APIRouter(prefix="/v1/agent", tags=["agent"])


async def job_usage_response(
    store: _UsageStorage, *, job_id: str, user_id: str
) -> JobUsageResponse | None:
    """An owner's job usage, as they read it here and are pushed it on change.

    Read from one snapshot, whose version it carries, so that a tab holding two
    of them, one read and one pushed, keeps the newer. ``None`` when the job is
    not theirs.
    """
    reads = await store.ledger.job_usage_reads(job_id=job_id, user_id=user_id)
    if reads is None:
        return None
    job = reads["job"]
    usage = summarize_job_usage(
        ledger_rows=reads["ledger_rows"],
        runtime_inputs=reads["inputs"],
        runtime=job.get("runtime"),
        terminal=job.get("state") in TERMINAL_STATES,
        conversation=summarize_conversation(reads["conversation_rows"]),
        external_provider=bool(job.get("model_connection_id")),
    )
    usage.version = reads["version"]
    return usage


@router.get("/jobs/{job_id}/usage", response_model=JobUsageResponse)
async def get_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UsageStorage | None = Depends(get_storage),
) -> JobUsageResponse:
    """Read the runtime usage and conversation statistics of one owned job."""
    job_store = _require_store(store)
    # The snapshot read is itself scoped to the caller's own, undeleted job:
    # None covers another owner's job, a missing one, and a deleted
    # conversation alike, and one 404 answers all three.
    usage = await job_usage_response(job_store, job_id=job_id, user_id=user["user_id"])
    if usage is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return usage
