"""Incrementally parse and spool current and rolling-upgrade base bundles."""

from __future__ import annotations

import asyncio
import base64
import binascii
import tempfile
from typing import TYPE_CHECKING, Generic, TypeVar

from fastapi import HTTPException, Request

from cloud_agent_shared.git.base_bundle_stream import (
    BASE_BUNDLE_CONTENT_TYPE,
    BASE_BUNDLE_FRAME_HEADER_BYTES,
    BASE_BUNDLE_STREAM_CHUNK_BYTES,
    LEGACY_BASE_BUNDLE_CONTENT_TYPE,
    MAX_BASE_BUNDLE_METADATA_BYTES,
    decode_base_bundle_header,
)
from cloud_agent_shared.git.source_archive import DEFAULT_MAX_BASE_BUNDLE_BYTES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import BinaryIO

_MetadataT = TypeVar("_MetadataT")
_LEGACY_BUNDLE_FIELD = b',"bundle_b64":"'
_LEGACY_TRAILER_MAX_BYTES = 1024
_JSON_WHITESPACE = b" \t\r\n"


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").partition(";")[0].strip().lower()


def _invalid_legacy_bundle() -> HTTPException:
    return HTTPException(status_code=422, detail="base bundle is not valid base64")


def _invalid_legacy_json() -> HTTPException:
    return HTTPException(status_code=422, detail="legacy base bundle JSON is invalid")


def _metadata_too_large() -> HTTPException:
    return HTTPException(status_code=422, detail="base bundle metadata exceeds the transport limit")


async def _read_framed_base_bundle(
    request: Request,
    validate_metadata: Callable[[bytearray], _MetadataT],
) -> tuple[_MetadataT, BinaryIO]:
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


