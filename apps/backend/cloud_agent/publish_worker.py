"""The publish step: immutable group snapshot → branch → draft PR.

This is the last hop of the chain and the one that produces the visible
product outcome. It runs only for a snapshot the workspace's owner asked for
(``request_snapshot_publication``): a turn boundary publishes nothing. It runs **in the gateway**, never in the sandbox, for one
reason: it is the only component that holds a GitHub credential. The agent
hands over a patch and nothing else, which is what lets the sandbox stay
credential-free.

Order of operations is load-bearing:

1. Claim one captured group snapshot. The claim is atomic (``SKIP LOCKED``
   plus a state transition to ``publishing``), so several control-plane
   processes cannot race into duplicate PRs.
2. Validate the patch **before** any bytes touch a worktree — the gate is the
   security boundary, and a blocked ``.github/`` change must never reach a
   branch, because pushing it can execute a workflow with repository secrets
   before a human reads the draft PR.
3. Push the group snapshot onto its stable ``agent/<origin-thread-id>`` branch
   with a pinned refspec.
4. Open a **draft** PR against the branch the job was started from, so a human
   reviews before anything can merge and the diff is the agent's own work. A
   later snapshot refreshes the branch's open PR and opens a new draft once
   someone has closed or merged it.

A rejected patch fails the snapshot and marks its group dirty without
rewriting successful Agent turns. It is not retried: a retry loop would either
spam the repository or bury the rejection. The owner's next publication
request captures the workspace again in its place. Every capture is the whole
diff from the published base, so that one carries the rejected changes too, and
is refused again if it still holds them.

A transient credential, git, or PR failure does not. The branch name and the
PR lookup are idempotent, so the snapshot returns to ``captured`` until its
``retry_at``. Five such defers is the budget; the next one fails the snapshot
so an outage cannot block the lineage forever.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import httpx

from cloud_agent.entitlement import RepoAccessUnavailable, RepoNotAllowed, require_entitled_repo
from cloud_agent.environments import EnvironmentEncryptionError
from cloud_agent.github_app import rate_limit_wait
from cloud_agent.logging_utils import get_logger
from cloud_agent.pr_titles import generate_pr_title, is_generic_pr_summary
from cloud_agent.publisher import (
    PatchRejected,
    PublishError,
    TransientPublishError,
    fetch_base_bundle,
    publish_patch,
)
from cloud_agent.schemas import BASE_REF_METADATA_KEY
from cloud_agent.settings import number_setting, setting_env
from cloud_agent.source_control_runtime import SourceControlUnavailable
from cloud_agent.task_titles import is_warmup_title
from cloud_agent.workers.background_wake import PUBLISH
from cloud_agent_shared.git.patch_gate import branch_name_for

if TYPE_CHECKING:
    from cloud_agent.grants import GrantClient
    from cloud_agent.ports import (
        HostPoolRepository,
        SnapshotRepository,
        SourceControlRepository,
    )
    from cloud_agent.ports.environments import EnvironmentRepository
    from cloud_agent.source_control_runtime import GitHubAppSource
    from cloud_agent.workers.background_wake import BackgroundWake


class PublishStorage(Protocol):
    """What publishing a group snapshot reaches the storage layer for.

    Two capabilities and no more: the snapshot rows it claims and records,
    and the host pool, which it needs only to find the sandbox manager holding the
    workspace it is publishing from.
    """

    snapshot: SnapshotRepository
    host_pool: HostPoolRepository
    source_control: SourceControlRepository
    environments: EnvironmentRepository


logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
POLL_INTERVAL_S = 15.0
#: The longest an idle publisher that is told about new work sleeps. It wakes
#: sooner for a notification or the earliest publication/base import retry.
PUBLISH_IDLE_S = 30.0
#: Refresh a live publication claim frequently enough that several sequential
#: provider/git calls at their 300-second maximum cannot resemble a crashed
#: publisher to the 900-second reaper.
_PUBLISH_HEARTBEAT_INTERVAL_S = 60.0

# Exactly what publishing does: push one branch, open one draft PR. Notably not
# `workflows`, so a patch touching `.github/` cannot be pushed even if both
# gates were bypassed — the credential itself would refuse.
_PUBLISH_SCOPE = {"contents": "write", "pull_requests": "write"}
#: How long the publishing token must still live when a publish starts. A
#: cached installation token is otherwise served until five minutes before it
#: expires, and one publish runs several network-bound commands and PR API
#: calls: a token with minutes left could expire after the branch was pushed,
#: and the PR call then fails with a 401, which is not retried.
_PUBLISH_TOKEN_MIN_TTL_S = 1800.0
#: How long the git thread waits for its commit to be recorded before the push.
#: A plain fenced update; a pool that cannot serve it in this time is an outage,
#: and the publication is retried rather than pushed unrecorded.
_PUSH_RECORD_TIMEOUT_S = 30.0

_HTTP_STATUS_IN_DETAIL = re.compile(r"\((\d{3})\)|returned error:\s*(\d{3})\b")
_GIT_DISCONNECT = re.compile(
    r"timed out|unable to access|could not resolve host|failed to connect|"
    r"connection (?:reset|refused|aborted)|network is unreachable|"
    r"temporary failure in name resolution|remote end hung up|early eof|"
    r"rpc failed|recv failure|send failure|could not read from remote|"
    r"connection closed|broken pipe",
    re.IGNORECASE,
)

# A PR base has to be a branch. `base_ref` is normally one (the composer offers
# a branch picker), but the API accepts any ref it can resolve, and a caller
# who passed a commit would otherwise turn a publishable job into a 422.
#
# Matched with `fullmatch`. The anchors make it equivalent today, and that is
# the point: `$` also matches before a trailing newline, so `match` would
# accept `abcdef1\n` — reachable only if the `.strip()` above ever goes away.
# A branch called `deadbee-release` is not affected either way.
_LOOKS_LIKE_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _retryable_github_status(status: int | None) -> bool:
    """Whether a provider refusal is conventionally safe to retry."""
    return status is not None and (status in {408, 429} or 500 <= status < 600)


def _github_api_error(operation: str, response: httpx.Response) -> PublishError:
    """Classify an API refusal without retaining an untrusted response body.

    A rate limit, which GitHub may answer with a 403, is retried after the wait
    it names.
    """
    detail = f"GitHub refused {operation} ({response.status_code})"
    wait = rate_limit_wait(response)
    if wait is not None:
        return TransientPublishError(detail, retry_after_s=wait)
    if _retryable_github_status(response.status_code):
        return TransientPublishError(detail)
    return PublishError(detail)


def _provider_wait(exc: BaseException) -> float | None:
    """How long GitHub asked to wait before a retry, when it refused for a rate limit."""
    wait = getattr(exc, "retry_after_s", None)
    return wait if isinstance(wait, (int, float)) else None


def _duplicate_pull_request_422(response: httpx.Response) -> bool:
    """Recognize GitHub's specific duplicate-open-PR validation response."""
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    messages = [payload.get("message")]
    errors = payload.get("errors")
    if isinstance(errors, dict):
        errors = [errors]
    if isinstance(errors, list):
        messages.extend(item.get("message") for item in errors if isinstance(item, dict))
    return any(
        isinstance(message, str) and "pull request already exists" in message.casefold()
        for message in messages
    )


