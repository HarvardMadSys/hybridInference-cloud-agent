"""OAuth for remote MCP servers: discovery, registration, PKCE and refresh.

OAuth is the one part of a generic connection where the platform acts as a
party to somebody else's protocol rather than as a proxy for the user, so the
rules are stricter than "redirect the browser and store the token".

**Discovery follows the resource, not the endpoint.** An MCP server's
authorization server is frequently a different origin, and the only
specification-blessed way to find it is the protected-resource metadata
document, whose location may also be named by a ``WWW-Authenticate`` header on
the 401 that started this. Guessing ``/.well-known/oauth-authorization-server``
on the MCP host works for exactly the servers that did not need the mechanism.

**Registration has three mechanisms and a precedence.** A pre-registered client
the deployment holds is used first, because it is the only one that needs no
round trip and no trust decision. Then client ID metadata documents, then
dynamic registration, each only when the authorization server says it supports
it. A server that supports none of them is not broken -- it expects the user to
paste a client id -- so the flow stops with that request rather than failing.

**The state binds everything a late callback could otherwise overwrite.** A
callback arriving after the connection was re-pointed, re-authorized or
disconnected must not write a token for the old target into the new row. The
flow row carries the versions it was started under, and the commit checks each
of them, which is why the checksum is a tuple rather than a flag.

**Refresh is a contract, not a guess.** Whether the issuer rotates the refresh
token, and whether the last response actually carried a new one, decide whether
the old one may be kept. Keeping a rotated-away token silently breaks the
connection at the next refresh, hours later, with no error until then.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote, urlencode, urlsplit

from cloud_agent.mcp.models import AuthKind, AuthProfile, OAuthProfile
from cloud_agent.mcp.net_policy import (
    EgressPolicy,
    EndpointRejected,
    normalize_url,
    validate_endpoint,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The path RFC 9728 defines for a protected resource's metadata.
PROTECTED_RESOURCE_WELL_KNOWN = "/.well-known/oauth-protected-resource"
#: The paths RFC 8414 and OpenID Connect define for an authorization server.
AS_METADATA_WELL_KNOWN = "/.well-known/oauth-authorization-server"
OIDC_METADATA_WELL_KNOWN = "/.well-known/openid-configuration"

#: How long a started flow stays resumable. Long enough for a login with a
#: second factor, short enough that an abandoned flow is not a standing offer.
FLOW_LIFETIME = timedelta(minutes=15)

#: Refresh this long before the access token actually expires, so a call that
#: starts just before the boundary does not fail just after it.
REFRESH_MARGIN = timedelta(seconds=120)

#: The registration mechanisms, in the order they are attempted.
RegistrationKind = Literal["preregistered", "cimd", "dcr"]


class OAuthError(RuntimeError):
    """Raised when a flow cannot proceed, with a diagnosis a user can act on."""

    def __init__(self, message: str, *, kind: str = "needs_authorization") -> None:
        """Record the diagnosis kind alongside the message."""
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectedResourceMetadata:
    """What a resource publishes about itself and its authorization servers."""

    resource: str
    authorization_servers: tuple[str, ...] = ()
    scopes_supported: tuple[str, ...] = ()
    bearer_methods_supported: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorizationServerMetadata:
    """The subset of authorization-server metadata this client uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    revocation_endpoint: str | None = None
    scopes_supported: tuple[str, ...] = ()
    response_types_supported: tuple[str, ...] = ("code",)
    grant_types_supported: tuple[str, ...] = ("authorization_code", "refresh_token")
    code_challenge_methods_supported: tuple[str, ...] = ("S256",)
    token_endpoint_auth_methods_supported: tuple[str, ...] = ("none",)
    #: Whether the server accepts a client ID metadata document.
    client_id_metadata_document_supported: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    def supports_pkce_s256(self) -> bool:
        """Whether the server accepts the only challenge method we will use."""
        return "S256" in self.code_challenge_methods_supported

    def allows_public_clients(self) -> bool:
        """Whether a client with no secret may use the token endpoint."""
        return "none" in self.token_endpoint_auth_methods_supported


