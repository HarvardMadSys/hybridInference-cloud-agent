"""Agent-sandbox job API (``/v1/agent/jobs``) — issue #1041, P0.

Two audiences, two authentication schemes:

- **Owners** (users, via this service's session — the ``HttpOnly`` cookie
  ``/v1/session/login`` sets, or the same token as a bearer): create, inspect,
  list, cancel jobs, browse the retained workspace, and subscribe to the event stream. Every
  read and mutation is scoped to ``user_id`` so one user can never touch
  another's job. The same dependency also requires
  the Gateway's explicit ``agent.use`` permission.
- **Workers** (the sandbox runner, via a per-attempt capability token minted at
  claim time): report events, renew the lease, and drive fenced Agent-turn
  transitions. The token carries the
  ``(job_id, attempt_id, lease_generation)`` fence, so a worker whose lease was
  reaped is rejected by the store even though its token still parses. Those
  rejections surface as **409**, the agreed signal for "you lost the lease,
  stop now".

The SSE endpoint streams the append-only event log with ``Last-Event-ID``
resume. It deliberately sets ``text/event-stream``, which the timeout
middleware recognizes to apply the long stream cap instead of the 120s request
timeout (the #865/#889 lesson: long-lived SSE must not inherit the short cap).
Because that cap is finite by design, the stream is resumable: clients
reconnect with ``Last-Event-ID`` and continue from the global cursor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

from cloud_agent.api.deps import (
    bearer_token,
    get_agent_app_credentials,
    get_agent_gitlab_oauth,
    get_agent_job_store,
    get_current_user,
    get_gateway_client,
    get_grant_client,
    get_log_store,
    looks_like_agent_token,
)
from cloud_agent.api.hosts import RESIDENCY_TTL_S, _mint_residency_grant
from cloud_agent.broker_routing import (
    ENV_BROKER_TOKEN,
    broker_for_attempt,
    broker_for_job,
    hydrate_shared_broker,
)
from cloud_agent.entitlement import (
    REPO_PATTERN,
    RepoNotAllowed,
    repos_for_user,
    require_entitled_repo,
)
from cloud_agent.execution_profiles import (
    RESERVED_EXECUTION_METADATA_KEYS,
    default_execution_profile,
)
from cloud_agent.gateway import GatewayError
from cloud_agent.github_app import AppNotInstalled, GitHubAppError
from cloud_agent.host_pool import hash_secret
from cloud_agent.identity import SESSION_COOKIE
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp_catalogue import catalogue as mcp_catalogue
from cloud_agent.mcp_registry import connection_states, registry_for_user
from cloud_agent.schemas import (
    BASE_REF_METADATA_KEY,
    EVENT_TYPE_PATTERN,
    PLATFORM_REASONING_EFFORTS,
    RESET_REASONING_EFFORT,
    AgentAttemptSummary,
    AgentAttemptTelemetry,
    AgentConfigResponse,
    AgentConversation,
    AgentConversationListResponse,
    AgentFollowUpRequest,
    AgentGitWorkspaceResponse,
    AgentJobCancelResponse,
    AgentJobCreate,
    AgentJobEvent,
    AgentJobEventsResponse,
    AgentJobListResponse,
    AgentJobResponse,
    AgentProject,
    AgentProjectListResponse,
    AgentSessionArchiveRequest,
    AgentSessionArchiveResponse,
    AgentSessionDeleteResponse,
    AgentSessionListResponse,
    AgentSessionResponse,
    AgentSessionSelection,
    AgentSessionState,
    AgentTerminalRequest,
    AgentTerminalResponse,
    AgentTerminalSessionCreateRequest,
    AgentTerminalSessionInputRequest,
    AgentTerminalSessionListResponse,
    AgentTerminalSessionResizeRequest,
    AgentTerminalSessionResponse,
    AgentThreadArchiveResponse,
    AgentThreadEvent,
    AgentThreadEventsResponse,
    AgentThreadMessageResponse,
    AgentThreadPinResponse,
    AgentThreadRenameRequest,
    AgentThreadRenameResponse,
    AgentThreadResponse,
    AgentThreadStatsResponse,
    AgentWorkspaceResponse,
    AgentWorkspaceWriteRequest,
    AttemptResources,
    GitHubConnectionResponse,
    GitHubConnectRequest,
    HostResidencyEstablishResponse,
    McpIntegrationResponse,
    McpServerSummary,
    OAuthConnectRequest,
    ProcessObservation,
    RepoBranchesResponse,
    SourceControlAccount,
    SourceControlIntegrationsResponse,
    SourceControlProviderResponse,
    SourceControlRepository,
    ToolCallResources,
    WorkerAckResponse,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerContinuityRequest,
    WorkerEventRequest,
    WorkerEventResponse,
    WorkerFinishRequest,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerResourcesRequest,
    WorkerSandboxAbandonRequest,
    WorkerSandboxAdmissionRequest,
    WorkerSandboxProcessRequest,
    WorkerSandboxReserveRequest,
    WorkerSandboxResidencyRequest,
    WorkerSandboxRunningRequest,
    WorkerSandboxSettleRequest,
    WorkerTerminalSuspendRequest,
    WorkerThreadSandbox,
)
from cloud_agent.source_control import (
    OAuthStateError,
    SourceControlError,
    consume_oauth_state,
    github_authorization_url,
    issue_oauth_state,
)
from cloud_agent.storage.store import RUNNING, SUCCEEDED, TERMINAL_STATES, request_fingerprint
from cloud_agent.task_titles import generate_thread_title
from cloud_agent.terminal_coordination import resume_settled_terminal
from cloud_agent.thread_stats import summarize_thread
from cloud_agent.tokens import (
    SCOPE_FULL,
    SCOPE_MCP,
    InvalidAgentToken,
    InvalidHostToken,
    mint_worker_token,
    parse_host_token,
    parse_worker_token,
)
from cloud_agent.visible_models import (
    agent_canonical_model,
    agent_model_reasoning_efforts,
    agent_visible_models,
)
from cloud_agent_host.egress import (
    EgressPolicyError,
    build_policy_from_env as build_egress_policy,
)
from cloud_agent_host.patch_gate import branch_name_for
from cloud_agent_host.runtimes import (
    registered_runtimes,
    runtime_reasoning_efforts,
    runtimes_supporting_mcp,
)
from cloud_agent_host.workspace_broker_client import (
    WorkspaceBrokerClient,
    WorkspaceBrokerError,
)
from cloud_agent_host.workspace_browser import (
    WorkspacePathError,
    normalize_workspace_path,
)

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

logger = get_logger(__name__)
#: The MCP relay's address **as a sandbox can reach it** — a host-local relay,
#: not this process's own bind address, which is why it is configured rather
#: than derived. Unset means this deployment offers no MCP: the claim carries
#: no address, and a runtime asked for servers anyway refuses rather than
#: resolving them against the inference gateway, which does not answer for MCP.
ENV_SANDBOX_MCP_URL = "AGENT_SANDBOX_MCP_URL"
# The host fork request is bounded to 180s. Keep one short recovery margin so
# a second API worker cannot take over while the first is still recording its
# receipt; a crashed worker becomes retryable without waiting for the 15-minute
# stalled-saga reconciler.
SESSION_FORK_COORDINATOR_LEASE_S = 210.0


router = APIRouter(prefix="/v1/agent")

# Same anti-buffering headers the completions SSE path uses: `no-transform`
# stops intermediary CDNs from buffering to compress, `X-Accel-Buffering: no`
# disables nginx buffering.
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_POLL_INTERVAL_S = 1.0
_KEEPALIVE_EVERY_N_POLLS = 15
_EVENT_PAGE_SIZE = 500

# The schema constrains event_type on the way in, but the SSE writer must be
# safe on its own: rows written before that constraint existed (or by any
# future non-HTTP writer) must not be able to break out of the ``event:`` field
# and inject frames into the owner's stream.
_SAFE_EVENT_TYPE = re.compile(EVENT_TYPE_PATTERN)
_TERMINAL_ID_PATTERN = r"^term_[A-Za-z0-9_-]{1,80}$"

# The clone credential is narrowed at the point it is minted, not merely by
# convention: the App also holds `contents: write` for the publisher, and an
# installation token inherits every permission the App has unless it is asked
# for less. Without this the runner would be handed push rights it must not
# have, on a host that runs untrusted repository code.
_CLONE_SCOPE = {"contents": "read"}


async def authenticate_agent_owner(
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """Authenticate an Agent owner from this service's session.

    There is **one** owner credential here: the session token
    :mod:`cloud_agent.identity` mints, carried either in the ``HttpOnly``
    cookie ``/v1/session/login`` sets — which is all the web app holds — or as
    a bearer, for a programmatic caller that has one. ``get_current_user``
    accepts both, preferring the cookie.

    **The API-key branch was deleted on 2026-08-04**, not merely disabled. It
    resolved an ``hyi-*`` key through the gateway's ``api_keys`` table, which
    did not move; nothing in this repository ever set the store it read, and
    ``GatewayClient`` answers "may this user sign in", never "who owns this
    key". A branch with no producer answers 503 in every real deployment while
    looking implemented, and invites a later reader to "fix" it by reaching
    back into the gateway's database — the coupling this split exists to
    remove. Its ported test is recorded in MIGRATION.md's discarded register.
    Restoring a programmatic owner credential means adding a gateway contract
    under ``contracts/``, not resurrecting this.

    One namespace is still refused **before** the session verifier sees it: a
    worker capability token (``ajt.*``) is the sandbox's credential, and a
    credential that escaped the sandbox must be told apart from an unknown one
    rather than reported as a missing session. The gateway spelled this as an
    allowlist of inference paths because the same dependency also guarded
    ``/v1/chat/completions``; this service serves no inference route, so it
    collapses to a flat 403 ``insufficient_scope``. The cookie still wins when
    both are present, which is the precedence ``session_token`` documents.
    """
    presented = bearer_token(authorization)
    if not request.cookies.get(SESSION_COOKIE) and looks_like_agent_token(presented):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": "This credential may only be used for model inference.",
                }
            },
        )

    return await get_current_user(request=request, authorization=authorization)


async def require_agent_owner(
    request: Request,
    user: dict[str, Any] = Depends(authenticate_agent_owner),
    gateway=Depends(get_gateway_client),
) -> dict[str, Any]:
    """Require current Gateway admission for every owner read and write.

    Identity selects the owner; the Gateway's explicit agent.use permission
    admits the request. Local ownership checks still fence every resource.
    """
    from cloud_agent.api.deps import require_agent_access

    decision = await require_agent_access(gateway, user["user_id"])
    return {
        **user,
        "agent_access": decision.public().model_dump(),
        "is_admin": decision.permits("agent.admin"),
    }


def _sse_frame(event: dict[str, Any]) -> str:
    """Render one stored event as an SSE frame with a safe event name."""
    event_type = event["event_type"]
    # fullmatch, not match: `$` also matches before a trailing newline, so
    # `match()` would accept "message\n" — exactly the value this guard
    # exists to reject, since the newline splits the SSE frame.
    if not _SAFE_EVENT_TYPE.fullmatch(event_type or ""):
        event_type = "malformed"
    data = json.dumps(_event_response(event).model_dump(), separators=(",", ":"))
    return f"id: {event['id']}\nevent: {event_type}\ndata: {data}\n\n"


def _require_store(store: AgentJobStore | None) -> AgentJobStore:
    """Return the store or fail with a clear 503 when the DB is absent."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "unavailable",
                    "message": "Agent jobs require a configured database.",
                }
            },
        )
    return store


def _iso(value: Any) -> str | None:
    """Render a timestamp column as ISO-8601 (or None)."""
    return value.isoformat() if value is not None else None


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


def _egress_tiers() -> tuple[str | None, str | None]:
    """Report the deployment's per-phase egress posture, if it has one."""
    try:
        policy = build_egress_policy()
    except EgressPolicyError:
        # A misconfigured policy is the runner's problem to refuse at preflight,
        # not a reason to fail an owner reading their own job.
        return None, None
    return policy.tier_for("setup").value, policy.tier_for("agent").value


def _attempt_duration_ms(telemetry: dict[str, Any] | None) -> int | None:
    """How long the selected attempt ran, or None while it still is."""
    if not telemetry:
        return None
    started, finished = telemetry.get("started_at"), telemetry.get("finished_at")
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _attempt_resources(telemetry: dict[str, Any] | None) -> AttemptResources | None:
    """Shape the resource rollup, or None when no accounting record was reported.

    A row whose counters are all null is still returned: it carries
    ``coverage`` and is how a reader learns the turn was not measured, which
    is a different statement from a turn that used nothing.
    """
    if not telemetry or telemetry.get("sampler_version") is None:
        return None
    return AttemptResources(
        cpu_usec=telemetry.get("cpu_usec"),
        mem_peak_bytes=telemetry.get("mem_peak_bytes"),
        mem_avg_bytes=telemetry.get("mem_avg_bytes"),
        io_read_bytes=telemetry.get("io_read_bytes"),
        io_write_bytes=telemetry.get("io_write_bytes"),
        net_rx_bytes=telemetry.get("net_rx_bytes"),
        net_tx_bytes=telemetry.get("net_tx_bytes"),
        sample_count=telemetry.get("sample_count") or 0,
        coverage=float(telemetry.get("coverage") or 0.0),
        sampler_version=telemetry.get("sampler_version"),
        tool_time_ms=telemetry.get("tool_time_ms"),
        tool_calls=[ToolCallResources(**call) for call in telemetry.get("tool_calls") or []],
        tool_calls_truncated=bool(telemetry.get("tool_calls_truncated")),
        process_observer=telemetry.get("process_observer"),
        process_observation_complete=bool(telemetry.get("process_observation_complete")),
        process_events_lost=int(telemetry.get("process_events_lost") or 0),
        processes_truncated=bool(telemetry.get("processes_truncated")),
        processes=[ProcessObservation(**process) for process in telemetry.get("processes") or []],
    )


def _attempt_summary_response(telemetry: dict[str, Any]) -> AgentAttemptSummary:
    """Shape one attempt without losing its identity at the API boundary."""
    return AgentAttemptSummary(
        attempt_id=telemetry["attempt_id"],
        attempt_no=telemetry["attempt_no"],
        continuity_mode=telemetry.get("continuity_mode"),
        context_status=telemetry.get("context_status"),
        duration_ms=_attempt_duration_ms(telemetry),
    )


