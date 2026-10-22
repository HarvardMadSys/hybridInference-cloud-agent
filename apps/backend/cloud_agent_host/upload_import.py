"""Import attachment deltas against host-private, confirmed container receipts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from typing import TYPE_CHECKING, Any

from cloud_agent_host.enrollment import credential_path
from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.git.source_archive import (
    DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES,
    upload_archive_size,
    write_upload_archive,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.claimed_job import ClaimedJob
    from cloud_agent_host.control_plane_client import ControlPlane


def _read(path: Path, container_id: str) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(receipt, dict) or receipt.get("container_id") != container_id:
        return {}
    files = receipt.get("files")
    if not isinstance(files, dict) or any(
        not isinstance(path, str) or not isinstance(value, int) for path, value in files.items()
    ):
        return {}
    return receipt


def _save(path: Path, receipt: dict[str, Any]) -> None:
    """Persist the invalidation before mutation and confirmation after import."""
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(receipt, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def import_uploads(
    *,
    job: ClaimedJob,
    control: ControlPlane,
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    fresh_container: bool,
    container_id: str,
) -> int:
    """Reconcile the whole manifest, importing only bodies not confirmed on this container.

    Receipts live outside sandbox mounts. The container's exact acquired ID
    fences reuse after replacement, and a process-shared lock serializes
    sibling imports. Invalidating before Docker writes makes interrupted
    imports conservative: even a later turn without uploads retries the full
    manifest. A missing receipt after a host restart is equally safe.
    """
    if job.thread_attachment_count <= 0:
        return 0
    root = credential_path().parent / "upload-receipts"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(spec.container_name.encode()).hexdigest()
    path = root / f"{key}.json"
    with (root / f"{key}.lock").open("a+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = {} if fresh_container else _read(path, container_id)
        if (
            receipt.get("thread_id") == job.thread_id
            and receipt.get("count") == job.thread_attachment_count
            and job.turn_attachment_count <= 0
        ):
            return 0
        entries = control.list_attachments()
        if (
            upload_archive_size((str(entry["path"]), int(entry["size_bytes"])) for entry in entries)
            > DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES
        ):
            raise SandboxError("attached files exceed the upload import size limit")
        wanted = {str(entry["path"]): int(entry["id"]) for entry in entries}
        confirmed = receipt.get("files", {})
        missing = [
            entry for entry in entries if confirmed.get(str(entry["path"])) != int(entry["id"])
        ]
        if missing:
            with tempfile.TemporaryFile() as archive:
                write_upload_archive(
                    (control.read_attachment(int(entry["id"])) for entry in missing), archive
                )
                archive.seek(0)
                _save(path, {"container_id": container_id, "files": {}})
                manager.import_archive(spec, "/uploads", archive)
        _save(
            path,
            {
                "container_id": container_id,
                "thread_id": job.thread_id,
                "count": job.thread_attachment_count,
                "files": wanted,
            },
        )
        return len(missing)