def protected_resource_metadata_urls(resource_url: str) -> tuple[str, ...]:
    """Candidate metadata URLs for a resource, most specific first.

    RFC 9728 inserts the resource's path after the well-known segment, so a
    server hosted under ``/mcp`` publishes at
    ``/.well-known/oauth-protected-resource/mcp``. The root form is tried
    second because several implementations publish only there.
    """
    endpoint = normalize_url(resource_url)
    candidates: list[str] = []
    path = endpoint.path.rstrip("/")
    if path and path != "/":
        candidates.append(f"{endpoint.origin}{PROTECTED_RESOURCE_WELL_KNOWN}{path}")
    candidates.append(f"{endpoint.origin}{PROTECTED_RESOURCE_WELL_KNOWN}")
    return tuple(dict.fromkeys(candidates))


def authorization_server_metadata_urls(issuer: str) -> tuple[str, ...]:
    """Candidate metadata URLs for an issuer, most specific first.

    The issuer's own path is preserved, which is what RFC 8414 requires and
    what a multi-tenant issuer depends on: dropping it would fetch the
    tenant-less document and produce a different ``issuer`` than the one
    discovered, which the specification says must be refused.
    """
    parts = urlsplit(issuer)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    return (
        f"{base}{AS_METADATA_WELL_KNOWN}{path}",
        f"{base}{OIDC_METADATA_WELL_KNOWN}{path}",
    )


def parse_protected_resource_metadata(
    payload: Mapping[str, Any], *, expect: str
) -> ProtectedResourceMetadata:
    """Read a protected-resource document, checking it describes ``expect``."""
    resource = str(payload.get("resource") or "").strip()
    if not resource:
        raise OAuthError("the resource metadata does not name a resource")
    if not _same_resource(resource, expect):
        raise OAuthError(f"the metadata at this address describes {resource!r}, not {expect!r}")
    servers = payload.get("authorization_servers")
    return ProtectedResourceMetadata(
        resource=resource,
        authorization_servers=tuple(str(item) for item in servers)
        if isinstance(servers, list)
        else (),
        scopes_supported=_string_tuple(payload.get("scopes_supported")),
        bearer_methods_supported=_string_tuple(payload.get("bearer_methods_supported")),
        raw=dict(payload),
    )


def parse_authorization_server_metadata(
    payload: Mapping[str, Any], *, expected_issuer: str
) -> AuthorizationServerMetadata:
    """Read an authorization-server document, checking its issuer matches.

    The issuer check is not pedantry: it is the defence against a metadata
    document that redirects the whole flow to an attacker's endpoints, which
    the specification calls out and which costs one comparison to prevent.
    """
    issuer = str(payload.get("issuer") or "").strip().rstrip("/")
    if not issuer:
        raise OAuthError("the authorization server metadata does not name an issuer")
    if issuer != expected_issuer.rstrip("/"):
        raise OAuthError(
            f"the metadata at this address names issuer {issuer!r}, not {expected_issuer!r}"
        )
    authorization_endpoint = str(payload.get("authorization_endpoint") or "")
    token_endpoint = str(payload.get("token_endpoint") or "")
    if not authorization_endpoint or not token_endpoint:
        raise OAuthError(
            "the authorization server metadata is missing an authorization or token endpoint"
        )
    return AuthorizationServerMetadata(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=_optional_string(payload.get("registration_endpoint")),
        revocation_endpoint=_optional_string(payload.get("revocation_endpoint")),
        scopes_supported=_string_tuple(payload.get("scopes_supported")),
        response_types_supported=_string_tuple(payload.get("response_types_supported"))
        or ("code",),
        grant_types_supported=_string_tuple(payload.get("grant_types_supported"))
        or ("authorization_code",),
        code_challenge_methods_supported=_string_tuple(
            payload.get("code_challenge_methods_supported")
        )
        or ("plain",),
        token_endpoint_auth_methods_supported=_string_tuple(
            payload.get("token_endpoint_auth_methods_supported")
        )
        or ("client_secret_basic",),
        client_id_metadata_document_supported=bool(
            payload.get("client_id_metadata_document_supported")
        ),
        raw=dict(payload),
    )