def _invalid_pr_request_error(response: httpx.Response) -> PublishError:
    """Return a safe, actionable diagnostic for a PR validation error.

    The draft is opened right after the publisher pushed its branch, and GitHub
    can answer ``head invalid`` until that branch reaches its pull-request API.
    That refusal alone is transient: the snapshot goes back to the durable
    retry budget, whose replay finds the commit already pushed. An invalid base
    is the branch the owner chose, which no retry changes, so it stays
    permanent, also when GitHub names the head beside it, as does every other
    validation failure.
    """
    fields: set[str] = set()
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, dict):
            errors = [errors]
        if isinstance(errors, list):
            fields = set()
            for item in errors:
                field = item.get("field") if isinstance(item, dict) else None
                if isinstance(field, str) and field in {"head", "base"}:
                    fields.add(field)
    if fields == {"head"}:
        return TransientPublishError(
            "GitHub rejected the draft PR (422): the head branch is invalid or unavailable"
        )
    if fields:
        field = sorted(fields)[0]
        return PublishError(
            f"GitHub rejected the draft PR (422): the {field} branch is invalid or unavailable"
        )
    return PublishError(
        "GitHub rejected the draft PR (422): request validation failed; check branch names and PR fields"
    )


def _pr_base_branch(job: dict[str, Any], fallback: str) -> str:
    """The branch this job's PR should target.

    The job's own ``base_ref`` when it has one: that is the branch the owner
    picked, and the one ``base_sha`` was resolved from. Using the deployment
    default instead meant a job started from ``release/x`` opened its PR
    against ``dev``, which shows every commit between the two branches as part
    of the agent's change and proposes merging into the wrong line of
    development.

    Falls back rather than failing when the ref cannot be a base. The ref is a
    display fact recorded at creation; refusing to publish over it would throw
    away a finished job's work, and the default still opens a reviewable PR.
    """
    metadata = job.get("metadata")
    base_ref = metadata.get(BASE_REF_METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(base_ref, str) or not base_ref.strip():
        return fallback
    base_ref = base_ref.strip()
    if _LOOKS_LIKE_SHA.fullmatch(base_ref):
        return fallback
    return base_ref


@dataclass(frozen=True)
class GitHubCredential:
    """How the publisher authenticates to GitHub for one repository.

    ``token`` is expected to be short-lived (a GitHub App installation token,
    which expires in an hour) and scoped to the repositories the user actually
    installed the app on. It stays in this process: it is given to git through
    the environment and to the API in a header, and never reaches a sandbox.
    """

    token: str
    api_base: str = field(default_factory=lambda: setting_env("AGENT_GITHUB_API") or GITHUB_API)

    @property
    def git_base(self) -> str:
        """The Git host paired with ``api_base``, which minted the token.

        ``api.github.com`` serves github.com's repositories; any other API URL
        is answered for by its own host. The token goes back to that host
        alone: pushing to github.com regardless sent a token another host
        minted to github.com.
        """
        parts = urlsplit(self.api_base)
        netloc = parts.netloc.rpartition("@")[2]
        if parts.hostname == "api.github.com":
            netloc = "github.com"
        return f"{parts.scheme or 'https'}://{netloc}"

    def clone_url(self, repo: str) -> str:
        """Return the credential-free clone URL for ``owner/name``."""
        return f"{self.git_base}/{repo}.git"

    def git_auth_env(self) -> dict[str, str]:
        """Carry the token in git's environment, never in its arguments or config.

        ``GIT_CONFIG_COUNT`` sets the header for one command, the way the
        host's runner fetches. Scoped to the Git host's URL, so a request git
        makes anywhere else does not carry it.
        """
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{self.git_base}/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }


