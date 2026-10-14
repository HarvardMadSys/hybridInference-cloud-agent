"""Live, revision-fenced sandbox archive policy inside the sandbox manager.

The control plane owns the policy in Postgres.  A host runner receives the
complete desired revision on every heartbeat and relays it over the sandbox manager's
authenticated private channel.  This controller is the stable lifecycle hook:
the shared-session supervisor binds it once, while the controller atomically
swaps the immutable S3 service behind that hook.

An archive already in flight keeps the service it began with.  A failed final
archive pins that same service for the exact stopped container, so a policy
change cannot turn its retry into an unarchived successful stop.

A lifecycle whose native history cannot be proven flushed (a crash, a reboot,
a stop that bypassed the broker, or a failed transport close) is never
archived. Its marker records an explicit ``skipped`` final instead, and the
sandbox continues: resuming it relies on the ordinary native-resume checks,
exactly as with archives disabled. The next clean stop archives normally.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_archive_s3 import (
    SandboxArchiveReceipt,
    SandboxArchiveS3Config,
    SandboxArchiveS3Uploader,
)
from cloud_agent_host.sandbox_archives import (
    SandboxArchiveService,
    SandboxInitialArchiveMarker,
    safe_archive_error_type,
)
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_BUCKET,
    SANDBOX_ARCHIVE_PREFIX,
    SandboxArchivePolicy,
)

logger = logging.getLogger(__name__)

# Final states that never upload. ``blocked`` is the pre-skip spelling written
# by older brokers; it is read exactly like ``skipped`` so groups those brokers
# quarantined finish their lifecycle and continue after an upgrade.
_UNARCHIVED_FINAL_STATES = frozenset({"skipped", "blocked"})
_COMPLETED_FINAL_STATES = frozenset({"uploaded"}) | _UNARCHIVED_FINAL_STATES

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cloud_agent_host.chat_sandbox import (
        ChatSandboxInspection,
        ChatSandboxManager,
        ChatSandboxSpec,
        RetainedChatBackend,
    )
    from cloud_agent_host.sandbox_checkpoints import (
        SandboxCheckpointCapture,
        SandboxCheckpointReceipt,
    )
    from cloud_agent_host.shared_session_supervisor import SharedMemberManifest


class _ArchiveService(Protocol):
    """The immutable service surface one policy revision owns."""

    def start(self) -> None: ...

    def close(self) -> None: ...

    def status(self) -> dict[str, object]: ...

    def probe_initial(
        self,
        backend: RetainedChatBackend,
        *,
        expected_container_id: str,
        preparation_id: str,
        members: Sequence[SharedMemberManifest],
    ) -> dict[str, object]: ...

    def capture_initial(
        self,
        backend: RetainedChatBackend,
        *,
        expected_container_id: str,
        preparation_id: str,
        base_sha: str,
        members: Sequence[SharedMemberManifest],
    ) -> dict[str, object]: ...

    def queue_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> SandboxArchiveReceipt: ...

    def capture_checkpoint(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> SandboxCheckpointReceipt: ...


@dataclass(eq=False)
class _ServiceSlot:
    """One immutable service plus calls and retries that still own it."""

    revision: int
    policy: SandboxArchivePolicy
    service: _ArchiveService
    active: int = 0
    retired: bool = False
    pins: set[tuple[str, int, str]] = field(default_factory=set)


@dataclass(frozen=True)
class _FinalArchiveMarker:
    """Canonical root-owned fence for one stopped container's final policy."""

    namespace_sha256: str
    sandbox_id: str
    generation: int
    container_id: str
    policy: SandboxArchivePolicy
    state: str = "pending"
    object_key: str | None = None
    archive_size_bytes: int | None = None
    archive_sha256: str | None = None
    version_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        namespace: str,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        policy: SandboxArchivePolicy,
    ) -> _FinalArchiveMarker:
        return cls(
            namespace_sha256=hashlib.sha256(namespace.encode("utf-8")).hexdigest(),
            sandbox_id=spec.sandbox_id,
            generation=spec.generation,
            container_id=inspection.container_id,
            policy=policy,
        )

    def __post_init__(self) -> None:
        if len(self.namespace_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.namespace_sha256
        ):
            raise ValueError("final archive marker namespace identity is invalid")
        for name, value in (
            ("sandbox id", self.sandbox_id),
            ("container id", self.container_id),
        ):
            if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
                raise ValueError(f"final archive marker {name} is invalid")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("final archive marker generation is invalid")
        if self.state not in {"pending"} | _COMPLETED_FINAL_STATES:
            raise ValueError("final archive marker state is invalid")
        receipt_values = (
            self.object_key,
            self.archive_size_bytes,
            self.archive_sha256,
            self.version_id,
        )
        if self.state != "uploaded":
            if any(value is not None for value in receipt_values):
                raise ValueError("non-uploaded final archive marker contains a receipt")
        else:
            expected_key = (
                f"{SANDBOX_ARCHIVE_PREFIX}/v1/{self.namespace_sha256}/"
                f"{hashlib.sha256(self.sandbox_id.encode('utf-8')).hexdigest()}/"
                f"g{self.generation}/final.tar.gz"
            )
            if not self.policy.enabled:
                raise ValueError("disabled final archive marker cannot be uploaded")
            if (
                not isinstance(self.object_key, str)
                or self.object_key != expected_key
                or not isinstance(self.archive_size_bytes, int)
                or isinstance(self.archive_size_bytes, bool)
                or self.archive_size_bytes <= 0
                or self.archive_size_bytes > self.policy.max_archive_bytes
                or not isinstance(self.archive_sha256, str)
                or len(self.archive_sha256) != 64
                or any(character not in "0123456789abcdef" for character in self.archive_sha256)
                or not isinstance(self.version_id, str)
                or not self.version_id
                or self.version_id.strip() != self.version_id
                or len(self.version_id) > 1024
                or self.version_id.lower() == "null"
                or "\x00" in self.version_id
            ):
                raise ValueError("uploaded final archive marker receipt is invalid")

    def with_receipt(self, receipt: SandboxArchiveReceipt) -> _FinalArchiveMarker:
        if self.state != "pending":
            raise SandboxError("only a pending final archive can accept an upload receipt")
        if receipt.phase != "final" or receipt.bucket != SANDBOX_ARCHIVE_BUCKET:
            raise SandboxError("final archive upload returned an unexpected destination")
        return replace(
            self,
            state="uploaded",
            object_key=receipt.key,
            archive_size_bytes=receipt.size,
            archive_sha256=receipt.sha256,
            version_id=receipt.version_id,
        )

    def encode(self) -> bytes:
        payload = {
            "container_id": self.container_id,
            "format_version": 1,
            "generation": self.generation,
            "namespace_sha256": self.namespace_sha256,
            "policy": self.policy.as_dict(),
            "receipt": (
                None
                if self.state != "uploaded"
                else {
                    "archive_sha256": self.archive_sha256,
                    "archive_size_bytes": self.archive_size_bytes,
                    "object_key": self.object_key,
                    "version_id": self.version_id,
                }
            ),
            "sandbox_id": self.sandbox_id,
            "state": self.state,
        }
        return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")

    @classmethod
    def decode(cls, payload: bytes) -> _FinalArchiveMarker:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxError("final archive marker is not canonical JSON") from exc
        if not isinstance(value, dict) or set(value) != {
            "container_id",
            "format_version",
            "generation",
            "namespace_sha256",
            "policy",
            "receipt",
            "sandbox_id",
            "state",
        }:
            raise SandboxError("final archive marker has an invalid schema")
        if value["format_version"] != 1 or isinstance(value["format_version"], bool):
            raise SandboxError("final archive marker format version is unsupported")
        if not isinstance(value["policy"], dict):
            raise SandboxError("final archive marker policy is invalid")
        receipt = value["receipt"]
        if receipt is not None and (
            not isinstance(receipt, dict)
            or set(receipt) != {"archive_sha256", "archive_size_bytes", "object_key", "version_id"}
        ):
            raise SandboxError("final archive marker receipt is invalid")
        try:
            marker = cls(
                namespace_sha256=value["namespace_sha256"],
                sandbox_id=value["sandbox_id"],
                generation=value["generation"],
                container_id=value["container_id"],
                policy=SandboxArchivePolicy.from_mapping(value["policy"]),
                state=value["state"],
                object_key=None if receipt is None else receipt["object_key"],
                archive_size_bytes=None if receipt is None else receipt["archive_size_bytes"],
                archive_sha256=None if receipt is None else receipt["archive_sha256"],
                version_id=None if receipt is None else receipt["version_id"],
            )
        except (TypeError, ValueError) as exc:
            raise SandboxError("final archive marker has invalid values") from exc
        if marker.encode() != payload:
            raise SandboxError("final archive marker is not canonical")
        return marker


