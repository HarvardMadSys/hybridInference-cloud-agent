"""The GitHub relay, end to end: placeholder in, the owner's token out, or nothing at all."""

from __future__ import annotations

import base64
import gzip
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import github_relay
from cloud_agent.api.deps import get_agent_app_credentials, get_http_client, get_storage
from cloud_agent.tokens import mint_residency_github_token

pytestmark = pytest.mark.asyncio

_OID = "a" * 40
_ZERO = "0" * 40
_OWNER_TOKEN = "ghu_owner_token"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "github-relay-test-secret")


class _Store:
    """A running turn of ``athr_1`` on ``owner/repo``, owned by ``user_1``."""

    def __init__(self, *, running: bool = True, connected: bool = True) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.cleared: list[str] = []
        store = self

        class Relay:
            async def resolve_github_caller(self, *, thread_id, residency_generation):
                if not running or (thread_id, residency_generation) != ("athr_1", 2):
                    return None
                return {
                    "job_id": "ajob_1",
                    "attempt_id": 1,
                    "thread_id": thread_id,
                    "user_id": "user_1",
                    "repo": "owner/repo",
                }

            async def record_pull_request(self, **row):
                store.recorded.append(row)

        class SourceControl:
            async def get_github_account(self, *, user_id):
                if not connected:
                    return None
                return {
                    "access_token_ciphertext": "ciphertext",
                    "access_expires_at": datetime.now(UTC) + timedelta(hours=1),
                }

            async def clear_github_tokens(self, *, user_id, access_token_ciphertext):
                store.cleared.append(user_id)
                return True

        self.github_relay = Relay()
        self.source_control = SourceControl()


