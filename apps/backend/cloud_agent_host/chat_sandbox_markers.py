"""Sandbox-manager-private markers inside one exact retained container.

A marker is a small root-owned file the sandbox user can neither write nor
forge: the initial archive's receipt, the final archive's policy fence, and
the record of the installation a container's layer holds. A running
container's markers are read and written by fixed programs run as guest root
through the exact container's exec boundary; a stopped container's final
marker is copied through ``docker cp``. Request data is passed as argv and
stdin, never interpolated into code.

Absence is the only soft outcome. A symlink, a non-regular file, an
unexpected owner, mode or link count, an oversized payload or an identity
race fails closed.
"""

from __future__ import annotations

import io
import tarfile
import tempfile
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import RetainedChatLost
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    import subprocess

    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection, ChatSandboxSpec

#: Sandbox Manager-owned durable receipt for the immutable pre-prompt archive. It is
#: deliberately outside every agent-writable or archived root.  Guest root
#: creates this directory as 0700 and the one marker as 0600; all operations
#: validate those exact properties without following symlinks.
# Keep the on-disk path so existing retained containers keep their archive fences.
INITIAL_ARCHIVE_MARKER_ROOT = "/var/lib/freeinference-broker"
INITIAL_ARCHIVE_MARKER_NAME = "initial-archive-v1.json"
INITIAL_ARCHIVE_MARKER_MAX_BYTES = 4096
#: Root-owned policy fence written before a final Docker stop. Unlike the
#: initial receipt, a newer running lifecycle deliberately replaces this file:
#: the retained bridge, not marker presence alone, distinguishes pending from
#: completed final archival after a sandbox manager restart.
FINAL_ARCHIVE_MARKER_NAME = "final-archive-v1.json"
# The canonical uploaded receipt can exceed 4 KiB at the accepted policy and
# S3 VersionId field bounds. Keep the sandbox-manager-private record tightly bounded,
# but leave enough room for every schema-valid marker.
FINAL_ARCHIVE_MARKER_MAX_BYTES = 8192
SETUP_MARKER_MAX_BYTES = 1024

# Runs only through an exact-container, guest-root ``docker exec``.  Keeping
# the filesystem checks in one source-controlled program makes read, publish,
# and delete agree on the same trust boundary.  The immediate parent is also
# checked because ``O_NOFOLLOW`` protects only the final path component.
_PREAMBLE = r"""
import fcntl
import os
import stat
import sys

root, name, limit_text, uid_text, gid_text = sys.argv[1:6]
limit = int(limit_text)
expected_uid = int(uid_text)
expected_gid = int(gid_text)

def same_identity(left, right):
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_gid,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_gid,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )

def checked_parent():
    parent = os.path.dirname(root)
    leaf = os.path.basename(root)
    if not parent or not leaf or leaf in ('.', '..'):
        raise RuntimeError('invalid marker root')
    before = os.lstat(parent)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        raise RuntimeError('marker parent cannot be trusted')
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    after = os.fstat(descriptor)
    if not same_identity(before, after):
        os.close(descriptor)
        raise RuntimeError('marker parent changed while opening')
    return descriptor, leaf

def checked_root():
    parent_fd, leaf = checked_parent()
    try:
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        else:
            os.fsync(parent_fd)
        before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise RuntimeError('marker directory cannot be trusted')
        descriptor = os.open(
            leaf,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        after = os.fstat(descriptor)
        if not same_identity(before, after):
            os.close(descriptor)
            raise RuntimeError('marker directory changed while opening')
    finally:
        os.close(parent_fd)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor

def read_checked(descriptor):
    try:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or not 0 < before.st_size <= limit
    ):
        raise RuntimeError('marker file cannot be trusted')
    marker_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        opened = os.fstat(marker_fd)
        if not same_identity(before, opened):
            raise RuntimeError('marker file changed while opening')
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(marker_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b''.join(chunks)
        after = os.fstat(marker_fd)
        if not same_identity(opened, after) or len(payload) != before.st_size:
            raise RuntimeError('marker file changed while reading')
        return payload
    finally:
        os.close(marker_fd)
"""

