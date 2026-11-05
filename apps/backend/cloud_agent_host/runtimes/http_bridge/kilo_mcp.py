"""Upgrade retained Kilo executables from the deployment's trusted sandbox image."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_exec import install_once
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.tree_copy import has_root_marker, staged_tree

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

KILO_MCP_PATCH_SHA256 = "1ea8a24628c39800cce5e592bd272cc8bb358f2b8ea07f92f2b96beef8bb2657"
#: The one Kilo version whose MCP isolation patch was reviewed. A literal on
#: purpose, not the shared pin: until the patch is reviewed against a new
#: version, :func:`ensure_kilo_runtime` refuses it, and a pin bump fails
#: ``tests/unit/test_kilo_mcp.py``.
KILO_MCP_VERSION = "7.4.17"

_ACTIVATE = r"""
import os
import stat
import sys
import tempfile

stage, target, expected = sys.argv[1:]
for directory in ("/opt", "/opt/runtimes", "/opt/runtimes/kilo", target, target + "/bin", stage):
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Kilo runtime directory is not protected")
with open(stage + "/mcp-isolation-patch.sha256", encoding="utf-8") as handle:
    if handle.read().strip() != expected:
        raise RuntimeError("Kilo runtime does not carry the reviewed isolation patch")
# OverlayFS cannot rename a directory from the image's lower layer. Keep the
# complete new build in the writable layer and atomically replace only the
# versioned executable links; sibling processes may still map the old binary.
build = "/opt/runtimes/kilo/.mcp-build-" + expected
if not os.path.exists(build):
    os.rename(stage, build)
else:
    info = os.lstat(build)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Kilo installed build is not protected")
for binary in ("kilo", "kilocode"):
    temporary = target + "/bin/.mcp-" + binary
    if os.path.lexists(temporary):
        os.unlink(temporary)
    os.symlink(build + "/bin/kilo", temporary)
    os.replace(temporary, target + "/bin/" + binary)
fd, temporary = tempfile.mkstemp(prefix=".mcp-marker-", dir=target)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(expected + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    os.fchmod(handle.fileno(), 0o644)
os.replace(temporary, target + "/mcp-isolation-patch.sha256")
"""


def _marker(version: str) -> str:
    """The file that names the isolation patch a Kilo version's build carries."""
    return f"/opt/runtimes/kilo/{version}/mcp-isolation-patch.sha256"


def ensure_kilo_runtime_once(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, version: str
) -> None:
    """Run :func:`ensure_kilo_runtime` under the chat's install lock, once per container.

    Once the marker holds the reviewed patch's digest, it does for the
    container's life. The image put it there, or the activation wrote it
    last, as root, mode 0644, having refused unless root owns ``/opt``, the
    Kilo runtime directories and the stage and no one else can write them.
    No member is ever root, so none can change the marker or the executables
    it vouches for, and the activation runs only where the marker is wrong.
    The digest is a constant of this host's code, so the path is the key.
    """
    install_once(
        manager, spec, _marker(version), lambda: ensure_kilo_runtime(manager, spec, version)
    )


def ensure_kilo_runtime(manager: ChatSandboxManager, spec: ChatSandboxSpec, version: str) -> None:
    """Install the patched same-version binary without touching member sessions.

    New images already contain it. For retained containers, Docker copies only
    immutable executable files from a stopped, networkless source container;
    no host mount, upstream secret or agent-controlled source is involved.
    The caller serializes upgrades with other trusted runtime preparation.
    """
    if version != KILO_MCP_VERSION:
        raise SandboxError("Kilo runtime version has no reviewed MCP isolation build")
    prefix = f"/opt/runtimes/kilo/{version}"
    if has_root_marker(manager, spec, _marker(version), KILO_MCP_PATCH_SHA256):
        return
    stage = f"/opt/runtimes/kilo/.mcp-install-{secrets.token_hex(8)}"
    with staged_tree(
        manager,
        spec,
        image=lambda: manager.backend.image,
        source=prefix,
        stage=stage,
        what="the deployment's Kilo runtime",
        copy_timeout_s=120,
    ):
        installed = manager.exec_capture(
            spec,
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                _ACTIVATE,
                f"{stage}/{version}",
                prefix,
                KILO_MCP_PATCH_SHA256,
            ],
            workdir="/",
            timeout_s=30,
            user_uid=0,
            user_gid=0,
        )
        if installed.returncode != 0:
            stderr = installed.stderr
            detail = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
            raise SandboxError(
                "could not activate the retained Kilo MCP runtime: " + detail.strip()[-500:]
            )