async def create_draft_pull_request(
    *,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    credential: GitHubCredential,
    timeout_s: float | None = None,
) -> str:
    """Open a draft PR and return its URL.

    Draft is not a nicety: the whole safety story assumes a human reads the
    change before it can merge, so this must never open a ready-for-review PR.
    """
    async with httpx.AsyncClient(
        timeout=timeout_s if timeout_s is not None else number_setting("publish_pr_timeout_s", 30.0)
    ) as client:
        response = await client.post(
            f"{credential.api_base}/repos/{repo}/pulls",
            headers=_github_headers(credential),
            json={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
                "draft": True,
            },
        )
    if response.status_code == 422:
        # An earlier create can have committed while its response was lost,
        # then remain briefly absent from the lookup. That known duplicate
        # response is uncertain and re-enters bounded reconciliation, as does a
        # head GitHub has not seen yet (see `_invalid_pr_request_error`); other
        # 422s are permanent validation failures.
        if _duplicate_pull_request_422(response):
            raise TransientPublishError(
                "GitHub reports that a pull request already exists; publication will reconcile"
            )
        raise _invalid_pr_request_error(response)
    if response.status_code >= 400:
        raise _github_api_error("the draft PR", response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TransientPublishError("GitHub returned an invalid draft PR response") from exc
    url = payload.get("html_url") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url.strip():
        # A 2xx with no usable URL is not a success this worker can record.
        # Returning "" put an empty `published_pr_url` on the job, which marks
        # it published — and the query this worker claims from selects on
        # `published_pr_url IS NULL`, so the job would never be retried and the
        # owner would never get a link. Failing is recoverable; that is not.
        raise TransientPublishError(
            f"GitHub accepted the draft PR ({response.status_code}) but returned no html_url"
        )
    return url


async def ensure_draft_pull_request(
    *,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    credential: GitHubCredential,
) -> str:
    """Refresh an open branch PR, or create a new draft after closure/merge.

    Look up the branch even on the first publication: a previous create may
    have reached GitHub before its URL could be recorded locally. Never infer
    that a saved URL still denotes an open PR.

    Looked up by head alone, and refreshed without touching its base: a
    reviewer who retargets the draft has decided where it merges, and filtering
    by the job's base made the next snapshot open a second draft beside it.
    ``base_branch`` is only for a draft this creates.
    """
    headers = _github_headers(credential)
    endpoint = f"{credential.api_base}/repos/{repo}/pulls"
    async with httpx.AsyncClient(timeout=number_setting("publish_pr_timeout_s", 30.0)) as client:
        response = await client.get(
            endpoint,
            headers=headers,
            params={"state": "open", "head": f"{repo.split('/')[0]}:{head_branch}"},
        )
        if response.status_code >= 400:
            raise _github_api_error("the PR lookup", response)
        try:
            pulls = response.json()
        except ValueError as exc:
            raise TransientPublishError("GitHub returned an invalid PR list") from exc
        if not isinstance(pulls, list):
            raise TransientPublishError("GitHub returned an invalid PR list")
        for pull in pulls:
            if not isinstance(pull, dict):
                raise TransientPublishError("GitHub returned an invalid PR list entry")
            if pull.get("state") != "open":
                continue
            number, url = pull.get("number"), pull.get("html_url")
            if not isinstance(number, int) or not isinstance(url, str) or not url.strip():
                raise TransientPublishError("GitHub returned an open PR without its number or URL")
            updated = await client.patch(
                f"{endpoint}/{number}", headers=headers, json={"title": title, "body": body}
            )
            if updated.status_code >= 400:
                raise _github_api_error("the PR update", updated)
            try:
                payload = updated.json()
            except ValueError as exc:
                raise TransientPublishError("GitHub returned an invalid PR update") from exc
            if not isinstance(payload, dict) or payload.get("state") not in {"open", "closed"}:
                raise TransientPublishError("GitHub returned an invalid PR update")
            if payload["state"] == "closed":
                # The owner can close/merge while publication is updating it.
                break
            return url
    return await create_draft_pull_request(
        repo=repo,
        head_branch=head_branch,
        base_branch=base_branch,
        title=title,
        body=body,
        credential=credential,
    )


def _github_headers(credential: GitHubCredential) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credential.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


_BACKTICK_RUN = re.compile(r"`+")


def _code_span(value: str) -> str:
    """Render agent-chosen text as one Markdown code span it cannot close early.

    A file name is the agent's to choose, and the App posts the body under its
    own name, so a name that closed its span could write mentions, closing
    keywords or links into it. A span ends at the first backtick run as long
    as the one that opened it, so the fence is one longer than any run inside,
    and padded when the text starts or ends with a backtick or a space (the
    renderer strips one space from each side). Control characters, which could
    end the list item with the span still open, are shown as escapes; the
    publisher's change-set gate refuses such names before this is reached.
    """
    text = "".join(
        ascii(char)[1:-1] if unicodedata.category(char).startswith("C") else char for char in value
    )
    fence = "`" * (max(map(len, _BACKTICK_RUN.findall(text)), default=0) + 1)
    if text[:1] in ("`", " ") or text[-1:] in ("`", " "):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def _pr_body(job: dict[str, Any], changed_files: list[str]) -> str:
    """Compose a PR body that tells the reviewer what they are reviewing."""
    files = "\n".join(f"- {_code_span(path)}" for path in changed_files[:50])
    if len(changed_files) > 50:
        files += f"\n- …and {len(changed_files) - 50} more"
    included = ", ".join(f"`{item}`" for item in job.get("included_job_ids") or [])
    attribution = (
        f"Opened from shared sandbox snapshot `{job['snapshot_id']}` "
        f"(workspace epoch {job['workspace_epoch']}).\n\n"
        f"**Included settled turns**\n\n{included or '`unknown`'}\n\n"
        "The chats share one workspace, so this diff is a group snapshot and is not "
        "attributed to only one chat.\n\n"
    )
    return (
        attribution + f"**Task**\n\n> {job['task_prompt'][:1500]}\n\n"
        f"**Changed files ({len(changed_files)})**\n\n{files}\n\n"
        "---\n"
        "This branch was produced by an agent running in a sandbox with no "
        "repository credential: it emitted a patch, which the platform "
        "validated (workflow-file block, secret scan, path-escape and size "
        "limits) before pushing. It is a draft on purpose — please review "
        "before merging."
    )


def _summary_line(value: Any) -> str | None:
    """Read the opening prose of a closing message, without Markdown scaffolding."""
    if not isinstance(value, str) or is_warmup_title(value):
        return None
    in_code = False
    for line in value[:4000].splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or line.startswith("<"):
            continue
        if any(unicodedata.category(char).startswith("C") and char != "\t" for char in line):
            continue
        line = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s+)", "", line)
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = " ".join(line.split()).strip(" *#")
        if (
            not any(char.isalnum() for char in line)
            or line.endswith(":")
            or is_generic_pr_summary(line)
            or line == "(the agent finished without a closing message)"
        ):
            continue
        return line
    return None


