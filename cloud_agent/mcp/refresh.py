"""Keeping a stored credential usable without asking the user again.

An access token expires while nobody is watching. The connection is still
valid, the refresh token still works, and the only thing that has to happen is
one token-endpoint call -- which must not happen twice, must not be lost when a
response omits a rotated token, and must not race a disconnect.

Three rules, and each is a failure the naive version has:

**One refresher, holding a lease.** A claim that only bumped the credential
version was not exclusive: the bump is a write, so the next worker to read the
row claims it while the first is still on the network with the same refresh
token. An authorization server that rotates tokens invalidates the first one,
and the second call destroys the connection it was trying to save. The claim is
now a lease with a generation -- taken only when no live lease is held, fenced
on the way back in -- so the second worker is told "somebody else is refreshing"
instead of issuing a second call, and a refresher that dies mid-flight leaves a
lease that expires rather than a lock.

**The rotation contract decides what is kept.** Whether an issuer rotates is
recorded on the connection, and a response that omits a refresh token means
"keep the old one" only when the issuer does not rotate. Keeping a
rotated-away token looks like success until the next refresh, hours later.

**A disconnect wins.** The write is conditional on the authorization epoch the
refresh saw, so a refresh that started before the owner disconnected cannot
restore the credential they removed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp import oauth
from cloud_agent.mcp.models import AuthKind, ConnectionErrorKind
from cloud_agent.mcp.net_policy import EgressPolicy
from cloud_agent.mcp.transports import HttpMetadataFetcher, TransportError
from cloud_agent.source_control import SourceControlCipher, SourceControlError

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

logger = get_logger(__name__)

#: Refresh this long before expiry, so a call that starts just before the
#: boundary does not fail just after it. A tool call can take minutes.
REFRESH_MARGIN_SECONDS = 120

#: How long one worker may hold the exclusive claim on a refresh.
#:
#: Comfortably longer than the token request's own 20-second timeout, because a
#: lease that expires while its holder is still waiting would let a second
#: caller spend the same refresh token -- the race the lease exists to prevent.
#: Short enough that a worker which died mid-flight blocks the connection for a
#: minute rather than until somebody notices.
REFRESH_LEASE_SECONDS = 60.0


class RefreshRefused(RuntimeError):
    """Raised when a credential cannot be refreshed, with a reason to show."""

    def __init__(self, message: str, *, kind: str = "needs_authorization") -> None:
        """Record the diagnosis kind alongside the message."""
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class RefreshOutcome:
    """What a refresh attempt did, whether or not it was the one that ran."""

    refreshed: bool
    #: True when another worker held the claim. Not an error: the caller should
    #: re-read the row and use whatever is there now.
    contended: bool = False
    expires_at: datetime | None = None


def _expiry_is_near(row: Mapping[str, Any], *, margin_seconds: int) -> bool:
    """Whether a stored credential expires inside the refresh margin."""
    expires_at = row.get("expires_at")
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    from datetime import timedelta

    return expires_at - timedelta(seconds=margin_seconds) <= datetime.now(timezone.utc)


def can_refresh(row: Mapping[str, Any]) -> bool:
    """Whether this connection has everything a refresh needs."""
    return (
        str(row.get("auth_kind")) == AuthKind.OAUTH.value
        and bool(row.get("refresh_token_ciphertext"))
        and bool(row.get("issuer"))
        and bool(row.get("resource"))
        and bool(row.get("token_endpoint") or row.get("issuer"))
    )


def should_refresh(row: Mapping[str, Any], *, margin_seconds: int = REFRESH_MARGIN_SECONDS) -> bool:
    """Whether this connection's credential should be renewed before use."""
    if not can_refresh(row):
        return False
    return _expiry_is_near(row, margin_seconds=margin_seconds)


