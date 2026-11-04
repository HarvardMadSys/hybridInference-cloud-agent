"""MCP servers that run inside the task sandbox: what crosses from admission to the host.

A connection with location ``sandbox`` is not reached through the relay. The
control plane sends its launch, already checked against the sandbox launch
policy (``cloud_agent.mcp.launch``), with the claim; the host admits it to the
sandbox's MCP gateway (``deploy/sandbox/cloud-agent-mcp``) for that member,
and the member's CLI reaches the gateway as one more MCP server, ``sandbox``,
beside the relay's ``session``.

Nothing here is a secret. A sandbox server gets no ``secret://`` value: the
sandbox also runs repository code, so a credential the server needed would
be one the repository could read. A server that needs one stays remote.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The gateway's fixed loopback address, and the name the CLIs know it by.
SANDBOX_MCP_URL = "http://127.0.0.1:47801/mcp"
SANDBOX_MCP_SERVER_NAME = "sandbox"
#: The package runners a sandbox server may start with: each installs one
#: pinned package. The gateway refuses anything else whatever it is sent.
SANDBOX_MCP_COMMANDS = ("npx", "uvx")
#: Replaced, when the server starts, with the sandbox browser's CDP URL.
BROWSER_CDP_PLACEHOLDER = "${CLOUD_AGENT_BROWSER_CDP}"
#: Where a sandbox server should write its files: kept out of Git, shown in
#: the workspace files (``CLOUD_AGENT_ARTIFACTS_DIR`` in the image).
ARTIFACTS_DIR = "/workspace/.cloud-agent-artifacts"
MAX_SANDBOX_MCP_SERVERS = 16
MAX_SANDBOX_MCP_ARGS = 64
MAX_SANDBOX_MCP_ENV = 64
#: As ``cloud_agent.mcp.models.ALIAS_PATTERN``: the alias the relay would give it.
ALIAS_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"
ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"


class SandboxMcpServer(BaseModel):
    """One admitted sandbox server, as a claim carries it to the host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(pattern=ALIAS_PATTERN)
    command: Literal["npx", "uvx"]
    args: tuple[str, ...] = Field(default=(), max_length=MAX_SANDBOX_MCP_ARGS)
    env: dict[str, str] = Field(default_factory=dict, max_length=MAX_SANDBOX_MCP_ENV)

    @field_validator("alias")
    @classmethod
    def _no_separator(cls, value: str) -> str:
        if "__" in value:
            raise ValueError("a sandbox MCP alias cannot contain '__'")
        return value

    @field_validator("args")
    @classmethod
    def _bounded_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if sum(len(arg) for arg in value) > 8192 or any("\0" in arg for arg in value):
            raise ValueError("sandbox MCP arguments are too long or contain NUL")
        return value

    @field_validator("env")
    @classmethod
    def _plain_env(cls, value: dict[str, str]) -> dict[str, str]:
        for name, item in value.items():
            if not re.fullmatch(ENV_NAME_PATTERN, name):
                raise ValueError(f"{name!r} is not an environment variable name")
            if item.startswith("secret://") or "\0" in item or len(item) > 4096:
                raise ValueError(f"{name}: a sandbox MCP server takes no secret values")
        return value

    def wire(self) -> dict[str, object]:
        """What the gateway's admission names this server with."""
        return {
            "alias": self.alias,
            "command": self.command,
            "args": list(self.args),
            "env": dict(sorted(self.env.items())),
        }


def parse_sandbox_mcp_servers(raw: object) -> tuple[SandboxMcpServer, ...]:
    """The servers a claim names, validated; absent from an older control plane."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_SANDBOX_MCP_SERVERS:
        raise ValueError(f"sandbox_mcp_servers must be a list of at most {MAX_SANDBOX_MCP_SERVERS}")
    servers = tuple(SandboxMcpServer.model_validate(item) for item in raw)
    if len({server.alias for server in servers}) != len(servers):
        raise ValueError("sandbox MCP aliases must be distinct")
    return servers
