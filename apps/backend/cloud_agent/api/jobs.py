"""The owner's view of a job: create, read, list, cancel, archive, pin, rename.

Ten endpoints behind the session credential. Everything is scoped to
``user_id`` — the ownership fence lives in ``_owner_auth._owned_job`` — so one
account can never read or mutate another's job, and a job that belongs to
somebody else is a 404 rather than a 403 so ids cannot be probed for existence.

``_job_usage``, ``_set_agent_thread_archived`` and ``_set_agent_thread_pinned``
travel with it: each has callers only here, and a helper whose only caller moves
has to move with it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query

from cloud_agent.api._job_views import (
    _attempt_resources,
    _attempt_summary_response,
    _iso,
    _job_response,
)
from cloud_agent.api._owner_auth import _owned_job, require_agent_owner
from cloud_agent.api._workspace_access import _workspace_broker_for_job, _workspace_id
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_log_store,
    get_storage,
    require_store,
)
from cloud_agent.application.job_admission import _create_job_as
from cloud_agent.domain.execution import TERMINAL_STATES
from cloud_agent.entitlement import REPO_PATTERN
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import (
    AgentAttemptTelemetry,
    AgentJobCancelResponse,
    AgentJobCreate,
    AgentJobListResponse,
    AgentJobResponse,
    AgentThreadArchiveResponse,
    AgentThreadPinResponse,
    AgentThreadRenameRequest,
    AgentThreadRenameResponse,
)
from cloud_agent.terminal_coordination import resume_settled_terminal

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        AttemptCommands,
        ConversationCommands,
        ConversationQueries,
        ExecutionQueries,
        HostPoolRepository,
        McpConnectionRepository,
        SandboxRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )


router = APIRouter(prefix="/v1/agent")

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _JobsStorage(Protocol):
    """The capabilities the job surface reaches the store for.

    Ten of them, and four are reached through a helper rather than spelled
    here: `application.job_admission._create_job_as` is typed against
    `_JobCreationStorage`, which names `attachments` among them.

    Four of these are reached through a helper rather than spelled here,
    and leaving them out is not a narrowing: `cancel_agent_job` hands the whole
    store to `_workspace_access._workspace_broker_for_job`, and `create_agent_job`
    hands it to `application.job_admission._create_job_as`, which is typed against
    `_JobCreationStorage` (`conversation_commands`, `mcp`, `skills`,
    `source_control`). mypy says so at the call site, which is the point of
    declaring capabilities rather than trusting a reader to trace them.
    """

    attachments: AttachmentRepository
    attempt_commands: AttemptCommands
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    execution_queries: ExecutionQueries
    host_pool: HostPoolRepository
    mcp: McpConnectionRepository
    sandbox: SandboxRepository
    skills: SkillRepository
    source_control: SourceControlPort


def _require_store(store: _JobsStorage | None) -> _JobsStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


async def _job_usage(log_store: Any, job_id: str) -> dict[str, Any]:
    """Read one job's spend and token totals from the billing ledger.

    Deliberately the ledger and not the agent's own report: a run that
    misstates its usage — which is a thing models do — cannot change what the
    owner is shown, and it is the same source the budget check already trusts.
    Absent on a deployment with no log store, rather than a fabricated zero.
    """
    usage: dict[str, Any] = {}
    cost_getter = getattr(log_store, "get_agent_job_cost", None)
    if cost_getter is not None:
        with contextlib.suppress(Exception):
            usage["spent_usd"] = await cost_getter(job_id)
    usage_getter = getattr(log_store, "get_agent_job_usage", None)
    if usage_getter is not None:
        with contextlib.suppress(Exception):
            totals = await usage_getter(job_id)
            usage["tokens_in"] = int(totals.get("tokens_in", 0))
            usage["tokens_out"] = int(totals.get("tokens_out", 0))
            usage["model_calls"] = int(totals.get("calls", 0))
    return usage


@router.post("/jobs", response_model=AgentJobResponse, status_code=201)
async def create_agent_job(
    body: AgentJobCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentJobResponse:
    """Queue a new agent job for the authenticated user.

    An ``Idempotency-Key`` names this one submission: a resend with the same
    key returns the job the first send created, and the same key under
    different content is a 409 ``idempotency_conflict``.
    """
    job_store = _require_store(store)
    job = await _create_job_as(
        body,
        user,
        job_store=job_store,
        app_credentials=app_credentials,
        gateway=gateway,
        client_request_id=idempotency_key,
    )
    return _job_response(job)


@router.get("/jobs", response_model=AgentJobListResponse)
async def list_agent_jobs(
    limit: int = Query(50, ge=1, le=200),
    archived: bool = Query(False),
    repo: str | None = Query(None, pattern=REPO_PATTERN, max_length=140),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentJobListResponse:
    """List jobs from the authenticated user's active or archived threads.

    ``repo`` narrows the page to one project. It is a filter over what the
    caller already owns, not an access grant, so an unentitled repo is not an
    error here — it simply matches none of their rows.
    """
    job_store = _require_store(store)
    jobs = await job_store.conversation_queries.list_jobs(
        user_id=user["user_id"], limit=limit, archived=archived, repo=repo
    )
    return AgentJobListResponse(jobs=[_job_response(job) for job in jobs])


@router.get("/jobs/{job_id}", response_model=AgentJobResponse)
async def get_agent_job(
    job_id: str,
    include_resources: bool = Query(True),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
    log_store: Any | None = Depends(get_log_store),
) -> AgentJobResponse:
    """Fetch one of the caller's agent jobs, with what it has spent so far."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    attempt_telemetry = await job_store.execution_queries.job_attempts_telemetry(job_id=job_id)
    telemetry = attempt_telemetry[-1] if attempt_telemetry else None
    # Existing clients keep the newest detail. The retry-aware client opts out
    # and requests heavy rows only for the attempt currently selected.
    if telemetry and include_resources:
        telemetry = await job_store.execution_queries.job_turn_telemetry(
            job_id=job_id, attempt_id=attempt_telemetry[-1]["attempt_id"]
        )
    return _job_response(
        job,
        await _job_usage(log_store, job_id),
        telemetry,
        attempt_telemetry,
    )


