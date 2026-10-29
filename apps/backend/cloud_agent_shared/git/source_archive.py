"""Stream a trusted, credential-scrubbed checkout into a chat container.

The source preparer may use an ephemeral directory in its own infrastructure
container, but the resulting chat has no host worktree.  This module turns the
prepared checkout into a bounded tar whose ownership is normalized for the
sandbox uid; Docker's archive API is the only boundary into ``/workspace``.
"""

from __future__ import annotations

import io
import os
import pathlib
import re
import stat
import struct
import subprocess
import tarfile
import tempfile
from typing import TYPE_CHECKING, Final

from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.git.source_cancellation import (
    CancellableArchiveWriter,
    check_source_stop,
    checked_entries,
    source_entries,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import BinaryIO

    from cloud_agent_shared.git.source_cancellation import StopSignal


#: Counts the working tree *and* the fetched commit's Git objects, so the
#: largest checkout that fits is well under this. A host may raise it.
DEFAULT_MAX_SOURCE_ARCHIVE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_SOURCE_ARCHIVE_ENTRIES = 1_000_000
DEFAULT_MAX_SOURCE_MANIFEST_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_BASE_BUNDLE_BYTES = 1024 * 1024 * 1024
#: Uploads are bounded far below the checkout. Every turn's files are imported
#: together, so the control plane admits a conversation's uploads only while the
#: archive built from them, headers and all (:func:`upload_archive_size`), fits
#: this; the host refuses a larger one in case that ever stops holding.
DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES = 64 * 1024 * 1024
#: How the upload archive is written. Its exact length depends on both.
_UPLOAD_FORMAT: Final = tarfile.PAX_FORMAT
_UPLOAD_ENCODING: Final = "utf-8"
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_MANIFEST_LENGTH = struct.Struct(">I")
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
}


def _entries(root: str, *, stop: StopSignal | None = None) -> Iterator[str]:
    """Yield every entry's path, checking cancellation even inside large directories."""
    return source_entries(root, stop)


def _manifest_names(manifest: BinaryIO, *, stop: StopSignal | None = None) -> Iterator[str]:
    """Replay the length-prefixed names below the checkout in a validated disk manifest."""
    while True:
        check_source_stop(stop)
        header = manifest.read(_MANIFEST_LENGTH.size)
        if not header:
            return
        if len(header) != _MANIFEST_LENGTH.size:
            raise SandboxError("source checkout manifest is truncated")
        (length,) = _MANIFEST_LENGTH.unpack(header)
        check_source_stop(stop)
        encoded = manifest.read(length)
        if len(encoded) != length:
            raise SandboxError("source checkout manifest is truncated")
        check_source_stop(stop)
        yield os.fsdecode(encoded)


def _human_size(size: int) -> str:
    for unit, scale in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if size >= scale:
            return f"{size / scale:.2f} {unit}"
    return f"{size} B"


