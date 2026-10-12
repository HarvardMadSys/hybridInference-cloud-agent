"""OAuth helpers for user-owned source-control connections.

GitHub remains authoritative through its App installations. GitLab is a
read-only discovery connection: it verifies the user and enumerates projects,
but it is deliberately not an Agent job target until the checkout and merge
request publishing lifecycle supports GitLab end to end.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

from cloud_agent.settings import number_setting, settings_environment

if TYPE_CHECKING:
    from cloud_agent.ports import SourceControlRepository

GITLAB_ORIGIN = "https://gitlab.com"
OAUTH_STATE_TTL = timedelta(minutes=10)
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
_REFRESH_LEASE_DB_MARGIN_SECONDS = 30
_GITLAB_REFRESH_WAIT_ATTEMPTS = 10
_GITLAB_REFRESH_WAIT_INTERVAL_S = 0.3


def _refresh_lease_seconds(request_timeout_s: float) -> int:
    """Keep a refresh lease live through provider I/O and its fenced write."""
    return max(1, math.ceil(request_timeout_s + _REFRESH_LEASE_DB_MARGIN_SECONDS))


#: Wraps stored provider credentials. Its own secret, not the gateway's — the
#: source derived this key from ``API_KEY_SECRET``, which meant the gateway
#: could decrypt this service's GitHub and GitLab tokens.
#:
#: **Re-keying does not re-encrypt.** Rows written under the old key cannot be
#: read under this one, so migrating them (task H1) is a decrypt-with-old,
#: encrypt-with-new step rather than a copy. Copying the ciphertext produces a
#: connection that looks present and fails at first use.
ENV_ENCRYPTION_KEY = "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY"


class SourceControlError(Exception):
    """A safe-to-display source-control integration failure."""


class OAuthStateError(SourceControlError):
    """An OAuth state is missing, expired, replayed, or owned by another user."""


#: Domain separator for source-control credentials. Any other kind of
#: user-owned credential this service stores derives its key under a different
#: domain from the *same* secret: one key to deploy and rotate, while a
#: ciphertext from one table cannot be decrypted — or planted — as another's.
SOURCE_CONTROL_DOMAIN = b"hybridinference/source-control/v1"


class SourceControlCipher:
    """Encrypt provider secrets with a domain-separated server key."""

    def __init__(self, secret: str | None = None, *, domain: bytes = SOURCE_CONTROL_DOMAIN) -> None:
        from cloud_agent.settings import setting_env

        material = secret if secret is not None else setting_env(ENV_ENCRYPTION_KEY, "")
        if not material:
            raise SourceControlError(
                f"{ENV_ENCRYPTION_KEY} must be set before source-control credentials can be stored"
            )
        digest = hashlib.sha256(domain + b"\0" + material.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        """Return authenticated ciphertext; plaintext is never persisted."""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        """Decrypt a persisted secret and fail closed on corruption."""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise SourceControlError(
                "stored source-control credential could not be decrypted"
            ) from exc


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()


async def issue_oauth_state(
    store: SourceControlRepository,
    *,
    user_id: str,
    provider: str,
    cipher: SourceControlCipher | None = None,
    code_verifier: str | None = None,
) -> str:
    """Persist a short-lived, single-use state bound to user and provider."""
    if code_verifier is not None and cipher is None:
        raise SourceControlError("an encryption key is required to store a PKCE verifier")
    state = secrets.token_urlsafe(32)
    await store.create_oauth_state(
        state_hash=_state_hash(state),
        user_id=user_id,
        provider=provider,
        code_verifier_ciphertext=(
            cipher.encrypt(code_verifier) if code_verifier and cipher is not None else None
        ),
        expires_at=datetime.now(timezone.utc) + OAUTH_STATE_TTL,
    )
    return state


async def consume_oauth_state(
    store: SourceControlRepository,
    *,
    state: str,
    user_id: str,
    provider: str,
    cipher: SourceControlCipher | None = None,
) -> str | None:
    """Atomically consume state; all invalid cases intentionally look alike."""
    if not state or len(state) > 512:
        raise OAuthStateError("The source-control authorization expired or is invalid.")
    found = await store.consume_oauth_state(
        state_hash=_state_hash(state), user_id=user_id, provider=provider
    )
    if found is None:
        raise OAuthStateError("The source-control authorization expired or is invalid.")
    encrypted_verifier = found.get("code_verifier_ciphertext")
    if not encrypted_verifier:
        return None
    if cipher is None:
        raise SourceControlError("an encryption key is required to read the PKCE verifier")
    return cipher.decrypt(encrypted_verifier)


def github_connect_configured(app_credentials: Any, env: dict[str, str] | None = None) -> bool:
    """Whether a user can connect their own GitHub on this deployment.

    The App, its OAuth half, and an installation URL to send people to. Short
    of all three there is nothing for a user to press, and the deployment's
    allowlist is the only way a repository becomes available.
    """
    source = env if env is not None else settings_environment()
    return (
        app_credentials is not None
        and bool((source.get("AGENT_GITHUB_APP_INSTALL_URL") or "").strip())
        and bool(getattr(app_credentials, "user_authorization_configured", True))
    )


def github_authorization_url(base_url: str, *, state: str) -> str:
    """Add state to an operator-configured GitHub authorization URL safely."""
    parsed = urlparse(base_url)
    try:
        invalid_port = parsed.port not in {None, 443}
    except ValueError:
        invalid_port = True
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.fragment
        or invalid_port
    ):
        raise SourceControlError("AGENT_GITHUB_APP_INSTALL_URL must be an https://github.com URL")
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "state"]
    query.append(("state", state))
    return urlunparse(parsed._replace(query=urlencode(query)))


@dataclass(frozen=True)
class GitLabOAuthConfig:
    """Server-owned GitLab.com OAuth application configuration."""

    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> GitLabOAuthConfig | None:
        """Load a complete, safe GitLab.com OAuth configuration or return None."""
        source = env if env is not None else settings_environment()
        client_id = (source.get("AGENT_GITLAB_OAUTH_CLIENT_ID") or "").strip()
        client_secret = (source.get("AGENT_GITLAB_OAUTH_CLIENT_SECRET") or "").strip()
        redirect_uri = (source.get("AGENT_GITLAB_OAUTH_REDIRECT_URI") or "").strip()
        if not any((client_id, client_secret, redirect_uri)):
            return None
        if not all((client_id, client_secret, redirect_uri)):
            raise SourceControlError(
                "GitLab OAuth requires client id, client secret, and redirect URI"
            )
        parsed = urlparse(redirect_uri)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if (
            not parsed.hostname
            or (parsed.scheme != "https" and not local_http)
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise SourceControlError(
                "AGENT_GITLAB_OAUTH_REDIRECT_URI must be HTTPS (HTTP is allowed only on localhost)"
            )
        return cls(client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri)


class GitLabOAuthClient:
    """GitLab.com OAuth and minimal read-only project-discovery client."""

    def __init__(
        self,
        config: GitLabOAuthConfig,
        *,
        cipher: SourceControlCipher,
        timeout_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.cipher = cipher
        self._timeout_override = timeout_s
        self._transport = transport

    async def authorization_url(self, store: SourceControlRepository, *, user_id: str) -> str:
        """Create a PKCE authorization URL backed by one-time server state."""
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        state = await issue_oauth_state(
            store,
            user_id=user_id,
            provider="gitlab",
            cipher=self.cipher,
            code_verifier=verifier,
        )
        return f"{GITLAB_ORIGIN}/oauth/authorize?{urlencode({'client_id': self.config.client_id, 'redirect_uri': self.config.redirect_uri, 'response_type': 'code', 'state': state, 'scope': 'read_user read_api', 'code_challenge': challenge.decode('ascii'), 'code_challenge_method': 'S256'})}"

    def _client(self, *, timeout_s: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.request_timeout_s if timeout_s is None else timeout_s,
            transport=self._transport,
            follow_redirects=False,
        )

    @property
    def request_timeout_s(self) -> float:
        """Effective deadline used for the next provider request."""
        if self._timeout_override is not None:
            return self._timeout_override
        return number_setting("gitlab_request_timeout_s", 20.0)

    @property
    def refresh_lease_seconds(self) -> int:
        """Lease long enough for provider I/O plus the fenced database write."""
        return _refresh_lease_seconds(self.request_timeout_s)

    @staticmethod
    def _require_success(response: httpx.Response, operation: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise SourceControlError(f"GitLab {operation} failed ({response.status_code})")
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceControlError(f"GitLab {operation} returned an invalid response") from exc
        if not isinstance(body, dict):
            raise SourceControlError(f"GitLab {operation} returned an invalid response")
        return body

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange a callback code using the same redirect URI and PKCE verifier."""
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{GITLAB_ORIGIN}/oauth/token",
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": self.config.redirect_uri,
                        "code_verifier": code_verifier,
                    },
                )
        except httpx.HTTPError as exc:
            raise SourceControlError("GitLab code exchange request failed") from exc
        body = self._require_success(response, "code exchange")
        if not isinstance(body.get("access_token"), str) or not isinstance(
            body.get("refresh_token"), str
        ):
            raise SourceControlError("GitLab code exchange returned no usable token")
        return body

    async def _refresh(
        self, refresh_token: str, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        try:
            async with self._client(timeout_s=timeout_s) as client:
                response = await client.post(
                    f"{GITLAB_ORIGIN}/oauth/token",
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                        "redirect_uri": self.config.redirect_uri,
                    },
                )
        except httpx.HTTPError as exc:
            raise SourceControlError("GitLab token refresh request failed") from exc
        body = self._require_success(response, "token refresh")
        if not isinstance(body.get("access_token"), str) or not isinstance(
            body.get("refresh_token"), str
        ):
            raise SourceControlError("GitLab token refresh returned no usable token")
        return body

    @staticmethod
    def token_expiry(token: dict[str, Any]) -> datetime:
        """Derive a bounded expiry from GitLab's token response."""
        created_at = token.get("created_at")
        expires_in = token.get("expires_in")
        try:
            if created_at is None or expires_in is None:
                raise ValueError("the token response carries no usable expiry")
            issued = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
            lifetime = max(1, int(expires_in))
        except (TypeError, ValueError, OSError):
            issued = datetime.now(timezone.utc)
            lifetime = 7200
        return issued + timedelta(seconds=lifetime)

    async def _api_get(
        self, path: str, *, access_token: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            async with self._client() as client:
                response = await client.get(
                    f"{GITLAB_ORIGIN}/api/v4{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise SourceControlError("GitLab API request failed") from exc
        if response.status_code >= 400:
            raise SourceControlError(f"GitLab API request failed ({response.status_code})")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceControlError("GitLab API returned an invalid response") from exc

    async def verify_user(self, access_token: str) -> dict[str, Any]:
        """Return the GitLab-attested identity for an access token."""
        body = await self._api_get("/user", access_token=access_token)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("id"), int)
            or not body.get("username")
        ):
            raise SourceControlError("GitLab did not return an authenticated user")
        return body

    async def list_projects(self, access_token: str) -> list[dict[str, Any]]:
        """Enumerate projects attested by GitLab, bounded to 500 entries."""
        projects: list[dict[str, Any]] = []
        for page in range(1, 6):
            body = await self._api_get(
                "/projects",
                access_token=access_token,
                params={
                    "membership": "true",
                    "simple": "true",
                    "order_by": "last_activity_at",
                    "sort": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(body, list):
                raise SourceControlError("GitLab projects response was not a list")
            for item in body:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("id"), int)
                    and item.get("path_with_namespace")
                ):
                    projects.append(
                        {
                            "id": str(item["id"]),
                            "name": str(item["path_with_namespace"]),
                            "web_url": item.get("web_url"),
                        }
                    )
            if len(body) < 100:
                break
        return projects

    async def _await_gitlab_refresh(
        self,
        store: SourceControlRepository,
        *,
        user_id: str,
    ) -> str:
        """Return only a winning refresh/reconnect token, or fail retryably."""
        for attempt in range(_GITLAB_REFRESH_WAIT_ATTEMPTS):
            current = await store.get_gitlab_connection(user_id=user_id)
            if current is None:
                raise SourceControlError("The GitLab connection changed while its token refreshed.")
            # A missing lease means either the winner stored its result or a
            # reconnect installed the current credential. This also handles a
            # loser whose initial snapshot already observed the winner's
            # generation. An abandoned lease retains its (possibly expired)
            # timestamp, so it fails retryably instead of exposing an old token.
            if current.get("refresh_lease_expires_at") is None:
                return self.cipher.decrypt(current["access_token_ciphertext"])
            if attempt + 1 < _GITLAB_REFRESH_WAIT_ATTEMPTS:
                await asyncio.sleep(_GITLAB_REFRESH_WAIT_INTERVAL_S)
        raise SourceControlError("The GitLab token is being refreshed. Try again shortly.")

    async def access_token_for_connection(
        self, store: SourceControlRepository, connection: dict[str, Any]
    ) -> str:
        """Decrypt a live token, rotating both tokens before expiry."""
        expires_at = connection["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc) + _TOKEN_REFRESH_MARGIN:
            return self.cipher.decrypt(connection["access_token_ciphertext"])
        connection_id = int(connection["id"])
        user_id = str(connection["user_id"])
        renewed = None
        async with store.serialize_gitlab_account(user_id=user_id) as account_store:
            # Pin the request timeout to the value that sized this claim. A
            # settings update between these awaits must affect the next
            # request, not lengthen this single-use refresh past its lease.
            refresh_timeout_s = self.request_timeout_s
            claim = await account_store.claim_gitlab_token_refresh(
                connection_id=connection_id,
                user_id=user_id,
                lease_seconds=_refresh_lease_seconds(refresh_timeout_s),
                margin_seconds=int(_TOKEN_REFRESH_MARGIN.total_seconds()),
            )
            if claim is not None:
                refresh_generation = int(claim["refresh_generation"])
                refresh_token = self.cipher.decrypt(claim["refresh_token_ciphertext"])
                # The claim committed before provider I/O. A transient failure
                # leaves its recoverable lease and releases this outer lock.
                refreshed = await self._refresh(refresh_token, timeout_s=refresh_timeout_s)
                if await account_store.update_gitlab_tokens(
                    connection_id=connection_id,
                    user_id=user_id,
                    refresh_generation=refresh_generation,
                    access_token_ciphertext=self.cipher.encrypt(refreshed["access_token"]),
                    refresh_token_ciphertext=self.cipher.encrypt(refreshed["refresh_token"]),
                    expires_at=self.token_expiry(refreshed),
                ):
                    renewed = str(refreshed["access_token"])
        if renewed is not None:
            return renewed
        # Poll only after releasing serialization, so the winner can store.
        return await self._await_gitlab_refresh(store, user_id=user_id)

    async def projects_for_connection(
        self, store: SourceControlRepository, connection: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List projects with the connection's live or refreshed token."""
        token = await self.access_token_for_connection(store, connection)
        return await self.list_projects(token)

    async def revoke(self, access_token: str) -> None:
        """Best-effort revocation; callers still delete local credentials on failure."""
        try:
            async with self._client() as client:
                await client.post(
                    f"{GITLAB_ORIGIN}/oauth/revoke",
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "token": access_token,
                    },
                )
        except httpx.HTTPError as exc:
            raise SourceControlError("GitLab token revocation request failed") from exc


__all__ = [
    "SOURCE_CONTROL_DOMAIN",
    "GitLabOAuthClient",
    "GitLabOAuthConfig",
    "OAuthStateError",
    "SourceControlCipher",
    "SourceControlError",
    "consume_oauth_state",
    "github_authorization_url",
    "issue_oauth_state",
]
