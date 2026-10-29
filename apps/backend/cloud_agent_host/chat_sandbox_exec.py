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
from cloud_agent_host.install_locks import install_lock
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection, ChatSandboxSpec

NOT_RUNNING = "chat sandbox must be started before exec"

Result = TypeVar("Result", bound="subprocess.CompletedProcess[Any]")


class ProvenContainers:
    """The container each spec's last start or network proof verified, by name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proven: dict[str, tuple[ChatSandboxSpec, ChatSandboxInspection]] = {}
        #: Root-owned helpers found in place, as (container id, key) pairs. A key
        #: is the helper's path, followed by its content's digest when a host
        #: could install other content there.
        self._helpers: set[tuple[str, str]] = set()

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
            dropped = self._proven.pop(spec.container_name, None)
            if dropped is not None and self._helpers:
                gone = dropped[1].container_id
                self._helpers = {held for held in self._helpers if held[0] != gone}

    def get(self, spec: ChatSandboxSpec) -> ChatSandboxInspection | None:
        """The container verified for exactly ``spec``, if any."""
        with self._lock:
            proven = self._proven.get(spec.container_name)
        return proven[1] if proven is not None and proven[0] == spec else None

    def holds_helper(self, proven: ChatSandboxInspection | None, path: str) -> bool:
        """Whether ``path`` was found installed in exactly the container ``proven``."""
        if proven is None:
            return False
        with self._lock:
            return (proven.container_id, path) in self._helpers

    def note_helper(self, proven: ChatSandboxInspection | None, path: str) -> None:
        """Remember a root-owned helper just found installed in the container ``proven``.

        Only a platform exec as root can change it again: a member is never
        root, and a container id is never reused. So it is not checked again
        for that container, and a turn does not queue behind a long install
        into it just to ask. ``proven`` is the container held before the check
        ran; were it replaced meanwhile, the check ran in the replacement, and
        what this records is an id nothing will look up again.
        """
        if proven is not None:
            with self._lock:
                self._helpers.add((proven.container_id, path))

    def forget_helpers(self, proven: ChatSandboxInspection | None, prefix: str) -> None:
        """Forget every helper under ``prefix`` in ``proven``, which a root exec is replacing.

        For a key that names a file's content as well as its path: once the
        file is replaced, what was found there before no longer holds.
        """
        if proven is not None:
            with self._lock:
                self._helpers = {
                    held
                    for held in self._helpers
                    if held[0] != proven.container_id or not held[1].startswith(prefix)
                }


def install_once(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, key: str, install: Callable[[], object]
) -> None:
    """Run ``install`` under the container's install lock, unless it already did for ``key``.

    ``key`` names a root-owned file no member can change, and ``install``
    finds it in place or puts it there, raising otherwise. Once it has, in
    the container ``spec``'s last start or network proof verified, it is not
    run there again (:meth:`ProvenContainers.note_helper`). The check comes
    before the lock, so a launch whose file is known in place does not queue
    behind a runtime or MCP install into the same container.
    """
    proven = manager._proven.get(spec)
    if manager._proven.holds_helper(proven, key):
        return
    with install_lock(spec.container_name):
        install()
    manager._proven.note_helper(proven, key)


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
