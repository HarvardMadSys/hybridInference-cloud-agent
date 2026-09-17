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

import json as json_module
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from cloud_agent.logging_utils import get_logger

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
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


class GitHubAppError(Exception):
    """Raised when App credentials cannot be obtained."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Record the HTTP status when the failure came from GitHub."""
        super().__init__(message)
        self.status = status


class AppNotInstalled(GitHubAppError):
    """The App does not cover this repository.

    Distinct from every other failure because it is a *settled* answer, not a
    transient one: retrying will not change it, and a caller may reasonably
    carry on without a credential (a public repository clones anonymously).
    Collapsing this together with "GitHub returned 503" is what turns a
    momentary outage into a permanently failed job.
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
        timeout_s: float = 30.0,
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
        self._timeout_s = timeout_s
        self._tokens: dict[tuple[int, str], _CachedToken] = {}
        self._installations: dict[str, int] = {}
        self._client = client
        self._owned: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield a client for one GitHub call, reusing this object's own.

        A client per call meant a fresh TCP connection and a fresh TLS
        handshake to api.github.com every time — and these calls are not
        occasional. Repository entitlement resolves through
        :meth:`repositories_for_installation` on every workspace file read and
        every terminal open, so a per-call handshake was on the interactive
        path, not just the publisher's.

        One client for this object's lifetime instead. It is created on first
        use rather than in ``__init__`` because a client binds to the running
        loop, and this object is built during application startup.
        """
        if self._client is not None:
            yield self._client
            return
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=self._timeout_s)
        yield self._owned

    async def aclose(self) -> None:
        """Release the client this object created, if it created one.

        A borrowed client belongs to whoever passed it in and is left open.
        """
        owned, self._owned = self._owned, None
        if owned is not None:
            await owned.aclose()

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
        that is how one App serves both the publisher (write, to push a branch)
        and the runner (read, to check the repository out) without handing the
        runner push rights on a host that executes untrusted code.
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

    async def exchange_user_code(self, code: str) -> str:
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
        token = body.get("access_token")
        if not token:
            # GitHub returns 200 with an error body for a spent or wrong code.
            raise GitHubAppError(f"GitHub returned no user token: {body.get('error', 'unknown')}")
        return token

    async def installations_for_user(self, user_token: str) -> list[dict[str, Any]]:
        """List the installations this user can actually reach."""
        async with self._http() as client:
            response = await client.get(
                f"{self._config.api_base}/user/installations",
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
        out = []
        for entry in response.json().get("installations", []):
            account = entry.get("account") or {}
            out.append({"installation_id": entry.get("id"), "account_login": account.get("login")})
        return [item for item in out if isinstance(item["installation_id"], int)]

    async def repositories_for_installation(self, installation_id: int) -> list[str]:
        """List ``owner/name`` for every repository one installation covers.

        Read with the installation's own token, so the answer is the App's
        actual reach rather than anything the caller supplied.
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
        slot — and it was not using it. Every workspace file read, file write
        and terminal create/list/stream resolves repository entitlement
        through :meth:`repositories_for_installation`, which starts here, so
        the omission put a token mint on the interactive path and kept it
        there.

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