def _pr_title(job: dict[str, Any], changed_files: list[str]) -> str:
    """Name completed snapshot work from its included turns' closing summaries.

    The task prompt describes requested work; a closing message describes what
    the agent actually did. Shared snapshots can include several chats, so keep
    a summary from each rather than naming only the anchor chat's instruction.
    """
    summaries = job.get("session_summaries")
    lines = (
        dict.fromkeys(line for summary in summaries if (line := _summary_line(summary)) is not None)
        if isinstance(summaries, list)
        else {}
    )
    # Share the title budget across summaries when several chats settle together.
    fragments = list(lines)
    if len(fragments) > 5:
        fragments = [*fragments[:4], f"+{len(fragments) - 4} more"]
    budget = (70 - 2 * (len(fragments) - 1)) // max(1, len(fragments))
    summary = "; ".join(
        line if len(line) <= budget else line[: budget - 1].rstrip() + "…" for line in fragments
    )
    if not summary:
        summary = (
            f"Update {', '.join(changed_files)}" if changed_files else "Update shared workspace"
        )
        summary = " ".join(summary.split())
    if len(summary) > 70:
        summary = summary[:69].rstrip() + "…"
    return f"[agent] {summary}"


async def _import_snapshot_base(
    store: PublishStorage,
    *,
    snapshot: dict[str, Any],
    commit_sha: str,
    bundle: bytes | None,
) -> bool:
    """Import a published commit into Docker without changing HEAD or files."""
    snapshot_id = int(snapshot["snapshot_id"])
    if not bundle:
        await store.snapshot.record_snapshot_base_import_failure(
            snapshot_id=snapshot_id,
            detail="the publisher produced no retained base bundle",
        )
        return False
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    sandbox_manager = await sandbox_manager_for_job(store.host_pool, snapshot["anchor_job_id"])
    if sandbox_manager is None:
        await store.snapshot.record_snapshot_base_import_failure(
            snapshot_id=snapshot_id,
            detail="the owning sandbox manager is unavailable for base import",
        )
        return False
    try:
        await sandbox_manager.import_shared_base(snapshot, commit_sha=commit_sha, bundle=bundle)
    except Exception as exc:
        await store.snapshot.record_snapshot_base_import_failure(
            snapshot_id=snapshot_id,
            detail=f"published base import failed: {exc}",
        )
        return False
    return await store.snapshot.complete_snapshot_base_import(
        snapshot_id=snapshot_id,
        commit_sha=commit_sha,
    )