@router.get("/jobs/{job_id}/attempts/{attempt_id}/telemetry", response_model=AgentAttemptTelemetry)
async def get_agent_attempt_telemetry(
    job_id: str,
    attempt_id: int = Path(..., ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentAttemptTelemetry:
    """Read measurements for one attempt belonging to the caller's job."""
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    telemetry = await job_store.execution_queries.job_turn_telemetry(
        job_id=job_id, attempt_id=attempt_id
    )
    if telemetry is None:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return AgentAttemptTelemetry(
        **_attempt_summary_response(telemetry).model_dump(),
        resources=_attempt_resources(telemetry),
    )


@router.post("/jobs/{job_id}/cancel", response_model=AgentJobCancelResponse)
async def cancel_agent_job(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentJobCancelResponse:
    """Request cancellation of one of the caller's agent jobs.

    Queued jobs cancel immediately; a running job is flagged and the worker
    performs the fenced terminal transition when it next heartbeats.
    """
    job_store = _require_store(store)
    owned = await _owned_job(job_store.conversation_queries, job_id, user)
    state = await job_store.attempt_commands.request_cancel(job_id=job_id, user_id=user["user_id"])
    if state in TERMINAL_STATES:
        resumed = True
        if owned.get("thread_sandbox") is not None:
            settled_broker = await _workspace_broker_for_job(job_store, owned)
            resumed = await resume_settled_terminal(
                _workspace_id(owned),
                job_id=job_id,
                broker=settled_broker,
            )
        if resumed:
            await job_store.sandbox.mark_terminal_resume_complete(job_id=job_id)
    job = await job_store.conversation_queries.get_job(job_id)
    return AgentJobCancelResponse(
        id=job_id,
        state=state or (job or {}).get("state", "unknown"),
        cancel_requested=bool((job or {}).get("cancel_requested")),
    )


async def _set_agent_thread_archived(
    *,
    job_id: str,
    archived: bool,
    user: dict[str, Any],
    store: _JobsStorage | None,
) -> AgentThreadArchiveResponse:
    """Set archive state without exposing whether another user's job exists."""
    job_store = _require_store(store)
    result = await job_store.conversation_commands.set_thread_archived(
        job_id=job_id,
        user_id=user["user_id"],
        archived=archived,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return AgentThreadArchiveResponse(
        thread_id=result["thread_id"],
        archived=archived,
        archived_at=_iso(result["archived_at"]),
    )


@router.post("/jobs/{job_id}/archive", response_model=AgentThreadArchiveResponse)
async def archive_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentThreadArchiveResponse:
    """Archive the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=True, user=user, store=store)


@router.delete("/jobs/{job_id}/archive", response_model=AgentThreadArchiveResponse)
async def restore_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentThreadArchiveResponse:
    """Restore the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=False, user=user, store=store)


@router.patch("/jobs/{job_id}/title", response_model=AgentThreadRenameResponse)
async def rename_agent_thread(
    job_id: str,
    body: AgentThreadRenameRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentThreadRenameResponse:
    """Rename the entire conversation containing an owned job."""
    result = await _require_store(store).conversation_commands.set_thread_title(
        job_id=job_id, user_id=user["user_id"], title=body.title
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return AgentThreadRenameResponse(**result)


async def _set_agent_thread_pinned(
    *,
    job_id: str,
    pinned: bool,
    user: dict[str, Any],
    store: _JobsStorage | None,
) -> AgentThreadPinResponse:
    """Set pin state without exposing whether another user's job exists."""
    job_store = _require_store(store)
    result = await job_store.conversation_commands.set_thread_pinned(
        job_id=job_id,
        user_id=user["user_id"],
        pinned=pinned,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return AgentThreadPinResponse(
        thread_id=result["thread_id"],
        pinned=pinned,
        pinned_at=_iso(result["pinned_at"]),
    )


@router.post("/jobs/{job_id}/pin", response_model=AgentThreadPinResponse)
async def pin_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentThreadPinResponse:
    """Pin the entire task thread containing an owned job."""
    return await _set_agent_thread_pinned(job_id=job_id, pinned=True, user=user, store=store)


@router.delete("/jobs/{job_id}/pin", response_model=AgentThreadPinResponse)
async def unpin_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> AgentThreadPinResponse:
    """Unpin the entire task thread containing an owned job."""
    return await _set_agent_thread_pinned(job_id=job_id, pinned=False, user=user, store=store)
