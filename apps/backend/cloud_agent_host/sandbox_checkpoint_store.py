"""Checkpoint receipt models and durable host-local receipt storage."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final

from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.sandbox_restore import pullable_image

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger("cloud_agent_host.sandbox_checkpoints")

CHECKPOINT_FORMAT_VERSION: Final = 1
_DIGEST_RE: Final = re.compile(r"[0-9a-f]{64}")
_CHECKPOINT_ID_RE: Final = re.compile(r"[0-9a-f]{32}")


class SandboxCheckpointError(SandboxError):
    """A checkpoint could not be captured or its archive was malformed."""


class CheckpointArchiveInvalid(SandboxCheckpointError):
    """A stored archive is not what its record says; retrying cannot help."""


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


__all__ = [
    "CHECKPOINT_FORMAT_VERSION",
    "CheckpointArchiveInvalid",
    "CheckpointRestore",
    "SandboxCheckpointError",
    "SandboxCheckpointReceipt",
    "SandboxCheckpointStore",
]
