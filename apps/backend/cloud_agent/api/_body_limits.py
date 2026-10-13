"""A request-body ceiling for the surfaces hosts call.

Hosts reach the control plane through their tunnel, not through the public
edge, so the edge's body limits never apply to them, and the application has
no body-size middleware of its own. Without one, the size of a host request is
whatever the host chooses to send, and every byte is buffered before a schema
gets to say no.

A route class is the narrowest place to add it. It bounds exactly the routers
that opt in, it reads the stream itself — so a chunked request with no
``Content-Length`` is cut off at the limit too — and it refuses before FastAPI
parses JSON, resolves dependencies or authenticates anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from fastapi import Request, Response

#: The largest body a host request may carry. The public edge allows 8 MiB to
#: its largest route, an upload; no host request comes near that. Events are
#: bounded to 256 KiB before a runner sends them, and a heartbeat describing
#: hundreds of retained sandboxes is a few hundred KiB.
MAX_HOST_REQUEST_BODY_BYTES = 8 * 1024 * 1024

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def payload_too_large(limit: int, what: str) -> JSONResponse:
    """The fixed 413 answer, which repeats nothing the caller sent."""
    return JSONResponse(
        status_code=413,
        content={
            "detail": {
                "error": {
                    "type": "payload_too_large",
                    "message": f"{what} is limited to {limit} bytes.",
                }
            }
        },
        headers={"Cache-Control": "no-store"},
    )


async def read_bounded_body(request: Request, limit: int) -> bytes | None:
    """Return the whole body, or ``None`` as soon as it passes ``limit`` bytes.

    Stops reading at the chunk that crosses the limit rather than draining the
    rest, so an oversized upload costs at most ``limit`` bytes of memory.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class HostBodyLimitRoute(APIRoute):
    """Refuse a request body larger than :attr:`max_body_bytes` before parsing."""

    max_body_bytes: ClassVar[int] = MAX_HOST_REQUEST_BODY_BYTES

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap routes that take a body; leave the others exactly as they were."""
        handler = super().get_route_handler()
        if not (self.methods or set()) & _BODY_METHODS:
            return handler
        limit = self.max_body_bytes

        async def bounded_handler(request: Request) -> Response:
            body = await read_bounded_body(request, limit)
            if body is None:
                return payload_too_large(limit, "A host request body")
            # Request.body() reads this same cache; hand FastAPI the bytes
            # accepted above rather than asking it to consume the stream again.
            request._body = body
            return await handler(request)

        return bounded_handler
