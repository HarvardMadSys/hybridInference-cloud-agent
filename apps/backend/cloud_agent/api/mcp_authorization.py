"""Browser sign-in for an OAuth MCP connection: starting it and completing its callback.

Split from :mod:`cloud_agent.api.mcp_connections`, whose router serves these
routes as its own. Starting a sign-in is `oauth_authorization.start_authorization`;
the callback is answered here, because it is a browser navigation that must
end at a page of the app rather than at an error body, and because it is
fenced to the account and the flow that started it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.deps import get_current_user, get_storage, require_local_admission
from cloud_agent.api.errors import as_http_exception
from cloud_agent.api.mcp_connections_shared import (
    _github_browser_client,
    _McpConnectionsStorage,
    _policy,
    _public_base_url,
    _require_store,
)
from cloud_agent.api.mcp_oauth_callback_checks import _callback_request_refusal, _stale_flow_refusal
from cloud_agent.crypto.credential_cipher import CredentialCipherError
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import github_oauth, oauth, oauth_authorization
from cloud_agent.mcp.http_body import IDENTITY_ENCODING_HEADERS
from cloud_agent.mcp.registry import mcp_cipher, oauth_cipher
from cloud_agent.settings import setting_env

if TYPE_CHECKING:
    from cloud_agent.mcp.net_policy import EgressPolicy

#: The records keep the name they had when every MCP owner route lived there.
logger = get_logger("cloud_agent.api.mcp_connections")

router = APIRouter(prefix="/v1/agent/mcp", tags=["agent-mcp"])


class AuthorizeRequest(BaseModel):
    """The client to authorize as, when the server cannot register one for us.

    Optional, and it exists because some servers expect the user to bring a
    client id: refusing to accept one would leave the browser flow permanently
    unavailable on exactly the servers that document it as the way in.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = Field(default=None, max_length=512)


class AuthorizeResponse(BaseModel):
    """Where to send the browser, and what the flow is waiting for."""

    authorization_url: str | None = None
    needs_client_id: bool = False
    message: str = ""
    redirect_uri: str | None = None


@router.post("/connections/{connection_id}/authorize", response_model=AuthorizeResponse)
async def authorize_connection(
    connection_id: int,
    body: AuthorizeRequest | None = None,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: _McpConnectionsStorage | None = Depends(get_storage),
) -> AuthorizeResponse:
    """Start a browser authorization for an OAuth connection.

    Discovery, registration and the challenge all happen here, so the browser
    is sent to a URL this service built rather than one a caller supplied.
    """
    # The docstring is the operation's published description, so it stays as
    # it was; the steps it names are `oauth_authorization.start_authorization`.
    job_store = _require_store(store)
    try:
        outcome = await oauth_authorization.start_authorization(
            job_store.mcp,
            connection_id=connection_id,
            user_id=user["user_id"],
            client_id=body.client_id if body is not None else None,
            client_secret=body.client_secret if body is not None else None,
            base_url=_public_base_url(),
            policy=_policy(),
            github_client=_github_browser_client(),
        )
    except (Refused, Unavailable) as exc:
        raise as_http_exception(exc) from exc
    except oauth_authorization.SignInUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error": {"message": str(exc)}}) from exc
    if isinstance(outcome, oauth_authorization.ClientIdNeeded):
        return AuthorizeResponse(
            needs_client_id=True, message=outcome.message, redirect_uri=outcome.redirect_uri
        )
    return AuthorizeResponse(authorization_url=outcome.authorization_url)


async def _callback_session_user(
    request: Request, storage: Any = Depends(get_storage)
) -> str | None:
    """The Agent account the returning browser is signed in as, or ``None``.

    Read by the same verifier as every owner route, but answered rather than
    raised: the callback is a browser navigation, and a 401 body is a dead end
    where a redirect back to the app can say what to do. The cookie is ``Lax``,
    so it travels on this top-level GET; a redirect cannot carry a bearer. A
    local account must also still hold ``agent.use``.
    """
    try:
        user = await get_current_user(request, authorization=None, storage=storage)
        await require_local_admission(request, str(user["user_id"]))
    except HTTPException:
        return None
    return str(user["user_id"])


#: Why a callback without the starting account's session was refused. Fixed
#: text: nothing about the flow, or about who started it, is disclosed.
_CALLBACK_NEEDS_SESSION = (
    "sign in to the account that started this authorization, then sign in to the server again"
)


