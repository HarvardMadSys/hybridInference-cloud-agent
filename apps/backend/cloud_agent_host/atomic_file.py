"""Atomic replacement for host-private durable files."""

from __future__ import annotations

import errno
import os
import tempfile
from contextlib import suppress
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

DirectorySync = Literal["strict", "best_effort"]


def atomic_replace_bytes(
    path: Path,
    payload: bytes,
    *,
    directory_sync: DirectorySync,
) -> None:
    """Replace ``path`` with durable ``payload`` from a private temporary file.

    The caller creates and protects the parent directory and holds any lock
    needed for a read-modify-write. ``strict`` propagates a directory-sync
    failure after the replacement is visible; it cannot roll that replacement
    back. ``best_effort`` keeps the published file and suppresses that failure.
    """
    if directory_sync not in {"strict", "best_effort"}:
        raise ValueError("directory sync policy must be strict or best_effort")

    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        try:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "atomic file write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        else:
            os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise

    try:
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if directory_sync == "strict":
            raise


__all__ = ["DirectorySync", "atomic_replace_bytes"]
