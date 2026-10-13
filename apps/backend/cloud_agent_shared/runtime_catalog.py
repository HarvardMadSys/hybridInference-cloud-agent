"""Runtime discovery vocabulary shared by admission and host adapters.

This catalog declares runtime IDs, mediated MCP support, and reasoning-effort
levels in each CLI's vocabulary. It contains no command builders, output
parsers, or host imports. The host adapters consume these declarations, and
contract tests check the catalog against the adapters actually registered.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeDefinition:
    """The runtime facts needed before a job reaches a host.

    Efforts are ordered weakest to strongest. An empty tuple means the CLI
    cannot express an effort, so callers omit that control entirely.
    """

    name: str
    mcp: bool = False
    reasoning_efforts: tuple[str, ...] = ()


CLAUDE_CODE = RuntimeDefinition(
    name="claude-code",
    mcp=True,
    reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
)
CODEX = RuntimeDefinition(
    name="codex",
    mcp=True,
    reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
)
PI = RuntimeDefinition(
    name="pi",
    mcp=True,
    reasoning_efforts=("minimal", "low", "medium", "high"),
)
OPENCODE = RuntimeDefinition(
    name="opencode",
    mcp=True,
    reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
)
KILO = RuntimeDefinition(
    name="kilo",
    mcp=True,
    reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
)

RUNTIME_CATALOG = (CLAUDE_CODE, CODEX, PI, OPENCODE, KILO)


def registered_runtimes() -> list[str]:
    """Return the runtime IDs this deployment supports."""
    return sorted(definition.name for definition in RUNTIME_CATALOG)


def runtimes_supporting_mcp() -> list[str]:
    """Return runtime IDs with an isolated platform-mediated MCP path."""
    return sorted(definition.name for definition in RUNTIME_CATALOG if definition.mcp)


def runtime_reasoning_efforts() -> dict[str, list[str]]:
    """Return verified effort levels in each runtime's ordered vocabulary."""
    return {
        definition.name: list(definition.reasoning_efforts)
        for definition in RUNTIME_CATALOG
        if definition.reasoning_efforts
    }