async def _retry_due_base_imports(
    store: PublishStorage,
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
) -> None:
    """Retry every import that is due, past any that fails again.

    A failure schedules that import's next try, so it is not handed out again
    in this pass. Should its schedule not be recorded, it is left for the next
    pass rather than retried in a loop.
    """
    tried: set[int] = set()
    while (snapshot := await store.snapshot.pending_snapshot_base_import()) is not None:
        snapshot_id = int(snapshot["snapshot_id"])
        if snapshot_id in tried:
            return
        tried.add(snapshot_id)
        await _retry_base_import(
            store, snapshot, credential=credential, app_credentials=app_credentials
        )


async def _retry_base_import(
    store: PublishStorage,
    snapshot: dict[str, Any],
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
) -> bool:
    """Import one published base again; a failure is recorded on its snapshot.

    The bundle is cut from the base the group's first snapshot started from
    (``origin_base_sha``), not from the previous published base. A published
    commit is imported as objects no ref names, so Git prunes it together with
    the commits published before it; the workspace's own history keeps the
    commit it started from, and every published commit descends from that.
    """
    try:
        await require_entitled_repo(
            snapshot["repo"],
            snapshot.get("user_id", ""),
            store=store.source_control,
            app_credentials=app_credentials,
        )
        if app_credentials is not None:
            credential = GitHubCredential(
                await app_credentials.token_for(
                    snapshot["repo"],
                    permissions=_PUBLISH_SCOPE,
                    repository_scoped=True,
                    min_ttl_s=_PUBLISH_TOKEN_MIN_TTL_S,
                )
            )
        if credential is None:
            raise PublishError("no GitHub credential is configured for base import")
        commit_sha = str(snapshot["published_commit_sha"])
        bundle = await asyncio.to_thread(
            fetch_base_bundle,
            clone_url=credential.clone_url(snapshot["repo"]),
            commit_sha=commit_sha,
            auth_env=credential.git_auth_env(),
            base_sha=snapshot.get("origin_base_sha") or snapshot.get("base_sha") or None,
        )
        return await _import_snapshot_base(
            store,
            snapshot=snapshot,
            commit_sha=commit_sha,
            bundle=bundle,
        )
    except Exception as exc:
        await store.snapshot.record_snapshot_base_import_failure(
            snapshot_id=int(snapshot["snapshot_id"]),
            detail=f"published base import retry failed: {exc}",
            delay_s=_provider_wait(exc),
        )
        return False


async def publish_one(
    store: PublishStorage,
    *,
    credential: GitHubCredential | None = None,
    app_credentials: Any | None = None,
    grants: GrantClient | None = None,
    base_branch: str | None = None,
    allow_workflow_changes: bool = False,
) -> str | None:
    """Publish at most one pending job. Returns the PR URL, or ``None``.

    ``base_branch`` is the deployment default, used only for a job that did not
    name a branch of its own — see :func:`_pr_base_branch`.

    Never raises for an individual job: a failure is recorded against that job
    so the owner sees why, and the loop stays alive for the next one.
    """
    job = await store.snapshot.claim_for_publish()
    if job is None:
        return None
    return await _publish_claimed(
        store,
        job,
        credential=credential,
        app_credentials=app_credentials,
        grants=grants,
        base_branch=base_branch
        if base_branch is not None
        else setting_env("AGENT_PUBLISH_BASE_BRANCH") or "dev",
        allow_workflow_changes=allow_workflow_changes,
    )


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status on a credential or PR failure, if it has one."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    if isinstance(exc, PublishError):
        match = _HTTP_STATUS_IN_DETAIL.search(str(exc))
        if match:
            return int(next(group for group in match.groups() if group))
    return None


def _is_retryable_publication_failure(exc: BaseException) -> bool:
    """Whether this failure may be deferred instead of failing the snapshot.

    Retryable: an explicitly uncertain remote effect; a credential mint that
    did not connect, timed out, or got 408/429/5xx; a git disconnect; or the
    same HTTP statuses from the remote. Patch, content, and other HTTP
    rejections are permanent.
    """
    if isinstance(exc, (TransientPublishError, EnvironmentEncryptionError)):
        return True
    if isinstance(exc, PatchRejected):
        # Its detail quotes the patch's own paths, not a status or a disconnect.
        return False
    if _provider_wait(exc) is not None:
        # A rate limit, whatever its status: waiting lifts it.
        return True
    status = _http_status(exc)
    if _retryable_github_status(status):
        return True
    if status is not None:
        return False
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    return isinstance(exc, PublishError) and _GIT_DISCONNECT.search(str(exc)) is not None


