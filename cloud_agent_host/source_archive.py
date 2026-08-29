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

from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterator


DEFAULT_MAX_SOURCE_ARCHIVE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_BASE_BUNDLE_BYTES = 1024 * 1024 * 1024
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


def build_source_archive(
    workdir: str,
    *,
    uid: int,
    gid: int,
    max_bytes: int = DEFAULT_MAX_SOURCE_ARCHIVE_BYTES,
) -> bytes:
    """Build a normalized tar of one prepared checkout, complete or refuse."""
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
        if estimated > max_bytes:
            raise SandboxError("source checkout exceeds the retained import size limit")

    buffer = io.BytesIO()

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

    try:
        with tarfile.open(fileobj=buffer, mode="w", dereference=False) as archive:
            for path in paths:
                relative = path.relative_to(root).as_posix()
                archive.add(path, arcname=relative, recursive=False, filter=normalize)
                if buffer.tell() > max_bytes:
                    raise SandboxError("source archive exceeds the retained import size limit")
    except (OSError, tarfile.TarError) as exc:
        raise SandboxError(f"source checkout could not be archived: {exc}") from exc
    payload = buffer.getvalue()
    if len(payload) > max_bytes:
        raise SandboxError("source archive exceeds the retained import size limit")
    return payload


def build_base_bundle(
    workdir: str,
    *,
    commit: str,
    max_bytes: int = DEFAULT_MAX_BASE_BUNDLE_BYTES,
) -> bytes:
    """Build a bounded credential-free Git bundle for one resolved base commit.

    Follow-up turns run against the commit produced by the trusted publisher,
    while the retained workspace intentionally keeps the Agent CLI's own HEAD,
    index and working tree. Shipping only this object's bundle lets the
    sandbox resolve the new diff base without cloning in the sandbox or
    rewriting any of that owner-visible Git state.
    """
    root = pathlib.Path(workdir).resolve()
    if max_bytes <= 0:
        raise ValueError("base bundle size limit must be positive")
    if not root.is_dir() or not (root / ".git").exists():
        raise SandboxError("source preparer produced no Git checkout")
    if not _FULL_OBJECT_ID.fullmatch(commit):
        raise SandboxError("base bundle commit must be a full Git object id")

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
            ["git", "-C", str(root), "bundle", "create", str(bundle_path), "HEAD"],
            env={**os.environ, **_GIT_ENV},
            capture_output=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise SandboxError(f"could not build retained base bundle: {detail[-300:]}")
        if bundle_path.stat().st_size > max_bytes:
            raise SandboxError("retained base bundle exceeds the import size limit")
        payload = bundle_path.read_bytes()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"could not build retained base bundle: {exc}") from exc
    finally:
        if bundle_path is not None:
            bundle_path.unlink(missing_ok=True)
    if len(payload) > max_bytes:
        raise SandboxError("retained base bundle exceeds the import size limit")
    return payload


__all__ = [
    "DEFAULT_MAX_BASE_BUNDLE_BYTES",
    "DEFAULT_MAX_SOURCE_ARCHIVE_BYTES",
    "build_base_bundle",
    "build_source_archive",
]
