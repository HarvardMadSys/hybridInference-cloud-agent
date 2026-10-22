"""Drain retired sandbox-manager pools without interrupting borrowed calls."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from cloud_agent.sandbox_manager_client import SandboxManagerError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx


class SandboxManagerPoolLease:
    """Count request-shaped borrowers; retirement prevents any new borrower."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.last_used = time.monotonic()
        self.active = 0
        self.retired = False
        self._drained = asyncio.Event()
        self._drained.set()

    @asynccontextmanager
    async def borrow(self) -> AsyncIterator[httpx.AsyncClient]:
        """Hold the connection pool until this response has been consumed."""
        if self.retired:
            raise SandboxManagerError(
                503, "The owning host connection was retired. Retry the request."
            )
        self.active += 1
        self.last_used = time.monotonic()
        self._drained.clear()
        try:
            yield self.client
        finally:
            self.active -= 1
            self.last_used = time.monotonic()
            if not self.active:
                self._drained.set()

    async def aclose(self) -> None:
        """Wait for current borrowers before releasing the sockets."""
        await self._drained.wait()
        await self.client.aclose()
