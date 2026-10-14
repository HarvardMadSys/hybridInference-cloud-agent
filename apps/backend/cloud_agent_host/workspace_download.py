"""Build user workspace downloads without the workspace's Git metadata."""

from __future__ import annotations

import gzip
import tarfile
from pathlib import PurePosixPath
from typing import BinaryIO


def write_workspace_download(source: BinaryIO, destination: BinaryIO) -> None:
    """Filter a completed Docker tar into a gzipped tar without extracting it.

    Only entries named ``.git`` and their descendants are omitted, including
    nested repositories and worktree/submodule metadata files. Other hidden
    files, permissions, and links are preserved. Internal sandbox exports
    remain unchanged and retain the Git data needed for workspace recovery.
    """
    with (
        gzip.GzipFile(fileobj=destination, mode="wb", compresslevel=6, mtime=0) as packed,
        tarfile.open(fileobj=source, mode="r|") as original,
        tarfile.open(fileobj=packed, mode="w|") as archive,
    ):
        for entry in original:
            if ".git" in PurePosixPath(entry.name).parts:
                continue
            # A hard link to omitted Git data would otherwise be unextractable.
            if entry.islnk() and ".git" in PurePosixPath(entry.linkname).parts:
                continue
            if entry.isfile():
                content = original.extractfile(entry)
                assert content is not None
                with content:
                    archive.addfile(entry, content)
            else:
                archive.addfile(entry)