def _write_source_manifest(
    root: str,
    manifest: BinaryIO,
    *,
    max_bytes: int,
    max_entries: int,
    max_manifest_bytes: int,
    stop: StopSignal | None = None,
) -> None:
    """Validate one streamed tree walk and spool only its bounded path records."""
    estimated = 0
    manifest_bytes = 0
    # The walk joins each name below the checkout to this prefix.
    prefix = os.path.join(root, "")
    entry_paths = _entries(root) if stop is None else _entries(root, stop=stop)
    for entries, path in enumerate(checked_entries(entry_paths, stop), start=1):
        if entries > max_entries:
            raise SandboxError(
                f"source checkout has more than {max_entries:,} entries, which exceeds "
                "this host's retained import metadata limit"
            )
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise SandboxError(
                f"source checkout changed while archiving: {os.path.basename(path)}"
            ) from exc
        mode = info.st_mode
        if stat.S_ISREG(mode):
            estimated += info.st_size
        elif not (stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise SandboxError(
                f"source checkout contains refused file type at {os.path.basename(path)!r}"
            )
        relative = os.fsencode(path.removeprefix(prefix))
        manifest_bytes += _MANIFEST_LENGTH.size + len(relative)
        if manifest_bytes > max_manifest_bytes:
            raise SandboxError(
                f"source checkout manifest exceeds this host's "
                f"{_human_size(max_manifest_bytes)} retained import metadata limit"
            )
        check_source_stop(stop)
        manifest.write(_MANIFEST_LENGTH.pack(len(relative)))
        manifest.write(relative)
    # Measured to the end, not the first byte over: this tells the user that
    # the repository itself, rather than a transient operation, is too large.
    if estimated > max_bytes:
        raise SandboxError(
            f"source checkout ({_human_size(estimated)} including its Git objects) exceeds "
            f"this host's {_human_size(max_bytes)} retained import size limit"
        )
    check_source_stop(stop)
    manifest.seek(0)


def _normalize_source_member(member: tarfile.TarInfo, *, uid: int, gid: int) -> tarfile.TarInfo:
    # What PurePosixPath would find, without parsing a path per member.
    if member.name.startswith("/") or ".." in member.name.split("/"):
        raise SandboxError(f"source archive path escapes workspace: {member.name!r}")
    if not (member.isfile() or member.isdir() or member.issym()):
        raise SandboxError(f"source archive has refused member type: {member.name!r}")
    member.uid = uid
    member.gid = gid
    member.uname = ""
    member.gname = ""
    # Reproducible metadata and no setuid/setgid bits crossing the boundary.
    member.mode &= 0o1777
    # Whole seconds, which the ustar header holds. A fractional one, as every
    # stat returns, costs each member a 1 KiB pax header, written to the spool,
    # copied into the container and counted against the size limit. Git there
    # compares every file's content after the copy whatever its mtime: its
    # inode, ctime and owner no longer match the index.
    member.mtime = int(member.mtime)
    return member


def _write_source_tar(
    root: str,
    manifest: BinaryIO,
    out: BinaryIO,
    *,
    uid: int,
    gid: int,
    max_bytes: int,
    stop: StopSignal | None = None,
) -> int:
    """Replay the bounded manifest without retaining tar's write-only member index."""
    over_limit = SandboxError(
        f"source archive exceeds this host's {_human_size(max_bytes)} retained import size limit"
    )
    start = out.tell()
    prefix = os.path.join(root, "")

    def normalize(member: tarfile.TarInfo) -> tarfile.TarInfo:
        return _normalize_source_member(member, uid=uid, gid=gid)

    try:
        with tarfile.open(
            fileobj=CancellableArchiveWriter(out, stop), mode="w", dereference=False
        ) as archive:
            names = _manifest_names(manifest, stop=stop)
            for name in checked_entries(names, stop):
                archive.add(prefix + name, arcname=name, recursive=False, filter=normalize)
                # A write-only source archive never reads TarFile's random-access index.
                archive.getmembers().clear()
                if out.tell() - start > max_bytes:
                    raise over_limit
    except (OSError, tarfile.TarError) as exc:
        raise SandboxError(f"source checkout could not be archived: {exc}") from exc
    written = out.tell() - start
    if written > max_bytes:
        raise over_limit
    return written


def build_source_archive(
    workdir: str,
    out: BinaryIO,
    *,
    uid: int,
    gid: int,
    max_bytes: int = DEFAULT_MAX_SOURCE_ARCHIVE_BYTES,
    max_entries: int = DEFAULT_MAX_SOURCE_ARCHIVE_ENTRIES,
    max_manifest_bytes: int = DEFAULT_MAX_SOURCE_MANIFEST_BYTES,
    stop: StopSignal | None = None,
) -> int:
    """Write a normalized tar of one prepared checkout to ``out``, complete or refuse.

    The tar goes to the caller's file rather than into memory: a checkout near
    the limit would otherwise be held in the turn process, twice, before Docker
    read any of it. The validated path manifest is bounded and disk-backed, so
    metadata memory does not grow with repository entry count. Returns the bytes
    written. After a refusal ``out`` may hold a partial archive and must be discarded.
    """
    check_source_stop(stop)
    root = pathlib.Path(workdir).resolve()
    if max_bytes <= 0:
        raise ValueError("source archive size limit must be positive")
    if max_entries <= 0:
        raise ValueError("source archive entry limit must be positive")
    if max_manifest_bytes <= 0:
        raise ValueError("source archive manifest size limit must be positive")
    if not root.is_dir() or not (root / ".git").exists():
        raise SandboxError("source preparer produced no Git checkout")

    try:
        with tempfile.TemporaryFile(prefix="freeinference-source-manifest-") as manifest:
            _write_source_manifest(
                str(root),
                manifest,
                max_bytes=max_bytes,
                max_entries=max_entries,
                max_manifest_bytes=max_manifest_bytes,
                stop=stop,
            )
            return _write_source_tar(
                str(root),
                manifest,
                out,
                uid=uid,
                gid=gid,
                max_bytes=max_bytes,
                stop=stop,
            )
    except SandboxError:
        raise
    except OSError as exc:
        raise SandboxError(f"source checkout could not be archived: {exc}") from exc


def build_base_bundle(
    workdir: str,
    *,
    commit: str,
    prerequisite: str | None = None,
    max_bytes: int = DEFAULT_MAX_BASE_BUNDLE_BYTES,
) -> BinaryIO:
    """Build a bounded credential-free Git bundle in a seekable temporary file.

    Follow-up turns run against the commit produced by the trusted publisher,
    while the retained workspace intentionally keeps the Agent CLI's own HEAD,
    index and working tree. Shipping only this object's bundle lets the
    sandbox resolve the new diff base without cloning in the sandbox or
    rewriting any of that owner-visible Git state.

    ``prerequisite`` names a commit the receiving workspace already holds. The
    bundle then carries only what ``commit`` adds on top of it — for a
    published turn, the agent's own change — instead of the whole tree a
    second time, which for a large repository is gigabytes against a limit of
    one. Git refuses to unbundle it into a workspace that lacks the
    prerequisite, so a wrong guess fails closed. Without one the bundle holds
    everything ``commit`` reaches in this checkout.
    """
    root = pathlib.Path(workdir).resolve()
    if max_bytes <= 0:
        raise ValueError("base bundle size limit must be positive")
    if not root.is_dir() or not (root / ".git").exists():
        raise SandboxError("source preparer produced no Git checkout")
    if not _FULL_OBJECT_ID.fullmatch(commit):
        raise SandboxError("base bundle commit must be a full Git object id")
    if prerequisite is not None and not _FULL_OBJECT_ID.fullmatch(prerequisite):
        raise SandboxError("base bundle prerequisite must be a full Git object id")
    revisions = "HEAD" if prerequisite is None else f"{prerequisite}..HEAD"

    bundle_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="agent-base-", suffix=".bundle", delete=False
        ) as bundle_file:
            bundle_path = pathlib.Path(bundle_file.name)
        resolved = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            env={**os.environ, **_GIT_ENV},
            capture_output=True,
            timeout=30,
            check=False,
        )
        if resolved.returncode != 0 or resolved.stdout.decode().strip().lower() != commit.lower():
            raise SandboxError("source checkout HEAD does not match retained base commit")
        result = subprocess.run(
            ["git", "-C", str(root), "bundle", "create", str(bundle_path), revisions],
            env={**os.environ, **_GIT_ENV},
            capture_output=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise SandboxError(f"could not build retained base bundle: {detail[-300:]}")
        size = bundle_path.stat().st_size
        if size > max_bytes:
            raise SandboxError(
                f"retained base bundle ({_human_size(size)}) exceeds the "
                f"{_human_size(max_bytes)} import size limit"
            )
        return bundle_path.open("rb")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"could not build retained base bundle: {exc}") from exc
    finally:
        if bundle_path is not None:
            bundle_path.unlink(missing_ok=True)


