"""Reconcile published GitHub pull requests and archive their settled sessions.

Postgres owns the publication association, claim lease and archive receipt.
GitHub supplies only the merge verdict; URLs or text supplied by a pull request
never choose a session or an authenticated request destination.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import httpx

from cloud_agent.entitlement import REPO_PATTERN, RepoNotAllowed, require_entitled_repo
from cloud_agent.github_app import GitHubAppError
from cloud_agent.logging_utils import get_logger
from cloud_agent.publish_worker import GitHubCredential
from cloud_agent.settings import boolean_setting, integer_setting, number_setting
from cloud_agent.source_control_runtime import SourceControlUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.ports import PrArchiveRepository, SourceControlRepository
    from cloud_agent.source_control_runtime import GitHubAppSource


class PrArchiveStorage(Protocol):
    """The durable merge checks and owner entitlement needed by this worker."""

    pr_archive: PrArchiveRepository
    source_control: SourceControlRepository


logger = get_logger(__name__)
_CONCURRENCY = 8
_PR_PATH = re.compile(r"/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?")
_MERGE_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_Verdict = tuple[str, datetime | None, str | None, float]


@dataclass
class _PassReads:
    """Share reads only within one owner's entitled repository scope."""

    checks: dict[tuple[str, int, str], asyncio.Task[_Verdict]] = field(default_factory=dict)
    paused: dict[tuple[str, str], float] = field(default_factory=dict)


class _CheckError(Exception):
    """Carry reason codes without provider output or credential-bearing URLs."""

    def __init__(
        self,
        code: str,
        *,
        retry_after: float = 0,
        transient: bool = False,
        terminal: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.transient = transient
        self.terminal = terminal


def _pr_number(url: str, repo: str) -> int:
    """Validate a persisted GitHub PR link against its trusted repository."""
    try:
        parsed = urlsplit(url)
        path = _PR_PATH.fullmatch(parsed.path)
        valid = (
            re.fullmatch(REPO_PATTERN, repo) is not None
            and parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() or ord(character) < 32 for character in url)
            and path is not None
            and path[1].lower() == repo.lower()
        )
        if valid and path is not None:
            return int(path[2])
    except (ValueError, TypeError):
        pass
    raise _CheckError("invalid_pr_mapping", terminal=True)


def _provider_wait(response: httpx.Response) -> float:
    """Read provider backoff headers without including them in diagnostics."""
    now = datetime.now(UTC)
    retry_after = response.headers.get("Retry-After", "")
    try:
        seconds = float(retry_after)
        if math.isfinite(seconds):
            return max(seconds, 0)
    except ValueError:
        try:
            date = parsedate_to_datetime(retry_after)
            if date.tzinfo is not None:
                return max((date - now).total_seconds(), 0)
        except (ValueError, TypeError, OverflowError):
            pass
    if response.headers.get("X-RateLimit-Remaining") == "0":
        try:
            reset = float(response.headers.get("X-RateLimit-Reset", ""))
            if math.isfinite(reset):
                return max(reset - now.timestamp(), 0)
        except ValueError:
            pass
    return 0


def _merge_verdict(body: Any, *, repo: str, number: int) -> tuple[str, datetime | None, str | None]:
    """Verify PR identity and return a strict, authenticated merge verdict."""
    if not isinstance(body, dict):
        raise _CheckError("invalid_github_response")
    base = body.get("base")
    repository = base.get("repo") if isinstance(base, dict) else None
    full_name = repository.get("full_name") if isinstance(repository, dict) else None
    response_number = body.get("number")
    response_url = body.get("html_url")
    if not isinstance(response_url, str):
        raise _CheckError("pr_identity_mismatch", terminal=True)
    try:
        url_number = _pr_number(response_url, repo)
    except _CheckError as exc:
        raise _CheckError("pr_identity_mismatch", terminal=True) from exc
    if (
        not isinstance(response_number, int)
        or isinstance(response_number, bool)
        or response_number != number
        or url_number != number
        or not isinstance(full_name, str)
        or full_name.lower() != repo.lower()
    ):
        raise _CheckError("pr_identity_mismatch", terminal=True)
    state, merged = body.get("state"), body.get("merged")
    if (
        not isinstance(state, str)
        or state not in {"open", "closed"}
        or not isinstance(merged, bool)
    ):
        raise _CheckError("invalid_github_response")
    if not merged:
        return state, None, None
    if state != "closed":
        raise _CheckError("invalid_github_response")
    try:
        merged_at = datetime.fromisoformat(body["merged_at"].replace("Z", "+00:00"))
        if merged_at.tzinfo is None:
            raise ValueError("merge time must include a timezone")
    except (KeyError, AttributeError, ValueError, TypeError) as exc:
        raise _CheckError("invalid_merge_time") from exc
    merge_sha = body.get("merge_commit_sha")
    if merge_sha is not None and (
        not isinstance(merge_sha, str) or _MERGE_SHA.fullmatch(merge_sha) is None
    ):
        raise _CheckError("invalid_merge_sha")
    return "merged", merged_at.astimezone(UTC), merge_sha


