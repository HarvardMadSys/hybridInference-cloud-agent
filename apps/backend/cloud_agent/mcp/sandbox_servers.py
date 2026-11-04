"""A ``sandbox`` connection, as a claim hands it to the host.

The relay never dials one: the host admits it to the sandbox's MCP gateway
for the member that runs the claimed turn (cloud_agent_shared.sandbox_mcp).
Its launch was checked when it was saved; it is checked again here, against
the same policy, because a row is only as current as the rules it was saved
under, and the gateway refuses anything the policy would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.launch import LaunchPlanError, build_launch_plan
from cloud_agent.mcp.models import AuthKind, ConnectionStatus, ExecutionLocation, Transport
from cloud_agent_shared.sandbox_mcp import ARTIFACTS_DIR, BROWSER_CDP_PLACEHOLDER, SandboxMcpServer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cloud_agent.mcp.models import McpConnection, ThreadBinding
    from cloud_agent.mcp.registry import ResolvedConnection

logger = get_logger(__name__)

_SANDBOX_NOTE = (
    "Runs inside each chat's sandbox with the agent's own access, so its tools are "
    "available without approval. It drives the browser the chat and its Forks share."
)

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
TEST_MESSAGE = (
    "This server runs inside each chat's sandbox. It starts, and is checked, when a chat "
    "first lists its tools; it cannot be reached from here."
)


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
            }
        )
    except (LaunchPlanError, ValidationError, ValueError) as exc:
        logger.warning(
            "agent_mcp_sandbox_server_refused",
            extra={
                "event": "agent_mcp_sandbox_server_refused",
                "connection_id": connection.connection_id,
                "detail": str(exc)[:300],
            },
        )
        return None


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


def refusal(location: ExecutionLocation, auth_kind: AuthKind | str, *, secret: bool) -> str | None:
    """Why a connection cannot be saved for the sandbox, or ``None``.

    The sandbox also runs repository code, so nothing secret goes there: no
    credential for the server to send, and no secret environment value.
    """
    if location is not ExecutionLocation.SANDBOX:
        return None
    if AuthKind(auth_kind) is not AuthKind.NONE or secret:
        return (
            "a server that runs in the sandbox cannot be given a credential, because the "
            "sandbox also runs the repository's code; add a server that needs one as remote"
        )
    return None