def _job_response(
    job: dict[str, Any],
    usage: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    attempt_telemetry: list[dict[str, Any]] | None = None,
) -> AgentJobResponse:
    """Shape a store job row for the owner-facing API."""
    usage = usage or {}
    setup_tier, agent_tier = _egress_tiers()
    metadata = dict(job.get("metadata") or {})
    base_ref = metadata.pop(BASE_REF_METADATA_KEY, None)
    if not isinstance(base_ref, str):
        base_ref = None
    sandbox = job.get("thread_sandbox") or {}
    native_fork_available = bool(
        sandbox.get("shared_live") is True
        and sandbox.get("native_fork_capable") is True
        and sandbox.get("native_state") == "ready"
        and sandbox.get("fork_operation_id") is None
        and sandbox.get("active_attempt_id") is None
        and bool(sandbox.get("last_settled_job_id"))
        and job.get("state") == SUCCEEDED
        and isinstance(job.get("native_turn_no"), int)
        and not isinstance(job.get("native_turn_no"), bool)
        and int(job["native_turn_no"]) > 0
    )
    return AgentJobResponse(
        id=job["id"],
        thread_id=job.get("thread_id"),
        parent_job_id=job.get("parent_job_id"),
        turn_no=job.get("turn_no") or 1,
        repo=job["repo"],
        task_prompt=job["task_prompt"],
        title=job.get("title"),
        title_source=job.get("title_source"),
        runtime=job["runtime"],
        model=job["model"],
        execution_profile=job.get("execution_profile", "trusted-v1"),
        reasoning_effort=job.get("reasoning_effort"),
        base_ref=base_ref,
        base_sha=job["base_sha"],
        output_branch=branch_name_for(
            sandbox.get("origin_thread_id") or job.get("thread_id") or job["id"]
        ),
        state=job["state"],
        cancel_requested=job["cancel_requested"],
        current_attempt_id=job["current_attempt_id"],
        published_pr_url=job["published_pr_url"],
        published_commit_sha=job.get("published_commit_sha"),
        detail=job["detail"],
        forked_from_job_id=job.get("fork_source_job_id"),
        metadata=metadata or None,
        mcp_servers=list(job.get("mcp_servers") or []),
        created_at=_iso(job["created_at"]),
        updated_at=_iso(job["updated_at"]),
        pinned_at=_iso(job.get("pinned_at")),
        archived_at=_iso(job.get("archived_at")),
        spent_usd=usage.get("spent_usd"),
        tokens_in=usage.get("tokens_in"),
        tokens_out=usage.get("tokens_out"),
        model_calls=usage.get("model_calls"),
        setup_egress_tier=setup_tier,
        agent_egress_tier=agent_tier,
        telemetry_attempt_id=(telemetry or {}).get("attempt_id"),
        continuity_mode=(telemetry or {}).get("continuity_mode"),
        context_status=(telemetry or {}).get("context_status"),
        resident_state=sandbox.get("resident_state"),
        sandbox_id=sandbox.get("sandbox_id"),
        workspace_shared=bool(sandbox.get("shared_live")),
        native_state=sandbox.get("native_state"),
        fork_anchor_job_id=sandbox.get("fork_anchor_job_id"),
        native_fork_available=native_fork_available,
        workspace_publication_state=sandbox.get("workspace_publication_state"),
        workspace_epoch=sandbox.get("workspace_epoch"),
        started_at=_iso((telemetry or {}).get("started_at")),
        finished_at=_iso((telemetry or {}).get("finished_at")),
        duration_ms=_attempt_duration_ms(telemetry),
        resources=_attempt_resources(telemetry),
        attempt_telemetry=[
            _attempt_summary_response(attempt) for attempt in (attempt_telemetry or [])
        ],
    )


def _event_response(event: dict[str, Any]) -> AgentJobEvent:
    """Shape a store event row for the API."""
    return AgentJobEvent(
        id=event["id"],
        attempt_id=event["attempt_id"],
        seq=event["seq"],
        event_type=event["event_type"],
        payload=event["payload"],
        created_at=_iso(event["created_at"]),
    )


