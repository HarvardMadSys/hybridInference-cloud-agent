"""Refresh immutable MCP adapters in retained, pre-deployment containers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloud_agent_host.install_locks import install_lock
from cloud_agent_host.trusted_files import install_trusted_file

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
    for helper in helpers:
        if found is not None:
            found[0].forget_helpers(found[1], f"{helper.target}@")
        directory, name = helper.target.rsplit("/", 1)
        install_trusted_file(
            manager,
            spec,
            directory=directory,
            name=name,
            content=helper.content,
            mode=helper.mode,
            what=f"{runtime} MCP adapter",
        )
        if found is not None:
            found[0].note_helper(found[1], helper.key)
