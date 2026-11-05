"""Install one root-owned, host-trusted file into a retained sandbox container.

Retained containers outlive the image they started from, so a helper the
deployment ships is copied in from this host before it is used, and only
when the container's copy differs. The probe compares owner, mode and
SHA-256; the copy is staged under a random name, checked again as root-owned
and then renamed over the target, so a member never sees a partial file and
cannot have planted the one that runs.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import tarfile
from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec


def install_trusted_file(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    *,
    directory: str,
    name: str,
    content: bytes,
    mode: int,
    what: str,
) -> None:
    """Make ``directory/name`` exactly ``content``, root-owned with ``mode``."""
    current = manager._require(spec)
    digest = hashlib.sha256(content).hexdigest()
    target = f"{directory}/{name}"
    probe = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            'test "$(stat -c %u:%a "$1")" = "0:$3" && test "$(sha256sum "$1" | cut -d " " -f 1)" = "$2"',
            "sh",
            target,
            digest,
            f"{mode:o}",
        ],
        workdir="/",
        timeout_s=20,
        user_uid=0,
        user_gid=0,
    )
    if probe.returncode == 0:
        return
    temporary = f"{directory}/.install-{secrets.token_hex(16)}"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        entry = tarfile.TarInfo(temporary.rsplit("/", 1)[-1])
        entry.mode = mode
        entry.size = len(content)
        # Explicit archive ownership works with dropped capabilities even
        # when a development runner's checkout belongs to a non-root UID.
        entry.uid = entry.gid = 0
        tar.addfile(entry, io.BytesIO(content))
    copied = manager._run_bytes(
        [manager.backend.docker_binary, "cp", "-", f"{current.container_id}:{directory}"],
        timeout=30,
        input_bytes=archive.getvalue(),
    )
    if copied.returncode != 0:
        raise SandboxError(f"could not install the {what}")
    installed = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            'test "$(stat -c %u:%g:%a "$1")" = "0:0:$3" && mv -f "$1" "$2"',
            "sh",
            temporary,
            target,
            f"{mode:o}",
        ],
        workdir="/",
        timeout_s=20,
        user_uid=0,
        user_gid=0,
    )
    if installed.returncode != 0:
        raise SandboxError(f"could not activate the {what}")
