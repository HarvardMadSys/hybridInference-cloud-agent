"""Revision and lifecycle tests for the sandbox manager's live archive controller."""

from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from cloud_agent_host import sandbox_archive_runtime as archive_runtime
from cloud_agent_host.chat_sandbox import FINAL_ARCHIVE_MARKER_MAX_BYTES
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveReceipt
from cloud_agent_host.sandbox_archives import SandboxInitialArchiveMarker
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

if TYPE_CHECKING:
    from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveS3Config

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_KMS_KEY = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc"
_KMS_KEY_2 = "arn:aws:kms:us-east-1:123456789012:key/abcdefab-1234-1234-1234-123456789abc"
_UNSAFE_CLOSE_ERROR = "https://user:secret@example.com/archive?token=leaked"


def _policy(
    revision: int,
    *,
    enabled: bool = True,
    region: str = _REGION,
    kms_key: str = _KMS_KEY,
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    return SandboxArchivePolicy(
        enabled=enabled,
        region=region if enabled else "",
        kms_key_arn=kms_key if enabled else "",
        expected_bucket_owner=_ACCOUNT if enabled else "",
        max_archive_bytes=max_archive_bytes,
        revision=revision,
    ).as_dict()


class _Service:
    def __init__(self, config: SandboxArchiveS3Config) -> None:
        self.config = config
        self.started = False
        self.closed = False
        self.close_calls = 0
        self.probe_entered = threading.Event()
        self.probe_release = threading.Event()
        self.block_probe = False
        self.fail_final = False
        self.fail_close = False
        self.final_calls = 0
        self.initial_probe_calls = 0
        self.initial_probe_preparation_ids: list[str] = []
        self.initial_capture_calls = 0

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError(_UNSAFE_CLOSE_ERROR)

    def status(self) -> dict[str, object]:
        return {"enabled": True, "running": self.started, "pending": 0}

    def probe_initial(self, _backend, **_kwargs: Any) -> dict[str, object]:
        self.initial_probe_calls += 1
        self.initial_probe_preparation_ids.append(_kwargs["preparation_id"])
        self.probe_entered.set()
        if self.block_probe:
            assert self.probe_release.wait(timeout=5)
        return {"enabled": True, "prepared": True, "stored": True}

    def capture_initial(self, _backend, **_kwargs: Any) -> dict[str, object]:
        self.initial_capture_calls += 1
        return {"enabled": True, "stored": True}

    def queue_final(self, spec, _inspection, _members) -> SandboxArchiveReceipt:
        self.final_calls += 1
        if self.fail_final:
            raise RuntimeError("upload failed")
        return SandboxArchiveReceipt(
            phase="final",
            bucket="freeinference",
            key=(
                f"cloud-agent/sandbox/v1/{hashlib.sha256(b'pool-east').hexdigest()}/"
                f"{hashlib.sha256(spec.sandbox_id.encode()).hexdigest()}/"
                f"g{spec.generation}/final.tar.gz"
            ),
            size=123,
            sha256="a" * 64,
            etag="etag",
            version_id="version-1",
            s3_checksum_sha256="checksum",
            multipart=False,
        )


class _MarkerManager:
    def __init__(self) -> None:
        self.marker: bytes | None = None
        self.initial_marker: bytes | None = None
        self.writes: list[bytes] = []
        self.clears = 0
        self.lose_uploaded_marker_response_once = False

    def write_final_archive_marker(self, _spec, payload: bytes, **_kwargs: Any) -> None:
        self.marker = payload
        self.writes.append(payload)

    def write_stopped_final_archive_marker(self, _spec, payload: bytes, **_kwargs: Any) -> None:
        self.marker = payload
        self.writes.append(payload)
        if self.lose_uploaded_marker_response_once and b'"state":"uploaded"' in payload:
            self.lose_uploaded_marker_response_once = False
            raise RuntimeError("marker response lost")

    def read_final_archive_marker(self, _spec, **_kwargs: Any) -> bytes:
        if self.marker is None:
            raise SandboxError("final archive marker is missing")
        return self.marker

    def read_running_final_archive_marker(self, _spec, **_kwargs: Any) -> bytes | None:
        return self.marker

    def read_stopped_final_archive_marker_optional(self, _spec, **_kwargs: Any) -> bytes | None:
        return self.marker

    def clear_final_archive_marker(self, _spec, **_kwargs: Any) -> None:
        self.marker = None
        self.clears += 1

    def read_initial_archive_marker(self, _spec, **_kwargs: Any) -> bytes | None:
        return self.initial_marker


def _controller(
    manager: _MarkerManager | None = None,
) -> tuple[SandboxArchiveController, list[_Service]]:
    services: list[_Service] = []

    def factory(config: SandboxArchiveS3Config) -> _Service:
        service = _Service(config)
        services.append(service)
        return service

    return (
        SandboxArchiveController(
            manager or _MarkerManager(),  # type: ignore[arg-type]
            namespace="pool-east",
            service_factory=factory,
        ),
        services,
    )


def _initial_backend() -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(sandbox_id="asbx", generation=1),
    )