async def _owned_job(
    store: AgentJobStore,
    job_id: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    """Fetch a job the caller owns, or raise 404.

    A job owned by someone else is reported as 404 rather than 403 so job ids
    cannot be probed for existence across accounts.
    """
    job = await store.get_job(job_id)
    if job is None or job["user_id"] != user["user_id"] or job.get("deleted_at") is not None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    return job


# ── Owner endpoints ────────────────────────────────────────────────────


@router.get("/config", response_model=AgentConfigResponse)
async def get_agent_config(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentConfigResponse:
    """What this deployment will actually accept.

    The composer showed a repository, a branch, a runtime and a model as static
    labels while submitting different hardcoded values, so the UI described a
    job nobody was running. A picker has to be built from the same answers the
    create endpoint enforces, or it is decoration.
    """
    setup_tier, agent_tier = _egress_tiers()
    # This user's repositories, not the deployment's: what the picker offers
    # has to be what the create endpoint will accept for *them*.
    repos = await repos_for_user(user["user_id"], store=store, app_credentials=app_credentials)
    return AgentConfigResponse(
        repos=repos,
        runtimes=registered_runtimes(),
        # Same predicate the create endpoint enforces and the sandbox's calls
        # will hit — not /v1/models, which answers for the *browsing* user and
        # offered models whose first agent call then 404ed.
        models=await agent_visible_models(gateway, user_id=user["user_id"]),
        # What each runtime can express, and which models take the parameter at
        # all. The composer offers the platform's levels narrowed to the first
        # and gated on the second, and no control when either is empty — which
        # is also what an older gateway, answering no domains, produces.
        runtime_reasoning_efforts=runtime_reasoning_efforts(),
        model_reasoning_efforts=await agent_model_reasoning_efforts(
            gateway, user_id=user["user_id"]
        ),
        # This user's connected servers, not the deployment's: same reason the
        # repository list above is theirs. Public fields only — McpServerSummary
        # has nowhere to put a URL or a credential, which is how it stays that
        # way.
        mcp_servers=[
            McpServerSummary(**entry)
            for entry in (
                await _mcp_servers_for_user(store, user["user_id"]) if store is not None else []
            )
        ],
        mcp_runtimes=runtimes_supporting_mcp(),
        setup_egress_tier=setup_tier,
        agent_egress_tier=agent_tier,
        # Both halves, and read from what is actually wired rather than from
        # the environment: an App the platform can mint tokens from, and at
        # least one repository this user may work on. Either alone leaves a
        # composer that cannot produce a runnable job.
        # The historical field name is narrower than its meaning in the UI:
        # it gates whether the composer can create a runnable job. An
        # operator-allowlisted public repository is runnable without a GitHub
        # App (the runner clones it anonymously), so requiring App credentials
        # here made the API accept the job while the browser disabled the form.
        github_connected=bool(repos),
        # OAuth must start on the authenticated integrations page so the
        # server can issue state bound to this user. Never expose the old raw
        # operator URL here: it had no CSRF binding.
        github_install_url="/agents/integrations" if app_credentials is not None else None,
    )


def _oauth_error(exc: Exception) -> HTTPException:
    """Map OAuth failures to a stable response without leaking credentials."""
    if isinstance(exc, OAuthStateError):
        message = str(exc)
        error_type = "oauth_state_invalid"
    else:
        message = "The source-control authorization could not be completed. Please try again."
        error_type = "source_control_connect_failed"
    return HTTPException(
        status_code=400,
        detail={"error": {"type": error_type, "message": message}},
    )


async def _connect_github_for_user(
    *,
    body: GitHubConnectRequest | OAuthConnectRequest,
    user_id: str,
    store: AgentJobStore,
    app_credentials: Any,
) -> GitHubConnectionResponse:
    await consume_oauth_state(
        store,
        state=body.state,
        user_id=user_id,
        provider="github",
    )
    user_token = await app_credentials.exchange_user_code(body.code)
    installations = await app_credentials.installations_for_user(user_token)
    install_url = None
    if not installations:
        github_install_url = (os.getenv("AGENT_GITHUB_APP_INSTALL_URL") or "").strip()
        if github_install_url:
            install_state = await issue_oauth_state(store, user_id=user_id, provider="github")
            install_url = github_authorization_url(github_install_url, state=install_state)
    for installation in installations:
        await store.record_repo_grant(
            user_id=user_id,
            installation_id=installation["installation_id"],
            account_login=installation.get("account_login"),
        )
    logger.info(
        "agent_github_connected",
        extra={"event": "agent_github_connected", "installations": len(installations)},
    )
    return GitHubConnectionResponse(
        connections=await store.list_repo_grants(user_id=user_id),
        repos=await repos_for_user(user_id, store=store, app_credentials=app_credentials),
        install_url=install_url,
    )


async def _source_control_providers(
    *,
    user_id: str,
    store: AgentJobStore,
    app_credentials: Any | None,
    gitlab_oauth: Any | None,
) -> list[SourceControlProviderResponse]:
    """Build user-scoped provider status with no credential material."""
    github_install_url = (os.getenv("AGENT_GITHUB_APP_INSTALL_URL") or "").strip()
    github_configured = (
        app_credentials is not None
        and bool(github_install_url)
        and bool(getattr(app_credentials, "user_authorization_configured", True))
    )
    github_connect_url = None
    github_error = None
    if github_configured:
        try:
            state = await issue_oauth_state(store, user_id=user_id, provider="github")
            github_connect_url = app_credentials.user_authorization_url(state)
        except (GitHubAppError, SourceControlError) as exc:
            github_configured = False
            github_error = str(exc)

    grants = await store.list_repo_grants(user_id=user_id)
    # The install URL is the wrong way to *start* a connection — GitHub sends
    # an already-installed App there straight to its settings page instead of
    # through the callback — but that settings page is the only route to
    # adding or removing the App's repositories, and GitHub routes user and
    # organization installations to the right one without us having to know
    # which this is. State rides along because a changed installation comes
    # back through the same callback, and it is issued separately from the
    # connect state so that following one link cannot invalidate the other.
    github_manage_url = None
    if github_configured and grants:
        try:
            manage_state = await issue_oauth_state(store, user_id=user_id, provider="github")
            github_manage_url = github_authorization_url(github_install_url, state=manage_state)
        except SourceControlError as exc:
            # A malformed install URL no longer breaks connecting, so it must
            # not surface as a connection error — but an operator still needs
            # to hear that the management link is missing because of it.
            logger.warning(
                "agent_github_manage_url_unavailable",
                extra={"event": "agent_github_manage_url_unavailable", "reason": str(exc)},
            )

    github_repos = await repos_for_user(
        user_id, store=store, app_credentials=app_credentials, env={"AGENT_REPO_ALLOWLIST": ""}
    )
    providers = [
        SourceControlProviderResponse(
            provider="github",
            configured=github_configured,
            connected=bool(grants),
            connect_url=github_connect_url,
            manage_url=github_manage_url,
            capabilities=["Agent checkout", "branch discovery", "draft PR publishing"],
            accounts=[
                SourceControlAccount(
                    id=str(grant["installation_id"]),
                    label=grant.get("account_login") or f"Installation {grant['installation_id']}",
                )
                for grant in grants
            ],
            repositories=[
                SourceControlRepository(
                    id=repo,
                    name=repo,
                    web_url=f"https://github.com/{repo}",
                )
                for repo in github_repos
            ],
            error=github_error,
        )
    ]

    gitlab_connection = await store.get_gitlab_connection(user_id=user_id)
    gitlab_projects: list[dict[str, Any]] = []
    gitlab_error = None
    gitlab_connect_url = None
    if gitlab_oauth is not None:
        try:
            gitlab_connect_url = await gitlab_oauth.authorization_url(store, user_id=user_id)
            if gitlab_connection:
                gitlab_projects = await gitlab_oauth.projects_for_connection(
                    store, gitlab_connection
                )
        except SourceControlError:
            if gitlab_connection:
                gitlab_error = "GitLab repositories are temporarily unavailable."
            else:
                gitlab_error = "GitLab authorization is temporarily unavailable."
    providers.append(
        SourceControlProviderResponse(
            provider="gitlab",
            configured=gitlab_oauth is not None,
            connected=gitlab_connection is not None,
            connect_url=gitlab_connect_url,
            capabilities=["project discovery"],
            accounts=(
                [
                    SourceControlAccount(
                        id=str(gitlab_connection["id"]),
                        label=gitlab_connection["username"],
                        web_url=gitlab_connection.get("web_url"),
                    )
                ]
                if gitlab_connection
                else []
            ),
            repositories=[SourceControlRepository(**project) for project in gitlab_projects],
            error=gitlab_error,
        )
    )
    return providers


async def _mcp_integrations(*, user_id: str, store: AgentJobStore) -> list[McpIntegrationResponse]:
    """The catalogue, marked with what this user has connected.

    The whole catalogue, not just the connected part: a page that listed only
    connections would have nowhere to show what is on offer, which is the
    question someone with no connections is asking.

    ``configured`` is false for every provider until the connect flow lands
    (#49's next change) — connecting takes an OAuth round trip this service
    cannot make yet, and a Connect button that cannot connect is worse than the
    "Coming soon" the UI already renders for a provider in this state. What is
    live here is everything *after* connecting: a row that exists resolves,
    scopes a job and authorizes tool calls, and can be removed.

    Read through ``connection_states`` rather than straight from the store, so
    a connection that exists but buys nothing says why. Reporting ``connected``
    from the row alone would put *Connected (octocat)* on this page beside a
    composer offering nothing — same data, two answers, and no way for the
    owner to tell which one is wrong.
    """
    states = {state.server: state for state in await connection_states(store, user_id=user_id)}
    return [
        McpIntegrationResponse(
            name=provider.name,
            label=provider.label,
            description=provider.description,
            capabilities=list(provider.capabilities),
            configured=False,
            connected=provider.name in states,
            account_label=(
                states[provider.name].account_label if provider.name in states else None
            ),
            error=states[provider.name].error if provider.name in states else None,
        )
        for provider in mcp_catalogue()
    ]


@router.get("/integrations", response_model=SourceControlIntegrationsResponse)
async def list_source_control_integrations(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlIntegrationsResponse:
    """Return configured/connected state for source control and MCP servers."""
    job_store = _require_store(store)
    return SourceControlIntegrationsResponse(
        providers=await _source_control_providers(
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
            gitlab_oauth=gitlab_oauth,
        ),
        mcp=await _mcp_integrations(user_id=user["user_id"], store=job_store),
    )


@router.delete("/integrations/mcp/{server}", response_model=SourceControlIntegrationsResponse)
async def disconnect_mcp_integration(
    server: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlIntegrationsResponse:
    """Remove this user's credential for one server.

    Scoped to the caller in the delete itself rather than by looking the row up
    first: there is no ordering of two statements that makes "check it is mine,
    then delete it" as hard to get wrong as a ``WHERE user_id`` that cannot be
    forgotten.

    Running jobs are not cancelled. Their next tool call to this server fails
    with the server reported as unavailable, because the relay resolves the
    owner's connections per call rather than at job start — so disconnecting
    takes effect immediately without anything having to hunt down live jobs.
    """
    job_store = _require_store(store)
    removed = await job_store.delete_mcp_connection(user_id=user["user_id"], server=server)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": f"You have no {server!r} MCP connection.",
                }
            },
        )
    logger.info(
        "agent_mcp_disconnected",
        extra={"event": "agent_mcp_disconnected", "server": server},
    )
    return SourceControlIntegrationsResponse(
        providers=await _source_control_providers(
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
            gitlab_oauth=gitlab_oauth,
        ),
        mcp=await _mcp_integrations(user_id=user["user_id"], store=job_store),
    )


@router.post("/integrations/github/connect", response_model=GitHubConnectionResponse)
async def connect_github_integration(
    body: OAuthConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Complete a GitHub App authorization protected by one-time state."""
    job_store = _require_store(store)
    if app_credentials is None:
        raise HTTPException(status_code=503, detail="GitHub App is not configured")
    try:
        return await _connect_github_for_user(
            body=body,
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except Exception as exc:
        if not isinstance(exc, (OAuthStateError, SourceControlError)):
            logger.warning("agent_github_connect_failed", exc_info=True)
        raise _oauth_error(exc) from exc


@router.post("/integrations/gitlab/connect", response_model=SourceControlProviderResponse)
async def connect_gitlab_integration(
    body: OAuthConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlProviderResponse:
    """Verify a GitLab user and persist only encrypted, refreshable tokens."""
    job_store = _require_store(store)
    if gitlab_oauth is None:
        raise HTTPException(status_code=503, detail="GitLab OAuth is not configured")
    try:
        verifier = await consume_oauth_state(
            job_store,
            state=body.state,
            user_id=user["user_id"],
            provider="gitlab",
            cipher=gitlab_oauth.cipher,
        )
        if verifier is None:
            raise OAuthStateError("The source-control authorization expired or is invalid.")
        token = await gitlab_oauth.exchange_code(body.code, verifier)
        profile = await gitlab_oauth.verify_user(token["access_token"])
        projects = await gitlab_oauth.list_projects(token["access_token"])
        previous_connection = await job_store.get_gitlab_connection(user_id=user["user_id"])
        connection_id = await job_store.upsert_gitlab_connection(
            user_id=user["user_id"],
            external_user_id=profile["id"],
            username=profile["username"],
            display_name=profile.get("name"),
            web_url=profile.get("web_url"),
            access_token_ciphertext=gitlab_oauth.cipher.encrypt(token["access_token"]),
            refresh_token_ciphertext=gitlab_oauth.cipher.encrypt(token["refresh_token"]),
            expires_at=gitlab_oauth.token_expiry(token),
        )
        if previous_connection is not None:
            try:
                await gitlab_oauth.revoke(
                    gitlab_oauth.cipher.decrypt(previous_connection["access_token_ciphertext"])
                )
            except Exception:
                logger.warning("agent_gitlab_previous_token_revoke_failed", exc_info=True)
    except Exception as exc:
        if not isinstance(exc, (OAuthStateError, SourceControlError)):
            logger.warning("agent_gitlab_connect_failed", exc_info=True)
        raise _oauth_error(exc) from exc
    logger.info("agent_gitlab_connected", extra={"event": "agent_gitlab_connected"})
    return SourceControlProviderResponse(
        provider="gitlab",
        configured=True,
        connected=True,
        capabilities=["project discovery"],
        accounts=[
            SourceControlAccount(
                id=str(connection_id), label=profile["username"], web_url=profile.get("web_url")
            )
        ],
        repositories=[SourceControlRepository(**project) for project in projects],
    )


@router.delete(
    "/integrations/gitlab/connections/{connection_id}",
    response_model=SourceControlIntegrationsResponse,
)
async def disconnect_gitlab_integration(
    connection_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlIntegrationsResponse:
    """Revoke GitLab best-effort, then always remove the local credential."""
    job_store = _require_store(store)
    connection = await job_store.get_gitlab_connection(user_id=user["user_id"])
    if connection is None or connection["id"] != connection_id:
        raise HTTPException(status_code=404, detail="No such GitLab connection")
    if gitlab_oauth is not None:
        try:
            access_token = await gitlab_oauth.access_token_for_connection(job_store, connection)
            await gitlab_oauth.revoke(access_token)
        except Exception:
            logger.warning("agent_gitlab_revoke_failed", exc_info=True)
    await job_store.delete_gitlab_connection(user_id=user["user_id"], connection_id=connection_id)
    return SourceControlIntegrationsResponse(
        providers=await _source_control_providers(
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
            gitlab_oauth=gitlab_oauth,
        ),
        # This response replaces the page's whole state, so it has to carry the
        # part this endpoint did not touch. Omitted, it would default to an
        # empty list and read as "you have no MCP connections".
        mcp=await _mcp_integrations(user_id=user["user_id"], store=job_store),
    )


@router.delete(
    "/integrations/github/connections/{installation_id}",
    response_model=GitHubConnectionResponse,
)
async def disconnect_github_integration(
    installation_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Drop only the authenticated user's GitHub installation grant."""
    job_store = _require_store(store)
    await job_store.revoke_repo_grant(user_id=user["user_id"], installation_id=installation_id)
    return GitHubConnectionResponse(
        connections=await job_store.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store, app_credentials=app_credentials
        ),
    )


@router.post("/github/connect", response_model=GitHubConnectionResponse)
async def connect_github(
    body: GitHubConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Record the installations this user proved they can reach.

    The browser sends the code GitHub handed it; the platform exchanges it for
    a token that speaks *as that user* and asks GitHub which installations they
    can see. Nothing the browser asserts is trusted — an installation id posted
    directly would be exactly the confused deputy this whole path exists to
    prevent, so the id is only ever taken from GitHub's own answer.
    """
    job_store = _require_store(store)
    if app_credentials is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "unavailable",
                    "message": "No GitHub App is configured for this deployment.",
                }
            },
        )
    try:
        return await _connect_github_for_user(
            body=body,
            user_id=user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except Exception as exc:
        if not isinstance(exc, (OAuthStateError, SourceControlError)):
            logger.warning("agent_github_connect_failed", exc_info=True)
        raise _oauth_error(exc) from exc


@router.delete("/github/connect/{installation_id}", response_model=GitHubConnectionResponse)
async def disconnect_github(
    installation_id: int,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Drop one connection. Uninstalling on GitHub is the other half."""
    job_store = _require_store(store)
    await job_store.revoke_repo_grant(user_id=user["user_id"], installation_id=installation_id)
    return GitHubConnectionResponse(
        connections=await job_store.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store, app_credentials=app_credentials
        ),
    )


@router.get("/branches", response_model=RepoBranchesResponse)
async def list_repo_branches(
    repo: str = Query(..., description="owner/name"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> RepoBranchesResponse:
    """List a repository's branches, for the composer's branch picker.

    Entitlement first: this reads a repository through the platform's own
    installation, so without the check it would be a way to enumerate branches
    of any repository the App happens to cover.
    """
    job_store = _require_store(store)
    try:
        await require_entitled_repo(
            repo, user["user_id"], store=job_store, app_credentials=app_credentials
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    if app_credentials is None:
        return RepoBranchesResponse()
    try:
        found = await app_credentials.branches_for_repo(repo)
    except Exception:
        logger.warning("agent_branches_unavailable", extra={"event": "agent_branches_unavailable"})
        return RepoBranchesResponse()
    return RepoBranchesResponse(default=found.get("default"), branches=found.get("branches", []))


async def _require_resolvable_model(
    model: str,
    *,
    gateway: Any,
    user: dict[str, Any],
) -> str:
    """Resolve the requested model to its canonical id, or refuse the job.

    Fail-fast on a model the job's first call would 404 on: rejecting here
    costs a 400, while accepting costs a claim, a clone and an attempt before
    the sandbox's first model call dies with an error that reads like a broken
    model.

    **Returns the canonical id, and the caller stores that.** Validating an
    alias and then recording the alias was the bug this signature exists to
    prevent: the gateway mints a grant against canonical ids, so a job carrying
    `claude-3-opus-latest` produced a grant whose `allowed_models` matched
    nothing. The job was created, and died at its first inference call with a
    404 from a model that plainly exists.
    """
    canonical = await agent_canonical_model(model, gateway, user_id=user["user_id"])
    if canonical is not None:
        return canonical
    visible = await agent_visible_models(gateway, user_id=user["user_id"])
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "type": "model_not_available",
                "message": (
                    f"Model {model!r} is not available to agent jobs on this deployment. "
                    f"Available: {', '.join(visible[:20]) or 'none'}."
                ),
            }
        },
    )


async def _mcp_servers_for_user(store: AgentJobStore, user_id: str) -> list[dict[str, Any]]:
    """The MCP servers *this user* has connected, for the composer.

    Names and display metadata only. A server's URL and its credential never
    leave this process: the sandbox reaches an upstream through the relay,
    which is the entire reason resolution lives beside the relay rather than
    beside the picker. :class:`McpServerSummary` has no field either could land
    in, so that stays true without a filter at this call site.

    Only connections that currently work: an expired credential, an unreadable
    one, or a deployment with no encryption key all leave the picker empty
    rather than offering a server whose first tool call is already known to
    fail. ``connection_states`` decides that, and the Integrations page reports
    the reason from the same source.
    """
    registry = await registry_for_user(store, user_id=user_id)
    servers: list[dict[str, Any]] = []
    for name in registry.names:
        server = registry.get(name)
        if server is None:  # pragma: no cover - names() is derived from servers
            continue
        servers.append(
            {
                "name": server.name,
                "description": server.description,
                "default": server.default,
                "tools": sorted(server.tools),
            }
        )
    return servers


async def _resolve_mcp_servers(
    requested: list[str] | None, *, runtime: str, store: AgentJobStore, user_id: str
) -> list[str]:
    """Settle which MCP servers a new job runs with, or refuse to create it.

    Resolved against the *creating user's* connections. That is the signature
    this whole feature turns on: a server someone else connected is unknown
    here, and there is no deployment-wide list to fall back to.

    Both failures are refusals rather than silent drops, and for the same
    reason: a job is submitted because someone wants a task done, and a task
    written around a tool the agent turns out not to have does not fail — it
    quietly produces the wrong work, having spent the budget.
    """
    connected = await _mcp_servers_for_user(store, user_id)
    names = sorted(str(entry.get("name") or "") for entry in connected)
    if requested is None:
        # ``None`` means the job did not choose, and takes everything its owner
        # connected — connecting a server is the opt-in. An explicit ``[]``
        # means it wants none.
        servers = sorted(
            str(entry.get("name") or "") for entry in connected if entry.get("default")
        )
    else:
        # Unknown names fail at creation rather than let the job discover
        # mid-run that the agent is missing the one capability it needed.
        unknown = sorted(set(requested) - set(names))
        if unknown:
            known = ", ".join(names) or "none yet"
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            f"you have not connected these MCP server(s): "
                            f"{', '.join(unknown)}. Connected: {known}. "
                            "Connect a server on the Integrations page."
                        ),
                    }
                },
            )
        servers = sorted(set(requested))
    if servers and runtime not in runtimes_supporting_mcp():
        supported = ", ".join(runtimes_supporting_mcp()) or "none"
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": (
                        f"the {runtime!r} runtime cannot be given MCP servers on this "
                        f"deployment. Runtimes that can: {supported}."
                    ),
                }
            },
        )
    return servers


def _require_registered_runtime(runtime: str) -> None:
    """400 unless ``runtime`` names a registered harness.

    The registry is the trust boundary (design D2): what may run is what this
    deployment registered, never whatever string a request carried.
    """
    known = registered_runtimes()
    if runtime not in known:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": (
                        f"unknown runtime {runtime!r}; this deployment registers: "
                        + ", ".join(known)
                    ),
                }
            },
        )


async def _require_offered_effort(
    effort: str | None,
    *,
    runtime: str,
    model: str,
    gateway: Any,
    user: dict[str, Any],
) -> str | None:
    """400 unless ``effort`` is one this runtime *and* this model both accept.

    The offered set is the intersection, and it is checked rather than clamped.
    Clamping looks kinder and is worse: glm-5.3 has no ``medium``, so a
    ``medium`` quietly served as ``high`` would bill and behave as a depth
    nobody chose while reporting the one they did — the same lie as a flag that
    resolves to nothing, which is what this whole control exists to end.

    Args:
        effort: The requested level, or ``None`` for Auto.
        runtime: The harness this turn will run on.
        model: The canonical model id, already resolved.
        gateway: Client for the gateway's internal contracts.
        user: The authenticated owner.

    Returns:
        The value to store: ``None`` for Auto, else the validated level.

    Raises:
        HTTPException: 400 when the pair does not offer this value.
    """
    if effort is None or effort == RESET_REASONING_EFFORT:
        # The sentinel means unset wherever it appears. On a follow-up it also
        # has to be told apart from "inherit" (that caller checks it before
        # getting here); on a create there is nothing to inherit, so the two
        # spellings are the same answer and neither is a 400.
        return None
    runtime_levels = runtime_reasoning_efforts().get(runtime, [])
    # The model's entry is read as a yes/no — does it take the parameter at
    # all — and not for its contents. Per-model domains were tried and
    # withdrawn: the providers' documented lists disagreed with what their
    # APIs accept, so the levels are this platform's and the same everywhere.
    takes_effort = bool(
        (await agent_model_reasoning_efforts(gateway, user_id=user["user_id"])).get(model)
    )
    offered = (
        [level for level in PLATFORM_REASONING_EFFORTS if level in runtime_levels]
        if takes_effort
        else []
    )
    if effort in offered:
        return effort
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "type": "reasoning_effort_not_available",
                "message": (
                    f"Reasoning effort {effort!r} is not available for {runtime!r} "
                    f"with {model!r}. Available: {', '.join(offered) or 'none'}."
                ),
            }
        },
    )


