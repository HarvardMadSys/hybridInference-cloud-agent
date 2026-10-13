"""Read upstream HTTP bodies without allowing automatic decompression.

MCP, OAuth and Registry requests negotiate identity encoding. A server that
ignores that negotiation is refused before httpx can expand a compressed
chunk: a size check after expansion does not bound the decoder's allocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


IDENTITY_ENCODING_HEADERS = {"accept-encoding": "identity"}


def require_identity_encoding(response: httpx.Response) -> None:
    """Refuse compressed replies before a body reader invokes a decoder."""
    encodings = response.headers.get("content-encoding", "").split(",")
    if any(encoding.strip().lower() not in {"", "identity"} for encoding in encodings):
        raise httpx.DecodingError(
            "the server sent a compressed response despite the request for identity encoding"
        )


async def iter_response_bytes(response: httpx.Response) -> AsyncIterator[bytes]:
    """Yield identity-encoded body bytes without httpx's compression decoders."""
    require_identity_encoding(response)
    if response.is_stream_consumed:
        # Local transports may supply an already-buffered response. Production
        # callers use stream=True; this branch also preserves those test clients.
        content = response.content
        for start in range(0, len(content), 64 * 1024):
            yield content[start : start + 64 * 1024]
        return
    async for chunk in response.aiter_raw():
        yield chunk