#: Print the marker, or exit with :data:`_ABSENT` when there is none.
_READ_PROGRAM = (
    _PREAMBLE
    + r"""
descriptor = checked_root()
try:
    payload = read_checked(descriptor)
    if payload is None:
        raise SystemExit(44)
    sys.stdout.buffer.write(payload)
finally:
    os.close(descriptor)
"""
)
#: Publish stdin as the marker, atomically and fsynced. ``argv[6]`` says
#: whether a different marker already there may be replaced.
_WRITE_PROGRAM = (
    _PREAMBLE
    + r"""
payload = sys.stdin.buffer.read(limit + 1)
if not 0 < len(payload) <= limit:
    raise RuntimeError('marker payload has an invalid size')
replace_existing = sys.argv[6] == '1'
descriptor = checked_root()
temporary = '.%s.%s.tmp' % (name, os.getpid())
try:
    existing = read_checked(descriptor)
    if existing is not None:
        if existing == payload:
            raise SystemExit(0)
        if not replace_existing:
            raise RuntimeError('a different marker already exists')
    marker_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=descriptor,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(marker_fd, payload[offset:])
        os.fchmod(marker_fd, 0o600)
        written = os.fstat(marker_fd)
        if (
            written.st_uid != expected_uid
            or written.st_gid != expected_gid
            or stat.S_IMODE(written.st_mode) != 0o600
            or written.st_nlink != 1
            or written.st_size != len(payload)
        ):
            raise RuntimeError('temporary marker cannot be trusted')
        os.fsync(marker_fd)
    finally:
        os.close(marker_fd)
    os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    os.fsync(descriptor)
    if read_checked(descriptor) != payload:
        raise RuntimeError('published marker failed verification')
finally:
    try:
        os.unlink(temporary, dir_fd=descriptor)
    except FileNotFoundError:
        pass
    os.close(descriptor)
"""
)
#: Remove the marker, if any, durably.
_CLEAR_PROGRAM = (
    _PREAMBLE
    + r"""
descriptor = checked_root()
try:
    if read_checked(descriptor) is None:
        raise SystemExit(0)
    os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)
    if read_checked(descriptor) is not None:
        raise RuntimeError('marker deletion was not durable')
finally:
    os.close(descriptor)
"""
)
#: Remove the marker only if it is still the one given on stdin.
_DELETE_PROGRAM = (
    _PREAMBLE
    + r"""
expected = sys.stdin.buffer.read(limit + 1)
if not 0 < len(expected) <= limit:
    raise RuntimeError('expected marker has an invalid size')
descriptor = checked_root()
try:
    if read_checked(descriptor) != expected:
        raise RuntimeError('marker changed before deletion')
    os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)
    try:
        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError('marker deletion was not durable')
finally:
    os.close(descriptor)
"""
)
#: The read program's exit status for a marker that is not there.
_ABSENT = 44


def require_container(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    expected_container_id: str,
    *,
    running: bool,
    before: str,
    refusal: str,
) -> ChatSandboxInspection:
    """The container ``spec`` names, if it is the expected one, running or stopped as required.

    A different container means the sandbox was lost, not that it is busy.
    Unlike the manager's ``_require_exact``, this looks the container up by
    name, as the marker methods always have.
    """
    current = manager._require(spec)
    if current.container_id != expected_container_id:
        raise RetainedChatLost(f"chat sandbox container identity changed before {before}")
    if current.running != running:
        raise SandboxError(refusal)
    return current


def check_payload(payload: object, max_bytes: int, what: str) -> None:
    """Refuse a marker payload that is not bytes, or is empty or over its bound."""
    if not isinstance(payload, bytes) or not 0 < len(payload) <= max_bytes:
        raise ValueError(f"{what} payload has an invalid size")


