"""Synchronize public server metadata without putting the Registry on a request path.

Only a complete, successful sync replaces the durable snapshot. Syncing never
contacts a listed server, installs a package, or changes a user's connections.
The ordinary import/authorization path still validates every chosen target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.http_body import IDENTITY_ENCODING_HEADERS, iter_response_bytes
from cloud_agent.settings import number_setting

if TYPE_CHECKING:
    import httpx

    from cloud_agent.ports.mcp_catalog import McpCatalogRepository, McpCatalogSync

logger = get_logger(__name__)
REGISTRY_URL = "https://registry.modelcontextprotocol.io"
SYNC_INTERVAL = 3600
RETRY_INTERVAL = 300
_OFFICIAL = "io.modelcontextprotocol.registry/official"
# An editorial shortlist for common coding workflows, not a download ranking.
# Match publisher identifiers, never display names. Metadata still comes from
# the live feed and every other active entry remains available in All servers.
_RECOMMENDED = (
    "io.github.github/github-mcp-server",
    "io.github.upstash/context7",
    "io.github.microsoft/playwright-mcp",
    "app.linear/linear",
    "com.notion/mcp",
    "com.figma.mcp/mcp",
    "com.supabase/mcp",
    "io.github.getsentry/sentry-mcp",
)
_RECOMMENDED_RANK = {name: index for index, name in enumerate(_RECOMMENDED)}
_MAX_CHECKPOINT_ENTRIES = 100_000
_MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
_MAX_CURSOR_BYTES = 16 * 1024
_ENTRY_MEMORY_OVERHEAD = 256
_CURSOR_MEMORY_OVERHEAD = 64
_MAX_METADATA_URL_BYTES = 8 * 1024
_MAX_HEADER_NAME_BYTES = 256
_MAX_HEADER_VALUE_BYTES = 8 * 1024
_MAX_REMOTE_HEADERS = 64
_MAX_PACKAGE_ENVIRONMENT_VARIABLES = 128
_MAX_SERVER_REMOTES = 256
_MAX_SERVER_PACKAGES = 256
_MAX_CONFIGURATIONS_PER_ENTRY = 128
_MAX_CONFIGURATION_BYTES = 64 * 1024
_MAX_ENTRY_CONFIGURATION_BYTES = 1024 * 1024


@dataclass
class _CatalogFetchCheckpoint:
    """Validated pages from one scan, never exposed as the live catalogue."""

    registry_url: str
    sync: McpCatalogSync | None = None
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    entry_sizes: dict[str, int] = field(default_factory=dict)
    cursors: set[str] = field(default_factory=set)
    entry_bytes: int = 0
    cursor_bytes: int = 0
    next_cursor: str | None = None
    pages: int = 0
    complete: bool = False

    def restart(self) -> None:
        """Discard a cursor the Registry has proved cannot be continued."""
        self.entries.clear()
        self.entry_sizes.clear()
        self.cursors.clear()
        self.entry_bytes = 0
        self.cursor_bytes = 0
        self.next_cursor = None
        self.pages = 0
        self.complete = False


def _sort_key(entry: dict[str, Any]) -> tuple[int, bool, str, str]:
    return (
        _RECOMMENDED_RANK.get(entry["name"], len(_RECOMMENDED)),
        not bool(entry["configurations"]),
        entry["label"].casefold(),
        entry["name"],
    )


def _utf8_within(value: str, limit: int) -> bool:
    try:
        return len(value.encode()) <= limit
    except UnicodeError:
        return False


def _https(value: Any) -> str | None:
    """Allow plain public HTTPS metadata links, without fetching their targets."""
    if not isinstance(value, str) or re.search(r"[\s{}$]", value):
        return None
    try:
        if not _utf8_within(value, _MAX_METADATA_URL_BYTES):
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port == 0
        ):
            return None
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value
    except (UnicodeError, ValueError):
        return None


def _config(label: str, body: dict[str, Any], title: str) -> dict[str, str] | None:
    rendered = json.dumps({"mcpServers": {label: body}}, indent=2)
    if len(rendered.encode()) > _MAX_CONFIGURATION_BYTES:
        return None
    return {"label": title, "json": rendered}


def _header_value(header: dict[str, Any]) -> str | None:
    """Represent a single credential input the existing importer can preserve."""
    name = header.get("name")
    if (
        not isinstance(name, str)
        or not _utf8_within(name, _MAX_HEADER_NAME_BYTES)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name)
    ):
        return None
    # Ordinary HTTP headers are not persisted by the current importer. Never
    # quietly drop a header and present the resulting config as ready to use.
    if not re.search(
        r"authorization|authentication|\bauth\b|token|secret|key|password", name, re.I
    ):
        return None
    value = header.get("value")
    if not value:
        return "${env:MCP_TOKEN}"
    if not isinstance(value, str):
        return None
    if not _utf8_within(value, _MAX_HEADER_VALUE_BYTES):
        return None
    # The importer supports one whole value, optionally prefixed by an auth
    # scheme; interpolated values with suffixes or several inputs need JSON.
    match = re.fullmatch(r"(?:(Bearer|Basic|Token) )?\{[\w.-]+\}", value, re.I)
    if match:
        return (f"{match[1]} " if match[1] else "") + "${env:MCP_TOKEN}"
    if "{" in value or "$" in value:
        return None
    return value


def _remote_options(label: str, remote: dict[str, Any]) -> list[dict[str, str]]:
    url = _https(remote.get("url"))
    transport = remote.get("type")
    if not url or not isinstance(transport, str) or transport not in {"streamable-http", "sse"}:
        return []
    body: dict[str, Any] = {"url": url, "type": "http" if transport == "streamable-http" else "sse"}
    headers = remote.get("headers") or []
    if (
        not isinstance(headers, list)
        or len(headers) > _MAX_REMOTE_HEADERS
        or any(not isinstance(h, dict) for h in headers)
    ):
        return []
    required = [h for h in headers if h.get("isRequired")]
    if len(required) > 1:
        return []
    if required:
        value = _header_value(required[0])
        if value is None:
            return []
        body["headers"] = {required[0]["name"]: value}
    first = _config(label, body, f"{url} ({transport})")
    options = [first] if first is not None else []
    if not required:
        for header in headers:
            value = _header_value(header)
            if value is not None:
                option = _config(
                    label,
                    {**body, "headers": {header["name"]: value}},
                    f"{url} · {header['name']}",
                )
                if option is not None:
                    options.append(option)
    return options


def _package_option(label: str, package: dict[str, Any]) -> dict[str, str] | None:
    """Prefill simple pinned npm/PyPI stdio installs; leave complex recipes explicit."""
    if (
        package.get("transport") != {"type": "stdio"}
        or package.get("runtimeArguments")
        or package.get("packageArguments")
    ):
        return None
    kind, name, version = (package.get(key) for key in ("registryType", "identifier", "version"))
    if (
        not isinstance(name, str)
        or not isinstance(version, str)
        or not _utf8_within(name, 512)
        or not _utf8_within(version, 256)
        or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", version)
    ):
        return None
    registry_base_url = package.get(
        "registryBaseUrl", "https://registry.npmjs.org" if kind == "npm" else "https://pypi.org"
    )
    if not isinstance(registry_base_url, str):
        return None
    if kind == "npm" and re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*", name):
        if registry_base_url.rstrip("/") != "https://registry.npmjs.org":
            return None
        command, args = "npx", ["-y", f"{name}@{version}"]
    elif kind == "pypi" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        if registry_base_url.rstrip("/") != "https://pypi.org":
            return None
        command, args = "uvx", [f"{name}=={version}"]
    else:
        return None
    if package.get("runtimeHint", command) != command:
        return None
    variables = package.get("environmentVariables") or []
    if not isinstance(variables, list) or len(variables) > _MAX_PACKAGE_ENVIRONMENT_VARIABLES:
        return None
    env: dict[str, str] = {}
    for variable in variables:
        if not isinstance(variable, dict):
            return None
        name = variable.get("name")
        if (
            not isinstance(name, str)
            or not _utf8_within(name, _MAX_HEADER_NAME_BYTES)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        ):
            return None
        value = variable.get("value", variable.get("default"))
        if (
            isinstance(value, str)
            and _utf8_within(value, _MAX_HEADER_VALUE_BYTES)
            and not re.search(r"[{}$]", value)
        ):
            env[name] = value
        elif variable.get("isRequired"):
            if not variable.get("isSecret"):
                return None
            env[name] = "${env:" + name + "}"
    body: dict[str, Any] = {"command": command, "args": args}
    if env:
        body["env"] = env
    return _config(label, body, f"{kind} · {package['identifier']}@{version}")


def _icon_url(icons: Any) -> str | None:
    """Choose a public image suitable for the directory's light icon tile."""
    if not isinstance(icons, list):
        return None
    candidates = [
        icon
        for icon in icons
        if isinstance(icon, dict)
        and _https(icon.get("src"))
        and (
            icon.get("mimeType") is None
            or (
                isinstance(icon.get("mimeType"), str)
                and icon.get("mimeType")
                in {"image/png", "image/jpeg", "image/jpg", "image/svg+xml", "image/webp"}
            )
        )
    ]
    candidates = [icon for icon in candidates if icon.get("theme") != "dark"] or candidates
    for icon in candidates:
        sizes = icon.get("sizes")
        if not isinstance(sizes, list) or not sizes or "any" in sizes:
            return str(icon["src"])
        if any(
            isinstance(size, str)
            and (match := re.fullmatch(r"(\d{1,4})x(\d{1,4})", size))
            and min(int(match[1]), int(match[2])) >= 48
            for size in sizes
        ):
            return str(icon["src"])
    return str(candidates[0]["src"]) if candidates else None


