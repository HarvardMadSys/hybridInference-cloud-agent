"""Owner-scoped reads and refresh requests for a job's inference usage."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel

from cloud_agent.api._owner_auth import _owned_job, require_agent_owner
from cloud_agent.api.deps import get_grant_client, get_storage, require_store
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.gateway import GatewayError
from cloud_agent.grant_usage import GrantUsageRequest  # noqa: TC001 — Pydantic resolves the model.
from cloud_agent.ledger import (
    JobUsageResponse,
    summarize_conversation,
    summarize_job_usage,
)

if TYPE_CHECKING:
    from cloud_agent.grants import GrantClient
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


class MessageRequestDetail(BaseModel):
    """A request matched by response identity inside one owner's execution."""

    status: Literal["available", "not_recorded", "ambiguous", "unavailable"]
    request: GrantUsageRequest | None = None
    reason: str | None = None


@router.get(
    "/jobs/{job_id}/attempts/{attempt_id}/messages/{message_id}/request",
    response_model=MessageRequestDetail,
)
async def get_message_request(
    response: Response,
    job_id: str,
    attempt_id: int = Path(ge=1),
    message_id: str = Path(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UsageStorage | None = Depends(get_storage),
    grants: GrantClient = Depends(get_grant_client),
) -> MessageRequestDetail:
    """Read measured request statistics without attributing turn totals to a message.

    All grant and time-window selectors come from the owned job's ledger.
    Multiple matches are ambiguous, never joined by order or guessed timing.
    Old gateways must echo the identity filter before any row is accepted.
    """
    response.headers["Cache-Control"] = "no-store"
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    rows = [
        row
        for row in await job_store.ledger.ledger_rows_for_job(job_id=job_id)
        if row["attempt_id"] == attempt_id
    ]
    if not rows:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    if not job.get("thread_id") or len(rows) > 16:
        return MessageRequestDetail(status="unavailable", reason="attribution_unavailable")
    for row in rows:
        if not row.get("id") or not row.get("grant_id"):
            return MessageRequestDetail(status="unavailable", reason="attribution_unavailable")
        if row.get("binding_id") != grants.binding_id:
            return MessageRequestDetail(status="unavailable", reason="gateway_binding_changed")
        if (
            row.get("window_since") is None
            or row.get("window_until") is None
            or row["window_since"] >= row["window_until"]
        ):
            return MessageRequestDetail(status="not_recorded", reason="window_pending")
    try:
        async with asyncio.timeout(15):
            pages = await asyncio.gather(
                *(
                    grants.usage_requests(
                        row["grant_id"],
                        since=row["window_since"],
                        until=row["window_until"],
                        limit=2,
                        external_job_id=f"thread:{job['thread_id']}",
                        response_id=message_id,
                    )
                    for row in rows
                )
            )
    except (GatewayError, TimeoutError) as exc:
        reason = (
            "message_correlation_unsupported"
            if isinstance(exc, GatewayError) and exc.error_type == "message_correlation_unsupported"
            else "gateway_unavailable"
        )
        return MessageRequestDetail(status="unavailable", reason=reason)
    matches = {item.request_id: item for page in pages for item in page.requests}
    if len(matches) > 1 or any(page.next_cursor is not None for page in pages):
        return MessageRequestDetail(status="ambiguous")
    if not matches:
        return MessageRequestDetail(status="not_recorded")
    return MessageRequestDetail(status="available", request=next(iter(matches.values())))


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
    )
    usage.version = reads["version"]
    return usage


async def _owner_usage(store: _UsageStorage, job: dict[str, Any]) -> JobUsageResponse:
    usage = await job_usage_response(store, job_id=job["id"], user_id=job["user_id"])
    if usage is None:
        # Deleted between the ownership check and the read.
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job['id']}"}},
        )
    return usage


@router.get("/jobs/{job_id}/usage", response_model=JobUsageResponse)
async def get_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UsageStorage | None = Depends(get_storage),
) -> JobUsageResponse:
    """Read cached Gateway snapshots and runtime usage for the same job."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    return await _owner_usage(job_store, job)


@router.post("/jobs/{job_id}/usage/refresh", response_model=JobUsageResponse)
async def refresh_job_usage(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _UsageStorage | None = Depends(get_storage),
) -> JobUsageResponse:
    """Queue a bounded refresh and return cached data without waiting for HTTP."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    await job_store.ledger.queue_job_ledger_refresh(job_id=job_id)
    return await _owner_usage(job_store, job)
