"""Copy a retained chat container's ``docker cp`` archive stream under its deadline and limit."""

from __future__ import annotations

import os
import time
from typing import IO, TYPE_CHECKING, NoReturn

from cloud_agent_host.chat_sandbox_models import ArchiveExportTooLarge
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    import selectors
    import subprocess

    from _typeshed import SupportsWrite


def pump_archive_export(
    process: subprocess.Popen[bytes],
    stdout: IO[bytes],
    selector: selectors.BaseSelector,
    destination: SupportsWrite[bytes],
    *,
    export_deadline: float,
    max_bytes: int,
    path: str,
) -> int:
    """Copy ``docker cp``'s output to ``destination`` until it ends; return the bytes copied."""
    total = 0
    while True:
        remaining = export_deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxError("chat sandbox archive export timed out")
        ready = selector.select(timeout=min(remaining, 1.0))
        if not ready:
            if process.poll() is None:
                continue
            chunk = os.read(stdout.fileno(), 1024 * 1024)
        else:
            chunk = os.read(stdout.fileno(), 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ArchiveExportTooLarge(f"chat sandbox archive for {path!r} exceeds its byte limit")
        if destination.write(chunk) != len(chunk):
            raise SandboxError("chat sandbox archive destination accepted a short write")
    return total


def raise_archive_export_failure(
    stderr: IO[bytes], *, path: str, container_id: str, total: int
) -> NoReturn:
    """Raise for a failed ``docker cp``: a path that is not there, or Docker's own error."""
    stderr.seek(0)
    detail = stderr.read(4096).decode("utf-8", "replace").strip()
    missing = f"Could not find the file {path} in container {container_id}"
    if total == 0 and detail in {
        missing,
        f"Error response from daemon: {missing}",
    }:
        raise FileNotFoundError(path)
    raise SandboxError(
        "chat sandbox archive export failed: " + (detail[:300] or "Docker cp failed")
    )


__all__ = ["pump_archive_export", "raise_archive_export_failure"]
