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

import os
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_agent_gitlab_oauth,
    get_gateway_client,
    get_storage,
)
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
    AgentConfigResponse,
    AgentConversation,
    AgentConversationListResponse,
    AgentGitWorkspaceResponse,
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
    _create_job_as,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _idempotency_conflict,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _JobCreationStorage,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_offered_effort,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_registered_runtime,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _require_resolvable_model,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _resolve_attachment_selection,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _resolve_mcp_bindings,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _resolve_skill_selection,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api._job_views import (
    _EVENT_PAGE_SIZE,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _SAFE_EVENT_TYPE,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _attempt_duration_ms,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _attempt_resources,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _attempt_summary_response,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _egress_tiers,
    _event_response,  # noqa: F401 — re-exported: the identity the host namespace kept.
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

# The owner surface's first batch moved to its own modules (R3-c of #225). The
# aggregator keeps binding the same objects, so a test, a ``Depends`` key or an
# importer that still says ``routes.X`` resolves to the identity it always did.
# These aliases exist for the transition; R5 deletes them once every caller
# imports the new home.
from cloud_agent.api.jobs import (
    _job_usage,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _set_agent_thread_archived,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _set_agent_thread_pinned,  # noqa: F401 — re-exported: the identity the host namespace kept.
    archive_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    cancel_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    create_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_attempt_telemetry,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_jobs,  # noqa: F401 — re-exported: the identity the host namespace kept.
    pin_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    rename_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    restore_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    unpin_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api.threads import (
    _KEEPALIVE_EVERY_N_POLLS,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _POLL_INTERVAL_S,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _SSE_HEADERS,
    _sse_frame,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_thread,  # noqa: F401 — re-exported: the identity the host namespace kept.
    get_agent_thread_stats,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_job_events,  # noqa: F401 — re-exported: the identity the host namespace kept.
    list_agent_thread_events,  # noqa: F401 — re-exported: the identity the host namespace kept.
    stream_agent_job_events,  # noqa: F401 — re-exported: the identity the host namespace kept.
)
from cloud_agent.api.turns import (
    SESSION_FORK_COORDINATOR_LEASE_S,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _sandbox_context_lost_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    _session_archived_error,  # noqa: F401 — re-exported: the identity the host namespace kept.
    create_agent_follow_up,  # noqa: F401 — re-exported: the identity the host namespace kept.
    fork_agent_job,  # noqa: F401 — re-exported: the identity the host namespace kept.
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


router = APIRouter(prefix="/v1/agent")


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


# ── Worker endpoints (capability-token authenticated) ──────────────────


from cloud_agent.api import jobs, threads, turns, worker

# The surface leaves this aggregator mounts, in the order the concerns first
# appear. FastAPI composes this router's own ``/v1/agent`` prefix with each
# leaf's, so a leaf that carried one too would double it; the leaves declare
# none while this module still holds handlers of its own. That is also why the
# includes come last: the handlers still declared above keep their relative
# order, and the leaves are ordered among themselves by the checklist's list.
router.include_router(jobs.router)
router.include_router(turns.router)
router.include_router(threads.router)
router.include_router(worker.router)

#: The leaf routers this aggregator mounts. Tests walk these rather than
#: ``router.routes``, which holds lazy include wrappers rather than routes.
SURFACE_ROUTERS = (jobs.router, turns.router, threads.router, worker.router)