def resource_metadata_from_challenge(header: str | None) -> str | None:
    """Extract ``resource_metadata`` from a ``WWW-Authenticate`` header.

    The header is the authoritative pointer when present, because it is how a
    server tells us where its metadata lives when the well-known location is
    not it. Parsed leniently: the parameter may be quoted, and the header may
    carry several challenges.
    """
    if not header:
        return None
    for match in _challenge_parameters(header):
        if match[0].lower() == "resource_metadata" and match[1]:
            return match[1]
    return None


def _challenge_parameters(header: str) -> list[tuple[str, str]]:
    """Split an authentication header into its ``key="value"`` parameters."""
    import re

    return [
        (name, value)
        for name, value in re.findall(r'([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*"([^"]*)"', header)
    ]


def _same_resource(left: str, right: str) -> bool:
    """Compare two resource identifiers by origin and path, ignoring case."""
    try:
        return normalize_url(left).url.rstrip("/") == normalize_url(right).url.rstrip("/")
    except EndpointRejected:
        return False


def _string_tuple(raw: Any) -> tuple[str, ...]:
    """Coerce a metadata list into a tuple of strings."""
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if isinstance(item, (str, int, float)))


def _optional_string(raw: Any) -> str | None:
    """Coerce an optional metadata field into a stripped string."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientIdentity:
    """Who the platform is, as far as the authorization server is concerned."""

    client_id: str
    registration: RegistrationKind
    #: Present only for a confidential client. It never leaves the platform.
    client_secret: str | None = None
    auth_method: str = "none"
    #: Whether the registration must be deleted when the connection is
    #: disconnected. A deployment-wide pre-registered client must not be.
    delete_on_disconnect: bool = False


def choose_registration(
    metadata: AuthorizationServerMetadata,
    *,
    preregistered_client_id: str | None = None,
    preregistered_client_secret: str | None = None,
    cimd_client_id: str | None = None,
) -> ClientIdentity:
    """Pick how to register, in the order the design specifies.

    Pre-registration wins because it needs no trust decision at connect time;
    a metadata document is next because it needs none either, while dynamic
    registration makes the authorization server create state on our behalf and
    is therefore last.
    """
    if preregistered_client_id:
        method = "client_secret_basic" if preregistered_client_secret else "none"
        return ClientIdentity(
            client_id=preregistered_client_id,
            registration="preregistered",
            client_secret=preregistered_client_secret,
            auth_method=method,
            delete_on_disconnect=False,
        )
    if metadata.client_id_metadata_document_supported and cimd_client_id:
        return ClientIdentity(
            client_id=cimd_client_id,
            registration="cimd",
            auth_method="none",
            delete_on_disconnect=False,
        )
    if metadata.registration_endpoint:
        if not metadata.allows_public_clients():
            raise OAuthError(
                "this server requires a registered client secret and does not allow "
                "automatic registration. Provide a client id and secret for it.",
                kind="client_registration_required",
            )
        return ClientIdentity(
            client_id="",
            registration="dcr",
            auth_method="none",
            delete_on_disconnect=True,
        )
    raise OAuthError(
        "this server does not support automatic client registration. Provide a client id "
        "for it in the connection's auth settings.",
        kind="client_registration_required",
    )


def registration_request_body(
    *,
    client_name: str,
    redirect_uris: Sequence[str],
    scopes: Sequence[str],
    grant_types: Sequence[str] = ("authorization_code", "refresh_token"),
) -> dict[str, Any]:
    """The body for a dynamic client registration request."""
    return {
        "client_name": client_name,
        "redirect_uris": list(redirect_uris),
        "grant_types": list(grant_types),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(scopes),
    }


def parse_registration_response(payload: Mapping[str, Any]) -> ClientIdentity:
    """Read a dynamic registration response."""
    client_id = str(payload.get("client_id") or "")
    if not client_id:
        raise OAuthError("the registration response did not contain a client id")
    secret = payload.get("client_secret")
    method = str(
        payload.get("token_endpoint_auth_method")
        or ("none" if not secret else "client_secret_basic")
    )
    return ClientIdentity(
        client_id=client_id,
        registration="dcr",
        client_secret=str(secret) if secret else None,
        auth_method=method,
        delete_on_disconnect=True,
    )


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PkcePair:
    """A PKCE verifier and its S256 challenge."""

    verifier: str
    challenge: str
    method: str = "S256"

    @classmethod
    def generate(cls) -> PkcePair:
        """Create a fresh pair from a high-entropy verifier."""
        verifier = secrets.token_urlsafe(64)[:128]
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return cls(verifier=verifier, challenge=challenge)


def new_state() -> str:
    """A single-use, high-entropy state value."""
    return secrets.token_urlsafe(32)


def state_hash(state: str) -> str:
    """The stored form of a state value.

    Hashed rather than stored verbatim so that a database read does not hand
    out a value that can be replayed into the callback endpoint.
    """
    return hashlib.sha256(state.encode()).hexdigest()


@dataclass(frozen=True)
class FlowVersions:
    """The versions a flow was started under, checked again on the callback."""

    connection_id: str
    config_revision: int
    authorization_epoch: int
    oauth_generation: int

    def as_dict(self) -> dict[str, Any]:
        """Render for storage."""
        return {
            "connection_id": self.connection_id,
            "config_revision": self.config_revision,
            "authorization_epoch": self.authorization_epoch,
            "oauth_generation": self.oauth_generation,
        }

    def matches(self, connection: Mapping[str, Any]) -> bool:
        """Whether a connection row still carries every version this flow saw."""
        return (
            str(connection.get("id") or "") == self.connection_id
            and int(connection.get("config_revision") or 0) == self.config_revision
            and int(connection.get("authorization_epoch") or 0) == self.authorization_epoch
            and int(connection.get("oauth_generation") or 0) == self.oauth_generation
        )


@dataclass(frozen=True)
class OAuthFlow:
    """One started authorization, from the redirect to the token exchange."""

    versions: FlowVersions
    state: str
    pkce: PkcePair
    client: ClientIdentity
    metadata: AuthorizationServerMetadata
    resource: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()
    created_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def expired(self) -> bool:
        """Whether this flow may no longer be completed."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    def authorization_url(self) -> str:
        """Build the URL the user's browser is sent to."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client.client_id,
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_challenge": self.pkce.challenge,
            "code_challenge_method": self.pkce.method,
        }
        if self.scopes:
            params["scope"] = " ".join(self.scopes)
        # RFC 8707: the token is minted for a named resource, which is what
        # stops one server's token being replayed against another's endpoint.
        params["resource"] = self.resource
        separator = "&" if urlsplit(self.metadata.authorization_endpoint).query else "?"
        return f"{self.metadata.authorization_endpoint}{separator}{urlencode(params)}"

    def token_request(self, code: str) -> TokenRequest:
        """Build the code exchange for a callback."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client.client_id,
            "code_verifier": self.pkce.verifier,
            "resource": self.resource,
        }
        return TokenRequest(
            url=self.metadata.token_endpoint,
            data=data,
            client=self.client,
        )


