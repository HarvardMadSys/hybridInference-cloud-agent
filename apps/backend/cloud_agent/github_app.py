"""GitHub App credentials for the publisher (issue #1041).

The design calls for a GitHub App rather than a personal access token, and the
difference is not bureaucratic. A PAT is a long-lived, account-wide credential
that someone has to create by hand and paste into a secret store. An App holds
a *private key*, and the platform derives short-lived tokens from it:

- an **App JWT**, valid ten minutes, proves "I am this App";
- exchanged for an **installation token**, valid one hour and scoped to the
  repositories that installation actually covers.

So a leak has a bounded life measured in minutes, the blast radius is the
repositories a user deliberately installed on, and nobody has to mint or rotate
a credential by hand. Revocation is uninstalling the App.

Tokens are cached until shortly before they expire, because the exchange is a
network round trip and the publisher runs per job.
"""

from __future__ import annotations

import asyncio
import json as json_module
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting
from cloud_agent.source_control import SourceControlCipher

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize"

# GitHub rejects an App JWT with more than ten minutes of life. Nine leaves
# room for clock skew without being refused.
_JWT_TTL_S = 540
# Installation tokens last an hour; refresh early so a job never starts with a
# credential that expires mid-push.
_TOKEN_REFRESH_MARGIN_S = 300
# How long one user's repository answer is reused. Entitlement is asked on every
# workspace file read and terminal open; a minute keeps those off GitHub while
# bounding how long a revoked permission still reads as granted.
_USER_REPOSITORIES_TTL_S = 60.0
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


class GitHubAppError(Exception):
    """Raised when App credentials cannot be obtained."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Record the HTTP status when the failure came from GitHub."""
        super().__init__(message)
        self.status = status


class UserAuthorizationRevoked(GitHubAppError):
    """GitHub no longer honours this user's token: the user has to connect again.

    Settled, like :class:`AppNotInstalled`: the user revoked the App's
    authorization, or its refresh token expired or was spent. Retrying cannot
    change it, and the stored token is forgotten rather than tried again.
    """


class RepositoryCreationError(GitHubAppError):
    """A safe repository-creation failure, including an uncertain outcome."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        """Keep an actionable reason without retaining GitHub's response body."""
        super().__init__(f"GitHub repository creation failed: {reason}", status=status)
        self.reason = reason


@dataclass(frozen=True)
class UserTokens:
    """What GitHub hands back for a user: a token, and how to renew it.

    An App that expires user tokens returns an access token good for eight
    hours and a single-use refresh token good for six months. One that does not
    returns only the access token, which then has no expiry here either.
    """

    access_token: str
    access_expires_at: datetime | None = None
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None


def _user_tokens(body: dict[str, Any]) -> UserTokens:
    """Read GitHub's token response, with its relative expiries made absolute."""
    now = datetime.now(timezone.utc)

    def at(seconds: Any) -> datetime | None:
        return now + timedelta(seconds=int(seconds)) if isinstance(seconds, int) else None

    return UserTokens(
        access_token=str(body["access_token"]),
        access_expires_at=at(body.get("expires_in")),
        refresh_token=body.get("refresh_token") or None,
        refresh_expires_at=at(body.get("refresh_token_expires_in")),
    )


class AppNotInstalled(GitHubAppError):
    """The App does not cover this repository.

    Distinct from every other failure because it is a *settled* answer, not a
    transient one: retrying will not change it, and a caller may reasonably
    carry on without a credential (a public repository clones anonymously).
    Collapsing this together with "GitHub returned 503" is what turns a
    momentary outage into a permanently failed job.
    """


class CommitNotOnBranch(GitHubAppError):
    """A commit exists in the repository's object store but not in a branch's history.

    Settled, and the caller's to fix: GitHub serves any object reachable in a
    repository's network by hash — a fork's pull request head among them — so
    a commit that resolves is not therefore one the repository's own history
    holds. The message names the commit and the branch and is safe to show.
    """


