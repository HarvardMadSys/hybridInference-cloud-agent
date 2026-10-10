"""The publish step: immutable group snapshot → branch → draft PR.

This is the last hop of the chain and the one that produces the visible
product outcome. It runs **in the gateway**, never in the sandbox, for one
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
   reviews before anything can merge and the diff is the agent's own work.

A rejected patch marks the snapshot/group dirty without rewriting successful
Agent turns. Gate violations are human-review situations, and a retry loop
would either spam the repository or bury the rejection.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.logging_utils import get_logger
from cloud_agent.publisher import PublishError, fetch_base_bundle, publish_patch
from cloud_agent.schemas import BASE_REF_METADATA_KEY
from cloud_agent.source_control_runtime import SourceControlUnavailable
from cloud_agent.workers.background_wake import PUBLISH
from cloud_agent_shared.git.patch_gate import branch_name_for

if TYPE_CHECKING:
    from cloud_agent.ports import (
        HostPoolRepository,
        SnapshotRepository,
        SourceControlRepository,
    )
    from cloud_agent.source_control_runtime import GitHubAppSource
    from cloud_agent.workers.background_wake import BackgroundWake


class PublishStorage(Protocol):
    """What publishing a group snapshot reaches the storage layer for.

    Two capabilities and no more: the snapshot rows it claims and records,
    and the host pool, which it needs only to find the broker holding the
    workspace it is publishing from.
    """

    snapshot: SnapshotRepository
    host_pool: HostPoolRepository
    source_control: SourceControlRepository


logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
POLL_INTERVAL_S = 15.0
#: The longest an idle publisher that is told about new work sleeps. It wakes
#: sooner for a notification or the earliest base import retry.
PUBLISH_IDLE_S = 30.0

# Exactly what publishing does: push one branch, open one draft PR. Notably not
# `workflows`, so a patch touching `.github/` cannot be pushed even if both
# gates were bypassed — the credential itself would refuse.
_PUBLISH_SCOPE = {"contents": "write", "pull_requests": "write"}

# A PR base has to be a branch. `base_ref` is normally one (the composer offers
# a branch picker), but the API accepts any ref it can resolve, and a caller
# who passed a commit would otherwise turn a publishable job into a 422.
#
# Matched with `fullmatch`. The anchors make it equivalent today, and that is
# the point: `$` also matches before a trailing newline, so `match` would
# accept `abcdef1\n` — reachable only if the `.strip()` above ever goes away.
# A branch called `deadbee-release` is not affected either way.
_LOOKS_LIKE_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


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
    installed the app on. It stays in this process: it is used to build the
    push URL and the API call, and never reaches a sandbox.
    """

    token: str
    api_base: str = GITHUB_API

    def clone_url(self, repo: str) -> str:
        """Return an authenticated clone URL for ``owner/name``."""
        return f"https://x-access-token:{self.token}@github.com/{repo}.git"


