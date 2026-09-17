"""Detect a stranded queue during ordinary operation as well as deployment."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from cloud_agent.ports import HostPoolRepository

logger = logging.getLogger(__name__)
ENV_ALERT_WEBHOOK = "AGENT_ALERT_WEBHOOK_URL"


def queue_alarm(snapshot: dict[str, int]) -> str:
    """Separate missing execution capacity from normal occupied slots."""
    if snapshot["unavailable"]:
        return "queued_without_host"
    if snapshot["queued"] and not snapshot["running"]:
        return "queue_not_starting"
    return ""


async def deliver_alert(event: str, snapshot: dict[str, int]) -> None:
    """Deliver counts only to the operator-configured webhook, if present."""
    url = os.environ.get(ENV_ALERT_WEBHOOK, "").strip()
    if not url:
        return
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        response = await client.post(
            url,
            json={
                "text": f"Cloud Agent: {event}",
                "event": event,
                "deployment_sha": os.environ.get("AGENT_DEPLOYMENT_SHA", ""),
                "queue": snapshot,
            },
        )
        response.raise_for_status()


async def monitor_queue(store: HostPoolRepository, *, interval_seconds: float = 60) -> None:
    """Report persistent failures, recovery and a bounded reminder.

    Two consecutive observations suppress one transient database failure.
    Delivery failure retries on the next pass and cannot stop the monitor or
    any request/reaper. A destination is configuration, not a feature gate:
    structured failure/recovery events are always emitted.
    """
    previous = ""
    observed = ""
    last_sent = 0.0
    while True:
        await asyncio.sleep(interval_seconds)
        snapshot: dict[str, int] = {}
        try:
            async with asyncio.timeout(15):
                snapshot = await store.queue_health(
                    expected_deployment_sha=os.environ.get("AGENT_DEPLOYMENT_SHA", "")
                )
            event = queue_alarm(snapshot)
        except Exception:
            event = "queue_health_unavailable"
        if event != observed:
            observed = event
            continue
        if event == previous and (not event or time.monotonic() - last_sent < 1800):
            continue
        notification = event or "queue_recovered"
        logger.log(
            logging.ERROR if event else logging.INFO,
            notification,
            extra={"event": notification, "queue": snapshot},
        )
        try:
            await deliver_alert(notification, snapshot)
        except Exception as exc:
            # Webhook URLs often contain credentials; never log the exception
            # message or traceback from the HTTP client.
            logger.warning("operational alert delivery failed (%s)", type(exc).__name__)
            continue
        previous = event
        last_sent = time.monotonic()