@router.get("/oauth/callback/{provider}", response_model=None)
async def oauth_callback(
    provider: str,
    request: Request,
    state: str = Query(""),
    code: str = Query(""),
    error: str = Query(""),
    store: _McpConnectionsStorage | None = Depends(get_storage),
    session_user: str | None = Depends(_callback_session_user),
) -> Response:
    """Complete a browser authorization, fenced to the flow that started it.

    The *state* identifies the flow; the Agent session decides who may finish
    it. A flow is consumed only for the account that started it, before any
    code is exchanged. Without that, a consent URL started by one user and
    approved in another person's browser would store that person's provider
    grant on the first user's connection -- the authorization-code injection
    RFC 9700 describes, which PKCE does not stop because the platform holds
    the verifier. The connection is still taken from the flow row, written by
    an authenticated request, rather than from anything this request claims.
    """
    job_store = _require_store(store)
    refusal = _callback_request_refusal(provider, error=error, state=state, code=code)
    if refusal is not None:
        return _callback_redirect(provider, ok=False, message=refusal)
    if session_user is None:
        return _callback_redirect(provider, ok=False, message=_CALLBACK_NEEDS_SESSION)

    flow = await job_store.mcp.consume_mcp_oauth_flow(
        state_hash=oauth.state_hash(state), user_id=session_user
    )
    if flow is None:
        # Expired, already used, or started by another account: one answer,
        # so a state value cannot be probed for whose flow it is.
        return _callback_redirect(
            provider,
            ok=False,
            message=(
                "this authorization expired, was already used, or was started by another account"
            ),
        )
    uses_github_client = github_oauth.is_managed(
        issuer=flow["issuer"],
        token_endpoint=flow["token_endpoint"],
        resource=str(flow["resource"]),
    ) and urlsplit(str(flow["redirect_uri"])).path.endswith("/v1/agent/mcp/oauth/callback/github")
    managed_github = provider == "github" and uses_github_client
    if str(flow["connection_id"]) != str(provider) and not managed_github:
        # A state minted for one connection cannot complete another's flow.
        return _callback_redirect(
            provider, ok=False, message="this authorization is not for that connection"
        )
    if managed_github:
        provider = str(flow["connection_id"])

    row = await job_store.mcp.get_mcp_connection(connection_id=int(flow["connection_id"]))
    if row is None or row.get("deleted_at") is not None:
        return _callback_redirect(provider, ok=False, message="that connection was removed")
    stale = _stale_flow_refusal(row, flow)
    if stale is not None:
        return _callback_redirect(provider, ok=False, message=stale)

    cipher = oauth_cipher()
    try:
        verifier = cipher.decrypt(str(flow["code_verifier_ciphertext"]))
    except CredentialCipherError:
        return _callback_redirect(provider, ok=False, message="this flow could not be read")

    from cloud_agent.mcp.transports import TransportError

    metadata = oauth.AuthorizationServerMetadata(
        issuer=str(flow["issuer"]),
        authorization_endpoint=str(flow["authorization_endpoint"]),
        token_endpoint=str(flow["token_endpoint"]),
        revocation_endpoint=flow.get("revocation_endpoint"),
        registration_endpoint=flow.get("registration_endpoint"),
    )
    identity = oauth.ClientIdentity(
        client_id=str(flow["client_id"]),
        registration=flow.get("registration") or "dcr",
        client_secret=(
            cipher.decrypt(str(flow["client_secret_ciphertext"]))
            if flow.get("client_secret_ciphertext")
            else None
        ),
        auth_method=str(flow["client_auth_method"]),
    )
    token_request = oauth.TokenRequest(
        url=metadata.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(flow["redirect_uri"]),
            "client_id": identity.client_id,
            "code_verifier": verifier,
            "resource": str(flow["resource"]),
        },
        client=identity,
    )
    try:
        payload = await _post_form(token_request, policy=_policy())
        tokens = oauth.parse_token_response(payload)
    except (TransportError, oauth.OAuthError) as exc:
        logger.warning("agent_mcp_oauth_exchange_failed", exc_info=True)
        return _callback_redirect(provider, ok=False, message=str(exc))

    credential_cipher = mcp_cipher()
    auth_config_patch: dict[str, Any] = {"client_id": identity.client_id}
    if uses_github_client or (row.get("auth_config") or {}).get("managed_github_oauth"):
        auth_config_patch["managed_github_oauth"] = uses_github_client
    written = await job_store.mcp.set_mcp_credential(
        connection_id=int(flow["connection_id"]),
        expected_authorization_epoch=int(flow["authorization_epoch"]),
        access_token_ciphertext=credential_cipher.encrypt(tokens.access_token),
        refresh_token_ciphertext=(
            credential_cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None
        ),
        expires_at=tokens.expires_at,
        scopes=list(tokens.scopes) or list(flow.get("scopes") or []),
        issuer=metadata.issuer,
        resource=str(flow["resource"]),
        # Whether this issuer rotates refresh tokens is not knowable here.
        # Being handed one at authorization time says nothing about whether the
        # *next* refresh invalidates it -- RFC 6749 §6 lets a non-rotating
        # issuer send one, and lets any issuer omit it -- so the fact is
        # recorded where it can be observed, in ``oauth.apply_refresh``, when a
        # refresh returns a different token. Claiming rotation now would turn
        # that ordinary omission into a forced reconnection.
        refresh_rotates=False,
        client_auth_method=identity.auth_method,
        registration=identity.registration,
        # The client this authorization actually used, written with the
        # credential it obtained. A dynamic registration is decided while the
        # browser is being sent away and lives only in the flow row until here,
        # so without this the connection keeps a token it has no client id to
        # renew: the first refresh sends an empty one, and the authorization
        # server refuses a grant it never issued to "nobody".
        #
        # Merged into ``auth_config`` inside the fenced write rather than
        # recorded through ``set_mcp_client_registration``, which is the manual
        # path: that one bumps the OAuth generation to invalidate a flow already
        # in flight, and this *is* the flow. Bumping it here would refuse the
        # write this same call is making.
        auth_config_patch=auth_config_patch,
        # The managed app secret remains in encrypted settings, not the user's
        # connection. Refresh resolves it there after checking GitHub's target.
        # Otherwise retain the flow's ciphertext under its OAuth key domain.
        # For a public client it is absent and any earlier secret is left alone:
        # the authentication method written above is what decides whether one is
        # sent, so a leftover secret cannot be used by accident.
        client_secret_ciphertext=(
            None if uses_github_client else flow.get("client_secret_ciphertext")
        ),
        token_endpoint=metadata.token_endpoint,
        revocation_endpoint=metadata.revocation_endpoint,
        registration_endpoint=metadata.registration_endpoint,
        # The previous refresh token is dead -- this is an authorization, so
        # whatever was there before belonged to a different grant. The one in
        # this response is not: it is what the connection renews with, and the
        # store keeps it.
        clear_refresh=True,
        reset_status=True,
        # Re-checked here rather than only before the exchange. The token
        # request took real time, and a newer flow may have completed inside it:
        # the epoch would not catch that, because re-authorizing the same
        # connection is not a change of target.
        expected_oauth_generation=int(flow["oauth_generation"])
        if flow.get("oauth_generation") is not None
        else None,
        expected_config_revision=int(flow["config_revision"])
        if flow.get("config_revision") is not None
        else None,
    )
    if not written:
        return _callback_redirect(
            provider, ok=False, message="the connection changed during authorization"
        )
    logger.info(
        "agent_mcp_authorized",
        extra={"event": "agent_mcp_authorized", "connection_id": flow["connection_id"]},
    )
    return _callback_redirect(str(flow["connection_id"]), ok=True, message="")


