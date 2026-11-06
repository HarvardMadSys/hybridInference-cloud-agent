"""Bounded workspace file I/O through a retained container's exec boundary.

The sandbox manager cannot resolve a host path for retained chats. These helpers run a
fixed, unprivileged program inside the exact labelled container. Request data
is passed as argv/stdin, never interpolated into code or a shell command.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cloud_agent_host.container.chat_sandbox import CHAT_WORKDIR
from cloud_agent_host.container.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.container.retained_backend import RetainedChatBackend
    from cloud_agent_shared.operation_profiling import StageObserver


class RetainedWorkspaceError(SandboxError):
    """A bounded owner-facing workspace failure."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


_READ_PROGRAM = r"""
import base64, json, os, stat, sys

root_path, relative, max_raw, entries_raw, *mode = sys.argv[1:]
max_bytes = int(max_raw)
max_entries = int(entries_raw)
# "images": answer only a passive raster image, under its own larger bound.
images_only = mode == ["images"]

def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

def fail(code, message):
    emit({"error": message})
    raise SystemExit(code)

parts = [] if not relative else relative.split("/")
if any(not part or part in (".", "..") for part in parts):
    fail(40, "workspace path is invalid")

opened = []
try:
    parent_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
    opened.append(parent_fd)
    for part in parts[:-1]:
        try:
            parent_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError:
            fail(40, "workspace path crosses an invalid directory")
        opened.append(parent_fd)
    name = parts[-1] if parts else None
    try:
        info = os.fstat(parent_fd) if name is None else os.stat(
            name, dir_fd=parent_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        fail(44, "workspace path does not exist")
    except OSError:
        fail(49, "workspace path cannot be read")

    if stat.S_ISLNK(info.st_mode):
        emit({
            "path": relative, "kind": "symlink", "size": info.st_size,
            "writable": False, "source": "workspace", "omitted_reason": "symlink",
        })
    elif images_only and not stat.S_ISREG(info.st_mode):
        fail(49, "workspace path is not an image")
    elif stat.S_ISDIR(info.st_mode):
        try:
            directory_fd = parent_fd if name is None else os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError:
            fail(49, "workspace directory cannot be read")
        if directory_fd != parent_fd:
            opened.append(directory_fd)
        children = []
        omitted = False
        try:
            names = os.listdir(directory_fd)
        except OSError:
            fail(49, "workspace directory cannot be read")
        for child_name in names:
            if child_name == ".git":
                continue
            try:
                child_name.encode("utf-8")
            except UnicodeEncodeError:
                # Not UTF-8: no reply can name it, nor a request name it back.
                omitted = True
                continue
            try:
                child = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            children.append((child_name, child))
        children.sort(key=lambda item: (not stat.S_ISDIR(item[1].st_mode), item[0].casefold()))
        entries = []
        for child_name, child in children[:max_entries]:
            child_path = relative + "/" + child_name if relative else child_name
            kind = (
                "symlink" if stat.S_ISLNK(child.st_mode)
                else "directory" if stat.S_ISDIR(child.st_mode)
                else "file"
            )
            entries.append({
                "name": child_name,
                "path": child_path,
                "kind": kind,
                "size": None if kind == "directory" else child.st_size,
                "binary": False,
                "truncated": False,
                "status": None,
                "omitted_reason": "symlink" if kind == "symlink" else None,
            })
        emit({
            "path": relative,
            "kind": "directory",
            "entries": entries,
            # Some entries are not listed: past the limit, or unnameable.
            "truncated": omitted or len(children) > max_entries,
            "writable": True,
            "source": "workspace",
        })
    elif not stat.S_ISREG(info.st_mode):
        fail(49, "workspace path is not a regular file")
    elif info.st_size > max_bytes:
        emit({
            "path": relative, "kind": "file", "content": None,
            "size": info.st_size, "binary": False, "truncated": True,
            "writable": False, "source": "workspace", "omitted_reason": "too_large",
        })
    else:
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError:
            fail(49, "workspace file cannot be read")
        opened.append(file_fd)
        opened_info = os.fstat(file_fd)
        if not stat.S_ISREG(opened_info.st_mode):
            fail(49, "workspace path is not a regular file")
        chunks = []
        size = 0
        while size <= max_bytes:
            chunk = os.read(file_fd, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            emit({
                "path": relative, "kind": "file", "content": None,
                "size": opened_info.st_size, "binary": False, "truncated": True,
                "writable": False, "source": "workspace", "omitted_reason": "too_large",
            })
        else:
            binary = b"\x00" in raw
            content = None
            if not binary:
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    binary = True
            # Sniff a small set of passive raster formats. Repository HTML
            # and SVG are never returned as an active preview media type.
            media_type = None
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                media_type = "image/png"
            elif raw.startswith(b"\xff\xd8\xff"):
                media_type = "image/jpeg"
            elif raw.startswith((b"GIF87a", b"GIF89a")):
                media_type = "image/gif"
            elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                media_type = "image/webp"
            if images_only and media_type is None:
                # The larger bound is for images; anything else answers nothing.
                emit({
                    "path": relative, "kind": "file", "content": None,
                    "size": len(raw), "binary": binary, "truncated": False,
                    "writable": False, "source": "workspace",
                    "omitted_reason": "not_an_image",
                })
                raise SystemExit(0)
            emit({
                "path": relative, "kind": "file", "content": content,
                "content_base64": base64.b64encode(raw).decode("ascii")
                    if binary or media_type else None,
                "media_type": media_type,
                "size": len(raw), "binary": binary, "truncated": False,
                "writable": not binary, "source": "workspace",
                "omitted_reason": "binary" if binary else None,
            })
finally:
    for fd in reversed(opened):
        try:
            os.close(fd)
        except OSError:
            pass
"""


