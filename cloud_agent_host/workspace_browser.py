"""Safe path helpers for the owner-facing retained workspace browser."""

from __future__ import annotations

from pathlib import PurePosixPath

MAX_FILE_PREVIEW_BYTES = 512 * 1024


class WorkspacePathError(ValueError):
    """A workspace path is not a safe canonical repository-relative path."""


def normalize_workspace_path(raw: str | None) -> str:
    """Validate and return a canonical POSIX path, or ``""`` for the root."""
    if raw is None or raw == "":
        return ""
    if (
        len(raw) > 4096
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("/")
        or (len(raw) >= 3 and raw[0].isalpha() and raw[1:3] == ":/")
    ):
        raise WorkspacePathError("path must be a safe repository-relative path")
    parts = raw.split("/")
    if any(not part or part in {".", ".."} or part.casefold() == ".git" for part in parts):
        raise WorkspacePathError("path must be a safe repository-relative path")
    path = PurePosixPath(*parts).as_posix()
    if path != raw:
        raise WorkspacePathError("path must use canonical POSIX encoding")
    return path
