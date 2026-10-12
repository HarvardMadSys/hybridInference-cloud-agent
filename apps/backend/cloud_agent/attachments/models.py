"""Bounds and shapes for files a user attaches to one task."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.settings import integer_setting
from cloud_agent_shared.git.source_archive import DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Deliberately small. Bytes live in the control plane's own database rather
#: than an object store this service does not have, so the ceiling is what
#: keeps that decision defensible rather than merely convenient.
MAX_ATTACHMENT_FILE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_FILES = 50
MAX_ATTACHMENT_PATH_LENGTH = 512
MAX_CONTENT_TYPE_LENGTH = 255

#: What one conversation's ``/uploads`` may hold, every turn's files together.
#: The host rebuilds that directory from all of them as one archive and
#: refuses one over ``DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES``, which would fail that
#: turn and every later one on a fresh container; the difference leaves room
#: for the archive's own headers. Not admin settings, for the same reason: the
#: host's limit is not one either.
MAX_THREAD_ATTACHMENT_BYTES = DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES - 4 * 1024 * 1024
MAX_THREAD_ATTACHMENT_FILES = 200

#: Everything one owner's uploads keep in the database, staged or attached to a
#: conversation that still exists. Attached bytes stay until the conversation
#: is deleted, so the per-turn bounds alone would let one account grow the
#: shared database without end.
MAX_USER_ATTACHMENT_BYTES = 1024 * 1024 * 1024

#: Where the sandbox sees them. Outside the worktree on purpose: the publish
#: path stages the workspace with ``git add -A -N``, so an upload written into
#: it would be committed and appear in the draft pull request.
SANDBOX_UPLOAD_DIR = "/uploads"

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


class AttachmentError(ValueError):
    """An upload the owner can correct."""


class AttachmentLeaseLost(PermissionError):
    """A worker no longer authorized to read its conversation's uploads."""


class AttachmentLimits(BaseModel):
    """Advertise the server's upload bounds to clients."""

    max_files: int = Field(default_factory=lambda: attachment_max_files())
    max_file_bytes: int = Field(default_factory=lambda: attachment_max_file_bytes())
    max_total_bytes: int = Field(default_factory=lambda: attachment_max_total_bytes())
    max_path_length: int = MAX_ATTACHMENT_PATH_LENGTH
    max_thread_files: int = Field(
        default=MAX_THREAD_ATTACHMENT_FILES,
        description="Files one conversation's turns may hold together.",
    )
    max_thread_bytes: int = Field(
        default=MAX_THREAD_ATTACHMENT_BYTES,
        description="Bytes one conversation's turns may hold together.",
    )


def attachment_max_files() -> int:
    """Read the current staging and turn quota without caching an admin edit."""
    return integer_setting("attachments.max_files", MAX_ATTACHMENT_FILES)


def attachment_max_file_bytes() -> int:
    """Read the per-file upload limit; the schema retains its absolute ceiling."""
    return integer_setting("attachments.max_file_bytes", MAX_ATTACHMENT_FILE_BYTES)


def attachment_max_total_bytes() -> int:
    """Read the combined staging and turn byte quota."""
    return integer_setting("attachments.max_total_bytes", MAX_ATTACHMENT_TOTAL_BYTES)


def attachment_max_user_bytes() -> int:
    """Read the ceiling on everything one owner's uploads keep stored."""
    return integer_setting("attachments.max_user_bytes", MAX_USER_ATTACHMENT_BYTES)


def attachment_size_label(size: int) -> str:
    """Keep size-limit errors readable even for a quota below one megabyte."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):g} MB"
    if size >= 1024:
        return f"{size / 1024:g} KB"
    return f"{size} bytes"


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


def _folders(path: str) -> list[str]:
    """The folders a path sits in under ``/uploads``, outermost first."""
    parts = path.split("/")
    return ["/".join(parts[:depth]) for depth in range(1, len(parts))]


def check_paths(paths: list[str], *, held: Iterable[str] = ()) -> None:
    """Refuse a turn's paths when ``/uploads`` cannot hold them all at once.

    A turn names each path once, and no path may be both a file and a folder:
    not within the turn, and not against what the conversation's ``/uploads``
    already ``held``. A path already held is fine on its own — the newer upload
    replaces the older, as saving a file under an existing name does. A file
    cannot replace a folder, nor a folder a file, in a retained sandbox (the
    import refuses to), so that is refused here, before the turn exists.
    """
    adding: set[str] = set()
    for path in paths:
        if path in adding:
            raise AttachmentError(f"Two attached files have the same path: {path}")
        adding.add(path)
    files = adding | set(held)
    folders = {folder for path in files for folder in _folders(path)}
    for path in paths:
        clash = (
            path
            if path in folders
            else next((folder for folder in _folders(path) if folder in files), None)
        )
        if clash is not None:
            raise AttachmentError(
                f"{clash} cannot be both a file and a folder under {SANDBOX_UPLOAD_DIR}."
            )


def check_thread_total(rows: list[dict[str, Any]]) -> None:
    """Bound what a conversation's ``/uploads`` holds, every turn's files together.

    ``rows`` are what it would hold with a new turn's files in (see
    :func:`current_uploads`): a file attached again counts once, as the host
    imports it once.
    """
    if len(rows) > MAX_THREAD_ATTACHMENT_FILES:
        raise AttachmentError(
            f"A conversation can hold at most {MAX_THREAD_ATTACHMENT_FILES} uploaded "
            "files. Start a new chat to attach more."
        )
    total = sum(int(row["size_bytes"]) for row in rows)
    if total > MAX_THREAD_ATTACHMENT_BYTES:
        raise AttachmentError(
            "A conversation's uploaded files must total no more than "
            f"{attachment_size_label(MAX_THREAD_ATTACHMENT_BYTES)}. "
            "Start a new chat to attach more."
        )


def current_uploads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a conversation's ``/uploads`` holds: the newest upload at each path.

    ``rows`` are the conversation's uploads in the order its turns attached
    them. A later upload at a path replaces an earlier one there, exactly as
    the import into a retained sandbox overwrites it. A conversation from
    before file/folder clashes were refused can also hold a later upload that
    needs an earlier file's path as a folder, or the reverse; the later one
    replaces the earlier there too, as it would on disk. What is left never
    names a path twice or as both a file and a folder, which is what the
    host's upload archive requires. Returned in the order given.
    """
    later_files: set[str] = set()
    later_folders: set[str] = set()
    kept: list[dict[str, Any]] = []
    for row in reversed(rows):
        path = str(row["path"])
        above = _folders(path)
        if path not in later_files and path not in later_folders and later_files.isdisjoint(above):
            kept.append(row)
        # Replaced or not, it was written after everything older than it.
        later_files.add(path)
        later_folders.update(above)
    kept.reverse()
    return kept