def _secondary_rate_limit(response: httpx.Response) -> bool:
    """Classify secondary throttling without logging provider response text."""
    try:
        body = response.json()
    except ValueError:
        return False
    message = body.get("message") if isinstance(body, dict) else None
    return isinstance(message, str) and any(
        marker in message.lower() for marker in ("rate limit", "abuse detection")
    )


def _healthy_interval(response: httpx.Response, poll_s: float) -> float:
    """Spread the configured batch across GitHub's remaining request budget."""
    try:
        remaining = int(response.headers.get("X-RateLimit-Remaining", ""))
        reset = float(response.headers.get("X-RateLimit-Reset", ""))
    except ValueError:
        return poll_s
    if remaining < 0 or not math.isfinite(reset):
        return poll_s
    window = max(0, reset - datetime.now(UTC).timestamp())
    if remaining == 0:
        return max(poll_s, window)
    # Leave ten percent of the advertised budget for publishing and other
    # repository reads. Missing headers preserve the ordinary poll interval.
    batch = integer_setting("pr_archive_batch_size", 20)
    return max(poll_s, batch * window / (remaining * 0.9))


async def _read_pr(
    store: PrArchiveStorage,
    claim: dict[str, Any],
    *,
    number: int,
    credential: GitHubCredential | None,
    app_credentials: Any,
    client: httpx.AsyncClient,
    poll_s: float,
    timeout_s: float,
    reads: _PassReads,
) -> _Verdict:
    """Bound entitlement, credential minting and the authenticated API read."""
    scope = (claim["repo"].lower(), claim["user_id"])
    remaining_pause = reads.paused.get(scope, 0) - datetime.now(UTC).timestamp()
    if remaining_pause > 0:
        raise _CheckError("github_rate_limited", retry_after=remaining_pause, transient=True)
    try:
        async with asyncio.timeout(max(90, timeout_s * 3)):
            return await _request_pr(
                store,
                claim,
                number=number,
                credential=credential,
                app_credentials=app_credentials,
                client=client,
                poll_s=poll_s,
                timeout_s=timeout_s,
            )
    except _CheckError as exc:
        if exc.code == "github_rate_limited":
            reads.paused[scope] = datetime.now(UTC).timestamp() + max(60, exc.retry_after)
        raise
    except TimeoutError as exc:
        raise _CheckError("github_timeout", transient=True) from exc