def build_upload_archive(
    files: list[tuple[str, bytes]],
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES,
) -> bytes:
    """Build a small upload archive in memory; streaming callers use ``write_upload_archive``."""
    buffer = io.BytesIO()
    write_upload_archive(files, buffer, max_bytes=max_bytes)
    return buffer.getvalue()


def write_upload_archive(
    files: Iterable[tuple[str, bytes]],
    destination: BinaryIO,
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES,
) -> None:
    """Write a root-owned, read-only tar, retaining only the current upload body.

    Three differences from :func:`build_source_archive`, each deliberate:

    * **Root-owned and unwritable.** The sandbox has no read-only mounts —
      ``_verify_mounts`` refuses bind mounts and volumes outright — so the only
      thing that can make ``/uploads`` read-only is ownership and mode. Members
      are ``uid 0``/``gid 0`` at ``0444`` and ``0555``, which the sandbox user
      can read and cannot alter. An agent that wants to change an upload has to
      copy it first, which keeps the change explicit.
    * **No symlinks.** The checkout came from a trusted clone; this content came
      from whoever used the browser. A symlink member is refused rather than
      followed, so an upload cannot point at anything outside ``/uploads``.
    * **Directories are emitted, not implied.** Extraction would otherwise
      create intermediate directories with its own defaults, and a directory the
      sandbox can write into is a directory it can add files to.
    """
    if max_bytes <= 0:
        raise ValueError("upload archive size limit must be positive")
    start = destination.tell()
    seen: set[str] = set()
    directories: set[str] = set()
    total = 0
    try:
        with tarfile.open(
            fileobj=destination, mode="w", format=_UPLOAD_FORMAT, encoding=_UPLOAD_ENCODING
        ) as archive:
            for path, content in files:
                name = pathlib.PurePosixPath(path)
                if name.is_absolute() or ".." in name.parts or not name.parts:
                    raise SandboxError(f"upload path escapes the upload directory: {path!r}")
                if path in seen:
                    raise SandboxError(f"upload path appears twice: {path!r}")
                seen.add(path)
                total += len(content)
                if total > max_bytes:
                    raise SandboxError("attached files exceed the upload import size limit")
                for depth in range(1, len(name.parts)):
                    parent = "/".join(name.parts[:depth])
                    if parent in directories:
                        continue
                    directories.add(parent)
                    archive.addfile(_upload_member(parent))
                archive.addfile(_upload_member(str(name), size=len(content)), io.BytesIO(content))
                if destination.tell() - start > max_bytes:
                    raise SandboxError("attached files exceed the upload import size limit")
    except tarfile.TarError as exc:
        raise SandboxError(f"attached files could not be archived: {exc}") from exc
    if destination.tell() - start > max_bytes:
        raise SandboxError("attached files exceed the upload import size limit")


