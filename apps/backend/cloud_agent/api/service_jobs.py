"""Headless on-call jobs: the door the gateway's alert relay dispatches through.

The codex on-call pipeline used to hand its analysis to a GitHub Actions
workflow — an ephemeral runner, billed per minute, that died with the org's
Actions quota (2026-08-05) and could reach neither the deployment nor its
database. This surface replaces that hand-off: the relay creates a job here,
the job runs on this platform's own runner with a per-attempt inference grant,
and the relay polls the result back and posts it to the alert's Slack thread.

Why a separate door rather than the owner surface: there is deliberately no
programmatic owner credential on this service (``authenticate_agent_owner``
documents the deletion), and resurrecting one for a relay would hand it a
session-shaped key that opens every owner route. This door instead:

- authenticates a **dedicated dispatch token** (``AGENT_ONCALL_DISPATCH_TOKEN``)
  the same way the runner's claim door authenticates its own — one credential,
  one door, blast radius bounded to on-call jobs. ``ADMIN_TOKEN`` still works,
  because admin legitimately outranks it.
- pins every job to **one configured service account**
  (``AGENT_ONCALL_USER_ID``), a gateway user whose role decides which models
  the grant may carry. The caller cannot choose whose quota it spends.
- creates jobs through :func:`cloud_agent.api.routes._create_job_as` — the
  owner route's own checks (model resolution, repo entitlement, ref pinning),
  not a parallel path that drifts.

Reads are fenced to that same service account: a job id belonging to anyone
else answers 404 here, exactly as the owner surface answers for a job the
session does not own.

Every decision fails closed. No dispatch token and no admin token: 401. No
configured service user: 503, because the deployment cannot run on-call jobs
at all and the operator — not the caller — is who can fix it.
"""

from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from cloud_agent.api._job_views import (
    _EVENT_PAGE_SIZE,
    _event_response,
    _job_response,
)
from cloud_agent.api._owner_auth import _owned_job
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_storage,
    require_agent_access,
    require_store,
    verify_admin_access,
)
from cloud_agent.application.job_admission import _create_job_as
from cloud_agent.logging_utils import get_logger
from cloud_agent.schemas import (
    AgentJobCancelResponse,
    AgentJobCreate,
    AgentJobEventsResponse,
    AgentJobResponse,
)

if TYPE_CHECKING:
    from cloud_agent.ports import (
        AttachmentRepository,
        AttemptCommands,
        ConversationCommands,
        ConversationQueries,
        ExecutionQueries,
        McpConnectionRepository,
        SkillRepository,
        SourceControlRepository,
    )

logger = get_logger(__name__)


def _require_store(store: _ServiceJobsStorage | None) -> _ServiceJobsStorage:
    """This surface's capabilities, or 503 when no database is configured."""
    return require_store(store)


class _ServiceJobsStorage(Protocol):
    """The capabilities this module's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """

    attachments: AttachmentRepository
    attempt_commands: AttemptCommands
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    execution_queries: ExecutionQueries
    mcp: McpConnectionRepository
    skills: SkillRepository
    source_control: SourceControlRepository


router = APIRouter(prefix="/v1/agent/service/oncall")

ENV_ONCALL_DISPATCH_TOKEN = "AGENT_ONCALL_DISPATCH_TOKEN"
ENV_ONCALL_USER_ID = "AGENT_ONCALL_USER_ID"

#: Reserved metadata mark. Stamped by the platform after caller metadata so a
#: body cannot un-mark itself, and read by anything that wants to tell an
#: on-call analysis from an owner's thread (the web UI, spend reports).
ONCALL_METADATA_KEY = "oncall"


async def verify_oncall_dispatch(
    request: Request,
    authorization: str | None = Header(None),
    gateway: Any = Depends(get_gateway_client),
) -> str:
    """Gate for the machine-to-machine on-call dispatch.

    Prefers the dedicated ``AGENT_ONCALL_DISPATCH_TOKEN``: the relay lives on
    the gateway host next to alert producers, and the credential it holds
    should open exactly one door — on-call jobs — not the whole admin surface.
    ``ADMIN_TOKEN`` (via :func:`verify_admin_access`) still works, both for
    migration and because admin legitimately outranks a dispatch credential.

    The dedicated token opens nothing else: no other route reads it, and to
    every other authenticator it is just an invalid credential. An **unset**
    token opens nothing here either — the comparison requires both sides.
    """
    configured = (os.environ.get(ENV_ONCALL_DISPATCH_TOKEN) or "").strip()
    presented = (
        authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    )
    if configured and presented and secrets.compare_digest(presented, configured):
        return "oncall-dispatch"
    return await verify_admin_access(request, authorization, gateway)


