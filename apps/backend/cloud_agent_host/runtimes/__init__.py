"""Agent CLI dialect adapters, exposed through a stable package API.

Each CLI's knowledge lives in one package: ``claude``, ``codex``, ``pi``, and
``http_bridge`` for OpenCode and Kilo, which share its launch. A package's
``adapter`` builds the CLI's resident live process (Claude's and pi's also
normalize the events it writes); its ``session`` speaks the CLI's live
protocol over :mod:`cloud_agent_host.drivers`; its ``member`` prepares a
member's private state and native Fork and defines the CLI's launch steps
(``member_launch``); MCP and plugin configuration sit beside them. ``base``
defines adapter and MCP contracts, ``events`` bounds normalized payloads, and
``preview`` batches partial replies. ``registry`` creates one adapter per run,
and ``live`` maps each CLI to its session and launch steps. Shared discovery
metadata lives in :mod:`cloud_agent_shared.runtime_catalog` so the control
plane can advertise capabilities without loading host code.

This module exports the dialect layer only. The sessions, ``live``,
``member_launch`` and the ``member`` modules build on :mod:`cloud_agent_host.drivers`, which imports this
API, so they are imported by their own module names.

The adapters preserve unrecognized output as ``raw`` events. Contract fixtures
under ``tests/fixtures/agent_runtime_streams/`` pin their observed CLI formats;
version pins and image wrappers live alongside ``deploy/sandbox/``.
"""

from .base import (
    EMPTY_RUNTIME_MCP_CONFIG,
    MCP_SERVERS_ENV,
    MCP_TOKEN_ENV,
    MERGED_MCP_SERVER_NAME,
    AgentRuntime,
    ExactBaseUrl,
    MessagesBaseUrl,
    MessagesViaChatUrl,
    ResponsesViaChatUrl,
    RuntimeCapabilities,
    RuntimeConnectionUnsupportedError,
    RuntimeEffortUnsupportedError,
    RuntimeMCPConfig,
    RuntimeMCPUnavailableError,
    TranslatedBaseUrl,
    compat_launch,
    connection_base_url,
    merged_mcp_server,
    meter_launch,
    meters_direct_plan,
)
from .claude.adapter import ClaudeCodeRuntime
from .codex.adapter import CodexRuntime
from .events import (
    DIFF,
    ERROR,
    LIFECYCLE,
    MAX_INIT_TOOLS,
    MESSAGE,
    RAW,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    USAGE,
    NormalizedEvent,
)
from .http_bridge.kilo import KiloRuntime
from .http_bridge.opencode import OpencodeRuntime
from .pi.adapter import PI_MCP_BRIDGE, PiRuntime
from .preview import REPLY_PREVIEW, ReplyPreview
from .registry import get_runtime, registered_runtimes

__all__ = [
    "DIFF",
    "EMPTY_RUNTIME_MCP_CONFIG",
    "ERROR",
    "LIFECYCLE",
    "MAX_INIT_TOOLS",
    "MCP_SERVERS_ENV",
    "MCP_TOKEN_ENV",
    "MERGED_MCP_SERVER_NAME",
    "MESSAGE",
    "PI_MCP_BRIDGE",
    "RAW",
    "REPLY_PREVIEW",
    "THINKING",
    "TOOL_RESULT",
    "TOOL_USE",
    "USAGE",
    "AgentRuntime",
    "ClaudeCodeRuntime",
    "CodexRuntime",
    "ExactBaseUrl",
    "KiloRuntime",
    "MessagesBaseUrl",
    "MessagesViaChatUrl",
    "NormalizedEvent",
    "OpencodeRuntime",
    "PiRuntime",
    "ReplyPreview",
    "ResponsesViaChatUrl",
    "RuntimeCapabilities",
    "RuntimeConnectionUnsupportedError",
    "RuntimeEffortUnsupportedError",
    "RuntimeMCPConfig",
    "RuntimeMCPUnavailableError",
    "TranslatedBaseUrl",
    "compat_launch",
    "connection_base_url",
    "get_runtime",
    "merged_mcp_server",
    "meter_launch",
    "meters_direct_plan",
    "registered_runtimes",
]
