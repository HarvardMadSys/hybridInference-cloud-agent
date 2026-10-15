"""Looking up the sandbox image the release pipeline published for a commit."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cloud_agent.providers.compute.sandbox_image import (
    DEFAULT_SANDBOX_IMAGE_REPOSITORY,
    find_published_sandbox_image,
    sandbox_image_repository,
)

_COMMIT = "e" * 40
_DIGEST = "sha256:" + "d" * 64


def _registry(*, tag_status: int = 200, digest: str = _DIGEST) -> tuple[Any, list[httpx.Request]]:
    """A registry that challenges anonymous readers, like GHCR."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/token":
            assert (
                request.url.params["scope"] == "repository:harvardmadsys/cloud-agent-sandbox:pull"
            )
            return httpx.Response(200, json={"token": "anonymous"})
        assert request.url.path == (
            f"/v2/harvardmadsys/cloud-agent-sandbox/manifests/sandbox-{_COMMIT}"
        )
        if request.headers.get("authorization") != "Bearer anonymous":
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer realm="https://ghcr.io/token",'
                    'service="ghcr.io",'
                    'scope="repository:harvardmadsys/cloud-agent-sandbox:pull"'
                },
            )
        return httpx.Response(tag_status, headers={"docker-content-digest": digest})

    return httpx.MockTransport(handle), seen


@pytest.mark.asyncio
async def test_the_published_image_is_pinned_by_digest():
    transport, seen = _registry()

    image = await find_published_sandbox_image(
        _COMMIT, repository=DEFAULT_SANDBOX_IMAGE_REPOSITORY, transport=transport
    )

    assert image == f"{DEFAULT_SANDBOX_IMAGE_REPOSITORY}@{_DIGEST}"
    assert [request.method for request in seen] == ["HEAD", "GET", "HEAD"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry",
    [
        {"tag_status": 404},
        {"digest": "sha256:short"},
    ],
)
async def test_an_unpublished_commit_builds_on_the_host(registry):
    transport, _ = _registry(**registry)

    assert (
        await find_published_sandbox_image(
            _COMMIT, repository=DEFAULT_SANDBOX_IMAGE_REPOSITORY, transport=transport
        )
        is None
    )


@pytest.mark.asyncio
async def test_an_unreachable_registry_builds_on_the_host():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    assert (
        await find_published_sandbox_image(
            _COMMIT,
            repository=DEFAULT_SANDBOX_IMAGE_REPOSITORY,
            transport=httpx.MockTransport(refuse),
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commit", "repository"),
    [
        (_COMMIT, ""),
        (_COMMIT, "cloud-agent-sandbox"),
        ("not-a-commit", DEFAULT_SANDBOX_IMAGE_REPOSITORY),
    ],
)
async def test_nothing_is_asked_without_a_registry_or_a_commit(commit, repository):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"asked {request.url}")

    assert (
        await find_published_sandbox_image(
            commit, repository=repository, transport=httpx.MockTransport(unreachable)
        )
        is None
    )


def test_the_repository_is_configurable_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv("AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY", raising=False)
    assert sandbox_image_repository() == DEFAULT_SANDBOX_IMAGE_REPOSITORY
    monkeypatch.setenv("AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY", "registry.example.org/sandbox")
    assert sandbox_image_repository() == "registry.example.org/sandbox"
    monkeypatch.setenv("AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY", "")
    assert sandbox_image_repository() == ""
