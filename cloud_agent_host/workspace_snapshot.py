"""Bounded, symlink-safe snapshots of files changed by an agent job."""

from __future__ import annotations

import errno
import json
import os
import pathlib
import re
import stat
from typing import TYPE_CHECKING, Any

from cloud_agent_host.patch_gate import MAX_CHANGED_FILES, validate_patch

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import RetainedChatBackend

SNAPSHOT_VERSION = 1
DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_FILES = MAX_CHANGED_FILES
DEFAULT_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class _UnsafePath(ValueError):
    """The requested path cannot be read without leaving the worktree."""


class _SymlinkPath(ValueError):
    """The requested path contains a symlink."""

    def __init__(self, name: str, *, size: int) -> None:
        super().__init__(name)
        self.size = size


def _path_parts(path: str) -> tuple[str, ...]:
    raw_parts = path.split("/")
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or re.match(r"^[A-Za-z]:/", path)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise _UnsafePath(path)
    candidate = pathlib.PurePosixPath(path)
    parts = candidate.parts
    if candidate.is_absolute():
        raise _UnsafePath(path)
    if any(part.casefold() == ".git" for part in parts):
        raise _UnsafePath(path)
    return parts


def _symlink_size(parent_fd: int, name: str) -> int | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_size if stat.S_ISLNK(info.st_mode) else None


def _open_changed_file(root_fd: int, parts: tuple[str, ...]) -> tuple[int, os.stat_result]:
    """Open one regular file beneath ``root_fd`` without following links."""
    directory_fd = os.dup(root_fd)
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                symlink_size = _symlink_size(directory_fd, component)
                if exc.errno in {errno.ELOOP, errno.ENOTDIR} and symlink_size is not None:
                    raise _SymlinkPath(component, size=symlink_size) from exc
                raise
            os.close(directory_fd)
            directory_fd = next_fd

        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        except OSError as exc:
            symlink_size = _symlink_size(directory_fd, parts[-1])
            if exc.errno in {errno.ELOOP, errno.ENOTDIR} and symlink_size is not None:
                raise _SymlinkPath(parts[-1], size=symlink_size) from exc
            raise
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            raise _UnsafePath("not a regular file")
        return file_fd, info
    finally:
        os.close(directory_fd)


def _added_paths(patch: str) -> set[str]:
    """Return paths represented as newly added in a git patch."""
    added: set[str] = set()
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    for section in sections:
        if not any(
            marker in section
            for marker in ("new file mode ", "\n--- /dev/null\n", "\nrename to ", "\ncopy to ")
        ):
            continue
        added.update(validate_patch(section).changed_files)
    return added


def _omitted_entry(path: str, status: str, size: int, reason: str) -> dict[str, Any]:
    return {"path": path, "status": status, "size": size, "omitted": reason}


def _encode_snapshot(
    files: list[dict[str, Any]],
    *,
    truncated: bool,
    skipped: int,
    max_file_bytes: int,
    max_total_bytes: int,
    max_files: int,
    max_artifact_bytes: int,
) -> str:
    """Apply the common artifact envelope and final serialized-size bound."""
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "files": files,
        "truncated": truncated,
        "limits": {
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "max_files": max_files,
            "max_artifact_bytes": max_artifact_bytes,
        },
    }
    if skipped:
        snapshot["skipped"] = skipped
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= max_artifact_bytes:
        return encoded

    snapshot["truncated"] = True
    for item in reversed(files):
        if "content" in item:
            item.pop("content")
            item["omitted"] = "artifact_limit"
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    while files and len(encoded.encode("utf-8")) > max_artifact_bytes:
        files.pop()
        skipped += 1
        snapshot["skipped"] = skipped
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_artifact_bytes:
        raise ValueError("workspace snapshot metadata exceeds the artifact limit")
    return encoded