#: Where agents and their browser tools save screenshots (CLOUD_AGENT_ARTIFACTS_DIR).
ARTIFACTS_RELATIVE = ".cloud-agent-artifacts"
#: How large an image the image read answers; the text preview stays smaller.
IMAGE_MAX_BYTES = 10 * 1024 * 1024
#: How many images one listing answers, newest first, and how many entries it
#: looks at to find them.
ARTIFACT_IMAGES_MAX = 200
ARTIFACT_SCAN_MAX = 5000

_IMAGES_PROGRAM = r"""
import json, os, stat, sys

root_path, artifacts, since_raw, max_raw, scan_raw = sys.argv[1:]
since = float(since_raw)
max_images = int(max_raw)
max_scan = int(scan_raw)
suffixes = (".png", ".jpg", ".jpeg", ".gif", ".webp")
images = []
scanned = 0
truncated = False

def walk(directory_fd, prefix, depth):
    global scanned, truncated
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError:
        return
    for name in names:
        if scanned >= max_scan:
            truncated = True
            return
        scanned += 1
        try:
            name.encode("utf-8")
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except (OSError, UnicodeEncodeError):
            continue
        path = prefix + "/" + name
        if stat.S_ISDIR(info.st_mode) and depth < 8:
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory_fd)
            except OSError:
                continue
            try:
                walk(child, path, depth + 1)
            finally:
                os.close(child)
        elif (stat.S_ISREG(info.st_mode) and name.lower().endswith(suffixes)
              and info.st_mtime >= since):
            images.append({"path": path, "size": info.st_size, "modified_at": info.st_mtime})

try:
    root = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
except OSError:
    print(json.dumps({"error": "workspace cannot be read"}))
    raise SystemExit(49)
try:
    try:
        top = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    except OSError:
        top = None
    if top is not None:
        try:
            walk(top, artifacts, 0)
        finally:
            os.close(top)
finally:
    os.close(root)
images.sort(key=lambda image: (-image["modified_at"], image["path"]))
if len(images) > max_images:
    truncated = True
print(json.dumps({"images": images[:max_images], "truncated": truncated},
                 ensure_ascii=False, separators=(",", ":")))
"""


