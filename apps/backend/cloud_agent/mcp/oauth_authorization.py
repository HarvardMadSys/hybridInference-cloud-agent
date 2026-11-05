"""Starting a browser authorization for an OAuth MCP connection.

The owner asks to sign in; this flow decides where the browser goes. It runs
in named steps, each the one place its rule lives:

1. **The connection**: the owner's row, after storing a client id the owner
   supplied (before discovery, so the registration choice sees it and the flow
   it starts is the one this client will complete).
2. **The target**: an OAuth connection with an address, and a deployment with
   a public callback address.
3. **Discovery** of the authorization server, under the deployment's egress
   policy.
4. **The client**: GitHub's managed app (only at GitHub's own endpoints), the
   owner's own client, or a registration the server must be asked for.
5. **Registration**, dynamically, when the server supports nothing better.
6. **The flow**: scopes, the resource, PKCE and state, and the authorization
   URL built from them.
7. **Persisting** the flow, its secrets encrypted, under the connection's
   version fence.

Discovery, registration and the challenge all happen here, so the browser is
sent to a URL this service built rather than one a caller supplied. Refusals
are ``Refused`` and ``Unavailable`` in the words the route always answered
with; the API maps them, and `SignInUnavailable`, onto HTTP.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.domain.errors import Refused, Unavailable
from cloud_agent.mcp import github_oauth, oauth
from cloud_agent.mcp.http_body import IDENTITY_ENCODING_HEADERS
from cloud_agent.mcp.models import AuthKind
from cloud_agent.mcp.net_policy import EndpointRejected
from cloud_agent.mcp.registry import connection_from_row, oauth_cipher

if TYPE_CHECKING:
    from cloud_agent.mcp.models import McpConnection
    from cloud_agent.mcp.net_policy import EgressPolicy
    from cloud_agent.ports import McpConnectionRepository


@dataclass(frozen=True)
class AuthorizationStarted:
    """The browser goes to ``authorization_url``."""

    authorization_url: str


@dataclass(frozen=True)
class ClientIdNeeded:
    """The server needs a client the owner registers at ``redirect_uri``."""

    message: str
    redirect_uri: str


class SignInUnavailable(Exception):
    """GitHub's browser sign-in has no client yet; an outage with no error type."""


@dataclass(frozen=True)
class _ChosenClient:
    identity: oauth.ClientIdentity
    metadata: oauth.AuthorizationServerMetadata
    managed: bool


def _invalid(message: str) -> Refused:
    return Refused("invalid", "invalid_request_error", message)


def _changed() -> Refused:
    return Refused("conflict", "conflict", "this connection changed while you were authorizing it")


def _no_encryption_key() -> Unavailable:
    """The deployment cannot hold a credential at all."""
    return Unavailable("server_error", "this deployment has no MCP encryption key configured")


def callback_url(base_url: str, connection_id: int) -> str:
    """The one redirect URI for a connection, used for register, authorize and redeem.

    Defined once because the three have to agree. A server compares the
    ``redirect_uri`` in the authorization request against the one registered
    for the client -- the specification requires it to -- so a registration that
    used a different string, even a prefix of the right one, fails the flow at
    the browser with nothing in this service's logs.
    """
    return oauth.redirect_uri(base_url, str(connection_id))


async def _connection_row(
    store: McpConnectionRepository,
    *,
    connection_id: int,
    user_id: str,
    client_id: str | None,
    client_secret: str | None,
) -> Mapping[str, Any]:
    """Step 1: the owner's row, with a client id they supplied stored first."""
    row = await store.get_mcp_connection(connection_id=connection_id, user_id=user_id)
    if row is None:
        raise Refused("not_found", "not_found", "no such MCP connection")
    if not client_id:
        return row
    secret_ciphertext = None
    if client_secret:
        try:
            secret_ciphertext = oauth_cipher().encrypt(client_secret)
        except CredentialCipherError as exc:
            raise _no_encryption_key() from exc
    stored = await store.set_mcp_client_registration(
        connection_id=connection_id,
        user_id=user_id,
        client_id=client_id.strip(),
        client_secret_ciphertext=secret_ciphertext,
    )
    if not stored:
        raise _changed()
    refreshed = await store.get_mcp_connection(connection_id=connection_id, user_id=user_id)
    return refreshed or row


