"""Incrementally parse and spool framed base-bundle HTTP requests."""

from __future__ import annotations

import asyncio
import tempfile
from typing import TYPE_CHECKING, TypeVar

from fastapi import HTTPException, Request

from cloud_agent_shared.git.base_bundle_stream import (
    BASE_BUNDLE_CONTENT_TYPE,
    BASE_BUNDLE_FRAME_HEADER_BYTES,
    decode_base_bundle_header,
)
from cloud_agent_shared.git.source_archive import DEFAULT_MAX_BASE_BUNDLE_BYTES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import BinaryIO

_MetadataT = TypeVar("_MetadataT")


def _require_content_type(request: Request) -> None:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != BASE_BUNDLE_CONTENT_TYPE:
        raise HTTPException(status_code=415, detail="base bundle content type is unsupported")


def bind_base_bundle_reader(
    validate_metadata: Callable[[bytearray], _MetadataT],
) -> Callable[[Request], Awaitable[tuple[_MetadataT, BinaryIO]]]:
    """Bind the host request model without coupling this parser to the API module."""

    async def read(request: Request) -> tuple[_MetadataT, BinaryIO]:
        _require_content_type(request)

        bundle = tempfile.TemporaryFile(  # noqa: SIM115 — ownership passes to the endpoint.
            prefix="agent-base-import-"
        )
        header = bytearray()
        metadata = bytearray()
        metadata_length: int | None = None
        body: _MetadataT | None = None
        bundle_size = 0
        try:
            async for chunk in request.stream():
                offset = 0
                if metadata_length is None:
                    take = min(BASE_BUNDLE_FRAME_HEADER_BYTES - len(header), len(chunk))
                    header.extend(chunk[:take])
                    offset += take
                    if len(header) == BASE_BUNDLE_FRAME_HEADER_BYTES:
                        try:
                            metadata_length = decode_base_bundle_header(bytes(header))
                        except ValueError as exc:
                            raise HTTPException(status_code=422, detail=str(exc)) from exc

                if metadata_length is not None and body is None:
                    take = min(metadata_length - len(metadata), len(chunk) - offset)
                    metadata.extend(chunk[offset : offset + take])
                    offset += take
                    if len(metadata) == metadata_length:
                        try:
                            body = validate_metadata(metadata)
                        except ValueError as exc:
                            raise HTTPException(
                                status_code=422, detail="base bundle metadata is invalid"
                            ) from exc

                if offset < len(chunk):
                    bundle_size += len(chunk) - offset
                    if bundle_size > DEFAULT_MAX_BASE_BUNDLE_BYTES:
                        raise HTTPException(
                            status_code=413, detail="base bundle exceeds the import limit"
                        )
                    await asyncio.to_thread(bundle.write, memoryview(chunk)[offset:])

            if body is None:
                raise HTTPException(status_code=422, detail="base bundle metadata is incomplete")
            if bundle_size == 0:
                raise HTTPException(status_code=422, detail="base bundle must be non-empty")
            await asyncio.to_thread(bundle.seek, 0)
            return body, bundle
        except BaseException:
            bundle.close()
            raise

    return read


__all__ = ["bind_base_bundle_reader"]