def catalog_entry(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize one active latest record, preserving its publisher identifier."""
    metadata = record.get("_meta")
    if not isinstance(metadata, Mapping):
        return None
    official = metadata.get(_OFFICIAL)
    if not isinstance(official, Mapping):
        return None
    if official.get("status") != "active" or official.get("isLatest") is not True:
        return None
    server = record.get("server")
    if not isinstance(server, Mapping):
        return None
    name = server.get("name")
    if not isinstance(name, str) or not name or len(name) > 250:
        return None
    fallback = name.rsplit("/", 1)[-1]
    if fallback.lower() in {"mcp", "mcp-server", "server"}:
        publisher = name.split("/", 1)[0].partition(".")[2]
        fallback = (publisher or name).removesuffix(".mcp")
        fallback = fallback[:1].upper() + fallback[1:]
    label = str(server.get("title") or fallback)[:200]
    options: dict[str, dict[str, str]] = {}
    option_bytes = 0

    def add_options(candidates: list[dict[str, str]]) -> bool:
        nonlocal option_bytes
        for option in candidates:
            rendered = option["json"]
            if rendered in options:
                continue
            size = len(rendered.encode())
            if (
                len(options) >= _MAX_CONFIGURATIONS_PER_ENTRY
                or option_bytes + size > _MAX_ENTRY_CONFIGURATION_BYTES
            ):
                return False
            options[rendered] = option
            option_bytes += size
        return True

    remotes = server.get("remotes") or []
    if isinstance(remotes, list):
        for remote in remotes[:_MAX_SERVER_REMOTES]:
            if isinstance(remote, dict) and not add_options(_remote_options(label, remote)):
                break
    packages = server.get("packages") or []
    if isinstance(packages, list) and len(options) < _MAX_CONFIGURATIONS_PER_ENTRY:
        for package in packages[:_MAX_SERVER_PACKAGES]:
            if (
                isinstance(package, dict)
                and (option := _package_option(label, package))
                and not add_options([option])
            ):
                break
    repository = server.get("repository")
    repository_url = repository.get("url") if isinstance(repository, Mapping) else None
    return {
        "name": name,
        "label": label,
        "description": str(server.get("description") or "")[:2000],
        "version": str(server.get("version") or "")[:100],
        "icon_url": _icon_url(server.get("icons")),
        "website_url": _https(server.get("websiteUrl")) or _https(repository_url),
        "configurations": list(options.values()),
    }


def _registry_url(value: str) -> str:
    """Return the validated base URL shared by a scan and its continuations."""
    registry_url = value.rstrip("/")
    if _https(registry_url) is None:
        raise ValueError("MCP Registry must be a public HTTPS URL")
    return registry_url


def _entry_size(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, separators=(",", ":")).encode()) + _ENTRY_MEMORY_OVERHEAD


def _checkpoint_page(
    state: _CatalogFetchCheckpoint,
    page_entries: dict[str, dict[str, Any]],
    page_sizes: dict[str, int],
    cursor: str | None,
) -> None:
    """Commit one validated page without allowing retained state to grow unbounded."""
    entry_count = len(set(state.entries).union(page_entries))
    entry_bytes = state.entry_bytes + sum(
        size - state.entry_sizes.get(name, 0) for name, size in page_sizes.items()
    )
    cursor_bytes = state.cursor_bytes
    if cursor:
        if not _utf8_within(cursor, _MAX_CURSOR_BYTES):
            state.restart()
            raise ValueError("Registry cursor exceeded size limit")
        encoded_cursor_bytes = len(cursor.encode())
        cursor_bytes += encoded_cursor_bytes + _CURSOR_MEMORY_OVERHEAD
    if entry_count > _MAX_CHECKPOINT_ENTRIES or entry_bytes + cursor_bytes > _MAX_CHECKPOINT_BYTES:
        state.restart()
        raise ValueError("Registry checkpoint exceeded size limit")
    state.entries.update(page_entries)
    state.entry_sizes.update(page_sizes)
    state.entry_bytes = entry_bytes
    state.cursor_bytes = cursor_bytes
    if cursor:
        state.cursors.add(cursor)


async def fetch_catalog(
    client: httpx.AsyncClient,
    *,
    checkpoint: _CatalogFetchCheckpoint | None = None,
    registry_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch bounded pages, resuming only pages validated in this process.

    A checkpoint is an unpublished work buffer. Callers may retain it after a
    transient request or total-scan timeout, but only a complete return value is
    eligible for the durable atomic swap.
    """
    registry_url = _registry_url(registry_url or REGISTRY_URL)
    state = checkpoint or _CatalogFetchCheckpoint(registry_url)
    if state.registry_url != registry_url:
        raise ValueError("MCP Registry changed during directory sync")
    if state.complete:
        return sorted(state.entries.values(), key=_sort_key)
    async with asyncio.timeout(number_setting("mcp_catalog_sync_timeout_s", 240)):
        while state.pages < 1000:
            params = {"version": "latest", "limit": "100"}
            if state.next_cursor is not None:
                params["cursor"] = state.next_cursor
            async with client.stream(
                "GET",
                f"{registry_url}/v0.1/servers",
                params=params,
                timeout=number_setting("mcp_catalog_request_timeout_s", 15),
                follow_redirects=False,
                headers=IDENTITY_ENCODING_HEADERS,
            ) as response:
                # A cursor rejected by the Registry cannot make progress on a
                # later retry. Keep rate limits and request timeouts resumable,
                # but make other client errors restart at page one next time.
                if (
                    400 <= response.status_code < 500
                    and response.status_code not in {408, 429}
                    and state.next_cursor is not None
                ):
                    state.restart()
                response.raise_for_status()
                data = bytearray()
                async for chunk in iter_response_bytes(response):
                    data.extend(chunk)
                    if len(data) > 5_000_000:
                        raise ValueError("Registry page exceeded size limit")
            page = json.loads(data)
            # A malformed or truncated response says nothing about whether the
            # opaque cursor is still valid. Keep the checkpoint and retry this
            # exact page; restarting would only replay every earlier page before
            # reaching the same upstream failure again.
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("servers"), list)
                or not isinstance(page.get("metadata"), dict)
            ):
                raise ValueError("Invalid Registry page")
            page_entries: dict[str, dict[str, Any]] = {}
            page_sizes: dict[str, int] = {}
            page_bytes = 0
            for record in page["servers"]:
                if not isinstance(record, Mapping):
                    continue
                entry = catalog_entry(record)
                if entry is not None:
                    name = entry["name"]
                    size = _entry_size(entry)
                    next_page_bytes = page_bytes + size - page_sizes.get(name, 0)
                    next_page_count = len(page_entries) + (name not in page_entries)
                    if (
                        len(state.entries) + next_page_count > _MAX_CHECKPOINT_ENTRIES
                        or state.entry_bytes + state.cursor_bytes + next_page_bytes
                        > _MAX_CHECKPOINT_BYTES
                    ):
                        state.restart()
                        raise ValueError("Registry checkpoint exceeded size limit")
                    page_entries[name] = entry
                    page_sizes[name] = size
                    page_bytes = next_page_bytes
            cursor = page["metadata"].get("nextCursor")
            if cursor is not None and not isinstance(cursor, str):
                state.restart()
                raise ValueError("Invalid Registry cursor")
            if cursor and cursor in state.cursors:
                state.restart()
                raise ValueError("Registry pagination did not advance")
            _checkpoint_page(state, page_entries, page_sizes, cursor)
            state.pages += 1
            if not cursor:
                if not state.entries:
                    state.restart()
                    raise ValueError("Registry returned an empty directory")
                state.complete = True
                return sorted(state.entries.values(), key=_sort_key)
            state.next_cursor = cursor
    state.restart()
    raise ValueError("Registry exceeded page limit")