def _oauth_target(row: Mapping[str, Any], *, base_url: str) -> McpConnection:
    """Step 2: an OAuth connection with an address, on a deployment with a callback."""
    connection = connection_from_row(row)
    if connection.auth.kind is not AuthKind.OAUTH:
        raise _invalid("this connection does not use OAuth")
    if not connection.config.url:
        raise _invalid("this connection has no address to authorize against")
    if not base_url:
        raise Unavailable(
            "server_error",
            "Server sign-in is unavailable because this deployment's public "
            "callback address is not configured. Please contact the administrator.",
        )
    return connection


async def _discover(
    fetcher: Any, connection: McpConnection, *, policy: EgressPolicy
) -> tuple[oauth.ProtectedResourceMetadata | None, oauth.AuthorizationServerMetadata]:
    """Step 3: the authorization server, found from the connection's address."""
    from cloud_agent.mcp.transports import TransportError

    try:
        return await oauth.discover_authorization_server(
            fetcher,
            resource_url=str(connection.config.url),
            policy=policy,
            preregistered_issuer=connection.auth.oauth.issuer if connection.auth.oauth else None,
        )
    except (oauth.OAuthError, TransportError, EndpointRejected) as exc:
        raise _invalid(str(exc)) from exc


def _managed_client(
    connection: McpConnection,
    metadata: oauth.AuthorizationServerMetadata,
    managed: tuple[str, str],
) -> tuple[str, str]:
    """GitHub's shared app, only while discovery returned GitHub's own endpoints.

    Never send the shared secret to an endpoint supplied by discovery.
    """
    if (
        not github_oauth.is_managed(
            issuer=metadata.issuer,
            token_endpoint=metadata.token_endpoint,
            resource=connection.config.url,
        )
        or metadata.authorization_endpoint != github_oauth.AUTHORIZATION_ENDPOINT
    ):
        raise oauth.OAuthError("GitHub returned unexpected sign-in endpoints.")
    return managed


def _choose_client(
    row: Mapping[str, Any],
    connection: McpConnection,
    metadata: oauth.AuthorizationServerMetadata,
    *,
    requested_client_id: str | None,
    github_client: tuple[str, str] | None,
) -> _ChosenClient:
    """Step 4: which client this flow signs in as, and the metadata it uses.

    Raises ``oauth.OAuthError`` for the caller to read: a server that needs a
    registered client is answered with the form for one, not a refusal.
    """
    settings = row.get("auth_config")
    configured = settings if isinstance(settings, Mapping) else {}
    client_id = str(configured.get("client_id") or "") or None
    github_endpoint = github_oauth.is_endpoint(connection.config.url)
    managed_client = (
        github_client
        if github_endpoint
        and (not client_id or configured.get("managed_github_oauth"))
        and not requested_client_id
        else None
    )
    secret = None
    if managed_client:
        client_id, secret = _managed_client(connection, metadata, managed_client)
    elif configured.get("managed_github_oauth") and not requested_client_id:
        raise _invalid(
            "GitHub browser sign-in is unavailable. Ask the service administrator to enable it."
        )
    elif row.get("client_secret_ciphertext"):
        secret = oauth_cipher().decrypt(str(row["client_secret_ciphertext"]))
    if github_endpoint and not client_id:
        raise SignInUnavailable(
            "GitHub browser sign-in is not available yet. "
            "Use a personal access token or ask the service administrator to enable it."
        )
    if github_endpoint and metadata.token_endpoint == github_oauth.TOKEN_ENDPOINT:
        # GitHub omits this field from metadata (RFC default: Basic), but
        # documents client_secret in the form body for both app types.
        metadata = replace(metadata, token_endpoint_auth_methods_supported=("client_secret_post",))
    identity = oauth.choose_registration(
        metadata, preregistered_client_id=client_id, preregistered_client_secret=secret
    )
    return _ChosenClient(identity=identity, metadata=metadata, managed=bool(managed_client))


async def _register_client(
    fetcher: Any,
    metadata: oauth.AuthorizationServerMetadata,
    *,
    policy: EgressPolicy,
    base_url: str,
    connection_id: int,
) -> oauth.ClientIdentity:
    """Step 5: register dynamically, when the server supports nothing better."""
    from cloud_agent.mcp.transports import TransportError

    if not metadata.registration_endpoint:
        raise _invalid("this authorization server has no registration endpoint")
    body = oauth.registration_request_body(
        client_name="HybridInference Cloud Agent",
        # The URI the redirect will actually use, computed the same way, not
        # the bare path. A server that checks the registered value against the
        # one in the authorization request -- which the specification requires
        # it to -- refuses the two that differ, and the mismatch is invisible
        # until somebody tries to connect.
        redirect_uris=[callback_url(base_url, connection_id)],
        scopes=metadata.scopes_supported,
    )
    try:
        payload = await post_json(fetcher, metadata.registration_endpoint, body, policy=policy)
    except TransportError as exc:
        raise _invalid(f"client registration failed: {exc}") from exc
    return oauth.parse_registration_response(payload)


