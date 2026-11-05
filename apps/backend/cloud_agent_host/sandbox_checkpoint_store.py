"""Checkpoint errors and durable host-local receipt storage."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import TYPE_CHECKING

from cloud_agent_host.atomic_file import atomic_replace_bytes
from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.sandbox_restore import SandboxCheckpointReceipt

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("cloud_agent_host.sandbox_checkpoints")


class SandboxCheckpointError(SandboxError):
    """A checkpoint could not be captured or its archive was malformed."""


class CheckpointArchiveInvalid(SandboxCheckpointError):
    """A stored archive is not what its record says; retrying cannot help."""


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
            # This was strict before the shared helper: a checkpoint receipt is
            # not reported until both its file and directory entry are durable.
            atomic_replace_bytes(target, payload, directory_sync="strict")

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


__all__ = [
    "CheckpointArchiveInvalid",
    "SandboxCheckpointError",
    "SandboxCheckpointStore",
]
