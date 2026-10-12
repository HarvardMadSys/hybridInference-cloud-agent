"""Directory sync stays complete, durable and independent of MCP connections."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api.mcp_connections import router
from cloud_agent.mcp.catalog import McpCatalog, catalog_entry, fetch_catalog
from cloud_agent.mcp.config_import import parse_import


def record(name="com.example/docs", *, status="active", **server):
    return {
        "server": {
            "name": name,
            "title": "Docs",
            "description": "Team knowledge",
            "version": "1.2.3",
            **server,
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {"status": status, "isLatest": True}
        },
    }


def test_missing_title_uses_publisher_for_generic_mcp_name():
    entry = catalog_entry(record("com.notion/mcp", title=None))
    assert entry["label"] == "Notion"
    assert entry["name"] == "com.notion/mcp"


def test_catalog_preserves_a_usable_public_icon_and_ignores_invalid_metadata():
    entry = catalog_entry(
        record(
            icons=[
                {"src": "https://127.0.0.1/private.svg"},
                {"src": "data:image/svg+xml,untrusted"},
                {"src": "https://example.com/dark.svg", "theme": "dark"},
                {"src": "https://example.com/tiny.png", "sizes": ["16x16"]},
                {"src": "https://example.com/icon.png", "sizes": ["96x96"]},
            ]
        )
    )
    assert entry["icon_url"] == "https://example.com/icon.png"
    assert catalog_entry(record(icons="invalid"))["icon_url"] is None


def test_recommended_view_uses_exact_publishers_and_current_order_on_cached_data():
    def entry(name, title="Docs", *, supported=True):
        remotes = [{"type": "streamable-http", "url": "https://docs.example.com/mcp"}]
        return catalog_entry(record(name, title=title, remotes=remotes if supported else []))

    catalog = McpCatalog(AsyncMock(), AsyncMock())
    # Deliberately in the old/alphabetical order, with a copycat display name
    # and a known publisher whose current recipe cannot be generated.
    catalog.snapshot = {
        "entries": [
            entry("com.example/github", "GitHub"),
            entry("app.linear/linear", "Linear"),
            entry("io.github.upstash/context7", "Context7"),
            entry("io.github.github/github-mcp-server", "GitHub"),
            entry("com.notion/mcp", "Notion", supported=False),
        ],
        "synced_at": datetime.now(timezone.utc),
    }
    first = catalog.search(limit=2, recommended=True)
    assert [e["name"] for e in first["entries"]] == [
        "io.github.github/github-mcp-server",
        "io.github.upstash/context7",
    ]
    assert first["total"] == 3
    assert first["next_offset"] == 2
    assert catalog.search(offset=2, recommended=True)["entries"][0]["name"] == "app.linear/linear"
    assert catalog.search()["total"] == 5
    assert catalog.search("com.example/github")["total"] == 1


def test_remote_choices_and_secret_headers_round_trip_through_importer():
    entry = catalog_entry(
        record(
            remotes=[
                {"type": "streamable-http", "url": "https://docs.example.com/mcp"},
                {"type": "sse", "url": "https://docs.example.com/sse"},
                {
                    "type": "streamable-http",
                    "url": "https://docs.example.com/private",
                    "headers": [
                        {
                            "name": "Authorization",
                            "isRequired": True,
                            "isSecret": True,
                            "value": "Bearer {token}",
                        },
                    ],
                },
            ]
        )
    )
    assert entry and len(entry["configurations"]) == 3
    drafts = [parse_import(option["json"]).drafts[0] for option in entry["configurations"]]
    assert [str(draft.transport) for draft in drafts] == [
        "streamable-http",
        "http-sse",
        "streamable-http",
    ]
    assert drafts[-1].header_auth.scheme == "Bearer"
    assert drafts[-1].secrets[0].value is None


@pytest.mark.parametrize(
    "remote",
    [
        {"type": "streamable-http", "url": "http://docs.example.com/mcp"},
        {"type": "streamable-http", "url": "https://127.0.0.1/mcp"},
        {"type": "streamable-http", "url": "https://docs.example.com/{tenant}"},
        {
            "type": "streamable-http",
            "url": "https://docs.example.com/mcp",
            "headers": [
                {"name": "X-Region", "isRequired": True, "value": "us-east"},
            ],
        },
        {
            "type": "streamable-http",
            "url": "https://docs.example.com/mcp",
            "headers": [
                {"name": "Authorization", "isRequired": True},
                {"name": "X-Key", "isRequired": True},
            ],
        },
    ],
)
def test_incomplete_or_unsupported_recipes_are_listed_without_a_broken_install(remote):
    entry = catalog_entry(record(remotes=[remote]))
    assert entry and entry["configurations"] == []


def test_package_is_pinned_and_uses_existing_location_and_secret_flow():
    entry = catalog_entry(
        record(
            packages=[
                {
                    "registryType": "npm",
                    "identifier": "@example/docs",
                    "version": "1.2.3",
                    "transport": {"type": "stdio"},
                    "environmentVariables": [
                        {"name": "API_KEY", "isRequired": True, "isSecret": True}
                    ],
                }
            ]
        )
    )
    draft = parse_import(entry["configurations"][0]["json"]).drafts[0]
    assert draft.location == "user-connector"
    assert draft.launch.args == ("-y", "@example/docs@1.2.3")
    assert draft.secrets[0].name == "API_KEY"
    assert draft.secrets[0].value is None


def test_required_package_arguments_are_not_silently_dropped():
    entry = catalog_entry(
        record(
            packages=[
                {
                    "registryType": "npm",
                    "identifier": "@example/docs",
                    "version": "1.2.3",
                    "transport": {"type": "stdio"},
                    "packageArguments": [{"type": "positional", "isRequired": True}],
                }
            ]
        )
    )
    assert not entry["configurations"]


async def test_paginated_latest_sync_filters_inactive_and_retains_later_pages():
    requests = []

    def handle(request):
        requests.append(request)
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "servers": [record(), record("com.example/old", status="deleted")],
                    "metadata": {"nextCursor": "opaque:+/cursor"},
                },
            )
        assert request.url.params["cursor"] == "opaque:+/cursor"
        return httpx.Response(200, json={"servers": [record("com.example/second")], "metadata": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        entries = await fetch_catalog(client)
    assert {entry["name"] for entry in entries} == {"com.example/docs", "com.example/second"}
    assert all(request.url.params["version"] == "latest" for request in requests)


@pytest.mark.parametrize("failure", ["http", "cursor", "empty"])
async def test_failed_sync_keeps_durable_snapshot_and_does_not_publish_partial_results(failure):
    old = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    repo = AsyncMock()
    repo.load_mcp_catalog.return_value = old

    def handle(request):
        if failure == "empty":
            return httpx.Response(200, json={"servers": [], "metadata": {}})
        if failure == "http" and "cursor" in request.url.params:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"servers": [record("com.example/new")], "metadata": {"nextCursor": "same"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = McpCatalog(repo, client)
        with pytest.raises((httpx.HTTPError, ValueError)):
            await catalog.refresh()
        assert catalog.snapshot == old
        assert catalog.search()["stale"] is True
        repo.save_mcp_catalog.assert_not_called()


async def test_restart_uses_fresh_cache_without_network_and_searches_locally():
    repo = AsyncMock()
    repo.load_mcp_catalog.return_value = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc),
    }
    client = AsyncMock()
    catalog = McpCatalog(repo, client)
    await catalog.refresh()
    assert catalog.search("EXAMPLE knowledge")["total"] == 1
    assert catalog.search("absent")["total"] == 0
    client.stream.assert_not_called()
    repo.save_mcp_catalog.assert_not_called()


async def test_saved_registry_url_replaces_fresh_catalog_on_next_refresh(monkeypatch):
    from cloud_agent.mcp import catalog as catalog_module

    values = {"mcp_registry_url": "https://registry.modelcontextprotocol.io"}
    monkeypatch.setattr(
        catalog_module, "text_setting", lambda name, default=None: values.get(name, default)
    )
    monkeypatch.setattr(
        catalog_module,
        "number_setting",
        lambda name, default=None: 7 if name == "mcp_catalog_request_timeout_s" else default,
    )
    repo = AsyncMock()
    repo.load_mcp_catalog.return_value = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc),
    }
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"servers": [record("com.example/new")], "metadata": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        catalog = McpCatalog(repo, http)
        await catalog.refresh()
        assert not requests
        values["mcp_registry_url"] = "https://registry.example"
        await catalog.refresh()
    assert str(requests[0].url).startswith("https://registry.example/v0.1/servers")
    assert requests[0].extensions["timeout"]["read"] == 7
    assert catalog.search()["entries"][0]["name"] == "com.example/new"


async def test_full_refresh_removes_deleted_entries_and_commits_before_serving():
    repo = AsyncMock()
    repo.load_mcp_catalog.return_value = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "servers": [record(status="deleted"), record("com.example/new")],
                    "metadata": {},
                },
            )
        )
    ) as client:
        catalog = McpCatalog(repo, client)
        await catalog.refresh()
    assert [entry["name"] for entry in catalog.search()["entries"]] == ["com.example/new"]
    repo.save_mcp_catalog.assert_awaited_once()


async def test_background_failure_retries_and_shutdown_cancels_cleanly(monkeypatch):
    catalog = McpCatalog(AsyncMock(), AsyncMock())
    catalog.refresh = AsyncMock(side_effect=RuntimeError("registry unavailable"))
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("cloud_agent.mcp.catalog.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await catalog.run()
    sleep.assert_awaited_once_with(300)


def test_catalog_route_requires_owner_and_validates_pagination():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_agent_owner] = lambda: "owner"
    client = TestClient(app)
    assert client.get("/v1/agent/mcp/catalog").json()["synced_at"] is None
    assert client.get("/v1/agent/mcp/catalog?offset=-1").status_code == 422
    assert client.get("/v1/agent/mcp/catalog?limit=101").status_code == 422
    assert client.get("/v1/agent/mcp/catalog?recommended=unknown").status_code == 422
    catalog = McpCatalog(AsyncMock(), AsyncMock())
    catalog.snapshot = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc),
    }
    app.state.mcp_catalog = catalog
    assert client.get("/v1/agent/mcp/catalog?recommended=true").json()["total"] == 0
    assert client.get("/v1/agent/mcp/catalog?recommended=false").json()["total"] == 1
    app.dependency_overrides.clear()
    assert client.get("/v1/agent/mcp/catalog").status_code == 401