def _initial_marker(policy: dict[str, Any], *, preparation_id: str = "ajob") -> bytes:
    return SandboxInitialArchiveMarker(
        namespace_sha256=hashlib.sha256(b"pool-east").hexdigest(),
        sandbox_id="asbx",
        generation=1,
        container_id="container",
        preparation_id=preparation_id,
        base_sha="b" * 40,
        policy=SandboxArchivePolicy.from_mapping(policy),
        bundle_size_bytes=123,
        bundle_sha256="a" * 64,
    ).encode()


def test_maximum_valid_uploaded_final_marker_fits_durable_bound() -> None:
    region = "r" * 128
    kms_prefix = f"arn:aws:kms:{region}:{_ACCOUNT}:key/"
    policy = SandboxArchivePolicy(
        enabled=True,
        region=region,
        kms_key_arn=kms_prefix + "k" * (2048 - len(kms_prefix)),
        expected_bucket_owner=_ACCOUNT,
        revision=(1 << 63) - 1,
    )
    spec = SimpleNamespace(sandbox_id="s" * 256, generation=(1 << 63) - 1)
    inspection = SimpleNamespace(container_id="c" * 256)
    pending = archive_runtime._FinalArchiveMarker.create(
        namespace="n" * 256,
        spec=spec,
        inspection=inspection,
        policy=policy,
    )
    key = (
        f"cloud-agent/sandbox/v1/{hashlib.sha256(('n' * 256).encode()).hexdigest()}/"
        f"{hashlib.sha256(spec.sandbox_id.encode()).hexdigest()}/"
        f"g{spec.generation}/final.tar.gz"
    )
    uploaded = pending.with_receipt(
        SandboxArchiveReceipt(
            phase="final",
            bucket="freeinference",
            key=key,
            size=policy.max_archive_bytes,
            sha256="a" * 64,
            etag="etag",
            version_id="v" * 1024,
            s3_checksum_sha256="checksum",
            multipart=True,
        )
    )

    payload = uploaded.encode()

    assert 4096 < len(payload) <= FINAL_ARCHIVE_MARKER_MAX_BYTES
    assert archive_runtime._FinalArchiveMarker.decode(payload) == uploaded


def test_unknown_policy_is_distinct_from_explicit_disabled_policy() -> None:
    controller, services = _controller()
    controller.start()

    assert controller.status() == {
        "configured": False,
        "enabled": False,
        "running": False,
        "applied_revision": None,
    }
    with pytest.raises(SandboxError, match="has not been delivered"):
        controller.queue_final(
            SimpleNamespace(sandbox_id="asbx", generation=1),
            SimpleNamespace(container_id="container"),
            (),
        )

    answer = controller.apply(_policy(4, enabled=False))

    assert answer["configured"] is True
    assert answer["enabled"] is False
    assert answer["running"] is False
    assert answer["applied_revision"] == 4
    assert services == []
    spec = SimpleNamespace(sandbox_id="asbx", generation=1)
    inspection = SimpleNamespace(container_id="container")
    controller.prepare_final(spec, inspection)
    controller.queue_final(
        spec,
        inspection,
        (),
    )
    controller.close()