async def _publish_due(
    store: PublishStorage,
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
    grants: GrantClient | None = None,
    base_branch: str,
) -> None:
    """Publish every captured snapshot, past any that fails.

    A refusal marks its snapshot failed. A retryable failure, including an
    entitlement outage, returns it to the queue until five defers are spent.
    Snapshots behind either still go out in this pass.
    """
    while (job := await store.snapshot.claim_for_publish()) is not None:
        await _publish_claimed(
            store,
            job,
            credential=credential,
            app_credentials=app_credentials,
            grants=grants,
            base_branch=base_branch,
        )


async def _heartbeat_snapshot_publish(
    store: PublishStorage,
    *,
    snapshot_id: int,
    expected_publish_attempt_count: int,
) -> None:
    """Keep this exact claim live while remote publication is in progress."""
    while True:
        await asyncio.sleep(_PUBLISH_HEARTBEAT_INTERVAL_S)
        try:
            renewed = await store.snapshot.renew_snapshot_publish_claim(
                snapshot_id=snapshot_id,
                expected_publish_attempt_count=expected_publish_attempt_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A brief database interruption must not abandon a claim while its
            # remote side effect is still running. The next one-minute beat
            # retries well inside the stalled-publisher window.
            logger.warning(
                "agent_snapshot_publish_heartbeat_failed",
                extra={
                    "event": "agent_snapshot_publish_heartbeat_failed",
                    "snapshot_id": snapshot_id,
                    "publish_attempt_count": expected_publish_attempt_count,
                },
                exc_info=True,
            )
            continue
        if not renewed:
            logger.warning(
                "agent_snapshot_publish_claim_lost",
                extra={
                    "event": "agent_snapshot_publish_claim_lost",
                    "snapshot_id": snapshot_id,
                    "publish_attempt_count": expected_publish_attempt_count,
                },
            )
            return


async def _publish_claimed(
    store: PublishStorage,
    job: dict[str, Any],
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
    grants: GrantClient | None = None,
    base_branch: str,
    allow_workflow_changes: bool = False,
) -> str | None:
    """Publish under a claim heartbeat spanning every local and remote phase."""
    snapshot_id = int(job["snapshot_id"])
    expected_publish_attempt_count = int(job["publish_attempt_count"])
    if not await store.snapshot.renew_snapshot_publish_claim(
        snapshot_id=snapshot_id,
        expected_publish_attempt_count=expected_publish_attempt_count,
    ):
        logger.warning(
            "agent_snapshot_publish_claim_lost",
            extra={
                "event": "agent_snapshot_publish_claim_lost",
                "snapshot_id": snapshot_id,
                "publish_attempt_count": expected_publish_attempt_count,
            },
        )
        return None
    heartbeat = asyncio.create_task(
        _heartbeat_snapshot_publish(
            store,
            snapshot_id=snapshot_id,
            expected_publish_attempt_count=expected_publish_attempt_count,
        ),
        name=f"snapshot-publish-heartbeat-{snapshot_id}",
    )
    try:
        return await _publish_claimed_owned(
            store,
            job,
            credential=credential,
            app_credentials=app_credentials,
            grants=grants,
            base_branch=base_branch,
            allow_workflow_changes=allow_workflow_changes,
        )
    except asyncio.CancelledError:
        # A control-plane stop must not leave this claim `publishing` until the
        # stalled-publish reaper reclaims it fifteen minutes later. Replaying a
        # publication is safe however far it got -- an already pushed commit is
        # reused rather than force-pushed, and the draft is found by its head
        # -- so the claim goes back to the queue now, under its own fence.
        with contextlib.suppress(Exception):
            await store.snapshot.defer_snapshot_publish(
                snapshot_id=snapshot_id,
                expected_publish_attempt_count=expected_publish_attempt_count,
                detail="the publisher stopped during publication; publication will retry",
            )
        raise
    finally:
        owner = asyncio.current_task()
        owner_cancellation_baseline = owner.cancelling() if owner is not None else 0
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            # The heartbeat's own cancellation is expected. Cancellation of
            # this publisher while it joins the heartbeat must stop the loop.
            if owner is not None and owner.cancelling() > owner_cancellation_baseline:
                raise


async def _publish_claimed_owned(
    store: PublishStorage,
    job: dict[str, Any],
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
    grants: GrantClient | None = None,
    base_branch: str,
    allow_workflow_changes: bool = False,
) -> str | None:
    """Publish one claimed snapshot. Returns the PR URL, or ``None``."""
    job_id = job["job_id"]
    base_sha = job["base_sha"]
    pr_base = _pr_base_branch(job, base_branch)
    expected_publish_attempt_count = int(job["publish_attempt_count"])

    async def fail(detail: str) -> None:
        await store.snapshot.fail_snapshot_publish(
            snapshot_id=int(job["snapshot_id"]),
            expected_publish_attempt_count=expected_publish_attempt_count,
            detail=detail,
        )

    async def retry(detail: str, *, delay_s: float | None = None) -> None:
        """Let storage atomically defer or exhaust this exact claim."""
        logger.warning(
            "agent_job_publish_retrying",
            extra={
                "event": "agent_job_publish_retrying",
                "job_id": job_id,
                "publish_attempt_count": expected_publish_attempt_count,
            },
        )
        await store.snapshot.defer_snapshot_publish(
            snapshot_id=int(job["snapshot_id"]),
            expected_publish_attempt_count=expected_publish_attempt_count,
            detail=detail,
            delay_s=delay_s,
        )

    # Re-checked here, before any credential is minted. Publishing happens well
    # after the job ran, and it is the step that asks for *write* authority —
    # so an entitlement withdrawn in between (a revoked connection, a narrowed
    # allowlist) has to be able to stop it. The read side already refuses at
    # claim; without this the same deployment would block the read-only token
    # and still hand out the push token.
    environment = None
    try:
        await require_entitled_repo(
            job["repo"],
            job.get("user_id", ""),
            store=store.source_control,
            app_credentials=app_credentials,
        )
    except RepoAccessUnavailable as exc:
        await retry(f"repository access check unavailable; publication will retry: {exc}")
        return None
    except RepoNotAllowed as exc:
        await fail(f"no longer entitled to publish to {job['repo']}: {exc}")
        return None

    # Prefer the App: it mints a token scoped to this repository's
    # installation, valid an hour. A static token is the fallback for a
    # deployment that has not set the App up.
    if app_credentials is not None:
        try:
            # Scoped twice: to this one repository, and to the two permissions
            # publishing actually uses. An installation token inherits every
            # permission the App holds unless it asks for less, so omitting
            # `permissions` here handed the publisher whatever the App could
            # ever do — against a repository the *user* named.
            credential = GitHubCredential(
                await app_credentials.token_for(
                    job["repo"],
                    permissions=_PUBLISH_SCOPE,
                    repository_scoped=True,
                    min_ttl_s=_PUBLISH_TOKEN_MIN_TTL_S,
                )
            )
        except Exception as exc:
            detail = f"could not obtain a GitHub credential: {exc}"
            if _is_retryable_publication_failure(exc):
                await retry(detail, delay_s=_provider_wait(exc))
            else:
                await fail(detail)
            return None
    if credential is None:
        await fail("no GitHub credential is configured for publishing")
        return None
    if not base_sha:
        await fail("cannot publish: the job has no base_sha to apply the patch onto")
        return None

    try:
        # Blocking git work: keep it off the event loop so a slow clone cannot
        # stall the gateway's request handling.
        environment = (
            await store.environments.get_job_environment(job_id)
            if getattr(store, "environments", None)
            else None
        )
        loop = asyncio.get_running_loop()

        def record_push(commit_sha: str) -> None:
            # On the git thread, just before the push. An unrecorded commit on
            # the branch is somebody else's to the next publication, so a push
            # this claim could not record does not happen.
            try:
                recorded = asyncio.run_coroutine_threadsafe(
                    store.snapshot.record_snapshot_push(
                        snapshot_id=int(job["snapshot_id"]),
                        expected_publish_attempt_count=expected_publish_attempt_count,
                        commit_sha=commit_sha,
                    ),
                    loop,
                ).result(timeout=_PUSH_RECORD_TIMEOUT_S)
            except Exception as exc:
                raise TransientPublishError(
                    f"could not record the commit before pushing it: {exc!r}"
                ) from exc
            if not recorded:
                raise TransientPublishError("the publication claim was lost before the push")

        result = await asyncio.to_thread(
            publish_patch,
            own_pushes=tuple(job.get("own_pushes") or ()),
            before_push=record_push,
            job_id=job_id,
            branch_id=job.get("branch_id") or job.get("thread_id") or job_id,
            patch=job["patch"],
            clone_url=credential.clone_url(job["repo"]),
            base_sha=base_sha,
            commit_message=f"agent: {job['task_prompt'][:60]}".strip(),
            allow_workflow_changes=allow_workflow_changes,
            include_base_bundle=True,
            auth_env=credential.git_auth_env(),
            **({"known_secrets": environment.secrets()} if environment else {}),
        )
        title = await generate_pr_title(job, result.changed_files, grants=grants)
        pr_url = await ensure_draft_pull_request(
            repo=job["repo"],
            head_branch=result.branch,
            base_branch=pr_base,
            title=title or _pr_title(job, result.changed_files),
            body=_pr_body(job, result.changed_files),
            credential=credential,
        )
    except Exception as exc:
        detail = str(exc) if isinstance(exc, PublishError) else f"publish failed: {exc}"
        if environment is not None:
            from cloud_agent_shared.secret_redaction import SecretRedactor

            detail = SecretRedactor(environment.secrets()).text(detail)
        if _is_retryable_publication_failure(exc):
            await retry(detail, delay_s=_provider_wait(exc))
            return None
        if isinstance(exc, PublishError):
            logger.warning(
                "agent_job_publish_rejected",
                extra={"event": "agent_job_publish_rejected", "job_id": job_id},
            )
        else:
            if environment is not None and environment.secrets():
                logger.error("agent job publish failed unexpectedly (%s)", type(exc).__name__)
            else:
                logger.exception("agent job publish failed unexpectedly")
        await fail(detail)
        return None

    recorded = await store.snapshot.record_snapshot_publish(
        snapshot_id=int(job["snapshot_id"]),
        expected_publish_attempt_count=expected_publish_attempt_count,
        pr_url=pr_url,
        commit_sha=result.commit_sha,
    )
    if not recorded:
        # The claim was no longer ours: the stalled-publish reaper failed it,
        # or another publisher settled it. The branch is the same one, but
        # this PR is recorded nowhere, so say so rather than pretend.
        logger.warning(
            "agent_job_publish_raced",
            extra={"event": "agent_job_publish_raced", "job_id": job_id, "pr_url": pr_url},
        )
        return None

    await _import_snapshot_base(
        store,
        snapshot=job,
        commit_sha=result.commit_sha,
        bundle=result.base_bundle,
    )

    logger.info(
        "agent_job_pr_opened",
        extra={
            "event": "agent_job_pr_opened",
            "job_id": job_id,
            "branch": branch_name_for(job.get("branch_id") or job.get("thread_id") or job_id),
            "base_branch": pr_base,
            "pr_url": pr_url,
        },
    )
    return pr_url


async def publish_loop(
    store: PublishStorage,
    *,
    credential_provider: Any = None,
    github_app: GitHubAppSource | None = None,
    grants: GrantClient | None = None,
    base_branch: str | None = None,
    interval_seconds: float | None = None,
    wake: BackgroundWake | None = None,
) -> None:
    """Drain the publish queue forever.

    With ``wake`` a snapshot that is captured, published, or whose base is to
    be imported is handled at once, and an idle publisher sleeps until it is
    woken or the earliest deferred publication or failed base import may be
    retried. While the App or a credential is missing, which nothing announces,
    it looks on its clock.

    ``credential_provider`` is called per job so a short-lived installation
    token is minted fresh rather than held for the lifetime of the process.
    A provider that returns ``None`` (GitHub not configured) makes this a
    no-op, which is what a deployment without the GitHub App should do.

    ``github_app`` is asked for the App's credentials once per pass, for the
    same reason: a loop that captured them at startup published nothing for
    the rest of the process after one failed load (issue #47).
    """
    without_source = False
    while True:
        poll_seconds = (
            interval_seconds
            if interval_seconds is not None
            else number_setting("publish_poll_seconds", POLL_INTERVAL_S)
        )
        if wake is None:
            await asyncio.sleep(poll_seconds)
        elif without_source or not wake.listening:
            await wake.wait(PUBLISH, poll_seconds)
        else:
            await wake.wait(PUBLISH, await _next_look_s(store))
        without_source = False
        try:
            try:
                app_credentials = github_app.get() if github_app is not None else None
            except SourceControlUnavailable:
                # Configured, and not loaded yet. Waiting is the whole answer:
                # the queue keeps every snapshot and its retry record, and the
                # static token is not a stand-in for an App the deployment
                # chose — it would publish under a different identity.
                without_source = True
                continue
            credential = credential_provider() if credential_provider else None
            # Either source is enough. Skipping when only the App is
            # configured is what made an App-only deployment — the one this
            # module recommends — silently never publish anything.
            if credential is None and app_credentials is None:
                without_source = True
                continue
            await _retry_due_base_imports(
                store,
                credential=credential,
                app_credentials=app_credentials,
            )
            # Drain rather than publishing one per tick, so a burst of
            # finished jobs does not sit in the queue for minutes.
            await _publish_due(
                store,
                credential=credential,
                app_credentials=app_credentials,
                grants=grants,
                base_branch=base_branch
                if base_branch is not None
                else setting_env("AGENT_PUBLISH_BASE_BRANCH") or "dev",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("agent job publish loop iteration failed", exc_info=True)


async def _next_look_s(store: PublishStorage) -> float:
    """How long the publisher may sleep before it looks for work again."""
    idle_s = number_setting("publish_idle_seconds", PUBLISH_IDLE_S)
    with contextlib.suppress(Exception):
        due = await store.snapshot.next_base_import_retry_due()
        if due is not None:
            idle_s = min(idle_s, (due - datetime.now(UTC)).total_seconds())
    with contextlib.suppress(Exception):
        due = await store.snapshot.next_snapshot_publish_retry_due()
        if due is not None:
            idle_s = min(idle_s, (due - datetime.now(UTC)).total_seconds())
    return max(idle_s, 0.0)
