"""Bounded framing for a streamed retained-base Git bundle.

The control plane must send the group identity beside the bundle, but putting
the bundle itself in JSON creates several payload-sized copies.  This tiny
wire format keeps only the bounded JSON metadata in memory: a magic/version,
a four-byte metadata length, the metadata, then the raw bundle through EOF.
"""

from __future__ import annotations

import json
import struct
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping


BASE_BUNDLE_CONTENT_TYPE: Final = "application/vnd.cloud-agent.base-bundle"
MAX_BASE_BUNDLE_METADATA_BYTES: Final = 16 * 1024 * 1024
BASE_BUNDLE_STREAM_CHUNK_BYTES: Final = 1024 * 1024
_MAGIC: Final = b"CAB1"
_HEADER: Final = struct.Struct("!4sI")
BASE_BUNDLE_FRAME_HEADER_BYTES: Final = _HEADER.size


def encode_base_bundle_metadata(metadata: Mapping[str, Any]) -> bytes:
    """Return the bounded frame prefix for one JSON metadata object."""
    encoded = json.dumps(metadata, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if not encoded or len(encoded) > MAX_BASE_BUNDLE_METADATA_BYTES:
        raise ValueError("base bundle metadata exceeds the transport limit")
    return _HEADER.pack(_MAGIC, len(encoded)) + encoded


def decode_base_bundle_header(header: bytes) -> int:
    """Validate a complete frame header and return its metadata length."""
    if len(header) != _HEADER.size:
        raise ValueError("base bundle frame header is incomplete")
    magic, length = _HEADER.unpack(header)
    if magic != _MAGIC:
        raise ValueError("base bundle frame version is unsupported")
    if not 0 < length <= MAX_BASE_BUNDLE_METADATA_BYTES:
        raise ValueError("base bundle metadata exceeds the transport limit")
    return length


__all__ = [
    "BASE_BUNDLE_CONTENT_TYPE",
    "BASE_BUNDLE_FRAME_HEADER_BYTES",
    "BASE_BUNDLE_STREAM_CHUNK_BYTES",
    "MAX_BASE_BUNDLE_METADATA_BYTES",
    "decode_base_bundle_header",
    "encode_base_bundle_metadata",
]
