"""What the GitHub relay lets a conversation do as its owner.

Pure decisions, no I/O, so every rule is testable on its own. The relay
(:mod:`cloud_agent.api.github_relay`) asks :func:`decide` about each request and
:func:`check_push_commands` about each push.

**Three outcomes, and only one of them speaks as the user.** A request that
targets the conversation's own repository and is on the allowlist is sent with
the owner's GitHub token (``CREDENTIAL``). A request that is not about that
repository is forwarded with no credential at all (``ANONYMOUS``), so an agent
can still read public repositories and pages, but as nobody. A request that
targets the conversation's repository and is not allowed is refused
(``REFUSE``): a repository's administration, its secrets and branch deletion
stay out of reach.

The repository check is the whole reason the user's token may be used here.
GitHub cannot narrow a user token to one repository, so this module does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs

#: The two names the relay answers for. Everything else on GitHub
#: (``codeload``, ``raw.githubusercontent.com``, release assets) is reached
#: directly over the public egress, anonymously, as before.
WEB_HOST = "github.com"
API_HOST = "api.github.com"
RELAY_HOSTS = frozenset({WEB_HOST, API_HOST})

GRAPHQL_REFUSAL = (
    "GitHub GraphQL is not available from FreeInference sessions: a GraphQL "
    "request can name any repository, and this session may only act on its own. "
    "Use the REST API instead, for example `gh api repos/{owner}/{repo}/pulls`."
)


class Outcome(Enum):
    """What the relay does with one request."""

    CREDENTIAL = "credential"
    ANONYMOUS = "anonymous"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Decision:
    """The outcome, why, and what the relay must still check or record."""

    outcome: Outcome
    reason: str = ""
    #: A ``git-receive-pack`` body: its ref updates are checked before the pack
    #: is forwarded (:func:`check_push_commands`).
    inspect_push: bool = False
    #: ``POST /repos/{owner}/{repo}/pulls``: GitHub's answer is recorded as the
    #: conversation's pull request.
    records_pull_request: bool = False
    #: ``POST /repos/{owner}/{repo}/git/refs``: the body's ``ref`` must be a branch.
    inspect_ref_creation: bool = False


_NAME = r"[A-Za-z0-9_.-]{1,100}"
_SMART_HTTP = re.compile(
    rf"^/(?P<owner>{_NAME})/(?P<repo>{_NAME}?)(?:\.git)?/"
    r"(?P<service>info/refs|git-upload-pack|git-receive-pack)$"
)
_API_REPO = re.compile(rf"^/repos/(?P<owner>{_NAME})/(?P<repo>{_NAME})(?P<rest>/.*)?$")

#: Writes the owner's token may make in the conversation's repository: the
#: work of opening, updating, reviewing, checking and merging a pull request,
#: and of the issues around it. Matched against the path after
#: ``/repos/{owner}/{repo}``. Anything not listed is refused.
_N = r"[0-9]+"
_WRITE_ALLOWLIST: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(methods), re.compile(f"^{pattern}$"))
    for methods, pattern in (
        ({"POST"}, r"/pulls"),
        ({"PATCH"}, rf"/pulls/{_N}"),
        ({"PUT"}, rf"/pulls/{_N}/(merge|update-branch)"),
        ({"POST", "DELETE"}, rf"/pulls/{_N}/requested_reviewers"),
        ({"POST"}, rf"/pulls/{_N}/reviews"),
        ({"PUT", "DELETE"}, rf"/pulls/{_N}/reviews/{_N}"),
        ({"POST", "PUT"}, rf"/pulls/{_N}/reviews/{_N}/(events|dismissals)"),
        ({"POST"}, rf"/pulls/{_N}/comments(/{_N}/replies)?"),
        ({"PATCH", "DELETE"}, rf"/pulls/comments/{_N}"),
        ({"POST"}, r"/issues"),
        ({"PATCH"}, rf"/issues/{_N}"),
        ({"POST"}, rf"/issues/{_N}/comments"),
        ({"PATCH", "DELETE"}, rf"/issues/comments/{_N}"),
        ({"POST", "PUT", "DELETE"}, rf"/issues/{_N}/labels(/[^/]+)?"),
        ({"POST", "DELETE"}, rf"/issues/{_N}/assignees"),
        ({"POST"}, rf"/(issues|pulls)/({_N}|comments/{_N})/reactions"),
        ({"POST"}, r"/statuses/[0-9a-fA-F]{40}"),
        ({"POST"}, r"/check-runs"),
        ({"PATCH"}, rf"/check-runs/{_N}"),
        ({"POST"}, rf"/check-suites/{_N}/rerequest"),
        ({"POST"}, rf"/actions/runs/{_N}/(rerun|rerun-failed-jobs|cancel)"),
        ({"POST"}, rf"/actions/jobs/{_N}/rerun"),
        ({"POST"}, r"/actions/workflows/[^/]+/dispatches"),
        ({"POST"}, r"/git/(blobs|trees|commits|refs)"),
        ({"PATCH"}, r"/git/refs/heads/.+"),
        ({"POST"}, r"/merges"),
    )
)

#: Reads the owner's token may not make even in its own repository: they
#: describe or reach the repository's administration and secrets.
_READ_DENYLIST = re.compile(
    r"^/(hooks|keys|invitations|collaborators/[^/]+/permission|"
    r"actions/(secrets|variables|permissions|runners|oidc)|"
    r"(environments/[^/]+/)?(secrets|variables)|dependabot/secrets|codespaces/secrets|"
    r"branches/[^/]+/protection|rulesets|rules/branches|autolinks|"
    r"pages/deployments|deployments/[^/]+/statuses)(/|$)"
)

#: Non-repository endpoints answered with the owner's token: who am I, and how
#: much quota is left. ``gh`` asks both before it does anything.
_ACCOUNT_READS = frozenset({"/user", "/rate_limit"})


def _same_repo(owner: str, repo: str, session_repo: str | None) -> bool:
    """GitHub names are case-insensitive; ``.git`` is a transport suffix."""
    if not session_repo:
        return False
    name = repo[:-4] if repo.lower().endswith(".git") else repo
    return f"{owner}/{name}".casefold() == session_repo.casefold()


def decide(*, host: str, method: str, path: str, query: str, session_repo: str | None) -> Decision:
    """Classify one request the relay received for ``host``.

    ``session_repo`` is the conversation's ``owner/name``, or ``None`` for a
    conversation without a repository, which therefore never speaks as anyone.
    """
    method = method.upper()
    if host == WEB_HOST:
        return _decide_web(method=method, path=path, query=query, session_repo=session_repo)
    if host == API_HOST:
        return _decide_api(method=method, path=path, session_repo=session_repo)
    return Decision(Outcome.REFUSE, f"the GitHub relay does not serve {host!r}")


def _decide_web(*, method: str, path: str, query: str, session_repo: str | None) -> Decision:
    match = _SMART_HTTP.match(path)
    if match is None or not _same_repo(match["owner"], match["repo"], session_repo):
        # A web page, or another repository: as nobody.
        return Decision(Outcome.ANONYMOUS)
    service = match["service"]
    if service == "info/refs":
        wanted = parse_qs(query).get("service", [""])
        if method not in {"GET", "HEAD"} or wanted[0] not in {
            "git-upload-pack",
            "git-receive-pack",
        }:
            return Decision(Outcome.REFUSE, "only git's smart HTTP protocol is relayed")
        return Decision(Outcome.CREDENTIAL)
    if method != "POST":
        return Decision(Outcome.REFUSE, "only git's smart HTTP protocol is relayed")
    if service == "git-receive-pack":
        return Decision(Outcome.CREDENTIAL, inspect_push=True)
    return Decision(Outcome.CREDENTIAL)


def _decide_api(*, method: str, path: str, session_repo: str | None) -> Decision:
    if path.rstrip("/") == "/graphql":
        return Decision(Outcome.REFUSE, GRAPHQL_REFUSAL)
    if path in _ACCOUNT_READS and method in {"GET", "HEAD"}:
        return Decision(Outcome.CREDENTIAL)
    match = _API_REPO.match(path)
    if match is None or not _same_repo(match["owner"], match["repo"], session_repo):
        return Decision(Outcome.ANONYMOUS)
    rest = match["rest"] or ""
    if method in {"GET", "HEAD"}:
        if _READ_DENYLIST.match(rest):
            return Decision(
                Outcome.REFUSE, "a repository's administration and secrets are not relayed"
            )
        return Decision(Outcome.CREDENTIAL)
    for methods, pattern in _WRITE_ALLOWLIST:
        if method in methods and pattern.match(rest):
            return Decision(
                Outcome.CREDENTIAL,
                records_pull_request=(method == "POST" and rest == "/pulls"),
                inspect_ref_creation=(method == "POST" and rest == "/git/refs"),
            )
    return Decision(
        Outcome.REFUSE,
        f"{method} {rest or '/'} is not something this session may do to its repository",
    )


# -- git push ---------------------------------------------------------------

_ZERO_OID = re.compile(r"^0+$")
_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
#: The ref-update section of a push is a few lines per ref; anything this large
#: before the flush packet is not a push the relay will read on.
MAX_PUSH_COMMAND_BYTES = 1 << 20


class PushRefused(Exception):
    """A push the relay will not forward, with the reason git shows the agent."""


@dataclass(frozen=True)
class RefUpdate:
    """One ``<old> <new> <ref>`` line of a push."""

    old: str
    new: str
    ref: str


def split_push_commands(data: bytes) -> tuple[list[RefUpdate], int] | None:
    """Read the ref updates at the head of a ``git-receive-pack`` body.

    Returns the updates and how many bytes of ``data`` they took, through the
    flush packet that ends them, or ``None`` if ``data`` does not hold the
    whole section yet. Raises :class:`PushRefused` for anything that is not a
    well-formed command section.
    """
    offset = 0
    updates: list[RefUpdate] = []
    while True:
        if len(data) - offset < 4:
            return None
        try:
            length = int(data[offset : offset + 4], 16)
        except ValueError as exc:
            raise PushRefused("the push is not in git's pkt-line format") from exc
        if length == 0:
            return updates, offset + 4
        if length < 4:
            raise PushRefused("the push is not in git's pkt-line format")
        if len(data) - offset < length:
            return None
        line = data[offset + 4 : offset + length]
        offset += length
        # Capabilities follow a NUL on the first command; a trailing LF is optional.
        text = line.split(b"\0", 1)[0].rstrip(b"\n").decode("utf-8", "replace")
        if text.startswith("shallow "):
            continue
        parts = text.split(" ")
        if len(parts) != 3 or not _OID.match(parts[0]) or not _OID.match(parts[1]):
            raise PushRefused("the push names a ref update git does not send")
        updates.append(RefUpdate(old=parts[0], new=parts[1], ref=parts[2]))


def check_push_commands(updates: list[RefUpdate]) -> None:
    """Refuse a push that deletes a ref or writes anything but a branch.

    Which branch is not decided here: like a local push, that is the
    repository's branch protection to decide. A force push of a branch is
    allowed, because an agent rebasing its own branch is ordinary work.
    """
    if not updates:
        raise PushRefused("the push updates no ref")
    for update in updates:
        if _ZERO_OID.match(update.new):
            raise PushRefused(f"deleting {update.ref} is not allowed from this session")
        if not update.ref.startswith("refs/heads/") or update.ref == "refs/heads/":
            raise PushRefused(f"only branches may be pushed from this session, not {update.ref}")


def check_ref_creation(ref: object) -> None:
    """``POST git/refs`` may create a branch, never a tag or another ref."""
    if not isinstance(ref, str) or not ref.startswith("refs/heads/") or ref == "refs/heads/":
        raise PushRefused("only branches may be created from this session")