@dataclass(frozen=True)
class TokenRequest:
    """One token-endpoint request, with the client authentication it needs."""

    url: str
    data: Mapping[str, str]
    client: ClientIdentity

    def form(self) -> dict[str, str]:
        """The form body, with client authentication applied.

        Which mechanism is used is what the authorization server *said* it
        supports, not what this platform would prefer. The two are alternatives
        under the specification, not a choice the client gets to make twice: a
        server that lists ``client_secret_basic`` and receives a secret in the
        body rejects the exchange, and sending both is not a way round it.
        """
        body = {key: str(value) for key, value in self.data.items()}
        if self.client.client_secret and not self.uses_basic_auth():
            body["client_secret"] = self.client.client_secret
        return body

    def uses_basic_auth(self) -> bool:
        """Whether this client authenticates with an ``Authorization`` header."""
        return bool(self.client.client_secret) and self.client.auth_method == "client_secret_basic"

    def headers(self) -> dict[str, str]:
        """The request headers, including Basic credentials when negotiated."""
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        }
        if self.uses_basic_auth():
            # Form-encoded, per RFC 6749 section 2.3.1: the id and secret are
            # percent-encoded *before* being joined and base64-ed, which is not
            # what a naive f-string produces for a secret containing a colon.
            credentials = f"{quote(self.client.client_id, safe='')}:{quote(self.client.client_secret or '', safe='')}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers["authorization"] = f"Basic {encoded}"
        return headers