class McpCatalog:
    """Process-local read view of a durable, periodically refreshed public directory."""

    def __init__(self, repository: McpCatalogRepository, client: httpx.AsyncClient) -> None:
        self.repository = repository
        self.client = client
        self.snapshot: dict[str, Any] | None = None
        self._fetch_checkpoint: _CatalogFetchCheckpoint | None = None

    async def refresh(self) -> None:
        """Reuse a fresh durable snapshot or atomically publish one complete fetch."""
        async with asyncio.timeout(number_setting("mcp_catalog_storage_timeout_s", 10)):
            stored = await self.repository.load_mcp_catalog()
            current_sync = await self.repository.begin_mcp_catalog_sync()
        if stored is not None:
            self.snapshot = stored
        registry_url = _registry_url(current_sync.registry_url)
        if registry_url != current_sync.registry_url:
            current_sync = replace(current_sync, registry_url=registry_url)
        if (
            stored is not None
            and stored.get("registry_url") == registry_url
            and (datetime.now(timezone.utc) - stored["synced_at"]).total_seconds()
            < number_setting("mcp_catalog_sync_seconds", SYNC_INTERVAL)
        ):
            self._fetch_checkpoint = None
            return
        # A bad outbound setting must not hide the durable snapshot loaded
        # above. Validation belongs here, immediately before a network scan.
        registry_url = _registry_url(registry_url)
        checkpoint = self._fetch_checkpoint
        if (
            checkpoint is None
            or checkpoint.registry_url != registry_url
            or checkpoint.sync is None
            or checkpoint.sync.settings_revision != current_sync.settings_revision
        ):
            checkpoint = _CatalogFetchCheckpoint(registry_url, sync=current_sync)
            self._fetch_checkpoint = checkpoint
        entries = await fetch_catalog(self.client, checkpoint=checkpoint, registry_url=registry_url)
        if checkpoint.sync is None:  # pragma: no cover - constructed above
            raise RuntimeError("MCP catalog checkpoint has no database fence")
        async with asyncio.timeout(number_setting("mcp_catalog_storage_timeout_s", 10)):
            save = await self.repository.save_mcp_catalog(entries, checkpoint.sync)
            winner = None if save.saved_at is not None else await self.repository.load_mcp_catalog()
        if save.saved_at is not None:
            self.snapshot = {
                "entries": entries,
                "synced_at": save.saved_at,
                "registry_url": checkpoint.sync.registry_url,
                "settings_revision": checkpoint.sync.settings_revision,
                "generation": checkpoint.sync.generation,
            }
        elif (
            winner is not None
            and int(winner.get("generation", -1)) > checkpoint.sync.generation
            and winner.get("registry_url") == save.registry_url
            and winner.get("settings_revision") == save.settings_revision
        ):
            # Another process completed a newer scan first. The conditional
            # database write fenced this result out. Adopt it only when it was
            # published under the authority observed by that same fenced write.
            self.snapshot = winner
        else:
            # The authoritative settings changed while this scan was in
            # flight. Retain the durable snapshot but retry with a fresh
            # settings revision instead of treating the stale source as fresh.
            raise RuntimeError("MCP catalog source fence changed during sync")
        self._fetch_checkpoint = None

    async def run(self) -> None:
        """Keep external failures out of startup, readiness and the connection API."""
        while True:
            try:
                await self.refresh()
                age = (
                    (datetime.now(timezone.utc) - self.snapshot["synced_at"]).total_seconds()
                    if self.snapshot
                    else 0
                )
                delay = max(1, number_setting("mcp_catalog_sync_seconds", SYNC_INTERVAL) - age)
            except Exception:
                logger.warning("MCP directory sync failed; keeping cached servers", exc_info=True)
                delay = number_setting("mcp_catalog_retry_seconds", RETRY_INTERVAL)
            await asyncio.sleep(delay)

    def search(
        self, query: str = "", offset: int = 0, limit: int = 24, *, recommended: bool = False
    ) -> dict[str, Any]:
        """Search only local metadata; requests never depend on Registry availability."""
        snapshot = self.snapshot
        terms = query.casefold().split()
        entries = snapshot["entries"] if snapshot else []
        matches = [
            entry
            for entry in entries
            if (not recommended or (entry["name"] in _RECOMMENDED_RANK and entry["configurations"]))
            and all(
                term in f"{entry['name']} {entry['label']} {entry['description']}".casefold()
                for term in terms
            )
        ]
        # Apply the current editorial order even to snapshots cached by an
        # earlier release, before slicing so every page has a stable order.
        matches.sort(key=_sort_key)
        synced_at = snapshot["synced_at"] if snapshot else None
        return {
            "entries": matches[offset : offset + limit],
            "total": len(matches),
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "synced_at": synced_at.isoformat() if synced_at else None,
            "stale": not synced_at
            or (datetime.now(timezone.utc) - synced_at).total_seconds()
            > 2 * number_setting("mcp_catalog_sync_seconds", SYNC_INTERVAL),
        }
