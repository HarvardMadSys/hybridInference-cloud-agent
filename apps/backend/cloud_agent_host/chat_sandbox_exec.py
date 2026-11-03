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

A network proof stands for a short time too (:func:`standing_network_proof`):
the exact running container it proved, unchanged and not restarted, may be
admitted on it again without repeating the guest's network sync. A proof
that fails withdraws the one before it.
"""

from __future__ import annotations

import functools
import math
import threading
import time
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

from cloud_agent_host.chat_sandbox_models import ChatSandboxStatus, RetainedChatLost
from cloud_agent_host.install_locks import install_lock
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection, ChatSandboxSpec

NOT_RUNNING = "chat sandbox must be started before exec"

Result = TypeVar("Result", bound="subprocess.CompletedProcess[Any]")
Proved = TypeVar("Proved")
ProofArgs = ParamSpec("ProofArgs")


class ProvenContainers:
    """The container each spec's last start or network proof verified, by name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proven: dict[str, tuple[ChatSandboxSpec, ChatSandboxInspection]] = {}
        #: Root-owned helpers found in place, as (container id, key) pairs. A key
        #: is the helper's path, followed by its content's digest when a host
        #: could install other content there.
        self._helpers: set[tuple[str, str]] = set()
        #: Each name's last network proof that passed: what it proved, and when
        #: it began (monotonic).
        self._proofs: dict[str, tuple[ChatSandboxSpec, ChatSandboxInspection, float]] = {}
        #: When a network proof of each name last failed. No proof begun
        #: earlier stands, even one that passed after the failure.
        self._withdrawn: dict[str, float] = {}

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
            self._proofs.pop(spec.container_name, None)
            self._withdrawn.pop(spec.container_name, None)
            dropped = self._proven.pop(spec.container_name, None)
            if dropped is not None and self._helpers:
                gone = dropped[1].container_id
                self._helpers = {held for held in self._helpers if held[0] != gone}

    def get(self, spec: ChatSandboxSpec) -> ChatSandboxInspection | None:
        """The container verified for exactly ``spec``, if any."""
        with self._lock:
            proven = self._proven.get(spec.container_name)
        return proven[1] if proven is not None and proven[0] == spec else None

    def note_proof(self, spec: ChatSandboxSpec, proved_from: float) -> None:
        """Keep the container just proved for ``spec`` as its standing network proof.

        ``proved_from`` is when the proof began. A proof of the same name that
        failed after that withdrew it already, so it is not kept.
        """
        name = spec.container_name
        with self._lock:
            proven = self._proven.get(name)
            if (
                proven is not None
                and proven[0] == spec
                and proved_from > self._withdrawn.get(name, -math.inf)
            ):
                self._proofs[name] = (spec, proven[1], proved_from)

    def withdraw_proof(self, spec: ChatSandboxSpec) -> None:
        """Drop ``spec``'s standing network proof: a proof of it just failed."""
        with self._lock:
            self._proofs.pop(spec.container_name, None)
            self._withdrawn[spec.container_name] = time.monotonic()

    def standing_proof(self, spec: ChatSandboxSpec) -> tuple[ChatSandboxInspection, float] | None:
        """The container ``spec``'s last passed network proof saw, and when it began."""
        with self._lock:
            proof = self._proofs.get(spec.container_name)
        return (proof[1], proof[2]) if proof is not None and proof[0] == spec else None

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


def guest_unproven(manager: ChatSandboxManager, container_id: str) -> bool:
    """Whether a start of this exact container has not passed its network check."""
    with manager._unproven_guests_lock:
        return container_id in manager._unproven_guests


def network_proof(
    method: Callable[Concatenate[ChatSandboxManager, ChatSandboxSpec, ProofArgs], Proved],
) -> Callable[Concatenate[ChatSandboxManager, ChatSandboxSpec, ProofArgs], Proved]:
    """Make a manager method that proves ``spec``'s network keep or withdraw that proof.

    One that returns proved the container it recorded; one that raises, for
    any reason, leaves no earlier proof standing.
    """

    @functools.wraps(method)
    def proving(
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        /,
        *args: ProofArgs.args,
        **kwargs: ProofArgs.kwargs,
    ) -> Proved:
        proved_from = time.monotonic()
        try:
            result = method(manager, spec, *args, **kwargs)
        except BaseException:
            manager._proven.withdraw_proof(spec)
            raise
        manager._proven.note_proof(spec, proved_from)
        return result

    return proving


def standing_network_proof(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    current: ChatSandboxInspection,
    *,
    max_age_s: float,
) -> float | None:
    """When the network proof ``current`` may still be admitted on began; ``None`` if none.

    ``current`` is a fresh inspection. The proof stands only for exactly the
    container it proved, still running, not restarted since (Docker's start
    time), on exactly the bridge object it was proved on and nothing else, for
    under ``max_age_s`` after it began, with no failed proof of the name since
    and no start of it still unproven. The host's rules are still required:
    the guard trusts its own verification no longer than it does for a start,
    and checks again, or raises, once that lapses. A stand-in manager that
    keeps no proofs has none standing.
    """
    proofs: ProvenContainers | None = getattr(manager, "_proven", None)
    standing = None if proofs is None else proofs.standing_proof(spec)
    if standing is None:
        return None
    proved, proved_from = standing
    seen = (current.container_id, current.started_at, current.networks, current.network_ids)
    if (
        guest_unproven(manager, current.container_id)
        or time.monotonic() - proved_from >= max_age_s
        or not current.running
        or current.status is not ChatSandboxStatus.RUNNING
        or not current.started_at
        or seen != (proved.container_id, proved.started_at, proved.networks, proved.network_ids)
        or current.networks != (manager.network.network_for(spec),)
    ):
        return None
    manager.network.require_host_rules()
    return proved_from


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
