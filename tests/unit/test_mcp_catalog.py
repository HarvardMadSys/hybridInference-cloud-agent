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
from cloud_agent.ports.mcp_catalog import McpCatalogSave, McpCatalogSync

_REGISTRY = "https://registry.modelcontextprotocol.io"


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


def sync(
    registry_url: str = _REGISTRY, *, revision: int = 1, generation: int = 1
) -> McpCatalogSync:
    return McpCatalogSync(registry_url, revision, generation)


def repository(snapshot, *, source: McpCatalogSync | None = None):
    repo = AsyncMock()
    repo.load_mcp_catalog.return_value = snapshot
    authority = source or sync()
    repo.begin_mcp_catalog_sync.return_value = authority
    repo.save_mcp_catalog.return_value = McpCatalogSave(
        datetime.now(timezone.utc),
        authority.registry_url,
        authority.settings_revision,
    )
    return repo


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


def test_registry_metadata_cannot_amplify_one_url_across_unbounded_configurations():
    oversized_url = "https://docs.example.com/" + "x" * 9_000
    headers = [{"name": f"X-Api-Key-{index}"} for index in range(1_000)]

    long_url_entry = catalog_entry(
        record(
            remotes=[
                {
                    "type": "streamable-http",
                    "url": oversized_url,
                    "headers": [{"name": "Authorization"}],
                }
            ]
        )
    )
    excess_headers_entry = catalog_entry(
        record(
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://docs.example.com/mcp",
                    "headers": headers,
                }
            ]
        )
    )

    assert long_url_entry is not None
    assert long_url_entry["configurations"] == []
    assert excess_headers_entry is not None
    assert excess_headers_entry["configurations"] == []


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


async def test_malformed_records_and_nested_shapes_do_not_hide_valid_entries():
    malformed = [
        None,
        [],
        {"_meta": []},
        {"_meta": {"io.modelcontextprotocol.registry/official": []}},
        {
            "_meta": {
                "io.modelcontextprotocol.registry/official": {
                    "status": "active",
                    "isLatest": True,
                }
            },
            "server": [],
        },
    ]
    valid_with_bad_repository = record("com.example/bad-repository", repository="bad")
    valid_with_bad_optional_shapes = record(
        "com.example/bad-optional-shapes",
        icons=[{"src": "https://example.com/icon.png", "mimeType": []}],
        remotes=[{"type": [], "url": "https://example.com/mcp"}],
        packages=[
            {
                "registryType": "npm",
                "identifier": "example",
                "version": "1.0.0",
                "transport": {"type": "stdio"},
                "registryBaseUrl": 123,
            }
        ],
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "servers": [
                        *malformed,
                        record("com.example/valid"),
                        valid_with_bad_repository,
                        valid_with_bad_optional_shapes,
                    ],
                    "metadata": {},
                },
            )
        )
    ) as client:
        entries = await fetch_catalog(client)

    assert [entry["name"] for entry in entries] == [
        "com.example/bad-optional-shapes",
        "com.example/bad-repository",
        "com.example/valid",
    ]
    assert entries[0]["configurations"] == []
    assert entries[0]["icon_url"] is None
    assert entries[1]["website_url"] is None


@pytest.mark.parametrize("failure", ["http", "cursor", "empty"])
async def test_failed_sync_keeps_durable_snapshot_and_does_not_publish_partial_results(failure):
    old = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    repo = repository(old)

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