class _LegacyBaseBundleReader(Generic[_MetadataT]):
    """Bounded state machine for the exact JSON shape emitted by old clients."""

    def __init__(
        self,
        bundle: BinaryIO,
        validate_metadata: Callable[[bytearray], _MetadataT],
    ) -> None:
        self.bundle = bundle
        self.validate_metadata = validate_metadata
        self.metadata = bytearray()
        self.encoded = bytearray()
        self.trailer = bytearray()
        self.body: _MetadataT | None = None
        self.bundle_size = 0
        self.saw_encoded = False
        self.saw_padding = False
        self.closed = False

    def _extend_metadata(self, value: bytes) -> None:
        if len(self.metadata) + len(value) > MAX_BASE_BUNDLE_METADATA_BYTES:
            raise _metadata_too_large()
        self.metadata.extend(value)

    def _metadata_segment(self, chunk: bytes) -> bytes | None:
        if self.body is not None:
            return chunk
        old_length = len(self.metadata)
        overlap = bytes(self.metadata[-(len(_LEGACY_BUNDLE_FIELD) - 1) :])
        boundary = overlap + chunk[: len(_LEGACY_BUNDLE_FIELD) - 1]
        boundary_position = boundary.find(_LEGACY_BUNDLE_FIELD)
        chunk_position = chunk.find(_LEGACY_BUNDLE_FIELD)
        crosses_boundary = (
            boundary_position >= 0
            and boundary_position < len(overlap)
            and boundary_position + len(_LEGACY_BUNDLE_FIELD) > len(overlap)
        )
        if crosses_boundary:
            marker_start = old_length - len(overlap) + boundary_position
            chunk_offset = boundary_position + len(_LEGACY_BUNDLE_FIELD) - len(overlap)
            del self.metadata[marker_start:]
            segment = chunk[chunk_offset:]
        elif chunk_position >= 0:
            self._extend_metadata(chunk[:chunk_position])
            segment = chunk[chunk_position + len(_LEGACY_BUNDLE_FIELD) :]
        else:
            self._extend_metadata(chunk)
            return None
        try:
            self.body = self.validate_metadata(self.metadata + b"}")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="base bundle metadata is invalid") from exc
        return segment

    async def _flush_encoded(self, *, final: bool) -> None:
        if final:
            if len(self.encoded) % 4:
                raise _invalid_legacy_bundle()
            length = len(self.encoded)
        else:
            length = len(self.encoded) - (len(self.encoded) % 4)
        if not length:
            return
        value = bytes(self.encoded[:length])
        del self.encoded[:length]
        if self.saw_padding:
            raise _invalid_legacy_bundle()
        try:
            decoded = await asyncio.to_thread(base64.b64decode, value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _invalid_legacy_bundle() from exc
        self.saw_padding = b"=" in value
        self.bundle_size += len(decoded)
        if self.bundle_size > DEFAULT_MAX_BASE_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail="base bundle exceeds the import limit")
        await asyncio.to_thread(self.bundle.write, decoded)

    async def _feed_encoded(self, value: bytes) -> None:
        self.saw_encoded = self.saw_encoded or bool(value)
        offset = 0
        while offset < len(value):
            take = min(BASE_BUNDLE_STREAM_CHUNK_BYTES - len(self.encoded), len(value) - offset)
            self.encoded.extend(value[offset : offset + take])
            offset += take
            if len(self.encoded) == BASE_BUNDLE_STREAM_CHUNK_BYTES:
                await self._flush_encoded(final=False)
        await self._flush_encoded(final=False)

    def _extend_trailer(self, value: bytes) -> None:
        if len(self.trailer) + len(value) > _LEGACY_TRAILER_MAX_BYTES:
            raise _invalid_legacy_json()
        self.trailer.extend(value)

    async def consume(self, chunk: bytes) -> None:
        segment = self._metadata_segment(chunk)
        if segment is None:
            return
        if self.closed:
            self._extend_trailer(segment)
            return
        quote = segment.find(b'"')
        if quote < 0:
            await self._feed_encoded(segment)
            return
        await self._feed_encoded(segment[:quote])
        await self._flush_encoded(final=True)
        self.closed = True
        self._extend_trailer(segment[quote + 1 :])

    async def finish(self) -> _MetadataT:
        if self.body is None:
            raise HTTPException(status_code=422, detail="base bundle metadata is incomplete")
        if not self.closed or bytes(self.trailer).strip(_JSON_WHITESPACE) != b"}":
            raise _invalid_legacy_json()
        if not self.saw_encoded or self.bundle_size == 0:
            raise HTTPException(status_code=422, detail="base bundle must be non-empty")
        await asyncio.to_thread(self.bundle.seek, 0)
        return self.body


async def _read_legacy_base_bundle(
    request: Request,
    validate_metadata: Callable[[bytearray], _MetadataT],
) -> tuple[_MetadataT, BinaryIO]:
    """Decode old JSON to disk with bounded metadata and base64 buffers."""
    bundle = tempfile.TemporaryFile(  # noqa: SIM115 — ownership passes to the endpoint.
        prefix="agent-base-import-legacy-"
    )
    reader = _LegacyBaseBundleReader(bundle, validate_metadata)
    try:
        async for chunk in request.stream():
            await reader.consume(chunk)
        return await reader.finish(), bundle
    except BaseException:
        bundle.close()
        raise


def bind_base_bundle_reader(
    validate_metadata: Callable[[bytearray], _MetadataT],
) -> Callable[[Request], Awaitable[tuple[_MetadataT, BinaryIO]]]:
    """Bind both rollout wire formats without coupling parsing to the API module."""

    async def read(request: Request) -> tuple[_MetadataT, BinaryIO]:
        content_type = _content_type(request)
        if content_type == BASE_BUNDLE_CONTENT_TYPE:
            return await _read_framed_base_bundle(request, validate_metadata)
        if content_type == LEGACY_BASE_BUNDLE_CONTENT_TYPE:
            return await _read_legacy_base_bundle(request, validate_metadata)
        raise HTTPException(status_code=415, detail="base bundle content type is unsupported")

    return read


__all__ = ["bind_base_bundle_reader"]
