"""Maintenance passes that repeat on a clock, each inside its own deadline."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: The workers using this have always logged as the application.
logger = get_logger("cloud_agent.app")


async def run_pass(
    operation: Callable[[], Awaitable[object]],
    *,
    deadline_seconds: float,
    description: str,
    event: str,
) -> None:
    """Run one pass inside its deadline; log a timeout or a failure, never raise it.

    ``description`` starts the message, as in "Environment maintenance sweep
    timed out", and ``event`` names it: ``<event>_timeout`` or ``<event>_failed``.
    """
    try:
        async with asyncio.timeout(deadline_seconds):
            await operation()
    except TimeoutError:
        logger.warning(
            "%s timed out",
            description,
            extra={"event": f"{event}_timeout", "deadline_seconds": deadline_seconds},
        )
    except Exception:
        logger.warning("%s failed", description, exc_info=True, extra={"event": f"{event}_failed"})


async def run_every(interval_seconds: float, *passes: Callable[[], Awaitable[None]]) -> None:
    """Wait ``interval_seconds``, run each pass in turn, and repeat until cancelled."""
    while True:
        await asyncio.sleep(interval_seconds)
        for run in passes:
            await run()
