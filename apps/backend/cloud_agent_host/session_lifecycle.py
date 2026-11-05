"""Shared retained-session lifecycle primitives."""

from __future__ import annotations

from cloud_agent_host.sandbox import SandboxError

DEFAULT_CHAT_IDLE_TIMEOUT_S = 30 * 60.0
DEFAULT_SWEEP_INTERVAL_S = 5.0


class SessionBusy(SandboxError):
    """Another fenced owner is already using this chat member."""


class SessionLeaseLost(SandboxError):
    """A local operation carried a stale or unknown activation lease."""


class StaleStartFence(SessionLeaseLost):
    """A request would start a stopped group under a start fence already sealed.

    A checkpoint of the group's last stop may already be the one it moves
    with, and it names that fence: a start under it would change the group
    after its checkpoint was taken. The control plane fences again and retries.
    """


__all__ = [
    "DEFAULT_CHAT_IDLE_TIMEOUT_S",
    "DEFAULT_SWEEP_INTERVAL_S",
    "SessionBusy",
    "SessionLeaseLost",
    "StaleStartFence",
]