async def _request_pr(
    store: PrArchiveStorage,
    claim: dict[str, Any],
    *,
    number: int,
    credential: GitHubCredential | None,
    app_credentials: Any,
    client: httpx.AsyncClient,
    poll_s: float,
    timeout_s: float,
) -> _Verdict:
    """Ask GitHub for one PR after rechecking its owner's entitlement."""
    await require_entitled_repo(
        claim["repo"],
        claim["user_id"],
        store=store.source_control,
        app_credentials=app_credentials,
    )
    if app_credentials is not None:
        credential = GitHubCredential(
            await app_credentials.token_for(
                claim["repo"],
                permissions={"pull_requests": "read"},
                repository_scoped=True,
            )
        )
    if credential is None:
        raise _CheckError("github_unconfigured", transient=True)
    response = await client.get(
        f"{credential.api_base.rstrip('/')}/repos/{claim['repo']}/pulls/{number}",
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=timeout_s,
        follow_redirects=False,
    )
    provider_wait = _provider_wait(response)
    if response.status_code == 429 or (
        response.status_code == 403
        and (
            provider_wait > 0
            or response.headers.get("X-RateLimit-Remaining") == "0"
            or _secondary_rate_limit(response)
        )
    ):
        raise _CheckError("github_rate_limited", retry_after=max(60, provider_wait), transient=True)
    if response.status_code in {401, 403, 404}:
        raise _CheckError("github_access_denied")
    if response.status_code >= 500:
        raise _CheckError("github_unavailable", retry_after=provider_wait, transient=True)
    if response.status_code != 200:
        raise _CheckError("github_http_error")
    try:
        body = response.json()
    except ValueError as exc:
        raise _CheckError("invalid_github_response") from exc
    verdict, merged_at, merge_sha = _merge_verdict(body, repo=claim["repo"], number=number)
    return verdict, merged_at, merge_sha, _healthy_interval(response, poll_s)


async def _check(
    store: PrArchiveStorage,
    claim: dict[str, Any],
    *,
    credential: GitHubCredential | None,
    app_credentials: Any,
    client: httpx.AsyncClient,
    poll_s: float,
    timeout_s: float,
    reads: _PassReads,
) -> bool:
    fields: dict[str, Any] = {
        "thread_id": claim["thread_id"],
        "job_id": claim["job_id"],
        "repo": claim["repo"],
    }

    async def reschedule(*, delay: float, code: str | None = None, terminal: bool = False) -> None:
        await store.pr_archive.reschedule_pr_archive_check(
            thread_id=claim["thread_id"],
            pr_url=claim["pr_url"],
            claim_token=claim["claim_token"],
            delay_s=delay,
            error_code=code,
            terminal=terminal,
        )

    try:
        number = _pr_number(claim["pr_url"], claim["repo"])
        fields["pr_number"] = number
        key = (claim["repo"].lower(), number, claim["user_id"])
        if key not in reads.checks:
            reads.checks[key] = asyncio.create_task(
                _read_pr(
                    store,
                    claim,
                    number=number,
                    credential=credential,
                    app_credentials=app_credentials,
                    client=client,
                    poll_s=poll_s,
                    timeout_s=timeout_s,
                    reads=reads,
                )
            )
        verdict, merged_at, merge_sha, next_check = await reads.checks[key]
        if verdict != "merged":
            await reschedule(delay=max(next_check, 300) if verdict == "closed" else next_check)
            if verdict == "closed":
                logger.info(
                    "agent_pr_archive_closed_unmerged",
                    extra={"event": "agent_pr_archive_closed_unmerged", **fields},
                )
            return False
        assert merged_at is not None
        detected_at = datetime.now(UTC)
        receipt = await store.pr_archive.complete_pr_merge_archive(
            thread_id=claim["thread_id"],
            job_id=claim["job_id"],
            user_id=claim["user_id"],
            pr_url=claim["pr_url"],
            claim_token=claim["claim_token"],
            merged_at=merged_at,
            merge_sha=merge_sha,
        )
        if receipt is None:
            await reschedule(delay=poll_s, code="association_changed")
            return False
        archived_at = receipt["archived_at"]
        latency = (
            (archived_at - merged_at).total_seconds() if isinstance(archived_at, datetime) else None
        )
        logger.info(
            "agent_session_archived_after_pr_merge",
            extra={
                "event": "agent_session_archived_after_pr_merge",
                **fields,
                "pr_url": claim["pr_url"],
                "merged_at": merged_at.isoformat(),
                "detected_at": detected_at.isoformat(),
                "archived_at": str(archived_at),
                "merge_to_archive_s": latency,
                "merge_sha": merge_sha,
                "attempts": int(claim.get("failures", 0)) + 1,
            },
        )
        return True
    except RepoNotAllowed:
        error = _CheckError("repo_access_denied")
    except httpx.TimeoutException:
        error = _CheckError("github_timeout", transient=True)
    except httpx.HTTPError:
        error = _CheckError("github_transport_error", transient=True)
    except GitHubAppError as exc:
        error = _CheckError(
            "github_access_denied" if exc.status in {401, 403, 404} else "github_unavailable",
            transient=exc.status not in {401, 403, 404},
        )
    except SourceControlUnavailable:
        error = _CheckError("github_source_unavailable", transient=True)
    except _CheckError as exc:
        error = exc
    except Exception:
        # Provider credential exceptions and storage failures may include
        # secrets in their message or traceback. A fixed code preserves both
        # confidentiality and recovery after this claim's lease expires.
        error = _CheckError("pr_archive_check_failed", transient=True)

    failures = int(claim.get("failures", 0)) + 1
    terminal = error.terminal or (
        not error.transient and failures >= integer_setting("pr_archive_max_failures", 10)
    )
    maximum = number_setting("pr_archive_retry_max_s", 3600)
    delay = max(
        error.retry_after,
        min(maximum, poll_s * 2 ** min(failures - 1, 20) * random.uniform(0.8, 1.2)),
    )
    try:
        await reschedule(delay=delay, code=error.code, terminal=terminal)
    except Exception:
        logger.warning(
            "agent_pr_archive_reschedule_failed",
            extra={"event": "agent_pr_archive_reschedule_failed", **fields},
        )
    logger.warning(
        "agent_pr_archive_blocked" if terminal else "agent_pr_archive_check_failed",
        extra={
            "event": "agent_pr_archive_blocked" if terminal else "agent_pr_archive_check_failed",
            **fields,
            "error_code": error.code,
            "failures": failures,
            "retry_s": delay,
        },
    )
    return False


