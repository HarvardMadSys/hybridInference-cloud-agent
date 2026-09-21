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
    _goal_view,
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
from cloud_agent.goals.models import GoalConflict
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
    GoalClearedResponse,
    GoalRequest,
    GoalResponse,
    GoalRevisionRequest,
)
from cloud_agent.terminal_coordination import resume_settled_terminal

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        AttemptCommands,
        ConversationCommands,
        ConversationQueries,
        ExecutionQueries,
        GoalRepository,
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
    goals: GoalRepository
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
    # This is the read the goal band renders from, so it is the one that pays
    # for the round count. The count is derived rather than stored (see
    # the goal repository), and a list of conversations must not be charged for
    # it.
    if job.get("goal") is not None:
        job = {**job, "goal": await job_store.goals.get_goal(thread_id=str(job["thread_id"]))}
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


def _no_such_thread_error(thread_id: str) -> HTTPException:
    """A goal read or write against a conversation this owner does not have."""
    return HTTPException(
        status_code=404,
        detail={"error": {"type": "not_found", "message": f"No such task: {thread_id}"}},
    )


def _no_such_goal_error() -> HTTPException:
    """A goal write against a conversation that no longer has one."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "goal_missing",
                "message": "This conversation has had its goal cleared. Set a new one first.",
            }
        },
    )


def _goal_conflict_error() -> HTTPException:
    """A goal write made against a revision the goal has already moved past."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "goal_revision_conflict",
                "message": "This goal changed while you were editing it. Reload and try again.",
            }
        },
    )