async def _create_job_as(
    body: AgentJobCreate,
    user: dict[str, Any],
    *,
    job_store: AgentJobStore,
    app_credentials: Any | None,
    gateway: Any,
    reserved_metadata: dict[str, Any] | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Create one job for ``user``, shared by the owner and service doors.

    Extracted so the on-call service surface (``api/service_jobs.py``) creates
    jobs through exactly the checks the owner route runs — model resolution,
    repo entitlement, ref pinning — rather than through a parallel path that
    drifts. ``reserved_metadata`` is stamped *after* caller metadata for the
    same reason ``BASE_REF_METADATA_KEY`` is popped first: a display fact the
    platform asserts must not be spoofable by the request body.
    """
    # Fail here, not at claim time. An unregistered runtime used to travel all
    # the way into a queued row and burn a claim before anything noticed —
    # the composer only offers registered ids, so an arbitrary string can only
    # arrive from a direct API caller, and it deserves a 400, not a dead job.
    # The check lives in the helper rather than in the owner route because the
    # service door creates jobs through here too: a check only one door runs
    # is precisely the drift this extraction exists to prevent.
    _require_registered_runtime(body.runtime)
    # Canonical from here on. The grant the gateway mints names canonical
    # ids, so a job that stored the alias would carry a scope matching
    # nothing — created successfully, dead at its first inference call.
    model = await _require_resolvable_model(body.model, gateway=gateway, user=user)
    # Checked against the *resolved* model, not the requested spelling: the
    # domain belongs to the model the turn will actually call.
    reasoning_effort = await _require_offered_effort(
        body.reasoning_effort, runtime=body.runtime, model=model, gateway=gateway, user=user
    )
    # The requester chooses the repository and the platform later mints a real
    # installation token for it. Without this the two combine into a confused
    # deputy: name any repository the App reaches, and read it back through
    # your own job's events and patch.
    try:
        await require_entitled_repo(
            body.repo,
            user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    # A branch is resolved to the commit it points at *now*. The job stores the
    # sha: a branch moves, so a job that recorded "dev" would silently mean a
    # different tree by the time it ran, and the publisher applies its patch
    # onto a pinned commit.
    base_sha = body.base_sha
    if base_sha is None and body.base_ref and app_credentials is not None:
        try:
            base_sha = await app_credentials.resolve_ref(body.repo, body.base_ref)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": f"could not resolve {body.base_ref!r} in {body.repo}: {exc}",
                    }
                },
            ) from exc

    metadata = dict(body.metadata or {})
    # Reserved display fact: caller metadata must not be able to claim the
    # job was pinned from a different branch than the one the API resolved.
    metadata.pop(BASE_REF_METADATA_KEY, None)
    # Execution policy and its evidence are server-owned facts. Opaque
    # metadata must not be able to manufacture a contradictory audit trail.
    for key in RESERVED_EXECUTION_METADATA_KEYS:
        metadata.pop(key, None)
    if body.base_ref:
        metadata[BASE_REF_METADATA_KEY] = body.base_ref
    for key, value in (reserved_metadata or {}).items():
        metadata[key] = value
    # Shared-live is enforced globally at claim time, not represented as an
    # optional per-job requirement. Job metadata therefore remains caller data
    # plus server-owned execution facts, without a hidden rollout switch.
    mcp_servers = await _resolve_mcp_servers(
        body.mcp_servers, runtime=body.runtime, store=job_store, user_id=user["user_id"]
    )
    try:
        execution_profile = default_execution_profile().value
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": {"type": "invalid_configuration", "message": str(exc)}},
        ) from exc
    job = await job_store.create_job(
        user_id=user["user_id"],
        repo=body.repo,
        task_prompt=body.task_prompt,
        runtime=body.runtime,
        model=model,
        execution_profile=execution_profile,
        reasoning_effort=reasoning_effort,
        base_sha=base_sha,
        setup_script=body.setup_script,
        metadata=metadata or None,
        mcp_servers=mcp_servers,
        client_request_id=client_request_id,
        # Over the request as sent, not as resolved: a branch that moved
        # between two sends of one submission is still one submission.
        client_request_fingerprint=(
            request_fingerprint("create", body.model_dump(mode="json"))
            if client_request_id is not None
            else None
        ),
    )
    if job.get("error") == "client_request_conflict":
        raise _idempotency_conflict()
    logger.info(
        "agent_job_created",
        extra={"event": "agent_job_created", "job_id": job["id"], "runtime": body.runtime},
    )
    return job


@router.post("/jobs", response_model=AgentJobResponse, status_code=201)
async def create_agent_job(
    body: AgentJobCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobListResponse:
    """List jobs from the authenticated user's active or archived threads.

    ``repo`` narrows the page to one project. It is a filter over what the
    caller already owns, not an access grant, so an unentitled repo is not an
    error here — it simply matches none of their rows.
    """
    job_store = _require_store(store)
    jobs = await job_store.list_jobs(
        user_id=user["user_id"], limit=limit, archived=archived, repo=repo
    )
    return AgentJobListResponse(jobs=[_job_response(job) for job in jobs])


@router.get("/sessions", response_model=AgentSessionListResponse)
async def list_agent_sessions(
    limit: int = Query(50, ge=1, le=200),
    archived: bool = Query(False),
    include_summary: bool = Query(True),
    cursor: str | None = Query(None, max_length=1024),
    search: str | None = Query(None, max_length=200, pattern=r"^[^\x00]*$"),
    repo: str | None = Query(None, pattern=REPO_PATTERN, max_length=140),
    state: AgentSessionState | None = Query(None),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentSessionListResponse:
    """Search complete conversations by stable title, project and display state."""
    job_store = _require_store(store)
    try:
        result = await job_store.list_sessions(
            user_id=user["user_id"],
            archived=archived,
            limit=limit,
            cursor=cursor,
            search=(search.strip() or None) if search is not None else None,
            repo=repo,
            state=state,
            include_summary=include_summary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AgentSessionListResponse(
        sessions=[
            AgentSessionResponse(
                thread_id=session["thread_id"],
                title=session["title"],
                archived_at=_iso(session["archived_at"]),
                job=_job_response(session["job"]),
                can_delete=session["can_delete"],
                delete_block_reason=session.get("delete_block_reason"),
            )
            for session in result["sessions"]
        ],
        total=result["total"],
        next_cursor=result["next_cursor"],
        projects=result["projects"],
    )


def _raise_session_mutation_error(result: dict[str, Any]) -> None:
    """Translate atomic selection failures without exposing foreign ownership."""
    error = result.get("error")
    if error is None:
        return
    messages = {
        "not_found": "One or more selected sessions are no longer available.",
        "not_archived": "Archive every selected session before deleting it.",
        "session_active": "A selected session is still active. Wait for its work to finish.",
        "fork_in_progress": "A selected session is being forked. Wait for the fork to finish.",
        "publication_pending": "A selected session has unfinished publication. Wait for it to finish.",
    }
    raise HTTPException(
        status_code=404 if error == "not_found" else 409,
        detail={"error": {"type": error, "message": messages[error]}},
    )


@router.post("/sessions/archive", response_model=AgentSessionArchiveResponse)
async def archive_agent_sessions(
    body: AgentSessionArchiveRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentSessionArchiveResponse:
    """Archive or unarchive an explicit owner-selected snapshot atomically."""
    result = await _require_store(store).set_sessions_archived(
        thread_ids=body.thread_ids, user_id=user["user_id"], archived=body.archived
    )
    _raise_session_mutation_error(result)
    return AgentSessionArchiveResponse(
        sessions=[
            AgentThreadArchiveResponse(
                thread_id=session["thread_id"],
                archived=body.archived,
                archived_at=_iso(session["archived_at"]),
            )
            for session in result["sessions"]
        ]
    )


@router.post("/sessions/delete", response_model=AgentSessionDeleteResponse)
async def delete_agent_sessions(
    body: AgentSessionSelection,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentSessionDeleteResponse:
    """Permanently remove archived sessions from owner history, retaining audit data."""
    result = await _require_store(store).delete_sessions(
        thread_ids=body.thread_ids, user_id=user["user_id"]
    )
    _raise_session_mutation_error(result)
    return AgentSessionDeleteResponse(thread_ids=result["thread_ids"])


@router.get("/conversations", response_model=AgentConversationListResponse)
async def list_agent_conversations(
    q: str = Query("", max_length=200),
    repo: str | None = Query(None, pattern=REPO_PATTERN, max_length=140),
    include_archived: bool = Query(False),
    pinned: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, min_length=1, max_length=2048),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentConversationListResponse:
    """Search owned conversation titles and repositories across all history."""
    job_store = _require_store(store)
    try:
        page = await job_store.list_conversations(
            user_id=user["user_id"],
            q=q.strip(),
            repo=repo,
            include_archived=include_archived,
            pinned=pinned,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AgentConversationListResponse(
        conversations=[
            AgentConversation(
                job=_job_response(item["job"]),
                title=item["title"],
                archived=item["archived"],
                last_activity_at=_iso(item["last_activity_at"]),
            )
            for item in page["conversations"]
        ],
        next_cursor=page["next_cursor"],
    )


@router.get("/projects", response_model=AgentProjectListResponse)
async def list_agent_projects(
    archived: bool = Query(False),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentProjectListResponse:
    """Summarize the caller's projects for the sidebar's task tree."""
    job_store = _require_store(store)
    projects = await job_store.list_projects(user_id=user["user_id"], archived=archived)
    return AgentProjectListResponse(
        projects=[
            AgentProject(
                repo=project["repo"],
                task_count=project["task_count"],
                active_count=project["active_count"],
                last_activity_at=_iso(project["last_activity_at"]),
                pinned_count=project.get("pinned_count", 0),
                pinned_at=_iso(project.get("pinned_at")),
            )
            for project in projects
        ]
    )


@router.get("/jobs/{job_id}", response_model=AgentJobResponse)
async def get_agent_job(
    job_id: str,
    include_resources: bool = Query(True),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    log_store=Depends(get_log_store),
) -> AgentJobResponse:
    """Fetch one of the caller's agent jobs, with what it has spent so far."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    attempt_telemetry = await job_store.job_attempts_telemetry(job_id=job_id)
    telemetry = attempt_telemetry[-1] if attempt_telemetry else None
    # Existing clients keep the newest detail. The retry-aware client opts out
    # and requests heavy rows only for the attempt currently selected.
    if telemetry and include_resources:
        telemetry = await job_store.job_turn_telemetry(
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentAttemptTelemetry:
    """Read measurements for one attempt belonging to the caller's job."""
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    telemetry = await job_store.job_turn_telemetry(job_id=job_id, attempt_id=attempt_id)
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobCancelResponse:
    """Request cancellation of one of the caller's agent jobs.

    Queued jobs cancel immediately; a running job is flagged and the worker
    performs the fenced terminal transition when it next heartbeats.
    """
    job_store = _require_store(store)
    owned = await _owned_job(job_store, job_id, user)
    state = await job_store.request_cancel(job_id=job_id, user_id=user["user_id"])
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
            await job_store.mark_terminal_resume_complete(job_id=job_id)
    job = await job_store.get_job(job_id)
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
    store: AgentJobStore | None,
) -> AgentThreadArchiveResponse:
    """Set archive state without exposing whether another user's job exists."""
    job_store = _require_store(store)
    result = await job_store.set_thread_archived(
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadArchiveResponse:
    """Archive the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=True, user=user, store=store)


@router.delete("/jobs/{job_id}/archive", response_model=AgentThreadArchiveResponse)
async def restore_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadArchiveResponse:
    """Restore the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=False, user=user, store=store)


@router.patch("/jobs/{job_id}/title", response_model=AgentThreadRenameResponse)
async def rename_agent_thread(
    job_id: str,
    body: AgentThreadRenameRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadRenameResponse:
    """Rename the entire conversation containing an owned job."""
    result = await _require_store(store).set_thread_title(
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
    store: AgentJobStore | None,
) -> AgentThreadPinResponse:
    """Set pin state without exposing whether another user's job exists."""
    job_store = _require_store(store)
    result = await job_store.set_thread_pinned(
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
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadPinResponse:
    """Pin the entire task thread containing an owned job."""
    return await _set_agent_thread_pinned(job_id=job_id, pinned=True, user=user, store=store)


@router.delete("/jobs/{job_id}/pin", response_model=AgentThreadPinResponse)
async def unpin_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadPinResponse:
    """Unpin the entire task thread containing an owned job."""
    return await _set_agent_thread_pinned(job_id=job_id, pinned=False, user=user, store=store)


def _sandbox_context_lost_error() -> HTTPException:
    """Return the stable owner-facing error for an unrunnable retained chat."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "sandbox_context_lost",
                "message": (
                    "This chat's retained sandbox has an indeterminate or lost "
                    "context and cannot safely accept another message."
                ),
            }
        },
    )


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "idempotency_conflict",
                "message": "This Idempotency-Key was already used for a different submission.",
            }
        },
    )


def _session_archived_error() -> HTTPException:
    """Require an explicit unarchive before accepting another conversation turn."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "session_archived",
                "message": "Unarchive this session before continuing the conversation.",
            }
        },
    )