def _resource(
    connection: McpConnection, resource_metadata: oauth.ProtectedResourceMetadata | None
) -> str:
    """The OAuth resource identifier, which can differ from the MCP transport URL.

    Reauthorization may reuse the issuer without fetching resource metadata;
    retain a saved resource only while it still describes the current URL.
    """
    url = str(connection.config.url)
    resource = url
    if resource_metadata:
        resource = resource_metadata.resource
    elif connection.auth.oauth and connection.auth.oauth.resource:
        # Editing the URL can leave the previous credential's resource on
        # the row. Keep the current URL when that saved resource no longer fits.
        with suppress(oauth.OAuthError):
            resource = oauth.parse_protected_resource_metadata(
                {"resource": connection.auth.oauth.resource}, expect=url
            ).resource
    return resource


def _flow(
    connection: McpConnection,
    connection_id: int,
    chosen: _ChosenClient,
    resource_metadata: oauth.ProtectedResourceMetadata | None,
    *,
    base_url: str,
) -> oauth.OAuthFlow:
    """Step 6: the flow this start offers the browser."""
    scopes = oauth.default_scopes(
        resource_metadata,
        chosen.metadata,
        connection.auth.oauth.scopes if connection.auth.oauth else (),
    )
    if chosen.managed:
        # Avoid requesting every optional toolset scope in GitHub's metadata.
        scopes = ("repo", "read:org")
    return oauth.OAuthFlow(
        versions=oauth.FlowVersions(
            connection_id=str(connection_id),
            config_revision=connection.config_revision,
            authorization_epoch=connection.authorization_epoch,
            oauth_generation=connection.oauth_generation,
        ),
        state=oauth.new_state(),
        pkce=oauth.PkcePair.generate(),
        client=chosen.identity,
        metadata=chosen.metadata,
        resource=_resource(connection, resource_metadata),
        redirect_uri=(
            oauth.redirect_uri(base_url, "github")
            if chosen.managed
            else callback_url(base_url, connection_id)
        ),
        scopes=scopes,
    )


async def _persist_flow(
    store: McpConnectionRepository,
    flow: oauth.OAuthFlow,
    connection: McpConnection,
    *,
    connection_id: int,
    user_id: str,
) -> None:
    """Step 7: store the flow, its verifier and client secret encrypted."""
    cipher = oauth_cipher()
    identity = flow.client
    metadata = flow.metadata
    # The generation is allocated in the store from the value just read. Passing
    # ``snapshot + 1`` lets two starts both insert a flow for a generation the
    # row has already left, and lets the later transaction delete the newer one.
    allocated = await store.create_mcp_oauth_flow(
        state_hash=oauth.state_hash(flow.state),
        user_id=user_id,
        connection_id=connection_id,
        config_revision=connection.config_revision,
        authorization_epoch=connection.authorization_epoch,
        expected_oauth_generation=connection.oauth_generation,
        code_verifier_ciphertext=cipher.encrypt(flow.pkce.verifier),
        client_id=identity.client_id,
        client_secret_ciphertext=(
            cipher.encrypt(identity.client_secret) if identity.client_secret else None
        ),
        client_auth_method=identity.auth_method,
        registration=identity.registration,
        issuer=metadata.issuer,
        resource=flow.resource,
        redirect_uri=flow.redirect_uri,
        scopes=list(flow.scopes),
        authorization_endpoint=metadata.authorization_endpoint,
        token_endpoint=metadata.token_endpoint,
        revocation_endpoint=metadata.revocation_endpoint,
        registration_endpoint=metadata.registration_endpoint,
        expires_at=datetime.now(UTC) + oauth.FLOW_LIFETIME,
    )
    if allocated is None:
        raise _changed()