def _upload_member(name: str, *, size: int | None = None) -> tarfile.TarInfo:
    """One ``/uploads`` entry, root-owned and read-only: a folder when ``size`` is ``None``."""
    member = tarfile.TarInfo(name)
    if size is None:
        member.type = tarfile.DIRTYPE
        member.mode = 0o555
    else:
        member.size = size
        member.mode = 0o444
    return member


def upload_archive_size(files: Iterable[tuple[str, int]]) -> int:
    """The exact length of what :func:`build_upload_archive` writes for these paths and sizes.

    Every folder level is an entry with a header of its own, and a name past
    100 characters, or one that is not ASCII, adds a pax header to it. So
    almost no content can make an archive over its limit, and the control
    plane, which holds paths and sizes, admits a conversation's uploads by
    this. A folder header's length depends only on its name's shape, so each
    shape is measured once: the deepest set it admits has 51,000 folders.
    """
    headers: dict[tuple[bool, int, int], int] = {}
    directories: set[str] = set()
    total = 0
    for path, length in files:
        name = pathlib.PurePosixPath(path)
        parent = ""
        for part in name.parts[:-1]:
            parent = f"{parent}/{part}" if parent else part
            if parent in directories:
                continue
            directories.add(parent)
            shape = (parent.isascii(), len(parent), len(parent.encode(_UPLOAD_ENCODING)))
            if shape not in headers:
                headers[shape] = len(_upload_member(parent).tobuf(_UPLOAD_FORMAT, _UPLOAD_ENCODING))
            total += headers[shape]
        member = _upload_member(str(name), size=length)
        total += len(member.tobuf(_UPLOAD_FORMAT, _UPLOAD_ENCODING))
        total += -(-length // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    # Closing writes two zero blocks, then pads the archive to a whole record.
    total += 2 * tarfile.BLOCKSIZE
    return -(-total // tarfile.RECORDSIZE) * tarfile.RECORDSIZE


__all__ = [
    "DEFAULT_MAX_BASE_BUNDLE_BYTES",
    "DEFAULT_MAX_SOURCE_ARCHIVE_BYTES",
    "DEFAULT_MAX_SOURCE_ARCHIVE_ENTRIES",
    "DEFAULT_MAX_SOURCE_MANIFEST_BYTES",
    "DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES",
    "build_base_bundle",
    "build_source_archive",
    "build_upload_archive",
    "upload_archive_size",
]
