"""Verify authenticated merge detection, recovery and session archive fencing."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent.publish_worker import GitHubCredential
from cloud_agent.source_control_runtime import SourceControlUnavailable
from cloud_agent.workers import pr_archive

pytestmark = pytest.mark.asyncio
_URL = "https://github.com/o/n/pull/7"
_CLAIM = {
    "thread_id": "athr_1",
    "job_id": "ajob_1",
    "user_id": "u1",
    "repo": "o/n",
    "pr_url": _URL,
    "claim_token": "claim-1",
    "failures": 0,
}
_MERGED = {
    "number": 7,
    "html_url": _URL,
    "base": {"repo": {"full_name": "o/n"}},
    "state": "closed",
    "merged": True,
    "merged_at": "2026-10-10T18:30:00Z",
    "merge_commit_sha": "a" * 40,
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "o/n")
    monkeypatch.setattr(pr_archive, "boolean_setting", lambda key, fallback: fallback)
    monkeypatch.setattr(pr_archive, "integer_setting", lambda key, fallback: fallback)
    monkeypatch.setattr(pr_archive, "number_setting", lambda key, fallback: fallback)
    monkeypatch.setattr(pr_archive.random, "uniform", lambda minimum, maximum: 1)


class _Repository:
    def __init__(self, claims=None, *, receipt=True, fail_completion=False):
        self.claims = list(claims if claims is not None else [dict(_CLAIM)])
        self.rescheduled: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.claim_calls: list[dict[str, Any]] = []
        self.receipt = receipt
        self.fail_completion = fail_completion

    async def claim_pr_archive_checks(self, **kwargs):
        self.claim_calls.append(kwargs)
        claims, self.claims = self.claims[: kwargs["limit"]], self.claims[kwargs["limit"] :]
        return claims

    async def reschedule_pr_archive_check(self, **kwargs):
        self.rescheduled.append(kwargs)
        return True

    async def complete_pr_merge_archive(self, **kwargs):
        if self.fail_completion:
            raise ConnectionError("secret storage DSN")
        self.completed.append(kwargs)
        if not self.receipt:
            return None
        return {
            "thread_id": kwargs["thread_id"],
            "archived_at": datetime(2026, 10, 10, 18, 30, 10, tzinfo=UTC),
        }


def _store(repository):
    return SimpleNamespace(pr_archive=repository, source_control=object())


async def _poll(repository, handler=None, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(200, json=_MERGED))
        )
    ) as client:
        return await pr_archive.poll_pr_merge_archives(
            _store(repository),
            client=client,
            credential_provider=lambda: GitHubCredential("private-token"),
            **kwargs,
        )


async def test_verified_merge_archives_exact_owner_session_and_logs_receipt(caplog):
    repository = _Repository()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_MERGED)

    with caplog.at_level(logging.INFO):
        assert await _poll(repository, handler) == 1
    assert str(requests[0].url) == "https://api.github.com/repos/o/n/pulls/7"
    assert requests[0].headers["Authorization"] == "Bearer private-token"
    assert repository.completed == [
        {
            "thread_id": "athr_1",
            "job_id": "ajob_1",
            "user_id": "u1",
            "pr_url": _URL,
            "claim_token": "claim-1",
            "merged_at": datetime(2026, 10, 10, 18, 30, tzinfo=UTC),
            "merge_sha": "a" * 40,
        }
    ]
    record = next(
        r
        for r in caplog.records
        if getattr(r, "event", "") == "agent_session_archived_after_pr_merge"
    )
    assert record.thread_id == "athr_1"
    assert record.pr_url == _URL
    assert record.merged_at == "2026-10-10T18:30:00+00:00"
    assert record.merge_to_archive_s == 10
    assert record.attempts == 1
    assert datetime.fromisoformat(record.detected_at).tzinfo is not None
    assert "private-token" not in caplog.text


@pytest.mark.parametrize("state, delay", [("open", 30), ("closed", 300)])
async def test_unmerged_pr_never_archives_but_can_be_reopened(state, delay):
    repository = _Repository()
    body = {**_MERGED, "merged": False, "merged_at": None, "state": state}
    assert await _poll(repository, lambda request: httpx.Response(200, json=body)) == 0
    assert repository.completed == []
    assert repository.rescheduled[0]["terminal"] is False
    assert repository.rescheduled[0]["delay_s"] == delay


async def test_closed_pr_archives_when_reopened_and_later_merged():
    repository = _Repository()
    closed = {**_MERGED, "merged": False, "merged_at": None}
    assert await _poll(repository, lambda request: httpx.Response(200, json=closed)) == 0
    assert repository.rescheduled[0]["terminal"] is False
    repository.claims.append(dict(_CLAIM))
    assert await _poll(repository) == 1
    assert repository.completed[0]["thread_id"] == "athr_1"


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/o/n/pull/7",
        "https://github.com/other/n/pull/7",
        "https://private-token@github.com/o/n/pull/7",
        "http://github.com/o/n/pull/7",
        "https://github.com/o/n/pull/7?next=secret",
        "https://github.com/o/n/pull/7#secret",
        "https://github.com/o/n/pull/true",
        "https://github.com/o/n/pull/7\n",
    ],
)
async def test_invalid_mapping_never_makes_authenticated_request(url):
    repository = _Repository([{**_CLAIM, "pr_url": url}])
    handler = AsyncMock(return_value=httpx.Response(200, json=_MERGED))
    assert await _poll(repository, handler) == 0
    handler.assert_not_called()
    assert not repository.completed
    assert repository.rescheduled[0]["error_code"] == "invalid_pr_mapping"
    assert repository.rescheduled[0]["terminal"] is True


@pytest.mark.parametrize(
    "replacement",
    [
        {"number": 8},
        {"number": True},
        {"html_url": "https://github.com/o/n/pull/8"},
        {"html_url": "https://attacker.example/o/n/pull/7"},
        {"base": {"repo": {"full_name": "other/n"}}},
    ],
)
async def test_wrong_pr_identity_cannot_archive(replacement):
    repository = _Repository()
    body = {**_MERGED, **replacement}
    assert await _poll(repository, lambda request: httpx.Response(200, json=body)) == 0
    assert not repository.completed
    assert repository.rescheduled[0]["error_code"] == "pr_identity_mismatch"
    assert repository.rescheduled[0]["terminal"] is True


@pytest.mark.parametrize(
    "replacement, code",
    [
        ({"merged": "true"}, "invalid_github_response"),
        ({"merged": 1}, "invalid_github_response"),
        ({"state": "open"}, "invalid_github_response"),
        ({"merged_at": None}, "invalid_merge_time"),
        ({"merged_at": "2026-10-10T18:30:00"}, "invalid_merge_time"),
        ({"merge_commit_sha": "not a SHA"}, "invalid_merge_sha"),
    ],
)
async def test_malformed_merge_evidence_retries_without_archiving(replacement, code):
    repository = _Repository()
    body = {**_MERGED, **replacement}
    assert await _poll(repository, lambda request: httpx.Response(200, json=body)) == 0
    assert not repository.completed
    assert repository.rescheduled[0]["error_code"] == code
    assert repository.rescheduled[0]["terminal"] is False


async def test_invalid_json_retries_without_logging_response(caplog):
    repository = _Repository()
    assert await _poll(repository, lambda request: httpx.Response(200, text="private-token")) == 0
    assert repository.rescheduled[0]["error_code"] == "invalid_github_response"
    assert "private-token" not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_missing_access_blocks_only_after_configured_failures(status):
    repository = _Repository([{**_CLAIM, "failures": 9}])
    assert (
        await _poll(repository, lambda request: httpx.Response(status, text="private-token")) == 0
    )
    assert not repository.completed
    assert repository.rescheduled[0]["error_code"] == "github_access_denied"
    assert repository.rescheduled[0]["terminal"] is True


async def test_a_check_skipped_for_access_is_tried_again_after_six_hours():
    repository = _Repository([{**_CLAIM, "failures": 9}])
    await _poll(repository, lambda request: httpx.Response(404))
    assert repository.rescheduled[0]["terminal"] is True
    assert repository.rescheduled[0]["delay_s"] == 6 * 3600
    # Ten failures to skip it, then 56 more tries: about two weeks.
    assert repository.claim_calls[0]["access_recovery_failures"] == 10 + 56


@pytest.mark.parametrize("status", [429, 403, 503])
async def test_provider_retry_after_is_honored_even_above_local_cap(status):
    repository = _Repository([{**_CLAIM, "failures": 100}])
    response = httpx.Response(status, headers={"Retry-After": "7200"})
    assert await _poll(repository, lambda request: response) == 0
    assert repository.rescheduled[0]["delay_s"] == 7200
    assert repository.rescheduled[0]["terminal"] is False


async def test_provider_retry_after_http_date_is_honored():
    repository = _Repository()
    retry_at = datetime.now(UTC) + timedelta(minutes=10)
    response = httpx.Response(
        429, headers={"Retry-After": retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")}
    )
    assert await _poll(repository, lambda request: response) == 0
    assert 590 < repository.rescheduled[0]["delay_s"] <= 600


async def test_rate_limit_reset_delays_next_check():
    repository = _Repository()
    reset = (datetime.now(UTC) + timedelta(minutes=10)).timestamp()
    response = httpx.Response(
        403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
    )
    assert await _poll(repository, lambda request: response) == 0
    assert 590 < repository.rescheduled[0]["delay_s"] <= 600
    assert repository.rescheduled[0]["error_code"] == "github_rate_limited"


async def test_secondary_rate_limit_without_headers_keeps_retrying(caplog):
    repository = _Repository([{**_CLAIM, "failures": 100}])
    body = {"message": "You have exceeded a secondary rate limit. private-token"}
    assert await _poll(repository, lambda request: httpx.Response(403, json=body)) == 0
    assert repository.rescheduled[0]["error_code"] == "github_rate_limited"
    assert repository.rescheduled[0]["terminal"] is False
    assert repository.rescheduled[0]["delay_s"] >= 60
    assert "private-token" not in caplog.text


async def test_healthy_checks_respect_remaining_provider_budget():
    repository = _Repository()
    reset = (datetime.now(UTC) + timedelta(hours=1)).timestamp()
    response = httpx.Response(
        200,
        json={**_MERGED, "merged": False, "state": "open"},
        headers={"X-RateLimit-Remaining": "1000", "X-RateLimit-Reset": str(reset)},
    )
    assert await _poll(repository, lambda request: response) == 0
    assert 75 < repository.rescheduled[0]["delay_s"] <= 80


async def test_shared_pr_is_read_once_for_same_owner_but_archives_each_session():
    repository = _Repository([{**_CLAIM, "thread_id": str(i)} for i in range(10)])
    handler = AsyncMock(return_value=httpx.Response(200, json=_MERGED))
    assert await _poll(repository, handler) == 10
    assert handler.await_count == 1
    assert len(repository.completed) == 10


async def test_read_cache_never_crosses_owner_credentials():
    repository = _Repository([dict(_CLAIM), {**_CLAIM, "thread_id": "athr_2", "user_id": "u2"}])
    handler = AsyncMock(return_value=httpx.Response(200, json=_MERGED))
    assert await _poll(repository, handler) == 2
    assert handler.await_count == 2


async def test_rate_limit_pauses_later_chunks_for_same_owner_repository():
    claims = [
        {**_CLAIM, "thread_id": str(i), "pr_url": f"https://github.com/o/n/pull/{i + 1}"}
        for i in range(10)
    ]
    repository = _Repository(claims)
    handler = AsyncMock(return_value=httpx.Response(429, headers={"Retry-After": "600"}))
    assert await _poll(repository, handler) == 0
    assert handler.await_count <= 8
    assert len(repository.rescheduled) == 10
    assert all(result["delay_s"] > 590 for result in repository.rescheduled)


async def test_timeout_retries_indefinitely_and_isolated_from_other_sessions(caplog):
    second = {**_CLAIM, "thread_id": "athr_2", "job_id": "ajob_2", "pr_url": _URL[:-1] + "8"}
    repository = _Repository([{**_CLAIM, "failures": 100}, second])

    def handler(request):
        if request.url.path.endswith("/7"):
            raise httpx.ReadTimeout("private-token", request=request)
        return httpx.Response(200, json={**_MERGED, "number": 8, "html_url": second["pr_url"]})

    assert await _poll(repository, handler) == 1
    assert repository.completed[0]["thread_id"] == "athr_2"
    assert repository.rescheduled[0]["error_code"] == "github_timeout"
    assert repository.rescheduled[0]["terminal"] is False
    assert repository.rescheduled[0]["delay_s"] == 3600
    assert "private-token" not in caplog.text


async def test_changed_publication_retries_instead_of_reporting_archive():
    repository = _Repository(receipt=False)
    assert await _poll(repository) == 0
    assert repository.rescheduled[0]["error_code"] == "association_changed"


async def test_archive_storage_failure_is_retryable_and_sanitized(caplog):
    repository = _Repository(fail_completion=True)
    assert await _poll(repository) == 0
    assert repository.rescheduled[0]["error_code"] == "pr_archive_check_failed"
    assert "secret storage DSN" not in caplog.text


async def test_repository_entitlement_is_checked_before_minting(monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ALLOWLIST", "")
    app = SimpleNamespace(token_for=AsyncMock(), cached_user_repositories=lambda *args: None)
    source_control = SimpleNamespace(list_repo_grants=AsyncMock(return_value=[]))
    repository = _Repository()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        await pr_archive.poll_pr_merge_archives(
            SimpleNamespace(pr_archive=repository, source_control=source_control),
            github_app=SimpleNamespace(get=lambda: app),
            client=client,
        )
    app.token_for.assert_not_called()
    assert repository.rescheduled[0]["error_code"] == "repo_access_denied"


async def test_live_app_credentials_use_only_repo_scoped_pr_read_permission():
    repository = _Repository()
    app = SimpleNamespace(token_for=AsyncMock(return_value="read-token"))
    provider = SimpleNamespace(get=lambda: app)
    calls = []

    def handler(request):
        calls.append(request.headers["Authorization"])
        return httpx.Response(200, json=_MERGED)

    assert await _poll(repository, handler, github_app=provider) == 1
    app.token_for.assert_awaited_once_with(
        "o/n", permissions={"pull_requests": "read"}, repository_scoped=True
    )
    assert calls == ["Bearer read-token"]
    replacement = SimpleNamespace(token_for=AsyncMock(return_value="rotated-token"))
    provider.get = lambda: replacement
    assert await _poll(_Repository(), handler, github_app=provider) == 1
    assert calls[-1] == "Bearer rotated-token"


async def test_unavailable_app_never_falls_back_to_static_identity():
    repository = _Repository()

    def unavailable():
        raise SourceControlUnavailable("github")

    assert await _poll(repository, github_app=SimpleNamespace(get=unavailable)) == 0
    assert repository.claim_calls == []


async def test_disabled_or_unconfigured_workers_leave_durable_checks_pending(monkeypatch):
    repository = _Repository()
    assert await pr_archive.poll_pr_merge_archives(_store(repository)) == 0
    monkeypatch.setattr(pr_archive, "boolean_setting", lambda key, fallback: False)
    assert await _poll(repository) == 0
    assert repository.claim_calls == []


async def test_batch_limit_is_bounded_and_claimed_chunks_do_not_wait_for_earlier_checks(
    monkeypatch,
):
    monkeypatch.setattr(
        pr_archive,
        "integer_setting",
        lambda key, fallback: 10 if key == "pr_archive_batch_size" else fallback,
    )
    repository = _Repository([{**_CLAIM, "thread_id": str(i)} for i in range(11)])
    assert await _poll(repository) == 10
    assert [c["limit"] for c in repository.claim_calls] == [8, 2]
    assert len(repository.claims) == 1


async def test_loop_survives_database_failure_and_reloads_settings(monkeypatch):
    passes = AsyncMock(side_effect=[ConnectionError("private DSN"), 0])
    intervals = []
    monkeypatch.setattr(pr_archive, "poll_pr_merge_archives", passes)
    monkeypatch.setattr(pr_archive, "number_setting", lambda key, fallback: 17)

    async def sleep(interval):
        intervals.append(interval)
        if len(intervals) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(pr_archive.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await pr_archive.pr_archive_loop(_store(_Repository()))
    assert passes.await_count == 2
    assert intervals == [17, 17]
