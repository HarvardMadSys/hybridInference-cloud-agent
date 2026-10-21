"""Host adapter registry and instance creation for each supported CLI."""

from __future__ import annotations

from .base import AgentRuntime  # noqa: TC001 — Public factory annotation introspection.
from .claude import ClaudeCodeRuntime
from .codex import CodexRuntime
from .kilo import KiloRuntime
from .opencode import OpencodeRuntime
from .pi import PiRuntime

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
