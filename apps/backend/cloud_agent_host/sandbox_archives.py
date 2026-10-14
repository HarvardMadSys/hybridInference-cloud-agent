"""Lifecycle orchestration for export-only retained-sandbox S3 archives.

Only the trusted sandbox manager constructs this service.  A runner can ask
for the one pre-prompt baseline over the sandbox manager API, and the shared-session
supervisor can hand it a stopped container for a final capture.  Neither path
puts S3 credentials or an S3 client in the agent container.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Final, cast

from cloud_agent_host.sandbox_archive_bundle import build_sandbox_archive_bundle
from cloud_agent_host.sandbox_archive_s3 import (
    SandboxArchiveAlreadyExists,
    SandboxArchiveDeadlineExceeded,
    SandboxArchiveReceipt,
    SandboxArchiveS3Config,
    SandboxArchiveS3Uploader,
    is_sandbox_archive_conditional_conflict,
)
from cloud_agent_shared.errors import SandboxError
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cloud_agent_host.chat_sandbox import (
        ChatSandboxInspection,
        ChatSandboxManager,
        ChatSandboxSpec,
        RetainedChatBackend,
    )
    from cloud_agent_host.sandbox_archive_bundle import ArchivePhase
    from cloud_agent_host.sandbox_checkpoints import (
        SandboxCheckpointCapture,
        SandboxCheckpointReceipt,
    )
    from cloud_agent_host.shared_session_supervisor import SharedMemberManifest

logger = logging.getLogger(__name__)

GIB: Final = 1024 * 1024 * 1024
# Building one bundle temporarily holds both the uncompressed Docker archive
# streams and the compressed outer object. Keep that aggregate bounded even
# when a caller raises the per-object S3 limit to its supported maximum.
DEFAULT_ARCHIVE_SPOOL_BUDGET_BYTES: Final = 8 * GIB
DEFAULT_ARCHIVE_OPERATION_TIMEOUT_S: Final = 12 * 60.0
INITIAL_ARCHIVE_MARKER_FORMAT_VERSION: Final = 2
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_ERROR_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def safe_archive_error_type(error: BaseException) -> str:
    """Return a bounded class name without copying provider-controlled text."""
    name = type(error).__name__
    return name if _ERROR_TYPE.fullmatch(name) else "Error"


class _BoundedBundleWriter:
    """Keep the compressed outer object inside the configured S3 ceiling."""

    def __init__(
        self,
        stream: BinaryIO,
        maximum: int,
        *,
        check_deadline: Callable[[], None],
    ) -> None:
        self._stream = stream
        self._maximum = maximum
        self._check_deadline = check_deadline
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes | bytearray | memoryview) -> int:
        self._check_deadline()
        projected = self.size + len(chunk)
        if projected > self._maximum:
            raise SandboxError(
                f"compressed sandbox archive exceeds the {self._maximum}-byte object limit"
            )
        written = self._stream.write(chunk)
        self._check_deadline()
        if written != len(chunk):
            raise OSError("short write while spooling a compressed sandbox archive")
        self._digest.update(chunk)
        self.size = projected
        return written

    def flush(self) -> None:
        self._check_deadline()
        self._stream.flush()
        self._check_deadline()

    def tell(self) -> int:
        self._check_deadline()
        return self._stream.tell()

    @property
    def sha256(self) -> str:
        """Return the digest of every compressed byte written so far."""
        return self._digest.hexdigest()


@dataclass
class _BuiltArchive:
    stream: BinaryIO
    size: int
    sha256: str

    def close(self) -> None:
        self.stream.close()


@dataclass(frozen=True)
class SandboxInitialArchiveMarker:
    """Strict canonical identity for a prepared immutable baseline."""

    namespace_sha256: str
    sandbox_id: str
    generation: int
    container_id: str
    preparation_id: str
    base_sha: str
    policy: SandboxArchivePolicy
    bundle_size_bytes: int
    bundle_sha256: str
    format_version: int = INITIAL_ARCHIVE_MARKER_FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != INITIAL_ARCHIVE_MARKER_FORMAT_VERSION:
            raise ValueError("initial archive marker format version is unsupported")
        if not _SHA256.fullmatch(self.namespace_sha256):
            raise ValueError("initial archive marker namespace identity is invalid")
        for name, value, maximum in (
            ("sandbox id", self.sandbox_id, 256),
            ("container id", self.container_id, 256),
            ("preparation id", self.preparation_id, 256),
        ):
            if not isinstance(value, str) or not value or "\x00" in value or len(value) > maximum:
                raise ValueError(f"initial archive marker {name} is invalid")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("initial archive marker generation is invalid")
        if not isinstance(self.base_sha, str) or not _GIT_OBJECT_ID.fullmatch(self.base_sha):
            raise ValueError("initial archive marker base SHA is invalid")
        if not isinstance(self.policy, SandboxArchivePolicy) or not self.policy.enabled:
            raise ValueError("initial archive marker policy must be enabled")
        if (
            isinstance(self.bundle_size_bytes, bool)
            or not isinstance(self.bundle_size_bytes, int)
            or self.bundle_size_bytes < 1
            or self.bundle_size_bytes > self.policy.max_archive_bytes
        ):
            raise ValueError("initial archive marker bundle size is invalid")
        if not isinstance(self.bundle_sha256, str) or not _SHA256.fullmatch(self.bundle_sha256):
            raise ValueError("initial archive marker bundle SHA-256 is invalid")

    def encode(self) -> bytes:
        """Encode this marker as bounded canonical JSON."""
        payload = {
            "base_sha": self.base_sha,
            "bundle_sha256": self.bundle_sha256,
            "bundle_size_bytes": self.bundle_size_bytes,
            "container_id": self.container_id,
            "format_version": self.format_version,
            "generation": self.generation,
            "namespace_sha256": self.namespace_sha256,
            "policy": self.policy.as_dict(),
            "preparation_id": self.preparation_id,
            "sandbox_id": self.sandbox_id,
        }
        return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")

    @classmethod
    def decode(cls, payload: bytes) -> SandboxInitialArchiveMarker:
        """Decode and validate one canonical initial-archive marker."""
        try:
            decoded = payload.decode("ascii")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxError("initial archive marker is not canonical JSON") from exc
        if not isinstance(value, dict) or set(value) != {
            "base_sha",
            "bundle_sha256",
            "bundle_size_bytes",
            "container_id",
            "format_version",
            "generation",
            "namespace_sha256",
            "policy",
            "preparation_id",
            "sandbox_id",
        }:
            raise SandboxError("initial archive marker has an invalid schema")
        try:
            policy = value.pop("policy", None)
            if not isinstance(policy, dict):
                raise ValueError("initial archive marker policy is invalid")
            marker = cls(policy=SandboxArchivePolicy.from_mapping(policy), **value)
        except (TypeError, ValueError) as exc:
            raise SandboxError("initial archive marker has invalid values") from exc
        if marker.encode() != payload:
            raise SandboxError("initial archive marker is not canonical")
        return marker


class SandboxArchiveService:
    """Capture immutable points and synchronously persist stopped sandboxes.

    Final capture deliberately has no in-memory work queue. The stop hook
    returns only after S3 accepted the final object, so a successful clean stop
    cannot silently lose an archive to queue pressure or a later sandbox manager
    shutdown. A small deadline-bounded semaphore applies backpressure
    *before* Docker starts copying bytes.
    """

    def __init__(
        self,
        manager: ChatSandboxManager,
        *,
        namespace: str,
        uploader: SandboxArchiveS3Uploader,
        config: SandboxArchiveS3Config,
        capture_capacity: int = 1,
        capture_slots: threading.BoundedSemaphore | None = None,
        spool_budget_bytes: int = DEFAULT_ARCHIVE_SPOOL_BUDGET_BYTES,
        max_upload_attempts: int = 3,
        retry_delay_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        operation_timeout_s: float = DEFAULT_ARCHIVE_OPERATION_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        checkpoint_capture: SandboxCheckpointCapture | None = None,
    ) -> None:
        if not namespace:
            raise ValueError("sandbox archive namespace must not be empty")
        if isinstance(capture_capacity, bool) or not isinstance(capture_capacity, int):
            raise ValueError("sandbox archive capture capacity must be a positive integer")
        if capture_capacity <= 0:
            raise ValueError("sandbox archive capture capacity must be a positive integer")
        if capture_slots is not None and capture_capacity != 1:
            raise ValueError(
                "an injected sandbox archive capture gate requires capture capacity one"
            )
        if isinstance(spool_budget_bytes, bool) or not isinstance(spool_budget_bytes, int):
            raise ValueError("sandbox archive spool budget must be a positive integer")
        if spool_budget_bytes <= 0:
            raise ValueError("sandbox archive spool budget must be a positive integer")
        if isinstance(max_upload_attempts, bool) or not isinstance(max_upload_attempts, int):
            raise ValueError("sandbox archive upload attempts must be a positive integer")
        if max_upload_attempts <= 0:
            raise ValueError("sandbox archive upload attempts must be a positive integer")
        if (
            isinstance(retry_delay_s, bool)
            or not isinstance(retry_delay_s, (int, float))
            or not math.isfinite(retry_delay_s)
            or retry_delay_s < 0
        ):
            raise ValueError("sandbox archive retry delay must be finite and non-negative")
        if not callable(sleep):
            raise TypeError("sandbox archive sleep callback must be callable")
        if (
            isinstance(operation_timeout_s, bool)
            or not isinstance(operation_timeout_s, (int, float))
            or not math.isfinite(operation_timeout_s)
            or operation_timeout_s <= 0
        ):
            raise ValueError("sandbox archive operation timeout must be finite and positive")
        if not callable(clock):
            raise TypeError("sandbox archive clock must be callable")
        if uploader.config != config:
            raise ValueError("sandbox archive uploader and service configuration differ")
        # At peak, one build owns input Docker tar streams totaling at most
        # max_archive_bytes and one compressed outer bundle with the same
        # ceiling. Refuse unsafe concurrency at construction rather than
        # discovering disk pressure after a stopped container emitted bytes.
        worst_case_spool_bytes = 2 * config.max_archive_bytes * capture_capacity
        if worst_case_spool_bytes > spool_budget_bytes:
            raise ValueError(
                "sandbox archive capture capacity and object limit require "
                f"{worst_case_spool_bytes} spool bytes, above the "
                f"{spool_budget_bytes}-byte budget"
            )
        self._manager = manager
        self._namespace = namespace
        self._uploader = uploader
        self._config = config
        self._policy = config.as_policy()
        self._capture_slots = capture_slots or threading.BoundedSemaphore(capture_capacity)
        self._max_upload_attempts = max_upload_attempts
        self._retry_delay_s = retry_delay_s
        self._sleep = sleep
        self._operation_timeout_s = float(operation_timeout_s)
        self._clock = clock
        self._lock = threading.Condition()
        self._accepting = False
        self._closing = False
        self._closed = False
        self._checkpoint_capture = checkpoint_capture
        self._phase_counts = {
            "initial": {"pending": 0, "uploaded": 0, "failed": 0},
            "final": {"pending": 0, "uploaded": 0, "failed": 0},
        }
        if checkpoint_capture is not None:
            self._phase_counts["checkpoint"] = {"pending": 0, "uploaded": 0, "failed": 0}

    def start(self) -> None:
        """Allow archive captures before the sandbox manager accepts requests."""
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError("sandbox archive service is closed")
            if self._accepting:
                return
            self._accepting = True

    def close(self) -> None:
        """Reject new captures, drain admitted work, then close SDK pools."""
        with self._lock:
            if self._closed:
                return
            if self._closing:
                while not self._closed:
                    self._lock.wait()
                return
            self._closing = True
            self._accepting = False
            while any(counts["pending"] for counts in self._phase_counts.values()):
                self._lock.wait()
        try:
            self._uploader.close()
        finally:
            with self._lock:
                self._closed = True
                self._closing = False
                self._lock.notify_all()

    def status(self) -> dict[str, object]:
        """Return bounded operational counters without object or user ids."""
        with self._lock:
            phases = {phase: dict(counts) for phase, counts in self._phase_counts.items()}
            return {
                "enabled": True,
                "running": self._accepting and not self._closed,
                "pending": sum(counts["pending"] for counts in phases.values()),
                "uploaded": sum(counts["uploaded"] for counts in phases.values()),
                "failed": sum(counts["failed"] for counts in phases.values()),
                "phases": phases,
            }

    def _admit_capture(self, phase: str) -> None:
        """Count an operation before it can wait for scarce capture capacity."""
        with self._lock:
            if not self._accepting or self._closed:
                raise SandboxError("sandbox archive service is not running")
            self._phase_counts[phase]["pending"] += 1

    def _finish_capture(self, phase: str, *, uploaded: bool | None) -> None:
        with self._lock:
            counts = self._phase_counts[phase]
            counts["pending"] -= 1
            if uploaded is not None:
                counts["uploaded" if uploaded else "failed"] += 1
            self._lock.notify_all()

    def capture_initial(
        self,
        backend: RetainedChatBackend,
        *,
        expected_container_id: str,
        preparation_id: str,
        base_sha: str,
        members: Sequence[SharedMemberManifest],
    ) -> dict[str, object]:
        """Build and synchronously store the one pre-prompt workspace baseline."""
        _validate_preparation_id(preparation_id)
        deadline = self._new_deadline()
        self._admit_capture("initial")
        acquired = False
        uploaded = False
        try:
            self._acquire_capture_slot(deadline)
            acquired = True
            if backend.manager is not self._manager:
                raise SandboxError("initial archive uses a different retained sandbox manager")
            inspection = self._exact_inspection(
                backend.spec,
                expected_container_id=expected_container_id,
                running=True,
                deadline=deadline,
            )
            existing_payload = self._manager.read_initial_archive_marker(
                backend.spec,
                expected_container_id=inspection.container_id,
            )
            if existing_payload is not None:
                marker = SandboxInitialArchiveMarker.decode(existing_payload)
                if (
                    marker.namespace_sha256
                    != hashlib.sha256(self._namespace.encode("utf-8")).hexdigest()
                    or marker.sandbox_id != backend.spec.sandbox_id
                    or marker.generation != backend.spec.generation
                    or marker.container_id != inspection.container_id
                    or marker.preparation_id != preparation_id
                    or marker.base_sha != base_sha
                    or marker.policy != self._policy
                ):
                    raise SandboxError(
                        "existing initial archive marker does not match this preparation"
                    )
                raise SandboxError("initial sandbox archive is already prepared and must be probed")
            archive = self._build(
                backend.spec,
                inspection,
                phase="initial",
                members=members,
                base_sha=base_sha,
                deadline=deadline,
            )
            try:
                marker = SandboxInitialArchiveMarker(
                    namespace_sha256=hashlib.sha256(self._namespace.encode("utf-8")).hexdigest(),
                    sandbox_id=backend.spec.sandbox_id,
                    generation=backend.spec.generation,
                    container_id=inspection.container_id,
                    preparation_id=preparation_id,
                    base_sha=base_sha,
                    policy=self._policy,
                    bundle_size_bytes=archive.size,
                    bundle_sha256=archive.sha256,
                )
                self._manager.write_initial_archive_marker(
                    backend.spec,
                    marker.encode(),
                    expected_container_id=inspection.container_id,
                )
                self._check_deadline(deadline)
                receipt = self._upload_initial(
                    sandbox_id=backend.spec.sandbox_id,
                    generation=backend.spec.generation,
                    archive=archive,
                    deadline=deadline,
                )
                uploaded = True
                return receipt
            finally:
                archive.close()
        finally:
            if acquired:
                self._capture_slots.release()
            self._finish_capture("initial", uploaded=uploaded)

    def probe_initial(
        self,
        backend: RetainedChatBackend,
        *,
        expected_container_id: str,
        preparation_id: str,
        members: Sequence[SharedMemberManifest],
    ) -> dict[str, object]:
        """Resolve a prior prepared baseline without reading its S3 body.

        An authoritative exact-key 404 rebuilds the frozen prepared state and
        retries only if its bytes still exactly match the durable marker. Every
        identity mismatch, drift, or other HEAD failure stops the activation
        before the runner can reset the existing workspace.
        """
        _validate_preparation_id(preparation_id)
        deadline = self._new_deadline()
        self._admit_capture("initial")
        acquired = False
        outcome: bool | None = False
        try:
            self._acquire_capture_slot(deadline)
            acquired = True
            if backend.manager is not self._manager:
                raise SandboxError("initial archive probe uses a different retained manager")
            inspection = self._exact_inspection(
                backend.spec,
                expected_container_id=expected_container_id,
                running=True,
                deadline=deadline,
            )
            payload = self._manager.read_initial_archive_marker(
                backend.spec,
                expected_container_id=inspection.container_id,
            )
            self._check_deadline(deadline)
            if payload is None:
                outcome = None
                return {
                    "enabled": True,
                    "prepared": False,
                    "stored": False,
                }
            marker = SandboxInitialArchiveMarker.decode(payload)
            expected_namespace = hashlib.sha256(self._namespace.encode("utf-8")).hexdigest()
            if (
                marker.namespace_sha256 != expected_namespace
                or marker.sandbox_id != backend.spec.sandbox_id
                or marker.generation != backend.spec.generation
                or marker.container_id != inspection.container_id
                or marker.preparation_id != preparation_id
                or marker.policy != self._policy
                or marker.bundle_size_bytes > self._config.max_archive_bytes
            ):
                raise SandboxError("initial archive marker does not match this preparation")
            try:
                stored = self._uploader.initial_exists(
                    namespace=self._namespace,
                    sandbox_id=backend.spec.sandbox_id,
                    generation=backend.spec.generation,
                    size=marker.bundle_size_bytes,
                    sha256=marker.bundle_sha256,
                    deadline=deadline,
                )
                self._check_deadline(deadline)
            except Exception as exc:
                if isinstance(exc, SandboxArchiveDeadlineExceeded):
                    raise SandboxError("initial sandbox archive probe timed out") from exc
                try:
                    self._check_deadline(deadline)
                except SandboxError:
                    raise SandboxError("initial sandbox archive probe timed out") from exc
                raise SandboxError("initial sandbox archive status could not be verified") from exc
            if stored:
                outcome = None
                return {
                    "enabled": True,
                    "prepared": True,
                    "stored": True,
                    "base_sha": marker.base_sha,
                    "size_bytes": marker.bundle_size_bytes,
                    "sha256": marker.bundle_sha256,
                }
            # A timed-out conditional write can still commit after HEAD returns
            # 404. Never delete the marker and rerun a potentially non-idempotent
            # setup. Rebuild while the supervisor holds the writer freeze, then
            # retry only the exact immutable bytes named by the marker. Either
            # racing conditional writer may win; the other can reconcile its
            # 412 only when the same metadata is present.
            archive = self._build(
                backend.spec,
                inspection,
                phase="initial",
                members=members,
                base_sha=marker.base_sha,
                deadline=deadline,
            )
            try:
                if (
                    archive.size != marker.bundle_size_bytes
                    or archive.sha256 != marker.bundle_sha256
                ):
                    raise SandboxError(
                        "prepared workspace no longer matches its initial archive marker"
                    )
                receipt = self._upload_initial(
                    sandbox_id=backend.spec.sandbox_id,
                    generation=backend.spec.generation,
                    archive=archive,
                    deadline=deadline,
                )
                outcome = True
                return {
                    **receipt,
                    "prepared": True,
                    "stored": True,
                    "base_sha": marker.base_sha,
                    "size_bytes": marker.bundle_size_bytes,
                    "sha256": marker.bundle_sha256,
                }
            finally:
                archive.close()
        finally:
            if acquired:
                self._capture_slots.release()
            self._finish_capture("initial", uploaded=outcome)

    def queue_final(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> SandboxArchiveReceipt:
        """Spool and store a stopped container before its clean stop succeeds."""
        deadline = self._new_deadline()
        self._admit_capture("final")
        acquired = False
        uploaded = False
        archive: _BuiltArchive | None = None
        try:
            self._acquire_capture_slot(deadline)
            acquired = True
            exact = self._exact_inspection(
                spec,
                expected_container_id=inspection.container_id,
                running=False,
                deadline=deadline,
            )
            archive = self._build(
                spec,
                exact,
                phase="final",
                members=members,
                base_sha=None,
                deadline=deadline,
            )
            receipt = self._upload_final(
                sandbox_id=spec.sandbox_id,
                generation=spec.generation,
                archive=archive,
                deadline=deadline,
            )
            uploaded = True
            return receipt
        finally:
            if archive is not None:
                archive.close()
            if acquired:
                self._capture_slots.release()
            self._finish_capture("final", uploaded=uploaded)

    def capture_checkpoint(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: tuple[SharedMemberManifest, ...],
    ) -> SandboxCheckpointReceipt:
        """Upload a restorable checkpoint of an exact cleanly stopped container.

        It shares the final archive's capture slot, so scratch space stays
        within the same budget, and its operation deadline.
        """
        capture = self._checkpoint_capture
        if capture is None:
            raise SandboxError("sandbox checkpoints are not configured on this sandbox manager")
        deadline = self._new_deadline()
        self._admit_capture("checkpoint")
        acquired = False
        uploaded = False
        try:
            self._acquire_capture_slot(deadline)
            acquired = True
            exact = self._exact_inspection(
                spec,
                expected_container_id=inspection.container_id,
                running=False,
                deadline=deadline,
            )
            receipt = capture.capture(
                spec,
                exact,
                members,
                upload=self._uploader.upload_checkpoint,
                max_bytes=self._config.max_archive_bytes,
                deadline=deadline,
            )
            uploaded = True
            return receipt
        finally:
            if acquired:
                self._capture_slots.release()
            self._finish_capture("checkpoint", uploaded=uploaded)

    def _require_accepting(self) -> None:
        with self._lock:
            if not self._accepting or self._closed:
                raise SandboxError("sandbox archive service is not running")

    def _new_deadline(self) -> float:
        now = self._now()
        deadline = now + self._operation_timeout_s
        if not math.isfinite(deadline):
            raise SandboxError("sandbox archive operation deadline is not finite")
        return deadline

    def _now(self) -> float:
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise SandboxError("sandbox archive clock returned a non-finite timestamp")
        return float(now)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._now()
        if remaining <= 0:
            raise SandboxError("sandbox archive operation timed out")
        return remaining

    def _check_deadline(self, deadline: float) -> None:
        self._remaining(deadline)

    def _acquire_capture_slot(self, deadline: float) -> None:
        """Backpressure before Docker copy, then fence a concurrent close."""
        self._require_accepting()
        acquired = self._capture_slots.acquire(timeout=self._remaining(deadline))
        if not acquired:
            raise SandboxError("sandbox archive operation timed out waiting for capture capacity")
        try:
            self._check_deadline(deadline)
            self._require_accepting()
        except BaseException:
            self._capture_slots.release()
            raise

    def _exact_inspection(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
        running: bool,
        deadline: float,
    ) -> ChatSandboxInspection:
        self._check_deadline(deadline)
        if not expected_container_id:
            raise SandboxError("sandbox archive has no exact container identity")
        inspection = self._manager.inspect(spec.container_name)
        self._check_deadline(deadline)
        if inspection is None:
            raise SandboxError("sandbox archive container is missing")
        if inspection.container_id != expected_container_id:
            raise SandboxError("sandbox archive container identity changed")
        if inspection.running is not running:
            state = "running" if running else "stopped"
            raise SandboxError(f"sandbox archive requires the exact {state} container")
        return inspection

    def _build(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        *,
        phase: ArchivePhase,
        members: Sequence[SharedMemberManifest],
        base_sha: str | None,
        deadline: float,
    ) -> _BuiltArchive:
        self._check_deadline(deadline)
        # Membership is an admission-time fence, not durable archive identity.
        # A replacement sandbox manager can reconstruct a stopped container before the
        # control plane sends its full fork manifest, and retries must not
        # replace an otherwise identical archive with origin-only metadata.
        # The selected /agent-state payload still contains every runtime's
        # native histories; keep the outer manifest limited to immutable
        # sandbox/container facts so the same stopped bytes stay deterministic.
        _ = members
        stream = cast(
            "BinaryIO",
            tempfile.TemporaryFile(  # noqa: SIM115 - ownership passes to queue/uploader.
                prefix=f"sandbox-{phase}-archive-"
            ),
        )
        bounded = _BoundedBundleWriter(
            stream,
            self._config.max_archive_bytes,
            check_deadline=lambda: self._check_deadline(deadline),
        )
        metadata: dict[str, object] = {
            "sandbox_namespace": self._namespace,
            "sandbox": {
                "sandbox_id": spec.sandbox_id,
                "generation": spec.generation,
                "origin_thread_id": spec.thread_id,
                "runtime_id": spec.runtime_id,
                "runtime_version": spec.runtime_version,
                "sandbox_image_digest": spec.sandbox_image_digest,
            },
            "container": {
                "container_id": inspection.container_id,
                "image": inspection.image,
                "sandbox_runtime": inspection.sandbox_runtime,
            },
        }
        if base_sha is not None:
            metadata["base_sha"] = base_sha

        def fetch_archive(path: str, destination: BinaryIO, *, max_bytes: int) -> int:
            self._check_deadline(deadline)
            try:
                return self._manager.export_archive(
                    spec,
                    path,
                    destination,
                    max_bytes=max_bytes,
                    expected_container_id=inspection.container_id,
                    deadline=deadline,
                )
            finally:
                self._check_deadline(deadline)

        try:
            build_sandbox_archive_bundle(
                cast("BinaryIO", bounded),
                phase=phase,
                fetch_archive=fetch_archive,
                metadata=metadata,
                max_member_bytes=self._config.max_archive_bytes,
                max_total_bytes=self._config.max_archive_bytes,
            )
            bounded.flush()
            self._check_deadline(deadline)
            if bounded.size <= 0:
                raise SandboxError("sandbox archive bundle is empty")
            stream.seek(0)
            return _BuiltArchive(
                stream=stream,
                size=bounded.size,
                sha256=bounded.sha256,
            )
        except BaseException:
            stream.close()
            raise

    @staticmethod
    def _initial_receipt(receipt: SandboxArchiveReceipt) -> dict[str, object]:
        return {
            "enabled": True,
            "stored": True,
            "already_exists": False,
            "bucket": receipt.bucket,
            "key": receipt.key,
            "size_bytes": receipt.size,
            "sha256": receipt.sha256,
            "version_id": receipt.version_id,
        }

    def _upload_initial(
        self,
        *,
        sandbox_id: str,
        generation: int,
        archive: _BuiltArchive,
        deadline: float,
    ) -> dict[str, object]:
        """Retry one immutable bundle without ever rebuilding its bytes."""
        for attempt in range(1, self._max_upload_attempts + 1):
            try:
                self._check_deadline(deadline)
                archive.stream.seek(0)
                receipt = self._uploader.upload(
                    namespace=self._namespace,
                    sandbox_id=sandbox_id,
                    generation=generation,
                    phase="initial",
                    body=archive.stream,
                    size=archive.size,
                    sha256=archive.sha256,
                    deadline=deadline,
                )
                self._check_deadline(deadline)
            except SandboxArchiveAlreadyExists as exc:
                # A prior attempt committed these exact immutable bytes but
                # its response was lost. The uploader accepted the 412 only
                # after an exact-key metadata match.
                return {
                    "enabled": True,
                    "stored": True,
                    "already_exists": True,
                    "bucket": exc.bucket,
                    "key": exc.key,
                }
            except Exception as exc:
                if isinstance(exc, SandboxArchiveDeadlineExceeded):
                    raise SandboxError("initial sandbox archive operation timed out") from exc
                # The uploader itself owns the bounded three-attempt retry for
                # S3's conditional-write 409 race. Retrying that exhausted
                # result here would silently multiply the documented cap.
                if is_sandbox_archive_conditional_conflict(exc):
                    logger.error(
                        "initial sandbox archive conditional conflict exhausted",
                        extra={
                            "event": "sandbox_initial_archive_upload_failed",
                            "error_type": safe_archive_error_type(exc),
                        },
                    )
                    raise SandboxError("initial sandbox archive could not be stored") from exc
                try:
                    self._check_deadline(deadline)
                except SandboxError:
                    raise SandboxError("initial sandbox archive operation timed out") from exc
                if attempt == self._max_upload_attempts:
                    logger.error(
                        "initial sandbox archive upload failed",
                        extra={
                            "event": "sandbox_initial_archive_upload_failed",
                            "attempts": attempt,
                            "error_type": safe_archive_error_type(exc),
                        },
                    )
                    raise SandboxError("initial sandbox archive could not be stored") from exc
                retry_delay = self._retry_delay_s * attempt
                remaining = self._remaining(deadline)
                if retry_delay >= remaining:
                    raise SandboxError("initial sandbox archive operation timed out") from exc
                self._sleep(retry_delay)
                try:
                    self._check_deadline(deadline)
                except SandboxError:
                    raise SandboxError("initial sandbox archive operation timed out") from exc
            else:
                return self._initial_receipt(receipt)
        raise AssertionError("unreachable initial sandbox archive retry state")

    def _upload_final(
        self,
        *,
        sandbox_id: str,
        generation: int,
        archive: _BuiltArchive,
        deadline: float,
    ) -> SandboxArchiveReceipt:
        for attempt in range(1, self._max_upload_attempts + 1):
            try:
                self._check_deadline(deadline)
                archive.stream.seek(0)
                receipt = self._uploader.upload(
                    namespace=self._namespace,
                    sandbox_id=sandbox_id,
                    generation=generation,
                    phase="final",
                    body=archive.stream,
                    size=archive.size,
                    sha256=archive.sha256,
                    deadline=deadline,
                )
                self._check_deadline(deadline)
            except Exception as exc:
                if isinstance(exc, SandboxArchiveDeadlineExceeded):
                    raise SandboxError("final sandbox archive operation timed out") from exc
                try:
                    self._check_deadline(deadline)
                except SandboxError:
                    raise SandboxError("final sandbox archive operation timed out") from exc
                if attempt == self._max_upload_attempts:
                    logger.error(
                        "sandbox final archive upload failed",
                        extra={
                            "event": "sandbox_final_archive_upload_failed",
                            "attempts": attempt,
                            "error_type": safe_archive_error_type(exc),
                        },
                    )
                    raise SandboxError("final sandbox archive could not be stored") from exc
                retry_delay = self._retry_delay_s * attempt
                remaining = self._remaining(deadline)
                if retry_delay >= remaining:
                    raise SandboxError("final sandbox archive operation timed out") from exc
                self._sleep(retry_delay)
                try:
                    self._check_deadline(deadline)
                except SandboxError:
                    raise SandboxError("final sandbox archive operation timed out") from exc
            else:
                logger.info(
                    "sandbox final archive uploaded",
                    extra={
                        "event": "sandbox_final_archive_uploaded",
                        "archive_sha256": receipt.sha256,
                        "archive_size_bytes": receipt.size,
                    },
                )
                return receipt
        raise AssertionError("unreachable final sandbox archive retry state")


def _validate_preparation_id(value: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 256:
        raise ValueError("initial archive preparation id is invalid")


__all__ = [
    "DEFAULT_ARCHIVE_OPERATION_TIMEOUT_S",
    "SandboxArchiveService",
    "SandboxInitialArchiveMarker",
]
