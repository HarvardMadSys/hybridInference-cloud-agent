"""Queue monitoring notices outages without paging for ordinary backpressure."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent import operational_health as health


@pytest.mark.parametrize(
    "snapshot,expected",
    [
        ({"queued": 0, "unavailable": 0, "running": 0}, ""),
        ({"queued": 2, "unavailable": 0, "running": 3}, ""),
        ({"queued": 2, "unavailable": 1, "running": 3}, "queued_without_host"),
        ({"queued": 2, "unavailable": 0, "running": 0}, "queue_not_starting"),
    ],
)
def test_queue_alarm(snapshot, expected):
    assert health.queue_alarm(snapshot) == expected


async def test_monitor_deduplicates_recovers_and_survives_delivery_failure(monkeypatch):
    failed = {"queued": 1, "unavailable": 1, "running": 0}
    healthy = {"queued": 0, "unavailable": 0, "running": 0}
    samples = [failed, failed, failed, failed, healthy, healthy]
    store = SimpleNamespace(queue_health=AsyncMock(side_effect=samples))
    send = AsyncMock(side_effect=[RuntimeError("receiver down"), None, None])
    monkeypatch.setattr(health, "deliver_alert", send)
    remaining = len(samples)

    async def tick(_):
        nonlocal remaining
        if remaining == 0:
            raise asyncio.CancelledError
        remaining -= 1

    monkeypatch.setattr(health, "asyncio", SimpleNamespace(sleep=tick, timeout=asyncio.timeout))
    with pytest.raises(asyncio.CancelledError):
        await health.monitor_queue(store)
    assert [call.args[0] for call in send.await_args_list] == [
        "queued_without_host",
        "queued_without_host",
        "queue_recovered",
    ]


async def test_delivery_has_no_user_data_and_no_redirects(monkeypatch):
    monkeypatch.setenv(health.ENV_ALERT_WEBHOOK, "https://alerts.example.invalid/secret")
    post = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None))

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 5, "follow_redirects": False}

        async def __aenter__(self):
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(health.httpx, "AsyncClient", Client)
    await health.deliver_alert("queued_without_host", {"queued": 2})
    assert set(post.call_args.kwargs["json"]) == {"text", "event", "deployment_sha", "queue"}
