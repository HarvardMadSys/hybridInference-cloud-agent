"""Bounded OCI checkpoint archive validation and rewriting.

The archive is sandbox output and is treated as untrusted. While streaming,
only regular files under ``blobs/sha256/`` and the OCI index files are accepted,
every kept blob is checked against its digest, and the manifest must be the
base image's layers followed by new ones. Nothing is extracted on the host.
The codec also enforces explicit size ceilings, and emits only the one image
identity chosen by its caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Final, Protocol

from cloud_agent_host.sandbox_checkpoint_store import SandboxCheckpointError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class _CheckpointContentStore(Protocol):
    """Verified digest-addressed content available to the pure codec."""

    def size(self, digest: str) -> int | None: ...
    def read(self, digest: str) -> bytes | None: ...
    def open(self, digest: str) -> BinaryIO: ...


# Index, layout and manifest files are tiny; anything larger is not an image
# description and is refused rather than buffered.
_MAX_METADATA_BYTES: Final = 1024 * 1024
_DIGEST_RE: Final = re.compile(r"[0-9a-f]{64}")
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


@dataclass(frozen=True)
class CheckpointArchive:
    """What an exported archive holds beyond its base image."""

    manifest_digest: str
    config_digest: str
    base_layers: tuple[str, ...]
    new_layers: tuple[str, ...]
    new_layer_bytes: int


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


class _BytesReader:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._offset = 0

    def read(self, amount: int = -1) -> bytes:
        end = len(self._body) if amount is None or amount < 0 else self._offset + amount
        chunk = self._body[self._offset : end]
        self._offset += len(chunk)
        return chunk


def _entry_bytes(size: int) -> int:
    """The conservative tar space one entry consumes, including PAX room."""
    padded = -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    return _ENTRY_OVERHEAD_BYTES + padded


def _require_entry_space(current: int, size: int, max_bytes: int) -> None:
    """Refuse an entry before its declared body can cross the archive ceiling."""
    if current + _entry_bytes(size) + _ARCHIVE_TRAILER_BYTES > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")


def _entry(
    name: str, size: int = 0, *, directory: bool = False, mtime: int | float = 0
) -> tarfile.TarInfo:
    """Build the one normalized header shape emitted by checkpoint codecs."""
    entry = tarfile.TarInfo(name)
    entry.mode = 0o755 if directory else 0o644
    entry.mtime = mtime
    entry.size = size
    if directory:
        entry.type = tarfile.DIRTYPE
    return entry


def _add_directory(target: tarfile.TarFile, name: str, *, mtime: int | float = 0) -> None:
    target.addfile(_entry(name, directory=True, mtime=mtime))


def _add_bytes(
    target: tarfile.TarFile,
    name: str,
    body: bytes,
    *,
    mtime: int | float = 0,
) -> None:
    target.addfile(_entry(name, len(body), mtime=mtime), _BytesReader(body))


def _copy_verified_blob(
    target: tarfile.TarFile,
    name: str,
    source: IO[bytes],
    size: int,
    digest: str,
    mismatch: str,
    *,
    mtime: int | float = 0,
) -> None:
    """Stream one declared-size blob into a tar and verify every copied byte."""
    reader = _DigestingReader(source, size)
    target.addfile(_entry(name, size, mtime=mtime), reader)
    if reader.digest.hexdigest() != digest:
        raise SandboxCheckpointError(mismatch)


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


def export_checkpoint_archive(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
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

    metadata: dict[str, bytes] = {}
    small_blobs: dict[str, bytes] = {}
    large_blob_bytes: dict[str, int] = {}
    with (
        tarfile.open(fileobj=save_stream, mode="r|") as source,
        tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as target,
    ):
        for member in source:
            kind, name = _member_kind(member)
            if kind == "blob" and name in base_layers:
                continue
            if kind == "directory":
                _require_entry_space(target.offset, 0, max_bytes)
                _add_directory(target, member.name, mtime=member.mtime)
                continue
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
                    # Held back until the manifest says whether the image uses
                    # it: a classic image store's `docker save` also emits
                    # metadata blobs no manifest names, which a restore refuses.
                    if hashlib.sha256(body).hexdigest() != name:
                        raise SandboxCheckpointError("checkpoint blob does not match its digest")
                    small_blobs[name] = body
            else:
                assert name is not None
                _require_entry_space(target.offset, member.size, max_bytes)
                _copy_verified_blob(
                    target,
                    member.name,
                    handle,
                    member.size,
                    name,
                    "checkpoint blob does not match its digest",
                    mtime=member.mtime,
                )
                large_blob_bytes[name] = member.size
        manifests = _image_manifests(metadata, small_blobs)
        if len(manifests) != 1:
            raise SandboxCheckpointError("checkpoint archive must describe exactly one image")
        manifest_digest, manifest = manifests[0]
        config_digest = _descriptor_digest(manifest.get("config"))
        if config_digest not in small_blobs:
            raise SandboxCheckpointError("checkpoint archive is missing its image config")
        inherited, added = _split_new_layers(_manifest_layers(manifest), base_layers)
        used = {manifest_digest, config_digest, *added}
        if set(large_blob_bytes) - used:
            raise SandboxCheckpointError("checkpoint archive holds blobs its image does not use")
        # The index names the image manifest itself, so a nested index's
        # blob, like any other the image does not use, is left out.
        metadata["index.json"] = _direct_index(
            metadata["index.json"], manifest_digest, small_blobs[manifest_digest], manifest
        )
        written = [
            (f"blobs/sha256/{name}", small_blobs[name]) for name in sorted(used & set(small_blobs))
        ]
        for name, body in [*written, *sorted(metadata.items())]:
            _require_entry_space(target.offset, len(body), max_bytes)
            _add_bytes(target, name, body)
    if output.tell() > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
    kept_blob_bytes = {
        **large_blob_bytes,
        **{name: len(body) for name, body in small_blobs.items() if name in used},
    }
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


def _direct_index(
    original: bytes, manifest_digest: str, manifest_body: bytes, manifest: Mapping[str, Any]
) -> bytes:
    """An ``index.json`` naming the image manifest directly, keeping the image's name."""
    try:
        descriptors = json.loads(original).get("manifests") or [{}]
        annotations = descriptors[0].get("annotations") or {}
    except (ValueError, AttributeError, IndexError):
        annotations = {}
    descriptor: dict[str, Any] = {
        "mediaType": manifest.get("mediaType") or "application/vnd.oci.image.manifest.v1+json",
        "digest": f"sha256:{manifest_digest}",
        "size": len(manifest_body),
    }
    if isinstance(annotations, dict) and annotations:
        descriptor["annotations"] = annotations
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [descriptor],
        },
        separators=(",", ":"),
    ).encode()


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


