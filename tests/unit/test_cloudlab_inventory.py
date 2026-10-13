"""CloudLab's public availability page is treated as untrusted input."""

from __future__ import annotations

import asyncio
import gzip
import html
import json
from datetime import UTC, datetime

import httpx
import pytest

from cloud_agent.application.cloudlab_inventory import (
    CLOUDLAB_HARDWARE_URL,
    CloudLabInventoryUnavailable,
    CloudLabNodeInventory,
)

UTAH = "urn:publicid:IDN+utah.cloudlab.us+authority+cm"
CLEMSON = "urn:publicid:IDN+clemson.cloudlab.us+authority+cm"
WISCONSIN = "urn:publicid:IDN+wisc.cloudlab.us+authority+cm"


class RawResponseStream(httpx.AsyncByteStream):
    """Model an unconsumed network response for raw-byte limit tests."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):
        yield self.body


def streamed_response(
    status: int,
    *,
    headers: dict[str, str],
    text: str | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    if text is not None:
        assert content is None
        content = text.encode()
    assert content is not None
    return httpx.Response(status, headers=headers, stream=RawResponseStream(content))


def inventory_html(*, utah=None, clemson=None, wisconsin=None) -> str:
    payload = {
        UTAH: {"name": "Cloudlab Utah", "typeinfo": utah or {}},
        CLEMSON: {"name": "Cloudlab Clemson", "typeinfo": clemson or {}},
        WISCONSIN: {"name": "Cloudlab Wisconsin", "typeinfo": wisconsin or {}},
    }
    encoded = html.escape(json.dumps(payload, separators=(",", ":")))
    return f"<html><script type='text/plain' id='amlist-json'>{encoded}</script></html>"


@pytest.mark.asyncio
async def test_preferred_nodes_keep_curated_order_regardless_of_availability_and_cache():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return streamed_response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=inventory_html(
                utah={
                    "c6620": {"count": 132, "free": 54},
                    "c6525-25g": {"count": 144, "free": 24},
                    "c6525-100g": {"count": 36, "free": 16},
                    "m510": {"count": 270, "free": 200},
                },
                clemson={
                    "r6615": {"count": 80, "free": 5},
                    "r650": {"count": 32, "free": 0},
                    "r6525": {"count": 32, "free": 32},
                },
                wisconsin={
                    "sm220u": {"count": 10, "free": 10},
                    "sm110p": {"count": 20, "free": 17},
                },
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = CloudLabNodeInventory(
            client=client,
            now=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        )
        first = await reader.preferred_nodes()
        second = await reader.preferred_nodes()

    assert second is first
    assert len(requests) == 1
    assert str(requests[0].url) == CLOUDLAB_HARDWARE_URL
    assert "authorization" not in requests[0].headers
    assert "x-api-token" not in requests[0].headers
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert [node["node_type"] for node in first["nodes"]] == [
        "r6615",
        "c6620",
        "r650",
        "r6525",
        "c6525-100g",
        "c6525-25g",
    ]
    assert first["nodes"][1] == {
        "node_type": "c6620",
        "site": "Utah",
        "available": 54,
        "total": 132,
        "introduced_year": 2025,
        "physical_cores": 28,
        "logical_threads": 56,
        "memory_gib": 128,
    }
    assert first["observed_at"] == "2026-10-11T12:00:00+00:00"
    assert first["filters"] == {
        "total_greater_than": 20,
        "minimum_introduced_year": 2021,
        "minimum_physical_cores": 16,
        "minimum_memory_gib": 128,
        "general_purpose_only": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total", "free", "expected_types"),
    [(20, 20, []), (21, 0, ["c6620"]), (21, 1, ["c6620"])],
)
async def test_preferred_nodes_filter_by_total_population(total, free, expected_types):
    response = streamed_response(
        200,
        headers={"Content-Type": "text/html"},
        text=inventory_html(utah={"c6620": {"count": total, "free": free}}),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await CloudLabNodeInventory(client=client).preferred_nodes()

    assert [node["node_type"] for node in result["nodes"]] == expected_types


@pytest.mark.asyncio
async def test_concurrent_reads_share_one_upstream_request():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return streamed_response(
            200,
            headers={"Content-Type": "text/html"},
            text=inventory_html(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = CloudLabNodeInventory(client=client)
        tasks = [asyncio.create_task(reader.preferred_nodes()) for _ in range(4)]
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks)

    assert calls == 1
    assert all(result["nodes"] == [] for result in results)


@pytest.mark.asyncio
async def test_expired_success_is_not_served_after_failure_and_failures_back_off():
    clock = [0.0]
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return streamed_response(
                200,
                headers={"Content-Type": "text/html"},
                text=inventory_html(utah={"c6620": {"count": 132, "free": 54}}),
            )
        return httpx.Response(503, text="unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = CloudLabNodeInventory(client=client, monotonic=lambda: clock[0])
        assert (await reader.preferred_nodes())["nodes"][0]["node_type"] == "c6620"
        clock[0] = 60.0
        with pytest.raises(CloudLabInventoryUnavailable):
            await reader.preferred_nodes()
        with pytest.raises(CloudLabInventoryUnavailable):
            await reader.preferred_nodes()

    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://example.invalid/redirect"}),
        httpx.Response(200, headers={"Content-Type": "application/json"}, text="{}"),
        httpx.Response(
            200,
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
            content=gzip.compress(b"not-a-trusted-identity-body"),
        ),
        streamed_response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html></html>",
        ),
        streamed_response(
            200,
            headers={"Content-Type": "text/html"},
            text=inventory_html(utah={"c6620": {"count": 10, "free": 11}}),
        ),
    ],
)
async def test_untrusted_or_redirected_inventory_fails_closed(response: httpx.Response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(CloudLabInventoryUnavailable):
            await CloudLabNodeInventory(client=client).preferred_nodes()


@pytest.mark.asyncio
async def test_oversized_inventory_fails_closed():
    response = streamed_response(
        200,
        headers={"Content-Type": "text/html"},
        content=b"x" * 1_000_001,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(CloudLabInventoryUnavailable):
            await CloudLabNodeInventory(client=client).preferred_nodes()
