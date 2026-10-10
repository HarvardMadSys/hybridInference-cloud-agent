"""Loading the source-control providers, and loading GitHub again after a failure.

Issue #47. The lifespan used to build the GitHub App and GitLab OAuth clients
once and turn any failure into ``None``. On 2026-08-07 the App's key file was
not yet readable by the container user when the control plane started; six
minutes later its owner was fixed, but the process was holding ``None`` and
never looked again. Every surface then described the fault as a deployment
with no App at all.

So a provider here is in one of three states, and they are kept apart:

- ``unconfigured``: the deployment does not use it. A working answer, and the
  one ``None`` has always meant.
- ``unavailable``: the deployment configured it and this process could not
  load it. Nothing may read that as ``unconfigured``: the entitlement check
  would call an installed repository access denied, a claim would hand a
  private repository to an anonymous checkout, and the goal consumer would
  spend a wakeup's retries on a deployment fault.
- ``ready``: loaded. Only that. A GitHub outage, an uninstalled App or a
  revoked authorization is still the ordinary error handling's business.

**A GitHub App that failed to load is loaded again.** A key file can be fixed
in place — a ``chown``, a remount — and a restart should not be the only way
to find out. There is no retry task: whatever needs the credentials asks for
them, and an ask that finds the App unavailable, with the last attempt at
least :data:`RETRY_INTERVAL_S` ago, reads the key again first. The publisher
asks every 15 seconds and the reaper every 30, so a repaired key is picked up
within a minute whether or not anybody is using the site.

Admin settings are checked when each provider is requested. Saving a changed
configuration replaces the credentials on the next operation. Explicitly
injected environment mappings remain fixed for callers that need an isolated
configuration, such as tests.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from cloud_agent.github_app import AppConfig, GitHubAppCredentials, GitHubAppError, build_app_jwt
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting, settings_environment
from cloud_agent.source_control import (
    ENV_ENCRYPTION_KEY,
    GitLabOAuthClient,
    GitLabOAuthConfig,
    SourceControlCipher,
    SourceControlError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: Logged under the name the lifespan used when it built these clients itself:
#: the deployment's log selection is on ``cloud_agent.app``, and the failure
#: this module exists to report must not move to a logger nobody watches.
logger = get_logger("cloud_agent.app")

LoadStatus = Literal["unconfigured", "unavailable", "ready"]

#: Seconds between attempts to load an App that failed. Also the ``Retry-After``
#: a caller refused in the meantime is given.
RETRY_INTERVAL_S = 30.0

#: The one variable that says a deployment uses a GitHub App. Not the key path:
#: the control-plane compose file always sets that one, and mounts
#: ``/dev/null`` there when no App exists, so the path's presence says nothing.
ENV_GITHUB_APP_ID = "AGENT_GITHUB_APP_ID"
_GITHUB_ENV_KEYS = (
    ENV_GITHUB_APP_ID,
    "AGENT_GITHUB_APP_PRIVATE_KEY",
    "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
    "AGENT_GITHUB_APP_CLIENT_ID",
    "AGENT_GITHUB_APP_CLIENT_SECRET",
    "AGENT_GITHUB_API",
)
_GITLAB_ENV_KEYS = (
    "AGENT_GITLAB_OAUTH_CLIENT_ID",
    "AGENT_GITLAB_OAUTH_CLIENT_SECRET",
    "AGENT_GITLAB_OAUTH_REDIRECT_URI",
    ENV_ENCRYPTION_KEY,
)


class SourceControlUnavailable(Exception):
    """A configured provider this process could not load, and is retrying.

    Deliberately not :class:`~cloud_agent.entitlement.RepoNotAllowed` and not a
    :class:`~cloud_agent.source_control.SourceControlError`: every caller that
    catches one of those would turn a deployment fault into the user's problem.
    """

    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} is temporarily unavailable")
        self.provider = provider


class GitHubAppSource(Protocol):
    """Where a long-lived loop asks for the App's credentials, once per pass.

    A loop that took the credentials themselves at startup kept whatever the
    startup produced — ``None``, after a failed load — for as long as it ran.
    """

    def get(self) -> GitHubAppCredentials | None:
        """The credentials; ``None`` if unconfigured; raise if unavailable."""
        ...


@dataclass(frozen=True)
class GitHubAppState:
    """One reading of the App: its credentials and the status they came with.

    Taken together because read apart they can disagree. A request that finds
    a reload in progress gets no credentials; if it then reads the status after
    that reload finished, it holds ``None`` beside ``ready`` — which a page
    renders as "not configured", the misreading issue #47 was about.
    """

    credentials: Any
    load_status: LoadStatus


class _LoadFailed(Exception):
    """Why one attempt to load the App failed, as a reason safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GitHubAppProvider:
    """The GitHub App's credentials, loaded again after a failure.

    One per control-plane process, built by the lifespan. Without an explicit
    environment it refreshes saved configuration before supplying credentials.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        retry_interval_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        credentials_factory: Callable[[AppConfig], GitHubAppCredentials] = GitHubAppCredentials,
    ) -> None:
        """Read the environment once and make the first attempt.

        Args:
            env: The process environment, or a test's stand-in for it.
            retry_interval_s: Minimum seconds between attempts after a failure.
            clock: A monotonic clock; injected so a test need not wait.
            credentials_factory: Builds the minter from a checked config. A test
                that must not reach GitHub substitutes a fake here, after the
                key has been read and signed with for real.
        """
        self._dynamic = env is None
        self._env = dict(env) if env is not None else settings_environment()
        self._configured = bool((self._env.get(ENV_GITHUB_APP_ID) or "").strip())
        self._retry_interval_override = retry_interval_s
        self._clock = clock
        self._credentials_factory = credentials_factory
        self._credentials: GitHubAppCredentials | None = None
        self._reason: str | None = None
        self._attempted_at: float | None = None
        self._unavailable_since: float | None = None
        self._closed = False
        self._retired: list[GitHubAppCredentials] = []
        # Requests resolve their dependencies on a thread pool, while the
        # publisher and the reaper ask from the event loop. One attempt at a
        # time; a caller that finds one in progress does not wait for it.
        self._lock = threading.Lock()
        if self._configured:
            self._load()

    @property
    def _retry_interval_s(self) -> float:
        if self._retry_interval_override is not None:
            return self._retry_interval_override
        return number_setting("github_reload_interval_s", RETRY_INTERVAL_S)

    @property
    def load_status(self) -> LoadStatus:
        """This process's current answer; reading it never loads anything."""
        if not self._configured:
            return "unconfigured"
        return "ready" if self._credentials is not None else "unavailable"

    def get(self) -> GitHubAppCredentials | None:
        """Return the credentials, or ``None`` when no App is configured.

        Raises:
            SourceControlUnavailable: The App is configured and this process
                could not load it. Tried again first when the last attempt is
                at least the retry interval old.
        """
        self._refresh_settings()
        credentials = self._credentials
        if credentials is not None or not self._configured:
            return credentials
        if not self._closed and self._due() and self._lock.acquire(blocking=False):
            try:
                if self._credentials is None and self._due():
                    self._load()
            finally:
                self._lock.release()
        credentials = self._credentials
        if credentials is None:
            raise SourceControlUnavailable("github")
        return credentials

    def _refresh_settings(self) -> None:
        if not self._dynamic or self._closed:
            return
        env = settings_environment()
        if all(env.get(key) == self._env.get(key) for key in _GITHUB_ENV_KEYS):
            return
        # A caller never gets the previous configuration while another thread
        # replaces it. The existing nonblocking load retry keeps its behavior.
        with self._lock:
            env = settings_environment()
            if all(env.get(key) == self._env.get(key) for key in _GITHUB_ENV_KEYS):
                return
            previous, self._credentials = self._credentials, None
            if previous is not None:
                previous.retire()
                self._retired.append(previous)
            self._env = env
            self._configured = bool((env.get(ENV_GITHUB_APP_ID) or "").strip())
            self._attempted_at = None
            self._reason = None
            self._unavailable_since = None
            if self._configured:
                self._load()

    def state(self) -> GitHubAppState:
        """Like :meth:`get`, answered as one consistent reading, never raised.

        For the surfaces that must still answer while the App is unavailable,
        and that report the status beside the credentials: both come from the
        same call, so they cannot describe two different moments.
        """
        try:
            credentials = self.get()
        except SourceControlUnavailable:
            return GitHubAppState(credentials=None, load_status="unavailable")
        return GitHubAppState(
            credentials=credentials,
            load_status="ready" if credentials is not None else "unconfigured",
        )

    async def aclose(self) -> None:
        """Close the loaded client, and load nothing after this."""
        self._closed = True
        credentials, self._credentials = self._credentials, None
        if credentials is not None:
            await credentials.aclose()
        retired, self._retired = self._retired, []
        for previous in retired:
            await previous.aclose()

    def _due(self) -> bool:
        return (
            self._attempted_at is None
            or self._clock() - self._attempted_at >= self._retry_interval_s
        )

    def _load(self) -> None:
        self._attempted_at = self._clock()
        try:
            credentials = self._build()
        except _LoadFailed as failure:
            self._failed(failure.reason, failure.__cause__)
            return
        except Exception as exc:
            # Unforeseen, and still a failure to load: the caller that happened
            # to trigger this attempt must get "unavailable", not a crash.
            self._failed("unexpected", exc)
            return
        self._credentials = credentials
        self._recovered()

    def _build(self) -> GitHubAppCredentials:
        try:
            config = AppConfig.from_env(self._env)
        except GitHubAppError as exc:
            # `from_env` raises for one thing: a key file it could not read.
            reason = "key_file_unreadable" if isinstance(exc.__cause__, OSError) else "unexpected"
            raise _LoadFailed(reason) from exc
        if config is None:
            # The App ID is set — that is what configured means — so the key is
            # what is missing: none inline, no path, or an empty file.
            raise _LoadFailed("key_missing")
        try:
            # Local, and discarded: an unusable key fails here rather than at
            # the first installation token, where it would look like GitHub.
            build_app_jwt(config)
        except GitHubAppError as exc:
            raise _LoadFailed("key_invalid") from exc
        return self._credentials_factory(config)

    def _failed(self, reason: str, exc: BaseException | None) -> None:
        if self._unavailable_since is None:
            self._unavailable_since = self._clock()
        # Once per reason. The state stands until it changes, and an ERROR every
        # thirty seconds would bury the one line that says what happened.
        if reason == self._reason:
            return
        self._reason = reason
        logger.error(
            "source_control_load_failed",
            extra={
                "event": "source_control_load_failed",
                "provider": "github",
                "reason": reason,
                "retry_in_seconds": self._retry_interval_s,
            },
            # The key file's path and the OS error are in here, which is what an
            # operator needs. The key itself never is: neither `open` nor the
            # signing library puts file contents in an exception.
            exc_info=exc,
        )

    def _recovered(self) -> None:
        if self._unavailable_since is not None:
            logger.info(
                "source_control_load_recovered",
                extra={
                    "event": "source_control_load_recovered",
                    "provider": "github",
                    "unavailable_seconds": round(self._clock() - self._unavailable_since, 1),
                },
            )
        self._reason = None
        self._unavailable_since = None


