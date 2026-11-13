"""The GitHub relay listener: nothing but the relay, with the App's credentials loaded."""

from __future__ import annotations

import contextlib

import pytest

from cloud_agent import app as app_module


@pytest.mark.asyncio
async def test_the_listener_loads_source_control_without_owning_background_work(monkeypatch):
    seen: list[dict] = []

    @contextlib.asynccontextmanager
    async def recording(app, **kwargs):
        seen.append(kwargs)
        yield

    monkeypatch.setattr(app_module, "lifespan", recording)
    relay = app_module.create_github_relay_app()
    async with relay.router.lifespan_context(relay):
        pass

    # Without source control an owner's token could not be renewed, and every
    # request about the conversation's repository would fail.
    assert seen == [{"background": False, "source_control": True}]
    # Every path is the relay's, which answers only for GitHub's two names.
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=relay), base_url="http://other") as client:
        for path in ("/docs", "/openapi.json", "/healthz", "/v1/agent/jobs"):
            assert (await client.get(path)).status_code == 421, path
