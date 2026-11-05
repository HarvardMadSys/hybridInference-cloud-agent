"""MCP servers that run inside the task sandbox: what crosses from admission to the host.

A connection with location ``sandbox`` is not reached through the relay. The
control plane sends its launch, already checked against the sandbox launch
policy (``cloud_agent.mcp.launch``), with the claim; the host admits it to the
sandbox's MCP gateway (``deploy/sandbox/cloud-agent-mcp``) for that member,
and the member's CLI reaches the gateway as one more MCP server, ``sandbox``,
beside the relay's ``session``.

A server may use credentials, as every command the agent runs may use the
owner's environment:

- **Its own keys**, pasted with its configuration, are stored sealed and named
  in ``env`` as ``secret://NAME``; the claim carries their values in
  ``secret_env``, decrypted for that claim only.
- **The chat's environment**: ``${NAME}`` in ``args`` or ``env`` is filled on
  the host from the member's environment values.

Neither value goes into the gateway's admission, which every slot can read
(:meth:`SandboxMcpServer.admission`). The host hands them to the slot's runner
alone (``cloud_agent_host.sandbox_mcp``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent_shared.secret_redaction import MASKED

if TYPE_CHECKING:
    from collections.abc import Mapping

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
SECRET_PREFIX = "secret://"
#: ``${NAME}``, filled from the chat's environment where the server starts.
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,127})\}")
MAX_SECRET_BYTES = 32768


def environment_references(values: object) -> set[str]:
    """The ``${NAME}`` names a server's arguments and environment refer to."""
    texts = values if isinstance(values, (list, tuple)) else [values]
    names: set[str] = set()
    for text in texts:
        if isinstance(text, str):
            names.update(ENV_REFERENCE.findall(text))
    names.discard(BROWSER_CDP_PLACEHOLDER[2:-1])
    return names


class SandboxMcpServer(BaseModel):
    """One admitted sandbox server, as a claim carries it to the host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(pattern=ALIAS_PATTERN)
    command: Literal["npx", "uvx"]
    args: tuple[str, ...] = Field(default=(), max_length=MAX_SANDBOX_MCP_ARGS)
    env: dict[str, str] = Field(default_factory=dict, max_length=MAX_SANDBOX_MCP_ENV)
    #: The values of ``env``'s ``secret://NAME`` entries. Never in a repr, and
    #: never in the gateway's admission.
    secret_env: Annotated[dict[str, str], MASKED] = Field(
        default_factory=dict, max_length=MAX_SANDBOX_MCP_ENV, repr=False
    )

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
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        for name, item in value.items():
            if not re.fullmatch(ENV_NAME_PATTERN, name):
                raise ValueError(f"{name!r} is not an environment variable name")
            if "\0" in item or len(item) > 4096:
                raise ValueError(f"{name}: an environment value is too long or contains NUL")
        return value

    @model_validator(mode="after")
    def _every_secret_has_its_value(self) -> SandboxMcpServer:
        named = {
            item.removeprefix(SECRET_PREFIX)
            for item in self.env.values()
            if item.startswith(SECRET_PREFIX)
        }
        if set(self.secret_env) != named:
            raise ValueError(f"{self.alias}: secret values do not match its secret:// entries")
        for name, item in self.secret_env.items():
            if "\0" in item or len(item.encode()) > MAX_SECRET_BYTES:
                raise ValueError(f"{self.alias}: the value of {name} is too long or contains NUL")
        return self

    def admission(self) -> dict[str, object]:
        """What the gateway's admission names this server with: no secret value."""
        return {
            "alias": self.alias,
            "command": self.command,
            "args": list(self.args),
            "env": dict(sorted(self.env.items())),
        }

    def secrets(self) -> dict[str, str]:
        """This server's own keys by name, for the output redactor."""
        return {f"sandbox-mcp:{self.alias}:{name}": value for name, value in self.secret_env.items()}

    def runner_secrets(self, environment: Mapping[str, str]) -> dict[str, str]:
        """The values only the slot's runner gets: its keys, and referenced environment.

        The environment values are the chat's own that ``${NAME}`` names.
        """
        referenced = environment_references([*self.args, *self.env.values()])
        values = {name: environment[name] for name in sorted(referenced) if name in environment}
        return {**values, **self.secret_env}


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
