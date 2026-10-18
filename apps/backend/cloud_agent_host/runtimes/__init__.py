"""Agent CLI dialect adapters, exposed through a stable package API.

Each CLI module owns command construction and event normalization. ``base``
defines adapter and MCP contracts, ``events`` bounds normalized payloads, and
``preview`` batches partial replies. ``registry`` creates one adapter per run.
Shared discovery metadata lives in :mod:`cloud_agent_shared.runtime_catalog`
so the control plane can advertise capabilities without loading host code.

The five adapters preserve unrecognized output as ``raw`` events. Contract
fixtures under ``tests/fixtures/agent_runtime_streams/`` pin their observed CLI
formats; version pins and image wrappers live alongside ``deploy/sandbox/``.
"""

from .base import (
    EMPTY_RUNTIME_MCP_CONFIG,
    MCP_SERVERS_ENV,
    MCP_TOKEN_ENV,
    MERGED_MCP_SERVER_NAME,
    AgentRuntime,
    RuntimeCapabilities,
    RuntimeEffortUnsupportedError,
    RuntimeMCPConfig,
    RuntimeMCPUnavailableError,
    merged_mcp_server,
)
from .claude import ClaudeCodeRuntime
from .codex import CodexRuntime
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
from .generic import GenericRuntime
from .kilo import KiloRuntime
from .opencode import OpencodeRuntime
from .pi import PI_MCP_BRIDGE, PiRuntime
from .preview import REPLY_PREVIEW, ReplyPreview
from .registry import (
    get_runtime,
    registered_runtimes,
    runtime_reasoning_efforts,
    runtimes_supporting_mcp,
)

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
    "GenericRuntime",
    "KiloRuntime",
    "NormalizedEvent",
    "OpencodeRuntime",
    "PiRuntime",
    "ReplyPreview",
    "RuntimeCapabilities",
    "RuntimeEffortUnsupportedError",
    "RuntimeMCPConfig",
    "RuntimeMCPUnavailableError",
    "get_runtime",
    "merged_mcp_server",
    "registered_runtimes",
    "runtime_reasoning_efforts",
    "runtimes_supporting_mcp",
]