def build_retained_workspace_snapshot(
    backend: RetainedChatBackend,
    patch: str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> str:
    """Build the same changed-file overlay through one retained-container exec."""
    if min(max_file_bytes, max_total_bytes, max_files, max_artifact_bytes) < 0:
        raise ValueError("workspace snapshot limits must be non-negative")

    changed_paths = validate_patch(patch).changed_files if patch.strip() else []
    added_paths = _added_paths(patch)
    selected_paths = changed_paths[:max_files]
    requested = [
        {
            "path": path,
            "status": "added" if path in added_paths else "modified",
        }
        for path in selected_paths
    ]
    request = json.dumps(
        {
            "paths": requested,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    result = backend.manager.exec_capture(
        backend.spec,
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            _RETAINED_SNAPSHOT_PROGRAM,
            "/workspace",
        ],
        workdir="/",
        timeout_s=120,
        input_bytes=request,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("retained workspace snapshot failed: " + detail[-300:])

    # The fixed in-container reader is bounded by the requested content
    # limits. Keep an independent capture bound before parsing in case the
    # sandbox image or Python executable is unexpectedly replaced.
    capture_limit = max_artifact_bytes + (max_total_bytes * 6) + (max_files * 1024) + 65_536
    if len(result.stdout) > capture_limit:
        raise ValueError("retained workspace snapshot output exceeds its capture limit")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("retained workspace snapshot returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("retained workspace snapshot returned invalid data")
    files = payload.get("files")
    truncated = payload.get("truncated")
    skipped = payload.get("skipped")
    if (
        not isinstance(files, list)
        or len(files) > len(selected_paths)
        or not isinstance(truncated, bool)
        or not isinstance(skipped, int)
        or isinstance(skipped, bool)
        or skipped < 0
    ):
        raise ValueError("retained workspace snapshot returned invalid data")

    expected_paths = set(selected_paths)
    observed_paths: set[str] = set()
    content_bytes = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("retained workspace snapshot returned invalid file data")
        path = item.get("path")
        status = item.get("status")
        size = item.get("size")
        content = item.get("content")
        omitted = item.get("omitted")
        if (
            not isinstance(path, str)
            or path not in expected_paths
            or path in observed_paths
            or status not in {"added", "modified", "deleted"}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or (content is not None and not isinstance(content, str))
            or (omitted is not None and not isinstance(omitted, str))
            or (content is not None and omitted is not None)
        ):
            raise ValueError("retained workspace snapshot returned invalid file data")
        if status == "deleted" and (size != 0 or content is not None or omitted is not None):
            raise ValueError("retained workspace snapshot returned invalid deletion data")
        observed_paths.add(path)
        if content is not None:
            encoded_size = len(content.encode("utf-8"))
            if encoded_size != size or encoded_size > max_file_bytes:
                raise ValueError("retained workspace snapshot returned invalid file size")
            content_bytes += encoded_size
    if (
        content_bytes > max_total_bytes
        or skipped > len(selected_paths)
        or len(files) + skipped != len(selected_paths)
    ):
        raise ValueError("retained workspace snapshot exceeded its content limits")

    return _encode_snapshot(
        files,
        truncated=truncated or len(changed_paths) > len(selected_paths),
        skipped=skipped,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_files=max_files,
        max_artifact_bytes=max_artifact_bytes,
    )


# This fixed program executes as the sandbox user inside the retained
# container. It mirrors the descriptor-relative reader above, but returns all
# changed files in one bounded Docker exec so a 200-file patch does not incur
# 200 separate lifecycle round trips. The trusted side validates both its
# input paths and its output before storing the artifact.
_RETAINED_SNAPSHOT_PROGRAM = r"""
import errno
import json
import os
import re
import stat
import sys

root_path = sys.argv[1]
request = json.load(sys.stdin)
paths = request["paths"]
max_file_bytes = int(request["max_file_bytes"])
max_total_bytes = int(request["max_total_bytes"])

def path_parts(path):
    raw = path.split("/")
    if (
        not path or "\0" in path or "\\" in path or re.match(r"^[A-Za-z]:/", path)
        or path.startswith("/") or any(part in ("", ".", "..") for part in raw)
        or any(part.casefold() == ".git" for part in raw)
    ):
        raise ValueError(path)
    return tuple(raw)

def symlink_size(parent_fd, name):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_size if stat.S_ISLNK(info.st_mode) else None

def open_changed_file(root_fd, parts):
    directory_fd = os.dup(root_fd)
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                size = symlink_size(directory_fd, component)
                if exc.errno in (errno.ELOOP, errno.ENOTDIR) and size is not None:
                    raise RuntimeError("symlink", size) from exc
                raise
            os.close(directory_fd)
            directory_fd = next_fd
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        except OSError as exc:
            size = symlink_size(directory_fd, parts[-1])
            if exc.errno in (errno.ELOOP, errno.ENOTDIR) and size is not None:
                raise RuntimeError("symlink", size) from exc
            raise
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            raise ValueError("not a regular file")
        return file_fd, info
    finally:
        os.close(directory_fd)

files = []
read_bytes = 0
skipped = 0
truncated = False
root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
root_fd = os.open(root_path, root_flags)
try:
    for requested in paths:
        path = requested["path"]
        status = requested["status"]
        try:
            parts = path_parts(path)
        except ValueError:
            skipped += 1
            truncated = True
            continue
        try:
            file_fd, info = open_changed_file(root_fd, parts)
        except FileNotFoundError:
            files.append({"path": path, "status": "deleted", "size": 0})
            continue
        except RuntimeError as exc:
            if exc.args and exc.args[0] == "symlink":
                files.append({
                    "path": path, "status": status, "size": int(exc.args[1]),
                    "omitted": "symlink",
                })
            else:
                files.append({
                    "path": path, "status": status, "size": 0, "omitted": "unreadable",
                })
            truncated = True
            continue
        except (OSError, ValueError):
            files.append({
                "path": path, "status": status, "size": 0, "omitted": "unreadable",
            })
            truncated = True
            continue

        try:
            size = info.st_size
            if size > max_file_bytes:
                files.append({
                    "path": path, "status": status, "size": size, "omitted": "too_large",
                })
                truncated = True
                continue
            remaining = max_total_bytes - read_bytes
            if size > remaining:
                files.append({
                    "path": path, "status": status, "size": size, "omitted": "total_limit",
                })
                truncated = True
                continue
            data = b""
            read_limit = min(max_file_bytes, remaining)
            while len(data) < read_limit:
                chunk = os.read(file_fd, min(65536, read_limit - len(data)))
                if not chunk:
                    break
                data += chunk
            read_bytes += len(data)
            final_size = os.fstat(file_fd).st_size
            if final_size > len(data):
                reason = "too_large" if final_size > max_file_bytes else "total_limit"
                files.append({
                    "path": path, "status": status, "size": final_size, "omitted": reason,
                })
                truncated = True
                continue
            if b"\0" in data:
                files.append({
                    "path": path, "status": status, "size": final_size, "omitted": "binary",
                })
                truncated = True
                continue
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                files.append({
                    "path": path, "status": status, "size": final_size, "omitted": "binary",
                })
                truncated = True
                continue
            files.append({
                "path": path, "status": status, "size": final_size, "content": content,
            })
        finally:
            os.close(file_fd)
finally:
    os.close(root_fd)

print(json.dumps(
    {"files": files, "truncated": truncated, "skipped": skipped},
    ensure_ascii=False,
    separators=(",", ":"),
))
"""


def build_workspace_snapshot(
    workdir: str,
    patch: str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> str:
    """Build a bounded JSON overlay for files named by an agent's patch.

    Paths are opened component-by-component relative to a descriptor for the
    worktree. No symlink is followed, including one in an intermediate
    directory, and special files are never read.
    """
    if min(max_file_bytes, max_total_bytes, max_files, max_artifact_bytes) < 0:
        raise ValueError("workspace snapshot limits must be non-negative")

    changed_paths = validate_patch(patch).changed_files if patch.strip() else []
    added_paths = _added_paths(patch)
    selected_paths = changed_paths[:max_files]
    truncated = len(changed_paths) > len(selected_paths)
    files: list[dict[str, Any]] = []
    read_bytes = 0
    skipped = 0

    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(workdir, root_flags)
    try:
        for path in selected_paths:
            try:
                parts = _path_parts(path)
            except _UnsafePath:
                skipped += 1
                truncated = True
                continue

            status = "added" if path in added_paths else "modified"
            try:
                file_fd, info = _open_changed_file(root_fd, parts)
            except FileNotFoundError:
                files.append({"path": path, "status": "deleted", "size": 0})
                continue
            except _SymlinkPath as exc:
                files.append(_omitted_entry(path, status, exc.size, "symlink"))
                truncated = True
                continue
            except (OSError, _UnsafePath):
                files.append(_omitted_entry(path, status, 0, "unreadable"))
                truncated = True
                continue

            size = info.st_size
            try:
                if size > max_file_bytes:
                    files.append(_omitted_entry(path, status, size, "too_large"))
                    truncated = True
                    continue
                remaining_total = max_total_bytes - read_bytes
                if size > remaining_total:
                    files.append(_omitted_entry(path, status, size, "total_limit"))
                    truncated = True
                    continue

                data = b""
                read_limit = min(max_file_bytes, remaining_total)
                while len(data) < read_limit:
                    chunk = os.read(file_fd, min(64 * 1024, read_limit - len(data)))
                    if not chunk:
                        break
                    data += chunk
                read_bytes += len(data)
                final_size = os.fstat(file_fd).st_size
                if final_size > len(data):
                    reason = "too_large" if final_size > max_file_bytes else "total_limit"
                    files.append(_omitted_entry(path, status, final_size, reason))
                    truncated = True
                    continue
                if b"\x00" in data:
                    files.append(_omitted_entry(path, status, final_size, "binary"))
                    truncated = True
                    continue
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    files.append(_omitted_entry(path, status, final_size, "binary"))
                    truncated = True
                    continue
                files.append(
                    {"path": path, "status": status, "size": final_size, "content": content}
                )
            finally:
                os.close(file_fd)
    finally:
        os.close(root_fd)

    return _encode_snapshot(
        files,
        truncated=truncated,
        skipped=skipped,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_files=max_files,
        max_artifact_bytes=max_artifact_bytes,
    )


__all__ = [
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "SNAPSHOT_VERSION",
    "build_retained_workspace_snapshot",
    "build_workspace_snapshot",
]
