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
import secrets
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_agent_gitlab_oauth,
    get_gateway_client,
    get_grant_client,
    get_log_store,
    get_storage,
)
from cloud_agent.api.hosts import RESIDENCY_TTL_S, _mint_residency_grant
from cloud_agent.broker_routing import broker_for_job
from cloud_agent.domain.execution import SUCCEEDED, TERMINAL_STATES
from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.entitlement import (
    REPO_PATTERN,
    RepoNotAllowed,
    repos_for_user,
    require_entitled_repo,
)
from cloud_agent.github_app import GitHubAppError
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.registry import resolve_connection
from cloud_agent.schemas import (
    RESET_REASONING_EFFORT,
    AgentAttemptTelemetry,
    AgentConfigResponse,
    AgentConversation,
    AgentConversationListResponse,
    AgentFollowUpRequest,
    AgentGitWorkspaceResponse,
    AgentJobCancelResponse,
    AgentJobCreate,
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
    GitHubConnectionResponse,
    GitHubConnectRequest,
    McpConnectionSummary,
    McpIntegrationResponse,
    OAuthConnectRequest,
    RepoBranchesResponse,
    SourceControlAccount,
    SourceControlIntegrationsResponse,
    SourceControlProviderResponse,
    SourceControlRepository,
)
from cloud_agent.source_control import (
    OAuthStateError,
    SourceControlError,
    consume_oauth_state,
    github_authorization_url,
    issue_oauth_state,
)
from cloud_agent.terminal_coordination import resume_settled_terminal
from cloud_agent.thread_stats import summarize_thread
from cloud_agent.visible_models import agent_model_reasoning_efforts, agent_visible_models
from cloud_agent.workspace_broker_client import WorkspaceBrokerError
from cloud_agent_contracts.workspace_browser import WorkspacePathError, normalize_workspace_path
from cloud_agent_host.runtimes import (
    registered_runtimes,
    runtime_reasoning_efforts,
    runtimes_supporting_mcp,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cloud_agent.ports import (
        AttachmentRepository,
        AttemptCommands,
        ConversationCommands,
        ConversationQueries,
        ExecutionQueries,
        GrantRepository,
        HostPoolRepository,
        McpConnectionRepository,
        ResidencyRepository,
        SandboxRepository,
        SessionForkRepository,
        SkillRepository,
        SourceControlRepository as SourceControlPort,
    )


# The shared helpers moved to their own modules (R3-a of #225). The aggregator
# keeps binding the same objects, so a test, a ``Depends`` key or an importer
# that still says ``routes.X`` resolves to the identity it always did. These
# aliases exist for the transition; R5 deletes them once every caller imports
# the new home.
from cloud_agent.api._job_admission import (
    ENV_SANDBOX_MCP_URL,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _binding_alias,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _create_job_as,
    _idempotency_conflict,
    _JobCreationStorage,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_offered_effort,
    _require_registered_runtime,
    _require_resolvable_model,
    _resolve_attachment_selection,
    _resolve_mcp_bindings,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _resolve_skill_selection,
)
from cloud_agent.api._job_views import (
    _EVENT_PAGE_SIZE,
    _SAFE_EVENT_TYPE,
    _attempt_duration_ms,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _attempt_resources,
    _attempt_summary_response,
    _egress_tiers,
    _event_response,
    _iso,
    _job_response,
)
from cloud_agent.api._owner_auth import (
    _owned_job,
    authenticate_agent_owner,  # noqa: F401 — re-exported: the identity the host namespace kept.
    require_agent_owner,
)
from cloud_agent.api._workspace_access import (
    _TERMINAL_ID_PATTERN,
    _workspace_broker_for_job,
    _workspace_broker_http_error,
    _workspace_id,
    _workspace_not_materialized,
    _workspace_not_ready,
)

# The worker surface moved to its own module (R3-b of #225). The aggregator
# keeps binding the same objects, so a test, a ``Depends`` key or an importer
# that still says ``routes.X`` resolves to the identity it always did. These
# aliases exist for the transition; R5 deletes them once every caller imports
# the new home.
from cloud_agent.api.worker import (
    _CLONE_SCOPE,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _HOST_TOKEN_HINT,  # noqa: F401 — re-exported: the identity the host namespace kept.
    MAX_TURN_RESOURCES_BODY_BYTES,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _lease_lost,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _match_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _mint_sandbox_grant,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _restore_terminal_suspension,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _turn_summary_conflict,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _worker_claims,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _worker_thread_id,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _worker_workspace_broker,  # noqa: F401 — re-exported: the identity the host namespace kept.
    verify_claiming_host,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_abandon_thread_sandbox,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_admit_thread_sandbox_turn,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_append_event,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_claim,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_establish_shared_sandbox_residency,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_finish,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_heartbeat,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_list_attachments,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_mark_thread_sandbox_running,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_read_attachment,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_record_thread_sandbox_process,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_release,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_report_continuity,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_report_resources,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_report_turn_resources,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_reserve_thread_sandbox,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_resume_terminals,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_settle_thread_sandbox,  # noqa: F401 — re-exported: the identity the host namespace kept.
    worker_suspend_terminals,  # noqa: F401 — re-exported: the identity the host namespace kept.
)

logger = get_logger(__name__)


class _RoutesStorage(Protocol):
    """The capabilities this module's handlers reach the store for.

    Named here so that reaching for one more is a deliberate edit to
    this list rather than a call that happens to type-check.
    """

    attachments: AttachmentRepository
    attempt_commands: AttemptCommands
    conversation_commands: ConversationCommands
    conversation_queries: ConversationQueries
    execution_queries: ExecutionQueries
    grants: GrantRepository
    host_pool: HostPoolRepository
    mcp: McpConnectionRepository
    skills: SkillRepository
    residency: ResidencyRepository
    sandbox: SandboxRepository
    session_fork: SessionForkRepository
    source_control: SourceControlPort


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


def _require_store(store: _RoutesStorage | None) -> _RoutesStorage:
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


# ── Owner endpoints ────────────────────────────────────────────────────


@router.get("/config", response_model=AgentConfigResponse)
async def get_agent_config(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
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
    repos = await repos_for_user(
        user["user_id"],
        store=None if store is None else store.source_control,
        app_credentials=app_credentials,
    )
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
        mcp_connections=[
            McpConnectionSummary(**entry)
            for entry in (
                await _mcp_connections_for_user(store, user["user_id"]) if store is not None else []
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
    store: _RoutesStorage,
    app_credentials: Any,
) -> GitHubConnectionResponse:
    await consume_oauth_state(
        store.source_control,
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
            install_state = await issue_oauth_state(
                store.source_control, user_id=user_id, provider="github"
            )
            install_url = github_authorization_url(github_install_url, state=install_state)
    for installation in installations:
        await store.source_control.record_repo_grant(
            user_id=user_id,
            installation_id=installation["installation_id"],
            account_login=installation.get("account_login"),
        )
    logger.info(
        "agent_github_connected",
        extra={"event": "agent_github_connected", "installations": len(installations)},
    )
    return GitHubConnectionResponse(
        connections=await store.source_control.list_repo_grants(user_id=user_id),
        repos=await repos_for_user(
            user_id, store=store.source_control, app_credentials=app_credentials
        ),
        install_url=install_url,
    )


async def _source_control_providers(
    *,
    user_id: str,
    store: _RoutesStorage,
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
    if github_configured and app_credentials is not None:
        try:
            state = await issue_oauth_state(
                store.source_control, user_id=user_id, provider="github"
            )
            github_connect_url = app_credentials.user_authorization_url(state)
        except (GitHubAppError, SourceControlError) as exc:
            github_configured = False
            github_error = str(exc)

    grants = await store.source_control.list_repo_grants(user_id=user_id)
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
            manage_state = await issue_oauth_state(
                store.source_control, user_id=user_id, provider="github"
            )
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
        user_id,
        store=store.source_control,
        app_credentials=app_credentials,
        env={"AGENT_REPO_ALLOWLIST": ""},
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

    gitlab_connection = await store.source_control.get_gitlab_connection(user_id=user_id)
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


async def _mcp_integrations(*, user_id: str, store: _RoutesStorage) -> list[McpIntegrationResponse]:
    """This user's MCP connections, in the aggregate integrations response.

    Read from the connections themselves rather than from a provider list, so
    a server with no template appears here exactly like one with a template.
    The management endpoints under ``/v1/agent/mcp`` are where a connection is
    added, tested or removed; this is the summary the settings page folds in
    beside source control.
    """
    rows = await store.mcp.list_mcp_connections(user_id=user_id)
    rendered: list[McpIntegrationResponse] = []
    for row in rows:
        resolved = await resolve_connection(store.mcp, row=row)
        snapshot = resolved.snapshot
        rendered.append(
            McpIntegrationResponse(
                name=str(row["label"]),
                label=str(row["label"]),
                description=str(row.get("template") or row.get("url") or ""),
                capabilities=sorted(
                    capability.name for capability in (snapshot.tools if snapshot else ())
                )[:32],
                configured=True,
                connected=True,
                account_label=row.get("account_label"),
                connect_url=None,
                error=resolved.error,
            )
        )
    return rendered


@router.get("/integrations", response_model=SourceControlIntegrationsResponse)
async def list_source_control_integrations(
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlIntegrationsResponse:
    """Return configured and connected state for source control and MCP.

    One response for one page, deliberately: the settings screen shows both
    kinds of connection together, and two requests would let it render a
    half-refreshed state after a disconnect. The MCP half is a summary — adding,
    testing and selecting a connection live under ``/v1/agent/mcp``.
    """
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


@router.post("/integrations/github/connect", response_model=GitHubConnectionResponse)
async def connect_github_integration(
    body: OAuthConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlProviderResponse:
    """Verify a GitLab user and persist only encrypted, refreshable tokens."""
    job_store = _require_store(store)
    if gitlab_oauth is None:
        raise HTTPException(status_code=503, detail="GitLab OAuth is not configured")
    try:
        verifier = await consume_oauth_state(
            job_store.source_control,
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
        previous_connection = await job_store.source_control.get_gitlab_connection(
            user_id=user["user_id"]
        )
        connection_id = await job_store.source_control.upsert_gitlab_connection(
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
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
) -> SourceControlIntegrationsResponse:
    """Revoke GitLab best-effort, then always remove the local credential."""
    job_store = _require_store(store)
    connection = await job_store.source_control.get_gitlab_connection(user_id=user["user_id"])
    if connection is None or connection["id"] != connection_id:
        raise HTTPException(status_code=404, detail="No such GitLab connection")
    if gitlab_oauth is not None:
        try:
            access_token = await gitlab_oauth.access_token_for_connection(job_store, connection)
            await gitlab_oauth.revoke(access_token)
        except Exception:
            logger.warning("agent_gitlab_revoke_failed", exc_info=True)
    await job_store.source_control.delete_gitlab_connection(
        user_id=user["user_id"], connection_id=connection_id
    )
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
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Drop only the authenticated user's GitHub installation grant."""
    job_store = _require_store(store)
    await job_store.source_control.revoke_repo_grant(
        user_id=user["user_id"], installation_id=installation_id
    )
    return GitHubConnectionResponse(
        connections=await job_store.source_control.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store.source_control, app_credentials=app_credentials
        ),
    )


@router.post("/github/connect", response_model=GitHubConnectionResponse)
async def connect_github(
    body: GitHubConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> GitHubConnectionResponse:
    """Drop one connection. Uninstalling on GitHub is the other half."""
    job_store = _require_store(store)
    await job_store.source_control.revoke_repo_grant(
        user_id=user["user_id"], installation_id=installation_id
    )
    return GitHubConnectionResponse(
        connections=await job_store.source_control.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store.source_control, app_credentials=app_credentials
        ),
    )


@router.get("/branches", response_model=RepoBranchesResponse)
async def list_repo_branches(
    repo: str = Query(..., description="owner/name"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
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
            repo, user["user_id"], store=job_store.source_control, app_credentials=app_credentials
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


async def _mcp_connections_for_user(store: _RoutesStorage, user_id: str) -> list[dict[str, Any]]:
    """The MCP connections *this user* has added, for the composer.

    Display metadata only. A connection's address and its credential never
    leave this process: the sandbox reaches a server through the relay, which
    is the entire reason resolution lives beside the relay rather than beside
    the picker. :class:`McpConnectionSummary` has no field either could land
    in, so that stays true without a filter at this call site.

    Unusable connections are listed with their reason rather than omitted. A
    picker that silently drops a server its owner is looking for is a bug
    report waiting to happen; one that says *this credential expired* is an
    instruction.
    """
    rows = await store.mcp.list_mcp_connections(user_id=user_id)
    summaries: list[dict[str, Any]] = []
    for row in rows:
        resolved = await resolve_connection(store.mcp, row=row)
        snapshot = resolved.snapshot
        summaries.append(
            {
                "connection_id": int(row["id"]),
                "label": resolved.connection.label,
                "transport": resolved.connection.config.transport.value,
                "location": resolved.connection.config.location.value,
                "status": resolved.connection.status.value,
                "available": resolved.usable,
                "error": resolved.error,
                "account_label": row.get("account_label"),
                "tool_count": len(snapshot.tools) if snapshot else 0,
            }
        )
    return summaries


@router.post("/jobs", response_model=AgentJobResponse, status_code=201)
async def create_agent_job(
    body: AgentJobCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentSessionListResponse:
    """Search complete conversations by stable title, project and display state."""
    job_store = _require_store(store)
    try:
        result = await job_store.conversation_queries.list_sessions(
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentSessionArchiveResponse:
    """Archive or unarchive an explicit owner-selected snapshot atomically."""
    result = await _require_store(store).conversation_commands.set_sessions_archived(
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentSessionDeleteResponse:
    """Permanently remove archived sessions from owner history, retaining audit data."""
    result = await _require_store(store).conversation_commands.delete_sessions(
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentConversationListResponse:
    """Search owned conversation titles and repositories across all history."""
    job_store = _require_store(store)
    try:
        page = await job_store.conversation_queries.list_conversations(
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentProjectListResponse:
    """Summarize the caller's projects for the sidebar's task tree."""
    job_store = _require_store(store)
    projects = await job_store.conversation_queries.list_projects(
        user_id=user["user_id"], archived=archived
    )
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None,
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentThreadArchiveResponse:
    """Archive the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=True, user=user, store=store)


@router.delete("/jobs/{job_id}/archive", response_model=AgentThreadArchiveResponse)
async def restore_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentThreadArchiveResponse:
    """Restore the entire task thread containing an owned job."""
    return await _set_agent_thread_archived(job_id=job_id, archived=False, user=user, store=store)


@router.patch("/jobs/{job_id}/title", response_model=AgentThreadRenameResponse)
async def rename_agent_thread(
    job_id: str,
    body: AgentThreadRenameRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None,
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentThreadPinResponse:
    """Pin the entire task thread containing an owned job."""
    return await _set_agent_thread_pinned(job_id=job_id, pinned=True, user=user, store=store)


@router.delete("/jobs/{job_id}/pin", response_model=AgentThreadPinResponse)
async def unpin_agent_thread(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> AgentJobResponse:
    """Append a turn and queue its member in the retained shared sandbox.

    An ``Idempotency-Key`` names this one submission, as for creation: a
    resend returns the turn the first send made, different content under the
    same key is a 409 ``idempotency_conflict``.
    """
    job_store = _require_store(store)
    parent = await _owned_job(job_store.conversation_queries, job_id, user)
    # Ownership is proved above; the resend is answered next, before the checks
    # that can change between two sends of one submission. A first send whose
    # response was lost, followed by the session being archived or its sandbox
    # context going indeterminate, must still return the turn that send made —
    # otherwise the retry is refused for a reason the first send never saw.
    fingerprint = (
        request_fingerprint(
            "follow_up",
            {
                "parent_job_id": job_id,
                **body.model_dump(mode="json"),
            },
        )
        if idempotency_key is not None
        else None
    )
    if idempotency_key is not None:
        replayed = await job_store.conversation_commands.replayed_submission(
            user_id=user["user_id"],
            client_request_id=idempotency_key,
            fingerprint=fingerprint,
        )
        if replayed is not None:
            if replayed.get("error") == "client_request_conflict":
                raise _idempotency_conflict()
            return _job_response(
                await _owned_job(job_store.conversation_queries, str(replayed["id"]), user)
            )
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
            store=job_store.source_control,
            app_credentials=app_credentials,
        )
    except RepoNotAllowed as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": {"type": "repo_not_allowed", "message": str(exc)}},
        ) from exc
    # An unavailable account integration must not prevent unrelated conversation
    # work. The runner receives only usable servers, resolved when it claims
    # this turn, and Tools retains each connection's diagnosis.
    skills = (
        await _resolve_skill_selection(
            job_store.skills, user_id=user["user_id"], skill_ids=body.skill_ids
        )
        if body.skill_ids is not None
        else None
    )
    attachments = await _resolve_attachment_selection(
        job_store.attachments, user_id=user["user_id"], attachment_ids=body.attachment_ids
    )
    job = await job_store.conversation_commands.create_follow_up(
        parent_job_id=job_id,
        user_id=user["user_id"],
        prompt=body.prompt,
        runtime=body.runtime,
        # Canonical, or the parent's when omitted — never the alias the caller
        # typed, which the gateway's grant would not match.
        model=model if body.model else None,
        reasoning_effort=reasoning_effort,
        reset_reasoning_effort=reset_effort,
        **({"skills": skills} if skills is not None else {}),
        **({"attachments": attachments} if attachments is not None else {}),
        client_request_id=idempotency_key,
        # The same value the replay lookup above used, so the pre-check and the
        # unique index cannot disagree about what was submitted.
        client_request_fingerprint=fingerprint,
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
    # Command rows omit retained membership joins. Return the same owner read
    # as GET /jobs/{id}, including the sandbox and native fork provenance.
    return _job_response(await _owned_job(job_store.conversation_queries, str(job["id"]), user))


@router.post("/jobs/{job_id}/fork", response_model=AgentJobResponse, status_code=201)
async def fork_agent_job(
    job_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=256),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
    grants: Any = Depends(get_grant_client),
) -> AgentJobResponse:
    """Fork one settled native session into the same shared Docker workspace."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
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

    operation = await job_store.session_fork.prepare_session_fork(
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
            "session_archived": (
                "session_archived",
                "This conversation is archived. Restore it before creating a side chat.",
            ),
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
            grant_id = await job_store.residency.release_residency(
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
        broker = await broker_for_job(job_store.host_pool, job_id)
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
        launch_common: dict[str, Any] = {
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
        claimed = await job_store.session_fork.claim_session_fork_materialization(
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
        residencies = await job_store.session_fork.establish_session_fork_residencies(
            operation_id=operation_id,
            coordinator_token=coordinator_token,
            ttl_seconds=RESIDENCY_TTL_S,
        )
        if residencies is None:
            await job_store.session_fork.release_session_fork_materialization(
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
            job_store.grants,
            grants,
            row=residencies["source"],
        )
        target_credential = await _mint_residency_grant(
            job_store.grants,
            grants,
            row=residencies["target"],
        )
        if not source_credential or not target_credential:
            await release_failed_fork_residencies(residencies)
            await job_store.session_fork.release_session_fork_materialization(
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
        materialized = await job_store.session_fork.record_session_fork_materialized(
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

    forked = await job_store.session_fork.finalize_session_fork(operation_id=operation_id)
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentThreadResponse:
    """Return the durable conversation containing one of the caller's jobs."""
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    thread = await job_store.conversation_queries.get_thread_for_job(
        job_id=job_id, user_id=user["user_id"]
    )
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentThreadStatsResponse:
    """Summarize the thread containing a job: turns, timings, tokens.

    Every number is derived from rows the platform already writes, so a
    thread that predates this endpoint answers it in full. Measures the
    thread's runtime does not report come back ``null`` rather than zero.
    """
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    inputs = await job_store.execution_queries.thread_stats_inputs(
        job_id=job_id, user_id=user["user_id"]
    )
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentThreadEventsResponse:
    """Page every turn's event log, with payloads and turn labels intact."""
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    events = await job_store.execution_queries.list_thread_events_after(
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> AgentJobEventsResponse:
    """Page through a job's append-only event log (non-streaming)."""
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)
    events = await job_store.execution_queries.list_events_after(
        job_id=job_id, after_id=after, limit=limit
    )
    return AgentJobEventsResponse(
        events=[_event_response(event) for event in events],
        next_cursor=events[-1]["id"] if events else after,
    )


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


async def _require_mutable_workspace_ready(
    *, job: dict[str, Any], store: SandboxRepository
) -> None:
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
            store=store.source_control,
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
    store: _RoutesStorage | None,
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
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    if require_ready:
        ready = job.get(
            "thread_sandbox"
        ) is not None and await job_store.sandbox.thread_workspace_ready(
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
        await job_store.sandbox.mark_terminal_resume_complete(job_id=job_id)
        job["terminal_resume_pending"] = False
    return job, broker


@router.get("/jobs/{job_id}/files", response_model=AgentWorkspaceResponse)
async def get_agent_job_workspace_file(
    job_id: str,
    path: str = Query("", max_length=4096),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentWorkspaceResponse:
    """Browse the chat's retained workspace through its owning host broker."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
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
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentWorkspaceResponse:
    """Save one text file directly into the retained chat workspace."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
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
    await _require_mutable_workspace_ready(job=job, store=job_store.sandbox)
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
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentTerminalResponse:
    """Run an owner-entered command inside the retained chat sandbox."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
    await _require_workspace_entitlement(
        job=job, user=user, store=job_store, app_credentials=app_credentials
    )
    await _require_mutable_workspace_ready(job=job, store=job_store.sandbox)
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
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

    async def relay() -> AsyncIterator[Any]:
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
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
    store: _RoutesStorage | None = Depends(get_storage),
    app_credentials: Any | None = Depends(get_agent_app_credentials),
) -> AgentGitWorkspaceResponse:
    """Read live retained-workspace status, diff and recent commits."""
    job_store = _require_store(store)
    job = await _owned_job(job_store.conversation_queries, job_id, user)
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
    store: _RoutesStorage | None = Depends(get_storage),
) -> StreamingResponse:
    """Stream a job's events as SSE, resumable via ``Last-Event-ID``.

    Each SSE frame carries the event's global id, so a client that reconnects
    with ``Last-Event-ID`` resumes exactly where it left off — which is what
    makes the finite stream cap (applied by the timeout middleware to
    ``text/event-stream``) safe for jobs that outlive one connection.
    """
    job_store = _require_store(store)
    await _owned_job(job_store.conversation_queries, job_id, user)

    cursor = after
    if last_event_id:
        with contextlib.suppress(ValueError):
            cursor = int(last_event_id)

    async def event_source() -> AsyncIterator[Any]:
        nonlocal cursor
        idle_polls = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                events = await job_store.execution_queries.list_events_after(
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

                job = await job_store.conversation_queries.get_job(job_id)
                if job is None:
                    return
                if job["state"] in TERMINAL_STATES:
                    # Drain to exhaustion before announcing the end. A single
                    # extra page is not enough: a client attaching to a
                    # finished job with more than two pages of backlog would
                    # see job_finished, close, and lose the rest forever.
                    while True:
                        tail = await job_store.execution_queries.list_events_after(
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


from cloud_agent.api import worker

# The worker surface is a leaf the aggregator mounts. It is included last
# because its seventeen handlers were the last contiguous block in this module,
# so the effective match order is the order they were declared in before the
# split — the reorder the checklist asks to be proven is not one that happens.
router.include_router(worker.router)

#: The leaf routers this aggregator mounts, in the order the concerns first
#: appear. Tests walk these rather than ``router.routes``, which holds lazy
#: include wrappers rather than routes.
SURFACE_ROUTERS = (worker.router,)