def _oncall_owner() -> dict[str, Any]:
    """The service account every on-call job runs as.

    Configured, never caller-chosen: the grant minted at claim time spends this
    user's quota and is clamped to what this user's role may reach, so letting
    the request body name the user would let the relay's credential spend any
    account on the deployment. 503 when unconfigured — the caller did nothing
    wrong, and retrying with another credential will not help.
    """
    user_id = (os.environ.get(ENV_ONCALL_USER_ID) or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "unavailable",
                    "message": (
                        f"{ENV_ONCALL_USER_ID} is not configured; this deployment "
                        "cannot run on-call jobs."
                    ),
                }
            },
        )
    return {"user_id": user_id, "authenticated": True}


@router.post("/jobs", response_model=AgentJobResponse, status_code=201)
async def create_oncall_job(
    body: AgentJobCreate,
    _caller: str = Depends(verify_oncall_dispatch),
    store: _ServiceJobsStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentJobResponse:
    """Queue one on-call analysis job for the configured service account.

    The body is the owner surface's own :class:`AgentJobCreate`, so the relay
    chooses the runtime, model, repository and prompt — and every one of those
    choices passes the owner route's checks under the service account: the
    model must resolve for that user, the repository must be entitled (for a
    headless account that means the deployment's ``AGENT_REPO_ALLOWLIST``), and
    a ``base_ref`` is pinned to the commit it names right now. Skill selection
    has the same semantics: omitted IDs use this service account's defaults;
    the relay can send ``skill_ids=[]`` to opt out explicitly.
    """
    job_store = _require_store(store)
    user = _oncall_owner()
    await require_agent_access(gateway, user["user_id"])
    job = await _create_job_as(
        body,
        user,
        job_store=job_store,
        app_credentials=app_credentials,
        gateway=gateway,
        reserved_metadata={ONCALL_METADATA_KEY: True},
    )
    logger.info(
        "oncall_job_created",
        extra={"event": "oncall_job_created", "job_id": job["id"], "runtime": body.runtime},
    )
    return _job_response(job)


@router.get("/jobs/{job_id}", response_model=AgentJobResponse)
async def get_oncall_job(
    job_id: str,
    _caller: str = Depends(verify_oncall_dispatch),
    store: _ServiceJobsStorage | None = Depends(get_storage),
) -> AgentJobResponse:
    """Return one on-call job's state, for the relay's poll loop.

    Fenced to the service account: a job owned by anyone else is 404 here, so
    the dispatch credential cannot be used to read other tenants' jobs.
    """
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, _oncall_owner())
    return _job_response(job)


@router.get("/jobs/{job_id}/events", response_model=AgentJobEventsResponse)
async def list_oncall_job_events(
    job_id: str,
    after: int = Query(0, ge=0, description="Return events with a global id greater than this."),
    limit: int = Query(_EVENT_PAGE_SIZE, ge=1, le=1000),
    _caller: str = Depends(verify_oncall_dispatch),
    store: _ServiceJobsStorage | None = Depends(get_storage),
) -> AgentJobEventsResponse:
    """Page through an on-call job's event log, fenced like the job itself.

    The relay reads these once, at completion: the final ``message`` event is
    the analysis, and a ``tool_result`` with exit code 0 is what lets it call
    the analysis grounded rather than hallucinated.
    """
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, _oncall_owner())
    events = await job_store.execution_queries.list_events_after(
        job_id=job_id, after_id=after, limit=limit
    )
    return AgentJobEventsResponse(
        events=[_event_response(event) for event in events],
        next_cursor=events[-1]["id"] if events else after,
    )


@router.post("/jobs/{job_id}/cancel", response_model=AgentJobCancelResponse)
async def cancel_oncall_job(
    job_id: str,
    _caller: str = Depends(verify_oncall_dispatch),
    store: _ServiceJobsStorage | None = Depends(get_storage),
) -> AgentJobCancelResponse:
    """Request cancellation of one on-call job.

    The relay calls this when its await deadline passes or the incident
    resolves while the analysis is still running — the answer is already
    stale, so the tokens it would keep spending buy nothing.

    Queued jobs cancel immediately; a running job is flagged and the worker
    performs the fenced terminal transition at its next heartbeat, after which
    the reaper stops renewing the attempt's grant and the sandbox credential
    dies on its own TTL. Unlike the owner route there is no terminal-resume
    settling here: on-call jobs open no interactive terminals, and reaching for
    the workspace broker would couple this door to a service it never needs.
    """
    job_store = _require_store(store)
    user = _oncall_owner()
    await _owned_job(job_store.conversation_queries, job_id, user)
    state = await job_store.attempt_commands.request_cancel(job_id=job_id, user_id=user["user_id"])
    job = await job_store.conversation_queries.get_job(job_id)
    return AgentJobCancelResponse(
        id=job_id,
        state=state or (job or {}).get("state", "unknown"),
        cancel_requested=bool((job or {}).get("cancel_requested")),
    )