_WRITE_PROGRAM = r"""
import errno, os, stat, sys

root_path, relative, max_raw = sys.argv[1:]
max_bytes = int(max_raw)
# Fork siblings use distinct UIDs and one shared GID, so every owner-authored
# file must remain writable by the whole chat group.
os.umask(0o002)
create_mode = 0o664
no_room = (errno.ENOSPC, errno.EDQUOT, errno.EFBIG)
parts = [] if not relative else relative.split("/")
if not parts or any(not part or part in (".", "..") for part in parts):
    raise SystemExit(40)
raw = sys.stdin.buffer.read(max_bytes + 1)
if len(raw) > max_bytes:
    raise SystemExit(43)
opened = []
try:
    parent_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
    opened.append(parent_fd)
    for part in parts[:-1]:
        try:
            parent_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError:
            raise SystemExit(40)
        opened.append(parent_fd)
    try:
        existing = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    except OSError:
        raise SystemExit(49)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise SystemExit(49)
    # No O_TRUNC: the old content stays until the new content has room.
    # O_NONBLOCK keeps a FIFO swapped in since the check from blocking the
    # open (the fstat below refuses it), and O_EXCL makes a file this program
    # creates one it may remove again.
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    if existing is None:
        flags |= os.O_EXCL
    try:
        file_fd = os.open(parts[-1], flags, create_mode, dir_fd=parent_fd)
    except OSError as exc:
        # No room to create the file, or to copy it up out of an image layer,
        # changes nothing either.
        raise SystemExit(47 if exc.errno in no_room else 49)
    opened.append(file_fd)
    original = os.fstat(file_fd)
    if not stat.S_ISREG(original.st_mode):
        raise SystemExit(49)
    # Reserve the new content's blocks before overwriting any old byte, so a
    # full disk or quota leaves the file exactly as it was.
    try:
        if raw:
            os.posix_fallocate(file_fd, 0, len(raw))
    except OSError as exc:
        try:
            if existing is None:
                os.unlink(parts[-1], dir_fd=parent_fd)
            else:
                os.ftruncate(file_fd, original.st_size)
        except OSError:
            pass
        raise SystemExit(47 if exc.errno in no_room else 49)
    view = memoryview(raw)
    offset = 0
    try:
        while offset < len(raw):
            written = os.pwrite(file_fd, view[offset:], offset)
            if written <= 0:
                raise SystemExit(49)
            offset += written
        os.ftruncate(file_fd, len(raw))
        os.fsync(file_fd)
    except OSError as exc:
        # Reserved blocks need not cover an overwrite where blocks are
        # shared (copy-on-write, reflinked copies), so room can still run
        # out here, after old bytes were replaced.
        if exc.errno in no_room:
            raise SystemExit(48)
        raise
finally:
    for fd in reversed(opened):
        try:
            os.close(fd)
        except OSError:
            pass
"""


def _error_from_result(returncode: int, stdout: bytes, stderr: bytes) -> RetainedWorkspaceError:
    messages = {
        40: (400, "workspace path crosses an invalid directory"),
        43: (413, "file exceeds the workspace write limit"),
        44: (404, "workspace path does not exist"),
        47: (507, "workspace has no room for this file; it was not changed"),
        48: (507, "workspace ran out of room while writing this file; it may be incomplete"),
        49: (409, "workspace path cannot be accessed"),
    }
    status, message = messages.get(returncode, (502, "retained workspace operation failed"))
    try:
        body = json.loads(stdout.decode("utf-8"))
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            message = body["error"]
    except (UnicodeDecodeError, json.JSONDecodeError):
        detail = stderr.decode("utf-8", "replace").strip()
        if detail and status == 502:
            message = detail[:300]
    return RetainedWorkspaceError(status, message)


def read_retained_file(
    backend: RetainedChatBackend,
    relative: str,
    *,
    max_preview_bytes: int,
    max_directory_entries: int,
    observer: StageObserver | None = None,
) -> dict[str, Any]:
    """Read one normalized path without following a repository symlink.

    ``observer`` times the exec that performs the read, so a measured file
    request can attribute the container round trip separately from the sandbox manager's
    own admission and network work. Nothing about the read changes: the helper
    program, its arguments and its bounds are exactly what they were.
    """
    result = backend.manager.exec_capture(
        backend.spec,
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            _READ_PROGRAM,
            CHAT_WORKDIR,
            relative,
            str(max_preview_bytes),
            str(max_directory_entries),
        ],
        # Never import repository-controlled modules while executing this
        # trusted sandbox manager helper. The explicit root argument still addresses
        # the workspace, while isolated Python from / keeps sys.path clean.
        workdir="/",
        timeout_s=60,
        observer=observer,
    )
    if result.returncode != 0:
        raise _error_from_result(result.returncode, result.stdout, result.stderr)
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetainedWorkspaceError(502, "retained workspace returned invalid data") from exc
    if not isinstance(payload, dict) or "kind" not in payload:
        raise RetainedWorkspaceError(502, "retained workspace returned invalid data")
    return payload


