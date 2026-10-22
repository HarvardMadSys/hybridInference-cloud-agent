"""Every registered runtime accepts the call its live session actually makes.

`KiloRuntime.prepare` once did not take `provides_isolation`, which its caller
passed unconditionally. Every Kilo job died with a `TypeError` before the
sandbox started — and nothing caught it, because each runtime's tests call it
with the arguments that runtime happens to want. A signature can only drift
from its caller if something checks them against each other.

So this calls `prepare_live` on every id in the registry with the exact keyword
set its session passes: `_JsonResidentSession._prepare_live` for the four JSON
protocols, and `ClaudeCodeLiveSession` for Claude Code. It is a compile-time
check written as a test, which is what Python leaves you when the sessions
reach a runtime through a table rather than a type.

The keyword lists are duplicated from the call sites on purpose. Importing
them, or deriving them by introspection, would make this test agree with the
sessions by construction — including when both are wrong together.
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent_host.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    get_runtime,
    registered_runtimes,
)

#: Exactly what `_JsonResidentSession._prepare_live` passes. Keep in step with
#: that call by hand — see the module docstring for why this is not imported.
_SESSION_KWARGS: dict[str, Any] = {
    "workdir": "/workspace",
    "model": "glm-5.1",
    "gateway_base_url": "http://gateway:8080",
    "credential": "agr.fake.fake",
    "provides_isolation": False,
    "mcp_config": EMPTY_RUNTIME_MCP_CONFIG,
    "reasoning_effort": None,
    "session_state_dir": "/agent-state/chat",
    "resume_session_id": None,
    "activation_dir": "/run/agent-activation/runtime",
}

#: Exactly what `ClaudeCodeLiveSession` passes.
_CLAUDE_SESSION_KWARGS: dict[str, Any] = {
    "workdir": "/workspace",
    "model": "",
    "gateway_base_url": "http://gateway:8080",
    "credential": "agr.fake.fake",
    "provides_isolation": False,
    "mcp_config": EMPTY_RUNTIME_MCP_CONFIG,
    "config_dir": "/agent-state/claude",
    "resume_session_id": None,
}


def _session_call(name: str) -> dict[str, Any]:
    return _CLAUDE_SESSION_KWARGS if name == "claude-code" else _SESSION_KWARGS


@pytest.mark.parametrize("name", registered_runtimes())
def test_prepare_live_accepts_its_sessions_call(name: str) -> None:
    """A registered runtime cannot refuse the only call it will ever get."""
    argv, env = get_runtime(name).prepare_live(**_session_call(name))
    assert argv, f"{name} produced no command"
    assert isinstance(env, dict)


@pytest.mark.parametrize("name", registered_runtimes())
def test_prepare_live_honours_isolation(name: str) -> None:
    """Passing the flag changes nothing structural — it must not crash.

    The value's *meaning* is per-runtime (the Codex session asks its
    app-server for `danger-full-access` inside a container backend, others
    ignore it). What is uniform is that both values are callable, so a runtime
    cannot accept the keyword and then reject one of its two possible values.
    """
    for isolation in (True, False):
        argv, _ = get_runtime(name).prepare_live(
            **{**_session_call(name), "provides_isolation": isolation}
        )
        assert argv