@router.post("/jobs/{job_id}/follow-ups", response_model=AgentJobResponse, status_code=201)
async def create_agent_follow_up(
    job_id: str,
    body: AgentFollowUpRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentJobResponse:
    """Append a turn and queue its member in the retained shared sandbox.

    An ``Idempotency-Key`` names this one submission, as for creation: a
    resend returns the turn the first send made, different content under the
    same key is a 409 ``idempotency_conflict``.
    """
    job_store = _require_store(store)
    parent = await _owned_job(job_store, job_id, user)
    if parent.get("archived_at") is not None:
        raise _session_archived_error()
    retained = parent.get("thread_sandbox")
    if isinstance(retained, dict) and (
        retained.get("context_status") == "lost"
        or retained.get("state") in {"stopped_dirty", "lost"}
    ):
        raise _sandbox_context_lost_error()
    # The harness is fixed per thread (design D2): continuity is the harness
    # session, and no other harness can continue it. The field survives as an
    # explicit no-op so existing callers that echo the thread's runtime keep
    # working; a *different* runtime is refused rather than silently starting
    # a turn the thread's memory cannot follow.
    if body.runtime is not None:
        _require_registered_runtime(body.runtime)
        if body.runtime != parent["runtime"]:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            f"this thread runs on {parent['runtime']!r} and a thread's "
                            "harness is fixed; start a new chat to use "
                            f"{body.runtime!r}"
                        ),
                    }
                },
            )
    # A follow-up may switch models; the switched-to model gets the same
    # fail-fast as a fresh job. An omitted model inherits the parent's, which
    # was validated when it was chosen.
    model = None
    if body.model:
        model = await _require_resolvable_model(body.model, gateway=gateway, user=user)
    # Effort is per turn, like the model, and inherits when omitted. Unlike the
    # model it needs a way *back*: null already means "no override, inherit", so
    # clearing an inherited effort takes the explicit reset sentinel. Validated
    # against the model this turn will run — which may be the switched-to one,
    # and a pair that was offered last turn need not be offered this one.
    reasoning_effort: str | None = None
    reset_effort = body.reasoning_effort == RESET_REASONING_EFFORT
    if body.reasoning_effort is not None and not reset_effort:
        reasoning_effort = await _require_offered_effort(
            body.reasoning_effort,
            runtime=parent["runtime"],
            model=model or parent["model"],
            gateway=gateway,
            user=user,
        )
    try:
        await require_entitled_repo(
            parent["repo"],
            user["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    # A thread's MCP servers are inherited by every turn, so a turn can end up
    # with tools it will not get a turn after the user disconnected a server
    # since the thread started. Refuse it rather than run a turn whose tools
    # silently vanish — the follow-up prompt was very likely written expecting
    # them. Runtime is fixed above; the optional field can only echo that value.
    #
    # **Checked on every turn.** Resolution now also
    # answers "is this still connected", which changes on its own, from the
    # Integrations page, between turns — leaving a plain follow-up to return 201
    # and fail at its first tool call, which is exactly the fail-fast this
    # function exists to provide.
    if parent.get("mcp_servers"):
        await _resolve_mcp_servers(
            list(parent["mcp_servers"]),
            # The runtime this turn will actually run on: an explicit echo or
            # the parent's value when omitted.
            runtime=body.runtime or parent["runtime"],
            store=job_store,
            user_id=user["user_id"],
        )
    job = await job_store.create_follow_up(
        parent_job_id=job_id,
        user_id=user["user_id"],
        prompt=body.prompt,
        runtime=body.runtime,
        # Canonical, or the parent's when omitted — never the alias the caller
        # typed, which the gateway's grant would not match.
        model=model if body.model else None,
        reasoning_effort=reasoning_effort,
        reset_reasoning_effort=reset_effort,
        client_request_id=idempotency_key,
        client_request_fingerprint=(
            request_fingerprint(
                "follow_up", {"parent_job_id": job_id, **body.model_dump(mode="json")}
            )
            if idempotency_key is not None
            else None
        ),
    )
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    if job.get("error") == "client_request_conflict":
        raise _idempotency_conflict()
    if job.get("error") == "session_archived":
        raise _session_archived_error()
    if job.get("error") == "sandbox_context_lost":
        # The durable store repeats the check under its thread/sandbox locks.
        # It can legitimately disagree with the advisory read above when the
        # active attempt becomes indeterminate during entitlement/tool checks.
        raise _sandbox_context_lost_error()
    logger.info(
        "agent_follow_up_created",
        extra={
            "event": "agent_follow_up_created",
            "job_id": job["id"],
            "thread_id": job.get("thread_id"),
            "parent_job_id": job.get("parent_job_id"),
        },
    )
    return _job_response(job)


@router.post("/jobs/{job_id}/fork", response_model=AgentJobResponse, status_code=201)
async def fork_agent_job(
    job_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    grants: Any = Depends(get_grant_client),
) -> AgentJobResponse:
    """Fork one settled native session into the same shared Docker workspace."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    not_settled = HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "not_settled",
                "message": "This turn is still active; wait for it to settle before forking it.",
            }
        },
    )
    if job["state"] not in TERMINAL_STATES:
        raise not_settled
    if job["state"] != SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "anchor_not_settled",
                    "message": "Only a successfully settled turn can be used as a Fork anchor.",
                }
            },
        )
    if job.get("thread_sandbox") is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_context_unavailable",
                    "message": "This historical chat has no shared native-session sandbox.",
                }
            },
        )

    sandbox = job.get("thread_sandbox") or {}
    if sandbox.get("shared_live") is not True:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_fork_unavailable",
                    "message": (
                        "This historical retained chat predates shared native-session "
                        "forks. Start a new chat to use same-workspace Fork."
                    ),
                }
            },
        )

    operation = await job_store.prepare_session_fork(
        source_job_id=job_id,
        user_id=user["user_id"],
        idempotency_key=idempotency_key or f"generated:{secrets.token_urlsafe(24)}",
    )
    if operation is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No such agent job: {job_id}"}},
        )
    error = operation.get("error")
    if error:
        error_contract = {
            "idempotency_conflict": (
                "idempotency_conflict",
                "This Idempotency-Key was already used for a different fork source.",
            ),
            "not_settled": (
                "not_settled",
                "This turn is still active; stop the run before forking it.",
            ),
            "anchor_not_settled": (
                "historical_snapshot_unavailable",
                "Native Fork requires a successfully completed assistant turn.",
            ),
            "not_shared_live": (
                "native_fork_unavailable",
                "This chat does not use a shared-native sandbox.",
            ),
            "native_fork_unavailable": (
                "native_fork_unavailable",
                "This retained sandbox version cannot fork from a historical turn.",
            ),
            "sandbox_not_ready": (
                "native_context_unavailable",
                "This chat's retained native context is not safe to fork.",
            ),
            "source_busy": (
                "sandbox_busy",
                "This chat is currently running a turn; retry after it settles.",
            ),
            "fork_in_progress": (
                "fork_in_progress",
                "Another native fork is already being prepared from this chat.",
            ),
            "resident_capacity_exhausted": (
                "resident_capacity_exhausted",
                "This shared sandbox has no free resident Agent CLI slot.",
            ),
            "native_context_unavailable": (
                "native_context_unavailable",
                "This chat has no settled native session to fork.",
            ),
            "runtime_mismatch": (
                "native_fork_unavailable",
                "The retained runtime no longer matches this chat.",
            ),
        }
        error_type, message = error_contract.get(
            str(error),
            ("native_fork_unavailable", "The retained native session cannot be forked now."),
        )
        raise HTTPException(
            status_code=409,
            detail={"error": {"type": error_type, "message": message}},
        )

    operation_id = str(operation["id"])

    if operation["state"] == "failed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_fork_failed",
                    "message": str(
                        operation.get("last_error") or "Native Fork could not complete."
                    ),
                }
            },
        )

    async def release_failed_fork_residencies(rows: dict[str, Any]) -> None:
        for role in ("source", "target"):
            residency = rows[role]
            grant_id = await job_store.release_residency(
                thread_id=str(residency["thread_id"]),
                generation=int(residency["generation"]),
                host=str(operation["host"]),
            )
            if grant_id and grants is not None:
                with contextlib.suppress(Exception):
                    await grants.revoke(grant_id)

    if operation["state"] == "preparing":
        # Resolve the host only for the state that still needs a native
        # mutation.  READY and MATERIALIZED are durable control-plane states:
        # a retry after a lost HTTP response must return/finalize the same
        # child even when the retaining host has since become unreachable.
        broker = await broker_for_job(job_store, job_id)
        if broker is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "sandbox_host_unavailable",
                        "message": (
                            "The host retaining this chat is not reachable for native Fork."
                        ),
                    }
                },
            )
        # Validate every immutable host input before minting or rotating either
        # residency. A malformed manifest is a control-plane defect, not a
        # reason to leak two renewable grants into an uncallable saga.
        source_job = operation.get("source_job") or {}
        source_thread_id = str(operation["source_thread_id"])
        members: list[dict[str, Any]] = []
        for member in operation.get("group_members") or []:
            manifest = {
                key: member.get(key)
                for key in (
                    "thread_id",
                    "native_session_id",
                    "context_status",
                    "activation_generation",
                    "process_generation",
                    "residency_generation",
                    "resident_state",
                    "resident_slot_id",
                    "last_settled_job_id",
                )
            }
            members.append(manifest)
        source_member = next(
            (member for member in members if member["thread_id"] == source_thread_id),
            None,
        )
        if source_member is None or operation.get("resident_slot_id") is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "native_context_unavailable",
                        "message": "The shared sandbox manifest is incomplete.",
                    }
                },
            )
        launch_common = {
            "runtime_id": str(operation["runtime_id"]),
            "runtime_version": operation.get("runtime_version"),
            "model": str(source_job.get("model") or ""),
            "reasoning_effort": source_job.get("reasoning_effort"),
            # Replaced by the broker's host-local, sandbox-reachable relay.
            "gateway_base_url": "",
            # Fork-only does not need a tool credential. A later turn whose
            # MCP set is non-empty performs a member-only native restart.
            "mcp_server_ids": [],
            "mcp_base_url": "",
            "mcp_credential": "",
        }
        coordinator_token = secrets.token_urlsafe(32)
        claimed = await job_store.claim_session_fork_materialization(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
            lease_seconds=SESSION_FORK_COORDINATOR_LEASE_S,
        )
        if not claimed:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "fork_in_progress",
                        "message": "This native Fork is already being materialized; retry shortly.",
                    }
                },
                headers={"Retry-After": "2"},
            )
        residencies = await job_store.establish_session_fork_residencies(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
            ttl_seconds=RESIDENCY_TTL_S,
        )
        if residencies is None:
            await job_store.release_session_fork_materialization(
                operation_id=operation_id,
                coordinator_token=coordinator_token,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "fork_fence_changed",
                        "message": "The source session changed before Fork could start.",
                    }
                },
            )
        for role in ("source", "target"):
            previous_grant = residencies[role].get("previous_grant_id")
            if previous_grant and grants is not None:
                with contextlib.suppress(Exception):
                    await grants.revoke(previous_grant)
        source_credential = await _mint_residency_grant(
            job_store,
            grants,
            row=residencies["source"],
        )
        target_credential = await _mint_residency_grant(
            job_store,
            grants,
            row=residencies["target"],
        )
        if not source_credential or not target_credential:
            await release_failed_fork_residencies(residencies)
            await job_store.release_session_fork_materialization(
                operation_id=operation_id,
                coordinator_token=coordinator_token,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "residency_grant_unavailable",
                        "message": "Fork could not obtain two independent resident credentials.",
                    }
                },
            )

        source_member["residency_generation"] = int(residencies["source"]["generation"])
        source_member["resident_state"] = "restarting"
        try:
            receipt = await broker.fork_shared_session(
                source_thread_id,
                sandbox_id=str(operation["sandbox_id"]),
                generation=int(operation["sandbox_generation"]),
                origin_thread_id=str(operation["origin_thread_id"]),
                runtime_id=str(operation["runtime_id"]),
                runtime_version=operation.get("runtime_version"),
                sandbox_image_digest=operation.get("sandbox_image_digest"),
                expected_container_id=str(operation["container_id"]),
                resident_slot_capacity=int(operation["resident_slot_capacity"]),
                memory_limit=str(operation["memory_limit"]),
                cpu_limit=str(operation["cpu_limit"]),
                stop_timeout_s=int(operation["stop_timeout_seconds"]),
                members=members,
                operation_id=operation_id,
                source_native_session_id=str(operation["source_native_session_id"]),
                source_native_turn_no=int(operation["source_native_turn_no"]),
                target_thread_id=str(operation["target_thread_id"]),
                target_slot_id=int(operation["target_resident_slot_id"]),
                source_launch={
                    **launch_common,
                    "native_session_id": str(operation["source_native_session_id"]),
                    "credential": source_credential,
                    "slot_id": int(operation["resident_slot_id"]),
                    "residency_generation": int(residencies["source"]["generation"]),
                },
                target_launch={
                    **launch_common,
                    "native_session_id": None,
                    "credential": target_credential,
                    "slot_id": int(operation["target_resident_slot_id"]),
                    "residency_generation": int(residencies["target"]["generation"]),
                },
            )
        except WorkspaceBrokerError as exc:
            # PREPARING is intentionally retained: a retry with the same key
            # recovers the in-container journal or safely resumes before the
            # native mutation was reached.
            raise _workspace_broker_http_error(exc) from exc
        materialized = await job_store.record_session_fork_materialized(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
            receipt=receipt,
        )
        if materialized is None:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "type": "invalid_native_fork_receipt",
                        "message": "The sandbox host returned a mismatched native Fork receipt.",
                    }
                },
            )

    forked = await job_store.finalize_session_fork(operation_id=operation_id)
    if forked is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "native_fork_fence_changed",
                    "message": "The source chat changed before native Fork finalization.",
                }
            },
        )
    logger.info(
        "agent_job_forked",
        extra={
            "event": "agent_job_forked",
            "job_id": forked["id"],
            "thread_id": forked.get("thread_id"),
            "source_job_id": job_id,
            "sandbox_clone": False,
            "workspace_shared": True,
            "fork_operation_id": operation_id,
        },
    )
    return _job_response(forked)


@router.get("/jobs/{job_id}/thread", response_model=AgentThreadResponse)
async def get_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadResponse:
    """Return the durable conversation containing one of the caller's jobs."""
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    thread = await job_store.get_thread_for_job(job_id=job_id, user_id=user["user_id"])
    if thread is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No thread for {job_id}"}},
        )
    return AgentThreadResponse(
        thread_id=thread["id"],
        repo=thread["repo"],
        title=thread["title"],
        messages=[
            AgentThreadMessageResponse(
                id=message["id"],
                role=message["role"],
                content=message["content"],
                job_id=message["job_id"],
                created_at=_iso(message["created_at"]),
            )
            for message in thread["messages"]
        ],
        jobs=[_job_response(job) for job in thread["jobs"]],
        created_at=_iso(thread["created_at"]),
        updated_at=_iso(thread["updated_at"]),
        archived_at=_iso(thread.get("archived_at")),
    )


@router.get("/jobs/{job_id}/thread/stats", response_model=AgentThreadStatsResponse)
async def get_agent_thread_stats(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadStatsResponse:
    """Summarize the thread containing a job: turns, timings, tokens.

    Every number is derived from rows the platform already writes, so a
    thread that predates this endpoint answers it in full. Measures the
    thread's runtime does not report come back ``null`` rather than zero.
    """
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    inputs = await job_store.thread_stats_inputs(job_id=job_id, user_id=user["user_id"])
    if inputs is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"type": "not_found", "message": f"No thread for {job_id}"}},
        )
    stats = summarize_thread(
        job_ids=inputs["job_ids"],
        attempts=inputs["attempts"],
        events=inputs["events"],
    )
    return AgentThreadStatsResponse(thread_id=inputs["thread_id"], **asdict(stats))


