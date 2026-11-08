"""A ``sandbox`` connection, as a claim hands it to the host.

The relay never dials one: the host admits it to the sandbox's MCP gateway
for the member that runs the claimed turn (cloud_agent_shared.sandbox_mcp).
Its launch was checked when it was saved; it is checked again here, against
the same policy, because a row is only as current as the rules it was saved
under, and the gateway refuses anything the policy would.

Its keys (``secret://NAME`` values, stored sealed) are opened here, for this
claim only, and travel with it to the host, which hands them to the slot's
runner alone. A new key shorter than an environment Secret may be is refused
when it is saved (`short_key_refusal`); one stored before that rule still
opens here, and runs unmasked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from cloud_agent.crypto.credential_cipher import CredentialCipherError
from cloud_agent.logging_utils import get_logger
from cloud_agent.mcp.launch import LaunchPlanError, build_launch_plan
from cloud_agent.mcp.models import (
    AuthKind,
    ConnectionErrorKind,
    ConnectionStatus,
    ExecutionLocation,
    Transport,
)
from cloud_agent.mcp.registry import mcp_cipher, resolve_connection
from cloud_agent_shared.sandbox_mcp import (
    ARTIFACTS_DIR,
    BROWSER_CDP_PLACEHOLDER,
    SECRET_PREFIX,
    SandboxMcpServer,
)
from cloud_agent_shared.secret_redaction import MIN_SECRET_CHARS

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
#: What testing one says when its launch no longer passes the sandbox policy.
REFUSED_MESSAGE = "This command can no longer run in a chat: pin an exact npx or uvx package."


async def checked_in_place(store: Any, row: Mapping[str, Any], *, record: bool) -> tuple[bool, str]:
    """Test a sandbox connection the only way the control plane can; whether it passed.

    Nothing here can reach a server that starts in a chat's sandbox. What can
    be checked is what a claim checks: its launch against the sandbox policy,
    and that its sealed keys open. A connection that passes is ready to turn on
    in a chat (``active``), which a saved one never was: ``draft`` means never
    tested, and nothing else would ever test it. One that fails is ``error``
    and says why. ``record`` publishes the verdict, fenced to the revisions
    read, as every other test does.
    """
    resolved = await resolve_connection(store, row=row, load_snapshot=False)
    passed = sandbox_mcp_server(resolved.connection, "check") is not None
    if record:
        await store.set_mcp_connection_status(
            connection_id=int(row["id"]),
            status=(ConnectionStatus.ACTIVE if passed else ConnectionStatus.ERROR).value,
            error_kind=(
                ConnectionErrorKind.NONE if passed else ConnectionErrorKind.LAUNCH_FAILED
            ).value,
            error_detail=None if passed else REFUSED_MESSAGE,
            expected_config_revision=int(row["config_revision"]),
            expected_authorization_epoch=int(row["authorization_epoch"]),
            expected_credential_version=int(row["credential_version"]),
            expected_capability_revision=int(row["capability_revision"]),
        )
    return passed, TEST_MESSAGE if passed else REFUSED_MESSAGE


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


def short_key_refusal(location: ExecutionLocation, keys: Mapping[str, str]) -> str | None:
    """Why a program's keys, about to be sealed, cannot be saved, or ``None``.

    The runner masks a program's keys in the turn's durable output only from
    ``MIN_SECRET_CHARS`` up (``SandboxMcpServer.secrets``), as it masks an
    environment Secret, so a shorter key is refused on the same terms. Asked
    only when a key is saved: a short key stored before this rule still runs.
    """
    if location is not ExecutionLocation.SANDBOX:
        return None
    for name in sorted(keys):
        value = keys[name]
        if value and len(value.strip()) < MIN_SECRET_CHARS:
            return (
                f"Secret {name} must contain at least {MIN_SECRET_CHARS} characters; "
                "a shorter value cannot be masked in the chat's output"
            )
    return None
