"""Refresh immutable MCP adapters in retained, pre-deployment containers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cloud_agent_host.trusted_files import install_trusted_file

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
    for name in _HELPERS.get(runtime, ()):
        executable = not name.endswith(".ts")
        install_trusted_file(
            manager,
            spec,
            directory="/usr/local/bin" if executable else "/usr/local/libexec",
            name=name,
            content=(source_root / name).read_bytes(),
            mode=0o555 if executable else 0o444,
            what=f"{runtime} MCP adapter",
        )