def _image_names(reference: str) -> tuple[str, str]:
    """Return the fully qualified name and tag Docker records for ``reference``."""
    repository, _, tag = reference.rpartition(":")
    first = repository.split("/", 1)[0]
    if "/" not in repository:
        repository = f"docker.io/library/{repository}"
    elif "." not in first and ":" not in first and first != "localhost":
        repository = f"docker.io/{repository}"
    return f"{repository}:{tag}", tag


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


def prepare_restore_archive(  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
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
            _add_directory(target, directory)
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
            _require_entry_space(output.tell(), member.size, max_bytes)
            if member.size <= _MAX_METADATA_BYTES:
                body = handle.read()
                if len(body) != member.size:
                    raise SandboxCheckpointError("checkpoint archive entry is truncated")
                if hashlib.sha256(body).hexdigest() != name:
                    raise SandboxCheckpointError("checkpoint blob does not match its digest")
                small_blobs[name] = body
                _add_bytes(target, f"blobs/sha256/{name}", body)
            else:
                _copy_verified_blob(
                    target,
                    f"blobs/sha256/{name}",
                    handle,
                    member.size,
                    name,
                    "checkpoint blob does not match its digest",
                )
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
            _add_bytes(target, name, body)
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
    store: _CheckpointContentStore,
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
        projected += _entry_bytes(size)
    if projected > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as target:
        for directory in ("blobs", "blobs/sha256"):
            _add_directory(target, directory)

        _add_bytes(target, f"blobs/sha256/{manifest_digest}", manifest_body)
        _add_bytes(target, f"blobs/sha256/{config_digest}", config_body)
        for layer in added:
            with store.open(layer) as source:
                _copy_verified_blob(
                    target,
                    f"blobs/sha256/{layer}",
                    source,
                    layer_sizes[layer],
                    layer,
                    "content store blob does not match its digest",
                )
        _add_bytes(target, "index.json", index_body)
        _add_bytes(target, "manifest.json", docker_manifest)
        _add_bytes(target, "oci-layout", layout_body)
    if output.tell() > max_bytes:
        raise SandboxCheckpointError("sandbox checkpoint exceeds the archive size limit")
    return CheckpointArchive(
        manifest_digest=manifest_digest,
        config_digest=config_digest,
        base_layers=inherited,
        new_layers=added,
        new_layer_bytes=sum(layer_sizes.values()),
    )


__all__ = [
    "CheckpointArchive",
    "CheckpointContentUnavailable",
    "export_checkpoint_archive",
    "export_from_content_store",
    "prepare_restore_archive",
    "read_base_layers",
]