async def poll_pr_merge_archives(
    store: PrArchiveStorage,
    *,
    credential_provider: Callable[[], GitHubCredential | None] | None = None,
    github_app: GitHubAppSource | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Check a bounded batch of durable associations; return archives completed."""
    if not boolean_setting("pr_archive_enabled", True):
        return 0
    try:
        app_credentials = github_app.get() if github_app is not None else None
        credential = (
            credential_provider() if app_credentials is None and credential_provider else None
        )
    except SourceControlUnavailable:
        logger.warning(
            "agent_pr_archive_source_unavailable",
            extra={"event": "agent_pr_archive_source_unavailable"},
        )
        return 0
    if credential is None and app_credentials is None:
        return 0
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await _poll(
                store, credential=credential, app_credentials=app_credentials, client=owned
            )
    return await _poll(store, credential=credential, app_credentials=app_credentials, client=client)


async def _poll(
    store: PrArchiveStorage,
    *,
    credential: GitHubCredential | None,
    app_credentials: Any,
    client: httpx.AsyncClient,
) -> int:
    remaining = integer_setting("pr_archive_batch_size", 20)
    timeout_s = number_setting("pr_archive_timeout_s", 10)
    poll_s = number_setting("pr_archive_poll_s", 30)
    archived = 0
    reads = _PassReads()
    while remaining > 0:
        claims = await store.pr_archive.claim_pr_archive_checks(
            limit=min(remaining, _CONCURRENCY), lease_seconds=max(120, timeout_s * 3 + 60)
        )
        if not claims:
            break
        remaining -= len(claims)
        results = await asyncio.gather(
            *(
                _check(
                    store,
                    claim,
                    credential=credential,
                    app_credentials=app_credentials,
                    client=client,
                    poll_s=poll_s,
                    timeout_s=timeout_s,
                    reads=reads,
                )
                for claim in claims
            )
        )
        archived += sum(results)
    return archived


async def pr_archive_loop(
    store: PrArchiveStorage,
    *,
    credential_provider: Callable[[], GitHubCredential | None] | None = None,
    github_app: GitHubAppSource | None = None,
    client: httpx.AsyncClient | None = None,
    interval_seconds: float | None = None,
) -> None:
    """Reconcile merge state continuously, surviving integration and database failures."""
    while True:
        try:
            await poll_pr_merge_archives(
                store,
                credential_provider=credential_provider,
                github_app=github_app,
                client=client,
            )
        except Exception:
            logger.warning(
                "agent_pr_archive_iteration_failed",
                extra={"event": "agent_pr_archive_iteration_failed"},
            )
        await asyncio.sleep(
            interval_seconds
            if interval_seconds is not None
            else number_setting("pr_archive_poll_s", 30)
        )
