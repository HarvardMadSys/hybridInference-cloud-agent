"""Read a conservative preferred-node view from CloudLab's public portal."""

from __future__ import annotations

import asyncio
import html
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable


CLOUDLAB_HARDWARE_URL: Final = "https://www.cloudlab.us/portal-hardware.php"
_MAX_RESPONSE_BYTES: Final = 1_000_000
_MIN_AVAILABLE_EXCLUSIVE: Final = 16
_MIN_INTRODUCED_YEAR: Final = 2021
_MIN_PHYSICAL_CORES: Final = 16
_MIN_MEMORY_GIB: Final = 128


class CloudLabInventoryUnavailable(RuntimeError):
    """The public inventory could not be read without guessing or stale data."""


@dataclass(frozen=True, slots=True)
class _PreferredNodeSpec:
    site: str
    site_urn: str
    introduced_year: int
    physical_cores: int
    logical_threads: int
    memory_gib: int


# Deliberately conservative. These are general-purpose, non-GPU CloudLab node
# families introduced in 2021 or later with at least 16 physical cores and
# 128 GiB RAM. Hardware facts come from CloudLab's public hardware catalog;
# availability is always taken from the live portal payload below.
_PREFERRED_NODE_SPECS: Final[dict[str, _PreferredNodeSpec]] = {
    "c6525-25g": _PreferredNodeSpec(
        "Utah", "urn:publicid:IDN+utah.cloudlab.us+authority+cm", 2021, 16, 32, 128
    ),
    "c6525-100g": _PreferredNodeSpec(
        "Utah", "urn:publicid:IDN+utah.cloudlab.us+authority+cm", 2021, 24, 48, 128
    ),
    "c6620": _PreferredNodeSpec(
        "Utah", "urn:publicid:IDN+utah.cloudlab.us+authority+cm", 2025, 28, 56, 128
    ),
    "r650": _PreferredNodeSpec(
        "Clemson", "urn:publicid:IDN+clemson.cloudlab.us+authority+cm", 2022, 72, 144, 256
    ),
    "r6525": _PreferredNodeSpec(
        "Clemson", "urn:publicid:IDN+clemson.cloudlab.us+authority+cm", 2022, 64, 128, 256
    ),
    "r6615": _PreferredNodeSpec(
        "Clemson", "urn:publicid:IDN+clemson.cloudlab.us+authority+cm", 2025, 32, 64, 192
    ),
    "sm220u": _PreferredNodeSpec(
        "Wisconsin", "urn:publicid:IDN+wisc.cloudlab.us+authority+cm", 2023, 32, 64, 256
    ),
    "sm110p": _PreferredNodeSpec(
        "Wisconsin", "urn:publicid:IDN+wisc.cloudlab.us+authority+cm", 2023, 16, 32, 128
    ),
}


