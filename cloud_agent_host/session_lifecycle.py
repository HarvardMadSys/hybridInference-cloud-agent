"""Shared retained-session lifecycle primitives."""

from __future__ import annotations

from cloud_agent_host.sandbox import SandboxError

DEFAULT_CHAT_IDLE_TIMEOUT_S = 30 * 60.0
DEFAULT_SWEEP_INTERVAL_S = 5.0


class SessionBusy(SandboxError):
    """Another fenced owner is already using this chat member."""


class SessionLeaseLost(SandboxError):
    """A local operation carried a stale or unknown activation lease."""


__all__ = [
    "DEFAULT_CHAT_IDLE_TIMEOUT_S",
    "DEFAULT_SWEEP_INTERVAL_S",
    "SessionBusy",
    "SessionLeaseLost",
]