@pytest.mark.parametrize("cursor", [0, False, [], {}])
async def test_falsey_non_string_cursor_keeps_snapshot_and_restarts_scan(cursor):
    old = {
        "entries": [catalog_entry(record("com.example/old"))],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    repo = repository(old)
    requests = []

    def handle(request):
        requests.append(request.url.params.get("cursor"))
        return httpx.Response(
            200,
            json={
                "servers": [record("com.example/partial")],
                "metadata": {"nextCursor": cursor},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = McpCatalog(repo, client)
        with pytest.raises(ValueError, match="Invalid Registry cursor"):
            await catalog.refresh()
        with pytest.raises(ValueError, match="Invalid Registry cursor"):
            await catalog.refresh()

    assert requests == [None, None]
    assert catalog.snapshot == old
    repo.save_mcp_catalog.assert_not_called()


@pytest.mark.parametrize("terminal", [True, False])
async def test_registry_page_limit_accepts_only_a_terminal_thousandth_page(terminal):
    pages = 0

    def handle(_request):
        nonlocal pages
        pages += 1
        metadata = {} if terminal and pages == 1000 else {"nextCursor": f"cursor-{pages}"}
        return httpx.Response(
            200,
            json={"servers": [record(f"com.example/page-{pages}")], "metadata": metadata},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if terminal:
            entries = await fetch_catalog(client)
            assert len(entries) == 1000
        else:
            with pytest.raises(ValueError, match="Registry exceeded page limit"):
                await fetch_catalog(client)

    assert pages == 1000


async def test_resumable_checkpoint_has_an_aggregate_memory_bound(monkeypatch):
    from cloud_agent.mcp import catalog as catalog_module

    monkeypatch.setattr(catalog_module, "_MAX_CHECKPOINT_BYTES", 900)
    cursors = []

    def handle(request):
        cursor = request.url.params.get("cursor")
        cursors.append(cursor)
        page = 1 if cursor is None else 2
        return httpx.Response(
            200,
            json={
                "servers": [record(f"com.example/page-{page}", description="x" * 300)],
                "metadata": {"nextCursor": f"cursor-{page}"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = McpCatalog(repository(None), client)
        with pytest.raises(ValueError, match="checkpoint exceeded size limit"):
            await catalog.refresh()
        with pytest.raises(ValueError, match="checkpoint exceeded size limit"):
            await catalog.refresh()

    # Exceeding the bound releases the retained pages; a retry starts at page one.
    assert cursors == [None, "cursor-1", None, "cursor-1"]


async def test_oversized_normalized_page_stops_before_processing_every_record(monkeypatch):
    from cloud_agent.mcp import catalog as catalog_module

    monkeypatch.setattr(catalog_module, "_MAX_CHECKPOINT_BYTES", 2_500)
    original = catalog_module.catalog_entry
    processed = 0

    def tracked(record):
        nonlocal processed
        processed += 1
        return original(record)

    monkeypatch.setattr(catalog_module, "catalog_entry", tracked)
    records = [record(f"com.example/page-{index}", description="x" * 300) for index in range(20)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"servers": records, "metadata": {}})
        )
    ) as client:
        with pytest.raises(ValueError, match="checkpoint exceeded size limit"):
            await fetch_catalog(client)

    assert 1 < processed < len(records)


async def test_transient_page_failure_resumes_without_publishing_partial_results():
    old = {
        "entries": [catalog_entry(record("com.example/old"))],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    repo = repository(old)
    first_sync = sync(generation=1)
    repo.begin_mcp_catalog_sync.side_effect = [first_sync, sync(generation=2)]
    cursors = []
    fail_once = True

    def handle(request):
        nonlocal fail_once
        cursor = request.url.params.get("cursor")
        cursors.append(cursor)
        if cursor is None:
            return httpx.Response(
                200,
                json={
                    "servers": [record("com.example/first")],
                    "metadata": {"nextCursor": "continue-here"},
                },
            )
        assert cursor == "continue-here"
        if fail_once:
            fail_once = False
            raise httpx.ReadTimeout("transient Registry timeout", request=request)
        return httpx.Response(
            200,
            json={"servers": [record("com.example/second")], "metadata": {}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = McpCatalog(repo, client)
        with pytest.raises(httpx.ReadTimeout):
            await catalog.refresh()
        assert catalog.snapshot == old
        repo.save_mcp_catalog.assert_not_called()

        await catalog.refresh()

    assert cursors == [None, "continue-here", "continue-here"]
    assert {entry["name"] for entry in catalog.snapshot["entries"]} == {
        "com.example/first",
        "com.example/second",
    }
    repo.save_mcp_catalog.assert_awaited_once()
    assert repo.save_mcp_catalog.await_args.args[1] == first_sync


async def test_registry_change_discards_an_incomplete_checkpoint():
    old = {
        "entries": [catalog_entry(record("com.example/old"))],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    repo = repository(old)
    repo.load_mcp_catalog.return_value = old
    repo.begin_mcp_catalog_sync.side_effect = [
        sync("https://registry-one.example", revision=1, generation=1),
        sync("https://registry-two.example", revision=2, generation=2),
    ]
    requests = []

    def handle(request):
        cursor = request.url.params.get("cursor")
        requests.append((request.url.host, cursor))
        if request.url.host == "registry-one.example":
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "servers": [record("com.example/partial")],
                        "metadata": {"nextCursor": "old-source-cursor"},
                    },
                )
            raise httpx.ReadTimeout("old Registry timeout", request=request)
        assert request.url.host == "registry-two.example"
        assert cursor is None
        return httpx.Response(
            200,
            json={"servers": [record("com.example/replacement")], "metadata": {}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = McpCatalog(repo, client)
        with pytest.raises(httpx.ReadTimeout):
            await catalog.refresh()
        await catalog.refresh()

    assert requests == [
        ("registry-one.example", None),
        ("registry-one.example", "old-source-cursor"),
        ("registry-two.example", None),
    ]
    assert [entry["name"] for entry in catalog.snapshot["entries"]] == ["com.example/replacement"]


async def test_completed_fetch_retries_storage_commit_without_refetching():
    old = {
        "entries": [catalog_entry(record("com.example/old"))],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    repo = repository(old)
    repo.save_mcp_catalog.side_effect = [
        TimeoutError("database timeout"),
        McpCatalogSave(datetime.now(timezone.utc), _REGISTRY, 1),
    ]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"servers": [record("com.example/new")], "metadata": {}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = McpCatalog(repo, client)
        with pytest.raises(TimeoutError, match="database timeout"):
            await catalog.refresh()
        assert catalog.snapshot == old

        await catalog.refresh()

    assert len(requests) == 1
    assert catalog.snapshot["entries"][0]["name"] == "com.example/new"
    assert repo.save_mcp_catalog.await_count == 2


async def test_rejected_older_write_exposes_the_durable_winner_not_the_local_scan():
    old = {
        "entries": [catalog_entry(record("com.example/old"))],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    winner = {
        "entries": [catalog_entry(record("com.example/winner"))],
        "synced_at": datetime.now(timezone.utc) + timedelta(seconds=1),
        "registry_url": _REGISTRY,
        "settings_revision": 1,
        "generation": 2,
    }
    repo = repository(old)
    repo.load_mcp_catalog.side_effect = [old, winner]
    repo.save_mcp_catalog.return_value = McpCatalogSave(None, _REGISTRY, 1)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"servers": [record("com.example/loser")], "metadata": {}},
            )
        )
    ) as client:
        catalog = McpCatalog(repo, client)
        await catalog.refresh()

    assert catalog.snapshot == winner
    assert catalog.search()["entries"][0]["name"] == "com.example/winner"
    assert repo.load_mcp_catalog.await_count == 2
    repo.save_mcp_catalog.assert_awaited_once()


async def test_rejected_scan_does_not_adopt_an_intermediate_registry_winner():
    registry_a = "https://registry-a.example"
    registry_b = "https://registry-b.example"
    registry_c = "https://registry-c.example"
    old = {
        "entries": [catalog_entry(record("com.example/old"))],
        "synced_at": datetime.now(timezone.utc) - timedelta(hours=3),
        "registry_url": registry_a,
        "settings_revision": 1,
        "generation": 0,
    }
    intermediate_winner = {
        "entries": [catalog_entry(record("com.example/intermediate"))],
        "synced_at": datetime.now(timezone.utc),
        "registry_url": registry_b,
        "settings_revision": 2,
        "generation": 2,
    }
    repo = repository(old, source=sync(registry_a, revision=1, generation=1))
    repo.load_mcp_catalog.side_effect = [old, intermediate_winner, intermediate_winner]
    repo.begin_mcp_catalog_sync.side_effect = [
        sync(registry_a, revision=1, generation=1),
        sync(registry_c, revision=3, generation=3),
    ]
    repo.save_mcp_catalog.side_effect = [
        McpCatalogSave(None, registry_c, 3),
        McpCatalogSave(datetime.now(timezone.utc), registry_c, 3),
    ]
    requests = []

    def handle(request):
        requests.append(request.url.host)
        name = "com.example/a" if request.url.host == "registry-a.example" else "com.example/c"
        return httpx.Response(200, json={"servers": [record(name)], "metadata": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        catalog = McpCatalog(repo, client)
        with pytest.raises(RuntimeError, match="source fence changed"):
            await catalog.refresh()
        assert catalog.snapshot == old
        await catalog.refresh()

    assert requests == ["registry-a.example", "registry-c.example"]
    assert catalog.snapshot["registry_url"] == registry_c
    assert [entry["name"] for entry in catalog.snapshot["entries"]] == ["com.example/c"]


@pytest.mark.parametrize("fresh", [False, True])
async def test_invalid_registry_setting_keeps_the_durable_snapshot(fresh):
    cached = {
        "entries": [catalog_entry(record("com.example/cached"))],
        "synced_at": datetime.now(timezone.utc) - (timedelta() if fresh else timedelta(hours=3)),
    }
    repo = repository(cached, source=sync("http://private.example"))
    client = AsyncMock()

    catalog = McpCatalog(repo, client)

    with pytest.raises(ValueError, match="public HTTPS"):
        await catalog.refresh()
    assert catalog.snapshot == cached
    assert catalog.search()["entries"][0]["name"] == "com.example/cached"
    repo.save_mcp_catalog.assert_not_called()
    client.stream.assert_not_called()


async def test_restart_uses_fresh_cache_without_network_and_searches_locally():
    cached = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc),
        "registry_url": _REGISTRY,
        "settings_revision": 1,
        "generation": 1,
    }
    repo = repository(cached)
    client = AsyncMock()
    catalog = McpCatalog(repo, client)
    await catalog.refresh()
    assert catalog.search("EXAMPLE knowledge")["total"] == 1
    assert catalog.search("absent")["total"] == 0
    client.stream.assert_not_called()
    repo.save_mcp_catalog.assert_not_called()


async def test_saved_registry_url_replaces_fresh_catalog_on_next_refresh(monkeypatch):
    from cloud_agent.mcp import catalog as catalog_module

    monkeypatch.setattr(
        catalog_module,
        "number_setting",
        lambda name, default=None: 7 if name == "mcp_catalog_request_timeout_s" else default,
    )
    cached = {
        "entries": [catalog_entry(record())],
        "synced_at": datetime.now(timezone.utc),
        "registry_url": _REGISTRY,
        "settings_revision": 1,
        "generation": 1,
    }
    repo = repository(cached)
    repo.begin_mcp_catalog_sync.side_effect = [
        sync(),
        sync("https://registry.example", revision=2, generation=3),
    ]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"servers": [record("com.example/new")], "metadata": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        catalog = McpCatalog(repo, http)
        await catalog.refresh()
        assert not requests
        await catalog.refresh()
    assert str(requests[0].url).startswith("https://registry.example/v0.1/servers")
    assert requests[0].extensions["timeout"]["read"] == 7
    assert catalog.search()["entries"][0]["name"] == "com.example/new"


async def test_full_refresh_removes_deleted_entries_and_commits_before_serving():
    repo = repository(
        {
            "entries": [catalog_entry(record())],
            "synced_at": datetime.now(timezone.utc) - timedelta(hours=2),
            "registry_url": _REGISTRY,
            "settings_revision": 1,
            "generation": 1,
        }
    )
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