@dataclass(frozen=True)
class TokenSet:
    """What a token response gave us, and what it implies for the next refresh."""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    token_type: str = "Bearer"

    @property
    def expires_soon(self) -> bool:
        """Whether the token is inside the refresh margin."""
        return self.needs_refresh()

    def needs_refresh(
        self, *, margin: timedelta = REFRESH_MARGIN, now: datetime | None = None
    ) -> bool:
        """Whether this token should be refreshed before the next use."""
        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        return self.expires_at - margin <= current


def parse_token_response(payload: Mapping[str, Any], *, now: datetime | None = None) -> TokenSet:
    """Read a token response into the shape the credential row stores."""
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise OAuthError("the token response did not contain an access token")
    expires_in = payload.get("expires_in")
    expires_at: datetime | None = None
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        current = now or datetime.now(timezone.utc)
        expires_at = current + timedelta(seconds=float(expires_in))
    refresh = payload.get("refresh_token")
    scope = payload.get("scope")
    scopes = tuple(str(scope).split()) if isinstance(scope, str) and scope.strip() else ()
    return TokenSet(
        access_token=access_token,
        refresh_token=str(refresh) if refresh else None,
        expires_at=expires_at,
        scopes=scopes,
        token_type=str(payload.get("token_type") or "Bearer"),
    )


