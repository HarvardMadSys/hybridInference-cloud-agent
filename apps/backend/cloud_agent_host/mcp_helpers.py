"""Refresh immutable MCP adapters in retained, pre-deployment containers."""

from __future__ import annotations

import hashlib
import io
import secrets
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

_HELPERS = {
    "pi": ("pi-freeinference", "pi-mcp-bridge.ts"),
    "opencode": ("opencode-freeinference", "http-agent-live-bridge"),
    "kilo": ("kilo-freeinference", "http-agent-live-bridge"),
}


def install_mcp_helpers(manager: ChatSandboxManager, spec: ChatSandboxSpec, runtime: str) -> None:
    """Atomically install only the adapter files shipped by this trusted host."""
    source_root = Path("/usr/local/libexec/mcp-runtime")
    if not source_root.is_dir():
        # Source checkouts run the same tests as the packaged host.
        # `parents[3]`: this file, `cloud_agent_host/`, `backend/`, `apps/` —
        # the package sits two levels deeper than it did at the root.
        source_root = Path(__file__).resolve().parents[3] / "deploy/sandbox"
    current = manager._require(spec)
    for name in _HELPERS.get(runtime, ()):
        source = source_root / name
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        directory = "/usr/local/libexec" if name.endswith(".ts") else "/usr/local/bin"
        target = f"{directory}/{name}"
        mode = 0o444 if name.endswith(".ts") else 0o555
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
            continue
        temporary = f"{directory}/.mcp-{secrets.token_hex(16)}"
        archive = io.BytesIO()
        content = source.read_bytes()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            entry = tarfile.TarInfo(temporary.rsplit("/", 1)[-1])
            entry.mode = mode
            entry.size = len(content)
            # Explicit archive ownership works with dropped capabilities even
            # when a development runner's checkout belongs to a non-root UID.
            entry.uid = entry.gid = 0
            tar.addfile(entry, io.BytesIO(content))
        copied = manager._run_bytes(
            [
                manager.backend.docker_binary,
                "cp",
                "-",
                f"{current.container_id}:{directory}",
            ],
            timeout=30,
            input_bytes=archive.getvalue(),
        )
        if copied.returncode != 0:
            raise SandboxError(f"could not install the {runtime} MCP adapter")
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
            raise SandboxError(f"could not activate the {runtime} MCP adapter")
