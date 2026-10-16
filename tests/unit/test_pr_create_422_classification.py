"""PR creation distinguishes validation errors from uncertain create races."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx
import pytest

from cloud_agent.publish_worker import (
    GitHubCredential,
    _is_retryable_publication_failure,
    ensure_draft_pull_request,
)
from cloud_agent.publisher import PublishError, TransientPublishError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_PR_URL = "https://github.example/o/n/pull/17"
_PR = {"number": 17, "state": "open", "html_url": _PR_URL}


@asynccontextmanager
async def _github_api(responder) -> AsyncIterator[tuple[str, list[tuple[str, str, bytes]]]]:
    requests: list[tuple[str, str, bytes]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers = {
                name.strip().lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for name, value in (line.split(":", 1),)
            }
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            requests.append((method, target, body))
            result = await responder(method, target, body)
            if result is None:
                # The remote commits the create but loses the HTTP response.
                writer.close()
                return
            status, payload = result
            body_bytes = json.dumps(payload).encode()
            reason = HTTPStatus(status).phrase
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + body_bytes
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        server.close()
        await server.wait_closed()


def _credential(api_base: str) -> GitHubCredential:
    return GitHubCredential("local-test-token", api_base=api_base)


@pytest.mark.parametrize(
    ("fields", "named", "retryable"),
    [
        (["head"], "head", True),
        (["base"], "base", False),
        (["head", "base"], "base", False),
    ],
    ids=["head-retried", "base-permanent", "both-permanent"],
)
async def test_invalid_target_422_names_the_bad_ref_and_retries_only_the_head(
    fields: list[str], named: str, retryable: bool
) -> None:
    """GitHub may not see a branch pushed a moment ago; a bad base stays bad.

    The publisher opens the draft right after its own push, and GitHub can
    answer ``head invalid`` until the new branch reaches its pull-request API,
    so that refusal goes back to the durable retry budget. An invalid base is
    the branch the owner chose, which no retry changes, so it stays permanent,
    also when GitHub names the head beside it.
    """

    async def respond(method: str, _target: str, _body: bytes):
        if method == "GET":
            return 200, []
        assert method == "POST"
        return 422, {
            "message": "Validation Failed",
            "errors": [
                {"resource": "PullRequest", "field": field, "code": "invalid"} for field in fields
            ],
        }

    async with _github_api(respond) as (api, requests):
        with pytest.raises(PublishError) as raised:
            await ensure_draft_pull_request(
                repo="o/n",
                head_branch="missing-branch",
                base_branch="dev",
                title="Title",
                body="Body",
                credential=_credential(api),
            )

    assert isinstance(raised.value, TransientPublishError) is retryable
    assert _is_retryable_publication_failure(raised.value) is retryable
    assert f"{named} branch" in str(raised.value)
    assert [method for method, _target, _body in requests] == ["GET", "POST"]


@pytest.mark.parametrize("lost_response", [False, True], ids=["duplicate-422", "lost-response"])
async def test_ambiguous_create_reconciles_when_the_pr_becomes_visible(lost_response: bool) -> None:
    created = False

    async def respond(method: str, _target: str, _body: bytes):
        nonlocal created
        if method == "GET":
            return 200, [_PR] if created else []
        if method == "PATCH":
            return 200, {"state": "open", "html_url": _PR_URL}
        assert method == "POST"
        created = True
        if lost_response:
            return None
        return 422, {
            "message": "Validation Failed",
            "errors": [{"message": "A pull request already exists for o/n:agent/task"}],
        }

    async with _github_api(respond) as (api, requests):
        if lost_response:
            with pytest.raises(httpx.RemoteProtocolError) as raised:
                await ensure_draft_pull_request(
                    repo="o/n",
                    head_branch="agent/task",
                    base_branch="dev",
                    title="Title",
                    body="Body",
                    credential=_credential(api),
                )
        else:
            with pytest.raises(TransientPublishError) as raised:
                await ensure_draft_pull_request(
                    repo="o/n",
                    head_branch="agent/task",
                    base_branch="dev",
                    title="Title",
                    body="Body",
                    credential=_credential(api),
                )
        assert _is_retryable_publication_failure(raised.value)
        url = await ensure_draft_pull_request(
            repo="o/n",
            head_branch="agent/task",
            base_branch="dev",
            title="Title",
            body="Body",
            credential=_credential(api),
        )

    assert url == _PR_URL
    assert [method for method, _target, _body in requests] == ["GET", "POST", "GET", "PATCH"]


async def test_permission_refusal_remains_a_permanent_api_error() -> None:
    async def respond(method: str, _target: str, _body: bytes):
        if method == "GET":
            return 200, []
        assert method == "POST"
        return 403, {"message": "Resource not accessible by integration"}

    async with _github_api(respond) as (api, _requests):
        with pytest.raises(PublishError) as raised:
            await ensure_draft_pull_request(
                repo="o/n",
                head_branch="agent/task",
                base_branch="dev",
                title="Title",
                body="Body",
                credential=_credential(api),
            )

    assert not isinstance(raised.value, TransientPublishError)
    assert _is_retryable_publication_failure(raised.value) is False


async def test_lost_create_response_reconciles_after_delayed_pr_visibility() -> None:
    created = False
    hidden_gets_after_create = 2

    async def respond(method: str, _target: str, _body: bytes):
        nonlocal created, hidden_gets_after_create
        if method == "GET":
            if created and hidden_gets_after_create > 0:
                hidden_gets_after_create -= 1
                return 200, []
            return 200, [_PR] if created else []
        if method == "PATCH":
            return 200, {"state": "open", "html_url": _PR_URL}
        assert method == "POST"
        if created:
            return 422, {
                "message": "Validation Failed",
                "errors": [{"message": "A pull request already exists for o/n:agent/task"}],
            }
        created = True
        # GitHub accepted the create but the response was lost.
        return None

    async with _github_api(respond) as (api, requests):
        with pytest.raises(httpx.RemoteProtocolError) as lost:
            await ensure_draft_pull_request(
                repo="o/n",
                head_branch="agent/task",
                base_branch="dev",
                title="Title",
                body="Body",
                credential=_credential(api),
            )
        assert _is_retryable_publication_failure(lost.value)

        for _ in range(2):
            with pytest.raises(TransientPublishError) as duplicate:
                await ensure_draft_pull_request(
                    repo="o/n",
                    head_branch="agent/task",
                    base_branch="dev",
                    title="Title",
                    body="Body",
                    credential=_credential(api),
                )
            assert _is_retryable_publication_failure(duplicate.value)

        url = await ensure_draft_pull_request(
            repo="o/n",
            head_branch="agent/task",
            base_branch="dev",
            title="Title",
            body="Body",
            credential=_credential(api),
        )

    assert url == _PR_URL
    assert [method for method, _target, _body in requests] == [
        "GET",
        "POST",
        "GET",
        "POST",
        "GET",
        "POST",
        "GET",
        "PATCH",
    ]


async def test_write_permission_revoked_after_lookup_is_permanent() -> None:
    write_permission = True

    async def respond(method: str, _target: str, _body: bytes):
        nonlocal write_permission
        if method == "GET":
            assert write_permission
            # The read succeeds, then write permission is revoked before POST.
            write_permission = False
            return 200, []
        assert method == "POST"
        if not write_permission:
            return 403, {"message": "Resource not accessible by integration"}
        raise AssertionError("unexpectedly retained pull-request write permission")

    async with _github_api(respond) as (api, requests):
        with pytest.raises(PublishError) as raised:
            await ensure_draft_pull_request(
                repo="o/n",
                head_branch="agent/task",
                base_branch="dev",
                title="Title",
                body="Body",
                credential=_credential(api),
            )

    assert not isinstance(raised.value, TransientPublishError)
    assert _is_retryable_publication_failure(raised.value) is False
    assert [method for method, _target, _body in requests] == ["GET", "POST"]