class _InventoryPayloadParser(HTMLParser):
    """Extract only the entity-escaped JSON from the named inert script."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capturing = False
        self._parts: list[str] = []
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "amlist-json":
            self._capturing = True
            self.found = True

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._capturing:
            self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._capturing:
            self._parts.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False

    def payload(self) -> str:
        return html.unescape("".join(self._parts)).strip()


class CloudLabNodeInventory:
    """Fetch, validate and briefly cache CloudLab preferred-node availability."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        cache_seconds: float = 60.0,
        timeout_seconds: float = 8.0,
        failure_backoff_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._cache_seconds = cache_seconds
        self._timeout_seconds = timeout_seconds
        self._failure_backoff_seconds = failure_backoff_seconds
        self._monotonic = monotonic
        self._now = now
        self._cached_at: float | None = None
        self._cached: dict[str, Any] | None = None
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    async def preferred_nodes(self) -> dict[str, Any]:
        """Return only fresh, currently plentiful candidates in preference order."""
        cached = self._fresh_cache()
        if cached is not None:
            return cached
        if self._monotonic() < self._retry_after:
            raise CloudLabInventoryUnavailable
        async with self._lock:
            cached = self._fresh_cache()
            if cached is not None:
                return cached
            if self._monotonic() < self._retry_after:
                raise CloudLabInventoryUnavailable
            try:
                snapshot = await self._refresh()
            except CloudLabInventoryUnavailable:
                self._retry_after = self._monotonic() + self._failure_backoff_seconds
                raise
            self._cached = snapshot
            self._cached_at = self._monotonic()
            self._retry_after = 0.0
            return snapshot

    def _fresh_cache(self) -> dict[str, Any] | None:
        if self._cached_at is None or self._cached is None:
            return None
        if self._monotonic() - self._cached_at >= self._cache_seconds:
            return None
        return self._cached

    async def _refresh(self) -> dict[str, Any]:
        try:
            body = await self._read_bounded()
            inventory = self._parse_inventory(body)
            nodes = self._preferred_from(inventory)
        except (httpx.HTTPError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            raise CloudLabInventoryUnavailable from None
        return {
            "nodes": nodes,
            "observed_at": self._now().isoformat(),
            "source": "CloudLab public hardware inventory",
            "filters": {
                "availability_greater_than": _MIN_AVAILABLE_EXCLUSIVE,
                "minimum_introduced_year": _MIN_INTRODUCED_YEAR,
                "minimum_physical_cores": _MIN_PHYSICAL_CORES,
                "minimum_memory_gib": _MIN_MEMORY_GIB,
                "general_purpose_only": True,
            },
        }

    async def _read_bounded(self) -> str:
        chunks: list[bytes] = []
        size = 0
        async with self._client.stream(
            "GET",
            CLOUDLAB_HARDWARE_URL,
            headers={"Accept": "text/html", "Accept-Encoding": "identity"},
            follow_redirects=False,
            timeout=self._timeout_seconds,
        ) as response:
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", "").lower():
                raise ValueError("unexpected content type")
            content_encoding = response.headers.get("content-encoding", "").strip().lower()
            if content_encoding not in {"", "identity"}:
                raise ValueError("encoded inventory response")
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise ValueError("inventory response too large")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")

    @staticmethod
    def _parse_inventory(body: str) -> dict[str, Any]:
        parser = _InventoryPayloadParser()
        parser.feed(body)
        parser.close()
        if not parser.found or not parser.payload():
            raise ValueError("inventory payload missing")
        payload = json.loads(parser.payload())
        if not isinstance(payload, dict):
            raise ValueError("inventory root is not an object")
        return payload

    @staticmethod
    def _preferred_from(inventory: dict[str, Any]) -> list[dict[str, Any]]:
        expected_sites = {spec.site_urn for spec in _PREFERRED_NODE_SPECS.values()}
        for site_urn in expected_sites:
            site = inventory.get(site_urn)
            if not isinstance(site, dict) or not isinstance(site.get("typeinfo"), dict):
                raise ValueError("CloudLab site inventory missing")

        nodes: list[dict[str, Any]] = []
        for node_type, spec in _PREFERRED_NODE_SPECS.items():
            typeinfo = inventory[spec.site_urn]["typeinfo"]
            availability = typeinfo.get(node_type)
            if availability is None:
                continue
            if not isinstance(availability, dict):
                raise ValueError("node availability is not an object")
            total = availability.get("count")
            free = availability.get("free")
            if (
                type(total) is not int
                or type(free) is not int
                or total < 0
                or free < 0
                or free > total
            ):
                raise ValueError("invalid node availability")
            if free <= _MIN_AVAILABLE_EXCLUSIVE:
                continue
            nodes.append(
                {
                    "node_type": node_type,
                    "site": spec.site,
                    "available": free,
                    "total": total,
                    "introduced_year": spec.introduced_year,
                    "physical_cores": spec.physical_cores,
                    "logical_threads": spec.logical_threads,
                    "memory_gib": spec.memory_gib,
                }
            )
        return sorted(nodes, key=lambda node: (-node["available"], node["node_type"]))