def test_revisions_are_monotonic_and_equal_revision_is_idempotent_only() -> None:
    controller, services = _controller()
    controller.start()

    assert controller.apply(_policy(7))["applied_revision"] == 7
    assert services[0].started is True
    assert controller.apply(_policy(7))["applied_revision"] == 7
    assert len(services) == 1, "an idempotent heartbeat does not allocate another service"
    with pytest.raises(ValueError, match="without a new revision"):
        controller.apply(_policy(7, enabled=False))
    with pytest.raises(ValueError, match="stale"):
        controller.apply(_policy(6))
    assert controller.applied_revision == 7
    controller.close()


def test_in_flight_call_keeps_its_service_until_the_revision_swaps() -> None:
    controller, services = _controller()
    controller.start()
    controller.apply(_policy(1))
    first = services[0]
    first.block_probe = True
    result: list[dict[str, object]] = []
    call = threading.Thread(
        target=lambda: result.append(
            controller.probe_initial(
                _initial_backend(),
                expected_container_id="container",
                preparation_id="ajob",
                members=(),
            )
        )
    )
    call.start()
    assert first.probe_entered.wait(timeout=5)

    controller.apply(_policy(2))

    assert first.closed is False
    assert services[1].started is True
    first.probe_release.set()
    call.join(timeout=5)
    assert not call.is_alive()
    assert result == [{"enabled": True, "prepared": True, "stored": True}]
    assert first.closed is True
    assert first.close_calls == 1
    controller.close()


def test_retired_client_close_failure_cannot_rollback_policy_application(
    caplog: pytest.LogCaptureFixture,
) -> None:
    controller, services = _controller()
    controller.start()
    controller.apply(_policy(1))
    services[0].fail_close = True

    status = controller.apply(_policy(2))

    assert status["applied_revision"] == 2
    assert services[0].close_calls == 1
    assert services[1].started is True
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "sandbox_archive_service_close_failed"
    )
    assert record.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert record.exc_info is None
    assert _UNSAFE_CLOSE_ERROR not in caplog.text
    controller.close()


def test_default_factory_shares_one_capture_gate_across_policy_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gates: list[threading.BoundedSemaphore] = []
    services: list[_Service] = []

    class Uploader:
        def __init__(self, config) -> None:
            self.config = config

    class Service(_Service):
        def __init__(self, _manager, *, config, capture_slots, **_kwargs: Any) -> None:
            super().__init__(config)
            gates.append(capture_slots)
            services.append(self)

    monkeypatch.setattr(archive_runtime, "SandboxArchiveS3Uploader", Uploader)
    monkeypatch.setattr(archive_runtime, "SandboxArchiveService", Service)
    controller = SandboxArchiveController(  # type: ignore[arg-type]
        _MarkerManager(),
        namespace="pool-east",
    )
    controller.start()

    controller.apply(_policy(1))
    controller.apply(_policy(2))

    assert len(gates) == 2
    assert gates[0] is gates[1]
    assert gates[0].acquire(blocking=False)
    assert not gates[1].acquire(blocking=False)
    gates[0].release()
    assert services[0].close_calls == 1
    controller.close()
    assert services[1].close_calls == 1


def test_failed_final_retry_stays_pinned_to_the_original_revision() -> None:
    controller, services = _controller()
    controller.start()
    controller.apply(_policy(1))
    first = services[0]
    first.fail_final = True
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container")
    controller.prepare_final(spec, inspection)

    with pytest.raises(RuntimeError, match="upload failed"):
        controller.queue_final(spec, inspection, ())
    controller.apply(_policy(2))

    assert first.closed is False
    first.fail_final = False
    controller.queue_final(spec, inspection, ())
    assert first.final_calls == 2
    assert services[1].final_calls == 0
    assert first.closed is True
    controller.close()


@pytest.mark.parametrize(
    "next_policy",
    [
        pytest.param(_policy(2, enabled=False), id="disabled"),
        pytest.param(_policy(2, kms_key=_KMS_KEY_2), id="kms-changed"),
    ],
)
def test_restart_recovers_failed_final_with_original_policy(next_policy) -> None:
    manager = _MarkerManager()
    first_controller, first_services = _controller(manager)
    first_controller.start()
    first_controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container")
    first_controller.prepare_final(spec, inspection)
    first_services[0].fail_final = True
    with pytest.raises(RuntimeError, match="upload failed"):
        first_controller.queue_final(spec, inspection, ())
    first_controller.close()

    replacement, replacement_services = _controller(manager)
    replacement.start()
    replacement.apply(next_policy)
    replacement.queue_final(spec, inspection, ())

    recovered = next(service for service in replacement_services if service.final_calls)
    assert recovered.config.kms_key_arn == _KMS_KEY
    assert recovered.final_calls == 1
    assert b'"state":"uploaded"' in manager.marker
    replacement.close()