async def refresh_credential(
    store: AgentJobStore,
    *,
    row: Mapping[str, Any],
    policy: EgressPolicy | None = None,
    cipher: SourceControlCipher | None = None,
    fetcher: HttpMetadataFetcher | None = None,
) -> RefreshOutcome:
    """Refresh one connection's credential if it is close to expiring.

    Returns without refreshing when the credential is still comfortably valid,
    so a caller can call this before every use rather than reasoning about
    when. Raises :class:`RefreshRefused` only when a refresh was *required* and
    did not happen.
    """
    if not can_refresh(row):
        if _expiry_is_near(row, margin_seconds=0):
            raise RefreshRefused("this connection has expired; connect it again")
        return RefreshOutcome(refreshed=False)
    if not should_refresh(row):
        return RefreshOutcome(refreshed=False, expires_at=row.get("expires_at"))

    credential_cipher = cipher
    if credential_cipher is None:
        try:
            from cloud_agent.mcp.registry import mcp_cipher

            credential_cipher = mcp_cipher()
        except SourceControlError as exc:
            raise RefreshRefused(
                "this deployment has no MCP encryption key, so a credential cannot be read",
                kind="needs_authorization",
            ) from exc

    claimed = await store.claim_mcp_refresh(
        connection_id=int(row["id"]),
        expected_credential_version=int(row.get("credential_version") or 1),
        lease_seconds=REFRESH_LEASE_SECONDS,
    )
    if claimed is None:
        # Somebody else is doing it, or the connection moved. Reporting rather
        # than retrying: a second call with a rotating refresh token would
        # invalidate theirs. The credential this caller read is near expiry,
        # not past it -- that is what the margin above buys -- so the caller
        # re-reads the row and uses what it finds there.
        return RefreshOutcome(refreshed=False, contended=True)

    try:
        token_request = _build_request(claimed, cipher=credential_cipher)
        payload = await _post(token_request, policy=policy or EgressPolicy(), fetcher=fetcher)
        tokens = oauth.parse_token_response(payload)
        merged = oauth.apply_refresh(
            {
                "refresh_token": _decrypt(
                    credential_cipher, claimed.get("refresh_token_ciphertext")
                ),
                "issuer": claimed.get("issuer"),
                "resource": claimed.get("resource"),
            },
            tokens,
            rotates=bool(claimed.get("refresh_rotates")),
        )
    except RefreshRefused:
        # Refused before anything was sent -- no recorded token endpoint, or an
        # unreadable credential. The lease is released so the next call retries,
        # rather than waiting out a timer for an attempt that never happened.
        await _release(store, claimed)
        raise
    except (TransportError, oauth.OAuthError) as exc:
        await _release(store, claimed)
        logger.warning(
            "agent_mcp_refresh_failed",
            extra={"event": "agent_mcp_refresh_failed", "connection_id": claimed["id"]},
        )
        # A rotating issuer that sent no new refresh token lands here too.
        # Converted so a caller sees one refusal type rather than two, and so
        # nothing is written: keeping the old token would look like success
        # until the next refresh, hours later.
        raise RefreshRefused(f"this connection could not be refreshed: {exc}") from exc

    written = await store.set_mcp_credential(
        connection_id=int(claimed["id"]),
        expected_authorization_epoch=int(claimed["authorization_epoch"]),
        access_token_ciphertext=credential_cipher.encrypt(tokens.access_token),
        refresh_token_ciphertext=(
            credential_cipher.encrypt(str(merged["refresh_token"]))
            if merged.get("refresh_token")
            else None
        ),
        expires_at=tokens.expires_at,
        scopes=list(tokens.scopes) or None,
        issuer=claimed.get("issuer"),
        resource=claimed.get("resource"),
        refresh_rotates=bool(merged.get("refresh_rotates")),
        clear_refresh=False,
        # The lease, not just the row: this answer was computed from a refresh
        # token that a superseding claim has since replaced. Without the fence a
        # slow refresher rewrites the credential its successor is replacing, and
        # the successor's own write is then the one that looks stale.
        expected_refresh_generation=int(claimed["refresh_generation"]),
        # The mirror of the callback fence. A refresh spends the same time on the
        # network as an authorization code exchange, and the owner can start a
        # new authorization inside that window -- which is a newer credential for
        # the same connection. Both writes then carry the epoch, because
        # re-authorizing is not a change of target, so the generation the flow
        # started under is what tells them apart.
        expected_oauth_generation=int(claimed.get("oauth_generation") or 0),
        expected_config_revision=int(claimed.get("config_revision") or 1),
    )
    if not written:
        # The owner disconnected, re-authorized, or the lease expired and
        # somebody else took it. Either way this result is not the current one,
        # and releasing is fenced on the generation so it cannot clear a live
        # successor's lease.
        await _release(store, claimed)
        raise RefreshRefused(
            "this connection moved while its credential was refreshing -- "
            "disconnected, re-authorized, or refreshed by another worker -- "
            "so the credential this attempt obtained was not written"
        )
    logger.info(
        "agent_mcp_refreshed",
        extra={"event": "agent_mcp_refreshed", "connection_id": claimed["id"]},
    )
    return RefreshOutcome(refreshed=True, expires_at=tokens.expires_at)


async def _release(store: AgentJobStore, claimed: Mapping[str, Any]) -> None:
    """Drop a lease this call still holds, best effort.

    Best effort on purpose: the caller is already on its way to a refusal, and
    failing it over the bookkeeping would replace a diagnosis the owner can act
    on with one they cannot. An unreleased lease is not lost work either -- it
    expires.
    """
    try:
        await store.release_mcp_refresh_claim(
            connection_id=int(claimed["id"]),
            refresh_generation=int(claimed["refresh_generation"]),
        )
    except Exception:  # pragma: no cover - defensive, the lease expires anyway
        logger.warning(
            "agent_mcp_refresh_release_failed",
            extra={"event": "agent_mcp_refresh_release_failed", "connection_id": claimed["id"]},
            exc_info=True,
        )


