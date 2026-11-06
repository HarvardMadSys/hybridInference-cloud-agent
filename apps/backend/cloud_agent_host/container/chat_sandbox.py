"""Docker-owned, restartable sandboxes for agent chats.

This module is the storage/lifecycle boundary for retained execution. A chat
sandbox has the following invariants:

* its name and ownership labels are stable for the lifetime of a chat;
* it has no bind or volume carrying workspace/session state;
* ``docker stop`` deactivates it without deleting its writable layer; and
* commands run with ``docker exec`` after an explicit, inspected start.

The control-plane and runner integration is intentionally layered on top of
this class.  Keeping Docker identity checks here means every caller gets the
same fencing and no-mount guarantees instead of rebuilding security-sensitive
argv in several services. Container identity, labels, paths and lifecycle value
objects live in :mod:`cloud_agent_host.container.chat_sandbox_models`, which this
module reexports for existing callers.

:class:`ChatSandboxManager` keeps the container lifecycle here -- create, start,
prove, stop, inspect, list, remove and the stopped-archive export -- and its
terminals. The rest of its methods are mixins beside it, one concern each:
``chat_sandbox_create`` (the ``docker create`` argv), ``chat_sandbox_members``
(member execs and processes), ``chat_sandbox_workspace`` (the shared workspace,
member homes and archive copies), ``chat_sandbox_observation`` (the guest
observer and tool marks) and ``chat_sandbox_markers`` (the private markers).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
import tty
from typing import TYPE_CHECKING, Any

from cloud_agent_host.archive.sandbox_checkpoints import checkpoint_image_reference
from cloud_agent_host.container.chat_sandbox_create import ContainerCreationMixin
from cloud_agent_host.container.chat_sandbox_exec import network_proof, spawn_target
from cloud_agent_host.container.chat_sandbox_inspect import container_inspection, inspect_many
from cloud_agent_host.container.chat_sandbox_markers import ArchiveMarkersMixin
from cloud_agent_host.container.chat_sandbox_members import MemberExecMixin
from cloud_agent_host.container.chat_sandbox_models import (
    ACTIVATION_TMPFS as ACTIVATION_TMPFS,
    CHAT_CHECKPOINT_LABEL as CHAT_CHECKPOINT_LABEL,
    CHAT_CONTAINER_PREFIX as CHAT_CONTAINER_PREFIX,
    CHAT_CPU_LIMIT_LABEL as CHAT_CPU_LIMIT_LABEL,
    CHAT_DISK_LIMIT_LABEL as CHAT_DISK_LIMIT_LABEL,
    CHAT_ENVIRONMENT_SNAPSHOT_LABEL as CHAT_ENVIRONMENT_SNAPSHOT_LABEL,
    CHAT_GENERATION_LABEL as CHAT_GENERATION_LABEL,
    CHAT_IMAGE_DIGEST_LABEL as CHAT_IMAGE_DIGEST_LABEL,
    CHAT_MEMORY_LIMIT_LABEL as CHAT_MEMORY_LIMIT_LABEL,
    CHAT_NAMESPACE_LABEL as CHAT_NAMESPACE_LABEL,
    CHAT_NETWORK_GENERATION_LABEL as CHAT_NETWORK_GENERATION_LABEL,
    CHAT_NETWORK_LABEL as CHAT_NETWORK_LABEL,
    CHAT_NETWORK_POOL_LABEL as CHAT_NETWORK_POOL_LABEL,
    CHAT_NETWORK_SANDBOX_ID_LABEL as CHAT_NETWORK_SANDBOX_ID_LABEL,
    CHAT_NETWORK_SERVICES_LABEL as CHAT_NETWORK_SERVICES_LABEL,
    CHAT_PROBE_LABEL as CHAT_PROBE_LABEL,
    CHAT_RESIDENT_CAPACITY_LABEL as CHAT_RESIDENT_CAPACITY_LABEL,
    CHAT_RUNTIME_LABEL as CHAT_RUNTIME_LABEL,
    CHAT_RUNTIME_VERSION_LABEL as CHAT_RUNTIME_VERSION_LABEL,
    CHAT_SANDBOX_ID_LABEL as CHAT_SANDBOX_ID_LABEL,
    CHAT_SANDBOX_LABEL as CHAT_SANDBOX_LABEL,
    CHAT_SHARED_LABEL as CHAT_SHARED_LABEL,
    CHAT_STATE_DIR as CHAT_STATE_DIR,
    CHAT_STOP_TIMEOUT_LABEL as CHAT_STOP_TIMEOUT_LABEL,
    CHAT_THREAD_LABEL as CHAT_THREAD_LABEL,
    CHAT_UPLOADS_DIR as CHAT_UPLOADS_DIR,
    CHAT_WORKDIR as CHAT_WORKDIR,
    CLAUDE_SHARED_PROJECTS_DIR as CLAUDE_SHARED_PROJECTS_DIR,
    ENVIRONMENT_TEST_LABEL as ENVIRONMENT_TEST_LABEL,
    MEMBER_TMPFS_ROOT as MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE as MEMBER_UID_BASE,
    MEMBER_UID_LIMIT as MEMBER_UID_LIMIT,
    ArchiveExportTooLarge as ArchiveExportTooLarge,
    ChatSandboxInspection as ChatSandboxInspection,
    ChatSandboxSpec as ChatSandboxSpec,
    ChatSandboxStatus as ChatSandboxStatus,
    MemberProcessReceiptMissing as MemberProcessReceiptMissing,
    RetainedChatFailStopped as RetainedChatFailStopped,
    RetainedChatLost as RetainedChatLost,
    SandboxActionFacts as SandboxActionFacts,
    SandboxRestoreFailed as SandboxRestoreFailed,
    claude_member_config_dir as claude_member_config_dir,
)
from cloud_agent_host.container.chat_sandbox_observation import GuestObserverMixin
from cloud_agent_host.container.chat_sandbox_terminal import (
    _RetainedTerminalProcess,
    _set_pty_size,
    _terminal_bashrc,
    _terminal_rcfile,
)
from cloud_agent_host.container.chat_sandbox_workspace import WorkspaceFilesMixin
from cloud_agent_host.container.exact_container import ExactContainer
from cloud_agent_host.container.guest_root_exec import GuestRootExec
from cloud_agent_host.container.sandbox import ContainerBackend, SandboxError, TerminalProcess
from cloud_agent_host.network.chat_sandbox_network import (
    _GUEST_NETWORK_CHECK as _GUEST_NETWORK_CHECK,
    CHAT_BOOTSTRAP_NETWORK as CHAT_BOOTSTRAP_NETWORK,
    CHAT_NETWORK_BRIDGE_OPTION as CHAT_NETWORK_BRIDGE_OPTION,
    CHAT_NETWORK_PREFIX as CHAT_NETWORK_PREFIX,
    CHAT_NETWORK_PREFIX_LENGTH as CHAT_NETWORK_PREFIX_LENGTH,
    CHAT_NETWORK_PRUNE_GRACE_S as CHAT_NETWORK_PRUNE_GRACE_S,
    CHAT_NULL_NETWORK as CHAT_NULL_NETWORK,
    CHAT_OFFLINE_DNS as CHAT_OFFLINE_DNS,
    RELAY_GATEWAY_PRIORITY as RELAY_GATEWAY_PRIORITY,
    ChatSandboxNetwork,
    _GroupNetworkSnapshot,
)
from cloud_agent_host.network.network_guard import GUARD_RECHECK_S
from cloud_agent_host.observe.guest_observer_locks import GuestObserverLocks
from cloud_agent_shared.operation_profiling import StageObserver, substage

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable
    from typing import BinaryIO


#: This module is a library boundary: it reports what it observed and
#: leaves the decision to the caller, so a warning here is the only
#: record an operator gets of a partially capable guest.
logger = logging.getLogger("cloud_agent_host.chat_sandbox")


class ChatSandboxManager(
    ArchiveMarkersMixin,
    GuestObserverMixin,
    WorkspaceFilesMixin,
    ContainerCreationMixin,
    MemberExecMixin,
    GuestRootExec,
):
    """Create and operate retained chat containers through Docker only."""

    def __init__(self, backend: ContainerBackend) -> None:
        self.backend = backend
        #: The identity gate every Docker operation on a chat goes through.
        self.exact = ExactContainer(backend, state_roots=(CHAT_WORKDIR, CHAT_STATE_DIR))
        self._network_lifecycle = ChatSandboxNetwork(self)
        # Multiple fork siblings may admit turns concurrently. Singleton
        # observer startup/rotation and its live cursors therefore need one
        # critical section per container; other containers do not wait on it.
        self._guest_observers = GuestObserverLocks()

    # ── the group network ──────────────────────────────────────────────
    #
    # The network component owns the routable bridge per sandbox group, used
    # by setup, the agent and every fork member alike, named from the group's
    # identity and guarded on the host by the deployment's rule set
    # (cloud_agent_shared.egress). Docker routes and masquerades it; the host's
    # rules decide what it reaches. The only other endpoints on it are the two
    # relays, copied from the relay network, and the host holds an exact
    # exception for each of them.

    @property
    def network(self) -> ChatSandboxNetwork:
        """Expose the group-network lifecycle owner to host collaborators."""
        return self._network_lifecycle

    def require_docker_data_root(self, expected: str) -> str:
        """Verify retained state is owned by the intended Docker data root.

        The sandbox manager intentionally cannot read or mount this host path. Docker
        reports its daemon-side root over the socket; comparing that value is
        the configuration fence that catches a host accidentally using its
        ephemeral system disk instead of the operator's durable Docker disk.
        Whether that path is a mounted durable device remains a host bootstrap
        check (``findmnt``), because a container with only the Docker socket
        must not be given the host filesystem merely to prove it.
        """
        if not expected or "\x00" in expected or not os.path.isabs(expected):
            raise ValueError("expected Docker data root must be an absolute host path")
        result = self.exact.run(
            [self.backend.docker_binary, "info", "--format", "{{.DockerRootDir}}"],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("Docker data-root inspection failed: " + result.stderr.strip()[:300])
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1 or not os.path.isabs(lines[0]):
            raise SandboxError("Docker returned an invalid data-root path")
        actual = os.path.normpath(lines[0])
        configured = os.path.normpath(expected)
        if actual != configured:
            raise SandboxError(
                f"Docker data root is {actual!r}, expected retained storage at {configured!r}"
            )
        return actual

    def create(
        self,
        spec: ChatSandboxSpec,
        *,
        image: str | None = None,
        checkpoint_id: str | None = None,
        environment_snapshot: tuple[str, str] | None = None,
        facts: SandboxActionFacts | None = None,
        observer: StageObserver | None = None,
    ) -> ChatSandboxInspection:
        """Create one chat container idempotently, never replacing an existing one.

        The container is created stopped on the dormant bootstrap bridge, and
        its group bridge is left to :meth:`start`, which attaches it while it
        is still stopped and proves the network before anything runs in it.
        Reconciling the bridge here as well only repeated that work: each
        reconciliation is a dozen Docker calls and a host-rule helper
        container. The host's rules are still checked first, so a host that
        cannot show them refuses before any container exists.
        """
        with substage(observer, "host_rules"):
            self._network_lifecycle.require_host_rules()
        existing = self.inspect(spec.container_name)
        if existing is not None:
            # Idempotent adoption, not a creation. No action is noted here:
            # the start() that follows knows whether this attempt had to run
            # `docker start`, and that is what classifies the path.
            self.exact.verify_identity(existing, spec)
            return existing
        self.exact.forget_proof(spec)
        command = self.build_create_command(
            spec,
            image=image,
            checkpoint_id=checkpoint_id,
            environment_snapshot=environment_snapshot,
        )
        with substage(observer, "container_create"):
            created = self.exact.run(command, timeout=120)
        if created.returncode != 0:
            # A racing replica may have won after our inspect.  Only accept the
            # resulting container after checking every ownership label.
            raced = self.inspect(spec.container_name)
            if raced is None:
                raise SandboxError(
                    "chat sandbox could not be created: " + created.stderr.strip()[:300]
                )
            # The other replica paid for the creation, so this attempt did
            # not create the container. `start` reports what it did with it.
            self.exact.verify_identity(raced, spec)
            return raced
        if facts is not None:
            # The literal the startup-path classifier reads. Emitting anything
            # else -- "created", say -- hid every brand-new group: the guard in
            # ``SandboxActionFacts.note`` would not recognise it either, so the
            # ``start`` that always follows overwrote it, and a container this
            # attempt really paid to create was reported as a wake from
            # stopped. Two of the four measured paths became indistinguishable.
            facts.note("create")
        try:
            inspected = self.inspect(spec.container_name)
            if inspected is None:
                raise SandboxError("Docker created the chat sandbox but it cannot be inspected")
            self.exact.verify_identity(inspected, spec)
        except BaseException:
            # This call made it and nothing has run in it, but nothing will
            # adopt it either: left, it would hold the group's name, and the
            # anonymous volumes its image declares, on a host the group may
            # never come back to. It goes by the id Docker printed, so only
            # what this call made can go.
            self._remove_created(spec, created.stdout.strip())
            raise
        return inspected

    def _remove_created(self, spec: ChatSandboxSpec, container_id: str) -> None:
        """Remove a container :meth:`create` made that failed its checks; best effort."""
        removed = None
        if container_id:
            with contextlib.suppress(SandboxError):
                removed = self.exact.run(
                    [self.backend.docker_binary, "rm", "--force", "--volumes", container_id],
                    timeout=120,
                )
        if removed is not None and "no such container" in removed.stderr.lower():
            return
        if removed is None or removed.returncode != 0:
            logger.warning(
                "chat sandbox that failed its checks could not be removed",
                extra={
                    "event": "sandbox_create_cleanup_failed",
                    "sandbox_id": spec.sandbox_id,
                },
            )

    def remove_superseded(
        self, spec: ChatSandboxSpec, *, keep_checkpoint: str | None = None
    ) -> bool:
        """Remove this group's container from an older generation, if one holds its name.

        A group moved here was rehomed under a new generation; whatever an
        earlier attempt left on this host under the old one is fenced out by
        the control plane and can never run again. Anything else holding the
        name is left alone. Returns whether a container was removed.

        A container recreated from a checkpoint takes that checkpoint's image
        with it, unless it is ``keep_checkpoint``, the one about to be loaded.
        """
        current = self.inspect(spec.container_name)
        if current is None:
            return False
        labels = current.labels
        try:
            generation = int(labels.get(CHAT_GENERATION_LABEL) or "0")
        except ValueError:
            return False
        if (
            labels.get(CHAT_SANDBOX_ID_LABEL) != spec.sandbox_id
            or labels.get(CHAT_NAMESPACE_LABEL) != self.backend.sandbox_namespace
            or not 0 < generation < spec.generation
        ):
            return False
        removed = self.exact.run(
            [self.backend.docker_binary, "rm", "--force", current.container_id], timeout=120
        )
        if removed.returncode != 0 and self.inspect(spec.container_name) is not None:
            raise SandboxError(
                "superseded chat sandbox could not be removed: " + removed.stderr.strip()[:300]
            )
        self.exact.forget_proof(spec)
        checkpoint_id = labels.get(CHAT_CHECKPOINT_LABEL)
        if checkpoint_id and checkpoint_id != keep_checkpoint:
            self.remove_checkpoint_image(spec, checkpoint_id)
        return True

    def require_existing(
        self,
        spec: ChatSandboxSpec,
        *,
        container_id: str,
    ) -> ChatSandboxInspection:
        """Adopt only the exact durable container; never recreate missing chat state."""
        if not container_id:
            raise ValueError("expected retained container id must be non-empty")
        try:
            current = self.exact.require(spec)
        except SandboxError as exc:
            raise RetainedChatLost("retained chat container is missing or invalid") from exc
        if current.container_id != container_id:
            raise RetainedChatLost(
                "retained chat container id does not match its durable sandbox record"
            )
        return current

    def require_stopped_archive(
        self, spec: ChatSandboxSpec, *, container_id: str
    ) -> ChatSandboxInspection:
        """Fence archival and removal to an exact owned container that is stopped or dead.

        Docker's dead state is terminal and not running: the container cannot be
        resumed, but its writable layer can still be exported. Removal stays
        ``docker rm`` without ``--force``, so a start between these fences is
        left in place.
        """
        current = self.require_existing(spec, container_id=container_id)
        if not self._belongs_to_namespace(current):
            raise SandboxError("sandbox archive requires this deployment's ownership label")
        if current.running or current.status not in {
            ChatSandboxStatus.STOPPED,
            ChatSandboxStatus.DEAD,
        }:
            raise SandboxError("sandbox archive requires a stopped or dead container")
        return current

    def export_stopped_archive(
        self,
        spec: ChatSandboxSpec,
        *,
        container_id: str,
        output: BinaryIO,
        max_bytes: int,
        timeout_s: float = 600,
    ) -> dict[str, Any]:
        """Export the Docker-owned filesystem without starting user processes.

        The normal mount fence rejects bind/volume state. Docker export
        includes workspace and all providers' native state in the writable
        layer, while stopped credential tmpfs mounts contain no retained data.
        Inspect configuration accompanies the export for offline recovery.
        """
        self.require_stopped_archive(spec, container_id=container_id)
        metadata_result = self.exact.run(
            [self.backend.docker_binary, "container", "inspect", container_id], timeout=30
        )
        if metadata_result.returncode != 0:
            raise SandboxError("sandbox archive metadata inspection failed")
        try:
            metadata = json.loads(metadata_result.stdout)[0]
            if not isinstance(metadata, dict) or metadata.get("Id") != container_id:
                raise ValueError("container identity changed")
        except (IndexError, TypeError, ValueError) as exc:
            raise SandboxError("sandbox archive metadata is invalid") from exc
        process: subprocess.Popen[bytes] | None = None
        try:
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(
                    [self.backend.docker_binary, "container", "export", container_id],
                    stdout=output,
                    stderr=errors,
                )
                deadline = time.monotonic() + timeout_s
                while process.poll() is None:
                    if os.fstat(output.fileno()).st_size > max_bytes:
                        raise SandboxError("sandbox archive exceeds the S3 upload size limit")
                    if time.monotonic() >= deadline:
                        raise SandboxError("sandbox archive export timed out")
                    time.sleep(0.1)
                if process.returncode != 0:
                    raise SandboxError("sandbox archive filesystem export failed")
                output.flush()
                if os.fstat(output.fileno()).st_size > max_bytes:
                    raise SandboxError("sandbox archive exceeds the S3 upload size limit")
        except OSError as exc:
            raise SandboxError("sandbox archive filesystem export failed") from exc
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.require_stopped_archive(spec, container_id=container_id)
        config = metadata.get("Config") or {}
        host_config = metadata.get("HostConfig") or {}
        owned_label_keys = {
            CHAT_SANDBOX_LABEL,
            CHAT_NAMESPACE_LABEL,
            CHAT_THREAD_LABEL,
            CHAT_SANDBOX_ID_LABEL,
            CHAT_GENERATION_LABEL,
            CHAT_RUNTIME_LABEL,
            CHAT_RUNTIME_VERSION_LABEL,
            CHAT_IMAGE_DIGEST_LABEL,
            CHAT_MEMORY_LIMIT_LABEL,
            CHAT_CPU_LIMIT_LABEL,
            CHAT_STOP_TIMEOUT_LABEL,
            CHAT_SHARED_LABEL,
            CHAT_RESIDENT_CAPACITY_LABEL,
        }
        # Docker inspect also includes environment values, host paths, mounts,
        # and arbitrary labels. Offline recovery needs image identity and
        # container policy, not those potential secret-bearing values.
        return {
            **{key: metadata[key] for key in ("Id", "Name", "Image", "Created") if key in metadata},
            "Config": {
                **{
                    key: config[key]
                    for key in ("Image", "User", "WorkingDir", "Entrypoint", "Cmd")
                    if key in config
                },
                "Labels": {
                    key: value
                    for key, value in (config.get("Labels") or {}).items()
                    if key in owned_label_keys
                },
            },
            "HostConfig": {
                key: host_config[key]
                for key in (
                    "Runtime",
                    "Memory",
                    "MemorySwap",
                    "NanoCpus",
                    "CpuQuota",
                    "CpuPeriod",
                    "CpuShares",
                    "PidsLimit",
                    "ReadonlyRootfs",
                    "CapDrop",
                    "CapAdd",
                    "SecurityOpt",
                )
                if key in host_config
            },
        }

    def remove_environment_test(self, spec: ChatSandboxSpec, *, container_id: str) -> None:
        """Remove only the exact independently labelled ephemeral test container."""
        inspection = self.exact.require_exact(spec, container_id)
        if inspection.labels.get(ENVIRONMENT_TEST_LABEL) != spec.labels.get(
            ENVIRONMENT_TEST_LABEL
        ) or not spec.labels.get(ENVIRONMENT_TEST_LABEL):
            raise SandboxError("Container is not an environment test")
        if inspection.running:
            self.stop(spec, expected_container_id=container_id)
        result = self.exact.run([self.backend.docker_binary, "rm", container_id], timeout=120)
        if result.returncode:
            raise SandboxError("Environment test cleanup failed")
        self.exact.forget_proof(spec)

    def remove_stopped_archived(self, spec: ChatSandboxSpec, *, container_id: str) -> None:
        """Remove only a verified archive's exact stopped container, without force."""
        current = self.require_stopped_archive(spec, container_id=container_id)
        self.require_stopped_archive(spec, container_id=container_id)
        result = self.exact.run(
            [self.backend.docker_binary, "container", "rm", container_id], timeout=60
        )
        if result.returncode != 0:
            raise SandboxError("archived sandbox removal failed")
        if self.inspect(container_id) is not None:
            raise SandboxError("archived sandbox still exists after removal")
        self.exact.forget_proof(spec)
        # Remove first, without force: a start after the stopped fence makes
        # Docker refuse removal. Generic stop/network release before rm could
        # stop a newly running original and discard post-backup writes.
        network_ids = dict(current.network_ids)
        for network_name in current.networks:
            if network_name in {CHAT_NULL_NETWORK, CHAT_BOOTSTRAP_NETWORK}:
                continue
            try:
                self._network_lifecycle._release_owned_network(
                    network_name,
                    spec,
                    only_if_sandbox_unattached=True,
                    expected_network_id=network_ids.get(network_name),
                )
            except SandboxError:
                # The container is already absent. Normal orphan-network
                # pruning can retry cleanup, fenced to that network's labels.
                logger.warning("archived sandbox network cleanup needs reconciliation")
        checkpoint_id = current.labels.get(CHAT_CHECKPOINT_LABEL)
        if checkpoint_id:
            self.remove_checkpoint_image(spec, checkpoint_id)

    def remove_checkpoint_image(self, spec: ChatSandboxSpec, checkpoint_id: str) -> None:
        """Drop this group's image of ``checkpoint_id``, unless a container still uses it.

        The image is named for this group's checkpoint, and only the group's
        container on this host ever runs it, so nothing else needs its layers.
        Best effort: Docker keeps an image a container still uses, an image
        already gone is what was asked for, and one left behind costs disk,
        not correctness.
        """
        reference = checkpoint_image_reference(spec.sandbox_id, checkpoint_id)
        try:
            removed = self.exact.run(
                [self.backend.docker_binary, "image", "rm", reference], timeout=120
            )
        except SandboxError:
            removed = None
        if removed is not None and "no such image" in removed.stderr.lower():
            return
        if removed is None or removed.returncode != 0:
            logger.warning(
                "restored checkpoint image could not be removed",
                extra={
                    "event": "sandbox_checkpoint_image_remove_failed",
                    "sandbox_id": spec.sandbox_id,
                    "checkpoint_id": checkpoint_id,
                },
            )

    def inspect(
        self, container: str, *, verify_mounts: bool = True
    ) -> ChatSandboxInspection | None:
        """Inspect a container; missing is ``None``, daemon/protocol failures raise."""
        return self.exact.inspect(container, verify_mounts=verify_mounts)

    def _belongs_to_namespace(self, inspection: ChatSandboxInspection) -> bool:
        """Fence daemon-wide discovery to this independently deployed stack.

        Ownership is the immutable namespace label and nothing else. Containers
        without one predate that label and the single-network change; they are
        left for the one-time cleanup rather than adopted by guesswork.
        """
        return inspection.labels.get(CHAT_NAMESPACE_LABEL) == self.backend.sandbox_namespace

    def list(self) -> builtins.list[ChatSandboxInspection]:
        """List retained containers owned by this deployment namespace.

        The owned ones are inspected together, in as few Docker processes as
        :func:`inspect_many` needs, rather than one process per container.
        """
        result = self.exact.run(
            [
                self.backend.docker_binary,
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                '{"id":{{json .ID}},"namespace":{{json (.Label "'
                + CHAT_NAMESPACE_LABEL
                + '")}},"probe":{{json (.Label "'
                + CHAT_PROBE_LABEL
                + '")}}}',
                "--filter",
                f"label={CHAT_SANDBOX_LABEL}=true",
            ],
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox listing failed: " + result.stderr.strip()[:300])
        owned: builtins.list[str] = []
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
                container_id, namespace = row["id"], row["namespace"]
                probe = row.get("probe") or ""
                if not isinstance(container_id, str) or not container_id:
                    raise ValueError("missing container id")
                if not isinstance(namespace, str) or not isinstance(probe, str):
                    raise ValueError("invalid labels")
            except (KeyError, TypeError, ValueError) as exc:
                raise SandboxError("Docker returned an invalid chat sandbox listing") from exc
            # Read ownership from ls before making a per-container RPC: a
            # wedged foreign Kata shim can block even docker inspect. A probe
            # belongs to the process running it and is never a chat to adopt.
            if namespace == self.backend.sandbox_namespace and not probe:
                owned.append(container_id)
        found: builtins.list[ChatSandboxInspection] = []
        for raw in inspect_many(self.exact.run, self.backend.docker_binary, "container", owned):
            # A foreign namespace cannot make this sandbox manager fail startup by
            # carrying policy-invalid mounts. Apply this stack's mount fence
            # only after ownership is established.
            item = container_inspection(raw)
            if self._belongs_to_namespace(item):
                self.exact.verify_mounts(item)
                if not item.labels.get(ENVIRONMENT_TEST_LABEL):
                    found.append(item)
        return found

    @network_proof
    def start(
        self,
        spec: ChatSandboxSpec,
        *,
        facts: SandboxActionFacts | None = None,
        observer: StageObserver | None = None,
        expected_container_id: str | None = None,
        defer_network_release_on_fail_stop: bool = False,
        before_stopped_start: Callable[[ChatSandboxInspection], None] | None = None,
    ) -> ChatSandboxInspection:
        """Start the exact stopped container and verify it reached RUNNING.

        A cold start is also where the network is proved from inside: nothing
        from the repository runs in a guest that has not passed the network
        lifecycle's in-guest check. A start that fails after ``docker
        start`` stops its guest again; a running guest whose proof never
        finished is proved before it is reused.
        """
        current = self.exact.require(spec)
        exact_container_id = expected_container_id or current.container_id
        if current.container_id != exact_container_id:
            raise RetainedChatLost("chat sandbox container identity changed before start")
        network_snapshot = _GroupNetworkSnapshot()
        with substage(observer, "group_network"):
            current = self._network_lifecycle._require_group_network(
                spec, current, network_snapshot
            )
        if current.container_id != exact_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during start")
        if current.running:
            if facts is not None:
                facts.note("reuse")
            if self.exact.guest_unproven(exact_container_id):
                # A start of this exact container failed before its in-guest
                # check and could not stop it either. Nothing has run in it
                # since; prove it now, or stop it.
                return self._prove_started_guest(
                    spec,
                    exact_container_id,
                    observer=observer,
                    defer_network_release=defer_network_release_on_fail_stop,
                    addresses=network_snapshot.addresses,
                )
            with substage(observer, "guest_network_sync"):
                self._network_lifecycle._sync_kata_guest_network(
                    spec, current, network_snapshot.addresses
                )
            return self.exact.record_proof(spec, current)
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("a paused chat sandbox is not a valid stopped activation")
        if current.status is ChatSandboxStatus.DEAD:
            raise SandboxError("a dead chat sandbox cannot be resumed")
        if before_stopped_start is not None:
            # The callback owns the durable pre-start fence. Invoke it only
            # after the exact object has been re-inspected and proven stopped,
            # immediately before this method performs the Docker transition.
            before_stopped_start(current)
        # Recorded before the Docker transition, and cleared only by a passed
        # check: a start Docker completes after its client gave up is
        # unproven too.
        self.exact.note_unproven_start(exact_container_id)
        with substage(observer, "container_start"):
            result = self.exact.run(
                [self.backend.docker_binary, "start", current.container_id], timeout=120
            )
        if facts is not None:
            facts.note("start")
        if result.returncode != 0:
            raise SandboxError("chat sandbox could not be started: " + result.stderr.strip()[:300])
        return self._prove_started_guest(
            spec,
            exact_container_id,
            observer=observer,
            defer_network_release=defer_network_release_on_fail_stop,
        )

    def _prove_started_guest(
        self,
        spec: ChatSandboxSpec,
        container_id: str,
        *,
        observer: StageObserver | None,
        defer_network_release: bool,
        addresses: dict[str, str] | None = None,
    ) -> ChatSandboxInspection:
        """Prove a started guest or exact-stop it on any network-check failure.

        A stop failure leaves the guest marked unproven before any reuse.
        """
        try:
            started = self.exact.require(spec)
            if started.container_id != container_id:
                raise RetainedChatLost("chat sandbox container identity changed during start")
            if not started.running or started.status is not ChatSandboxStatus.RUNNING:
                raise SandboxError(
                    f"chat sandbox started into unexpected state {started.status.value!r}"
                )
            with substage(observer, "guest_network_sync"):
                self._network_lifecycle._sync_kata_guest_network(spec, started, addresses)
            with substage(observer, "guest_network_check"):
                self._network_lifecycle._verify_guest_network(
                    spec,
                    expected_container_id=container_id,
                    defer_network_release=defer_network_release,
                )
            with substage(observer, "host_rules_recheck"):
                self._network_lifecycle.verify_host_rules([spec], renew_after_s=GUARD_RECHECK_S)
        except RetainedChatFailStopped:
            raise
        except Exception as exc:
            stopped: ChatSandboxInspection | None = None
            try:
                stopped = self.stop(
                    spec,
                    expected_container_id=container_id,
                    release_networks=not defer_network_release,
                )
            except Exception as stop_exc:
                from cloud_agent_host.network.guarded_workloads import exact_stop_unidentified

                exact_stop_unidentified(self, container_id)
                logger.warning(
                    "chat_unproven_guest_structured_stop_failed",
                    extra={
                        "sandbox_id": spec.sandbox_id,
                        "detail": str(exc)[:300],
                        "stop_detail": str(stop_exc)[:300],
                    },
                )
            if stopped is None:
                raise
            raise RetainedChatFailStopped(
                f"chat sandbox stopped before its network check passed: {exc}",
                stopped,
            ) from exc
        self.exact.clear_unproven_start(container_id)
        verified = self.exact.require(spec)
        if verified.container_id != container_id:
            raise RetainedChatLost("chat sandbox container identity changed during start")
        return self.exact.record_proof(spec, verified)

    def stop(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        release_networks: bool = True,
    ) -> ChatSandboxInspection:
        """Deactivate, release private networking, and retain the writable layer."""
        current = (
            self.exact.require_exact(spec, expected_container_id)
            if expected_container_id is not None
            else self.exact.require(spec)
        )
        exact_container_id = current.container_id
        if current.status is ChatSandboxStatus.PAUSED:
            raise SandboxError("refusing to treat a paused chat sandbox as cleanly stopped")
        if not current.running:
            if not release_networks:
                return current
            return self.release_stopped_networks(
                spec,
                expected_container_id=exact_container_id,
            )
        result = self.exact.run(
            [
                self.backend.docker_binary,
                "stop",
                "--time",
                str(spec.stop_timeout_s),
                current.container_id,
            ],
            timeout=spec.stop_timeout_s + 30,
        )
        if result.returncode != 0:
            raise SandboxError("chat sandbox could not be stopped: " + result.stderr.strip()[:300])
        try:
            stopped = self.exact.require_exact(spec, exact_container_id)
        except RetainedChatLost as exc:
            raise RetainedChatLost("chat sandbox container identity changed during stop") from exc
        if stopped.running or stopped.status not in {
            ChatSandboxStatus.STOPPED,
            ChatSandboxStatus.CREATED,
            ChatSandboxStatus.DEAD,
        }:
            raise SandboxError(
                f"chat sandbox stopped into unexpected state {stopped.status.value!r}"
            )
        if not release_networks:
            return stopped
        return self.release_stopped_networks(
            spec,
            expected_container_id=exact_container_id,
        )

    def release_stopped_networks(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> ChatSandboxInspection:
        """Release networking only for one exact, already-stopped container."""
        current = self.exact.require_exact(spec, expected_container_id)
        if current.running:
            raise SandboxError("refusing to release networking from a running chat sandbox")
        released = self._network_lifecycle._release_sandbox_networks(
            spec,
            current,
            expected_container_id=expected_container_id,
        )
        if released.container_id != expected_container_id:
            raise RetainedChatLost("chat sandbox container identity changed during stop")
        return released

    @network_proof
    def require_network(
        self, spec: ChatSandboxSpec, *, observer: StageObserver | None = None
    ) -> None:
        """Prove this container is on exactly its guarded group bridge.

        The same proof :meth:`start` makes of a running container, for work
        that does not go through a start: an SSH-access change, a request
        whose admission only retried an archive completion, and a command
        whose backend holds no proof under a minute old. So it is cheap: the
        guard's last verification is reused for a minute, and the relays and
        their host exceptions are reconciled against what Docker reports now.
        A guest whose in-guest check never passed is refused until
        :meth:`start` proves it.
        """
        snapshot = _GroupNetworkSnapshot()
        with substage(observer, "network_ensure"):
            current = self._network_lifecycle._require_group_network(
                spec, self.exact.require(spec), snapshot
            )
        expected = self._network_lifecycle.network_for(spec)
        if set(current.networks) != {expected}:
            raise SandboxError(
                f"retained chat network is {list(current.networks)!r}, not its group "
                f"bridge {expected!r}"
            )
        if self.exact.guest_unproven(current.container_id):
            raise SandboxError(
                "retained chat started without passing its network check; it must be "
                "activated again before anything runs in it"
            )
        with substage(observer, "guest_network_sync"):
            self._network_lifecycle._sync_kata_guest_network(spec, current, snapshot.addresses)
        self.exact.record_proof(spec, current)

    def ensure_kilo_mcp_runtime(self, spec: ChatSandboxSpec, version: str) -> None:
        """Bring retained Kilo members onto the deployment's isolated runtime."""
        from cloud_agent_host.runtimes.http_bridge.kilo_mcp import ensure_kilo_runtime_once

        ensure_kilo_runtime_once(self, spec, version)

    def ensure_downloaded_runtime(self, spec: ChatSandboxSpec, runtime: str, version: str) -> None:
        """Copy a version downloaded after the image was built into this chat."""
        from cloud_agent_host.member_setup.runtime_install import ensure_downloaded_runtime_once

        ensure_downloaded_runtime_once(self, spec, runtime, version)

    def ensure_mcp_helpers(self, spec: ChatSandboxSpec, runtime: str) -> None:
        """Keep existing conversations on the same adapters as new conversations."""
        from cloud_agent_host.member_setup.mcp_helpers import install_mcp_helpers_once

        install_mcp_helpers_once(self, spec, runtime)

    def spawn_terminal(
        self,
        spec: ChatSandboxSpec,
        *,
        env: dict[str, str] | None = None,
        workdir: str = CHAT_WORKDIR,
        rows: int,
        cols: int,
        argv: builtins.list[str] | None = None,
        user_uid: int | None = None,
    ) -> TerminalProcess:
        """Attach a PTY to one shell without creating or killing a container."""
        current, env = spawn_target(
            self, spec, env, not_running="chat sandbox must be started before terminal exec"
        )
        token = secrets.token_hex(16)
        if user_uid is not None:
            from cloud_agent_host.environment_process import (
                prepare_home,
                prepare_tool_home,
                tool_environment,
            )

            directory = prepare_home(self, spec, user_uid)
            pidfile = f"{directory}/terminal-{token}.pid"
            env = {
                **tool_environment(),
                **(env or {}),
                "HOME": prepare_tool_home(self, spec, user_uid),
            }
        else:
            pidfile = f"{ACTIVATION_TMPFS}/terminal-{token}.pid"
        rcfile = _terminal_rcfile(pidfile)
        # The session shell gets its own process group.  Sandbox Manager suspend/kill
        # then signals this terminal tree only; pausing or killing the retained
        # container would also stop the Agent CLI and violate one-chat
        # lifecycle ownership.
        #
        # Docker's TTY exec already starts this wrapper as the foreground
        # session/process-group leader, and the pidfile names that group.
        # Every shell command has to stay in it: a background shell observes
        # /dev/null, while another ``setsid`` loses this exec's controlling
        # terminal.
        #
        # bash (when the image has it) gives the owner line editing, history
        # and completion. An interactive bash always turns job control on and
        # makes itself a process-group leader, ignoring ``+m``, so it replaces
        # the wrapper rather than running under it: it then leads the group the
        # pidfile already names. Its rcfile turns job control back off before
        # every prompt -- bash restores it after reading startup files -- so the
        # commands it runs join that group too.
        #
        # Nothing in this group can resume a stopped job, so Ctrl-Z must not
        # stop one: an ignored SIGTSTP is inherited by every command. The ``sh``
        # fallback keeps its wrapper for the pidfile cleanup, which therefore
        # has to outlive the Ctrl-C and Ctrl-\ that reach the whole group.
        script = (
            'pidfile="$1"; rcfile="$2"; '
            'printf "%s\\n" "$$" > "$pidfile" || exit 126; '
            "umask 0002; "
            "trap '' TSTP TTIN TTOU; "
            'if [ "$#" -gt 3 ]; then shift 3; exec "$@"; fi; '
            "if command -v bash >/dev/null 2>&1; then "
            'if printf "%s" "$3" > "$rcfile"; then '
            "SHELL=$(command -v bash); export SHELL; "
            'exec bash --rcfile "$rcfile" -i; fi; fi; '
            'rm -f "$rcfile"; '
            'cleanup() { rm -f "$pidfile"; }; '
            "trap 'cleanup' EXIT; "
            "trap : INT QUIT; "
            '/bin/sh +m -i; status=$?; exit "$status"'
        )
        command = self.build_exec_command(
            spec,
            [
                "/bin/sh",
                "-c",
                script,
                "sh",
                pidfile,
                rcfile,
                _terminal_bashrc(pidfile, rcfile),
                *(argv or []),
            ],
            env=env,
            workdir=workdir,
            interactive=True,
            tty=True,
            user_uid=user_uid,
            user_gid=self.backend.gid if user_uid is not None else None,
            container_id=current.container_id,
        )
        master_fd, slave_fd = os.openpty()
        try:
            _set_pty_size(slave_fd, rows, cols)
            tty.setraw(slave_fd)
            # All three streams share the PTY. The Docker CLI reads the window
            # size from its *stdout*: with a pipe there it sends the exec no
            # console size and drops every SIGWINCH, so the shell inside never
            # learns the browser's dimensions.
            process = subprocess.Popen(
                command,
                env=self.exec_client_env(env),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                bufsize=0,
                start_new_session=True,
            )
        except Exception:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            raise
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        return _RetainedTerminalProcess(
            process,
            manager=self,
            spec=spec,
            pidfile=pidfile,
            input_fd=master_fd,
            operation_uid=user_uid,
        )

    def reap_stale_terminal_processes(self, spec: ChatSandboxSpec) -> None:
        """Remove PTYs whose sandbox manager client disappeared before graceful close.

        Interactive terminal streams are intentionally ephemeral and cannot
        be reattached after a sandbox manager process restart.  The retained container
        is durable, however, and Docker may leave an exec process alive after
        its attaching client dies.  Match both the unguessable pidfile and the
        exact argv before signalling, so a stale/reused numeric PID can never
        target the Agent CLI or PID 1.  The ``sh`` wrapper's argv carries the
        pidfile; a bash terminal's carries its rcfile, named after the pidfile.
        """
        current = self.exact.require(spec)
        if not current.running:
            return
        script = (
            "set -eu; command -v tr >/dev/null; command -v grep >/dev/null; "
            f"for pidfile in {ACTIVATION_TMPFS}/terminal-*.pid; do "
            '[ -e "$pidfile" ] || continue; '
            'rcfile="${pidfile%.pid}.bashrc"; '
            'pid=$(cat "$pidfile" 2>/dev/null || :); '
            'case "$pid" in ""|*[!0-9]*) rm -f "$pidfile" "$rcfile"; continue;; esac; '
            'if [ -r "/proc/$pid/cmdline" ] '
            '&& tr "\\000" "\\n" < "/proc/$pid/cmdline" '
            '| grep -Fqx -e "$pidfile" -e "$rcfile"; then '
            'kill -CONT -"$pid" 2>/dev/null || :; '
            'kill -KILL -"$pid" 2>/dev/null || :; '
            'attempt=0; while kill -0 -"$pid" 2>/dev/null; do '
            'attempt=$((attempt + 1)); test "$attempt" -lt 100 || exit 124; '
            "sleep 0.05; done; "
            'fi; rm -f "$pidfile" "$rcfile"; done'
        )
        result = self.exec_capture(
            spec,
            ["/bin/sh", "-c", script],
            workdir="/",
            timeout_s=15,
        )
        if result.returncode != 0:
            raise SandboxError(
                "stale retained terminal cleanup failed: "
                + result.stderr.decode("utf-8", "replace")[:300]
            )


__all__ = [
    "ACTIVATION_TMPFS",
    "CHAT_CONTAINER_PREFIX",
    "CHAT_GENERATION_LABEL",
    "CHAT_IMAGE_DIGEST_LABEL",
    "CHAT_NAMESPACE_LABEL",
    "CHAT_RESIDENT_CAPACITY_LABEL",
    "CHAT_RUNTIME_LABEL",
    "CHAT_RUNTIME_VERSION_LABEL",
    "CHAT_SANDBOX_ID_LABEL",
    "CHAT_SANDBOX_LABEL",
    "CHAT_SHARED_LABEL",
    "CHAT_STATE_DIR",
    "CHAT_THREAD_LABEL",
    "CHAT_UPLOADS_DIR",
    "CHAT_WORKDIR",
    "MEMBER_TMPFS_ROOT",
    "MEMBER_UID_BASE",
    "MEMBER_UID_LIMIT",
    "ChatSandboxInspection",
    "ChatSandboxManager",
    "ChatSandboxSpec",
    "ChatSandboxStatus",
    "RetainedChatFailStopped",
    "RetainedChatLost",
]