async def _post_form(request: oauth.TokenRequest, *, policy: EgressPolicy) -> Mapping[str, Any]:
    """POST a token request under the egress policy.

    Raises only the caller's own refusal type. A policy refusal, a name that
    does not resolve, a transport failure or an answer that is not JSON all end
    as one, because the callback's job is to take the browser back to a page
    that says what happened -- not to raise a 500 out of a redirect it cannot
    send.
    """
    import httpx

    from cloud_agent.mcp.net_policy import EndpointRejected, check_addresses, validate_endpoint
    from cloud_agent.mcp.transports import TransportError, pinned_transport, read_bounded

    try:
        endpoint = validate_endpoint(request.url, policy)
        check_addresses(
            endpoint.host,
            await oauth_authorization.resolve_addresses(endpoint.host, endpoint.port),
            policy,
        )
    except EndpointRejected as exc:
        raise TransportError(str(exc), kind=exc.kind) from exc
    except OSError as exc:
        raise TransportError(f"the token endpoint could not be resolved: {exc}") from exc

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
            headers=IDENTITY_ENCODING_HEADERS,
            transport=pinned_transport(),
        ) as client:
            # Streamed for the same reason as everywhere else on this path: a
            # token response is a body a server chooses the size of, and this
            # one carries the access and refresh tokens.
            response = await client.send(
                client.build_request(
                    "POST",
                    endpoint.url,
                    data=request.form(),
                    headers={
                        "Accept": "application/json",
                        **request.headers(),
                        **IDENTITY_ENCODING_HEADERS,
                    },
                ),
                stream=True,
            )
            try:
                status = response.status_code
                payload = await read_bounded(response)
            finally:
                await response.aclose()
    except (httpx.HTTPError, TransportError) as exc:
        # Normalized here rather than left to escape: this is the second HTTP
        # client in the OAuth path, and the fetcher above already made this
        # conversion. A raw transport error would leave the browser at a 500
        # with no diagnosis, which is a worse version of the failure the caller
        # already knows how to report.
        raise TransportError(f"the token endpoint could not be reached: {exc}") from exc
    if status >= 400:
        raise TransportError(f"the token endpoint answered {status}", kind="needs_authorization")
    try:
        document = json.loads(payload or b"{}")
    except ValueError as exc:
        raise TransportError("the token response is not JSON") from exc
    if not isinstance(document, Mapping):
        raise TransportError("the token response is not an object")
    return document


def _callback_redirect(provider: str, *, ok: bool, message: str) -> Response:
    """Send the browser back to the app with a result it can display."""
    # The registered callback belongs to the API; the result belongs to the web
    # app, whose origin and mount path may differ (for example /agents/api vs /agents).
    base = (setting_env("AGENT_WEB_BASE_URL") or "").strip().rstrip("/")
    result = {"mcp_authorized": "1" if ok else "0", "mcp_connection": provider}
    if message:
        result["mcp_message"] = message
    target = f"{base}/integrations?{urlencode(result)}"
    return RedirectResponse(url=target, status_code=302)


__all__ = ["AuthorizeRequest", "AuthorizeResponse", "router"]
