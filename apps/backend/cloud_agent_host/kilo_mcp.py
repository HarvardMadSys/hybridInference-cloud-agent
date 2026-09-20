"""Upgrade retained Kilo executables from the deployment's trusted sandbox image."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

KILO_MCP_PATCH_SHA256 = "1ea8a24628c39800cce5e592bd272cc8bb358f2b8ea07f92f2b96beef8bb2657"
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
    marker = f"{prefix}/mcp-isolation-patch.sha256"
    probe = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            'test "$(stat -c %u:%a "$1")" = 0:644 && test "$(cat "$1")" = "$2"',
            "sh",
            marker,
            KILO_MCP_PATCH_SHA256,
        ],
        workdir="/",
        timeout_s=20,
        user_uid=0,
        user_gid=0,
    )
    if probe.returncode == 0:
        return
    current = manager._require(spec)
    image = manager.backend.image
    source = manager._run(
        [
            manager.backend.docker_binary,
            "create",
            "--network",
            "none",
            "--entrypoint",
            "/bin/true",
            image,
        ],
        timeout=30,
    )
    if source.returncode != 0:
        raise SandboxError("could not prepare the deployment's Kilo runtime")
    container_id = source.stdout.strip()
    stage = f"/opt/runtimes/kilo/.mcp-install-{container_id[:16]}"
    try:
        # Keep root ownership in Docker's archive rather than round-tripping
        # through the runner's filesystem (whose UID may differ in development).
        exported = manager._run_bytes(
            [manager.backend.docker_binary, "cp", f"{container_id}:{prefix}", "-"],
            timeout=120,
        )
        if exported.returncode != 0:
            raise SandboxError("could not read the deployment's Kilo runtime")
        created = manager.exec_capture(
            spec,
            ["mkdir", "-m", "0755", stage],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if created.returncode != 0:
            raise SandboxError("could not prepare the retained Kilo runtime directory")
        copied = manager._run_bytes(
            [manager.backend.docker_binary, "cp", "-", f"{current.container_id}:{stage}"],
            timeout=120,
            input_bytes=exported.stdout,
        )
        if copied.returncode != 0:
            raise SandboxError("could not copy the Kilo runtime into the retained sandbox")
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

    finally:
        manager.exec_capture(
            spec, ["rm", "-rf", stage], workdir="/", timeout_s=30, user_uid=0, user_gid=0
        )
        manager._run([manager.backend.docker_binary, "rm", container_id], timeout=30)
