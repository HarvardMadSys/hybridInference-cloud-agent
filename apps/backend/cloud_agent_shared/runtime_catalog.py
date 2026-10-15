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

    ``npm_package`` and ``binary`` name what a host downloads when an admin
    updates this agent from the Settings page, and the executable that package
    puts in ``bin/``. An empty package means the runtime cannot be updated that
    way: Kilo runs a build carrying a reviewed isolation patch, which the
    package on npm does not have.
    """

    name: str
    mcp: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    npm_package: str = ""
    binary: str = ""


CLAUDE_CODE = RuntimeDefinition(
    name="claude-code",
    mcp=True,
    reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
    npm_package="@anthropic-ai/claude-code",
    binary="claude",
)
CODEX = RuntimeDefinition(
    name="codex",
    mcp=True,
    reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    npm_package="@openai/codex",
    binary="codex",
)
PI = RuntimeDefinition(
    name="pi",
    mcp=True,
    reasoning_efforts=("minimal", "low", "medium", "high"),
    npm_package="@mariozechner/pi-coding-agent",
    binary="pi",
)
OPENCODE = RuntimeDefinition(
    name="opencode",
    mcp=True,
    reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    npm_package="opencode-ai",
    binary="opencode",
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


def downloadable_runtimes() -> dict[str, RuntimeDefinition]:
    """Runtimes an admin can update from npm, keyed by runtime ID."""
    return {definition.name: definition for definition in RUNTIME_CATALOG if definition.npm_package}


def runtime_reasoning_efforts() -> dict[str, list[str]]:
    """Return verified effort levels in each runtime's ordered vocabulary."""
    return {
        definition.name: list(definition.reasoning_efforts)
        for definition in RUNTIME_CATALOG
        if definition.reasoning_efforts
    }
