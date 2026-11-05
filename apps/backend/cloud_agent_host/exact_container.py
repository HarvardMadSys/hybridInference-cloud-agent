"""The one identity gate every retained chat container operation goes through.

:class:`ExactContainer` inspects a chat's container, refuses one whose
identity is not exactly the spec's (labels, namespace, quota, image, name, no
state mount), runs the Docker CLI, and remembers what each spec's last start
or network proof verified. The sandbox manager, the group network and the
helpers split out of the manager all reach Docker through it, so the checks
live in one place instead of being rebuilt by each caller. :meth:`run` and
:meth:`run_bytes` are the bare Docker CLI and check nothing themselves: callers
reach a container only after :meth:`require` or a recorded proof, and only
:meth:`record_proof` records one, of an inspection that is exactly the spec's.
Running a helper as Kata guest root is not one of its operations: it is the
sandbox manager's own (:mod:`cloud_agent_host.guest_root_exec`).

A container's labels, mounts, tmpfs, runtime and image never change for its
id, and Docker refuses an exec into one that is stopped, paused or removed.
So once a start or a network proof has verified the container for a spec, a
bounded command for that spec execs into it by id without another ``docker
container inspect`` (:meth:`ExactContainer.exec_target`). A refused exec
reads like a failed command, so it is inspected afterwards
(:meth:`ExactContainer.checked_exec`): it raises what an inspection first
would have raised, and a same-name replacement is refused rather than
entered. A spawn is still inspected first, since its refusal would surface
only when it exits, and so is every lifecycle transition and archive read.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, TypeVar

from cloud_agent_host.chat_sandbox_inspect import container_inspection
from cloud_agent_host.chat_sandbox_models import (
    CHAT_CHECKPOINT_LABEL,
    CHAT_DISK_LIMIT_LABEL,
    CHAT_ENVIRONMENT_SNAPSHOT_LABEL,
    CHAT_NAMESPACE_LABEL,
    RetainedChatLost,
)
from cloud_agent_host.docker_engine import run_docker
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_checkpoints import checkpoint_image_reference
from cloud_agent_shared.environment_snapshots import snapshot_image_reference

if TYPE_CHECKING:
    from typing import BinaryIO

    from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection, ChatSandboxSpec
    from cloud_agent_host.sandbox import ContainerBackend

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


class ExactContainer:
    """Inspect, identity-check and enter retained chat containers through Docker only."""

    def __init__(self, backend: ContainerBackend, *, state_roots: tuple[str, ...]) -> None:
        if not state_roots:
            raise ValueError("an exact container needs the state roots it refuses as mounts")
        self.backend = backend
        #: Where retained chat state lives: only ever the container's writable layer.
        self._state_roots = state_roots
        # Track started guests that have not passed network proof; failed stops
        # remain here so the next reuse must prove them again.
        self._unproven_guests: set[str] = set()
        self._unproven_guests_lock = threading.Lock()
        #: What each spec's last start or network proof verified, which a
        #: bounded exec then enters by id (:meth:`exec_target`). Written only
        #: through :meth:`record_proof`, which verifies what it records.
        self._proven = ProvenContainers()

    # ── started guests whose network check has not passed ──────────────

    def note_unproven_start(self, container_id: str) -> None:
        """Record a start of this exact container before its Docker transition."""
        with self._unproven_guests_lock:
            self._unproven_guests.add(container_id)

    def clear_unproven_start(self, container_id: str) -> None:
        """Record that a start of this exact container passed its network check."""
        with self._unproven_guests_lock:
            self._unproven_guests.discard(container_id)

    def guest_unproven(self, container_id: str) -> bool:
        """Whether a start of this exact container has not passed its network check."""
        with self._unproven_guests_lock:
            return container_id in self._unproven_guests

    # ── proofs ─────────────────────────────────────────────────────────

    def record_proof(
        self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection
    ) -> ChatSandboxInspection:
        """Remember ``inspection`` as the container verified for ``spec``, and return it.

        The inspection a start or network proof just required: its mounts and
        identity are checked again here, without Docker, so nothing can be
        recorded for ``spec`` that is not exactly its container.
        """
        self.verify_mounts(inspection)
        self.verify_identity(inspection, spec)
        return self._proven.record(spec, inspection)

    def forget_proof(self, spec: ChatSandboxSpec) -> None:
        """Drop what was proven for ``spec``'s name, which is being created or removed."""
        self._proven.forget(spec)

    def proven_container(self, spec: ChatSandboxSpec) -> ChatSandboxInspection | None:
        """The container verified for exactly ``spec``, if any."""
        return self._proven.get(spec)

    def note_network_proof(self, spec: ChatSandboxSpec, proved_from: float) -> None:
        """Keep the container just proved for ``spec`` as its standing network proof."""
        self._proven.note_proof(spec, proved_from)

    def withdraw_network_proof(self, spec: ChatSandboxSpec) -> None:
        """Drop ``spec``'s standing network proof: a proof of it just failed."""
        self._proven.withdraw_proof(spec)

    def standing_proof(self, spec: ChatSandboxSpec) -> tuple[ChatSandboxInspection, float] | None:
        """The container ``spec``'s last passed network proof saw, and when it began."""
        return self._proven.standing_proof(spec)

    def holds_helper(self, proven: ChatSandboxInspection | None, path: str) -> bool:
        """Whether ``path`` was found installed in exactly the container ``proven``."""
        return self._proven.holds_helper(proven, path)

    def note_helper(self, proven: ChatSandboxInspection | None, path: str) -> None:
        """Remember a root-owned helper just found installed in ``proven``.

        See :meth:`ProvenContainers.note_helper` for why it is not checked again.
        """
        self._proven.note_helper(proven, path)

    def forget_helpers(self, proven: ChatSandboxInspection | None, prefix: str) -> None:
        """Forget every helper under ``prefix`` in ``proven``, which a root exec is replacing."""
        self._proven.forget_helpers(proven, prefix)

    # ── Docker ─────────────────────────────────────────────────────────

    def run(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        """Run one Docker CLI command against this deployment's engine."""
        return run_docker(argv, timeout=timeout, engine=getattr(self.backend, "engine", None))

    @staticmethod
    def run_bytes(
        argv: list[str],
        *,
        timeout: float,
        input_bytes: bytes | None = None,
        stdin: BinaryIO | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one Docker CLI command whose standard streams carry bytes, such as ``cp``."""
        try:
            return subprocess.run(
                argv,
                env={**os.environ},
                input=input_bytes,
                stdin=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"Docker archive command failed: {exc}") from exc

    # ── identity ───────────────────────────────────────────────────────

    def inspect(
        self, container: str, *, verify_mounts: bool = True
    ) -> ChatSandboxInspection | None:
        """Inspect a container; missing is ``None``, daemon/protocol failures raise."""
        result = self.run(
            [self.backend.docker_binary, "container", "inspect", container], timeout=30
        )
        if result.returncode != 0:
            lowered = result.stderr.lower()
            if "no such container" in lowered or "no such object" in lowered:
                return None
            raise SandboxError("chat sandbox inspect failed: " + result.stderr.strip()[:300])
        try:
            raw = json.loads(result.stdout)[0]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SandboxError("Docker returned an invalid chat sandbox inspection") from exc
        inspection = container_inspection(raw)
        if verify_mounts:
            self.verify_mounts(inspection)
        return inspection

    def require(self, spec: ChatSandboxSpec) -> ChatSandboxInspection:
        """Inspect the container under ``spec``'s name and require exactly its identity."""
        current = self.inspect(spec.container_name)
        if current is None:
            raise SandboxError("chat sandbox is missing")
        self.verify_identity(current, spec)
        return current

    def require_exact(
        self,
        spec: ChatSandboxSpec,
        container_id: str,
    ) -> ChatSandboxInspection:
        """Require the original Docker object even after its name is reused."""
        current = self.inspect(container_id)
        if current is None:
            raise RetainedChatLost("chat sandbox exact container is missing")
        if current.container_id != container_id:
            raise RetainedChatLost("chat sandbox container identity changed")
        self.verify_identity(current, spec, require_name=False)
        return current

    def verify_mounts(self, inspection: ChatSandboxInspection) -> None:
        """Chat state must be in the writable layer, never a host/volume mount.

        Nor a tmpfs: one over the workspace or the agent state would empty it
        at every stop without an error anywhere.
        """
        mounted = [
            (str(mount.get("Type") or ""), str(mount.get("Destination") or ""))
            for mount in inspection.mounts
        ]
        mounted += [("tmpfs", destination) for destination in inspection.tmpfs]
        for mount_type, destination in mounted:
            if mount_type in {"bind", "volume"}:
                raise SandboxError(
                    f"retained chat sandbox has forbidden {mount_type} mount at "
                    f"{destination or '<unknown>'}"
                )
            path = destination.rstrip("/")
            if any(path == root or path.startswith(f"{root}/") for root in self._state_roots):
                raise SandboxError(
                    f"retained chat state path {destination!r} is mounted, not Docker-layer owned"
                )

    def verify_identity(
        self,
        inspection: ChatSandboxInspection,
        spec: ChatSandboxSpec,
        *,
        require_name: bool = True,
    ) -> None:
        """Refuse a container whose labels, quota, namespace, image or name are not ``spec``'s."""
        expected = spec.docker_labels()
        mismatched = {
            key: (inspection.labels.get(key), value)
            for key, value in expected.items()
            if inspection.labels.get(key) != value
        }
        # Groups created before disk limits have no label and no quota. An
        # unexpected positive quota is still an immutable policy mismatch.
        disk_limit = inspection.labels.get(CHAT_DISK_LIMIT_LABEL, "0")
        if disk_limit != str(spec.disk_limit_mib):
            mismatched[CHAT_DISK_LIMIT_LABEL] = (disk_limit, str(spec.disk_limit_mib))
        namespace = inspection.labels.get(CHAT_NAMESPACE_LABEL)
        if namespace is not None and namespace != self.backend.sandbox_namespace:
            mismatched[CHAT_NAMESPACE_LABEL] = (
                namespace,
                self.backend.sandbox_namespace,
            )
        # A recreated group runs its checkpoint's image, which extends the
        # immutable one; the checkpoint label says which, and only that image
        # is accepted for it.
        checkpoint_id = inspection.labels.get(CHAT_CHECKPOINT_LABEL)
        snapshot = inspection.labels.get(CHAT_ENVIRONMENT_SNAPSHOT_LABEL)
        snapshot_image = None
        if snapshot and not checkpoint_id:
            owner, _, snapshot_id = snapshot.partition(":")
            try:
                snapshot_image = snapshot_image_reference(owner, snapshot_id)
            except ValueError:
                mismatched[CHAT_ENVIRONMENT_SNAPSHOT_LABEL] = (snapshot, "")
        expected_image = (
            checkpoint_image_reference(spec.sandbox_id, checkpoint_id)
            if checkpoint_id
            else snapshot_image or spec.sandbox_image_digest
        )
        if (checkpoint_id or snapshot_image or spec.sandbox_image_digest is not None) and (
            inspection.image != expected_image
        ):
            mismatched["container-image"] = (inspection.image, expected_image or "")
        if (require_name and inspection.name != spec.container_name) or mismatched:
            raise SandboxError(
                "Docker name is occupied by a container outside this chat/generation"
            )

    # ── bounded exec ───────────────────────────────────────────────────

    def exec_target(
        self,
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
        proven = None if expected_container_id is not None else self._proven.get(spec)
        if proven is not None:
            return proven, proven
        current = (
            self.require_exact(spec, expected_container_id)
            if expected_container_id is not None
            else self.require(spec)
        )
        if not_running is not None and not current.running:
            raise SandboxError(not_running)
        return current, None

    def checked_exec(
        self,
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
            current = self.require(spec)
            if current.container_id != proven.container_id:
                raise RetainedChatLost("chat sandbox container identity changed")
            if not current.running:
                raise SandboxError(not_running)
        return result