@pytest.mark.parametrize(
    "next_policy",
    [
        pytest.param(_policy(2, enabled=False), id="disabled"),
        pytest.param(_policy(2, kms_key=_KMS_KEY_2), id="kms-changed"),
    ],
)
def test_replacement_preserves_running_orphan_pending_policy(next_policy) -> None:
    manager = _MarkerManager()
    first, first_services = _controller(manager)
    first.start()
    first.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    running = SimpleNamespace(container_id="container")
    first.prepare_final(spec, running)
    persisted = manager.marker
    assert persisted is not None
    first.close()

    replacement, replacement_services = _controller(manager)
    replacement.start()
    replacement.apply(next_policy)

    # Reconcile sees the still-running orphan and prepares its stop again.
    # The fsynced rev-1 marker, not the newly delivered policy, is authoritative.
    replacement.prepare_final(spec, running)
    assert manager.marker == persisted
    replacement.queue_final(spec, running, ())

    recovered = next(service for service in replacement_services if service.final_calls)
    assert recovered.config.revision == 1
    assert recovered.config.kms_key_arn == _KMS_KEY
    assert first_services[0].final_calls == 0
    replacement.close()


def test_restart_after_uploaded_receipt_does_not_create_a_duplicate_version() -> None:
    manager = _MarkerManager()
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container")
    controller.prepare_final(spec, inspection)
    controller.queue_final(spec, inspection, ())
    assert services[0].final_calls == 1
    uploaded = manager.marker
    assert uploaded is not None
    controller.close()

    replacement, replacement_services = _controller(manager)
    replacement.start()
    replacement.apply(_policy(2))
    replacement.queue_final(spec, inspection, ())

    assert all(service.final_calls == 0 for service in replacement_services)
    assert manager.marker == uploaded
    replacement.close()


@pytest.mark.parametrize(
    "current_policy",
    [
        pytest.param(_policy(2, enabled=False), id="disabled"),
        pytest.param(_policy(2, kms_key=_KMS_KEY_2), id="kms-changed"),
    ],
)
def test_prepare_start_replaces_uploaded_history_with_current_policy_fence(
    current_policy,
) -> None:
    manager = _MarkerManager()
    first, first_services = _controller(manager)
    first.start()
    first.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    stopped = SimpleNamespace(container_id="container")
    first.prepare_final(spec, stopped)
    first.queue_final(spec, stopped, ())
    assert first_services[0].final_calls == 1
    assert manager.marker is not None and b'"state":"uploaded"' in manager.marker
    first.close()

    replacement, replacement_services = _controller(manager)
    replacement.start()
    replacement.apply(current_policy)

    replacement.prepare_start(spec, stopped)

    assert manager.marker is not None
    marker = archive_runtime._FinalArchiveMarker.decode(manager.marker)
    assert marker.state == "pending"
    assert marker.policy == SandboxArchivePolicy.from_mapping(current_policy)

    replacement.apply(_policy(3, enabled=False) if marker.policy.enabled else _policy(3))
    if marker.policy.enabled:
        pinned = next(service for service in replacement_services if service.config.revision == 2)
        assert pinned.closed is False
        replacement.queue_final(spec, stopped, ())
        assert pinned.final_calls == 1
        assert pinned.closed is True
    else:
        replacement.queue_final(spec, stopped, ())
        assert all(service.final_calls == 0 for service in replacement_services)
    replacement.close()


def test_safe_start_clears_a_stale_uploaded_marker_and_releases_its_pin() -> None:
    manager = _MarkerManager()
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    stopped = SimpleNamespace(container_id="container")
    controller.prepare_final(spec, stopped)
    controller.queue_final(spec, stopped, ())
    assert manager.marker is not None
    assert b'"state":"uploaded"' in manager.marker
    controller.prepare_start(spec, stopped)
    controller.complete_start(spec, stopped)

    assert manager.marker is None
    assert manager.clears == 1
    controller.apply(_policy(2))
    assert services[0].closed is True
    with pytest.raises(SandboxError, match="marker is missing"):
        controller.queue_final(spec, stopped, ())
    controller.close()


