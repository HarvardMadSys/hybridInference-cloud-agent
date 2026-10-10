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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import number_setting, text_setting

if TYPE_CHECKING:
    import httpx

    from cloud_agent.ports.mcp_catalog import McpCatalogRepository

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


def _sort_key(entry: dict[str, Any]) -> tuple[int, bool, str, str]:
    return (
        _RECOMMENDED_RANK.get(entry["name"], len(_RECOMMENDED)),
        not bool(entry["configurations"]),
        entry["label"].casefold(),
        entry["name"],
    )


def _https(value: Any) -> str | None:
    """Allow plain public HTTPS metadata links, without fetching their targets."""
    if not isinstance(value, str) or re.search(r"[\s{}$]", value):
        return None
    try:
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
    except ValueError:
        return None


def _config(label: str, body: dict[str, Any], title: str) -> dict[str, str]:
    return {"label": title, "json": json.dumps({"mcpServers": {label: body}}, indent=2)}


def _header_value(header: dict[str, Any]) -> str | None:
    """Represent a single credential input the existing importer can preserve."""
    name = header.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
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
    if not url or transport not in {"streamable-http", "sse"}:
        return []
    body: dict[str, Any] = {"url": url, "type": "http" if transport == "streamable-http" else "sse"}
    headers = remote.get("headers") or []
    if not isinstance(headers, list) or any(not isinstance(h, dict) for h in headers):
        return []
    required = [h for h in headers if h.get("isRequired")]
    if len(required) > 1:
        return []
    if required:
        value = _header_value(required[0])
        if value is None:
            return []
        body["headers"] = {required[0]["name"]: value}
    options = [_config(label, body, f"{url} ({transport})")]
    if not required:
        for header in headers:
            value = _header_value(header)
            if value is not None:
                options.append(
                    _config(
                        label,
                        {**body, "headers": {header["name"]: value}},
                        f"{url} · {header['name']}",
                    )
                )
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
        or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", version)
    ):
        return None
    if kind == "npm" and re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*", name):
        if (
            package.get("registryBaseUrl", "https://registry.npmjs.org").rstrip("/")
            != "https://registry.npmjs.org"
        ):
            return None
        command, args = "npx", ["-y", f"{name}@{version}"]
    elif kind == "pypi" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        if package.get("registryBaseUrl", "https://pypi.org").rstrip("/") != "https://pypi.org":
            return None
        command, args = "uvx", [f"{name}=={version}"]
    else:
        return None
    if package.get("runtimeHint", command) != command:
        return None
    env: dict[str, str] = {}
    for variable in package.get("environmentVariables") or []:
        if not isinstance(variable, dict):
            return None
        name = variable.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return None
        value = variable.get("value", variable.get("default"))
        if isinstance(value, str) and not re.search(r"[{}$]", value):
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
        and icon.get("mimeType")
        in {None, "image/png", "image/jpeg", "image/jpg", "image/svg+xml", "image/webp"}
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


def catalog_entry(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one active latest record, preserving its publisher identifier."""
    official = record.get("_meta", {}).get(_OFFICIAL, {})
    if official.get("status") != "active" or official.get("isLatest") is not True:
        return None
    server = record.get("server", {})
    name = server.get("name")
    if not isinstance(name, str) or not name or len(name) > 250:
        return None
    fallback = name.rsplit("/", 1)[-1]
    if fallback.lower() in {"mcp", "mcp-server", "server"}:
        publisher = name.split("/", 1)[0].partition(".")[2]
        fallback = (publisher or name).removesuffix(".mcp")
        fallback = fallback[:1].upper() + fallback[1:]
    label = str(server.get("title") or fallback)[:200]
    options = []
    for remote in server.get("remotes") or []:
        if isinstance(remote, dict):
            options.extend(_remote_options(label, remote))
    for package in server.get("packages") or []:
        if isinstance(package, dict) and (option := _package_option(label, package)):
            options.append(option)
    return {
        "name": name,
        "label": label,
        "description": str(server.get("description") or "")[:2000],
        "version": str(server.get("version") or "")[:100],
        "icon_url": _icon_url(server.get("icons")),
        "website_url": _https(server.get("websiteUrl"))
        or _https((server.get("repository") or {}).get("url")),
        "configurations": list({option["json"]: option for option in options}.values()),
    }


async def fetch_catalog(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch bounded pages; any incomplete scan leaves the previous snapshot intact."""
    entries: dict[str, dict[str, Any]] = {}
    cursors: set[str] = set()
    params = {"version": "latest", "limit": "100"}
    registry_url = text_setting("mcp_registry_url", REGISTRY_URL).rstrip("/")
    if _https(registry_url) is None:
        raise ValueError("MCP Registry must be a public HTTPS URL")
    async with asyncio.timeout(number_setting("mcp_catalog_sync_timeout_s", 240)):
        for _ in range(1000):
            async with client.stream(
                "GET",
                f"{registry_url}/v0.1/servers",
                params=params,
                timeout=number_setting("mcp_catalog_request_timeout_s", 15),
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 5_000_000:
                        raise ValueError("Registry page exceeded size limit")
            page = json.loads(data)
            if not isinstance(page.get("servers"), list) or not isinstance(
                page.get("metadata"), dict
            ):
                raise ValueError("Invalid Registry page")
            for record in page["servers"]:
                try:
                    entry = catalog_entry(record)
                except (AttributeError, TypeError, ValueError):
                    # A bad publisher's entry must not hide the rest of the directory.
                    continue
                if entry is not None:
                    entries[entry["name"]] = entry
            cursor = page["metadata"].get("nextCursor")
            if not cursor:
                if not entries:
                    raise ValueError("Registry returned an empty directory")
                return sorted(entries.values(), key=_sort_key)
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Registry pagination did not advance")
            cursors.add(cursor)
            params["cursor"] = cursor
    raise ValueError("Registry exceeded page limit")


class McpCatalog:
    """Process-local read view of a durable, periodically refreshed public directory."""

    def __init__(self, repository: McpCatalogRepository, client: httpx.AsyncClient) -> None:
        self.repository = repository
        self.client = client
        self.snapshot: dict[str, Any] | None = None
        self._registry_url = text_setting("mcp_registry_url", REGISTRY_URL)

    async def refresh(self) -> None:
        """Reuse a fresh durable snapshot or atomically publish one complete fetch."""
        registry_url = text_setting("mcp_registry_url", REGISTRY_URL)
        async with asyncio.timeout(number_setting("mcp_catalog_storage_timeout_s", 10)):
            stored = await self.repository.load_mcp_catalog()
        if stored is not None:
            self.snapshot = stored
            if self._registry_url == registry_url and (
                datetime.now(timezone.utc) - stored["synced_at"]
            ).total_seconds() < number_setting("mcp_catalog_sync_seconds", SYNC_INTERVAL):
                return
        started = datetime.now(timezone.utc)
        entries = await fetch_catalog(self.client)
        async with asyncio.timeout(number_setting("mcp_catalog_storage_timeout_s", 10)):
            await self.repository.save_mcp_catalog(entries, started)
        self.snapshot = {"entries": entries, "synced_at": started}
        self._registry_url = registry_url

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
