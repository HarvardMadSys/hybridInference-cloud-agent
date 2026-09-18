"""Bounds and shapes for files a user attaches to one task."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

#: Deliberately small. Bytes live in the control plane's own database rather
#: than an object store this service does not have, so the ceiling is what
#: keeps that decision defensible rather than merely convenient.
MAX_ATTACHMENT_FILE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_FILES = 50
MAX_ATTACHMENT_PATH_LENGTH = 512
MAX_CONTENT_TYPE_LENGTH = 255

#: Where the sandbox sees them. Outside the worktree on purpose: the publish
#: path stages the workspace with ``git add -A -N``, so an upload written into
#: it would be committed and appear in the draft pull request.
SANDBOX_UPLOAD_DIR = "/uploads"

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


class AttachmentError(ValueError):
    """An upload the owner can correct."""


class AttachmentLimits(BaseModel):
    """Advertise the server's upload bounds to clients."""

    max_files: int = MAX_ATTACHMENT_FILES
    max_file_bytes: int = MAX_ATTACHMENT_FILE_BYTES
    max_total_bytes: int = MAX_ATTACHMENT_TOTAL_BYTES
    max_path_length: int = MAX_ATTACHMENT_PATH_LENGTH


class AttachmentView(BaseModel):
    """One stored upload, without its bytes."""

    model_config = ConfigDict(extra="ignore")

    id: int
    path: str
    size_bytes: int
    content_type: str


class AttachmentRef(BaseModel):
    """What a turn retains about an upload it attached."""

    id: int
    path: str
    size_bytes: int = Field(ge=0)
    content_type: str = ""


def safe_attachment_path(path: str) -> str:
    """Return the path a file may take under ``/uploads``, or refuse it.

    The same predicate the browser applies before uploading and the same one
    repository content paths go through: a path arriving from a directory
    upload is no more trusted than one arriving from a repository. Refusing is
    the whole behaviour — a path is never rewritten into a safe one, because
    that hides the problem instead of reporting it.
    """
    cleaned = (path or "").strip().replace("\\", "/")
    if not cleaned or len(cleaned) > MAX_ATTACHMENT_PATH_LENGTH:
        raise AttachmentError("An upload path must be 1 to 512 characters.")
    if cleaned.startswith("/") or _DRIVE_PREFIX.match(cleaned):
        raise AttachmentError("An upload path must be relative.")
    for part in cleaned.split("/"):
        if not part or part in {".", ".."} or part.casefold() == ".git" or "\x00" in part:
            raise AttachmentError(f"Unusable upload path: {path!r}")
    return cleaned