def refresh_request(
    *,
    metadata: AuthorizationServerMetadata,
    client: ClientIdentity,
    refresh_token: str,
    resource: str,
    scopes: Sequence[str] = (),
) -> TokenRequest:
    """Build a refresh request for one connection."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client.client_id,
        "resource": resource,
    }
    if scopes:
        data["scope"] = " ".join(scopes)
    return TokenRequest(url=metadata.token_endpoint, data=data, client=client)


def apply_refresh(
    previous: Mapping[str, Any],
    response: TokenSet,
    *,
    rotates: bool,
) -> dict[str, Any]:
    """Merge a refresh response into the credential that should be stored.

    The only interesting decision is the refresh token. A response that omits
    one means "keep using the old one" for an issuer that does not rotate, and
    for an issuer that does it means the response is incomplete -- keeping the
    old token there would look like it worked until the next refresh, so the
    connection is reported as needing re-authorization instead.

    Whether the issuer rotates is therefore a fact about what it has *done*,
    not about whether it sent something -- and it is not a promise about the
    next response either. RFC 6749 §6 lets an issuer issue a new refresh token
    on one refresh and omit the field on another; the omission means "keep
    using the one you have", not "the one you have is dead". So the flag
    records the observation an omission cannot undo (a *different* token came
    back at least once) and nothing is refused because a field is missing: an
    issuer that really did invalidate the stored token says so at the next
    refresh, where `invalid_grant` is a refusal the owner can act on. Inferring
    it from an omission instead reconnects working connections.
    """
    merged: dict[str, Any] = {
        "access_token": response.access_token,
        "expires_at": response.expires_at,
    }
    rotated = bool(response.refresh_token) and response.refresh_token != previous.get(
        "refresh_token"
    )
    if response.refresh_token:
        merged["refresh_token"] = response.refresh_token
    else:
        merged["refresh_token"] = previous.get("refresh_token")
    merged["refresh_rotates"] = rotates or rotated
    if response.scopes:
        merged["scopes"] = list(response.scopes)
    merged["issuer"] = previous.get("issuer")
    merged["resource"] = previous.get("resource")
    return merged


def revocation_request(
    metadata: AuthorizationServerMetadata,
    client: ClientIdentity,
    token: str,
    *,
    token_type_hint: str = "refresh_token",
) -> tuple[str, dict[str, str]] | None:
    """Build a revocation call, or ``None`` when the server does not offer one.

    Revocation is best-effort by design: a server without the endpoint gets its
    token dropped locally, and the connection is reported as disconnected
    either way. Pretending otherwise would leave a user believing a credential
    was destroyed when it was not.
    """
    if not metadata.revocation_endpoint:
        return None
    body = {"token": token, "token_type_hint": token_type_hint, "client_id": client.client_id}
    if client.client_secret:
        body["client_secret"] = client.client_secret
    return metadata.revocation_endpoint, body


def build_profile(flow: OAuthFlow, client: ClientIdentity) -> OAuthProfile:
    """Record the settings a later refresh needs, from a completed flow."""
    return OAuthProfile(
        issuer=flow.metadata.issuer,
        resource=flow.resource,
        scopes=flow.scopes,
        client_id=client.client_id,
        client_auth_method=client.auth_method,
        registration=client.registration,
    )


def default_scopes(
    resource_metadata: ProtectedResourceMetadata | None,
    server_metadata: AuthorizationServerMetadata,
    requested: Sequence[str] = (),
) -> tuple[str, ...]:
    """Choose the scopes to ask for.

    An explicit request from the connection wins; otherwise the resource's own
    advertisement, then the authorization server's. Asking for nothing is a
    valid outcome -- several servers treat an absent ``scope`` as "the default
    set", and inventing one would over-ask.
    """
    if requested:
        return tuple(requested)
    if resource_metadata and resource_metadata.scopes_supported:
        return resource_metadata.scopes_supported
    return server_metadata.scopes_supported


@runtime_checkable
class MetadataFetcher(Protocol):
    """Fetches and decodes one metadata document.

    Injected so that the flow logic above can be tested without a network, and
    so the egress policy is applied by the same code path that dials the MCP
    endpoint rather than by a second, weaker one.
    """

    async def fetch_json(
        self,
        url: str,
        *,
        policy: EgressPolicy,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        """Return the decoded document and its response headers."""
        ...


async def discover_authorization_server(
    fetcher: MetadataFetcher,
    *,
    resource_url: str,
    policy: EgressPolicy,
    challenge_header: str | None = None,
    preregistered_issuer: str | None = None,
) -> tuple[ProtectedResourceMetadata | None, AuthorizationServerMetadata]:
    """Find the authorization server for one MCP endpoint.

    ``preregistered_issuer`` short-circuits discovery the same way a
    pre-registered client does: when the deployment already knows the issuer,
    fetching a metadata document to learn it is a round trip that can only
    introduce a different answer.
    """
    resource_metadata: ProtectedResourceMetadata | None = None
    issuer: str | None = preregistered_issuer
    if issuer is None:
        candidates: list[str] = []
        from_challenge = resource_metadata_from_challenge(challenge_header)
        if from_challenge:
            candidates.append(from_challenge)
        candidates.extend(protected_resource_metadata_urls(resource_url))
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                validate_endpoint(candidate, policy)
                payload, _ = await fetcher.fetch_json(candidate, policy=policy)
                resource_metadata = parse_protected_resource_metadata(payload, expect=resource_url)
            except (OAuthError, EndpointRejected, RuntimeError) as exc:
                last_error = exc
                continue
            if not resource_metadata.authorization_servers:
                raise OAuthError(
                    "this server publishes resource metadata but names no authorization server"
                )
            issuer = resource_metadata.authorization_servers[0]
            break
        if issuer is None:
            raise OAuthError(
                "could not find this server's authorization server metadata"
                + (f": {last_error}" if last_error else ""),
                kind="protocol_incompatible",
            )

    last_error = None
    for candidate in authorization_server_metadata_urls(issuer):
        try:
            validate_endpoint(candidate, policy)
            payload, _ = await fetcher.fetch_json(candidate, policy=policy)
            metadata = parse_authorization_server_metadata(payload, expected_issuer=issuer)
        except (OAuthError, EndpointRejected, RuntimeError) as exc:
            last_error = exc
            continue
        if not metadata.supports_pkce_s256():
            raise OAuthError(
                "this authorization server does not accept S256 PKCE, which this platform "
                "requires for a browser-based flow",
                kind="protocol_incompatible",
            )
        return resource_metadata, metadata
    raise OAuthError(
        f"could not read the authorization server metadata for {issuer}"
        + (f": {last_error}" if last_error else ""),
        kind="protocol_incompatible",
    )


def client_metadata_document(
    *, client_id: str, client_name: str, redirect_uris: Sequence[str]
) -> str:
    """Serialize a client ID metadata document.

    Served by the platform at the URL that doubles as the client id, which is
    what the mechanism is: the authorization server fetches this to learn who
    is asking, instead of registering them first.
    """
    return json.dumps(
        {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": list(redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        sort_keys=True,
    )


def redirect_uri(base_url: str, provider: str) -> str:
    """The exact callback one connection will use.

    ``provider`` is whatever this deployment puts in the path to tell its
    callbacks apart; the state parameter is what identifies the flow, because a
    callback arrives before anything else is known about it. Whatever the value,
    the registration request has to carry *this* string: an authorization server
    compares the ``redirect_uri`` it is asked for against the one registered for
    the client, and refuses one that differs.
    """
    return f"{base_url.rstrip('/')}/v1/agent/mcp/oauth/callback/{provider}"


def default_auth_profile() -> AuthProfile:
    """The profile a freshly imported HTTP connection starts with."""
    return AuthProfile(kind=AuthKind.NONE)


__all__ = [
    "AS_METADATA_WELL_KNOWN",
    "FLOW_LIFETIME",
    "OIDC_METADATA_WELL_KNOWN",
    "PROTECTED_RESOURCE_WELL_KNOWN",
    "REFRESH_MARGIN",
    "AuthorizationServerMetadata",
    "ClientIdentity",
    "FlowVersions",
    "MetadataFetcher",
    "OAuthError",
    "OAuthFlow",
    "PkcePair",
    "ProtectedResourceMetadata",
    "RegistrationKind",
    "TokenRequest",
    "TokenSet",
    "apply_refresh",
    "authorization_server_metadata_urls",
    "build_profile",
    "choose_registration",
    "client_metadata_document",
    "default_auth_profile",
    "default_scopes",
    "discover_authorization_server",
    "new_state",
    "parse_authorization_server_metadata",
    "parse_protected_resource_metadata",
    "parse_registration_response",
    "parse_token_response",
    "protected_resource_metadata_urls",
    "redirect_uri",
    "refresh_request",
    "registration_request_body",
    "resource_metadata_from_challenge",
    "revocation_request",
    "state_hash",
]
