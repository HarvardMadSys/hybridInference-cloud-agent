"""A ``sandbox`` connection, as a claim hands it to the host.

The relay never dials one: the host admits it to the sandbox's MCP gateway
for the member that runs the claimed turn (cloud_agent_shared.sandbox_mcp).
Its launch was checked when it was saved; it is checked again here, against
the same policy, because a row is only as current as the rules it was saved
under, and the gateway refuses anything the policy would.

Its keys (``secret://NAME`` values, stored sealed) are opened here, for this
claim only, and travel with it to the host, which hands them to the slot's
runner alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from cloud_agent.credential_cipher import CredentialCipherError
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.launch import LaunchPlanError, build_launch_plan
from cloud_agent.mcp.models import AuthKind, ConnectionStatus, ExecutionLocation, Transport
from cloud_agent.mcp.registry import mcp_cipher
from cloud_agent_shared.sandbox_mcp import (
    ARTIFACTS_DIR,
    BROWSER_CDP_PLACEHOLDER,
    SECRET_PREFIX,
    SandboxMcpServer,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloud_agent.mcp.models import McpConnection, ThreadBinding
    from cloud_agent.mcp.registry import ResolvedConnection

logger = get_logger(__name__)

_SANDBOX_NOTE = "It drives the browser the chat and its Forks share."

#: Presets for the sandbox browser, offered beside the remote templates. Each
#: binds a browser MCP server to the managed browser through the placeholder
#: the gateway fills in, and points its files at the artifacts directory,
#: which is kept out of Git. Versions are exact, as the sandbox requires.
TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "name": "playwright-sandbox",
        "label": "Playwright browser",
        "description": (
            "Open the task's own localhost app in the sandbox browser, click, fill in "
            "forms, read the page and take screenshots."
        ),
        "url": "",
        "transport": Transport.STDIO.value,
        "auth_kind": AuthKind.NONE.value,
        "location": ExecutionLocation.SANDBOX.value,
        "launch": {
            "command": "npx",
            "args": [
                "-y",
                "@playwright/mcp@0.0.83",
                "--cdp-endpoint",
                BROWSER_CDP_PLACEHOLDER,
                "--output-dir",
                f"{ARTIFACTS_DIR}/playwright-mcp",
            ],
        },
        "documentation_url": "https://github.com/microsoft/playwright-mcp",
        "notes": [_SANDBOX_NOTE],
    },
    {
        "name": "chrome-devtools-sandbox",
        "label": "Chrome DevTools",
        "description": (
            "Debug pages in the sandbox browser: console messages, network requests, "
            "performance traces and screenshots."
        ),
        "url": "",
        "transport": Transport.STDIO.value,
        "auth_kind": AuthKind.NONE.value,
        "location": ExecutionLocation.SANDBOX.value,
        "launch": {
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@1.11.0", "--wsEndpoint", BROWSER_CDP_PLACEHOLDER],
        },
        "documentation_url": "https://github.com/ChromeDevTools/chrome-devtools-mcp",
        "notes": [_SANDBOX_NOTE],
    },
)

#: What testing a sandbox connection says: it starts in a chat's sandbox, so
#: it is checked there, when a chat first lists its tools.
TEST_MESSAGE = "This server starts with a chat. Its tools appear here once a chat has used it."


def sandbox_mcp_server(connection: McpConnection, alias: str) -> SandboxMcpServer | None:
    """The host's copy of a sandbox connection, or ``None`` for any other location.

    A sandbox connection that no longer passes the policy is left out, with a
    log line, rather than sent to the relay, which could not reach it either.
    """
    if connection.config.location is not ExecutionLocation.SANDBOX:
        return None
    launch = connection.config.launch
    try:
        if launch is None:
            raise LaunchPlanError("no launch")
        build_launch_plan(
            launch, connection_id=connection.connection_id, location=ExecutionLocation.SANDBOX
        )
        return SandboxMcpServer.model_validate(
            {
                "alias": alias,
                "command": launch.command,
                "args": launch.args,
                "env": dict(launch.env),
                "secret_env": _opened(launch.env, launch.sealed_env),
            }
        )
    except (LaunchPlanError, ValidationError, ValueError, CredentialCipherError) as exc:
        logger.warning(
            "agent_mcp_sandbox_server_refused",
            extra={
                "event": "agent_mcp_sandbox_server_refused",
                "connection_id": connection.connection_id,
                "detail": str(exc)[:300],
            },
        )
        return None


def _opened(env: Mapping[str, str], sealed: Mapping[str, str]) -> dict[str, str]:
    """The values of ``env``'s ``secret://NAME`` entries, decrypted for this claim."""
    names = {
        value.removeprefix(SECRET_PREFIX)
        for value in env.values()
        if value.startswith(SECRET_PREFIX)
    }
    if not names:
        return {}
    missing = sorted(names - set(sealed))
    if missing:
        raise LaunchPlanError(f"no stored value for {', '.join(missing)}")
    cipher = mcp_cipher()
    return {name: cipher.decrypt(sealed[name]) for name in sorted(names)}


def split_bindings(
    bindings: Sequence[tuple[ResolvedConnection, ThreadBinding]],
) -> tuple[list[str], list[SandboxMcpServer]]:
    """A conversation's usable bindings: relay aliases, and sandbox servers.

    A binding counts when it is enabled and its connection is usable and not
    failing. A sandbox server is not behind the relay, so its alias is not
    among the relay's, and its launch goes to the host instead.
    """
    aliases: list[str] = []
    servers: list[SandboxMcpServer] = []
    for resolved, binding in bindings:
        if not (
            binding.enabled
            and resolved.usable
            and resolved.connection.status is not ConnectionStatus.ERROR
        ):
            continue
        if resolved.connection.config.location is not ExecutionLocation.SANDBOX:
            aliases.append(binding.alias)
            continue
        server = sandbox_mcp_server(resolved.connection, binding.alias)
        if server is not None:
            servers.append(server)
    return aliases, servers


def refusal(location: ExecutionLocation, auth_kind: AuthKind | str) -> str | None:
    """Why a connection cannot be saved for the sandbox, or ``None``.

    A program's keys are environment values (sealed with its launch); a
    sign-in, a header the platform sends, has nothing to send to.
    """
    if location is not ExecutionLocation.SANDBOX:
        return None
    if AuthKind(auth_kind) is not AuthKind.NONE:
        return "a command takes its keys as environment values, not a sign-in"
    return None