def _run(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    program: str,
    *,
    name: str,
    max_bytes: int,
    expected_container_id: str,
    extra_args: tuple[str, ...] = (),
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one marker program as guest root in the exact container."""
    argv = [
        "python3",
        "-c",
        program,
        INITIAL_ARCHIVE_MARKER_ROOT,
        name,
        str(max_bytes),
        "0",
        "0",
        *extra_args,
    ]
    if input_bytes is None:
        return manager.exec_capture(
            spec,
            argv,
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
            expected_container_id=expected_container_id,
        )
    return manager.exec_capture(
        spec,
        argv,
        workdir="/",
        timeout_s=20,
        input_bytes=input_bytes,
        user_uid=0,
        user_gid=0,
        expected_container_id=expected_container_id,
    )


def _stderr_tail(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stderr.decode("utf-8", "replace")[-300:]


def read(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    name: str,
    max_bytes: int,
    what: str,
    expected_container_id: str,
) -> bytes | None:
    """Read one marker from the running container, or ``None`` when it is absent."""
    result = _run(
        manager,
        spec,
        _READ_PROGRAM,
        name=name,
        max_bytes=max_bytes,
        expected_container_id=expected_container_id,
    )
    if result.returncode == _ABSENT:
        return None
    if result.returncode != 0:
        raise SandboxError(f"{what} cannot be trusted: " + _stderr_tail(result))
    payload = result.stdout
    if not 0 < len(payload) <= max_bytes:
        raise SandboxError(f"{what} has an invalid size")
    return payload


def write(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    payload: bytes,
    *,
    name: str,
    max_bytes: int,
    expected_container_id: str,
    replace_existing: bool,
) -> None:
    """Publish one fsynced marker in the running container."""
    result = _run(
        manager,
        spec,
        _WRITE_PROGRAM,
        name=name,
        max_bytes=max_bytes,
        expected_container_id=expected_container_id,
        extra_args=("1" if replace_existing else "0",),
        input_bytes=payload,
    )
    if result.returncode != 0:
        raise SandboxError("sandbox archive marker could not be persisted: " + _stderr_tail(result))


def clear_final(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, *, expected_container_id: str
) -> None:
    """Remove the final marker, if any, from the exact running container."""
    require_container(
        manager,
        spec,
        expected_container_id,
        running=True,
        before="marker clear",
        refusal="final archive marker can be cleared only while running",
    )
    result = _run(
        manager,
        spec,
        _CLEAR_PROGRAM,
        name=FINAL_ARCHIVE_MARKER_NAME,
        max_bytes=FINAL_ARCHIVE_MARKER_MAX_BYTES,
        expected_container_id=expected_container_id,
    )
    if result.returncode != 0:
        raise SandboxError(
            "final archive marker could not be cleared safely: " + _stderr_tail(result)
        )


def delete_initial(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    expected_payload: bytes,
    *,
    expected_container_id: str,
) -> None:
    """Remove the initial marker, only if it still holds ``expected_payload``."""
    check_payload(expected_payload, INITIAL_ARCHIVE_MARKER_MAX_BYTES, "initial archive marker")
    result = _run(
        manager,
        spec,
        _DELETE_PROGRAM,
        name=INITIAL_ARCHIVE_MARKER_NAME,
        max_bytes=INITIAL_ARCHIVE_MARKER_MAX_BYTES,
        expected_container_id=expected_container_id,
        input_bytes=expected_payload,
    )
    if result.returncode != 0:
        raise SandboxError(
            "initial archive marker could not be removed safely: " + _stderr_tail(result)
        )


def read_stopped_final(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, *, expected_container_id: str
) -> bytes:
    """Copy the final marker out of the exact stopped container, checking its tar entry first."""
    require_container(
        manager,
        spec,
        expected_container_id,
        running=False,
        before="marker read",
        refusal="final archive recovery requires the exact stopped container",
    )
    marker_path = f"{INITIAL_ARCHIVE_MARKER_ROOT}/{FINAL_ARCHIVE_MARKER_NAME}"
    with tempfile.TemporaryFile(prefix="freeinference-final-archive-marker-") as archive:
        manager.export_archive(
            spec,
            marker_path,
            archive,
            max_bytes=FINAL_ARCHIVE_MARKER_MAX_BYTES + 32 * 1024,
            expected_container_id=expected_container_id,
        )
        archive.seek(0)
        try:
            with tarfile.open(fileobj=archive, mode="r:*") as copied:
                entries = copied.getmembers()
                if len(entries) != 1:
                    raise SandboxError("final archive marker copy has unexpected entries")
                entry = entries[0]
                if (
                    not entry.isreg()
                    or entry.name.rsplit("/", 1)[-1] != FINAL_ARCHIVE_MARKER_NAME
                    or entry.uid != 0
                    or entry.gid != 0
                    or entry.mode & 0o7777 != 0o600
                    or not 0 < entry.size <= FINAL_ARCHIVE_MARKER_MAX_BYTES
                ):
                    raise SandboxError("final archive marker cannot be trusted")
                source = copied.extractfile(entry)
                if source is None:
                    raise SandboxError("final archive marker has no readable payload")
                payload = source.read(FINAL_ARCHIVE_MARKER_MAX_BYTES + 1)
        except (tarfile.TarError, OSError) as exc:
            raise SandboxError("final archive marker copy is invalid") from exc
    if len(payload) != entry.size:
        raise SandboxError("final archive marker changed while being read")
    return payload


def write_stopped_final(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    payload: bytes,
    *,
    expected_container_id: str,
) -> None:
    """Copy the final marker into the exact stopped container's layer, then read it back."""
    check_payload(payload, FINAL_ARCHIVE_MARKER_MAX_BYTES, "final archive marker")
    current = require_container(
        manager,
        spec,
        expected_container_id,
        running=False,
        before="marker write",
        refusal="stopped final archive marker requires a stopped container",
    )
    with tempfile.TemporaryFile(prefix="freeinference-final-archive-marker-write-") as archive:
        with tarfile.open(fileobj=archive, mode="w") as payload_archive:
            marker_root = INITIAL_ARCHIVE_MARKER_ROOT.rsplit("/", 1)[-1]
            directory = tarfile.TarInfo(marker_root)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o700
            directory.uid = 0
            directory.gid = 0
            directory.mtime = 0
            payload_archive.addfile(directory)
            entry = tarfile.TarInfo(f"{marker_root}/{FINAL_ARCHIVE_MARKER_NAME}")
            entry.size = len(payload)
            entry.mode = 0o600
            entry.uid = 0
            entry.gid = 0
            entry.mtime = 0
            payload_archive.addfile(entry, io.BytesIO(payload))
        archive.seek(0)
        result = manager._run_bytes(
            [
                manager.backend.docker_binary,
                "cp",
                "-",
                f"{current.container_id}:{INITIAL_ARCHIVE_MARKER_ROOT.rsplit('/', 1)[0]}",
            ],
            timeout=30,
            stdin=archive,
        )
    if result.returncode != 0:
        raise SandboxError(
            "stopped final archive marker could not be persisted: " + _stderr_tail(result)
        )
    # Through the manager, so the read-back is the one its callers rely on.
    if manager.read_final_archive_marker(spec, expected_container_id=expected_container_id) != (
        payload
    ):
        raise SandboxError("stopped final archive marker failed verification")
