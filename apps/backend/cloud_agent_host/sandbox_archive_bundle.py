"""Build a bounded, deterministic archive of one chat-sandbox lifecycle point.

The inputs are Docker archive streams.  They stay nested tar payloads:
this module neither extracts them nor treats their member names as paths on the
host.  That keeps an inner symlink or traversal name inert at this boundary.
Any future restore path must validate an inner archive before extracting it.

The fetch callback is deliberately injected. Lifecycle code can prove either
the running pre-prompt barrier or the stopped final barrier before supplying
Docker's stream, without coupling this primitive to Docker or the manager.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import tarfile
import tempfile
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol, cast

from cloud_agent_shared.errors import SandboxError

ArchivePhase = Literal["initial", "final"]

DEFAULT_MAX_ARCHIVE_MEMBER_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
_FORMAT_NAME = "cloud-agent-stopped-sandbox"
_FORMAT_VERSION = 1


@dataclass(frozen=True)
class _PathArchive:
    container_path: str
    archive_path: str
    optional: bool = False


@dataclass(frozen=True)
class _SpooledArchive:
    spec: _PathArchive
    stream: BinaryIO
    size_bytes: int
    sha256: str


class ArchiveFetcher(Protocol):
    """Callback that streams one container path into a caller-owned sink."""

    def __call__(
        self,
        path: str,
        destination: BinaryIO,
        *,
        max_bytes: int,
    ) -> int:
        """Write one bounded Docker archive stream and return bytes written."""
        ...


# Fixed input and output names are part of the security boundary.  A caller
# cannot turn a container path into an outer-tar traversal name.
_PATH_ARCHIVES = (
    _PathArchive("/workspace", "paths/workspace.tar"),
    _PathArchive("/agent-state", "paths/agent-state.tar"),
    _PathArchive(
        "/home/agent/.freeinference-platform",
        "paths/freeinference-platform.tar",
        optional=True,
    ),
)


def _validate_limit(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


class _BoundedDigestWriter:
    """A write-only sink that enforces both archive limits as bytes arrive."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        container_path: str,
        prior_total: int,
        max_member_bytes: int,
        max_total_bytes: int,
    ) -> None:
        self._stream = stream
        self._container_path = container_path
        self._prior_total = prior_total
        self._max_member_bytes = max_member_bytes
        self._max_total_bytes = max_total_bytes
        self._digest = hashlib.sha256()
        self.size_bytes = 0

    def write(self, chunk: bytes | bytearray | memoryview) -> int:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise SandboxError("sandbox archive fetch callback wrote non-bytes data")
        projected_size = self.size_bytes + len(chunk)
        if projected_size > self._max_member_bytes:
            raise SandboxError(
                f"sandbox path archive {self._container_path!r} exceeds the "
                f"{self._max_member_bytes}-byte member limit"
            )
        if self._prior_total + projected_size > self._max_total_bytes:
            raise SandboxError(
                f"sandbox path archives exceed the {self._max_total_bytes}-byte total limit"
            )
        written = self._stream.write(chunk)
        if written != len(chunk):
            raise OSError("short write while spooling a sandbox path archive")
        self._digest.update(chunk)
        self.size_bytes = projected_size
        return written

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


def _fetch_archive(
    fetch_archive: ArchiveFetcher,
    spec: _PathArchive,
    stream: BinaryIO,
    *,
    prior_total: int,
    max_member_bytes: int,
    max_total_bytes: int,
) -> tuple[int, str] | None:
    writer = _BoundedDigestWriter(
        stream,
        container_path=spec.container_path,
        prior_total=prior_total,
        max_member_bytes=max_member_bytes,
        max_total_bytes=max_total_bytes,
    )
    try:
        reported_size = fetch_archive(
            spec.container_path,
            cast("BinaryIO", writer),
            max_bytes=max_member_bytes,
        )
    except FileNotFoundError as exc:
        if spec.optional and writer.size_bytes == 0:
            return None
        if spec.optional:
            raise SandboxError(
                f"optional sandbox path archive {spec.container_path!r} disappeared after "
                "streaming bytes"
            ) from exc
        raise SandboxError(
            f"required sandbox path archive {spec.container_path!r} is missing"
        ) from exc
    except SandboxError:
        raise
    except Exception as exc:
        raise SandboxError(
            f"sandbox path archive {spec.container_path!r} could not be fetched: {exc}"
        ) from exc
    if isinstance(reported_size, bool) or not isinstance(reported_size, int):
        raise SandboxError(
            f"sandbox path archive {spec.container_path!r} reported a non-integer size"
        )
    if reported_size != writer.size_bytes:
        raise SandboxError(
            f"sandbox path archive {spec.container_path!r} reported {reported_size} bytes "
            f"after writing {writer.size_bytes}"
        )
    if writer.size_bytes == 0:
        raise SandboxError(f"sandbox path archive {spec.container_path!r} is empty")
    stream.seek(0)
    return writer.size_bytes, writer.sha256


def _tar_info(name: str, size: int, *, mode: int) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = mode
    member.mtime = 0
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    return member


