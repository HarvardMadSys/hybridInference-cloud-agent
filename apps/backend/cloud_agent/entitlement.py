"""Which repositories a job may target (issue #1041).

The design gives the platform the security boundary and grades credentials by
blast radius. Without a check here both gradings leak in the same way: the job
carries a repository *the requester chose*, and the platform later mints a real
installation token for that string — a read token for the runner to clone with,
and a write token for the publisher to push with. Neither asks whose repository
it is. A user could therefore name any repository the App happens to be
installed on, have the platform clone it into a sandbox, and read the contents
back through their own job's events and retained workspace. That is a confused
deputy: the requester supplies the target, the platform supplies the authority.

This is a single-tenant dogfood ("repo == our own"), so the entitlement is an
explicit deployment allowlist rather than a per-user GitHub identity. That is
the honest shape for the stage: it fails closed, it needs no OAuth linkage that
does not exist yet, and it makes the multi-tenant version an obvious next step
(replace :func:`repo_is_allowed` with a per-user installation lookup) instead of
a hole nobody wrote down.

Unset means *nothing* is allowed. A deployment that has not been configured
must refuse jobs, not accept every repository on the internet.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from cloud_agent.github_app import UserAuthorizationRevoked
from cloud_agent.github_user_token import forget_revoked_token, user_access_token
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import setting_env

if TYPE_CHECKING:
    from cloud_agent.ports import SourceControlRepository

logger = get_logger(__name__)

# `owner/name`, the only shape GitHub uses and the only one the publisher and
# runner know how to handle. Also keeps a leading `-` out of git's argv.
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
_REPO_RE = re.compile(REPO_PATTERN)

_ALLOWLIST_VAR = "AGENT_REPO_ALLOWLIST"


class RepoNotAllowed(Exception):
    """Raised when a job names a repository this deployment will not work on."""


class GitHubReconnectRequired(RepoNotAllowed):
    """The user connected GitHub, but GitHub can no longer be asked as them.

    A connection made before users' tokens were kept, or one whose authorization
    GitHub has since refused. A refusal like any other — every caller that
    handles :class:`RepoNotAllowed` stays closed — with a message that says what
    fixes it.
    """


def allowed_repos(env: dict[str, str] | None = None) -> list[str]:
    """Return the configured allowlist, lowercased.

    Entries are exact ``owner/name`` values, or ``owner/*`` to allow every
    repository under one owner — enough for "our own org" without becoming a
    pattern language nobody can audit.
    """
    raw = (env.get(_ALLOWLIST_VAR) if env is not None else setting_env(_ALLOWLIST_VAR)) or ""
    return [entry.strip().lower() for entry in raw.split(",") if entry.strip()]


def repo_is_allowed(repo: str, env: dict[str, str] | None = None) -> bool:
    """Whether ``repo`` is one this deployment is configured to work on."""
    if not _REPO_RE.match(repo or ""):
        return False
    candidate = repo.lower()
    owner = candidate.split("/", 1)[0]
    return any(entry == candidate or entry == f"{owner}/*" for entry in allowed_repos(env))


def require_allowed_repo(repo: str, env: dict[str, str] | None = None) -> None:
    """Raise :class:`RepoNotAllowed` unless ``repo`` is entitled.

    Called both when the job is created and again when a credential is about to
    be minted for it. The second call is not redundant: a row written before
    this check existed, or while the allowlist was wider, must not be able to
    produce a token now.
    """
    if not _REPO_RE.match(repo or ""):
        raise RepoNotAllowed(f"repo must be 'owner/name', got {repo!r}")
    if not repo_is_allowed(repo, env):
        configured = allowed_repos(env)
        detail = (
            f"{_ALLOWLIST_VAR} is not configured, so no repository is allowed"
            if not configured
            else f"{repo!r} is not in {_ALLOWLIST_VAR}"
        )
        raise RepoNotAllowed(f"This deployment will not run agent jobs against {repo!r}: {detail}.")


async def _connected_repos(
    user_id: str, *, store: SourceControlRepository, app_credentials: Any
) -> tuple[set[str], bool]:
    """The repositories this user's GitHub connection reaches, and whether to reconnect.

    For each installation the user connected, GitHub is asked *as the user*
    which of its repositories they can push to. It used to be asked with the
    installation's own token, which answers with every repository the
    installation covers: GitHub reports an organization's installation to
    anyone with access to one of its repositories, so an outside collaborator
    on one repository was offered all the others the App could see.
    """
    grants = await store.list_repo_grants(user_id=user_id)
    if not grants:
        return set(), False
    installation_ids = tuple(sorted(int(grant["installation_id"]) for grant in grants))
    cached = app_credentials.cached_user_repositories(user_id, installation_ids)
    if cached is not None:
        return set(cached), False
    token = await user_access_token(user_id, store=store, app_credentials=app_credentials)
    if token is None:
        return set(), True
    repos: set[str] = set()
    complete = True
    for installation_id in installation_ids:
        try:
            names = await app_credentials.writable_repositories_for_user(
                token.value, installation_id
            )
        except UserAuthorizationRevoked:
            await forget_revoked_token(user_id, token, store=store)
            return set(), True
        except Exception:
            # One unreachable installation must not hide the others, and must
            # not widen anything either — it simply contributes nothing, and
            # an answer missing it is not remembered.
            complete = False
            logger.warning(
                "agent_github_user_repositories_unavailable",
                extra={"event": "agent_github_user_repositories_unavailable"},
            )
            continue
        repos.update(name for name in names if _REPO_RE.match(name or ""))
    if complete:
        app_credentials.remember_user_repositories(user_id, installation_ids, sorted(repos))
    return repos, False


async def repos_for_user(
    user_id: str,
    *,
    store: SourceControlRepository | None = None,
    app_credentials: Any = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Every repository this user may target, from both sources of entitlement.

    Two independent ones, and a repository qualifies on either:

    - **A connection the user made.** They authorized the App, GitHub told us
      which installations they can reach, and — asked as them — which of those
      installations' repositories they can push to. This is the design's "user
      authorizes the repo", and it is an entitlement because every step of it
      is GitHub's answer rather than the requester's.
    - **The deployment allowlist.** The single-tenant dogfood, where the repo
      is the operator's own and there is no user to connect.

    Both fail closed: with no connection and no allowlist the answer is empty,
    and an empty answer means no job can be created at all. So does a
    connection GitHub cannot be asked as the user about any more.
    """
    # Only concrete entries seed the list a picker shows; a wildcard is a rule,
    # not a repository, and `require_entitled_repo` matches it separately.
    repos: set[str] = {entry for entry in allowed_repos(env) if not entry.endswith("/*")}
    if store is None or app_credentials is None:
        return sorted(repos)
    try:
        connected, _ = await _connected_repos(user_id, store=store, app_credentials=app_credentials)
    except Exception:
        return sorted(repos)
    repos.update(connected)
    return sorted(repos)


async def require_entitled_repo(
    repo: str,
    user_id: str,
    *,
    store: SourceControlRepository | None = None,
    app_credentials: Any = None,
    env: dict[str, str] | None = None,
) -> None:
    """Raise :class:`RepoNotAllowed` unless this user may target ``repo``."""
    if not _REPO_RE.match(repo or ""):
        raise RepoNotAllowed(f"repo must be 'owner/name', got {repo!r}")
    # The allowlist half first, so an `owner/*` rule is honoured without needing
    # to enumerate every repository under that owner.
    if repo_is_allowed(repo, env):
        return
    connected: set[str] = set()
    reconnect = False
    if store is not None and app_credentials is not None:
        try:
            connected, reconnect = await _connected_repos(
                user_id, store=store, app_credentials=app_credentials
            )
        except Exception:
            connected = set()
    if any(entry.lower() == repo.lower() for entry in connected):
        return
    if reconnect:
        raise GitHubReconnectRequired(
            f"Reconnect GitHub to work on {repo!r}: repositories are now checked "
            "against your own GitHub access, which this connection cannot show."
        )
    raise RepoNotAllowed(
        f"You have not connected {repo!r} with access to push to it. Give the GitHub "
        "App access to that repository, or ask an operator to add it to this "
        "deployment's allowlist."
    )


__all__ = [
    "REPO_PATTERN",
    "GitHubReconnectRequired",
    "RepoNotAllowed",
    "allowed_repos",
    "repo_is_allowed",
    "repos_for_user",
    "require_allowed_repo",
    "require_entitled_repo",
]