def test_native_flush_skip_finishes_without_upload_and_the_next_stop_archives() -> None:
    manager = _MarkerManager()
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container", running=True)

    controller.invalidate_final(spec, inspection)

    assert manager.marker is not None
    assert b'"state":"skipped"' in manager.marker
    # A running prepare cannot overwrite the skip, and its stop never uploads.
    controller.prepare_final(spec, inspection)
    assert b'"state":"skipped"' in manager.marker
    controller.queue_final(spec, inspection, ())
    assert services[0].final_calls == 0
    # Starting again replaces the completed skip with a fresh fence, so the
    # next clean stop of the continued sandbox archives as usual.
    controller.prepare_start(spec, inspection)
    assert b'"state":"pending"' in manager.marker
    controller.complete_start(spec, inspection)
    controller.prepare_final(spec, inspection)
    controller.queue_final(spec, inspection, ())
    assert services[0].final_calls == 1
    assert b'"state":"uploaded"' in manager.marker
    controller.close()


def test_legacy_blocked_marker_is_read_as_a_skip() -> None:
    manager = _MarkerManager()
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container", running=False)
    controller.invalidate_final(spec, inspection)
    assert manager.marker is not None
    # An older broker wrote the same record with state "blocked".
    manager.marker = manager.marker.replace(b'"state":"skipped"', b'"state":"blocked"')

    controller.queue_final(spec, inspection, ())
    assert services[0].final_calls == 0
    controller.prepare_start(spec, inspection)
    assert b'"state":"pending"' in manager.marker
    controller.close()


@pytest.mark.parametrize("running", [True, False])
def test_markerless_lifecycle_under_enabled_policy_is_skipped_not_quarantined(
    running: bool,
) -> None:
    manager = _MarkerManager()
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container", running=running)

    assert controller.prepare_markerless_final(spec, inspection) is True

    assert manager.marker is not None and b'"state":"skipped"' in manager.marker
    controller.queue_final(spec, inspection, ())
    assert services[0].final_calls == 0
    controller.close()


def test_markerless_lifecycle_stays_fenced_until_a_policy_arrives() -> None:
    manager = _MarkerManager()
    controller, _services = _controller(manager)
    controller.start()

    assert (
        controller.prepare_markerless_final(
            SimpleNamespace(sandbox_id="asbx", generation=3),
            SimpleNamespace(container_id="container", running=True),
        )
        is False
    )
    assert manager.marker is None
    controller.close()


def test_native_flush_skip_uses_the_stopped_marker_writer_after_exact_stop() -> None:
    class MarkerManager(_MarkerManager):
        def __init__(self) -> None:
            super().__init__()
            self.running_writes = 0
            self.stopped_writes = 0

        def write_final_archive_marker(self, *args, **kwargs) -> None:
            self.running_writes += 1
            super().write_final_archive_marker(*args, **kwargs)

        def write_stopped_final_archive_marker(self, *args, **kwargs) -> None:
            self.stopped_writes += 1
            super().write_stopped_final_archive_marker(*args, **kwargs)

    manager = MarkerManager()
    controller, _services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))

    controller.invalidate_final(
        SimpleNamespace(sandbox_id="asbx", generation=3),
        SimpleNamespace(container_id="container", running=False),
    )

    assert manager.running_writes == 0
    assert manager.stopped_writes == 1
    assert manager.marker is not None and b'"state":"skipped"' in manager.marker
    controller.close()


def test_final_marker_response_loss_reuses_the_known_receipt_without_reupload() -> None:
    manager = _MarkerManager()
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container")
    controller.prepare_final(spec, inspection)
    manager.lose_uploaded_marker_response_once = True

    with pytest.raises(RuntimeError, match="marker response lost"):
        controller.queue_final(spec, inspection, ())
    assert services[0].final_calls == 1

    controller.queue_final(spec, inspection, ())

    assert services[0].final_calls == 1
    assert manager.marker is not None
    assert b'"state":"uploaded"' in manager.marker
    controller.close()


