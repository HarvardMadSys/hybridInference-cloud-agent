"""Source-control integrations: what is connected, and connecting it.

The GitHub App installation flow, the GitLab OAuth flow, and the branch list
the composer offers. Every step is scoped to the calling user: a connection
somebody else added is invisible here, not merely refused.

The four helpers travel with it — `_oauth_error`, `_connect_github_for_user`,
`_source_control_providers` and `_mcp_integrations` — because each is reached
only from this module's handlers.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_agent_gitlab_oauth,
    get_github_app_state,
    get_gitlab_load_status,
    get_storage,
    require_store,
)
from cloud_agent.entitlement import RepoNotAllowed, repos_for_user, require_entitled_repo
from cloud_agent.github_app import GitHubAppError
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.registry import resolve_connection
from cloud_agent.schemas import (
    GitHubConnectionResponse,
    GitHubConnectRequest,
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
from cloud_agent.source_control_runtime import (  # noqa: TC001 — FastAPI resolves them.
    GitHubAppState,
    LoadStatus,
)

if TYPE_CHECKING:
    from cloud_agent.ports import (
        McpConnectionRepository,
        SourceControlRepository as SourceControlPort,
    )


router = APIRouter(prefix="/v1/agent")

#: The moved handlers keep logging under the aggregator's logger name: the
#: deployment's log pipeline selects on ``cloud_agent.api.routes``, and a
#: relocation must not move this module's records to a logger nobody watches.
logger = get_logger("cloud_agent.api.routes")


class _SourceControlStorage(Protocol):
    """The two capabilities the integration surface reaches for."""

    mcp: McpConnectionRepository
    source_control: SourceControlPort


def _require_store(store: _SourceControlStorage | None) -> _SourceControlStorage:
    """Return the store or fail with a clear 503 when the DB is absent."""
    return require_store(store)


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
    store: _SourceControlStorage,
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
    store: _SourceControlStorage,
    app_credentials: Any | None,
    gitlab_oauth: Any | None,
    github_load_status: LoadStatus,
    gitlab_load_status: LoadStatus,
) -> list[SourceControlProviderResponse]:
    """Build user-scoped provider status with no credential material.

    A provider that is ``unavailable`` arrives here with no client, so it
    reads as unconfigured in every field but ``load_status`` — no connect link,
    no repository list — while the user's own connections still come from
    their records: an outage must not look like a lost connection.
    """
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
            load_status=github_load_status,
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
            load_status=gitlab_load_status,
        )
    )
    return providers


async def _mcp_integrations(
    *, user_id: str, store: _SourceControlStorage
) -> list[McpIntegrationResponse]:
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
    store: _SourceControlStorage | None = Depends(get_storage),
    github: GitHubAppState = Depends(get_github_app_state),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
    gitlab_load_status: LoadStatus = Depends(get_gitlab_load_status),
) -> SourceControlIntegrationsResponse:
    """Return configured and connected state for source control and MCP.

    One response for one page, deliberately: the settings screen shows both
    kinds of connection together, and two requests would let it render a
    half-refreshed state after a disconnect. The MCP half is a summary — adding,
    testing and selecting a connection live under ``/v1/agent/mcp``.

    Answers while the GitHub App is unavailable, and says so, rather than
    failing the whole page: that is the one place a user sees the outage.
    """
    job_store = _require_store(store)
    return SourceControlIntegrationsResponse(
        providers=await _source_control_providers(
            user_id=user["user_id"],
            store=job_store,
            app_credentials=github.credentials,
            gitlab_oauth=gitlab_oauth,
            github_load_status=github.load_status,
            gitlab_load_status=gitlab_load_status,
        ),
        mcp=await _mcp_integrations(user_id=user["user_id"], store=job_store),
    )


@router.post("/integrations/github/connect", response_model=GitHubConnectionResponse)
async def connect_github_integration(
    body: OAuthConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SourceControlStorage | None = Depends(get_storage),
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
    store: _SourceControlStorage | None = Depends(get_storage),
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
        load_status="ready",
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
    store: _SourceControlStorage | None = Depends(get_storage),
    github: GitHubAppState = Depends(get_github_app_state),
    gitlab_oauth: Any | None = Depends(get_agent_gitlab_oauth),
    gitlab_load_status: LoadStatus = Depends(get_gitlab_load_status),
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
            app_credentials=github.credentials,
            gitlab_oauth=gitlab_oauth,
            github_load_status=github.load_status,
            gitlab_load_status=gitlab_load_status,
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
    store: _SourceControlStorage | None = Depends(get_storage),
    github: GitHubAppState = Depends(get_github_app_state),
) -> GitHubConnectionResponse:
    """Drop only the authenticated user's GitHub installation grant.

    Works while the App is unavailable: the grant is this service's own row,
    and a user who wants a connection gone should not have to wait for an
    outage to end.
    """
    job_store = _require_store(store)
    await job_store.source_control.revoke_repo_grant(
        user_id=user["user_id"], installation_id=installation_id
    )
    return GitHubConnectionResponse(
        connections=await job_store.source_control.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store.source_control, app_credentials=github.credentials
        ),
    )


@router.post("/github/connect", response_model=GitHubConnectionResponse)
async def connect_github(
    body: GitHubConnectRequest,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SourceControlStorage | None = Depends(get_storage),
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
    store: _SourceControlStorage | None = Depends(get_storage),
    github: GitHubAppState = Depends(get_github_app_state),
) -> GitHubConnectionResponse:
    """Drop one connection. Uninstalling on GitHub is the other half.

    Works while the App is unavailable: the grant is this service's own row.
    """
    job_store = _require_store(store)
    await job_store.source_control.revoke_repo_grant(
        user_id=user["user_id"], installation_id=installation_id
    )
    return GitHubConnectionResponse(
        connections=await job_store.source_control.list_repo_grants(user_id=user["user_id"]),
        repos=await repos_for_user(
            user["user_id"], store=job_store.source_control, app_credentials=github.credentials
        ),
    )


@router.get("/branches", response_model=RepoBranchesResponse)
async def list_repo_branches(
    repo: str = Query(..., description="owner/name"),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _SourceControlStorage | None = Depends(get_storage),
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