def read_retained_image(
    backend: RetainedChatBackend,
    relative: str,
    *,
    observer: StageObserver | None = None,
) -> dict[str, Any]:
    """Read one PNG, JPEG, GIF or WebP file, up to :data:`IMAGE_MAX_BYTES`.

    The text preview's bound is far smaller than a full-page screenshot, so an
    image has its own. The larger bound buys nothing else: a file that is not
    one of those formats, by its bytes rather than its name, answers no content.
    """
    result = backend.manager.exec_capture(
        backend.spec,
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            _READ_PROGRAM,
            CHAT_WORKDIR,
            relative,
            str(IMAGE_MAX_BYTES),
            "1",
            "images",
        ],
        workdir="/",
        timeout_s=60,
        observer=observer,
    )
    if result.returncode != 0:
        raise _error_from_result(result.returncode, result.stdout, result.stderr)
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetainedWorkspaceError(502, "retained workspace returned invalid data") from exc
    if not isinstance(payload, dict) or payload.get("kind") not in {"file", "symlink"}:
        raise RetainedWorkspaceError(502, "retained workspace returned invalid data")
    return payload


def list_retained_images(backend: RetainedChatBackend, *, since: float) -> dict[str, Any]:
    """The images under the artifacts directory modified at or after ``since``, newest first.

    ``since`` is a Unix time. At most :data:`ARTIFACT_IMAGES_MAX` are answered,
    from at most :data:`ARTIFACT_SCAN_MAX` entries; ``truncated`` says either
    bound was reached. A workspace without the directory has no images.
    """
    result = backend.manager.exec_capture(
        backend.spec,
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            _IMAGES_PROGRAM,
            CHAT_WORKDIR,
            ARTIFACTS_RELATIVE,
            repr(float(since)),
            str(ARTIFACT_IMAGES_MAX),
            str(ARTIFACT_SCAN_MAX),
        ],
        workdir="/",
        timeout_s=60,
    )
    if result.returncode != 0:
        raise _error_from_result(result.returncode, result.stdout, result.stderr)
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetainedWorkspaceError(502, "retained workspace returned invalid data") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("images"), list):
        raise RetainedWorkspaceError(502, "retained workspace returned invalid data")
    return payload


def write_retained_file(
    backend: RetainedChatBackend,
    relative: str,
    content: str,
    *,
    max_write_bytes: int,
    max_preview_bytes: int,
    max_directory_entries: int,
) -> dict[str, Any]:
    """Replace one UTF-8 file's content in place, keeping its inode, mode and owner.

    Room for the new content is reserved before any old byte is overwritten,
    so a full disk or quota answers 507 and leaves the file as it was. Where
    blocks are shared (copy-on-write storage) room can still run out during
    the overwrite, which answers 507 saying the file may be incomplete. The
    replacement is not atomic: a concurrent reader can see it part-written.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > max_write_bytes:
        raise RetainedWorkspaceError(413, "file exceeds the workspace write limit")
    result = backend.manager.exec_capture(
        backend.spec,
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            _WRITE_PROGRAM,
            CHAT_WORKDIR,
            relative,
            str(max_write_bytes),
        ],
        workdir="/",
        timeout_s=60,
        input_bytes=encoded,
    )
    if result.returncode != 0:
        raise _error_from_result(result.returncode, result.stdout, result.stderr)
    return read_retained_file(
        backend,
        relative,
        max_preview_bytes=max_preview_bytes,
        max_directory_entries=max_directory_entries,
    )


__all__ = [
    "ARTIFACTS_RELATIVE",
    "IMAGE_MAX_BYTES",
    "RetainedWorkspaceError",
    "list_retained_images",
    "read_retained_file",
    "read_retained_image",
    "write_retained_file",
]
