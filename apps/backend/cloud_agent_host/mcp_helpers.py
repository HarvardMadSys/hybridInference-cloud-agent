"""Refresh immutable MCP adapters in retained, pre-deployment containers."""

from __future__ import annotations

import hashlib
import io
import secrets
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloud_agent_host.install_locks import install_lock
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
    from cloud_agent_host.chat_sandbox_exec import ProvenContainers
    from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection

_HELPERS = {
    "pi": ("pi-freeinference", "pi-mcp-bridge.ts"),
    "opencode": ("opencode-freeinference", "http-agent-live-bridge"),
    "kilo": ("kilo-freeinference", "http-agent-live-bridge"),
}


@dataclass(frozen=True)
class _Helper:
    """One adapter as this host ships it, and where it goes in the guest."""

    target: str
    mode: int
    content: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def key(self) -> str:
        """Its path and content, as a proven container remembers them found."""
        return f"{self.target}@sha256:{self.digest}"


def _helpers(runtime: str) -> list[_Helper]:
    """``runtime``'s adapters, each read once from this host's copy."""
    source_root = Path("/usr/local/libexec/mcp-runtime")
    if not source_root.is_dir():
        # Source checkouts run the same tests as the packaged host.
        # `parents[3]`: this file, `cloud_agent_host/`, `backend/`, `apps/` —
        # the package sits two levels deeper than it did at the root.
        source_root = Path(__file__).resolve().parents[3] / "deploy/sandbox"
    helpers = []
    for name in _HELPERS.get(runtime, ()):
        directory = "/usr/local/libexec" if name.endswith(".ts") else "/usr/local/bin"
        mode = 0o444 if name.endswith(".ts") else 0o555
        helpers.append(_Helper(f"{directory}/{name}", mode, (source_root / name).read_bytes()))
    return helpers


def install_mcp_helpers(manager: ChatSandboxManager, spec: ChatSandboxSpec, runtime: str) -> None:
    """Atomically install only the adapter files shipped by this trusted host."""
    _install(manager, spec, runtime, _helpers(runtime))


def install_mcp_helpers_once(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, runtime: str
) -> None:
    """Install ``runtime``'s adapters under the chat's install lock, once per container.

    Each adapter is root's and no one else can write it, in the image's
    root-owned ``/usr/local``, so no member can change it, and root changes it
    only here, to the content this host ships. Once found with that content
    in the container ``spec``'s last proof verified, it is not looked for
    there again, and a launch does not queue behind another install into the
    container for it. It is remembered with its content's digest and
    forgotten when it is replaced, so a host that ships other content still
    installs it.
    """
    proven = manager._proven.get(spec)
    missing = [
        helper
        for helper in _helpers(runtime)
        if not manager._proven.holds_helper(proven, helper.key)
    ]
    if missing:
        with install_lock(spec.container_name):
            _install(manager, spec, runtime, missing, found=(manager._proven, proven))


def _install(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    runtime: str,
    helpers: list[_Helper],
    *,
    found: tuple[ProvenContainers, ChatSandboxInspection | None] | None = None,
) -> None:
    """Probe each helper as root, and install the ones that differ.

    ``found`` remembers, for that container, each helper in place afterwards.
    """
    current = manager._require(spec)
    for helper in helpers:
        directory = helper.target.rsplit("/", 1)[0]
        probe = manager.exec_capture(
            spec,
            [
                "/bin/sh",
                "-c",
                'test "$(stat -c %u:%a "$1")" = "0:$3" && test "$(sha256sum "$1" | cut -d " " -f 1)" = "$2"',
                "sh",
                helper.target,
                helper.digest,
                f"{helper.mode:o}",
            ],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if probe.returncode == 0:
            if found is not None:
                found[0].note_helper(found[1], helper.key)
            continue
        if found is not None:
            found[0].forget_helpers(found[1], f"{helper.target}@")
        temporary = f"{directory}/.mcp-{secrets.token_hex(16)}"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            entry = tarfile.TarInfo(temporary.rsplit("/", 1)[-1])
            entry.mode = helper.mode
            entry.size = len(helper.content)
            # Explicit archive ownership works with dropped capabilities even
            # when a development runner's checkout belongs to a non-root UID.
            entry.uid = entry.gid = 0
            tar.addfile(entry, io.BytesIO(helper.content))
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
                helper.target,
                f"{helper.mode:o}",
            ],
            workdir="/",
            timeout_s=20,
            user_uid=0,
            user_gid=0,
        )
        if installed.returncode != 0:
            raise SandboxError(f"could not activate the {runtime} MCP adapter")
        if found is not None:
            found[0].note_helper(found[1], helper.key)