@dataclass(frozen=True)
class AppConfig:
    """The App's identity. The private key never leaves this process."""

    app_id: str
    private_key: str
    api_base: str = GITHUB_API

    # The OAuth half of a GitHub App. Only needed for the user-facing connect
    # flow — minting installation tokens uses the private key alone.
    client_id: str = ""
    client_secret: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str]) -> AppConfig | None:
        """Build from environment, or None when the App is not configured.

        Returning None rather than raising is deliberate: a deployment without
        the App should simply not publish, not fail to boot.
        """
        app_id = (env.get("AGENT_GITHUB_APP_ID") or "").strip()
        key = env.get("AGENT_GITHUB_APP_PRIVATE_KEY") or ""
        if not key and (path := env.get("AGENT_GITHUB_APP_PRIVATE_KEY_PATH")):
            try:
                with open(path, encoding="utf-8") as handle:
                    key = handle.read()
            except OSError as exc:
                raise GitHubAppError(f"cannot read App private key at {path}: {exc}") from exc
        # A key pasted into an env var usually arrives with escaped newlines;
        # PEM parsing fails on those in a way that is tedious to diagnose.
        key = key.replace("\\n", "\n").strip()
        if not app_id or not key:
            return None
        return cls(
            app_id=app_id,
            private_key=key,
            api_base=env.get("AGENT_GITHUB_API", GITHUB_API),
            client_id=(env.get("AGENT_GITHUB_APP_CLIENT_ID") or "").strip(),
            client_secret=(env.get("AGENT_GITHUB_APP_CLIENT_SECRET") or "").strip(),
        )


def build_app_jwt(config: AppConfig, *, now: int | None = None) -> str:
    """Sign the short-lived JWT that proves App identity."""
    issued = int(now if now is not None else time.time())
    try:
        return jwt.encode(
            {
                # Backdated by a minute: GitHub rejects a token whose iat is in
                # the future, and a slightly fast clock is common.
                "iat": issued - 60,
                "exp": issued + _JWT_TTL_S,
                "iss": config.app_id,
            },
            config.private_key,
            algorithm="RS256",
        )
    except Exception as exc:
        raise GitHubAppError(f"could not sign the App JWT: {exc}") from exc


@dataclass
class _CachedToken:
    """One installation's token and when it stops being usable."""

    token: str
    expires_at: float


