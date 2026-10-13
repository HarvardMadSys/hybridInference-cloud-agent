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
import subprocess
import tarfile
import tempfile
from typing import TYPE_CHECKING

from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO


#: Counts the working tree *and* the fetched commit's Git objects, so the
#: largest checkout that fits is well under this. A host may raise it.
DEFAULT_MAX_SOURCE_ARCHIVE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_BASE_BUNDLE_BYTES = 1024 * 1024 * 1024
#: Uploads are bounded far below the checkout: the control plane caps what one
#: conversation's uploads may total below this (``MAX_THREAD_ATTACHMENT_BYTES``),
#: because every turn's files are imported together, and this is the host's own
#: refusal if that ever stops holding.
DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES = 64 * 1024 * 1024
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
}


def _entries(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """Yield every entry without following a repository symlink."""
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        parent = pathlib.Path(directory)
        for name in sorted(names):
            yield parent / name
        for name in sorted(files):
            yield parent / name


def _human_size(size: int) -> str:
    for unit, scale in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if size >= scale:
            return f"{size / scale:.2f} {unit}"
    return f"{size} B"


def build_source_archive(
    workdir: str,
    out: BinaryIO,
    *,
    uid: int,
    gid: int,
    max_bytes: int = DEFAULT_MAX_SOURCE_ARCHIVE_BYTES,
) -> int:
    """Write a normalized tar of one prepared checkout to ``out``, complete or refuse.

    The tar goes to the caller's file rather than into memory: a checkout near
    the limit would otherwise be held in the turn process, twice, before Docker
    read any of it. Returns the bytes written. After a refusal ``out`` holds a
    partial archive and must be discarded.
    """
    root = pathlib.Path(workdir).resolve()
    if max_bytes <= 0:
        raise ValueError("source archive size limit must be positive")
    if not root.is_dir() or not (root / ".git").exists():
        raise SandboxError("source preparer produced no Git checkout")

    paths = list(_entries(root))
    estimated = 0
    for path in paths:
        try:
            info = path.lstat()
        except OSError as exc:
            raise SandboxError(f"source checkout changed while archiving: {path.name}") from exc
        mode = info.st_mode
        if stat.S_ISREG(mode):
            estimated += info.st_size
        elif not (stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise SandboxError(f"source checkout contains refused file type at {path.name!r}")
    # Measured to the end, not to the first byte over: the size is what tells
    # the user this is the repository and not a transient failure to retry.
    if estimated > max_bytes:
        raise SandboxError(
            f"source checkout ({_human_size(estimated)} including its Git objects) exceeds "
            f"this host's {_human_size(max_bytes)} retained import size limit"
        )

    def normalize(member: tarfile.TarInfo) -> tarfile.TarInfo:
        name = pathlib.PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise SandboxError(f"source archive path escapes workspace: {member.name!r}")
        if not (member.isfile() or member.isdir() or member.issym()):
            raise SandboxError(f"source archive has refused member type: {member.name!r}")
        member.uid = uid
        member.gid = gid
        member.uname = ""
        member.gname = ""
        # Reproducible metadata and no setuid/setgid bits crossing the boundary.
        member.mode &= 0o1777
        return member

    # Tar headers and padding on top of the file bytes measured above.
    over_limit = SandboxError(
        f"source archive exceeds this host's {_human_size(max_bytes)} retained import size limit"
    )
    start = out.tell()
    try:
        with tarfile.open(fileobj=out, mode="w", dereference=False) as archive:
            for path in paths:
                relative = path.relative_to(root).as_posix()
                archive.add(path, arcname=relative, recursive=False, filter=normalize)
                if out.tell() - start > max_bytes:
                    raise over_limit
    except (OSError, tarfile.TarError) as exc:
        raise SandboxError(f"source checkout could not be archived: {exc}") from exc
    written = out.tell() - start
    if written > max_bytes:
        raise over_limit
    return written


def build_base_bundle(
    workdir: str,
    *,
    commit: str,
    prerequisite: str | None = None,
    max_bytes: int = DEFAULT_MAX_BASE_BUNDLE_BYTES,
) -> bytes:
    """Build a bounded credential-free Git bundle for one resolved base commit.

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
        payload = bundle_path.read_bytes()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"could not build retained base bundle: {exc}") from exc
    finally:
        if bundle_path is not None:
            bundle_path.unlink(missing_ok=True)
    if len(payload) > max_bytes:
        raise SandboxError(
            f"retained base bundle ({_human_size(len(payload))}) exceeds the "
            f"{_human_size(max_bytes)} import size limit"
        )
    return payload


def build_upload_archive(
    files: list[tuple[str, bytes]],
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES,
) -> bytes:
    """Build a root-owned, read-only tar of the files a user attached.

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
    buffer = io.BytesIO()
    seen: set[str] = set()
    directories: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=buffer, mode="w") as archive:
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
                    entry = tarfile.TarInfo(parent)
                    entry.type = tarfile.DIRTYPE
                    entry.mode = 0o555
                    archive.addfile(entry)
                member = tarfile.TarInfo(str(name))
                member.size = len(content)
                member.mode = 0o444
                archive.addfile(member, io.BytesIO(content))
                if buffer.tell() > max_bytes:
                    raise SandboxError("attached files exceed the upload import size limit")
    except tarfile.TarError as exc:
        raise SandboxError(f"attached files could not be archived: {exc}") from exc
    payload = buffer.getvalue()
    if len(payload) > max_bytes:
        raise SandboxError("attached files exceed the upload import size limit")
    return payload


__all__ = [
    "DEFAULT_MAX_BASE_BUNDLE_BYTES",
    "DEFAULT_MAX_SOURCE_ARCHIVE_BYTES",
    "DEFAULT_MAX_UPLOAD_ARCHIVE_BYTES",
    "build_base_bundle",
    "build_source_archive",
    "build_upload_archive",
]