@router.get("/jobs/{job_id}/thread/events", response_model=AgentThreadEventsResponse)
async def list_agent_thread_events(
    job_id: str,
    after: int = Query(0, ge=0, description="Return events with a global id greater than this."),
    limit: int = Query(_EVENT_PAGE_SIZE, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentThreadEventsResponse:
    """Page every turn's event log, with payloads and turn labels intact."""
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    events = await job_store.list_thread_events_after(
        job_id=job_id, user_id=user["user_id"], after_id=after, limit=limit
    )
    if events is None:
        raise HTTPException(status_code=404, detail="job not found")
    return AgentThreadEventsResponse(
        events=[
            AgentThreadEvent(
                id=event["id"],
                job_id=event["job_id"],
                turn_no=event.get("turn_no") or 1,
                attempt_id=event["attempt_id"],
                seq=event["seq"],
                event_type=event["event_type"],
                payload=event["payload"],
                created_at=_iso(event["created_at"]),
            )
            for event in events
        ],
        next_cursor=events[-1]["id"] if events else after,
    )


@router.get("/jobs/{job_id}/events", response_model=AgentJobEventsResponse)
async def list_agent_job_events(
    job_id: str,
    after: int = Query(0, ge=0, description="Return events with a global id greater than this."),
    limit: int = Query(_EVENT_PAGE_SIZE, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> AgentJobEventsResponse:
    """Page through a job's append-only event log (non-streaming)."""
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)
    events = await job_store.list_events_after(job_id=job_id, after_id=after, limit=limit)
    return AgentJobEventsResponse(
        events=[_event_response(event) for event in events],
        next_cursor=events[-1]["id"] if events else after,
    )


def _workspace_id(job: dict[str, Any]) -> str:
    """Address the chat member inside its retained shared container."""
    thread_id = job.get("thread_id")
    if not thread_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This chat has no retained workspace.",
                }
            },
        )
    return str(thread_id)


def _workspace_broker_http_error(exc: WorkspaceBrokerError) -> HTTPException:
    """Map private broker failures onto bounded owner-facing errors."""
    missing_workspace = _workspace_not_materialized(exc)
    status = (
        409
        if missing_workspace
        else exc.status_code
        if exc.status_code in {400, 404, 408, 409, 413, 429}
        else 503
    )
    message = "This chat has no retained workspace." if missing_workspace else exc.message
    return HTTPException(
        status_code=status,
        detail={"error": {"type": "workspace_unavailable", "message": message}},
    )


def _workspace_not_materialized(exc: WorkspaceBrokerError) -> bool:
    """Recognize the shared-only broker's missing-workspace response."""
    return exc.status_code == 404 and exc.message == "shared chat workspace is not materialized"


async def _workspace_broker_for_job(
    store: AgentJobStore,
    job: dict[str, Any],
) -> WorkspaceBrokerClient | None:
    """Resolve the owning broker and restore shared aliases before use."""
    broker = await broker_for_job(store, job["id"])
    try:
        return await hydrate_shared_broker(job, broker)
    except WorkspaceBrokerError as exc:
        raise _workspace_broker_http_error(exc) from exc


def _terminal_broker_http_error(exc: WorkspaceBrokerError) -> HTTPException:
    """Preserve terminal errors while presenting a missing workspace as a conflict."""
    if _workspace_not_materialized(exc):
        return HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This chat has no live terminal workspace.",
                }
            },
        )
    return _workspace_broker_http_error(exc)


def _workspace_not_ready() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "workspace_not_ready",
                "message": "The workspace is temporarily locked while the agent prepares or saves it.",
            }
        },
    )


async def _require_mutable_workspace_ready(*, job: dict[str, Any], store: AgentJobStore) -> None:
    """Fence retained mutations through the durable thread-wide barrier."""
    if job.get("thread_sandbox") is None:
        raise _workspace_not_ready()
    thread_id = job.get("thread_id")
    if not thread_id or not await store.thread_workspace_ready(thread_id=str(thread_id)):
        raise _workspace_not_ready()


async def _require_workspace_entitlement(
    *,
    job: dict[str, Any],
    user: dict[str, Any],
    store: Any,
    app_credentials: Any | None,
) -> None:
    """Recheck that the owner may still access the workspace repository."""
    try:
        await require_entitled_repo(
            job["repo"],
            user["user_id"],
            store=store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc


async def _terminal_owner_workspace(
    *,
    job_id: str,
    user: dict[str, Any],
    store: AgentJobStore | None,
    app_credentials: Any | None,
    require_ready: bool = False,
    recheck_entitlement: bool = True,
) -> tuple[dict[str, Any], Any]:
    """Authorize one terminal operation and return its private broker.

    Creating or attaching to a terminal rechecks repository entitlement. Once
    attached, the opaque terminal id acts as a short-lived capability scoped
    to the already-authenticated job owner. Input and resize must not enumerate
    GitHub installations for every keystroke; they still recheck ownership and
    the broker-side workspace/session binding. Delete skips entitlement as well
    so a revoked owner can always clean up a live process.
    """
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    if require_ready:
        ready = job.get("thread_sandbox") is not None and await job_store.thread_workspace_ready(
            thread_id=str(job["thread_id"])
        )
        if not ready:
            raise _workspace_not_ready()
    if recheck_entitlement:
        await _require_workspace_entitlement(
            job=job,
            user=user,
            store=job_store,
            app_credentials=app_credentials,
        )
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This deployment has no live workspace broker.",
                }
            },
        )
    if job.get("terminal_resume_pending"):
        # The broker resolved a few lines up is this job's own host's, so the
        # resume goes to the machine holding the retained workspace rather than to
        # whatever the deployment's single-host variable names.
        if not await resume_settled_terminal(
            _workspace_id(job),
            job_id=job_id,
            broker=broker,
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": "The settled terminal workspace is still resuming.",
                    }
                },
            )
        await job_store.mark_terminal_resume_complete(job_id=job_id)
        job["terminal_resume_pending"] = False
    return job, broker


@router.get("/jobs/{job_id}/files", response_model=AgentWorkspaceResponse)
async def get_agent_job_workspace_file(
    job_id: str,
    path: str = Query("", max_length=4096),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentWorkspaceResponse:
    """Browse the chat's retained workspace through its owning host broker."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    try:
        safe_path = normalize_workspace_path(path)
    except WorkspacePathError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_path", "message": str(exc)}},
        ) from exc

    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )

    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This chat has no owning workspace broker.",
                }
            },
        )
    try:
        return AgentWorkspaceResponse(**(await broker.files(_workspace_id(job), safe_path)))
    except WorkspaceBrokerError as exc:
        raise _workspace_broker_http_error(exc) from exc


@router.put("/jobs/{job_id}/files", response_model=AgentWorkspaceResponse)
async def write_agent_job_workspace_file(
    job_id: str,
    body: AgentWorkspaceWriteRequest,
    path: str = Query(..., min_length=1, max_length=4096),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentWorkspaceResponse:
    """Save one text file directly into the retained chat workspace."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    try:
        safe_path = normalize_workspace_path(path)
    except WorkspacePathError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": {"type": "invalid_path", "message": str(exc)}},
        ) from exc
    if not safe_path:
        raise HTTPException(status_code=400, detail="A file path is required.")
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    await _require_mutable_workspace_ready(job=job, store=job_store)
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "This deployment has no live workspace broker.",
                }
            },
        )
    try:
        return AgentWorkspaceResponse(
            **(await broker.write_file(_workspace_id(job), safe_path, body.content))
        )
    except WorkspaceBrokerError as exc:
        if _workspace_not_materialized(exc):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": "This retained workspace is not materialized.",
                    }
                },
            ) from exc
        raise _workspace_broker_http_error(exc) from exc


@router.post("/jobs/{job_id}/terminal", response_model=AgentTerminalResponse)
async def run_agent_job_terminal_command(
    job_id: str,
    body: AgentTerminalRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalResponse:
    """Run an owner-entered command inside the retained chat sandbox."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    await _require_mutable_workspace_ready(job=job, store=job_store)
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        raise HTTPException(status_code=409, detail="This deployment has no live workspace broker.")
    try:
        result = await broker.terminal(
            _workspace_id(job),
            command=body.command,
            cwd=body.cwd,
            timeout_seconds=body.timeout_seconds,
        )
        return AgentTerminalResponse(**result)
    except WorkspaceBrokerError as exc:
        if _workspace_not_materialized(exc):
            raise HTTPException(
                status_code=409,
                detail="This retained terminal workspace is not materialized.",
            ) from exc
        raise _workspace_broker_http_error(exc) from exc


@router.post(
    "/jobs/{job_id}/terminals",
    response_model=AgentTerminalSessionResponse,
)
async def create_agent_job_terminal(
    job_id: str,
    body: AgentTerminalSessionCreateRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Open an interactive PTY in the agent's workspace."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
    )
    try:
        return AgentTerminalSessionResponse(
            **(
                await broker.create_terminal(
                    _workspace_id(job),
                    rows=body.rows,
                    cols=body.cols,
                )
            )
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.get(
    "/jobs/{job_id}/terminals",
    response_model=AgentTerminalSessionListResponse,
)
async def list_agent_job_terminals(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionListResponse:
    """List interactive terminals, including exited sessions kept for replay."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
    )
    try:
        return AgentTerminalSessionListResponse(**(await broker.list_terminals(_workspace_id(job))))
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.get("/jobs/{job_id}/terminals/{terminal_id}/stream")
async def stream_agent_job_terminal(
    job_id: str,
    request: Request,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    after: int = Query(0, ge=0, le=2**63 - 1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> StreamingResponse:
    """Relay a resumable private PTY stream without exposing its broker token."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
    )
    try:
        upstream = await broker.stream_terminal(_workspace_id(job), terminal_id, after=after)
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc

    async def relay():
        try:
            async for chunk in upstream:
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post(
    "/jobs/{job_id}/terminals/{terminal_id}/input",
    response_model=AgentTerminalSessionResponse,
)
async def write_agent_job_terminal_input(
    job_id: str,
    body: AgentTerminalSessionInputRequest,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Write bounded base64 input to an active interactive terminal."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(await broker.terminal_input(_workspace_id(job), terminal_id, data=body.data))
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.post(
    "/jobs/{job_id}/terminals/{terminal_id}/resize",
    response_model=AgentTerminalSessionResponse,
)
async def resize_agent_job_terminal(
    job_id: str,
    body: AgentTerminalSessionResizeRequest,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Resize an active interactive terminal."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        require_ready=True,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(
                await broker.resize_terminal(
                    _workspace_id(job), terminal_id, rows=body.rows, cols=body.cols
                )
            )
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.delete(
    "/jobs/{job_id}/terminals/{terminal_id}",
    response_model=AgentTerminalSessionResponse,
)
async def delete_agent_job_terminal(
    job_id: str,
    terminal_id: str = Path(..., pattern=_TERMINAL_ID_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalSessionResponse:
    """Idempotently kill an interactive terminal, even while a new job runs."""
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=app_credentials,
        recheck_entitlement=False,
    )
    try:
        return AgentTerminalSessionResponse(
            **(await broker.delete_terminal(_workspace_id(job), terminal_id))
        )
    except WorkspaceBrokerError as exc:
        raise _terminal_broker_http_error(exc) from exc


@router.get("/jobs/{job_id}/git", response_model=AgentGitWorkspaceResponse)
async def get_agent_job_git_workspace(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentGitWorkspaceResponse:
    """Read live retained-workspace status, diff and recent commits."""
    job_store = _require_store(store)
    job = await _owned_job(job_store, job_id, user)
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    broker = await _workspace_broker_for_job(job_store, job)
    if broker is None:
        return AgentGitWorkspaceResponse(available=False)
    try:
        return AgentGitWorkspaceResponse(
            **(await broker.git(_workspace_id(job), base_sha=job.get("base_sha")))
        )
    except WorkspaceBrokerError as exc:
        if _workspace_not_materialized(exc):
            return AgentGitWorkspaceResponse(available=False)
        raise _workspace_broker_http_error(exc) from exc


@router.get("/jobs/{job_id}/stream")
async def stream_agent_job_events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    after: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> StreamingResponse:
    """Stream a job's events as SSE, resumable via ``Last-Event-ID``.

    Each SSE frame carries the event's global id, so a client that reconnects
    with ``Last-Event-ID`` resumes exactly where it left off — which is what
    makes the finite stream cap (applied by the timeout middleware to
    ``text/event-stream``) safe for jobs that outlive one connection.
    """
    job_store = _require_store(store)
    await _owned_job(job_store, job_id, user)

    cursor = after
    if last_event_id:
        with contextlib.suppress(ValueError):
            cursor = int(last_event_id)

    async def event_source():
        nonlocal cursor
        idle_polls = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                events = await job_store.list_events_after(
                    job_id=job_id, after_id=cursor, limit=_EVENT_PAGE_SIZE
                )
                if events:
                    idle_polls = 0
                    for event in events:
                        cursor = event["id"]
                        yield _sse_frame(event)
                else:
                    idle_polls += 1
                    if idle_polls % _KEEPALIVE_EVERY_N_POLLS == 0:
                        # Comment frame: keeps proxies from reaping an idle
                        # connection without polluting the event sequence.
                        yield ": keepalive\n\n"

                job = await job_store.get_job(job_id)
                if job is None:
                    return
                if job["state"] in TERMINAL_STATES:
                    # Drain to exhaustion before announcing the end. A single
                    # extra page is not enough: a client attaching to a
                    # finished job with more than two pages of backlog would
                    # see job_finished, close, and lose the rest forever.
                    while True:
                        tail = await job_store.list_events_after(
                            job_id=job_id, after_id=cursor, limit=_EVENT_PAGE_SIZE
                        )
                        if not tail:
                            break
                        for event in tail:
                            cursor = event["id"]
                            yield _sse_frame(event)
                    final = json.dumps(
                        {
                            "state": job["state"],
                            "published_pr_url": job["published_pr_url"],
                            "last_event_id": cursor,
                        },
                        separators=(",", ":"),
                    )
                    yield f"event: job_finished\ndata: {final}\n\n"
                    return

                await asyncio.sleep(_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            # Client disconnect or the stream cap firing: nothing to clean up,
            # the event log is durable and the client can resume by cursor.
            raise

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ── Worker endpoints (capability-token authenticated) ──────────────────

#: Cheap structural check before touching the signing key: host credentials
#: carry their own token prefix, so unrelated credentials never reach the host
#: parser at all.
_HOST_TOKEN_HINT = "aht."


async def verify_claiming_host(
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> dict[str, str]:
    """Authenticate the enrolled host that is claiming retained work.

    The credential names the host and is re-checked by ``claim_job`` in the
    same transaction that takes a capacity slot. There is no shared claim
    token and no request-body host name, so a machine cannot claim as a peer.
    """
    presented = (
        authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    )
    if presented and store is not None and presented.startswith(_HOST_TOKEN_HINT):
        try:
            claims = parse_host_token(presented)
        except (InvalidHostToken, ValueError):
            # ValueError is the unset-``AGENT_CONTROL_TOKEN_SECRET`` case.
            claims = None
        if claims is not None:
            credential_hash = hash_secret(presented)
            host = await store.authenticate_host(
                host=claims["host"], credential_hash=credential_hash
            )
            if host is None:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "error": {
                            "type": "unauthorized",
                            "message": "This host credential is no longer valid.",
                        }
                    },
                )
            return {"host": host["host"], "credential_hash": credential_hash}
    raise HTTPException(
        status_code=401,
        detail={
            "error": {
                "type": "unauthorized",
                "message": "A valid enrolled host credential is required.",
            }
        },
    )


def _worker_claims(authorization: str | None) -> dict[str, Any]:
    """Parse and verify the worker capability token from the auth header."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "unauthorized", "message": "Missing worker token."}},
        )
    try:
        claims = parse_worker_token(token)
    except InvalidAgentToken as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"type": "unauthorized", "message": f"Invalid worker token: {exc}"}},
        ) from exc
    if claims.get("scope") != SCOPE_FULL:
        # Anything that is not the runner's full-scope control token. Two
        # narrower scopes exist and neither belongs here: ``mcp``, which the
        # sandbox carries to reach this service's MCP relay, and ``model``,
        # which predates the gateway grant and is kept only for jobs started
        # before the cutover.
        #
        # This is the structural refusal for both — and the reason the relay's
        # own check is written positively rather than as "not full". A sandbox
        # credential arriving on the worker surface is what an escaped
        # credential looks like.
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "type": "insufficient_scope",
                    "message": (
                        "This credential may only be used for model calls, not for "
                        "reporting job state."
                    ),
                }
            },
        )
    return claims


