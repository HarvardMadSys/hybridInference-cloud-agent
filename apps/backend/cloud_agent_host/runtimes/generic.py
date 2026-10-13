"""Template-based headless adapter with raw event fallback."""

from __future__ import annotations

import shlex

from .base import EMPTY_RUNTIME_MCP_CONFIG, AgentRuntime, RuntimeMCPConfig
from .events import RAW, NormalizedEvent


class GenericRuntime(AgentRuntime):
    """Tier 2: run any headless CLI and stream its output as raw events.

    The point of Tier 2 is that a new agent costs nothing to support: it gets
    a working log tail immediately, and only earns a normalizing adapter once
    it proves worth the maintenance.
    """

    name = "generic"

    def __init__(self, command_template: str, *, binary: str = "") -> None:
        """Build from a shell-style template using ``{prompt}`` / ``{model}``."""
        self.command_template = command_template
        self.binary = binary or shlex.split(command_template)[0]

    def prepare(
        self,
        *,
        workdir: str,
        task_prompt: str,
        model: str,
        gateway_base_url: str,
        credential: str,
        provides_isolation: bool = False,
        mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
        reasoning_effort: str | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Expand the template into argv without ever invoking a shell.

        A Tier 2 runtime is any headless CLI, so there is no flag this adapter
        could know to pass and no way to shut out a repository's own config
        *unless* an image-shipped wrapper supplies one. That is what
        :meth:`_accept_mcp` decides, and why the base answer is a refusal: a
        non-empty set is dropped tools or someone else's servers, and neither
        is a thing to discover mid-run.
        """
        self.effort_args(reasoning_effort)
        argv = [
            part.replace("{prompt}", task_prompt).replace("{model}", model)
            for part in shlex.split(self.command_template)
        ]
        base = gateway_base_url.rstrip("/").removesuffix("/v1")
        env = {
            "OPENAI_BASE_URL": f"{base}/v1",
            "OPENAI_API_KEY": credential,
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_API_KEY": credential,
            "MCP_TOOL_TIMEOUT": "1560000",
        }
        self._accept_mcp(mcp_config, gateway_base_url=gateway_base_url, env=env)
        return argv, env

    def parse_event(self, line: str) -> NormalizedEvent | None:
        """Emit every non-empty line as a raw event."""
        line = line.rstrip()
        if not line:
            return None
        return NormalizedEvent(RAW, {"text": line[:4000]})
