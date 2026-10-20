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
networkless source container (``tree_copy``, which the Kilo runtime uses
too), and are activated only if every one is root-owned and writable by root
alone, so no member can change the CLI another member runs.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from cloud_agent_host.runtime_images import (
    discover,
    image_id,
    short_id,
)
from cloud_agent_host.sandbox import runtime_bin_dir
from cloud_agent_host.tree_copy import has_root_marker, staged_tree
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
    if has_root_marker(manager, spec, f"{prefix}/{MARKER}", version):
        return
    stage = f"/opt/runtimes/{runtime_id}/.install-{secrets.token_hex(8)}"
    with staged_tree(
        manager,
        spec,
        image=_source_image(manager, runtime_id, version),
        source=prefix,
        stage=stage,
        what=f"{runtime_id} {version}",
        copy_timeout_s=_COPY_TIMEOUT_S,
    ):
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