def _lease_lost() -> HTTPException:
    """The 409 every fenced worker write raises when its lease is gone."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "type": "lease_lost",
                "message": (
                    "This attempt no longer holds the job lease; stop all work immediately."
                ),
            }
        },
    )


def _match_job(claims: dict[str, Any], job_id: str) -> None:
    """Reject a token minted for a different job."""
    if claims["job_id"] != job_id:
        raise _lease_lost()


async def _restore_terminal_suspension(
    broker: WorkspaceBrokerClient | None,
    *,
    workspace_id: str,
    job_id: str,
    lease_generation: int,
) -> None:
    """Best-effort compensation after a worker fails to finalize a resume."""
    if broker is None:
        return
    try:
        await broker.suspend_terminals(
            workspace_id,
            lease_generation=lease_generation,
            job_id=job_id,
        )
    except WorkspaceBrokerError:
        logger.warning(
            "agent_terminal_resume_compensation_failed",
            exc_info=True,
            extra={
                "event": "agent_terminal_resume_compensation_failed",
                "workspace_id": workspace_id,
                "job_id": job_id,
                "lease_generation": lease_generation,
            },
        )


async def _worker_workspace_broker(
    job_store: AgentJobStore,
    job: dict[str, Any],
    *,
    attempt_id: int,
    required: bool,
) -> WorkspaceBrokerClient | None:
    """Resolve the retained owner for worker-side terminal coordination.

    A failed attempt may settle before it ever reserves Docker, in which case
    there is genuinely no terminal state to coordinate. Once a group mapping
    exists, however, a missing broker is a routing failure and must not be
    treated as a successful no-op.
    """
    if job.get("thread_sandbox") is None:
        if required:
            raise _workspace_not_ready()
        return None
    broker = await broker_for_attempt(job_store, attempt_id)
    try:
        broker = await hydrate_shared_broker(job, broker)
    except WorkspaceBrokerError as exc:
        raise _workspace_broker_http_error(exc) from exc
    if broker is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "The retained workspace owner is unavailable.",
                }
            },
        )
    return broker


async def _mint_sandbox_grant(
    job_store: AgentJobStore,
    grants: Any,
    claim: dict[str, Any],
) -> str:
    """Ask the gateway for the capability this attempt's sandbox runs with.

    Scoped to exactly the model the job named and exactly the MCP servers it
    was created with — not to everything the owner's role could reach. The
    gateway may narrow that further, and what comes back is what is handed
    over; the runner never sees what was asked for.

    **Failure hands the job back.** A grant that could not be minted means the
    sandbox has no way to call a model, so starting the attempt would burn one
    of the job's three tries producing an agent that fails at its first
    request. Releasing the claim costs nothing — a released claim is not a
    spent attempt — and the runner idles until the gateway answers again. That
    is the fail-closed direction: no grant, no run.

    The grant id is recorded against the attempt under its own fence, because
    renewal and revocation both need to find it later, and because the fence is
    what makes "is this attempt still the current one" answerable at all.

    **A grant that cannot be linked is revoked, not handed over.** The link is
    the only thing that will ever renew or revoke this capability, so a grant
    without one is a live credential nothing owns: it survives to its TTL, it
    is not renewed so the run fails anyway, and no operator can find it. Both
    ways that can happen — a gateway answer with no ``grant_id``, and a fenced
    UPDATE that matched nothing because the attempt was superseded in between —
    end the same way: withdraw it and fail the claim.

    Args:
        job_store: The store holding the attempt.
        grants: The grant client.
        claim: The row ``claim_job`` returned.

    Returns:
        The grant's bearer token — the only credential that enters the sandbox.

    Raises:
        HTTPException: 503, after releasing the claim, if no grant could be got
            or the one that was minted could not be linked to this attempt.
    """
    try:
        grant = await grants.mint(
            user_id=claim["user_id"],
            job_id=claim["id"],
            attempt_id=claim["attempt_id"],
            models=[claim["model"]],
        )
    except GatewayError as exc:
        await job_store.release_claim(
            job_id=claim["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        logger.warning(
            "agent_job_grant_unavailable",
            exc_info=True,
            extra={"event": "agent_job_grant_unavailable", "job_id": claim["id"]},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "credential_unavailable",
                    "message": (
                        "Could not mint an inference grant for this job; it has been "
                        "returned to the queue and no attempt was spent."
                    ),
                }
            },
        ) from exc

    sandbox_token = str(grant.get("token") or "")
    grant_id = str(grant.get("grant_id") or "")

    # **What was asked for is not what was granted.** The gateway clamps the
    # requested models against the role the user has *now*, and a job can sit
    # in the queue for a long time — long enough for a downgrade, a tier
    # change, or a model being withdrawn from that tier. The clamp is not an
    # error there: the request was satisfiable, just not on the caller's terms,
    # and the response reports the effective list.
    #
    # Unchecked, that lands as a sandbox that starts, spends an attempt, and
    # fails on its first inference call with an authorization error the user
    # cannot act on. Checked here, the job goes back to the queue with nothing
    # spent, which is the same shape as every other credential failure above.
    #
    # Absence is refusal too. The contract says this field is returned; if it
    # is not, the scope cannot be verified, and "cannot verify" is not a reason
    # to proceed with a capability.
    granted_models = grant.get("allowed_models")
    if not isinstance(granted_models, list) or claim["model"] not in granted_models:
        if grant_id:
            await grants.revoke(grant_id)
        await job_store.release_claim(
            job_id=claim["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        logger.warning(
            "agent_job_grant_scope_insufficient",
            extra={
                "event": "agent_job_grant_scope_insufficient",
                "job_id": claim["id"],
                "model": claim["model"],
                "granted": granted_models if isinstance(granted_models, list) else None,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "model_not_granted",
                    "message": (
                        "The gateway did not grant this job's model; it has been "
                        "returned to the queue and no attempt was spent."
                    ),
                }
            },
        )

    linked = bool(grant_id) and await job_store.record_attempt_grant(
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        grant_id=grant_id,
    )
    if not linked:
        # Withdraw it before anything else. This is the one moment the grant id
        # is known to a process that is about to forget it, and after that only
        # its TTL can end it.
        if grant_id:
            await grants.revoke(grant_id)
        await job_store.release_claim(
            job_id=claim["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        logger.warning(
            "agent_job_grant_unlinked",
            extra={
                "event": "agent_job_grant_unlinked",
                "job_id": claim["id"],
                "attempt_id": claim["attempt_id"],
                "grant_id": grant_id or None,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "credential_unavailable",
                    "message": (
                        "The inference grant for this job could not be recorded against "
                        "its attempt; it has been withdrawn and no attempt was spent."
                    ),
                }
            },
        )
    return sandbox_token


@router.post("/worker/claim", response_model=WorkerClaimResponse | None)
async def worker_claim(
    body: WorkerClaimRequest,
    background_tasks: BackgroundTasks,
    claiming_host: dict[str, str] = Depends(verify_claiming_host),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    grants: Any = Depends(get_grant_client),
) -> WorkerClaimResponse | None:
    """Claim the next queued job and mint this attempt's capability token.

    Returns ``null`` (HTTP 200) when the queue is empty, and equally when an
    operator has pinned agent jobs to a different host than the one this runner
    reports. The two are deliberately the same answer on the wire: a runner
    that has been switched away from should idle exactly as it does when there
    is no work, not treat it as an error worth retrying differently. Which host
    is active is an admin question, answered on the admin surface.

    **Enrolled-host only.** ``claim_job`` takes the oldest queued job across all
    tenants, and the response carries that job's repo, prompt, and metadata
    plus a working capability token for it — so ordinary API-key auth here
    would let any customer dequeue and read another customer's job, and drain
    the queue besides.

    The per-host credential opens registration/heartbeat/claim surfaces and
    never enters the sandbox; only the returned per-attempt tokens go in.
    """
    job_store = _require_store(store)
    if not (os.environ.get(ENV_BROKER_TOKEN) or "").strip():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": "The retained workspace broker credential is not configured.",
                }
            },
        )
    claim = await job_store.claim_job(
        worker_id=body.worker_id,
        lease_ttl_seconds=body.lease_ttl_seconds,
        host=claiming_host["host"],
        host_credential_hash=claiming_host["credential_hash"],
        claim_attestation=(body.attestation.model_dump() if body.attestation is not None else None),
        expected_deployment_sha=(os.environ.get("AGENT_DEPLOYMENT_SHA") or "").strip() or None,
    )
    if claim is None:
        return None
    fence = {
        "job_id": claim["id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
    }
    # Two credentials with different powers. The runner keeps the full one and
    # passes only the sandbox one inside, so a credential that leaks from inside
    # the agent can make model calls the gateway already meters but cannot touch
    # this job's event log, lifecycle, or terminal state.
    #
    # Only the first is ours to mint. ``SCOPE_FULL`` is this service's own
    # control credential, signed with this service's own secret and verified by
    # this service — correctly local. The sandbox's credential is not: it buys
    # inference, which is the gateway's to authorize and the gateway's to meter,
    # so it is asked for rather than minted. (E9. Before this, the claim minted
    # a ``SCOPE_MODEL`` token signed with a root the gateway does not know —
    # which bought nothing, and failed at the sandbox's first model call.)
    token = mint_worker_token(**fence, scope=SCOPE_FULL)
    sandbox_token = await _mint_sandbox_grant(job_store, grants, claim)

    # The sandbox's *second* credential, and this one is ours to mint: MCP is
    # served by this service's relay, against this service's registry and this
    # service's attempt fence. Scoped `mcp` so it opens the relay and nothing
    # else — not the worker surface, and certainly not the gateway.
    #
    # Two credentials rather than one because they authorize at two services
    # now. A leak is bounded by whichever it was: the grant buys models it
    # names, this buys tools the job asked for, and neither buys the other.
    mcp_token = mint_worker_token(**fence, scope=SCOPE_MCP)
    # A third credential, weaker than either: read-only, this repository only,
    # one hour. The runner needs it to check the repository out — a runner that
    # cannot clone runs the agent in an empty directory — and it is deliberately
    # not the publisher's token, which can write. It stays in the runner and
    # never enters the sandbox. Absent (null) is a working configuration: a
    # public repository clones without any credential at all.
    # Re-checked at mint time, not just at create time. A row written before
    # this check existed — or while the allowlist was wider — must not be able
    # to produce a credential now.
    try:
        # The same check create used. The allowlist-only variant here meant a
        # job entitled by its owner's own GitHub connection passed creation and
        # was then refused at claim — the two gates disagreeing about what the
        # word entitled means.
        await require_entitled_repo(
            claim["repo"],
            claim["user_id"],
            store=job_store,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        await job_store.release_claim(
            job_id=claim["id"],
            attempt_id=claim["attempt_id"],
            lease_generation=claim["lease_generation"],
        )
        logger.warning(
            "agent_job_repo_not_allowed",
            extra={"event": "agent_job_repo_not_allowed", "job_id": claim["id"]},
        )
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc

    clone_token: str | None = None
    if app_credentials is not None:
        try:
            clone_token = await app_credentials.token_for(
                claim["repo"], permissions=_CLONE_SCOPE, repository_scoped=True
            )
        except AppNotInstalled:
            # A settled answer: the App does not cover this repository. Hand
            # the job over without a credential — a public repository clones
            # anonymously, and a private one fails with a message the owner can
            # act on ("install the App"), which retrying would not improve.
            logger.info(
                "agent_job_clone_token_unavailable",
                extra={"event": "agent_job_clone_token_unavailable", "job_id": claim["id"]},
            )
        except Exception as exc:
            # Anything else is GitHub or the network having a bad minute.
            # Returning no token here would send the runner off to clone
            # anonymously, fail on a private repository, and mark the job
            # *terminally* failed — turning a momentary outage into the owner's
            # problem.
            #
            # Hand the claim back rather than just letting the lease lapse. A
            # lapsed lease still spends a retry, so a GitHub outage lasting
            # across three claim cycles would fail every queued private-repo
            # job outright, without an agent ever having started — the very
            # outcome this branch exists to avoid, only slower.
            await job_store.release_claim(
                job_id=claim["id"],
                attempt_id=claim["attempt_id"],
                lease_generation=claim["lease_generation"],
            )
            logger.warning(
                "agent_job_clone_token_error",
                extra={"event": "agent_job_clone_token_error", "job_id": claim["id"]},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "credential_unavailable",
                        "message": (
                            "Could not mint a repository credential for this job; it has "
                            "been returned to the queue and no attempt was spent."
                        ),
                    }
                },
            ) from exc

    response = WorkerClaimResponse(
        job_id=claim["id"],
        thread_id=claim.get("thread_id"),
        parent_job_id=claim.get("parent_job_id"),
        turn_no=claim.get("turn_no") or 1,
        attempt_id=claim["attempt_id"],
        attempt_no=claim["attempt_no"],
        repo=claim["repo"],
        base_sha=claim["base_sha"],
        task_prompt=claim["task_prompt"],
        setup_script=claim.get("setup_script"),
        runtime=claim["runtime"],
        model=claim["model"],
        execution_profile=claim.get("execution_profile", "trusted-v1"),
        reasoning_effort=claim.get("reasoning_effort"),
        runtime_version=claim.get("runtime_version"),
        thread_sandbox=claim.get("thread_sandbox"),
        worker_token=token,
        sandbox_token=sandbox_token,
        mcp_token=mcp_token,
        # Where the sandbox reaches the relay. Configured rather than derived:
        # the runner sees this service through a host-local relay whose address
        # is nothing this process can work out for itself.
        mcp_base_url=os.environ.get(ENV_SANDBOX_MCP_URL, ""),
        clone_token=clone_token,
        mcp_servers=list(claim.get("mcp_servers") or []),
        metadata=claim["metadata"],
    )
    if claim.get("title_generation_pending") is True:
        # FastAPI sends the claim first; title inference cannot delay the runner.
        # The store only marks the first eligible claim, so follow-ups and retries
        # retain a stable title without making further title-generation calls.
        background_tasks.add_task(
            generate_thread_title,
            job_store,
            **fence,
            prompt=claim["task_prompt"],
            model=claim["model"],
            sandbox_token=sandbox_token,
        )
    return response


@router.post("/worker/jobs/{job_id}/heartbeat", response_model=WorkerHeartbeatResponse)
async def worker_heartbeat(
    job_id: str,
    body: WorkerHeartbeatRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerHeartbeatResponse:
    """Renew the attempt's lease and learn about pending cancellation."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    beat = await job_store.heartbeat(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        lease_ttl_seconds=body.lease_ttl_seconds,
    )
    if not beat["ok"]:
        raise _lease_lost()
    return WorkerHeartbeatResponse(
        ok=True, state=beat["state"], cancel_requested=beat["cancel_requested"]
    )