class SandboxArchiveController:
    """Apply DB policy revisions without rebinding the supervisor stop hook."""

    def __init__(
        self,
        manager: ChatSandboxManager,
        *,
        namespace: str,
        service_factory: Callable[[SandboxArchiveS3Config], _ArchiveService] | None = None,
        checkpoint_capture: SandboxCheckpointCapture | None = None,
    ) -> None:
        if not namespace:
            raise ValueError("sandbox archive namespace must not be empty")
        self._manager = manager
        self._namespace = namespace
        self._checkpoint_capture = checkpoint_capture
        self._service_factory = service_factory or self._new_service
        # Every policy revision may temporarily retain its own service for an
        # in-flight capture or failed final retry. They nevertheless share one
        # host-wide spool admission gate, so rapid policy changes cannot
        # multiply the service's documented aggregate scratch-space bound.
        self._capture_slots = threading.BoundedSemaphore(1)
        self._condition = threading.Condition()
        self._policy: SandboxArchivePolicy | None = None
        self._current: _ServiceSlot | None = None
        self._retired: set[_ServiceSlot] = set()
        self._final_pins: dict[tuple[str, int, str], _ServiceSlot] = {}
        self._final_receipts: dict[tuple[str, int, str], SandboxArchiveReceipt] = {}
        self._started = False
        self._closed = False

    def _config_for(self, policy: SandboxArchivePolicy) -> SandboxArchiveS3Config:
        """Build an uploader config for one pinned policy.

        Markers inside the sandbox omit credentials. A recovered upload uses
        the access key the sandbox manager currently holds; the marker still chooses
        the region, KMS key, owner and size limits.
        """
        current = self._policy
        access_key_id = policy.aws_access_key_id
        secret = policy.aws_secret_access_key
        if secret == "" and current is not None and current.aws_secret_access_key:
            access_key_id = current.aws_access_key_id
            secret = current.aws_secret_access_key
        return SandboxArchiveS3Config.from_policy(
            policy,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret,
        )

    def _new_service(self, config: SandboxArchiveS3Config) -> _ArchiveService:
        uploader = SandboxArchiveS3Uploader(config)
        return SandboxArchiveService(
            self._manager,
            namespace=self._namespace,
            uploader=uploader,
            config=config,
            capture_slots=self._capture_slots,
            checkpoint_capture=self._checkpoint_capture,
        )

    @property
    def configured(self) -> bool:
        """Whether the runner has delivered an explicit policy, including disabled."""
        with self._condition:
            return self._policy is not None

    @property
    def enabled(self) -> bool:
        """Whether the last applied policy enables new archive captures."""
        with self._condition:
            return bool(self._policy and self._policy.enabled)

    @property
    def applied_revision(self) -> int | None:
        """The exact desired revision this sandbox manager accepted."""
        with self._condition:
            return None if self._policy is None else self._policy.revision

    def start(self) -> None:
        """Admit policy application and start an already-configured service."""
        service: _ArchiveService | None = None
        with self._condition:
            if self._closed:
                raise RuntimeError("sandbox archive controller is closed")
            if self._started:
                return
            self._started = True
            if self._current is not None:
                service = self._current.service
        if service is not None:
            try:
                service.start()
            except Exception:
                with self._condition:
                    self._started = False
                raise

    def close(self) -> None:
        """Stop policy changes after lifecycle callers have drained."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._started = False
            while any(slot.active for slot in self._all_slots_locked()):
                self._condition.wait()
            slots = self._all_slots_locked()
            self._current = None
            self._retired.clear()
            self._final_pins.clear()
            self._final_receipts.clear()
        self._close_services([slot.service for slot in slots])

    def apply(self, settings: Mapping[str, Any]) -> dict[str, object]:
        """Validate and atomically apply one complete DB policy revision."""
        policy = SandboxArchivePolicy.from_mapping(settings)
        # Heartbeats re-offer the complete current revision.  Resolve that
        # overwhelmingly common case before allocating an unused uploader and
        # service.  The guarded checks below repeat this after construction to
        # fence concurrent policy calls.
        with self._condition:
            if self._closed:
                raise RuntimeError("sandbox archive controller is closed")
            if self._policy is not None and policy.revision < self._policy.revision:
                raise ValueError("sandbox archive policy revision is stale")
            if self._policy is not None and policy.revision == self._policy.revision:
                if not policy.equivalent(self._policy):
                    raise ValueError("sandbox archive policy changed without a new revision")
                return self.status()

        candidate: _ArchiveService | None = None
        if policy.enabled:
            candidate = self._service_factory(SandboxArchiveS3Config.from_policy(policy))

        close_candidate = False
        services_to_close: list[_ArchiveService] = []
        with self._condition:
            if self._closed:
                close_candidate = candidate is not None
                error: Exception | None = RuntimeError("sandbox archive controller is closed")
            elif self._policy is not None and policy.revision < self._policy.revision:
                close_candidate = candidate is not None
                error = ValueError("sandbox archive policy revision is stale")
            elif self._policy is not None and policy.revision == self._policy.revision:
                close_candidate = candidate is not None
                error = (
                    None
                    if policy.equivalent(self._policy)
                    else ValueError("sandbox archive policy changed without a new revision")
                )
            else:
                error = None
                if candidate is not None and self._started:
                    # start() is local state only; no AWS request occurs here.
                    try:
                        candidate.start()
                    except Exception as exc:
                        close_candidate = True
                        error = exc
                if error is None:
                    previous = self._current
                    self._policy = policy
                    self._current = (
                        None
                        if candidate is None
                        else _ServiceSlot(
                            revision=policy.revision,
                            policy=policy,
                            service=candidate,
                        )
                    )
                    if previous is not None:
                        previous.retired = True
                        self._retired.add(previous)
                        services_to_close.extend(self._closable_locked())

        if close_candidate and candidate is not None:
            self._close_services([candidate])
        self._close_services(services_to_close)
        if error is not None:
            raise error
        return self.status()

    def status(self) -> dict[str, object]:
        """Return policy rollout and bounded counters, never destination identity."""
        with self._condition:
            policy = self._policy
            slot = self._current
        if policy is None:
            return {
                "configured": False,
                "enabled": False,
                "running": False,
                "applied_revision": None,
            }
        if slot is None:
            return {
                "configured": True,
                "enabled": False,
                "running": False,
                "applied_revision": policy.revision,
            }
        return {
            **slot.service.status(),
            "configured": True,
            "enabled": True,
            "applied_revision": policy.revision,
        }

    def probe_initial(
        self,
        backend: RetainedChatBackend,
        *,
        expected_container_id: str,
        preparation_id: str,
        members: Sequence[SharedMemberManifest],
    ) -> dict[str, object]:
        """Probe using the immutable service revision acquired for this call."""
        slot, marker = self._acquire_initial_slot(
            backend,
            expected_container_id=expected_container_id,
            preparation_id=preparation_id,
        )
        if slot is None:
            return {"enabled": False, "prepared": False, "stored": False}
        try:
            # The marker names the activation that created the immutable
            # baseline. A later never-settled job may recover it, but recovery
            # must continue using that original identity all the way through
            # the service's marker and S3 checks.
            effective_preparation_id = (
                marker.preparation_id if marker is not None else preparation_id
            )
            return slot.service.probe_initial(
                backend,
                expected_container_id=expected_container_id,
                preparation_id=effective_preparation_id,
                members=members,
            )
        finally:
            self._release(slot)

    def capture_initial(
        self,
        backend: RetainedChatBackend,
        *,
        expected_container_id: str,
        preparation_id: str,
        base_sha: str,
        members: Sequence[SharedMemberManifest],
    ) -> dict[str, object]:
        """Capture using the immutable service revision acquired for this call."""
        slot, marker = self._acquire_initial_slot(
            backend,
            expected_container_id=expected_container_id,
            preparation_id=preparation_id,
        )
        if slot is None:
            return {"enabled": False, "stored": False}
        try:
            if marker is not None:
                if marker.base_sha != base_sha:
                    raise SandboxError("initial archive marker does not match this preparation")
                return slot.service.probe_initial(
                    backend,
                    expected_container_id=expected_container_id,
                    preparation_id=marker.preparation_id,
                    members=members,
                )
            return slot.service.capture_initial(
                backend,
                expected_container_id=expected_container_id,
                preparation_id=preparation_id,
                base_sha=base_sha,
                members=members,
            )
        finally:
            self._release(slot)

    def prepare_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
    ) -> None:
        """Durably bind the next exact stop to the currently applied policy.

        The marker is written while the container is still running and before
        Docker stop. An enabled service is pinned first, so a concurrent policy
        revision cannot close it while the fsynced marker is being published.
        Disabled policies are marked too: after a crash, they distinguish an
        intentionally unarchived stop from a stale marker left by an older
        enabled lifecycle.
        """
        self._prepare_final_marker(spec, inspection, stopped=False)

    def prepare_markerless_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
    ) -> bool:
        """Fence a recovered markerless lifecycle so it can finish without upload.

        A running group normally has no final marker: ``complete_start`` removes
        its start fail-stop fence.  After a sandbox manager restart, a reboot or a stop
        that bypassed the broker, its native history is unknown. Under a
        disabled policy this writes the ordinary disabled marker; under an
        enabled one it writes an explicit ``skipped`` final, never an archive of
        unknown history. Hold the policy lock through the durable marker write
        so a concurrent enable cannot turn unknown native history into an
        enabled archive.

        ``True`` means a durable marker now authorizes ordinary finalization.
        ``False`` means no policy has been delivered yet, so the supervisor must
        keep the markerless group fenced until one arrives.
        """
        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            policy = self._policy
            if policy is None:
                return False
            if policy.enabled:
                # A markerless lifecycle has no prepare pin to release.
                self._write_skipped_final(spec, inspection, policy, release_pin=False)
                return True
            # Condition uses an RLock.  Keeping this outer acquisition while
            # the shared helper re-enters it freezes the disabled revision
            # until its marker is fsynced.
            self._prepare_final_marker(
                spec,
                inspection,
                stopped=not inspection.running,
            )
            return True

    def prepare_start(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Arm a policy fence while stopped in case container start fail-stops."""
        self._prepare_final_marker(spec, inspection, stopped=True)

    def complete_start(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Clear the start fail-stop fence once the container is safely running."""
        key = (spec.sandbox_id, spec.generation, inspection.container_id)
        # Release the in-memory pin before deleting the durable fence. Retired
        # client cleanup is best-effort, so it cannot turn this committed
        # lifecycle transition back into a start failure.
        self._release_final_pin(key)
        self._manager.clear_final_archive_marker(
            spec,
            expected_container_id=inspection.container_id,
        )

    def invalidate_final(self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection) -> None:
        """Durably skip this lifecycle's archive when native history was not flushed."""
        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            policy = self._policy
            if policy is None:
                raise SandboxError("sandbox archive policy has not been delivered")
        self._write_skipped_final(spec, inspection, policy)

    def _write_skipped_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        policy: SandboxArchivePolicy,
        *,
        release_pin: bool = True,
    ) -> None:
        """Record that this exact lifecycle finishes without a final archive."""
        key = (spec.sandbox_id, spec.generation, inspection.container_id)
        marker = replace(
            _FinalArchiveMarker.create(
                namespace=self._namespace,
                spec=spec,
                inspection=inspection,
                policy=policy,
            ),
            state="skipped",
        )
        writer = (
            self._manager.write_final_archive_marker
            if inspection.running
            else self._manager.write_stopped_final_archive_marker
        )
        writer(
            spec,
            marker.encode(),
            expected_container_id=inspection.container_id,
        )
        # A pin left by an earlier prepare must not upload this lifecycle.
        if release_pin:
            self._release_final_pin(key)
        logger.warning(
            "sandbox final archive skipped: native history was not proven flushed",
            extra={
                "event": "sandbox_final_archive_skipped",
                "sandbox_id": spec.sandbox_id,
                "generation": spec.generation,
            },
        )

    def _prepare_final_marker(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        *,
        stopped: bool,
    ) -> None:
        key = (spec.sandbox_id, spec.generation, inspection.container_id)
        reader = (
            self._manager.read_stopped_final_archive_marker_optional
            if stopped
            else self._manager.read_running_final_archive_marker
        )
        existing_payload = reader(
            spec,
            expected_container_id=inspection.container_id,
        )
        existing: _FinalArchiveMarker | None = None
        if existing_payload is not None:
            existing = _FinalArchiveMarker.decode(existing_payload)
            if (
                existing.sandbox_id != spec.sandbox_id
                or existing.generation != spec.generation
                or existing.container_id != inspection.container_id
                or existing.namespace_sha256
                != hashlib.sha256(self._namespace.encode("utf-8")).hexdigest()
            ):
                raise SandboxError("final archive marker does not match the sandbox")
        # An uploaded or skipped final completed the prior stopped lifecycle.
        replace_uploaded_history = bool(
            stopped and existing is not None and existing.state in _COMPLETED_FINAL_STATES
        )
        added_pin = False
        services_to_close: list[_ArchiveService] = []
        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            policy = self._policy
            if policy is None:
                raise SandboxError("sandbox archive policy has not been delivered")
            slot = self._final_pins.get(key)
            if replace_uploaded_history:
                assert existing is not None
                if slot is not None and slot.policy != existing.policy:
                    raise SandboxError(
                        "final archive in-memory pin does not match its durable policy"
                    )
                current_slot = self._current
                if policy.enabled and (current_slot is None or current_slot.policy != policy):
                    raise SandboxError("sandbox archive service does not match current policy")

                # The completed marker closed the prior stopped lifecycle.
                # Starting the same container again needs a fresh fence under
                # today's policy so a start fail-stop cannot reuse that outcome.
                if slot is not None:
                    self._final_pins.pop(key, None)
                    slot.pins.discard(key)
                self._final_receipts.pop(key, None)
                marker_policy = policy
                slot = current_slot
                if marker_policy.enabled:
                    assert slot is not None
                    self._final_pins[key] = slot
                    slot.pins.add(key)
                    added_pin = True
                services_to_close = self._closable_locked()
                self._condition.notify_all()
            elif existing is not None:
                # A marker that made it to the sandbox-manager-private filesystem is
                # the durable revision fence. A replacement sandbox manager may see a
                # newer (or disabled) DB policy before it finishes stopping a
                # running orphan; never overwrite the already-fsynced policy.
                marker_policy = existing.policy
                if existing.state in _COMPLETED_FINAL_STATES:
                    return
                if slot is not None and slot.policy != marker_policy:
                    raise SandboxError(
                        "final archive in-memory pin does not match its durable policy"
                    )
                if marker_policy.enabled and slot is None:
                    slot = next(
                        (
                            candidate
                            for candidate in self._all_slots_locked()
                            if candidate.policy == marker_policy
                        ),
                        None,
                    )
                    if slot is None:
                        service = self._service_factory(self._config_for(marker_policy))
                        try:
                            service.start()
                        except BaseException:
                            self._close_services([service])
                            raise
                        slot = _ServiceSlot(
                            revision=marker_policy.revision,
                            policy=marker_policy,
                            service=service,
                            retired=True,
                        )
                        self._retired.add(slot)
                    self._final_pins[key] = slot
                    slot.pins.add(key)
                    added_pin = True
            elif slot is not None:
                marker_policy = slot.policy
            else:
                marker_policy = policy
                slot = self._current
                if marker_policy.enabled:
                    if slot is None or slot.policy != marker_policy:
                        raise SandboxError("sandbox archive service does not match current policy")
                    self._final_pins[key] = slot
                    slot.pins.add(key)
                    added_pin = True
        self._close_services(services_to_close)
        if existing is not None and not replace_uploaded_history:
            return
        marker = _FinalArchiveMarker.create(
            namespace=self._namespace,
            spec=spec,
            inspection=inspection,
            policy=marker_policy,
        )
        try:
            writer = (
                self._manager.write_stopped_final_archive_marker
                if stopped
                else self._manager.write_final_archive_marker
            )
            writer(
                spec,
                marker.encode(),
                expected_container_id=inspection.container_id,
            )
        except BaseException:
            services_to_close = []
            if added_pin:
                with self._condition:
                    if self._final_pins.get(key) is slot:
                        self._final_pins.pop(key, None)
                        assert slot is not None
                        slot.pins.discard(key)
                    services_to_close = self._closable_locked()
                    self._condition.notify_all()
            self._close_services(services_to_close)
            raise

    def _release_final_pin(self, key: tuple[str, int, str]) -> None:
        with self._condition:
            slot = self._final_pins.pop(key, None)
            self._final_receipts.pop(key, None)
            if slot is not None:
                slot.pins.discard(key)
            services_to_close = self._closable_locked()
            self._condition.notify_all()
        self._close_services(services_to_close)

    def queue_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> None:
        """Archive a clean stop, pinning failed retries to their original service."""
        key = (spec.sandbox_id, spec.generation, inspection.container_id)
        slot = self._acquire_final_slot(spec, inspection, key)
        if slot is None:
            return
        succeeded = False
        try:
            with self._condition:
                receipt = self._final_receipts.get(key)
            if receipt is None:
                receipt = slot.service.queue_final(spec, inspection, members)
                with self._condition:
                    self._final_receipts[key] = receipt
            uploaded_marker = _FinalArchiveMarker.create(
                namespace=self._namespace,
                spec=spec,
                inspection=inspection,
                policy=slot.policy,
            ).with_receipt(receipt)
            self._manager.write_stopped_final_archive_marker(
                spec,
                uploaded_marker.encode(),
                expected_container_id=inspection.container_id,
            )
            if slot.policy.checkpoints_enabled and self._checkpoint_capture is not None:
                self._capture_checkpoint(slot, spec, inspection, members)
            succeeded = True
        finally:
            services_to_close: list[_ArchiveService]
            with self._condition:
                slot.active -= 1
                if succeeded:
                    self._final_pins.pop(key, None)
                    self._final_receipts.pop(key, None)
                    slot.pins.discard(key)
                services_to_close = self._closable_locked()
                self._condition.notify_all()
            self._close_services(services_to_close)

    @staticmethod
    def _capture_checkpoint(
        slot: _ServiceSlot,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> None:
        """Checkpoint a clean stop whose final archive is already durable.

        A checkpoint is an optimisation for moving the chat later, never a
        condition of stopping it: a failure is logged and the stop completes.
        """
        try:
            receipt = slot.service.capture_checkpoint(spec, inspection, members)
        except Exception as exc:
            logger.warning(
                "sandbox checkpoint failed; the clean stop still completes",
                extra={
                    "event": "sandbox_checkpoint_failed",
                    "sandbox_id": spec.sandbox_id,
                    "generation": spec.generation,
                    "error_type": safe_archive_error_type(exc),
                },
            )
            return
        logger.info(
            "sandbox checkpoint stored",
            extra={
                "event": "sandbox_checkpoint_stored",
                "sandbox_id": spec.sandbox_id,
                "generation": spec.generation,
                "size_bytes": receipt.size_bytes,
                "new_layer_bytes": receipt.new_layer_bytes,
                "durations_ms": receipt.durations_ms,
            },
        )

    def _acquire_final_slot(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        key: tuple[str, int, str],
    ) -> _ServiceSlot | None:
        """Acquire an in-memory pin or reconstruct its exact persisted policy."""
        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            if self._policy is None:
                raise SandboxError("sandbox archive policy has not been delivered")
            slot = self._final_pins.get(key)
            if slot is not None:
                slot.active += 1
                return slot

        # No in-memory pin means this is recovery after sandbox manager restart. The
        # retained bridge says completion is pending; only the root-owned marker
        # may choose a policy. Never substitute the current DB revision.
        marker = _FinalArchiveMarker.decode(
            self._manager.read_final_archive_marker(
                spec,
                expected_container_id=inspection.container_id,
            )
        )
        expected_namespace = hashlib.sha256(self._namespace.encode("utf-8")).hexdigest()
        if (
            marker.namespace_sha256 != expected_namespace
            or marker.sandbox_id != spec.sandbox_id
            or marker.generation != spec.generation
            or marker.container_id != inspection.container_id
        ):
            raise SandboxError("final archive marker does not match the stopped sandbox")
        if not marker.policy.enabled:
            return None
        if marker.state in _COMPLETED_FINAL_STATES:
            return None

        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            slot = self._final_pins.get(key)
            if slot is None:
                slot = next(
                    (
                        candidate
                        for candidate in self._all_slots_locked()
                        if candidate.policy == marker.policy
                    ),
                    None,
                )
            if slot is None:
                service = self._service_factory(self._config_for(marker.policy))
                try:
                    service.start()
                except BaseException:
                    self._close_services([service])
                    raise
                slot = _ServiceSlot(
                    revision=marker.policy.revision,
                    policy=marker.policy,
                    service=service,
                    retired=True,
                )
                self._retired.add(slot)
            self._final_pins[key] = slot
            slot.pins.add(key)
            slot.active += 1
            return slot

    def _acquire_current(self) -> _ServiceSlot | None:
        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            if self._policy is None:
                raise SandboxError("sandbox archive policy has not been delivered")
            slot = self._current
            if slot is not None:
                slot.active += 1
            return slot

    def _acquire_initial_slot(
        self,
        backend: RetainedChatBackend,
        *,
        expected_container_id: str,
        preparation_id: str,
    ) -> tuple[_ServiceSlot | None, SandboxInitialArchiveMarker | None]:
        """Resolve a prepared baseline before consulting the current policy.

        A response-lost initial upload can outlive a disable or key/size
        rotation. The root-owned marker is therefore authoritative for that
        immutable baseline, and recovery reconstructs its exact original
        service instead of probing or overwriting it with current settings.
        """
        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            if self._policy is None:
                raise SandboxError("sandbox archive policy has not been delivered")

        payload = self._manager.read_initial_archive_marker(
            backend.spec,
            expected_container_id=expected_container_id,
        )
        marker = None if payload is None else SandboxInitialArchiveMarker.decode(payload)
        if marker is not None:
            expected_namespace = hashlib.sha256(self._namespace.encode("utf-8")).hexdigest()
            if (
                marker.namespace_sha256 != expected_namespace
                or marker.sandbox_id != backend.spec.sandbox_id
                or marker.generation != backend.spec.generation
                or marker.container_id != expected_container_id
            ):
                raise SandboxError("initial archive marker does not match this preparation")

        with self._condition:
            if self._closed or not self._started:
                raise SandboxError("sandbox archive controller is not running")
            if self._policy is None:
                raise SandboxError("sandbox archive policy has not been delivered")
            if marker is None:
                slot = self._current
            else:
                slot = next(
                    (
                        candidate
                        for candidate in self._all_slots_locked()
                        if candidate.policy == marker.policy
                    ),
                    None,
                )
                if slot is None:
                    service = self._service_factory(self._config_for(marker.policy))
                    try:
                        service.start()
                    except BaseException:
                        self._close_services([service])
                        raise
                    slot = _ServiceSlot(
                        revision=marker.policy.revision,
                        policy=marker.policy,
                        service=service,
                        retired=True,
                    )
                    self._retired.add(slot)
            if slot is not None:
                slot.active += 1
            return slot, marker

    def _release(self, slot: _ServiceSlot) -> None:
        with self._condition:
            slot.active -= 1
            services_to_close = self._closable_locked()
            self._condition.notify_all()
        self._close_services(services_to_close)

    @staticmethod
    def _close_services(services: Sequence[_ArchiveService]) -> None:
        """Close every retired client without changing committed lifecycle state."""
        for service in services:
            try:
                service.close()
            except Exception as exc:
                logger.error(
                    "retired sandbox archive service cleanup failed",
                    extra={
                        "event": "sandbox_archive_service_close_failed",
                        "error_type": safe_archive_error_type(exc),
                    },
                )

    def _all_slots_locked(self) -> list[_ServiceSlot]:
        slots = set(self._retired)
        if self._current is not None:
            slots.add(self._current)
        slots.update(self._final_pins.values())
        return list(slots)

    def _closable_locked(self) -> list[_ArchiveService]:
        closable = [slot for slot in self._retired if slot.active == 0 and not slot.pins]
        for slot in closable:
            self._retired.discard(slot)
        return [slot.service for slot in closable]


__all__ = ["SandboxArchiveController"]