class GitHubAppCredentials:
    """Mints and caches installation tokens for the publisher."""

    def __init__(
        self,
        config: AppConfig,
        *,
        timeout_s: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build a minter.

        Args:
            config: The App's identity and private key.
            timeout_s: Deadline for one GitHub call.
            client: An existing client to borrow. Supplying one makes this
                object a user of someone else's pool rather than the owner of
                its own, which is what tests want; :meth:`aclose` then leaves
                it alone.
        """
        self._config = config
        self._timeout_override = timeout_s
        self._tokens: dict[tuple[int, str], _CachedToken] = {}
        self._installations: dict[str, int] = {}
        #: Per user, per set of connected installations: when the answer was
        #: read, and the repositories it named.
        self._user_repositories: dict[tuple[str, tuple[int, ...]], tuple[float, list[str]]] = {}
        self._cipher: SourceControlCipher | None = None
        self._client = client
        self._owned: httpx.AsyncClient | None = None
        self._active_http = 0
        self._retired = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._retire_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def _http(self, *, timeout_s: float | None = None) -> AsyncIterator[httpx.AsyncClient]:
        """Yield a client for one GitHub call, reusing this object's own.

        A client per call meant a fresh TCP connection and a fresh TLS
        handshake to api.github.com every time — and these calls are not
        occasional. Repository entitlement asks GitHub on workspace file reads
        and terminal opens (through :meth:`writable_repositories_for_user`,
        whenever its minute-long answer has lapsed), so a per-call handshake
        was on the interactive path, not just the publisher's.

        One client for this object's lifetime instead. It is created on first
        use rather than in ``__init__`` because a client binds to the running
        loop, and this object is built during application startup.
        """
        effective_timeout_s = self._timeout_s if timeout_s is None else timeout_s
        if self._client is not None:
            yield self._client
            return
        # Requests that acquired the previous configuration may still finish
        # after an admin saved a replacement. Their remaining calls use a
        # temporary pool; the retired shared pool closes once its current
        # callers finish, without interrupting an in-flight GitHub request.
        if self._retired:
            async with httpx.AsyncClient(timeout=effective_timeout_s) as client:
                yield client
            return
        self._loop = asyncio.get_running_loop()
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=effective_timeout_s)
        else:
            self._owned.timeout = httpx.Timeout(effective_timeout_s)
        self._active_http += 1
        try:
            yield self._owned
        finally:
            self._active_http -= 1
            if self._retired and not self._active_http:
                await self.aclose()

    def retire(self) -> None:
        """Release this configuration's pool after its current calls finish."""
        self._retired = True
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._schedule_retirement)

    @property
    def _timeout_s(self) -> float:
        if self._timeout_override is not None:
            return self._timeout_override
        return number_setting("github_request_timeout_s", 30.0)

    @property
    def request_timeout_s(self) -> float:
        """Effective deadline used for the next provider request."""
        return self._timeout_s

    def _schedule_retirement(self) -> None:
        if not self._active_http and self._owned is not None:
            self._retire_task = asyncio.create_task(self.aclose())

    async def aclose(self) -> None:
        """Release the client this object created, if it created one.

        A borrowed client belongs to whoever passed it in and is left open.
        """
        owned, self._owned = self._owned, None
        if owned is not None:
            await owned.aclose()

    @property
    def cipher(self) -> SourceControlCipher:
        """The key users' GitHub tokens are stored under — GitLab's, domain and all.

        Built on first use, because a deployment that never lets users connect
        publishes with the private key alone and needs no encryption key.
        """
        if self._cipher is None:
            self._cipher = SourceControlCipher()
        return self._cipher

    @property
    def user_authorization_configured(self) -> bool:
        """Whether the App can exchange a browser authorization code."""
        return bool(self._config.client_id and self._config.client_secret)

    def user_authorization_url(self, state: str) -> str:
        """Start GitHub's user OAuth flow, independently of App installation.

        The App installation URL only starts OAuth for a new installation. If
        the App is already installed, GitHub opens the installation settings
        page instead and never calls us back. Reauthorization therefore has to
        use the web application flow directly.
        """
        if not self.user_authorization_configured:
            raise GitHubAppError("the GitHub App's user authorization is not configured")
        query = urlencode({"client_id": self._config.client_id, "state": state})
        return f"{GITHUB_OAUTH_AUTHORIZE}?{query}"

    async def _request(
        self, method: str, path: str, *, token: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call the GitHub API and return the decoded body."""
        async with self._http() as client:
            response = await client.request(
                method,
                f"{self._config.api_base}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=json,
            )
        if response.status_code >= 400:
            # The body can echo the repository name; the token never appears in
            # it, and it is the only useful diagnostic for a misconfigured App.
            raise GitHubAppError(
                f"GitHub {method} {path} failed ({response.status_code}): {response.text[:200]}",
                status=response.status_code,
            )
        return response.json()

    async def installation_id_for(self, repo: str) -> int:
        """Return the installation covering ``owner/name``.

        A repository with no installation is not an error to retry — it means
        the user has not installed the App there, and the publisher must say so
        rather than appear to be broken.
        """
        if repo in self._installations:
            return self._installations[repo]
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise GitHubAppError(f"repo must be 'owner/name', got {repo!r}")
        try:
            body = await self._request(
                "GET",
                f"/repos/{owner}/{name}/installation",
                token=build_app_jwt(self._config),
            )
        except GitHubAppError as exc:
            # 404 is the settled answer "not installed here"; 5xx and the rest
            # are GitHub having a bad minute, and must stay distinguishable.
            if exc.status == 404:
                raise AppNotInstalled(f"no App installation covers {repo}", status=404) from exc
            raise
        installation_id = body.get("id")
        if not isinstance(installation_id, int):
            raise AppNotInstalled(f"no App installation covers {repo}")
        self._installations[repo] = installation_id
        return installation_id

    async def token_for(
        self,
        repo: str,
        *,
        permissions: dict[str, str] | None = None,
        repository_scoped: bool = False,
    ) -> str:
        """Return a live installation token for one repository.

        ``permissions`` narrows the token below what the App holds — an
        installation token inherits *every* App permission unless it asks for
        less, so a consumer that only needs to read must say so. Combined with
        ``repository_scoped``, which limits the token to this repository alone,
        that is how one App serves checkout (read), sandbox branch/PR writes,
        and the trusted publisher without sharing broader installation scope.
        """
        installation_id = await self.installation_id_for(repo)
        token, minted = await self._token_in_scope(
            installation_id,
            permissions=permissions,
            repository=repo if repository_scoped else None,
        )
        if minted:
            logger.info(
                "agent_github_app_token_minted",
                extra={
                    "event": "agent_github_app_token_minted",
                    "repo": repo,
                    "installation_id": installation_id,
                    "permissions": sorted(permissions or ()) or "inherited",
                },
            )
        return token

    async def _token_in_scope(
        self,
        installation_id: int,
        *,
        permissions: dict[str, str] | None = None,
        repository: str | None = None,
    ) -> tuple[str, bool]:
        """Return a token for one installation and scope, and whether it is new.

        The one place a token is looked up, minted and cached, so that every
        caller lands in the cache rather than only the ones that remembered to.

        Narrowed tokens must not share a cache slot with full ones, in either
        direction: serving a read-only token to the publisher would break every
        push, and serving a full one to the runner would silently undo the
        narrowing that parameter exists for. The scope key is what keeps them
        apart, and it is derived from the same two values that shape the
        request — so a slot cannot describe a scope the token does not have.
        """
        scope_key = json_module.dumps({"p": permissions, "r": repository}, sort_keys=True)
        cached = self._tokens.get((installation_id, scope_key))
        if cached and cached.expires_at - _TOKEN_REFRESH_MARGIN_S > time.time():
            return cached.token, False

        payload: dict[str, Any] = {}
        if permissions:
            payload["permissions"] = permissions
        if repository:
            payload["repositories"] = [repository.partition("/")[2]]
        body = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=build_app_jwt(self._config),
            json=payload or None,
        )
        token = body.get("token")
        if not token:
            raise GitHubAppError("GitHub returned no installation token")
        # GitHub reports expiry; fall back to the documented hour if absent so
        # a missing field cannot produce a token cached forever.
        expires_at = time.time() + 3600
        if raw_expiry := body.get("expires_at"):
            expires_at = _parse_expiry(raw_expiry, default=expires_at)
        self._tokens[installation_id, scope_key] = _CachedToken(token=token, expires_at=expires_at)
        return token, True

    async def exchange_user_code(self, code: str) -> UserTokens:
        """Trade the callback's code for a token that speaks *as the user*.

        This is what makes the connection an entitlement rather than a claim:
        the resulting token can only see installations the user themselves can
        reach, so what we record afterwards is GitHub's answer, not the
        browser's.
        """
        if not (self._config.client_id and self._config.client_secret):
            raise GitHubAppError(
                "the GitHub App's client id and secret are not configured, so the "
                "connect flow cannot verify who is connecting"
            )
        async with self._http() as client:
            response = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "code": code,
                },
            )
        if response.status_code >= 400:
            raise GitHubAppError(f"GitHub refused the code exchange ({response.status_code})")
        body = response.json()
        if not body.get("access_token"):
            # GitHub returns 200 with an error body for a spent or wrong code.
            raise GitHubAppError(f"GitHub returned no user token: {body.get('error', 'unknown')}")
        return _user_tokens(body)

    async def refresh_user_token(
        self, refresh_token: str, *, timeout_s: float | None = None
    ) -> UserTokens:
        """Spend a refresh token for a new pair. Each refresh token works once."""
        effective_timeout_s = self.request_timeout_s if timeout_s is None else timeout_s
        async with self._http(timeout_s=effective_timeout_s) as client:
            response = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=effective_timeout_s,
            )
        if response.status_code >= 500:
            raise GitHubAppError(
                f"GitHub could not refresh the user token ({response.status_code})",
                status=response.status_code,
            )
        body = response.json() if response.status_code < 400 else {}
        if not body.get("access_token"):
            # `bad_refresh_token` and the rest: expired, spent, or revoked.
            raise UserAuthorizationRevoked(
                f"GitHub refused the refresh token: {body.get('error', response.status_code)}"
            )
        return _user_tokens(body)

    async def revoke_user_authorization(self, access_token: str) -> None:
        """Withdraw the user's authorization of the App, and every token it issued.

        Best effort: a caller forgets its stored tokens whether or not GitHub
        answers.
        """
        async with self._http() as client:
            response = await client.request(
                "DELETE",
                f"{self._config.api_base}/applications/{self._config.client_id}/grant",
                auth=(self._config.client_id, self._config.client_secret),
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"access_token": access_token},
            )
        if response.status_code >= 400 and response.status_code != 404:
            raise GitHubAppError(
                f"GitHub did not revoke the authorization ({response.status_code})",
                status=response.status_code,
            )

    def cached_user_repositories(
        self, user_id: str, installation_ids: tuple[int, ...]
    ) -> list[str] | None:
        """This user's repositories if read within the last minute, else ``None``."""
        cached = self._user_repositories.get((user_id, installation_ids))
        if cached is None or time.monotonic() - cached[0] > number_setting(
            "github_repository_cache_s", _USER_REPOSITORIES_TTL_S
        ):
            return None
        return list(cached[1])

    def remember_user_repositories(
        self, user_id: str, installation_ids: tuple[int, ...], repositories: list[str]
    ) -> None:
        """Keep one user's answer for the next minute's reads."""
        self._user_repositories[user_id, installation_ids] = (time.monotonic(), list(repositories))

    def forget_user(self, user_id: str) -> None:
        """Drop a user's remembered answer, when their connection changed."""
        for key in [key for key in self._user_repositories if key[0] == user_id]:
            self._user_repositories.pop(key, None)

    async def create_personal_repository(
        self, user_token: str, *, name: str, description: str, private: bool
    ) -> dict[str, str]:
        """Create an initialized repository in the authorizing user's account.

        A user token and GitHub's personal-account endpoint select the owner;
        neither an installation credential nor an owner supplied by the browser
        can do so. This write is sent once. If the response is lost, callers
        must check GitHub before trying again rather than risk another write.
        """
        try:
            async with self._http() as client:
                response = await client.post(
                    f"{self._config.api_base}/user/repos",
                    headers={
                        "Authorization": f"Bearer {user_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={
                        "name": name,
                        "description": description,
                        "private": private,
                        "auto_init": True,
                    },
                )
        except httpx.RequestError as exc:
            raise RepositoryCreationError("uncertain") from exc
        status = response.status_code
        if status == 401:
            raise UserAuthorizationRevoked("GitHub refused the user's token", status=status)
        if status == 429 or (
            status == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
            )
        ):
            raise RepositoryCreationError("unavailable", status=status)
        if status in (403, 404):
            raise RepositoryCreationError("forbidden", status=status)
        if status in (400, 422):
            try:
                body = response.json()
            except ValueError:
                body = {}
            errors = body.get("errors", []) if isinstance(body, dict) else []
            duplicate = isinstance(errors, list) and any(
                isinstance(error, dict)
                and error.get("resource") == "Repository"
                and error.get("field") == "name"
                and (
                    error.get("code") == "already_exists"
                    or (
                        error.get("code") == "custom"
                        and str(error.get("message", "")).lower().startswith("name already exists")
                    )
                )
                for error in errors
            )
            raise RepositoryCreationError(
                "already_exists" if duplicate else "invalid", status=status
            )
        if status != 201:
            raise RepositoryCreationError("uncertain", status=status)
        try:
            body = response.json()
        except ValueError as exc:
            raise RepositoryCreationError("uncertain", status=status) from exc
        fields = ("full_name", "html_url", "default_branch")
        if not isinstance(body, dict) or any(
            not isinstance(body.get(field), str) or not body[field] for field in fields
        ):
            raise RepositoryCreationError("uncertain", status=status)
        return {field: body[field] for field in fields}

    async def writable_repositories_for_user(
        self, user_token: str, installation_id: int
    ) -> list[str]:
        """The repositories of one installation this user may push to.

        Asked as the user, so GitHub answers with the repositories *they* can
        access — the installation's own token lists every repository it covers,
        which for an organization is repositories this user was never given.
        Push, not read: the agent's work ends as a branch pushed and a pull
        request opened, which a user who can only read could not do themselves.

        An installation the user can no longer reach contributes nothing; a
        token GitHub refuses is :class:`UserAuthorizationRevoked`.
        """
        repos: list[str] = []
        page = 1
        async with self._http() as client:
            while page <= 10:  # bounded: 1000 repositories is plenty
                response = await client.get(
                    f"{self._config.api_base}/user/installations/{installation_id}/repositories",
                    params={"per_page": 100, "page": page},
                    headers={
                        "Authorization": f"Bearer {user_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if response.status_code == 401:
                    raise UserAuthorizationRevoked("GitHub refused the user's token")
                if response.status_code in (403, 404):
                    return []
                if response.status_code >= 400:
                    raise GitHubAppError(
                        f"could not list the user's repositories in installation "
                        f"{installation_id} ({response.status_code})",
                        status=response.status_code,
                    )
                entries = response.json().get("repositories", [])
                repos.extend(
                    entry["full_name"]
                    for entry in entries
                    if entry.get("full_name") and (entry.get("permissions") or {}).get("push")
                )
                if len(entries) < 100:
                    break
                page += 1
        return repos

    async def user_profile(self, user_token: str) -> dict[str, Any]:
        """Return ``{"id", "login"}`` for the GitHub user a token speaks as.

        The account a connection belongs to is GitHub's answer for the token the
        connect flow just exchanged, never a name the browser supplied.
        """
        async with self._http() as client:
            response = await client.get(
                f"{self._config.api_base}/user",
                headers={
                    "Authorization": f"Bearer {user_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code >= 400:
            raise GitHubAppError(f"could not read the GitHub user ({response.status_code})")
        body = response.json()
        user_id, login = body.get("id"), body.get("login")
        if not isinstance(user_id, int) or not isinstance(login, str) or not login:
            raise GitHubAppError("GitHub returned no user for the authorization")
        return {"id": user_id, "login": login}

    async def installations_for_user(self, user_token: str) -> list[dict[str, Any]]:
        """List every installation this user can actually reach.

        Every page: the connect flow makes a user's grants exactly this list, so
        stopping at GitHub's default thirty would revoke the thirty-first.
        """
        out: list[dict[str, Any]] = []
        page = 1
        async with self._http() as client:
            while page <= 10:  # bounded, like the repository listing
                response = await client.get(
                    f"{self._config.api_base}/user/installations",
                    params={"per_page": 100, "page": page},
                    headers={
                        "Authorization": f"Bearer {user_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if response.status_code >= 400:
                    raise GitHubAppError(
                        f"could not read the user's installations ({response.status_code})"
                    )
                entries = response.json().get("installations", [])
                for entry in entries:
                    account = entry.get("account") or {}
                    out.append(
                        {
                            "installation_id": entry.get("id"),
                            "account_login": account.get("login"),
                            # The account's id, not its login: a login can be
                            # renamed, and this is compared with the user's own.
                            "account_id": account.get("id"),
                        }
                    )
                if len(entries) < 100:
                    break
                page += 1
        return [item for item in out if isinstance(item["installation_id"], int)]

    async def repositories_for_installation(self, installation_id: int) -> list[str]:
        """List ``owner/name`` for every repository one installation covers.

        Read with the installation's own token, so the answer is the App's
        actual reach rather than anything the caller supplied.

        **Not an entitlement.** It was one, and that was the hole: an
        organization's installation covers repositories a given member or
        collaborator was never given. What a user may work on is asked as the
        user — :meth:`writable_repositories_for_user`.
        """
        token = await self._installation_token(installation_id)
        repos: list[str] = []
        page = 1
        async with self._http() as client:
            while page <= 10:  # bounded: 1000 repositories is plenty
                response = await client.get(
                    f"{self._config.api_base}/installation/repositories",
                    params={"per_page": 100, "page": page},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if response.status_code >= 400:
                    raise GitHubAppError(
                        f"could not list repositories for installation {installation_id} "
                        f"({response.status_code})"
                    )
                body = response.json()
                names = [
                    item.get("full_name")
                    for item in body.get("repositories", [])
                    if item.get("full_name")
                ]
                repos.extend(names)
                if len(names) < 100:
                    break
                page += 1
        return repos

    async def branches_for_repo(self, repo: str) -> dict[str, Any]:
        """Return ``{"default": name, "branches": [...]}`` for one repository.

        Read with the repository's own installation token, so a caller cannot
        learn anything about a repository the App does not cover.
        """
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise GitHubAppError(f"repo must be 'owner/name', got {repo!r}")
        token = await self.token_for(repo, permissions={"contents": "read"}, repository_scoped=True)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with self._http() as client:
            meta = await client.get(
                f"{self._config.api_base}/repos/{owner}/{name}", headers=headers
            )
            listing = await client.get(
                f"{self._config.api_base}/repos/{owner}/{name}/branches",
                params={"per_page": 100},
                headers=headers,
            )
        if meta.status_code >= 400 or listing.status_code >= 400:
            raise GitHubAppError(f"could not read branches for {repo}")
        names = [item.get("name") for item in listing.json() if item.get("name")]
        default = meta.json().get("default_branch")
        # Default first, so the picker's first option is the one a reader expects.
        ordered = ([default] if default in names else []) + sorted(n for n in names if n != default)
        return {"default": default, "branches": ordered}

    async def resolve_ref(self, repo: str, ref: str) -> str:
        """Resolve a branch (or any ref) to the commit sha it points at.

        The job stores the *sha*, never the branch name. A branch moves; a job
        that recorded "dev" would silently mean a different tree by the time it
        ran, and the publisher applies its patch onto a pinned commit.
        """
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise GitHubAppError(f"repo must be 'owner/name', got {repo!r}")
        token = await self.token_for(repo, permissions={"contents": "read"}, repository_scoped=True)
        async with self._http() as client:
            response = await client.get(
                f"{self._config.api_base}/repos/{owner}/{name}/commits/{ref}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code >= 400:
            raise GitHubAppError(f"could not resolve {ref!r} in {repo}")
        sha = response.json().get("sha")
        if not isinstance(sha, str) or not sha:
            raise GitHubAppError(f"GitHub returned no commit for {ref!r} in {repo}")
        return sha

    async def require_commit_on_branch(
        self, repo: str, sha: str, *, branch: str | None = None
    ) -> str:
        """Confirm ``sha`` is in the history of a branch of ``repo``; return that branch.

        A job's base commit is fetched by hash and the publisher pushes a
        branch built on it, so a hash alone would let a submission start from
        any object GitHub serves for the repository, such as a fork's pull
        request head, and have the platform push it under its own name.

        ``branch`` is the one the job names. When it is not a branch of this
        repository (a tag, a commit, ``refs/pull/...``) or none is named, the
        repository's default branch is the anchor instead: the branch is read
        through ``/branches/``, so a caller cannot point the comparison at
        another repository's branch. Comparing the tip with the commit answers
        ``behind`` or ``identical`` exactly when the commit is an ancestor of
        the tip. Raises :class:`CommitNotOnBranch` when it is not.
        """
        if not _COMMIT_SHA.fullmatch(sha or ""):
            raise GitHubAppError(f"base commit must be a commit sha, got {sha!r}")
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise GitHubAppError(f"repo must be 'owner/name', got {repo!r}")
        token = await self.token_for(repo, permissions={"contents": "read"}, repository_scoped=True)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        base_url = f"{self._config.api_base}/repos/{owner}/{name}"

        async def branch_tip(client: httpx.AsyncClient, candidate: str) -> str | None:
            response = await client.get(
                f"{base_url}/branches/{quote(candidate, safe='/')}", headers=headers
            )
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise GitHubAppError(
                    f"could not read branch {candidate!r} in {repo}", status=response.status_code
                )
            commit = response.json().get("commit")
            tip = commit.get("sha") if isinstance(commit, dict) else None
            return tip if isinstance(tip, str) and _COMMIT_SHA.fullmatch(tip) else None

        async with self._http() as client:
            anchor = branch or ""
            tip = await branch_tip(client, anchor) if anchor else None
            if tip is None:
                meta = await client.get(base_url, headers=headers)
                if meta.status_code >= 400:
                    raise GitHubAppError(f"could not read {repo}", status=meta.status_code)
                default = meta.json().get("default_branch")
                if not isinstance(default, str) or not default:
                    raise GitHubAppError(f"GitHub returned no default branch for {repo}")
                anchor, tip = default, await branch_tip(client, default)
                if tip is None:
                    raise GitHubAppError(f"could not read branch {default!r} in {repo}")
            if tip.lower().startswith(sha.lower()):
                return anchor
            comparison = await client.get(
                f"{base_url}/compare/{tip}...{sha}", params={"per_page": 1}, headers=headers
            )
        refused = CommitNotOnBranch(
            f"commit {sha[:12]} is not in the history of branch {anchor!r} in {repo}"
        )
        if comparison.status_code in (404, 422):
            # No such commit, or none GitHub can relate to the tip.
            raise refused
        if comparison.status_code >= 400:
            raise GitHubAppError(
                f"could not compare {sha[:12]} with {anchor!r} in {repo}",
                status=comparison.status_code,
            )
        if comparison.json().get("status") not in ("behind", "identical"):
            raise refused
        return anchor

    async def repository_contents(
        self,
        repo: str,
        *,
        path: str = "",
        ref: str,
        allow_symbolic_ref: bool = False,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Read one file or directory from a repository at a pinned commit.

        The token is narrowed twice: ``contents: read`` and the one repository
        named by the job.  ``ref`` must be a commit hash rather than a moving
        branch, otherwise the Files view could silently show a different tree
        from the one the agent actually edited. Skill previews explicitly allow
        symbolic refs and confirm their independently computed content digest.

        ``path`` is expected to have passed the owner API's stricter workspace
        validation.  It is still encoded one segment at a time here so a slash
        or query marker can never change the GitHub endpoint being called.
        """
        if not allow_symbolic_ref and not _COMMIT_SHA.fullmatch(ref or ""):
            raise GitHubAppError("repository contents require a pinned commit sha")
        _repository_content_endpoint(repo, path)
        token = await self.token_for(repo, permissions={"contents": "read"}, repository_scoped=True)
        async with self._http() as client:
            return await read_repository_contents(
                client,
                api_base=self._config.api_base,
                repo=repo,
                path=path,
                ref=ref,
                token=token,
                max_response_bytes=max_response_bytes,
            )

    async def _installation_token(self, installation_id: int) -> str:
        """Return a plain installation token for a known installation id.

        This is byte-for-byte the request :meth:`token_for` makes when it is
        asked to narrow nothing, so it belongs in that same unnarrowed cache
        slot — and it was not using it. When repository entitlement still
        resolved through :meth:`repositories_for_installation`, which starts
        here, on every workspace file read, file write and terminal
        create/list/stream, the omission put a token mint on the interactive
        path and kept it there.

        Sharing the slot is correct rather than merely convenient: the scope
        key is derived from the same values that shape the request, so an
        unnarrowed token can only ever satisfy an unnarrowed caller.
        """
        token, _ = await self._token_in_scope(installation_id)
        return token

    def forget(self, repo: str) -> None:
        """Drop cached state for a repo, e.g. after the App is uninstalled."""
        installation_id = self._installations.pop(repo, None)
        if installation_id is not None:
            for key in [k for k in self._tokens if k[0] == installation_id]:
                self._tokens.pop(key, None)


def _parse_expiry(raw: str, *, default: float) -> float:
    """Parse GitHub's ISO-8601 expiry, falling back on anything unexpected."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


async def read_repository_contents(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    repo: str,
    path: str,
    ref: str,
    token: str | None = None,
    max_response_bytes: int | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Read a fixed GitHub Contents endpoint with optional bounded streaming."""
    endpoint = _repository_content_endpoint(repo, path)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with client.stream(
        "GET",
        f"{api_base}{endpoint}",
        params={"ref": ref},
        headers=headers,
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise GitHubAppError(
                f"could not read repository contents for {repo} ({response.status_code})",
                status=response.status_code,
            )
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if max_response_bytes is not None and len(data) > max_response_bytes:
                raise GitHubAppError("Repository content exceeds the allowed size")
    body = json_module.loads(data)
    if not isinstance(body, (dict, list)):
        raise GitHubAppError("GitHub returned malformed repository contents")
    return body


def _repository_content_endpoint(repo: str, path: str) -> str:
    """Validate and encode a repository-relative GitHub Contents path."""
    owner, separator, name = repo.partition("/")
    if not owner or separator != "/" or not name or "/" in name:
        raise GitHubAppError(f"repo must be 'owner/name', got {repo!r}")

    parts = path.split("/") if path else []
    if (len(path) >= 3 and path[0].isalpha() and path[1:3] == ":/") or any(
        not part
        or part in {".", ".."}
        or part.casefold() == ".git"
        or "\\" in part
        or "\x00" in part
        for part in parts
    ):
        raise GitHubAppError("repository content path is not a safe relative path")

    endpoint = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/contents"
    if parts:
        endpoint += "/" + "/".join(quote(part, safe="") for part in parts)
    return endpoint