@router.post("/worker/jobs/{job_id}/events", response_model=WorkerEventResponse, status_code=201)
async def worker_append_event(
    job_id: str,
    body: WorkerEventRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerEventResponse:
    """Append one normalized event to the job's append-only log."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    event_id = await job_store.append_event(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        event_type=body.event_type,
        payload=body.payload,
        event_seq=body.event_seq,
    )
    if event_id is None:
        raise _lease_lost()
    return WorkerEventResponse(event_id=event_id)


@router.post("/worker/jobs/{job_id}/continuity", response_model=WorkerAckResponse)
async def worker_report_continuity(
    job_id: str,
    body: WorkerContinuityRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Record which continuity rung this attempt runs on (design §9).

    Observability, deliberately fenced like every other worker write: a
    superseded attempt must not be able to relabel how the live one ran.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    recorded = await job_store.record_continuity_mode(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        mode=body.mode,
        context_status=body.context_status,
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


async def _worker_thread_id(job_store: AgentJobStore, job_id: str) -> str:
    """Resolve the claimed job's thread for restartable-sandbox writes."""
    job = await job_store.get_job(job_id)
    thread_id = None if job is None else job.get("thread_id")
    if not thread_id:
        raise HTTPException(status_code=409, detail="this job has no chat thread")
    return str(thread_id)


@router.post(
    "/worker/jobs/{job_id}/sandbox/reserve",
    response_model=WorkerThreadSandbox,
)
async def worker_reserve_thread_sandbox(
    job_id: str,
    body: WorkerSandboxReserveRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerThreadSandbox:
    """Reserve immutable one-chat/one-container identity before Docker create."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store, job_id)
    try:
        row = await job_store.reserve_thread_sandbox(
            thread_id=thread_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            host=body.host,
            sandbox_id=body.sandbox_id,
            container_name=body.container_name,
            runtime_id=body.runtime_id,
            runtime_version=body.runtime_version,
            sandbox_image_digest=body.sandbox_image_digest,
            execution_profile=body.execution_profile.value,
            resident_slot_capacity=body.resident_slot_capacity,
            memory_limit=body.memory_limit,
            cpu_limit=body.cpu_limit,
            stop_timeout_seconds=body.stop_timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise _lease_lost()
    return WorkerThreadSandbox(**row)


@router.post(
    "/worker/jobs/{job_id}/sandbox/running",
    response_model=WorkerThreadSandbox,
)
async def worker_mark_thread_sandbox_running(
    job_id: str,
    body: WorkerSandboxRunningRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerThreadSandbox:
    """Confirm the exact retained container reached RUNNING."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store, job_id)
    row = await job_store.mark_thread_sandbox_running(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        container_id=body.container_id,
        context_status=body.context_status,
    )
    if row is None:
        raise _lease_lost()
    return WorkerThreadSandbox(**row)


@router.post(
    "/worker/jobs/{job_id}/sandbox/residency",
    response_model=HostResidencyEstablishResponse,
)
async def worker_establish_shared_sandbox_residency(
    job_id: str,
    body: WorkerSandboxResidencyRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    grants: Any = Depends(get_grant_client),
) -> HostResidencyEstablishResponse:
    """Mint the renewable member grant before the supervisor opens a CLI."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store, job_id)
    host = await job_store.host_for_job(job_id=job_id)
    if host is None or not host.get("host"):
        raise _lease_lost()
    row = await job_store.establish_residency(
        thread_id=thread_id,
        host=str(host["host"]),
        runtime_id=body.runtime_id,
        runtime_version=body.runtime_version,
        sandbox_id=body.sandbox_id,
        ttl_seconds=RESIDENCY_TTL_S,
    )
    if row is None:
        raise _lease_lost()
    credential = await _mint_residency_grant(job_store, grants, row=row)
    if not credential:
        # Resident processes cannot safely outlive an attempt-scoped token, so
        # renewable residency is all-or-nothing at admission.
        await job_store.release_residency(
            thread_id=thread_id,
            generation=int(row["generation"]),
            host=str(host["host"]),
        )
        raise HTTPException(
            status_code=503,
            detail="a renewable shared-session credential could not be minted",
        )
    recorded = await job_store.record_shared_thread_residency(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_id=body.sandbox_id,
        residency_generation=int(row["generation"]),
    )
    if not recorded:
        released = await job_store.release_residency(
            thread_id=thread_id,
            generation=int(row["generation"]),
            host=str(host["host"]),
        )
        if released and grants is not None:
            await grants.revoke(released)
        raise _lease_lost()
    return HostResidencyEstablishResponse(
        thread_id=thread_id,
        generation=int(row["generation"]),
        ttl_seconds=RESIDENCY_TTL_S,
        credential=credential,
        model=row.get("model"),
    )


@router.post(
    "/worker/jobs/{job_id}/sandbox/process",
    response_model=WorkerAckResponse,
)
async def worker_record_thread_sandbox_process(
    job_id: str,
    body: WorkerSandboxProcessRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Fence and record a replacement Agent CLI process generation."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store, job_id)
    recorded = await job_store.record_thread_sandbox_process(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        process_generation=body.process_generation,
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post(
    "/worker/jobs/{job_id}/sandbox/admit",
    response_model=WorkerAckResponse,
)
async def worker_admit_thread_sandbox_turn(
    job_id: str,
    body: WorkerSandboxAdmissionRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Persist at-most-once admission before the prompt reaches the CLI."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store, job_id)
    recorded = await job_store.admit_thread_sandbox_turn(
        thread_id=thread_id,
        job_id=job_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post(
    "/worker/jobs/{job_id}/sandbox/settle",
    response_model=WorkerThreadSandbox,
)
async def worker_settle_thread_sandbox(
    job_id: str,
    body: WorkerSandboxSettleRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerThreadSandbox:
    """Persist the native context boundary before arming idle deactivation."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store, job_id)
    row = await job_store.settle_thread_sandbox(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        job_id=job_id,
        context_status=body.context_status,
        native_session_id=body.native_session_id,
        base_sha=body.base_sha,
        idle_timeout_seconds=body.idle_timeout_seconds,
    )
    if row is None:
        raise _lease_lost()
    return WorkerThreadSandbox(**row)


@router.post(
    "/worker/jobs/{job_id}/sandbox/abandon",
    response_model=WorkerThreadSandbox,
)
async def worker_abandon_thread_sandbox(
    job_id: str,
    body: WorkerSandboxAbandonRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    grants: Any = Depends(get_grant_client),
) -> WorkerThreadSandbox:
    """Release a failed activation; ambiguous admission becomes LOST."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    thread_id = await _worker_thread_id(job_store, job_id)
    row = await job_store.abandon_thread_sandbox(
        thread_id=thread_id,
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        sandbox_generation=body.sandbox_generation,
        activation_generation=body.activation_generation,
        lost=body.lost,
        detail=body.detail,
        idle_timeout_seconds=body.idle_timeout_seconds,
    )
    if row is None:
        raise _lease_lost()
    if int(row.get("residency_generation") or 0) > 0:
        # The shared supervisor closes this member's process on abandon. End
        # its matching renewable capability now instead of leaving a dead
        # process's grant live until the residency TTL. The generation fence
        # makes a concurrent successor immune to this cleanup.
        released = await job_store.release_residency(
            thread_id=thread_id,
            generation=int(row["residency_generation"]),
            host=str(row["host"]),
        )
        if released and grants is not None:
            with contextlib.suppress(Exception):
                await grants.revoke(released)
    return WorkerThreadSandbox(**row)


@router.post("/worker/jobs/{job_id}/resources", response_model=WorkerAckResponse)
async def worker_report_resources(
    job_id: str,
    body: WorkerResourcesRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Record one closed attempt's resource and timing telemetry.

    Fenced like every other worker write, and idempotent: the rollup
    describes an attempt that is already closed, so a retried post is the same
    report said twice rather than a second report to add.
    """
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    recorded = await job_store.record_attempt_resources(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        rollup=body.model_dump(),
    )
    if not recorded:
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/terminals/suspend", response_model=WorkerAckResponse)
async def worker_suspend_terminals(
    job_id: str,
    body: WorkerTerminalSuspendRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Fence terminal writes, then freeze retained PTYs for a protected phase."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    job = await job_store.get_job(job_id)
    if job is None:
        raise _lease_lost()
    workspace_id = _workspace_id(job)
    event_id = await job_store.append_event(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
        event_type="lifecycle",
        payload={"phase": body.phase},
    )
    if event_id is None:
        raise _lease_lost()
    broker = await _worker_workspace_broker(
        job_store, job, attempt_id=claims["attempt_id"], required=True
    )
    try:
        await broker.suspend_terminals(
            workspace_id,
            lease_generation=claims["lease_generation"],
            job_id=job_id,
        )
    except WorkspaceBrokerError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": exc.message,
                }
            },
        ) from exc
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/terminals/resume", response_model=WorkerAckResponse)
async def worker_resume_terminals(
    job_id: str,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Resume retained PTYs, then atomically publish workspace readiness."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    job = await job_store.get_job(job_id)
    if job is None:
        raise _lease_lost()
    workspace_id = _workspace_id(job)
    fenced = await job_store.fence_terminal_workspace(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if not fenced:
        raise _lease_lost()
    broker = await _worker_workspace_broker(
        job_store, job, attempt_id=claims["attempt_id"], required=True
    )
    try:
        await broker.resume_terminals(
            workspace_id,
            lease_generation=claims["lease_generation"],
            job_id=job_id,
        )
    except WorkspaceBrokerError as exc:
        await _restore_terminal_suspension(
            broker,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "workspace_unavailable",
                    "message": exc.message,
                }
            },
        ) from exc
    try:
        event_id = await job_store.append_event(
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            event_type="lifecycle",
            payload={"phase": "workspace_ready"},
        )
    except Exception:
        await _restore_terminal_suspension(
            broker,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise
    if event_id is None:
        await _restore_terminal_suspension(
            broker,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise _lease_lost()
    return WorkerAckResponse(ok=True)


@router.post("/worker/jobs/{job_id}/finish", response_model=WorkerAckResponse)
async def worker_finish(
    job_id: str,
    body: WorkerFinishRequest,
    authorization: str | None = Header(None),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> WorkerAckResponse:
    """Drive the fenced terminal transition for this attempt."""
    job_store = _require_store(store)
    claims = _worker_claims(authorization)
    _match_job(claims, job_id)
    job = await job_store.get_job(job_id)
    if job is None:
        raise _lease_lost()
    workspace_id = _workspace_id(job)
    if body.state not in TERMINAL_STATES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "invalid_request",
                    "message": f"state must be one of {list(TERMINAL_STATES)}",
                }
            },
        )
    fenced = await job_store.fence_terminal_workspace(
        attempt_id=claims["attempt_id"],
        lease_generation=claims["lease_generation"],
    )
    if not fenced:
        raise _lease_lost()
    broker = await _worker_workspace_broker(
        job_store, job, attempt_id=claims["attempt_id"], required=False
    )
    if broker is not None:
        try:
            await broker.resume_terminals(
                workspace_id,
                lease_generation=claims["lease_generation"],
                job_id=job_id,
            )
        except WorkspaceBrokerError as exc:
            await _restore_terminal_suspension(
                broker,
                workspace_id=workspace_id,
                job_id=job_id,
                lease_generation=claims["lease_generation"],
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "workspace_unavailable",
                        "message": exc.message,
                    }
                },
            ) from exc
    try:
        ok = await job_store.transition(
            job_id=job_id,
            attempt_id=claims["attempt_id"],
            lease_generation=claims["lease_generation"],
            from_states=(RUNNING,),
            to_state=body.state,
            detail=body.detail,
            base_sha=body.base_sha,
        )
    except Exception:
        await _restore_terminal_suspension(
            broker,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise
    if not ok:
        await _restore_terminal_suspension(
            broker,
            workspace_id=workspace_id,
            job_id=job_id,
            lease_generation=claims["lease_generation"],
        )
        raise _lease_lost()
    return WorkerAckResponse(ok=True, state=body.state)