@pytest.mark.parametrize(
    "current_policy",
    [
        pytest.param(_policy(2, enabled=False), id="disabled"),
        pytest.param(
            _policy(2, kms_key=_KMS_KEY_2, max_archive_bytes=1024 * 1024 * 1024),
            id="kms-and-size-rotated",
        ),
    ],
)
def test_initial_recovery_uses_marker_policy_before_current_policy(current_policy) -> None:
    manager = _MarkerManager()
    manager.initial_marker = _initial_marker(_policy(1))
    controller, services = _controller(manager)
    controller.start()
    controller.apply(current_policy)

    status = controller.probe_initial(
        _initial_backend(),
        expected_container_id="container",
        preparation_id="ajob",
        members=(),
    )

    recovered = next(service for service in services if service.initial_probe_calls)
    assert status["stored"] is True
    assert recovered.config.revision == 1
    assert recovered.config.kms_key_arn == _KMS_KEY
    assert recovered.config.max_archive_bytes == 2 * 1024 * 1024 * 1024
    controller.close()


def test_new_first_turn_recovers_the_abandoned_jobs_prepared_marker() -> None:
    manager = _MarkerManager()
    manager.initial_marker = _initial_marker(_policy(1), preparation_id="ajob_abandoned")
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(2))

    status = controller.probe_initial(
        _initial_backend(),
        expected_container_id="container",
        preparation_id="ajob_replacement",
        members=(),
    )

    recovered = next(service for service in services if service.initial_probe_calls)
    assert status["stored"] is True
    assert recovered.initial_probe_preparation_ids == ["ajob_abandoned"]
    controller.close()


def test_capture_with_a_prepared_initial_marker_reconciles_instead_of_replacing_it() -> None:
    manager = _MarkerManager()
    manager.initial_marker = _initial_marker(_policy(1))
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(2, kms_key=_KMS_KEY_2))

    status = controller.capture_initial(
        _initial_backend(),
        expected_container_id="container",
        preparation_id="ajob",
        base_sha="b" * 40,
        members=(),
    )

    recovered = next(service for service in services if service.initial_probe_calls)
    assert status["stored"] is True
    assert recovered.initial_probe_calls == 1
    assert all(service.initial_capture_calls == 0 for service in services)
    assert manager.initial_marker == _initial_marker(_policy(1))
    controller.close()


@pytest.mark.parametrize("kind", ["legacy-v1", "credential-bearing", "identity-mismatch"])
def test_untrusted_initial_marker_fails_closed_before_current_policy(kind: str) -> None:
    manager = _MarkerManager()
    marker = json.loads(_initial_marker(_policy(1)))
    if kind == "legacy-v1":
        marker["format_version"] = 1
        marker.pop("policy")
    elif kind == "credential-bearing":
        marker["policy"]["aws_access_key_id"] = "forbidden"
    else:
        marker["container_id"] = "other-container"
    manager.initial_marker = (
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    controller, services = _controller(manager)
    controller.start()
    controller.apply(_policy(2, enabled=False))

    with pytest.raises(SandboxError):
        controller.probe_initial(
            _initial_backend(),
            expected_container_id="container",
            preparation_id="ajob",
            members=(),
        )

    assert all(service.initial_probe_calls == 0 for service in services)
    controller.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_key", "cloud-agent/sandbox/not-the-final-key"),
        ("archive_size_bytes", 3 * 1024 * 1024 * 1024),
        ("version_id", "null"),
    ],
)
def test_untrusted_uploaded_final_receipt_cannot_authorize_release(
    field: str, value: object
) -> None:
    manager = _MarkerManager()
    controller, _services = _controller(manager)
    controller.start()
    controller.apply(_policy(1))
    spec = SimpleNamespace(sandbox_id="asbx", generation=3)
    inspection = SimpleNamespace(container_id="container")
    controller.prepare_final(spec, inspection)
    controller.queue_final(spec, inspection, ())
    assert manager.marker is not None
    marker = json.loads(manager.marker)
    marker["receipt"][field] = value
    manager.marker = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    controller.close()

    replacement, replacement_services = _controller(manager)
    replacement.start()
    replacement.apply(_policy(2))
    with pytest.raises(SandboxError, match="invalid values"):
        replacement.queue_final(spec, inspection, ())
    assert all(service.final_calls == 0 for service in replacement_services)
    replacement.close()