async def ensure_fresh(
    store: AgentJobStore,
    *,
    row: Mapping[str, Any],
    policy: EgressPolicy | None = None,
) -> Mapping[str, Any]:
    """Return the row to use, refreshing its credential first if it is due.

    The one entry point every caller should use. A call site that read a row and
    used it directly would work until a token aged out, then fail with a 401
    whose cause is three layers away from the code that skipped this.
    """
    if not should_refresh(row):
        return row
    outcome = await refresh_credential(store, row=row, policy=policy)
    if outcome.contended:
        # Another instance holds the lease. Re-read rather than wait: whatever is
        # there now is at least as fresh as what this caller held, and the row
        # this caller read was inside the refresh *margin* -- near expiry, not
        # past it -- so the credential it already has is still usable for the
        # call that is about to be made. Blocking on the lease would trade that
        # for a wait as long as the lease itself.
        refreshed = await store.get_mcp_connection(connection_id=int(row["id"]))
        return refreshed or row
    if not outcome.refreshed:
        return row
    refreshed = await store.get_mcp_connection(connection_id=int(row["id"]))
    return refreshed or row


def _build_request(row: Mapping[str, Any], *, cipher: SourceControlCipher) -> oauth.TokenRequest:
    """Build the refresh request from the stored issuer and client settings."""
    metadata = oauth.AuthorizationServerMetadata(
        issuer=str(row["issuer"]),
        authorization_endpoint=str(row.get("authorization_endpoint") or row["issuer"]),
        token_endpoint=str(row["token_endpoint"]) if row.get("token_endpoint") else "",
        revocation_endpoint=row.get("revocation_endpoint"),
        registration_endpoint=row.get("registration_endpoint"),
    )
    if not metadata.token_endpoint:
        # Without a recorded token endpoint the issuer must be re-discovered,
        # which is a network round trip the caller did not ask for. Reported
        # instead of guessed: a token endpoint built from the issuer's origin
        # is right often enough to be dangerous.
        raise RefreshRefused(
            "this connection has no recorded token endpoint; reconnect it",
            kind="protocol_incompatible",
        )
    config = row.get("auth_config")
    settings = config if isinstance(config, Mapping) else {}
    identity = oauth.ClientIdentity(
        client_id=str(settings.get("client_id") or ""),
        registration=str(row.get("registration") or "dcr"),  # type: ignore[arg-type]
        # A different key domain from the tokens below. The client secret is
        # written by the authorization flow under the OAuth cipher -- it is the
        # secret this deployment holds *for the authorization server* -- while
        # the access and refresh tokens are written under the MCP one. Reading
        # both with a single cipher fails for every confidential client, and it
        # fails at decryption, so it looks like a corrupt credential rather than
        # a wrong key.
        client_secret=_decrypt(_client_secret_cipher(cipher), row.get("client_secret_ciphertext")),
        auth_method=str(row.get("client_auth_method") or "none"),
    )
    return oauth.refresh_request(
        metadata=metadata,
        client=identity,
        refresh_token=_decrypt(cipher, row.get("refresh_token_ciphertext")) or "",
        resource=str(row["resource"]),
        scopes=list(row.get("scopes") or ()),
    )


def _client_secret_cipher(cipher: SourceControlCipher | None) -> SourceControlCipher | None:
    """The cipher a connection's client secret was written under.

    Separate from the token cipher by design and not by accident: the two are
    different credential kinds and the deployment wraps them under different
    domains so that one cannot be read as the other. Returns ``None`` when the
    deployment has no key, which leaves the secret unread rather than raising
    here -- the caller decides whether a client secret was required.
    """
    if cipher is not None:
        return cipher
    try:
        from cloud_agent.mcp.registry import oauth_cipher

        return oauth_cipher()
    except SourceControlError:
        return None


def _decrypt(cipher: SourceControlCipher | None, value: Any) -> str | None:
    """Decrypt one stored secret, or ``None`` when there is nothing to read."""
    if not value:
        return None
    try:
        return cipher.decrypt(str(value))
    except SourceControlError as exc:
        raise RefreshRefused(
            "this connection's stored credential could not be read; reconnect it"
        ) from exc


async def _post(
    request: oauth.TokenRequest,
    *,
    policy: EgressPolicy,
    fetcher: HttpMetadataFetcher | None,
) -> Mapping[str, Any]:
    """Perform the token request under the same egress policy as everything else."""
    del fetcher  # Reserved for a caller that wants to inject a client.
    import httpx

    from cloud_agent.mcp.net_policy import check_addresses, validate_endpoint

    endpoint = validate_endpoint(request.url, policy)
    import asyncio

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(endpoint.host, endpoint.port, type=0)
    check_addresses(endpoint.host, {info[4][0] for info in infos}, policy)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
        response = await client.post(endpoint.url, data=request.form(), headers=request.headers())
        if response.status_code >= 400:
            raise RefreshRefused(
                f"the token endpoint answered {response.status_code}",
                kind=ConnectionErrorKind.NEEDS_AUTHORIZATION.value,
            )
        document = response.json()
    if not isinstance(document, Mapping):
        raise RefreshRefused("the token response is not an object")
    return document


__all__ = [
    "REFRESH_LEASE_SECONDS",
    "REFRESH_MARGIN_SECONDS",
    "RefreshOutcome",
    "RefreshRefused",
    "can_refresh",
    "ensure_fresh",
    "refresh_credential",
    "should_refresh",
]