async def create_draft_pull_request(
    *,
    repo: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    credential: GitHubCredential,
    timeout_s: float = 30.0,
) -> str:
    """Open a draft PR and return its URL.

    Draft is not a nicety: the whole safety story assumes a human reads the
    change before it can merge, so this must never open a ready-for-review PR.
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(
            f"{credential.api_base}/repos/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
                "draft": True,
            },
        )
    if response.status_code >= 400:
        raise PublishError(
            f"GitHub refused the draft PR ({response.status_code}): {response.text[:300]}"
        )
    url = response.json().get("html_url")
    if not isinstance(url, str) or not url.strip():
        # A 2xx with no usable URL is not a success this worker can record.
        # Returning "" put an empty `published_pr_url` on the job, which marks
        # it published — and the query this worker claims from selects on
        # `published_pr_url IS NULL`, so the job would never be retried and the
        # owner would never get a link. Failing is recoverable; that is not.
        raise PublishError(
            f"GitHub accepted the draft PR ({response.status_code}) but returned no html_url"
        )
    return url


def _pr_body(job: dict[str, Any], changed_files: list[str]) -> str:
    """Compose a PR body that tells the reviewer what they are reviewing."""
    files = "\n".join(f"- `{path}`" for path in changed_files[:50])
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
    from cloud_agent.broker_routing import broker_for_job

    broker = await broker_for_job(store.host_pool, snapshot["anchor_job_id"])
    if broker is None:
        await store.snapshot.record_snapshot_base_import_failure(
            snapshot_id=snapshot_id,
            detail="the owning workspace broker is unavailable for base import",
        )
        return False
    try:
        await broker.import_shared_base(snapshot, commit_sha=commit_sha, bundle=bundle)
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


async def retry_snapshot_base_import(
    store: PublishStorage,
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
) -> bool:
    """Retry one durable post-publish object import after a broker outage."""
    snapshot = await store.snapshot.pending_snapshot_base_import()
    if snapshot is None:
        return False
    return await _retry_base_import(
        store, snapshot, credential=credential, app_credentials=app_credentials
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
    """Import one published base again; a failure is recorded on its snapshot."""
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
                )
            )
        if credential is None:
            raise PublishError("no GitHub credential is configured for base import")
        commit_sha = str(snapshot["published_commit_sha"])
        bundle = await asyncio.to_thread(
            fetch_base_bundle,
            clone_url=credential.clone_url(snapshot["repo"]),
            commit_sha=commit_sha,
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
        )
        return False


async def publish_one(
    store: PublishStorage,
    *,
    credential: GitHubCredential | None = None,
    app_credentials: Any | None = None,
    base_branch: str = "dev",
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
        base_branch=base_branch,
        allow_workflow_changes=allow_workflow_changes,
    )


async def _publish_due(
    store: PublishStorage,
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
    base_branch: str,
) -> None:
    """Publish every captured snapshot, past any that fails.

    A failure marks its snapshot failed, so it is not claimed again, and the
    snapshots behind it still go out in this pass rather than at the next.
    """
    while (job := await store.snapshot.claim_for_publish()) is not None:
        await _publish_claimed(
            store,
            job,
            credential=credential,
            app_credentials=app_credentials,
            base_branch=base_branch,
        )


async def _publish_claimed(
    store: PublishStorage,
    job: dict[str, Any],
    *,
    credential: GitHubCredential | None,
    app_credentials: Any | None,
    base_branch: str,
    allow_workflow_changes: bool = False,
) -> str | None:
    """Publish one claimed snapshot. Returns the PR URL, or ``None``."""
    job_id = job["job_id"]
    base_sha = job["base_sha"]
    pr_base = _pr_base_branch(job, base_branch)

    async def fail(detail: str) -> None:
        await store.snapshot.fail_snapshot_publish(
            snapshot_id=int(job["snapshot_id"]),
            detail=detail,
        )

    # Re-checked here, before any credential is minted. Publishing happens well
    # after the job ran, and it is the step that asks for *write* authority —
    # so an entitlement withdrawn in between (a revoked connection, a narrowed
    # allowlist) has to be able to stop it. The read side already refuses at
    # claim; without this the same deployment would block the read-only token
    # and still hand out the push token.
    try:
        await require_entitled_repo(
            job["repo"],
            job.get("user_id", ""),
            store=store.source_control,
            app_credentials=app_credentials,
        )
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
                    job["repo"], permissions=_PUBLISH_SCOPE, repository_scoped=True
                )
            )
        except Exception as exc:
            await fail(f"could not obtain a GitHub credential: {exc}")
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
        result = await asyncio.to_thread(
            publish_patch,
            job_id=job_id,
            branch_id=job.get("branch_id") or job.get("thread_id") or job_id,
            patch=job["patch"],
            clone_url=credential.clone_url(job["repo"]),
            base_sha=base_sha,
            commit_message=f"agent: {job['task_prompt'][:60]}".strip(),
            allow_workflow_changes=allow_workflow_changes,
            include_base_bundle=True,
        )
        pr_url = job.get("parent_pr_url")
        if not pr_url:
            pr_url = await create_draft_pull_request(
                repo=job["repo"],
                head_branch=result.branch,
                base_branch=pr_base,
                title=f"[agent] {job['task_prompt'][:70]}".strip(),
                body=_pr_body(job, result.changed_files),
                credential=credential,
            )
    except PublishError as exc:
        logger.warning(
            "agent_job_publish_rejected",
            extra={"event": "agent_job_publish_rejected", "job_id": job_id},
        )
        await fail(str(exc))
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("agent job publish failed unexpectedly")
        await fail(f"publish failed: {exc}")
        return None

    recorded = await store.snapshot.record_snapshot_publish(
        snapshot_id=int(job["snapshot_id"]),
        pr_url=pr_url,
        commit_sha=result.commit_sha,
    )
    if not recorded:
        # Someone recorded a PR while we worked. The branch we pushed is
        # harmless (it is the same one), but say so rather than pretend.
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
    base_branch: str = "dev",
    interval_seconds: float = POLL_INTERVAL_S,
    wake: BackgroundWake | None = None,
) -> None:
    """Drain the publish queue forever.

    With ``wake`` a snapshot that is captured, published, or whose base is to
    be imported is handled at once, and an idle publisher sleeps until it is
    woken or the earliest failed base import may be retried. While the App or
    a credential is missing, which nothing announces, it looks on its clock.

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
        if wake is None:
            await asyncio.sleep(interval_seconds)
        elif without_source or not wake.listening:
            await wake.wait(PUBLISH, interval_seconds)
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
                base_branch=base_branch,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("agent job publish loop iteration failed", exc_info=True)


async def _next_look_s(store: PublishStorage) -> float:
    """How long the publisher may sleep before it looks for work again."""
    idle_s = PUBLISH_IDLE_S
    with contextlib.suppress(Exception):
        due = await store.snapshot.next_base_import_retry_due()
        if due is not None:
            idle_s = min(idle_s, (due - datetime.now(UTC)).total_seconds())
    return max(idle_s, 0.0)
