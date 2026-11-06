"""How a Sandbox Manager request runs blocking work and ends its streams."""

from __future__ import annotations

import asyncio
import functools
import threading
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from concurrent.futures import Executor

    from starlette.types import Receive, Scope, Send


class _EndpointLifecycleCalls:
    """Track executor work through request cancellation and sandbox manager shutdown."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._accepting = True
        self._calls: set[asyncio.Future[Any]] = set()

    async def run(
        self,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Admit one executor call, retaining ownership until it really exits."""
        return await self.run_on(None, function, *args, **kwargs)

    async def run_on(
        self,
        executor: Executor | None,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Admit one call to ``executor``, or the loop's default one for ``None``."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            if not self._accepting:
                raise HTTPException(status_code=503, detail="sandbox manager is shutting down")
            future = loop.run_in_executor(executor, functools.partial(function, *args, **kwargs))
            self._calls.add(future)
            future.add_done_callback(self._call_finished)
        # Cancellation of an ASGI request must not detach an already-running
        # lifecycle call from sandbox manager shutdown ownership.
        return await asyncio.shield(future)

    def _call_finished(self, future: asyncio.Future[Any]) -> None:
        loop = future.get_loop()
        if not loop.is_closed():
            loop.create_task(self._discard(future))

    async def _discard(self, future: asyncio.Future[Any]) -> None:
        async with self._lock:
            self._calls.discard(future)

    async def seal_and_wait(self) -> None:
        """Reject new executor calls and wait for every admitted call to exit."""
        async with self._lock:
            self._accepting = False
            calls = tuple(self._calls)
        if calls:
            await asyncio.gather(
                *(asyncio.shield(call) for call in calls),
                return_exceptions=True,
            )
        async with self._lock:
            self._calls.difference_update(calls)


class _EndingStreamingResponse(StreamingResponse):
    """A streaming response that runs ``on_end`` however the response ends.

    Starlette stops pulling a body when its client goes away, a send fails or
    its task is cancelled, but never closes it. A generator left suspended
    runs its own cleanup only when a garbage collector reaches it, which a
    reference cycle can put off indefinitely.
    """

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        on_end: Callable[[], Awaitable[None]],
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._on_end = on_end

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._on_end()


class _CommandSlots:
    """Admit owner commands without waiting, per workspace and per sandbox manager."""

    def __init__(self, *, total: int, per_workspace: int) -> None:
        self._lock = threading.Lock()
        self._total = total
        self._per_workspace = per_workspace
        self._running: dict[str, int] = {}

    def take(self, workspace_id: str) -> Callable[[], None]:
        """Take a slot or refuse with 429; return what gives it back, once."""
        with self._lock:
            held = self._running.get(workspace_id, 0)
            if held >= self._per_workspace or sum(self._running.values()) >= self._total:
                raise HTTPException(
                    status_code=429,
                    detail="too many workspace commands are running; retry when one finishes",
                )
            self._running[workspace_id] = held + 1
        returned = False

        def give_back() -> None:
            nonlocal returned
            with self._lock:
                if returned:
                    return
                returned = True
                remaining = self._running[workspace_id] - 1
                if remaining:
                    self._running[workspace_id] = remaining
                else:
                    del self._running[workspace_id]

        return give_back
