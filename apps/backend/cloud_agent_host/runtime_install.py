"""Put a downloaded agent CLI version into a retained chat before it runs.

A chat on a version the sandbox image was built with finds the binary where
the image put it. A chat on a version downloaded since (``runtime_downloads``)
gets that version's files copied into its own writable layer, at the same
path, the first time a member starts:

    /opt/runtimes/<runtime>/<version>

Selecting the version is then the same ``PATH`` prepend as for an image
version, and nothing else changes. Copied rather than mounted, because a
retained chat's state must live in its writable layer (``_verify_mounts``
refuses any mount): the copy travels with the chat when its layer is archived
or restored, and the host can drop its download without breaking the chat.

The files come from the host's own runtime image through a stopped,
networkless source container — the same path the Kilo runtime takes — and are
activated only if every one is root-owned and writable by root alone, so no
member can change the CLI another member runs.
"""

from __future__ import annotations

import secrets
import subprocess
from typing import TYPE_CHECKING, BinaryIO, cast

from cloud_agent_host.runtime_images import (
    SOURCE_LABEL,
    WORK_LABEL,
    discover,
    image_id,
    short_id,
)
from cloud_agent_host.sandbox import runtime_bin_dir
from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.runtime_catalog import downloadable_runtimes

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

#: Written last, so a copy interrupted half-way is never mistaken for a whole.
MARKER = ".freeinference-runtime"
_COPY_TIMEOUT_S = 300.0

_ACTIVATE = r"""
import os
import stat
import sys

stage, target, version, marker = sys.argv[1:]
source = os.path.join(stage, version)
for root, dirs, files in os.walk(source):
    for name in [root, *(os.path.join(root, entry) for entry in dirs + files)]:
        info = os.lstat(name)
        if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise RuntimeError(f"runtime file is not protected: {name}")
parent = os.path.dirname(target)
info = os.lstat(parent)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
    raise RuntimeError("runtime directory is not protected")
if os.path.lexists(target):
    # A copy that died before its marker: replace it whole.
    import shutil

    shutil.rmtree(target)
os.rename(source, target)
path = os.path.join(target, marker)
with open(path + ".tmp", "w", encoding="utf-8") as handle:
    handle.write(version + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    os.fchmod(handle.fileno(), 0o644)
os.replace(path + ".tmp", path)
"""


def _source_image(manager: ChatSandboxManager, runtime_id: str, version: str) -> str:
    """This host's build of the version, on today's sandbox image if there is one."""
    backend = manager.backend
    builds = [
        image
        for image in discover(backend.docker_binary, backend.sandbox_namespace)
        if (image.runtime, image.version) == (runtime_id, version)
    ]
    if not builds:
        raise SandboxError(
            f"{runtime_id} {version} is not downloaded on this host; "
            "update the agent again from the admin Settings page"
        )
    current = image_id(backend.docker_binary, backend.image)
    for image in builds:
        if current is not None and image.base == short_id(current):
            return image.ref
    return builds[0].ref


def ensure_downloaded_runtime(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, runtime_id: str, version: str
) -> None:
    """Make ``version`` runnable in this chat; a no-op when it already is.

    The caller serializes this with other trusted runtime preparation.
    """
    if runtime_id not in downloadable_runtimes():
        return
    if version in manager.backend.image_menu().get(runtime_id, []):
        return
    prefix = runtime_bin_dir(runtime_id, version).removesuffix("/bin")
    marker = f"{prefix}/{MARKER}"
    present = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            'test "$(stat -c %u:%a "$1")" = 0:644 && test "$(cat "$1")" = "$2"',
            "sh",
            marker,
            version,
        ],
        workdir="/",
        timeout_s=20,
        user_uid=0,
        user_gid=0,
    )
    if present.returncode == 0:
        return

    docker = manager.backend.docker_binary
    current = manager._require(spec)
    source = manager._run(
        [
            docker,
            "create",
            "--network",
            "none",
            # Not install work, whatever the image says. A version kept by an
            # earlier release carries the work label of the container it was
            # committed from, and a runner restarting during this copy removes
            # every container that has it (``RuntimeDownloads``).
            "--label",
            f"{WORK_LABEL}=",
            # A copy source, so one this process dies before removing is
            # removed once it is too old to be a copy still running.
            "--label",
            f"{SOURCE_LABEL}={manager.backend.sandbox_namespace}",
            "--entrypoint",
            "/bin/true",
            _source_image(manager, runtime_id, version),
        ],
        timeout=60,
    )
    if source.returncode != 0:
        raise SandboxError(f"could not read the downloaded {runtime_id} {version}")
    source_id = source.stdout.strip()
    stage = f"/opt/runtimes/{runtime_id}/.install-{secrets.token_hex(8)}"
    try:
        made = manager.exec_capture(
            spec,
            ["mkdir", "-m", "0755", stage],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if made.returncode != 0:
            raise SandboxError(f"could not prepare {runtime_id} {version} in this chat")
        # Streamed, not buffered: a CLI is a few hundred megabytes. Root
        # ownership stays in Docker's archive the whole way.
        reader = subprocess.Popen(
            [docker, "cp", f"{source_id}:{prefix}", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            copied = manager._run_bytes(
                [docker, "cp", "-", f"{current.container_id}:{stage}"],
                timeout=_COPY_TIMEOUT_S,
                stdin=cast("BinaryIO", reader.stdout),
            )
        finally:
            if reader.stdout is not None:
                reader.stdout.close()
            try:
                read = reader.wait(timeout=30)
            except subprocess.TimeoutExpired:
                reader.kill()
                read = -1
        if read != 0 or copied.returncode != 0:
            raise SandboxError(f"could not copy {runtime_id} {version} into this chat")
        activated = manager.exec_capture(
            spec,
            ["/usr/bin/python3", "-I", "-c", _ACTIVATE, stage, prefix, version, MARKER],
            workdir="/",
            timeout_s=120,
            user_uid=0,
            user_gid=0,
        )
        if activated.returncode != 0:
            stderr = activated.stderr
            detail = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
            raise SandboxError(
                f"could not activate {runtime_id} {version} in this chat: " + detail.strip()[-300:]
            )
    finally:
        manager.exec_capture(
            spec, ["rm", "-rf", stage], workdir="/", timeout_s=60, user_uid=0, user_gid=0
        )
        manager._run([docker, "rm", source_id], timeout=30)