class GitLabOAuthLoad:
    """GitLab OAuth, refreshed from saved settings when used."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._dynamic = env is None
        self._env = dict(env) if env is not None else settings_environment()
        self._lock = threading.Lock()
        self._client: GitLabOAuthClient | None = None
        self._load_status: LoadStatus = "unconfigured"
        self._load(self._env)

    @property
    def client(self) -> GitLabOAuthClient | None:
        """Return the client built from the current saved provider configuration."""
        self._refresh_settings()
        return self._client

    @property
    def load_status(self) -> LoadStatus:
        """Return the current configuration's load result."""
        self._refresh_settings()
        return self._load_status

    def _refresh_settings(self) -> None:
        if not self._dynamic:
            return
        env = settings_environment()
        if all(env.get(key) == self._env.get(key) for key in _GITLAB_ENV_KEYS):
            return
        with self._lock:
            env = settings_environment()
            if all(env.get(key) == self._env.get(key) for key in _GITLAB_ENV_KEYS):
                return
            self._env = env
            self._load(env)

    def _load(self, env: Mapping[str, str]) -> None:
        self._client = None
        self._load_status = "unconfigured"
        try:
            config = GitLabOAuthConfig.from_env(dict(env))
            if config is None:
                return
            self._client = GitLabOAuthClient(
                config, cipher=SourceControlCipher(env.get(ENV_ENCRYPTION_KEY, ""))
            )
            self._load_status = "ready"
        except Exception as exc:
            self._load_status = "unavailable"
            logger.error(
                "source_control_load_failed",
                extra={
                    "event": "source_control_load_failed",
                    "provider": "gitlab",
                    "reason": (
                        "invalid_configuration"
                        if isinstance(exc, SourceControlError)
                        else "unexpected"
                    ),
                },
                exc_info=True,
            )


class SourceControlRuntime:
    """Both providers, as the lifespan builds them into ``app.state.source_control``."""

    def __init__(self, *, github: GitHubAppProvider, gitlab: GitLabOAuthLoad) -> None:
        self.github = github
        self.gitlab = gitlab

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SourceControlRuntime:
        """Load saved provider settings, or an explicitly fixed environment."""
        return cls(github=GitHubAppProvider(env), gitlab=GitLabOAuthLoad(env))

    async def aclose(self) -> None:
        """Close what the providers opened. GitLab opens a client per call."""
        await self.github.aclose()


__all__ = [
    "ENV_GITHUB_APP_ID",
    "RETRY_INTERVAL_S",
    "GitHubAppProvider",
    "GitHubAppSource",
    "GitHubAppState",
    "GitLabOAuthLoad",
    "LoadStatus",
    "SourceControlRuntime",
    "SourceControlUnavailable",
]
