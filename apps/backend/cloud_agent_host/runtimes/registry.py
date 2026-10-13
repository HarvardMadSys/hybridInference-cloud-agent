"""Host adapter registry and instance creation for each supported CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .claude import ClaudeCodeRuntime
from .codex import CodexRuntime
from .kilo import KiloRuntime
from .opencode import OpencodeRuntime
from .pi import PiRuntime

if TYPE_CHECKING:
    from .base import AgentRuntime


_REGISTRY: dict[str, type[AgentRuntime]] = {
    ClaudeCodeRuntime.name: ClaudeCodeRuntime,
    CodexRuntime.name: CodexRuntime,
    PiRuntime.name: PiRuntime,
    OpencodeRuntime.name: OpencodeRuntime,
    KiloRuntime.name: KiloRuntime,
}


def registered_runtimes() -> list[str]:
    """Runtime ids this deployment can actually run.

    The composer offers these rather than a hardcoded list, so the picker
    cannot advertise a runtime the backend would refuse at job creation.
    """
    return sorted(_REGISTRY)


def runtimes_supporting_mcp() -> list[str]:
    """Runtime ids that can be given MCP servers safely.

    Used by the composer and by job creation, so a job never reaches a sandbox
    having been promised tools its harness will not load.
    """
    return sorted(
        name for name, runtime_cls in _REGISTRY.items() if runtime_cls().capabilities().mcp
    )


def runtime_reasoning_efforts() -> dict[str, list[str]]:
    """Effort levels each registered runtime can express, in its own words.

    A runtime that cannot express one is absent rather than empty-listed, so
    the composer reaches "no control here" the same way whether the runtime
    cannot say it or the platform is talking to something too old to answer.

    Used by the composer and by job creation, so a turn is never created
    carrying a depth its harness has no flag for — the MCP argument applied to
    the other thing a runtime either can or cannot do.
    """
    efforts: dict[str, list[str]] = {}
    for name, runtime_cls in _REGISTRY.items():
        declared = runtime_cls().capabilities().reasoning_efforts
        if declared:
            efforts[name] = list(declared)
    return efforts


def get_runtime(name: str) -> AgentRuntime:
    """Return one of the five live-session runtime adapters.

    Unknown ids fail closed. A command template cannot opt a one-shot process
    into the retained topology because it cannot attest native resume, live
    transport, immediate fork, or concurrent shared-workspace behavior.
    """
    runtime_cls = _REGISTRY.get(name)
    if runtime_cls is not None:
        return runtime_cls()
    known = ", ".join(sorted(_REGISTRY))
    raise KeyError(f"Unknown agent runtime {name!r}. Known: {known}")
