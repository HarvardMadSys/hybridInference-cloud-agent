"""Publication borrows the control plane's shared HTTP pool.

A publication's pull-request exchange rides the pool the loop is lent, and
opened a client of its own before. A borrowed client is
left open, and its own redirect setting never decides where a token goes.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import Mock

import httpx
import pytest
from tests.unit.test_agent_publish_worker import (  # noqa: F401 - the autouse fixture
    _JOB,
    FakeStore,
    _entitled,
    _patch_pr,
    _patch_publish,
)

from cloud_agent.publish_worker import GitHubCredential, ensure_draft_pull_request, publish_loop
from cloud_agent.publisher import PublishResult, TransientPublishError

pytestmark = pytest.mark.asyncio


async def test_the_pull_request_exchange_rides_one_borrowed_client(monkeypatch):
    """The lookup, the refresh and the create share the lender's pool and open none."""
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"number": 7, "state": "open", "html_url": "https://github.com/o/n/pull/7"}],
            )
        if request.method == "PATCH":
            return httpx.Response(200, json={"state": "closed"})
        return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as borrowed:
        monkeypatch.setattr(
            httpx, "AsyncClient", Mock(side_effect=AssertionError("opened a client of its own"))
        )
        url = await ensure_draft_pull_request(
            repo="o/n",
            head_branch="agent/athr_1",
            base_branch="dev",
            title="[agent] Fix retries",
            body="Summary",
            credential=GitHubCredential("t"),
            client=borrowed,
        )
        assert not borrowed.is_closed, "the pool belongs to whoever lent it"
    assert url == "https://github.com/o/n/pull/8"
    assert requests == ["GET", "PATCH", "POST"]


@pytest.mark.parametrize("step", ["GET", "POST"])
async def test_a_borrowed_client_never_carries_the_token_through_a_redirect(step):
    """The lender's redirect setting must not decide where an installation token goes."""
    asked: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append((request.method, request.url.host))
        if request.method == step:
            return httpx.Response(307, headers={"location": "https://elsewhere.example/pulls"})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"html_url": "https://github.com/o/n/pull/8"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as borrowed:
        with pytest.raises(TransientPublishError):
            await ensure_draft_pull_request(
                repo="o/n",
                head_branch="agent/athr_1",
                base_branch="dev",
                title="[agent] Fix retries",
                body="Summary",
                credential=GitHubCredential("t"),
                client=borrowed,
            )
    assert asked[-1] == (step, "api.github.com")
    assert {host for _method, host in asked} == {"api.github.com"}


async def test_the_loop_lends_its_pool_to_each_pull_request(monkeypatch):
    """A publication's GitHub calls used to open a client of their own."""
    store = FakeStore(dict(_JOB))
    _patch_publish(
        monkeypatch, PublishResult(branch="agent/ajob_1", commit_sha="s", changed_files=["x"])
    )
    opened = _patch_pr(monkeypatch)
    shared = object()
    task = asyncio.create_task(
        publish_loop(
            store,
            credential_provider=lambda: GitHubCredential("t"),
            interval_seconds=0.01,
            http=shared,
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.01)
        if store.snapshot_recorded:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert store.snapshot_recorded == (9, "https://github.com/o/n/pull/7", "s")
    assert opened["client"] is shared
