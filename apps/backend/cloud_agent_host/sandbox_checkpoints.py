"""Restorable checkpoints: a stopped sandbox's changes over its image, kept in S3.

A checkpoint is ``docker commit`` of a cleanly stopped chat container, exported
as an OCI image archive that omits every layer the sandbox image already
provides. A host holding the same base image can load it and recreate the
container, which is what will let a chat move between hosts. This module only
captures; restore is separate.

The archive is sandbox output and is treated as untrusted. While streaming,
only regular files under ``blobs/sha256/`` and the OCI index files are accepted,
every kept blob is checked against its digest, and the manifest must be the
base image's layers followed by new ones. Nothing is extracted on the host.

Docker has no command that saves a single layer. When containerd's content
store is mounted read-only into the broker, the new layers are read straight
from it: ``docker image inspect`` names the committed manifest, and the
manifest names its blobs. Otherwise the whole committed image is streamed
through ``docker image save`` and the base image's blobs are dropped on the
way, which reads the entire image for every checkpoint.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Final, TypeVar

from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.sandbox_restore import pullable_image

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from cloud_agent_host.chat_sandbox import ChatSandboxInspection, ChatSandboxSpec
    from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveReceipt
    from cloud_agent_host.shared_session_supervisor import SharedMemberManifest

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

CHECKPOINT_FORMAT_VERSION: Final = 1
#: Where host Compose mounts containerd's content store, read-only.
DEFAULT_CONTAINERD_CONTENT_DIR: Final = "/var/lib/hybridinference/containerd-content"
CHECKPOINT_IMAGE_REPOSITORY: Final = "freeinference-checkpoint"
# Index, layout and manifest files are tiny; anything larger is not an image
# description and is refused rather than buffered.
_MAX_METADATA_BYTES: Final = 1024 * 1024
_COPY_CHUNK_BYTES: Final = 1024 * 1024
_DIGEST_RE: Final = re.compile(r"[0-9a-f]{64}")
_CHECKPOINT_ID_RE: Final = re.compile(r"[0-9a-f]{32}")
_METADATA_FILES: Final = frozenset(("index.json", "oci-layout", "manifest.json", "repositories"))
_DIRECTORIES: Final = frozenset(("blobs", "blobs/sha256"))
_INDEX_TYPES: Final = frozenset(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
_MANIFEST_TYPES: Final = frozenset(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
# Upper bound on what one entry adds besides its padded body (a ustar header,
# plus room for a PAX extension), and on what closing the archive adds (two
# zero blocks padded out to a full record).
_ENTRY_OVERHEAD_BYTES: Final = 3 * tarfile.BLOCKSIZE
_ARCHIVE_TRAILER_BYTES: Final = 2 * tarfile.BLOCKSIZE + tarfile.RECORDSIZE
_COMMIT_TIMEOUT_S: Final = 600.0
_IMAGE_REMOVE_TIMEOUT_S: Final = 120.0
_PULL_TIMEOUT_S: Final = 1800.0
_LOAD_TIMEOUT_S: Final = 1800.0


class SandboxCheckpointError(SandboxError):
    """A checkpoint could not be captured or its archive was malformed."""


@dataclass(frozen=True)
class CheckpointArchive:
    """What an exported archive holds beyond its base image."""

    manifest_digest: str
    config_digest: str
    base_layers: tuple[str, ...]
    new_layers: tuple[str, ...]
    new_layer_bytes: int


@dataclass(frozen=True)
class SandboxCheckpointReceipt:
    """Durable identity of one uploaded checkpoint, reported to the control plane."""

    checkpoint_id: str
    sandbox_id: str
    generation: int
    container_id: str
    base_image: str
    runtime_id: str
    runtime_version: str | None
    object_key: str
    version_id: str | None
    sha256: str
    size_bytes: int
    new_layer_bytes: int
    layer_count: int
    created_at: str
    members: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    durations_ms: dict[str, int] = field(default_factory=dict)
    format_version: int = CHECKPOINT_FORMAT_VERSION
    #: The control plane's start fence for the run this checkpoint ends. The
    #: checkpoint covers the group only while that fence is still current;
    #: ``None`` (the fence was unknown here) never covers it.
    start_seq: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form the store keeps and the heartbeat carries."""
        payload = asdict(self)
        payload["members"] = [dict(member) for member in self.members]
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SandboxCheckpointReceipt:
        """Rebuild a stored receipt, refusing anything this version did not write."""
        try:
            receipt = cls(
                checkpoint_id=str(value["checkpoint_id"]),
                sandbox_id=str(value["sandbox_id"]),
                generation=int(value["generation"]),
                container_id=str(value["container_id"]),
                base_image=str(value["base_image"]),
                runtime_id=str(value["runtime_id"]),
                runtime_version=(
                    None if value.get("runtime_version") is None else str(value["runtime_version"])
                ),
                object_key=str(value["object_key"]),
                version_id=None if value.get("version_id") is None else str(value["version_id"]),
                sha256=str(value["sha256"]),
                size_bytes=int(value["size_bytes"]),
                new_layer_bytes=int(value["new_layer_bytes"]),
                layer_count=int(value["layer_count"]),
                created_at=str(value["created_at"]),
                members=tuple(dict(member) for member in value.get("members") or ()),
                durations_ms={
                    str(key): int(duration)
                    for key, duration in (value.get("durations_ms") or {}).items()
                },
                format_version=int(value.get("format_version", 0)),
                start_seq=None if value.get("start_seq") is None else int(value["start_seq"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxCheckpointError("stored sandbox checkpoint receipt is invalid") from exc
        if receipt.format_version != CHECKPOINT_FORMAT_VERSION:
            raise SandboxCheckpointError("stored sandbox checkpoint format is unsupported")
        if _DIGEST_RE.fullmatch(receipt.sha256) is None:
            raise SandboxCheckpointError("stored sandbox checkpoint digest is invalid")
        return receipt


class SandboxCheckpointStore:
    """Keep the latest checkpoint receipt of each sandbox on broker-private disk.

    The heartbeat repeats the latest receipt until the group is gone, and the
    control plane records it idempotently, so no acknowledgement is needed. A
    receipt that cannot be read is reported as absent rather than failing the
    listing that carries it.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def _path(self, sandbox_id: str) -> Path:
        name = hashlib.sha256(sandbox_id.encode("utf-8")).hexdigest()
        return self._root / f"{name}.json"

    def save(self, receipt: SandboxCheckpointReceipt) -> None:
        """Atomically replace the sandbox's latest receipt."""
        payload = json.dumps(receipt.as_dict(), sort_keys=True, separators=(",", ":")).encode()
        with self._lock:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self._path(receipt.sandbox_id)
            descriptor, temporary = tempfile.mkstemp(dir=self._root, prefix=".receipt-")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
            directory = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def latest(self, sandbox_id: str) -> SandboxCheckpointReceipt | None:
        """Return the sandbox's latest receipt, or ``None`` when there is none."""
        path = self._path(sandbox_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning(
                "sandbox checkpoint receipt could not be read",
                extra={"event": "sandbox_checkpoint_receipt_unreadable"},
            )
            return None
        try:
            receipt = SandboxCheckpointReceipt.from_dict(json.loads(raw))
        except (ValueError, SandboxCheckpointError):
            logger.warning(
                "sandbox checkpoint receipt is invalid",
                extra={"event": "sandbox_checkpoint_receipt_invalid"},
            )
            return None
        return receipt if receipt.sandbox_id == sandbox_id else None


class _DigestingReader:
    """Read exactly ``size`` bytes while hashing them."""

    def __init__(self, source: IO[bytes], size: int) -> None:
        self._source = source
        self._remaining = size
        self.digest = hashlib.sha256()

    def read(self, amount: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        wanted = self._remaining if amount is None or amount < 0 else min(amount, self._remaining)
        chunk = self._source.read(wanted)
        self._remaining -= len(chunk)
        self.digest.update(chunk)
        return chunk


def _member_kind(member: tarfile.TarInfo) -> tuple[str, str | None]:
    """Classify one archive entry, refusing anything an image archive never holds."""
    name = member.name.rstrip("/")
    while name.startswith("./"):
        name = name[2:]
    if member.isdir():
        if name in _DIRECTORIES:
            return "directory", None
        raise SandboxCheckpointError("checkpoint archive contains an unexpected directory")
    if not member.isreg():
        raise SandboxCheckpointError("checkpoint archive contains a non-regular file")
    if name in _METADATA_FILES:
        if member.size > _MAX_METADATA_BYTES:
            raise SandboxCheckpointError("checkpoint archive metadata is too large")
        return "metadata", name
    prefix = "blobs/sha256/"
    if name.startswith(prefix) and _DIGEST_RE.fullmatch(name[len(prefix) :]):
        return "blob", name[len(prefix) :]
    raise SandboxCheckpointError("checkpoint archive contains an unexpected file")


def _load_json(raw: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SandboxCheckpointError(f"checkpoint {what} is not JSON") from exc
    if not isinstance(value, dict):
        raise SandboxCheckpointError(f"checkpoint {what} is not an object")
    return value


def _descriptor_digest(descriptor: Any) -> str:
    if not isinstance(descriptor, dict):
        raise SandboxCheckpointError("checkpoint descriptor is invalid")
    digest = descriptor.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise SandboxCheckpointError("checkpoint descriptor digest is invalid")
    value = digest[len("sha256:") :]
    if _DIGEST_RE.fullmatch(value) is None:
        raise SandboxCheckpointError("checkpoint descriptor digest is invalid")
    return value


def _image_manifests(
    metadata: Mapping[str, bytes],
    small_blobs: Mapping[str, bytes],
) -> list[tuple[str, dict[str, Any]]]:
    """Resolve ``index.json`` to the image manifests present in the archive."""
    index_raw = metadata.get("index.json")
    if index_raw is None:
        raise SandboxCheckpointError("checkpoint archive has no index.json")
    manifests = _walk_manifests(
        list(_load_json(index_raw, "index").get("manifests") or []), small_blobs.get
    )
    if not manifests:
        raise SandboxCheckpointError("checkpoint archive has no image manifest")
    return manifests


def _walk_manifests(
    descriptors: list[Any],
    read: Callable[[str], bytes | None],
) -> list[tuple[str, dict[str, Any]]]:
    """Follow index descriptors to every image manifest whose blob is present.

    The result is empty when none is present; callers decide what that means.
    """
    pending: list[Any] = list(descriptors)
    manifests: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    while pending:
        descriptor = pending.pop()
        digest = _descriptor_digest(descriptor)
        if digest in seen:
            continue
        seen.add(digest)
        raw = read(digest)
        if raw is None:
            # A multi-platform index lists manifests whose blobs were never
            # pulled; only the ones the archive carries describe this image.
            continue
        document = _load_json(raw, "manifest")
        media_type = document.get("mediaType") or descriptor.get("mediaType")
        if media_type in _INDEX_TYPES:
            pending.extend(document.get("manifests") or [])
        elif media_type in _MANIFEST_TYPES:
            manifests.append((digest, document))
        else:
            raise SandboxCheckpointError("checkpoint archive has an unknown manifest type")
    return manifests


def _manifest_layers(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise SandboxCheckpointError("checkpoint manifest has no layers")
    return tuple(_descriptor_digest(layer) for layer in layers)


def read_base_layers(save_stream: IO[bytes]) -> frozenset[str]:
    """Return every layer digest a ``docker save`` of the base image provides."""
    metadata: dict[str, bytes] = {}
    small_blobs: dict[str, bytes] = {}
    with tarfile.open(fileobj=save_stream, mode="r|") as archive:
        for member in archive:
            kind, name = _member_kind(member)
            if kind == "directory":
                continue
            if member.size > _MAX_METADATA_BYTES:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise SandboxCheckpointError("base image archive entry is unreadable")
            body = handle.read()
            assert name is not None
            if kind == "metadata":
                metadata[name] = body
            else:
                small_blobs[name] = body
    layers: set[str] = set()
    for _digest, manifest in _image_manifests(metadata, small_blobs):
        layers.update(_manifest_layers(manifest))
    return frozenset(layers)


def export_checkpoint_archive(
    save_stream: IO[bytes],
    output: BinaryIO,
    *,
    base_layers: frozenset[str],
    max_bytes: int,
) -> CheckpointArchive:
    """Copy a committed image's ``docker save`` stream without the base layers.

    The result is a loadable OCI archive on any host that already holds the
    base image. Every kept blob is verified against its digest, and the
    manifest must be the base layers followed by at least one new layer.

    ``max_bytes`` bounds the scratch file, not only the result: each entry's
    declared size is checked before any of it is written, and a tar entry
    never copies more than it declares.
    """
    if max_bytes <= 0:
        raise ValueError("checkpoint size limit must be positive")

    def reserve(size: int) -> None:
        padded = -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
        projected = target.offset + _ENTRY_OVERHEAD_BYTES + padded + _ARCHIVE_TRAILER_BYTES
        if projected > max_bytes:
            raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")

    metadata: dict[str, bytes] = {}
    small_blobs: dict[str, bytes] = {}
    kept_blob_bytes: dict[str, int] = {}
    with (
        tarfile.open(fileobj=save_stream, mode="r|") as source,
        tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as target,
    ):
        for member in source:
            kind, name = _member_kind(member)
            if kind == "blob" and name in base_layers:
                continue
            entry = tarfile.TarInfo(member.name)
            entry.mode = 0o755 if kind == "directory" else 0o644
            entry.mtime = member.mtime
            if kind == "directory":
                entry.type = tarfile.DIRTYPE
                reserve(0)
                target.addfile(entry)
                continue
            reserve(member.size)
            entry.size = member.size
            handle = source.extractfile(member)
            if handle is None:
                raise SandboxCheckpointError("checkpoint archive entry is unreadable")
            if kind == "metadata" or member.size <= _MAX_METADATA_BYTES:
                body = handle.read()
                if len(body) != member.size:
                    raise SandboxCheckpointError("checkpoint archive entry is truncated")
                assert name is not None
                if kind == "metadata":
                    metadata[name] = body
                else:
                    if hashlib.sha256(body).hexdigest() != name:
                        raise SandboxCheckpointError("checkpoint blob does not match its digest")
                    small_blobs[name] = body
                    kept_blob_bytes[name] = len(body)
                target.addfile(entry, _BytesReader(body))
            else:
                reader = _DigestingReader(handle, member.size)
                target.addfile(entry, reader)
                assert name is not None
                if reader.digest.hexdigest() != name:
                    raise SandboxCheckpointError("checkpoint blob does not match its digest")
                kept_blob_bytes[name] = member.size
    if output.tell() > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
    manifests = _image_manifests(metadata, small_blobs)
    if len(manifests) != 1:
        raise SandboxCheckpointError("checkpoint archive must describe exactly one image")
    manifest_digest, manifest = manifests[0]
    config_digest = _descriptor_digest(manifest.get("config"))
    if config_digest not in small_blobs:
        raise SandboxCheckpointError("checkpoint archive is missing its image config")
    inherited, added = _split_new_layers(_manifest_layers(manifest), base_layers)
    missing = [layer for layer in added if layer not in kept_blob_bytes]
    if missing:
        raise SandboxCheckpointError("checkpoint archive is missing a new layer")
    return CheckpointArchive(
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        base_layers=inherited,
        new_layers=added,
        new_layer_bytes=sum(kept_blob_bytes[layer] for layer in added),
    )


def _split_new_layers(
    layers: tuple[str, ...],
    base_layers: frozenset[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a manifest into the image's layers and the checkpoint's own."""
    split = next(
        (position for position, layer in enumerate(layers) if layer not in base_layers),
        len(layers),
    )
    inherited, added = layers[:split], layers[split:]
    if not inherited:
        raise SandboxCheckpointError("checkpoint does not extend the sandbox image")
    if not added:
        raise SandboxCheckpointError("checkpoint adds no layer to the sandbox image")
    if any(layer in base_layers for layer in added):
        raise SandboxCheckpointError("checkpoint layers interleave with the sandbox image")
    return inherited, added


class CheckpointContentUnavailable(SandboxCheckpointError):
    """The content store cannot serve this image; the capture falls back to save."""


class ContainerdContentStore:
    """A read-only view of containerd's content store, whose blobs are digest-named.

    Containerd writes a blob under its digest only after verifying it, and this
    view verifies every read again, so a wrong mount or a damaged file is
    refused instead of copied.
    """

    def __init__(self, root: Path) -> None:
        self._blobs = root / "blobs" / "sha256"

    def available(self) -> bool:
        """Whether the mount looks like a content store at all."""
        try:
            return self._blobs.is_dir()
        except OSError:
            return False

    def _path(self, digest: str) -> Path:
        if _DIGEST_RE.fullmatch(digest) is None:
            raise SandboxCheckpointError("content store digest is invalid")
        return self._blobs / digest

    def size(self, digest: str) -> int | None:
        """Return a blob's size, or ``None`` when the store does not hold it."""
        try:
            return self._path(digest).stat().st_size
        except FileNotFoundError:
            return None

    def read(self, digest: str) -> bytes | None:
        """Return a small, verified blob such as a manifest, index or config."""
        size = self.size(digest)
        if size is None:
            return None
        if size > _MAX_METADATA_BYTES:
            raise SandboxCheckpointError("content store metadata blob is too large")
        body = self._path(digest).read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise SandboxCheckpointError("content store blob does not match its digest")
        return body

    def open(self, digest: str) -> BinaryIO:
        """Open a blob for streaming; the caller verifies what it copies."""
        return self._path(digest).open("rb")


def _image_names(reference: str) -> tuple[str, str]:
    """Return the fully qualified name and tag Docker records for ``reference``."""
    repository, _, tag = reference.rpartition(":")
    first = repository.split("/", 1)[0]
    if "/" not in repository:
        repository = f"docker.io/library/{repository}"
    elif "." not in first and ":" not in first and first != "localhost":
        repository = f"docker.io/{repository}"
    return f"{repository}:{tag}", tag


def checkpoint_image_reference(sandbox_id: str, checkpoint_id: str) -> str:
    """The one local name a checkpoint image has, on the host that took it or restores it."""
    sandbox_hash = hashlib.sha256(sandbox_id.encode("utf-8")).hexdigest()[:32]
    return f"{CHECKPOINT_IMAGE_REPOSITORY}/{sandbox_hash}:{checkpoint_id}"


def _image_metadata(
    *,
    reference: str,
    manifest_digest: str,
    manifest_size: int,
    media_type: str,
    config_digest: str,
    layers: tuple[str, ...],
) -> tuple[bytes, bytes, bytes]:
    """``index.json``, ``manifest.json`` and ``oci-layout`` naming one image ``reference``.

    The layout ``docker image save`` writes, so ``docker load`` accepts it with
    either image store. Written by this module rather than copied from an
    archive, so the only name a load can create is the one passed here.
    """
    name, tag = _image_names(reference)
    index_body = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": media_type,
                    "digest": f"sha256:{manifest_digest}",
                    "size": manifest_size,
                    "annotations": {
                        "io.containerd.image.name": name,
                        "org.opencontainers.image.ref.name": tag,
                    },
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    docker_manifest = json.dumps(
        [
            {
                "Config": f"blobs/sha256/{config_digest}",
                "RepoTags": [reference],
                "Layers": [f"blobs/sha256/{layer}" for layer in layers],
            }
        ],
        separators=(",", ":"),
    ).encode()
    return index_body, docker_manifest, b'{"imageLayoutVersion":"1.0.0"}'


def prepare_restore_archive(
    source: IO[bytes],
    output: IO[bytes],
    *,
    reference: str,
    base_layers: frozenset[str],
    max_bytes: int,
) -> CheckpointArchive:
    """Check a downloaded checkpoint and rewrite it so loading can only create ``reference``.

    The archive was written by whichever host took the checkpoint, so nothing
    in it is trusted. Every blob is verified against its digest while it is
    copied. The image must be exactly one manifest whose layers are this
    host's base image followed by layers the archive carries: a manifest that
    named any other layer could assemble a container from an image this host
    holds for someone else. Only the blobs that image needs are accepted. The
    archive's own ``index.json`` and ``manifest.json`` are read but never
    copied; the output gets fresh ones naming ``reference``, so a forged name
    cannot retag an image on this host.
    """
    if max_bytes <= 0:
        raise ValueError("checkpoint size limit must be positive")
    if not base_layers:
        raise SandboxCheckpointError("restore needs the sandbox image's layers")
    metadata: dict[str, bytes] = {}
    small_blobs: dict[str, bytes] = {}
    blob_bytes: dict[str, int] = {}
    with (
        tarfile.open(fileobj=source, mode="r|") as archive,
        tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as target,
    ):
        for directory in ("blobs", "blobs/sha256"):
            entry = tarfile.TarInfo(directory)
            entry.type = tarfile.DIRTYPE
            entry.mode = 0o755
            target.addfile(entry)
        for member in archive:
            kind, name = _member_kind(member)
            if kind == "directory":
                continue
            assert name is not None
            handle = archive.extractfile(member)
            if handle is None:
                raise SandboxCheckpointError("checkpoint archive entry is unreadable")
            if kind == "metadata":
                body = handle.read()
                if len(body) != member.size:
                    raise SandboxCheckpointError("checkpoint archive entry is truncated")
                metadata[name] = body
                continue
            if name in blob_bytes:
                raise SandboxCheckpointError("checkpoint archive repeats a blob")
            padded = -(-member.size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
            if output.tell() + _ENTRY_OVERHEAD_BYTES + padded + _ARCHIVE_TRAILER_BYTES > max_bytes:
                raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
            entry = tarfile.TarInfo(f"blobs/sha256/{name}")
            entry.mode = 0o644
            entry.size = member.size
            if member.size <= _MAX_METADATA_BYTES:
                body = handle.read()
                if len(body) != member.size:
                    raise SandboxCheckpointError("checkpoint archive entry is truncated")
                if hashlib.sha256(body).hexdigest() != name:
                    raise SandboxCheckpointError("checkpoint blob does not match its digest")
                small_blobs[name] = body
                target.addfile(entry, _BytesReader(body))
            else:
                reader = _DigestingReader(handle, member.size)
                target.addfile(entry, reader)
                if reader.digest.hexdigest() != name:
                    raise SandboxCheckpointError("checkpoint blob does not match its digest")
            blob_bytes[name] = member.size
        manifests = _image_manifests(metadata, small_blobs)
        if len(manifests) != 1:
            raise SandboxCheckpointError("checkpoint archive must describe exactly one image")
        manifest_digest, manifest = manifests[0]
        media_type = manifest.get("mediaType")
        if media_type not in _MANIFEST_TYPES:
            raise SandboxCheckpointError("checkpoint archive has an unknown manifest type")
        config_digest = _descriptor_digest(manifest.get("config"))
        if config_digest not in small_blobs:
            raise SandboxCheckpointError("checkpoint archive is missing its image config")
        layers = _manifest_layers(manifest)
        inherited, added = _split_new_layers(layers, base_layers)
        if any(layer not in blob_bytes for layer in added):
            raise SandboxCheckpointError("checkpoint archive is missing a new layer")
        if set(blob_bytes) != {manifest_digest, config_digest, *added}:
            raise SandboxCheckpointError("checkpoint archive holds blobs its image does not use")
        index_body, docker_manifest, layout_body = _image_metadata(
            reference=reference,
            manifest_digest=manifest_digest,
            manifest_size=blob_bytes[manifest_digest],
            media_type=str(media_type),
            config_digest=config_digest,
            layers=layers,
        )
        for name, body in (
            ("index.json", index_body),
            ("manifest.json", docker_manifest),
            ("oci-layout", layout_body),
        ):
            entry = tarfile.TarInfo(name)
            entry.mode = 0o644
            entry.size = len(body)
            target.addfile(entry, _BytesReader(body))
    if output.tell() > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
    return CheckpointArchive(
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        base_layers=inherited,
        new_layers=added,
        new_layer_bytes=sum(blob_bytes[layer] for layer in added),
    )


def _descriptor_of(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("mediaType") not in _INDEX_TYPES | _MANIFEST_TYPES:
        raise CheckpointContentUnavailable(f"{what} has no image descriptor")
    _descriptor_digest(raw)
    return raw


def export_from_content_store(
    store: ContainerdContentStore,
    output: BinaryIO,
    *,
    image: Mapping[str, Any],
    base: Mapping[str, Any],
    reference: str,
    max_bytes: int,
) -> CheckpointArchive:
    """Write a committed image's new layers as a loadable OCI archive.

    ``image`` and ``base`` are the descriptors ``docker image inspect`` reports
    for the commit and for the sandbox image. Only the commit's manifest, its
    config and its new layers are written, in the layout ``docker image save``
    produces, so it loads on any host that holds the base image. Everything
    is sized before the first byte is written and verified while copied.
    """
    if max_bytes <= 0:
        raise ValueError("checkpoint size limit must be positive")
    base_manifests = _walk_manifests([_descriptor_of(base, "sandbox image")], store.read)
    if not base_manifests:
        raise CheckpointContentUnavailable("sandbox image manifest is not in the content store")
    base_layers = frozenset(
        layer for _digest, manifest in base_manifests for layer in _manifest_layers(manifest)
    )
    image_descriptor = _descriptor_of(image, "checkpoint image")
    manifests = _walk_manifests([image_descriptor], store.read)
    if not manifests:
        raise CheckpointContentUnavailable("checkpoint manifest is not in the content store")
    if len(manifests) != 1:
        raise SandboxCheckpointError("checkpoint image must have exactly one manifest")
    manifest_digest, manifest = manifests[0]
    config_digest = _descriptor_digest(manifest.get("config"))
    layers = _manifest_layers(manifest)
    inherited, added = _split_new_layers(layers, base_layers)
    manifest_body = store.read(manifest_digest)
    config_body = store.read(config_digest)
    sizes = {layer: store.size(layer) for layer in added}
    if manifest_body is None or config_body is None or None in sizes.values():
        raise CheckpointContentUnavailable("checkpoint blobs are not in the content store")
    layer_sizes = {layer: size for layer, size in sizes.items() if size is not None}
    index_body, docker_manifest, layout_body = _image_metadata(
        reference=reference,
        manifest_digest=manifest_digest,
        manifest_size=len(manifest_body),
        media_type=str(manifest.get("mediaType") or image_descriptor["mediaType"]),
        config_digest=config_digest,
        layers=layers,
    )
    files: list[tuple[str, int]] = [
        (f"blobs/sha256/{manifest_digest}", len(manifest_body)),
        (f"blobs/sha256/{config_digest}", len(config_body)),
        *((f"blobs/sha256/{layer}", layer_sizes[layer]) for layer in added),
        ("index.json", len(index_body)),
        ("manifest.json", len(docker_manifest)),
        ("oci-layout", len(layout_body)),
    ]
    projected = _ARCHIVE_TRAILER_BYTES + 2 * _ENTRY_OVERHEAD_BYTES
    for _name, size in files:
        projected += _ENTRY_OVERHEAD_BYTES + -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    if projected > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as target:
        for directory in ("blobs", "blobs/sha256"):
            entry = tarfile.TarInfo(directory)
            entry.type = tarfile.DIRTYPE
            entry.mode = 0o755
            target.addfile(entry)

        def add_bytes(name: str, body: bytes) -> None:
            entry = tarfile.TarInfo(name)
            entry.mode = 0o644
            entry.size = len(body)
            target.addfile(entry, _BytesReader(body))

        add_bytes(f"blobs/sha256/{manifest_digest}", manifest_body)
        add_bytes(f"blobs/sha256/{config_digest}", config_body)
        for layer in added:
            entry = tarfile.TarInfo(f"blobs/sha256/{layer}")
            entry.mode = 0o644
            entry.size = layer_sizes[layer]
            with store.open(layer) as source:
                reader = _DigestingReader(source, entry.size)
                target.addfile(entry, reader)
            if reader.digest.hexdigest() != layer:
                raise SandboxCheckpointError("content store blob does not match its digest")
        add_bytes("index.json", index_body)
        add_bytes("manifest.json", docker_manifest)
        add_bytes("oci-layout", layout_body)
    if output.tell() > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
    return CheckpointArchive(
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        base_layers=inherited,
        new_layers=added,
        new_layer_bytes=sum(layer_sizes.values()),
    )


class _BytesReader:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._offset = 0

    def read(self, amount: int = -1) -> bytes:
        end = len(self._body) if amount is None or amount < 0 else self._offset + amount
        chunk = self._body[self._offset : end]
        self._offset += len(chunk)
        return chunk


class _Watchdog:
    """Kill a child process if the deadline passes while its output is read.

    Reading ``docker image save`` blocks; a timeout on ``wait`` alone would only
    start after the read returned. Killing the process ends the read with EOF,
    so the caller can release its capture slot and finish the stop.
    """

    def __init__(self, process: subprocess.Popen[bytes], seconds: float) -> None:
        self.fired = threading.Event()
        self._process = process
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        self.fired.set()
        with contextlib.suppress(OSError):
            self._process.kill()

    def __enter__(self) -> _Watchdog:
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._timer.cancel()


@dataclass(frozen=True)
class CheckpointRestore:
    """The control plane's record of the checkpoint a moved group is recreated from."""

    checkpoint_id: str
    object_key: str
    version_id: str | None
    sha256: str
    size_bytes: int
    base_image: str
    format_version: int = CHECKPOINT_FORMAT_VERSION

    def __post_init__(self) -> None:
        if _CHECKPOINT_ID_RE.fullmatch(self.checkpoint_id or "") is None:
            raise SandboxCheckpointError("checkpoint id must be 32 lowercase hex digits")
        if _DIGEST_RE.fullmatch(self.sha256 or "") is None:
            raise SandboxCheckpointError("checkpoint digest must be 64 lowercase hex digits")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise SandboxCheckpointError("checkpoint size must be an integer")
        if self.size_bytes <= 0:
            raise SandboxCheckpointError("checkpoint size must be positive")
        if self.format_version != CHECKPOINT_FORMAT_VERSION:
            raise SandboxCheckpointError("checkpoint format is not supported by this host")
        if not pullable_image(self.base_image):
            raise SandboxCheckpointError("checkpoint base image is not a pullable digest")


class SandboxCheckpointCapture:
    """Commit, export and upload one stopped group through the Docker CLI."""

    def __init__(
        self,
        *,
        docker_binary: str,
        spool_dir: Path,
        store: SandboxCheckpointStore,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        content_dir: Path | None = None,
    ) -> None:
        self._docker = docker_binary
        self._spool_dir = spool_dir
        self.store = store
        self._content = None if content_dir is None else ContainerdContentStore(content_dir)
        self._run = run
        self._popen = popen
        self._clock = clock
        self._now = now
        self._base_layers: dict[str, frozenset[str]] = {}
        self._base_lock = threading.Lock()
        #: Where a receipt learns its group's start fence; the sandbox manager
        #: wires its supervisor in once both exist.
        self.start_seq_for: Callable[[str], int | None] = lambda _sandbox_id: None

    def capture(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: Iterable[SharedMemberManifest],
        *,
        upload: Callable[..., SandboxArchiveReceipt],
        max_bytes: int,
        deadline: float,
    ) -> SandboxCheckpointReceipt:
        """Checkpoint one exact stopped container and remember its receipt."""
        if inspection.running:
            raise SandboxCheckpointError("refusing to checkpoint a running sandbox")
        base_image = spec.sandbox_image_digest or inspection.image
        if not base_image:
            raise SandboxCheckpointError("sandbox checkpoint needs its base image identity")
        start_seq = self.start_seq_for(spec.sandbox_id)
        checkpoint_id = secrets.token_hex(16)
        reference = checkpoint_image_reference(spec.sandbox_id, checkpoint_id)
        durations: dict[str, int] = {}
        started = self._clock()
        timeout = min(_COMMIT_TIMEOUT_S, self._remaining(deadline))
        try:
            committed = self._run(
                [
                    self._docker,
                    "container",
                    "commit",
                    "--pause=false",
                    inspection.container_id,
                    reference,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except BaseException:
            # A timeout kills only the client: the daemon may still finish the
            # commit and tag the image, which nothing else would ever remove.
            self._remove_image(reference)
            raise
        if committed.returncode != 0:
            raise SandboxCheckpointError("sandbox checkpoint commit failed")
        durations["commit"] = self._elapsed_ms(started)
        try:
            self._spool_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=self._spool_dir, prefix="checkpoint-") as spool:
                archive = self._export_from_content(
                    reference, base_image, spool, max_bytes, deadline, durations
                )
                if archive is None:
                    base_layers = self._base_image_layers(base_image, deadline, durations)
                    started = self._clock()
                    archive = self._export(reference, spool, base_layers, max_bytes, deadline)
                    durations["export"] = self._elapsed_ms(started)
                size = spool.tell()
                spool.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: spool.read(_COPY_CHUNK_BYTES), b""):
                    digest.update(chunk)
                spool.seek(0)
                started = self._clock()
                uploaded = upload(
                    sandbox_id=spec.sandbox_id,
                    generation=spec.generation,
                    checkpoint_id=checkpoint_id,
                    body=spool,
                    size=size,
                    sha256=digest.hexdigest(),
                    deadline=deadline,
                )
                durations["upload"] = self._elapsed_ms(started)
        finally:
            self._remove_image(reference)
        receipt = SandboxCheckpointReceipt(
            checkpoint_id=checkpoint_id,
            sandbox_id=spec.sandbox_id,
            generation=spec.generation,
            container_id=inspection.container_id,
            base_image=base_image,
            runtime_id=spec.runtime_id,
            runtime_version=spec.runtime_version,
            object_key=uploaded.key,
            version_id=uploaded.version_id,
            sha256=uploaded.sha256,
            size_bytes=uploaded.size,
            new_layer_bytes=archive.new_layer_bytes,
            layer_count=len(archive.base_layers) + len(archive.new_layers),
            created_at=self._now().isoformat(),
            members=tuple(
                {
                    "thread_id": member.thread_id,
                    "native_session_id": member.native_session_id,
                    "last_settled_job_id": member.last_settled_job_id,
                    "context_status": member.context_status,
                }
                for member in members
            ),
            durations_ms=durations,
            start_seq=start_seq,
        )
        self.store.save(receipt)
        return receipt

    def _export_from_content(
        self,
        reference: str,
        base_image: str,
        spool: BinaryIO,
        max_bytes: int,
        deadline: float,
        durations: dict[str, int],
    ) -> CheckpointArchive | None:
        """Export through the content store, or return ``None`` to use image save."""
        if self._content is None or not self._content.available():
            return None
        started = self._clock()
        try:
            archive = export_from_content_store(
                self._content,
                spool,
                image=self._image_descriptor(reference, deadline),
                base=self._image_descriptor(base_image, deadline),
                reference=reference,
                max_bytes=max_bytes,
            )
        except CheckpointContentUnavailable as exc:
            logger.warning(
                "sandbox checkpoint falls back to image save",
                extra={"event": "sandbox_checkpoint_content_unavailable", "detail": str(exc)},
            )
            spool.seek(0)
            spool.truncate()
            return None
        durations["export"] = self._elapsed_ms(started)
        return archive

    def restore(
        self,
        sandbox_id: str,
        restore: CheckpointRestore,
        *,
        download: Callable[..., None],
        max_bytes: int,
        deadline: float,
    ) -> str:
        """Make ``restore``'s checkpoint a local image and return its name.

        The base image is pulled by digest when this host lacks it. The object
        is downloaded against its receipt, checked and rewritten by
        :func:`prepare_restore_archive`, and only then loaded. An image this
        host already holds under the checkpoint's name came from that same
        path (or is the original commit) and is reused.
        """
        reference = checkpoint_image_reference(sandbox_id, restore.checkpoint_id)
        if self._image_present(reference, deadline):
            return reference
        if not self._image_present(restore.base_image, deadline):
            pulled = self._run(
                [self._docker, "image", "pull", "--quiet", restore.base_image],
                capture_output=True,
                text=True,
                timeout=min(_PULL_TIMEOUT_S, self._remaining(deadline)),
                check=False,
            )
            if pulled.returncode != 0:
                raise SandboxCheckpointError("sandbox image could not be pulled for restore")
        base_layers = self.base_layers(restore.base_image, deadline)
        self._spool_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (
            tempfile.NamedTemporaryFile(dir=self._spool_dir, prefix="restore-") as downloaded,
            tempfile.NamedTemporaryFile(dir=self._spool_dir, prefix="restore-") as prepared,
        ):
            download(
                sandbox_id=sandbox_id,
                key=restore.object_key,
                version_id=restore.version_id,
                size=restore.size_bytes,
                sha256=restore.sha256,
                output=downloaded,
                deadline=deadline,
            )
            downloaded.flush()
            downloaded.seek(0)
            prepare_restore_archive(
                downloaded,
                prepared,
                reference=reference,
                base_layers=base_layers,
                max_bytes=max_bytes,
            )
            prepared.flush()
            loaded = self._run(
                [self._docker, "image", "load", "--quiet", "--input", prepared.name],
                capture_output=True,
                text=True,
                timeout=min(_LOAD_TIMEOUT_S, self._remaining(deadline)),
                check=False,
            )
        if loaded.returncode != 0 or not self._image_present(reference, deadline):
            raise SandboxCheckpointError("sandbox checkpoint could not be loaded")
        return reference

    def _image_present(self, image: str, deadline: float) -> bool:
        inspected = self._run(
            [self._docker, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=min(_IMAGE_REMOVE_TIMEOUT_S, self._remaining(deadline)),
            check=False,
        )
        return inspected.returncode == 0

    def _image_descriptor(self, image: str, deadline: float) -> dict[str, Any]:
        """Ask Docker which manifest or index an image is, as containerd stores it."""
        inspected = self._run(
            [self._docker, "image", "inspect", "--format", "{{json .Descriptor}}", image],
            capture_output=True,
            text=True,
            timeout=min(_IMAGE_REMOVE_TIMEOUT_S, self._remaining(deadline)),
            check=False,
        )
        if inspected.returncode != 0:
            raise CheckpointContentUnavailable("image descriptor is unavailable")
        try:
            descriptor = json.loads(inspected.stdout)
        except ValueError as exc:
            raise CheckpointContentUnavailable("image descriptor is not JSON") from exc
        return _descriptor_of(descriptor, "image")

    def base_layers(self, base_image: str, deadline: float) -> frozenset[str]:
        """Every layer digest ``base_image`` has on this host, for checking a restore.

        Read from containerd's content store when it is mounted, else from one
        ``docker image save`` of the image, remembered per image either way.
        """
        with self._base_lock:
            cached = self._base_layers.get(base_image)
        if cached is not None:
            return cached
        if self._content is not None and self._content.available():
            try:
                found = _walk_manifests(
                    [self._image_descriptor(base_image, deadline)], self._content.read
                )
            except CheckpointContentUnavailable:
                found = []
            layers = frozenset(
                layer for _digest, manifest in found for layer in _manifest_layers(manifest)
            )
            if layers:
                with self._base_lock:
                    self._base_layers[base_image] = layers
                return layers
        return self._base_image_layers(base_image, deadline, {})

    def _base_image_layers(
        self,
        base_image: str,
        deadline: float,
        durations: dict[str, int],
    ) -> frozenset[str]:
        with self._base_lock:
            cached = self._base_layers.get(base_image)
            if cached is not None:
                return cached
            started = self._clock()
            process = self._popen(
                [self._docker, "image", "save", base_image],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                assert process.stdout is not None
                stdout = process.stdout
                layers = self._read_before_deadline(
                    process,
                    deadline,
                    lambda: read_base_layers(stdout),
                    "sandbox image scan failed",
                )
            finally:
                self._reap(process)
            if not layers:
                raise SandboxCheckpointError("sandbox image has no layers")
            self._base_layers[base_image] = layers
            durations["base_scan"] = self._elapsed_ms(started)
            return layers

    def _export(
        self,
        reference: str,
        spool: BinaryIO,
        base_layers: frozenset[str],
        max_bytes: int,
        deadline: float,
    ) -> CheckpointArchive:
        process = self._popen(
            [self._docker, "image", "save", reference],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdout is not None
            stdout = process.stdout
            return self._read_before_deadline(
                process,
                deadline,
                lambda: export_checkpoint_archive(
                    stdout,
                    spool,
                    base_layers=base_layers,
                    max_bytes=max_bytes,
                ),
                "sandbox checkpoint export failed",
            )
        finally:
            self._reap(process)

    def _read_before_deadline(
        self,
        process: subprocess.Popen[bytes],
        deadline: float,
        read: Callable[[], _T],
        message: str,
    ) -> _T:
        """Consume a child's output and exit status, or kill it at the deadline."""
        with _Watchdog(process, self._remaining(deadline)) as watchdog:
            try:
                result = read()
                self._wait(process, deadline, message)
            except (tarfile.TarError, EOFError, OSError, SandboxCheckpointError) as exc:
                if watchdog.fired.is_set():
                    raise SandboxCheckpointError("sandbox checkpoint deadline expired") from exc
                if isinstance(exc, SandboxCheckpointError):
                    raise
                raise SandboxCheckpointError(message) from exc
        if watchdog.fired.is_set():
            raise SandboxCheckpointError("sandbox checkpoint deadline expired")
        return result

    def _wait(self, process: subprocess.Popen[bytes], deadline: float, message: str) -> None:
        try:
            returncode = process.wait(timeout=self._remaining(deadline))
        except subprocess.TimeoutExpired as exc:
            raise SandboxCheckpointError("sandbox checkpoint deadline expired") from exc
        if returncode != 0:
            raise SandboxCheckpointError(message)

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()

    def _remove_image(self, reference: str) -> None:
        try:
            removed = self._run(
                [self._docker, "image", "rm", reference],
                capture_output=True,
                text=True,
                timeout=_IMAGE_REMOVE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            removed = None
        if removed is None or removed.returncode != 0:
            logger.warning(
                "sandbox checkpoint image could not be removed",
                extra={"event": "sandbox_checkpoint_image_remove_failed"},
            )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise SandboxCheckpointError("sandbox checkpoint deadline expired")
        return remaining

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))


__all__ = [
    "CHECKPOINT_FORMAT_VERSION",
    "CHECKPOINT_IMAGE_REPOSITORY",
    "DEFAULT_CONTAINERD_CONTENT_DIR",
    "CheckpointArchive",
    "CheckpointContentUnavailable",
    "CheckpointRestore",
    "ContainerdContentStore",
    "SandboxCheckpointCapture",
    "SandboxCheckpointError",
    "SandboxCheckpointReceipt",
    "SandboxCheckpointStore",
    "checkpoint_image_reference",
    "export_checkpoint_archive",
    "export_from_content_store",
    "prepare_restore_archive",
    "read_base_layers",
]
