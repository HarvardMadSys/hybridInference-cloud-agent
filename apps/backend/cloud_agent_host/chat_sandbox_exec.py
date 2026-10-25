"""Which container a retained chat command execs into, and when to inspect it first.

A container's labels, mounts, tmpfs, runtime and image never change for its
id, and Docker refuses an exec into one that is stopped, paused or removed.
So once a start or a network proof has verified the container for a spec, a
bounded command for that spec execs into it by id without another ``docker
container inspect``. A refused exec reads like a failed command, so it is
inspected afterwards: it raises what an inspection first would have raised,
and a same-name replacement is refused rather than entered. A spawn is still
inspected first, since its refusal would surface only when it exits, and so is
every lifecycle transition and archive read.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, TypeVar

from cloud_agent_host.chat_sandbox_models import RetainedChatLost
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    import subprocess

    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection, ChatSandboxSpec

NOT_RUNNING = "chat sandbox must be started before exec"

Result = TypeVar("Result", bound="subprocess.CompletedProcess[Any]")


class ProvenContainers:
    """The container each spec's last start or network proof verified, by name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proven: dict[str, tuple[ChatSandboxSpec, ChatSandboxInspection]] = {}

    def record(
        self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection
    ) -> ChatSandboxInspection:
        """Remember the container just verified for ``spec``, and return it."""
        with self._lock:
            self._proven[spec.container_name] = (spec, inspection)
        return inspection

    def forget(self, spec: ChatSandboxSpec) -> None:
        """Drop the container held for ``spec``'s name, which is being created or removed."""
        with self._lock:
            self._proven.pop(spec.container_name, None)

    def get(self, spec: ChatSandboxSpec) -> ChatSandboxInspection | None:
        """The container verified for exactly ``spec``, if any."""
        with self._lock:
            proven = self._proven.get(spec.container_name)
        return proven[1] if proven is not None and proven[0] == spec else None


def exec_target(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    expected_container_id: str | None = None,
    *,
    not_running: str | None = NOT_RUNNING,
) -> tuple[ChatSandboxInspection, ChatSandboxInspection | None]:
    """Return the container to exec into, and it again if it was not inspected now.

    ``expected_container_id`` fences an archive or lifecycle read on that exact
    container, which is always inspected first. ``not_running=None`` leaves
    refusing a stopped container that was inspected now to the caller.
    """
    proven = None if expected_container_id is not None else manager._proven.get(spec)
    if proven is not None:
        return proven, proven
    current = (
        manager._require_exact(spec, expected_container_id)
        if expected_container_id is not None
        else manager._require(spec)
    )
    if not_running is not None and not current.running:
        raise SandboxError(not_running)
    return current, None


def checked_exec(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    proven: ChatSandboxInspection | None,
    result: Result,
    *,
    not_running: str = NOT_RUNNING,
) -> Result:
    """Return an exec's result, first explaining one into a proven container that failed.

    The container still running under the proven id ran the command, and its
    result stands. Otherwise Docker refused the exec, and the error is the one
    an inspection first would have raised.
    """
    if proven is not None and result.returncode != 0:
        current = manager._require(spec)
        if current.container_id != proven.container_id:
            raise RetainedChatLost("chat sandbox container identity changed")
        if not current.running:
            raise SandboxError(not_running)
    return result


def spawn_target(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, *, not_running: str = NOT_RUNNING
) -> ChatSandboxInspection:
    """Inspect the container a process is spawned into, refusing a same-name replacement."""
    current = manager._require(spec)
    proven = manager._proven.get(spec)
    if proven is not None and proven.container_id != current.container_id:
        raise RetainedChatLost("chat sandbox container identity changed")
    if not current.running:
        raise SandboxError(not_running)
    return current