async def start_authorization(
    store: McpConnectionRepository,
    *,
    connection_id: int,
    user_id: str,
    client_id: str | None,
    client_secret: str | None,
    base_url: str,
    policy: EgressPolicy,
    github_client: tuple[str, str] | None,
) -> AuthorizationStarted | ClientIdNeeded:
    """Start the owner's browser authorization of one OAuth connection.

    ``client_id`` and ``client_secret`` are what the owner supplied with this
    request, if anything; ``github_client`` is the service's GitHub app.

    Raises:
        Refused: no such connection, not an OAuth target, a discovery or
            registration failure, or a connection that changed meanwhile.
        Unavailable: no callback address, or no encryption key.
        SignInUnavailable: GitHub's browser sign-in has no client.
    """
    from cloud_agent.mcp.transports import HttpMetadataFetcher

    row = await _connection_row(
        store,
        connection_id=connection_id,
        user_id=user_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    connection = _oauth_target(row, base_url=base_url)
    fetcher = HttpMetadataFetcher()
    resource_metadata, metadata = await _discover(fetcher, connection, policy=policy)
    try:
        chosen = _choose_client(
            row,
            connection,
            metadata,
            requested_client_id=client_id,
            github_client=github_client,
        )
    except oauth.OAuthError as exc:
        if exc.kind == "client_registration_required":
            return ClientIdNeeded(
                message=str(exc), redirect_uri=callback_url(base_url, connection_id)
            )
        raise _invalid(str(exc)) from exc
    if chosen.identity.registration == "dcr":
        identity = await _register_client(
            fetcher, chosen.metadata, policy=policy, base_url=base_url, connection_id=connection_id
        )
        chosen = replace(chosen, identity=identity)
    flow = _flow(connection, connection_id, chosen, resource_metadata, base_url=base_url)
    try:
        authorization_url = flow.authorization_url()
    except oauth.OAuthError as exc:
        raise _invalid(str(exc)) from exc
    await _persist_flow(store, flow, connection, connection_id=connection_id, user_id=user_id)
    return AuthorizationStarted(authorization_url=authorization_url)


async def post_json(
    fetcher: Any, url: str, body: Mapping[str, Any], *, policy: EgressPolicy
) -> Mapping[str, Any]:
    """POST a JSON body under the same egress policy as everything else.

    Every failure leaves here as a refusal the caller can report. A raw
    ``httpx`` exception or a policy refusal escaping this helper becomes a 500
    with no diagnosis -- from a URL the *authorization server's* metadata chose,
    which is the one input in this path the user did not supply.
    """
    import httpx

    from cloud_agent.mcp.net_policy import check_addresses, validate_endpoint
    from cloud_agent.mcp.transports import TransportError, pinned_transport, read_bounded

    try:
        endpoint = validate_endpoint(url, policy)
        loop_addresses = await resolve_addresses(endpoint.host, endpoint.port)
        check_addresses(endpoint.host, loop_addresses, policy)
    except EndpointRejected as exc:
        raise _invalid(str(exc)) from exc
    except OSError as exc:
        raise _invalid(f"that address could not be resolved: {exc}") from exc
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
            headers=IDENTITY_ENCODING_HEADERS,
            transport=pinned_transport(),
        ) as client:
            # Streamed, so the size limit is applied while the body arrives
            # rather than to a body the registration server already chose the
            # size of. Registration is the one request here whose *response*
            # contains a client secret.
            response = await client.send(
                client.build_request(
                    "POST",
                    endpoint.url,
                    json=dict(body),
                    headers={"accept": "application/json", **IDENTITY_ENCODING_HEADERS},
                ),
                stream=True,
            )
            try:
                status = response.status_code
                payload = await read_bounded(response)
            finally:
                await response.aclose()
    except (httpx.HTTPError, TransportError) as exc:
        raise _invalid(f"the authorization server could not be reached: {exc}") from exc
    if status >= 400:
        raise _invalid(f"the authorization server answered {status} to registration")
    try:
        document = json.loads(payload or b"{}")
    except ValueError as exc:
        raise _invalid("the registration response is not JSON") from exc
    if not isinstance(document, Mapping):
        raise _invalid("the registration response is not an object")
    del fetcher
    return document


async def resolve_addresses(host: str, port: int) -> list[str]:
    """Resolve a host to its addresses, for the egress check.

    Under the spelling the connection layer dials rather than the one that was
    typed: ``getaddrinfo`` applies the 2003 IDNA codec, so a unicode label
    resolves as a different domain from the one httpx connects to.
    """
    from cloud_agent.mcp.net_policy import dial_host

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(dial_host(host), port, type=0)
    return [str(info[4][0]) for info in infos]


__all__ = [
    "AuthorizationStarted",
    "ClientIdNeeded",
    "SignInUnavailable",
    "callback_url",
    "post_json",
    "resolve_addresses",
    "start_authorization",
]