def _copy_json_value(value: object, *, path: str, active: set[int]) -> object:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"sandbox archive metadata {path} is not finite")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"sandbox archive metadata {path} contains a cycle")
        items = list(value.items())
        for key, _item in items:
            if not isinstance(key, str):
                raise TypeError(f"sandbox archive metadata {path} has a non-string key")
        active.add(identity)
        try:
            return {
                key: _copy_json_value(item, path=f"{path}.{key}", active=active)
                for key, item in sorted(items)
            }
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"sandbox archive metadata {path} contains a cycle")
        active.add(identity)
        try:
            return [
                _copy_json_value(item, path=f"{path}[{index}]", active=active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    raise TypeError(f"sandbox archive metadata {path} has non-JSON value {type(value).__name__}")


def _canonical_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("sandbox archive metadata must be a mapping")
    try:
        copied = _copy_json_value(metadata, path="$", active=set())
    except RecursionError as exc:
        raise ValueError("sandbox archive metadata is nested too deeply") from exc
    assert isinstance(copied, dict)
    # The round trip is the final contract check and ensures the manifest does
    # not retain a caller-owned Mapping/list subclass.
    encoded = json.dumps(
        copied,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return cast("dict[str, object]", json.loads(encoded))


def _manifest(
    phase: ArchivePhase,
    archives: list[_SpooledArchive],
    metadata: dict[str, object],
) -> dict[str, object]:
    payloads = [
        {
            "archive_path": item.spec.archive_path,
            "container_path": item.spec.container_path,
            "content_type": "application/x-tar",
            "disposition": "opaque",
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in archives
    ]
    return {
        "format": _FORMAT_NAME,
        "format_version": _FORMAT_VERSION,
        "metadata": metadata,
        "phase": phase,
        "payload_count": len(payloads),
        "payload_size_bytes": sum(item.size_bytes for item in archives),
        "payloads": payloads,
    }


def build_sandbox_archive_bundle(
    out: BinaryIO,
    *,
    phase: ArchivePhase,
    fetch_archive: ArchiveFetcher,
    metadata: Mapping[str, object] | None = None,
    max_member_bytes: int = DEFAULT_MAX_ARCHIVE_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_ARCHIVE_TOTAL_BYTES,
) -> dict[str, object]:
    """Write a deterministic ``tar.gz`` bundle and return its manifest.

    For an ``initial`` bundle, ``fetch_archive`` is called once for
    ``/workspace``.  A ``final`` bundle additionally fetches ``/agent-state``
    and ``/home/agent/.freeinference-platform`` in that order.  The callback
    writes each Docker tar stream to the supplied sink and returns the number
    of bytes written.  The platform path is omitted from a final bundle when
    the callback raises :class:`FileNotFoundError` before writing any bytes;
    the other paths are required.

    ``metadata`` is defensively copied after accepting only JSON objects,
    arrays and finite scalar values.  Canonical key ordering makes equivalent
    mappings serialize identically; caller mutation cannot change the returned
    manifest or bundle.

    Inputs are completely fetched, bounded, hashed and spooled to anonymous
    temporary files before the first output byte is written.  Therefore a
    fetch or limit failure leaves ``out`` untouched.  An output failure can
    leave a partial bundle, which the caller must discard.

    Determinism covers the outer member order, JSON encoding, tar metadata and
    gzip timestamp.  Identical callback bytes and arguments produce identical
    output; the opaque Docker tar headers themselves are intentionally not
    rewritten.
    """
    if phase not in {"initial", "final"}:
        raise ValueError("sandbox archive phase must be 'initial' or 'final'")
    _validate_limit("max_member_bytes", max_member_bytes)
    _validate_limit("max_total_bytes", max_total_bytes)
    if not callable(fetch_archive):
        raise TypeError("fetch_archive must be callable")
    copied_metadata = _canonical_metadata(metadata)

    with ExitStack() as stack:
        archives: list[_SpooledArchive] = []
        total = 0
        selected = _PATH_ARCHIVES[:1] if phase == "initial" else _PATH_ARCHIVES
        for spec in selected:
            stream = cast(
                "BinaryIO",
                stack.enter_context(tempfile.TemporaryFile(prefix="sandbox-archive-")),
            )
            fetched = _fetch_archive(
                fetch_archive,
                spec,
                stream,
                prior_total=total,
                max_member_bytes=max_member_bytes,
                max_total_bytes=max_total_bytes,
            )
            if fetched is None:
                continue
            size, sha256 = fetched
            total += size
            archives.append(
                _SpooledArchive(
                    spec=spec,
                    stream=stream,
                    size_bytes=size,
                    sha256=sha256,
                )
            )

        manifest = _manifest(phase, archives, copied_metadata)
        manifest_bytes = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "ascii"
            )
            + b"\n"
        )
        try:
            # GzipFile lets us fix the timestamp; tarfile's ``w:gz`` shorthand
            # would stamp the bundle with wall-clock time.
            with (
                gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=9,
                    fileobj=out,
                    mtime=0,
                ) as compressed,
                tarfile.open(
                    fileobj=compressed,
                    mode="w|",
                    format=tarfile.PAX_FORMAT,
                ) as bundle,
            ):
                bundle.addfile(
                    _tar_info("manifest.json", len(manifest_bytes), mode=0o444),
                    io.BytesIO(manifest_bytes),
                )
                for item in archives:
                    item.stream.seek(0)
                    bundle.addfile(
                        _tar_info(item.spec.archive_path, item.size_bytes, mode=0o400),
                        item.stream,
                    )
        except (OSError, tarfile.TarError) as exc:
            raise SandboxError(f"sandbox archive bundle could not be written: {exc}") from exc
    return manifest


__all__ = [
    "DEFAULT_MAX_ARCHIVE_MEMBER_BYTES",
    "DEFAULT_MAX_ARCHIVE_TOTAL_BYTES",
    "ArchiveFetcher",
    "ArchivePhase",
    "build_sandbox_archive_bundle",
]