class _GitHub:
    """GitHub, answering every request with ``status`` and recording what it got."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.status = status
        self.body = body if body is not None else {"ok": True}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(await request.aread())
        # A stream, as from the network: the relay streams it back unread.
        return httpx.Response(
            self.status,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(self.body).encode()),
        )


async def _send(
    store: _Store,
    github: _GitHub,
    method: str,
    url: str,
    *,
    auth: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    app = FastAPI()
    app.include_router(github_relay.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_agent_app_credentials] = lambda: SimpleNamespace(
        cipher=SimpleNamespace(decrypt=lambda ciphertext: _OWNER_TOKEN)
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(github.handle))
    app.dependency_overrides[get_http_client] = lambda: upstream
    headers = dict(kwargs.pop("headers", {}))
    if auth is not None:
        headers["Authorization"] = auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://x") as client:
            return await client.request(method, url, headers=headers, **kwargs)
    finally:
        await upstream.aclose()


def _placeholder(generation: int = 2) -> str:
    return mint_residency_github_token(thread_id="athr_1", residency_generation=generation)


def _basic(token: str) -> str:
    return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _pkt(text: str) -> bytes:
    return f"{len(text) + 4:04x}".encode() + text.encode()


async def test_the_conversation_s_repository_is_reached_with_the_owner_s_token():
    github = _GitHub()
    response = await _send(
        _Store(),
        github,
        "GET",
        "https://api.github.com/repos/owner/repo/pulls?state=open",
        auth=f"token {_placeholder()}",
    )
    assert response.status_code == 200
    (sent,) = github.requests
    assert str(sent.url) == "https://api.github.com/repos/owner/repo/pulls?state=open"
    assert sent.headers["authorization"] == f"Bearer {_OWNER_TOKEN}"


async def test_another_repository_is_reached_as_nobody():
    github = _GitHub()
    await _send(
        _Store(),
        github,
        "GET",
        "https://api.github.com/repos/other/repo/pulls",
        auth=f"token {_placeholder()}",
    )
    (sent,) = github.requests
    assert "authorization" not in sent.headers


async def test_a_placeholder_without_a_running_turn_buys_nothing():
    github = _GitHub()
    stopped = await _send(
        _Store(running=False),
        github,
        "GET",
        "https://api.github.com/repos/owner/repo",
        auth=f"token {_placeholder()}",
    )
    rotated = await _send(
        _Store(),
        github,
        "GET",
        "https://api.github.com/repos/owner/repo",
        auth=f"token {_placeholder(generation=1)}",
    )
    forged = await _send(
        _Store(),
        github,
        "GET",
        "https://api.github.com/repos/owner/repo",
        auth="token agh.e30.AAAA",
    )
    assert [stopped.status_code, rotated.status_code, forged.status_code] == [401, 401, 401]
    assert github.requests == []


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://api.github.com/graphql"),
        ("PATCH", "https://api.github.com/repos/owner/repo"),
        ("GET", "https://api.github.com/repos/owner/repo/actions/secrets"),
        ("DELETE", "https://api.github.com/repos/owner/repo/git/refs/heads/dev"),
    ],
)
async def test_refused_requests_never_reach_github(method, url):
    github = _GitHub()
    response = await _send(_Store(), github, method, url, auth=f"token {_placeholder()}")
    assert response.status_code == 403
    assert "message" in response.json()
    assert github.requests == []


async def test_an_owner_without_github_is_told_to_connect_it():
    github = _GitHub()
    response = await _send(
        _Store(connected=False),
        github,
        "GET",
        "https://api.github.com/repos/owner/repo",
        auth=f"token {_placeholder()}",
    )
    assert response.status_code == 403
    assert "connected GitHub" in response.json()["message"]
    assert github.requests == []


async def test_an_owner_s_own_credential_passes_through_untouched():
    github = _GitHub()
    await _send(
        _Store(),
        github,
        "POST",
        "https://api.github.com/graphql",
        auth="Bearer ghp_owner_injected",
        json={"query": "{viewer{login}}"},
    )
    (sent,) = github.requests
    assert sent.headers["authorization"] == "Bearer ghp_owner_injected"


async def test_an_escaped_repository_segment_is_refused():
    github = _GitHub()
    response = await _send(
        _Store(),
        github,
        "GET",
        "https://api.github.com/repos/owner%2Frepo/x/pulls",
        auth=f"token {_placeholder()}",
    )
    assert response.status_code == 400
    assert github.requests == []


async def test_a_created_pull_request_is_recorded_for_the_conversation():
    github = _GitHub(
        201,
        {
            "number": 42,
            "html_url": "https://github.com/owner/repo/pull/42",
            "head": {"ref": "fix/model-picker"},
            "base": {"ref": "dev"},
            "draft": True,
        },
    )
    store = _Store()
    response = await _send(
        store,
        github,
        "POST",
        "https://api.github.com/repos/owner/repo/pulls",
        auth=f"token {_placeholder()}",
        json={"title": "Fix", "head": "fix/model-picker", "base": "dev", "draft": True},
    )
    assert response.status_code == 201
    assert response.json()["number"] == 42
    assert json.loads(github.bodies[0])["head"] == "fix/model-picker"
    assert store.recorded == [
        {
            "thread_id": "athr_1",
            "job_id": "ajob_1",
            "user_id": "user_1",
            "repo": "owner/repo",
            "number": 42,
            "url": "https://github.com/owner/repo/pull/42",
            "head_ref": "fix/model-picker",
            "base_ref": "dev",
            "draft": True,
        }
    ]


async def test_creating_a_tag_through_the_api_is_refused():
    github = _GitHub()
    response = await _send(
        _Store(),
        github,
        "POST",
        "https://api.github.com/repos/owner/repo/git/refs",
        auth=f"token {_placeholder()}",
        json={"ref": "refs/tags/v1", "sha": _OID},
    )
    assert response.status_code == 403
    assert github.requests == []


async def test_a_branch_push_is_forwarded_byte_for_byte_as_the_owner():
    github = _GitHub()
    body = _pkt(f"{_ZERO} {_OID} refs/heads/fix\0report-status\n") + b"0000PACK-bytes"
    response = await _send(
        _Store(),
        github,
        "POST",
        "https://github.com/owner/repo.git/git-receive-pack",
        auth=_basic(_placeholder()),
        content=body,
        headers={"Content-Type": "application/x-git-receive-pack-request"},
    )
    assert response.status_code == 200
    assert github.bodies == [body]
    assert github.requests[0].headers["authorization"] == _basic(_OWNER_TOKEN)


@pytest.mark.parametrize(
    "command",
    [f"{_OID} {_ZERO} refs/heads/dev", f"{_ZERO} {_OID} refs/tags/v1"],
    ids=["delete", "tag"],
)
@pytest.mark.parametrize("encoding", ["identity", "gzip"])
async def test_a_push_that_deletes_or_tags_is_refused_as_git_reports_it(command, encoding):
    github = _GitHub()
    body = _pkt(f"{command}\0report-status\n") + b"0000PACK"
    headers = {"Content-Type": "application/x-git-receive-pack-request"}
    if encoding == "gzip":
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    response = await _send(
        _Store(),
        github,
        "POST",
        "https://github.com/owner/repo.git/git-receive-pack",
        auth=_basic(_placeholder()),
        content=body,
        headers=headers,
    )
    assert response.status_code == 200
    assert response.content.startswith(b"00") and b"ERR " in response.content
    assert github.requests == []


async def test_a_token_github_refuses_is_forgotten_so_the_owner_reconnects():
    github = _GitHub(401, {"message": "Bad credentials"})
    store = _Store()
    response = await _send(
        store,
        github,
        "GET",
        "https://api.github.com/repos/owner/repo",
        auth=f"token {_placeholder()}",
    )
    assert response.status_code == 401
    assert store.cleared == ["user_1"]


async def test_only_github_s_two_names_are_served():
    github = _GitHub()
    response = await _send(
        _Store(), github, "GET", "https://example.com/", auth=f"token {_placeholder()}"
    )
    assert response.status_code == 421
    assert github.requests == []