@router.get("/threads/{thread_id}/goal", response_model=GoalResponse)
async def get_agent_goal(
    thread_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> GoalResponse:
    """The conversation's goal, or the fact that it has none.

    Answered from the thread rather than from a turn, and answered with the
    *control revision* even when there is no goal: that revision is what the
    next write's ``expected_revision`` is compared against, so a client can
    create the first goal on a conversation it just read without a second
    round trip.
    """
    job_store = _require_store(store)
    state = await job_store.goals.get_thread_goal_state(
        user_id=user["user_id"], thread_id=thread_id
    )
    if state is None:
        raise _no_such_thread_error(thread_id)
    return GoalResponse(
        thread_id=thread_id,
        goal=_goal_view(state["goal"]),
        revision=int(state["revision"]),
    )


@router.put("/threads/{thread_id}/goal", response_model=GoalResponse)
async def set_agent_goal(
    thread_id: str,
    body: GoalRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> GoalResponse:
    """Create, replace or edit the conversation's completion condition.

    A new objective is a new authorization: it mints a new goal identity, starts
    the round budget over, and becomes active. Editing the cap of an active goal
    leaves the identity and the rounds spent alone; editing a paused one leaves
    it paused, because changing the words is not resuming it.

    ``revision`` is what makes two edits from two tabs resolvable — the second is
    refused with a 409 rather than silently overwriting the first. Omitted means
    "write it whatever it currently says", which is what a first set wants.
    """
    job_store = _require_store(store)
    try:
        goal = await job_store.goals.set_goal(
            thread_id=thread_id,
            user_id=user["user_id"],
            goal=body.goal,
            expected_revision=body.revision,
        )
    except GoalConflict as exc:
        raise _goal_conflict_error() from exc
    if goal is None:
        raise _no_such_thread_error(thread_id)
    logger.info(
        "agent_goal_set",
        extra={
            "event": "agent_goal_set",
            "thread_id": thread_id,
            "goal_id": goal["goal_id"],
            "goal_revision": goal["revision"],
            "max_rounds": goal["max_rounds"],
        },
    )
    return GoalResponse(
        thread_id=thread_id,
        goal=_goal_view(goal),
        revision=int(goal["revision"]),
    )


@router.post("/threads/{thread_id}/goal/pause", response_model=GoalResponse)
async def pause_agent_goal(
    thread_id: str,
    body: GoalRevisionRequest | None = None,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> GoalResponse:
    """Stop the goal continuing itself; leave the turn that is already running.

    Queued automatic rounds are cancelled with it, so a round the goal asked for
    cannot start after the owner stopped it. A *running* round is deliberately
    left alone: "stop after this turn" and "stop this turn" are different
    requests, and the second one is the existing stop button.
    """
    job_store = _require_store(store)
    state = await job_store.goals.get_thread_goal_state(
        user_id=user["user_id"], thread_id=thread_id
    )
    if state is None:
        raise _no_such_thread_error(thread_id)
    goal = state["goal"]
    if goal is None:
        raise _no_such_goal_error()
    expected = body.revision if body is not None else None
    if expected is not None and expected != int(state["revision"]):
        raise _goal_conflict_error()
    if goal["state"] == "active":
        paused = await job_store.goals.pause_goal(
            thread_id=thread_id, revision=int(goal["revision"]), reason="user"
        )
        if paused is None:
            raise _goal_conflict_error()
        goal = paused
    logger.info(
        "agent_goal_paused_by_owner",
        extra={"event": "agent_goal_paused_by_owner", "thread_id": thread_id},
    )
    return GoalResponse(thread_id=thread_id, goal=_goal_view(goal), revision=int(goal["revision"]))


@router.post("/threads/{thread_id}/goal/resume", response_model=GoalResponse)
async def resume_agent_goal(
    thread_id: str,
    body: GoalRevisionRequest | None = None,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> GoalResponse:
    """Start a paused goal moving again, and record the obligation to schedule.

    The owner's authorization is the trigger: no model report is needed to start
    working again, and the wakeup this writes is what makes the next round
    happen — the request does not wait for it, and does not need to.
    """
    job_store = _require_store(store)
    state = await job_store.goals.get_thread_goal_state(
        user_id=user["user_id"], thread_id=thread_id
    )
    if state is None:
        raise _no_such_thread_error(thread_id)
    goal = state["goal"]
    if goal is None:
        raise _no_such_goal_error()
    expected = body.revision if body is not None else None
    if expected is not None and expected != int(state["revision"]):
        raise _goal_conflict_error()
    if goal["state"] == "paused" and int(goal["rounds_used"]) >= int(goal["max_rounds"]):
        # Resuming would authorize a round the budget cannot pay for. The owner
        # has to raise the cap first, and telling them that here is better than
        # a goal that becomes active and pauses itself on the next decision.
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "goal_rounds_exhausted",
                    "message": (
                        f"This goal has used all {goal['max_rounds']} of its automatic "
                        "rounds. Raise the limit, then resume it."
                    ),
                }
            },
        )
    if goal["state"] != "paused":
        # Already active or complete. Active needs nothing; complete needs a new
        # authorization, which is a set, not a resume.
        return GoalResponse(
            thread_id=thread_id, goal=_goal_view(goal), revision=int(goal["revision"])
        )
    resumed = await job_store.goals.resume_goal(
        thread_id=thread_id, user_id=user["user_id"], revision=int(goal["revision"])
    )
    if resumed is None:
        raise _goal_conflict_error()
    logger.info(
        "agent_goal_resumed",
        extra={"event": "agent_goal_resumed", "thread_id": thread_id},
    )
    return GoalResponse(
        thread_id=thread_id, goal=_goal_view(resumed), revision=int(resumed["revision"])
    )


@router.delete("/threads/{thread_id}/goal", response_model=GoalClearedResponse)
async def clear_agent_goal(
    thread_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _JobsStorage | None = Depends(get_storage),
) -> GoalClearedResponse:
    """Remove the goal, and every automatic round that was queued for it.

    The revision moves and the identity is cleared, so a report written about
    the removed objective can never match whatever the owner sets next.
    """
    job_store = _require_store(store)
    cleared = await job_store.goals.clear_goal(thread_id=thread_id, user_id=user["user_id"])
    state = await job_store.goals.get_thread_goal_state(
        user_id=user["user_id"], thread_id=thread_id
    )
    if state is None:
        raise _no_such_thread_error(thread_id)
    logger.info(
        "agent_goal_cleared",
        extra={"event": "agent_goal_cleared", "thread_id": thread_id, "cleared": cleared},
    )
    return GoalClearedResponse(
        thread_id=thread_id,
        cleared=cleared,
        revision=int(state["revision"]),
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
