"""Focused lifecycle tests for retained-sandbox archive orchestration."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, BinaryIO, cast

import pytest

from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    RetainedChatBackend,
)
from cloud_agent_host.sandbox_archive_s3 import (
    ArchivePhase,
    SandboxArchiveAlreadyExists,
    SandboxArchiveReceipt,
    SandboxArchiveS3Config,
    SandboxArchiveS3Uploader,
)
from cloud_agent_host.sandbox_archives import SandboxArchiveService
from cloud_agent_host.shared_session_supervisor import SharedMemberManifest
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Callable

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_KMS_KEY = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc"
_NAMESPACE = "host-pool-east"
_CONTAINER_ID = "container-exact-123"
_BASE_SHA = "b" * 40
_PREPARATION_ID = "ajob_initial"


def _config() -> SandboxArchiveS3Config:
    return SandboxArchiveS3Config(
        region=_REGION,
        kms_key_arn=_KMS_KEY,
        expected_bucket_owner=_ACCOUNT,
    )


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="thread-origin",
        sandbox_id="sandbox-123",
        generation=7,
        runtime_id="codex",
        runtime_version="1.2.3",
        sandbox_image_digest="sha256:image",
    )


def _inspection(spec: ChatSandboxSpec, *, running: bool) -> ChatSandboxInspection:
    return ChatSandboxInspection(
        container_id=_CONTAINER_ID,
        name=spec.container_name,
        status=ChatSandboxStatus.RUNNING if running else ChatSandboxStatus.STOPPED,
        running=running,
        image="sha256:image",
        labels=spec.docker_labels(),
        mounts=(),
        networks=("sandbox-network",),
        sandbox_runtime="io.containerd.kata.v2",
    )


def _members() -> tuple[SharedMemberManifest, ...]:
    # Deliberately unsorted: the service should make manifest output stable.
    return (
        SharedMemberManifest(
            thread_id="thread-z",
            native_session_id="native-z",
            context_status="live",
        ),
        SharedMemberManifest(thread_id="thread-a"),
    )


class _Manager:
    def __init__(self, spec: ChatSandboxSpec, *, running: bool) -> None:
        self.backend = SimpleNamespace(image="sha256:image", runtime="runc")
        self.current = _inspection(spec, running=running)
        self.payloads: dict[str, bytes] = {
            "/workspace": b"workspace docker archive",
            "/agent-state": b"native agent history",
            "/home/agent/.freeinference-platform": b"platform state",
        }
        self.export_calls: list[dict[str, object]] = []
        self.marker: bytes | None = None
        self.marker_writes: list[tuple[bytes, str]] = []

    def inspect(self, container_name: str) -> ChatSandboxInspection | None:
        if container_name != self.current.name:
            return None
        return self.current

    def export_archive(
        self,
        spec: ChatSandboxSpec,
        path: str,
        destination: BinaryIO,
        *,
        max_bytes: int,
        expected_container_id: str | None = None,
        deadline: float | None = None,
    ) -> int:
        self.export_calls.append(
            {
                "spec": spec,
                "path": path,
                "max_bytes": max_bytes,
                "expected_container_id": expected_container_id,
                "deadline": deadline,
            }
        )
        if path not in self.payloads:
            raise FileNotFoundError(path)
        payload = self.payloads[path]
        written = destination.write(payload)
        assert written == len(payload)
        return written

    def read_initial_archive_marker(
        self,
        _spec: ChatSandboxSpec,
        *,
        expected_container_id: str,
    ) -> bytes | None:
        assert expected_container_id == self.current.container_id
        return self.marker

    def write_initial_archive_marker(
        self,
        _spec: ChatSandboxSpec,
        payload: bytes,
        *,
        expected_container_id: str,
    ) -> None:
        assert expected_container_id == self.current.container_id
        self.marker_writes.append((payload, expected_container_id))
        if self.marker is not None and self.marker != payload:
            raise SandboxError("different marker")
        self.marker = payload


class _Uploader:
    def __init__(
        self,
        config: SandboxArchiveS3Config,
        *,
        outcomes: list[BaseException | None] | None = None,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
        exists_outcomes: list[BaseException | bool] | None = None,
    ) -> None:
        self.config = config
        self.outcomes = list(outcomes or [])
        self.entered = entered
        self.release = release
        self.calls: list[dict[str, Any]] = []
        self.bodies: list[BinaryIO] = []
        self.exists_outcomes = list(exists_outcomes or [])
        self.exists_calls: list[dict[str, Any]] = []
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def upload(
        self,
        *,
        namespace: str,
        sandbox_id: str,
        generation: int,
        phase: str,
        body: BinaryIO,
        size: int,
        sha256: str | None = None,
        deadline: float | None = None,
    ) -> SandboxArchiveReceipt:
        position = body.tell()
        payload = body.read(size)
        self.calls.append(
            {
                "namespace": namespace,
                "sandbox_id": sandbox_id,
                "generation": generation,
                "phase": phase,
                "size": size,
                "sha256": sha256,
                "position": position,
                "deadline": deadline,
                "payload": payload,
                "thread": threading.current_thread().name,
            }
        )
        self.bodies.append(body)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=10)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        digest = hashlib.sha256(payload).hexdigest()
        return SandboxArchiveReceipt(
            phase=cast("ArchivePhase", phase),
            bucket=self.config.bucket,
            key=f"{self.config.prefix}/v1/opaque/{phase}.tar.gz",
            size=size,
            sha256=digest,
            etag="etag",
            version_id="version-1",
            s3_checksum_sha256="checksum",
            multipart=False,
        )

    def upload_checkpoint(self, **kwargs: Any) -> SandboxArchiveReceipt:
        raise AssertionError("checkpoint capture is faked in these tests")

    def initial_exists(self, **kwargs: Any) -> bool:
        self.exists_calls.append(kwargs)
        outcome = self.exists_outcomes.pop(0) if self.exists_outcomes else False
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _no_sleep(_delay: float) -> None:
    return


def _zero_clock() -> float:
    return 0.0


def _service(
    manager: _Manager,
    uploader: _Uploader,
    *,
    capture_capacity: int = 1,
    spool_budget_bytes: int = 8 * 1024 * 1024 * 1024,
    max_upload_attempts: int = 3,
    retry_delay_s: float = 0,
    sleep: Callable[[float], None] = _no_sleep,
    operation_timeout_s: float = 12 * 60,
    clock: Callable[[], float] = _zero_clock,
) -> SandboxArchiveService:
    return SandboxArchiveService(
        cast("ChatSandboxManager", manager),
        namespace=_NAMESPACE,
        uploader=cast("SandboxArchiveS3Uploader", uploader),
        config=uploader.config,
        capture_capacity=capture_capacity,
        spool_budget_bytes=spool_budget_bytes,
        max_upload_attempts=max_upload_attempts,
        retry_delay_s=retry_delay_s,
        sleep=sleep,
        operation_timeout_s=operation_timeout_s,
        clock=clock,
    )


def _read_bundle(payload: bytes) -> tuple[list[str], dict[str, Any], dict[str, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = archive.getnames()
        contents: dict[str, bytes] = {}
        for member in archive.getmembers():
            stream = archive.extractfile(member)
            assert stream is not None
            contents[member.name] = stream.read()
    return names, json.loads(contents["manifest.json"]), contents


def test_initial_capture_is_synchronous_workspace_only_and_returns_safe_receipt() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    uploader = _Uploader(_config())
    service = _service(manager, uploader)
    service.start()
    try:
        receipt = service.capture_initial(
            RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
            expected_container_id=_CONTAINER_ID,
            preparation_id=_PREPARATION_ID,
            base_sha=_BASE_SHA,
            members=_members(),
        )
    finally:
        service.close()

    assert [call["path"] for call in manager.export_calls] == ["/workspace"]
    assert all(call["expected_container_id"] == _CONTAINER_ID for call in manager.export_calls)
    assert [call["deadline"] for call in manager.export_calls] == [12 * 60.0]
    assert len(uploader.calls) == 1
    upload = uploader.calls[0]
    assert {
        key: upload[key] for key in ("namespace", "sandbox_id", "generation", "phase", "position")
    } == {
        "namespace": _NAMESPACE,
        "sandbox_id": spec.sandbox_id,
        "generation": spec.generation,
        "phase": "initial",
        "position": 0,
    }
    assert upload["thread"] == threading.current_thread().name
    assert upload["deadline"] == 12 * 60.0
    assert upload["sha256"] == hashlib.sha256(upload["payload"]).hexdigest()

    names, manifest, contents = _read_bundle(upload["payload"])
    assert names == ["manifest.json", "paths/workspace.tar"]
    assert contents["paths/workspace.tar"] == manager.payloads["/workspace"]
    assert manifest["phase"] == "initial"
    assert manifest["metadata"]["base_sha"] == _BASE_SHA
    assert "members" not in manifest["metadata"]
    assert manager.marker is not None
    marker = json.loads(manager.marker)
    assert marker == {
        "base_sha": _BASE_SHA,
        "bundle_sha256": upload["sha256"],
        "bundle_size_bytes": upload["size"],
        "container_id": _CONTAINER_ID,
        "format_version": 2,
        "generation": spec.generation,
        "namespace_sha256": hashlib.sha256(_NAMESPACE.encode()).hexdigest(),
        "policy": uploader.config.as_policy().as_dict(),
        "preparation_id": _PREPARATION_ID,
        "sandbox_id": spec.sandbox_id,
    }
    assert manager.marker == (
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")

    assert receipt == {
        "enabled": True,
        "stored": True,
        "already_exists": False,
        "bucket": "freeinference",
        "key": "cloud-agent/sandbox/v1/opaque/initial.tar.gz",
        "size_bytes": upload["size"],
        "sha256": hashlib.sha256(upload["payload"]).hexdigest(),
        "version_id": "version-1",
    }
    assert service.status()["phases"] == {
        "initial": {"pending": 0, "uploaded": 1, "failed": 0},
        "final": {"pending": 0, "uploaded": 0, "failed": 0},
    }
    assert uploader.bodies[0].closed


def test_initial_already_exists_is_a_successful_idempotent_result() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    exists = SandboxArchiveAlreadyExists(
        bucket="freeinference",
        key="cloud-agent/sandbox/v1/opaque/initial.tar.gz",
    )
    uploader = _Uploader(_config(), outcomes=[exists])
    service = _service(manager, uploader)
    service.start()
    try:
        receipt = service.capture_initial(
            RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
            expected_container_id=_CONTAINER_ID,
            preparation_id=_PREPARATION_ID,
            base_sha=_BASE_SHA,
            members=(),
        )
    finally:
        service.close()

    assert receipt == {
        "enabled": True,
        "stored": True,
        "already_exists": True,
        "bucket": exists.bucket,
        "key": exists.key,
    }
    assert len(uploader.calls) == 1
    assert uploader.bodies[0].closed


def test_initial_upload_failure_logs_safe_type_and_discards_the_spool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    unsafe = "https://user:secret@example.com/private/object?X-Amz-Credential=leaked"
    error = RuntimeError(unsafe)
    uploader = _Uploader(_config(), outcomes=[error, error, error])
    service = _service(manager, uploader)
    service.start()
    try:
        with pytest.raises(SandboxError, match="could not be stored") as excinfo:
            service.capture_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                base_sha=_BASE_SHA,
                members=(),
            )
        assert excinfo.value.__cause__ is error
    finally:
        service.close()

    assert len(uploader.calls) == 3
    assert {call["position"] for call in uploader.calls} == {0}
    assert len({cast("bytes", call["payload"]) for call in uploader.calls}) == 1
    assert uploader.bodies[0].closed
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "sandbox_initial_archive_upload_failed"
    )
    assert record.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert record.exc_info is None
    assert unsafe not in caplog.text


def test_initial_conditional_conflict_has_exactly_three_total_put_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unsafe = "conditional conflict for s3://private-bucket/secret-object"

    class ConditionalConflict(RuntimeError):
        def __init__(self) -> None:
            self.response = {
                "Error": {"Code": "ConditionalRequestConflict"},
                "ResponseMetadata": {"HTTPStatusCode": 409},
            }
            super().__init__(unsafe)

    class AlwaysConflictClient:
        def __init__(self) -> None:
            self.puts = 0

        def get_bucket_versioning(self, **_kwargs: Any) -> dict[str, str]:
            return {"Status": "Enabled"}

        def put_object(self, **_kwargs: Any) -> dict[str, Any]:
            self.puts += 1
            raise ConditionalConflict()

    spec = _spec()
    manager = _Manager(spec, running=True)
    client = AlwaysConflictClient()
    config = _config()
    uploader = SandboxArchiveS3Uploader(
        config,
        client=client,  # type: ignore[arg-type]
        clock=_zero_clock,
    )
    service = SandboxArchiveService(
        cast("ChatSandboxManager", manager),
        namespace=_NAMESPACE,
        uploader=uploader,
        config=config,
        max_upload_attempts=5,
        retry_delay_s=0,
        sleep=_no_sleep,
        clock=_zero_clock,
    )
    service.start()
    try:
        with pytest.raises(SandboxError, match="could not be stored"):
            service.capture_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                base_sha=_BASE_SHA,
                members=(),
            )
    finally:
        service.close()

    assert client.puts == 3
    assert service.status()["phases"] == {
        "initial": {"pending": 0, "uploaded": 0, "failed": 1},
        "final": {"pending": 0, "uploaded": 0, "failed": 0},
    }
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "sandbox_initial_archive_upload_failed"
    )
    assert record.error_type == "ConditionalConflict"  # type: ignore[attr-defined]
    assert record.exc_info is None
    assert unsafe not in caplog.text


def test_commit_response_loss_reuses_the_marker_without_rebuilding() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    uploader = _Uploader(
        _config(),
        outcomes=[SystemExit("response lost after commit")],
        exists_outcomes=[True],
    )
    service = _service(manager, uploader)
    service.start()
    try:
        with pytest.raises(SystemExit, match="response lost"):
            service.capture_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                base_sha=_BASE_SHA,
                members=(),
            )
        marker = manager.marker
        assert marker is not None

        status = service.probe_initial(
            RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
            expected_container_id=_CONTAINER_ID,
            preparation_id=_PREPARATION_ID,
            members=(),
        )
    finally:
        service.close()

    assert status == {
        "enabled": True,
        "prepared": True,
        "stored": True,
        "base_sha": _BASE_SHA,
        "size_bytes": uploader.calls[0]["size"],
        "sha256": uploader.calls[0]["sha256"],
    }
    assert manager.marker == marker
    assert [call["path"] for call in manager.export_calls] == ["/workspace"]
    assert len(uploader.calls) == 1


def test_initial_probe_is_pending_for_its_whole_head_operation_and_completes_neutrally() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    uploader = _Uploader(_config())
    service = _service(manager, uploader)
    service.start()
    service.capture_initial(
        RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
        expected_container_id=_CONTAINER_ID,
        preparation_id=_PREPARATION_ID,
        base_sha=_BASE_SHA,
        members=(),
    )
    entered = threading.Event()
    release = threading.Event()
    result: list[dict[str, object]] = []

    def blocking_exists(**_kwargs: Any) -> bool:
        entered.set()
        assert release.wait(timeout=5)
        return True

    uploader.initial_exists = blocking_exists  # type: ignore[method-assign]

    def probe() -> None:
        result.append(
            service.probe_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                members=(),
            )
        )

    worker = threading.Thread(target=probe)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        status = service.status()
        assert status["pending"] == 1
        assert status["phases"] == {
            "initial": {"pending": 1, "uploaded": 1, "failed": 0},
            "final": {"pending": 0, "uploaded": 0, "failed": 0},
        }
    finally:
        release.set()
        worker.join(timeout=5)
        service.close()

    assert not worker.is_alive()
    assert result[0]["stored"] is True
    assert service.status()["phases"] == {
        "initial": {"pending": 0, "uploaded": 1, "failed": 0},
        "final": {"pending": 0, "uploaded": 0, "failed": 0},
    }


def test_authoritative_initial_404_reuploads_the_exact_marked_bundle() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    uploader = _Uploader(
        _config(),
        outcomes=[SystemExit("upload process died")],
        exists_outcomes=[False],
    )
    service = _service(manager, uploader)
    service.start()
    try:
        with pytest.raises(SystemExit):
            service.capture_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                base_sha=_BASE_SHA,
                members=(),
            )
        marker = manager.marker
        assert marker is not None
        status = service.probe_initial(
            RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
            expected_container_id=_CONTAINER_ID,
            preparation_id=_PREPARATION_ID,
            members=_members(),
        )
    finally:
        service.close()

    assert {
        key: status[key] for key in ("enabled", "prepared", "stored", "already_exists", "base_sha")
    } == {
        "enabled": True,
        "prepared": True,
        "stored": True,
        "already_exists": False,
        "base_sha": _BASE_SHA,
    }
    assert manager.marker == marker
    assert [call["path"] for call in manager.export_calls] == ["/workspace", "/workspace"]
    assert len(uploader.calls) == 2
    assert uploader.calls[1]["payload"] == uploader.calls[0]["payload"]
    assert uploader.calls[1]["size"] == uploader.calls[0]["size"]
    assert uploader.calls[1]["sha256"] == uploader.calls[0]["sha256"]
    assert service.status()["phases"] == {
        "initial": {"pending": 0, "uploaded": 1, "failed": 1},
        "final": {"pending": 0, "uploaded": 0, "failed": 0},
    }


def test_authoritative_initial_404_preserves_marker_and_fails_closed_on_drift() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    uploader = _Uploader(
        _config(),
        outcomes=[SystemExit("upload process died")],
        exists_outcomes=[False],
    )
    service = _service(manager, uploader)
    service.start()
    try:
        with pytest.raises(SystemExit):
            service.capture_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                base_sha=_BASE_SHA,
                members=(),
            )
        marker = manager.marker
        assert marker is not None
        manager.payloads["/workspace"] = b"workspace changed after the marked capture"
        with pytest.raises(SandboxError, match="no longer matches"):
            service.probe_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                members=(),
            )
    finally:
        service.close()

    assert manager.marker == marker
    assert [call["path"] for call in manager.export_calls] == ["/workspace", "/workspace"]
    assert len(uploader.calls) == 1
    assert service.status()["phases"] == {
        "initial": {"pending": 0, "uploaded": 0, "failed": 2},
        "final": {"pending": 0, "uploaded": 0, "failed": 0},
    }


def test_transient_initial_head_failure_preserves_marker_and_fails_closed() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    denied = RuntimeError("HEAD denied")
    uploader = _Uploader(
        _config(),
        outcomes=[SystemExit("upload process died")],
        exists_outcomes=[denied],
    )
    service = _service(manager, uploader)
    service.start()
    try:
        with pytest.raises(SystemExit):
            service.capture_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                base_sha=_BASE_SHA,
                members=(),
            )
        marker = manager.marker
        with pytest.raises(SandboxError, match="could not be verified") as excinfo:
            service.probe_initial(
                RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                expected_container_id=_CONTAINER_ID,
                preparation_id=_PREPARATION_ID,
                members=(),
            )
        assert excinfo.value.__cause__ is denied
    finally:
        service.close()

    assert manager.marker == marker


def test_final_capture_requires_the_exact_stopped_container() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    uploader = _Uploader(_config())
    service = _service(manager, uploader)
    service.start()
    try:
        with pytest.raises(SandboxError, match="exact stopped container"):
            service.queue_final(spec, manager.current, ())

        manager.current = _inspection(spec, running=False)
        stale = replace(manager.current, container_id="container-replaced")
        with pytest.raises(SandboxError, match="identity changed"):
            service.queue_final(spec, stale, ())
    finally:
        service.close()

    assert manager.export_calls == []
    assert uploader.calls == []


def test_final_spools_and_uploads_before_the_stop_hook_returns() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    uploader = _Uploader(_config())
    service = _service(manager, uploader)
    service.start()
    service.queue_final(spec, manager.current, _members())
    service.close()

    assert [call["path"] for call in manager.export_calls] == [
        "/workspace",
        "/agent-state",
        "/home/agent/.freeinference-platform",
    ]
    assert all(call["expected_container_id"] == _CONTAINER_ID for call in manager.export_calls)
    assert len(uploader.calls) == 1
    upload = uploader.calls[0]
    assert {
        key: upload[key] for key in ("namespace", "sandbox_id", "generation", "phase", "position")
    } == {
        "namespace": _NAMESPACE,
        "sandbox_id": spec.sandbox_id,
        "generation": spec.generation,
        "phase": "final",
        "position": 0,
    }
    assert upload["thread"] == threading.current_thread().name
    names, manifest, contents = _read_bundle(upload["payload"])
    assert names == [
        "manifest.json",
        "paths/workspace.tar",
        "paths/agent-state.tar",
        "paths/freeinference-platform.tar",
    ]
    assert contents["paths/workspace.tar"] == manager.payloads["/workspace"]
    assert contents["paths/agent-state.tar"] == manager.payloads["/agent-state"]
    assert (
        contents["paths/freeinference-platform.tar"]
        == manager.payloads["/home/agent/.freeinference-platform"]
    )
    assert manifest["phase"] == "final"
    assert "base_sha" not in manifest["metadata"]
    assert "members" not in manifest["metadata"]
    assert service.status() == {
        "enabled": True,
        "running": False,
        "pending": 0,
        "uploaded": 1,
        "failed": 0,
        "phases": {
            "initial": {"pending": 0, "uploaded": 0, "failed": 0},
            "final": {"pending": 0, "uploaded": 1, "failed": 0},
        },
    }
    assert uploader.bodies[0].closed


@pytest.mark.parametrize("phase", ["initial", "final"])
def test_transient_member_state_cannot_change_archive_bytes(phase: str) -> None:
    """Sandbox Manager reconstruction must not degrade or fork a deterministic object."""
    payloads: list[bytes] = []
    member_sets = (
        _members(),
        (
            SharedMemberManifest(
                thread_id="thread-origin",
                native_session_id=None,
                context_status="new",
                resident_slot_id=0,
            ),
        ),
    )
    for members in member_sets:
        spec = _spec()
        manager = _Manager(spec, running=phase == "initial")
        uploader = _Uploader(_config())
        service = _service(manager, uploader)
        service.start()
        try:
            if phase == "initial":
                service.capture_initial(
                    RetainedChatBackend(manager, spec),  # type: ignore[arg-type]
                    expected_container_id=_CONTAINER_ID,
                    preparation_id=_PREPARATION_ID,
                    base_sha=_BASE_SHA,
                    members=members,
                )
            else:
                service.queue_final(spec, manager.current, members)
        finally:
            service.close()
        payloads.append(uploader.calls[0]["payload"])

    assert payloads[0] == payloads[1]


def test_final_omits_an_absent_optional_platform_path() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    manager.payloads.pop("/home/agent/.freeinference-platform")
    uploader = _Uploader(_config())
    service = _service(manager, uploader)
    service.start()
    service.queue_final(spec, manager.current, ())
    service.close()

    names, manifest, _contents = _read_bundle(uploader.calls[0]["payload"])
    assert names == ["manifest.json", "paths/workspace.tar", "paths/agent-state.tar"]
    assert manifest["payload_count"] == 2
    assert service.status()["uploaded"] == 1


def test_final_retry_rewinds_the_same_spool_before_each_attempt() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    uploader = _Uploader(_config(), outcomes=[RuntimeError("transient"), None])
    delays: list[float] = []
    service = _service(
        manager,
        uploader,
        max_upload_attempts=2,
        retry_delay_s=0.25,
        sleep=delays.append,
    )
    service.start()
    service.queue_final(spec, manager.current, ())
    service.close()

    assert [call["position"] for call in uploader.calls] == [0, 0]
    assert uploader.calls[0]["payload"] == uploader.calls[1]["payload"]
    assert uploader.bodies[0] is uploader.bodies[1]
    assert uploader.bodies[0].closed
    assert delays == [0.25]
    assert service.status()["uploaded"] == 1
    assert service.status()["failed"] == 0


def test_final_exhaustion_is_reported_and_never_counted_as_a_clean_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    unsafe = "https://user:secret@example.com/private-final?token=leaked"
    errors: list[BaseException | None] = [
        RuntimeError(unsafe),
        RuntimeError(unsafe),
    ]
    uploader = _Uploader(_config(), outcomes=errors)
    service = _service(manager, uploader, max_upload_attempts=2)
    service.start()
    try:
        with pytest.raises(SandboxError, match="final sandbox archive could not be stored") as exc:
            service.queue_final(spec, manager.current, ())
        assert exc.value.__cause__ is errors[-1]
    finally:
        service.close()

    assert len(uploader.calls) == 2
    assert uploader.bodies[0] is uploader.bodies[1]
    assert uploader.bodies[0].closed
    assert service.status()["uploaded"] == 0
    assert service.status()["failed"] == 1
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "sandbox_final_archive_upload_failed"
    )
    assert record.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert record.exc_info is None
    assert unsafe not in caplog.text


def test_capture_capacity_blocks_before_copy_instead_of_dropping_a_clean_stop() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    entered = threading.Event()
    release = threading.Event()
    uploader = _Uploader(_config(), entered=entered, release=release)
    service = _service(manager, uploader, capture_capacity=1)
    service.start()
    failures: list[BaseException] = []

    def capture() -> None:
        try:
            service.queue_final(spec, manager.current, ())
        except BaseException as exc:  # pragma: no cover - assertion reports the value.
            failures.append(exc)

    first = threading.Thread(target=capture, name="first-final")
    second = threading.Thread(target=capture, name="second-final")
    first.start()
    try:
        assert entered.wait(timeout=5)
        second.start()
        # The second capture waits on the semaphore before inspect/export, so
        # only the first final's three roots can have reached Docker.
        second.join(timeout=0.05)
        assert second.is_alive()
        assert len(manager.export_calls) == 3
        assert service.status()["pending"] == 2
        assert service.status()["phases"]["final"]["pending"] == 2
    finally:
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        service.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert service.status()["uploaded"] == 2
    assert len(manager.export_calls) == 6


def test_close_is_bounded_and_prevents_a_waiter_from_starting_copy() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    entered = threading.Event()
    release = threading.Event()
    uploader = _Uploader(_config(), entered=entered, release=release)
    service = _service(manager, uploader, capture_capacity=1)
    service.start()
    failures: list[BaseException] = []

    def capture() -> None:
        try:
            service.queue_final(spec, manager.current, ())
        except BaseException as exc:
            failures.append(exc)

    active = threading.Thread(target=capture, name="active-final")
    waiter = threading.Thread(target=capture, name="waiting-final")
    close_done = threading.Event()

    def close_service() -> None:
        service.close()
        close_done.set()

    closer = threading.Thread(target=close_service, name="archive-close")
    active.start()
    try:
        assert entered.wait(timeout=5)
        waiter.start()
        waiter.join(timeout=0.05)
        assert waiter.is_alive()
        closer.start()
        closer.join(timeout=0.05)
        assert closer.is_alive()
        assert uploader.close_calls == 0
        assert service.status()["running"] is False
        assert len(manager.export_calls) == 3
    finally:
        release.set()
        active.join(timeout=5)
        waiter.join(timeout=5)
        closer.join(timeout=5)

    assert not active.is_alive()
    assert not waiter.is_alive()
    assert not closer.is_alive()
    assert close_done.is_set()
    assert uploader.close_calls == 1
    assert len(uploader.calls) == 1
    assert len(manager.export_calls) == 3
    assert len(failures) == 1
    assert isinstance(failures[0], SandboxError)
    assert "not running" in str(failures[0])
    assert service.status() == {
        "enabled": True,
        "running": False,
        "pending": 0,
        "uploaded": 1,
        "failed": 1,
        "phases": {
            "initial": {"pending": 0, "uploaded": 0, "failed": 0},
            "final": {"pending": 0, "uploaded": 1, "failed": 1},
        },
    }


def test_spool_budget_rejects_unsafe_concurrency_before_capture() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    uploader = _Uploader(_config())

    with pytest.raises(ValueError, match=r"above the .*budget"):
        _service(
            manager,
            uploader,
            capture_capacity=2,
            spool_budget_bytes=7 * 1024 * 1024 * 1024,
        )

    assert manager.export_calls == []
    assert uploader.calls == []


def test_operation_deadline_expires_before_copy_when_capture_admission_consumes_budget() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    uploader = _Uploader(_config())
    readings = iter((0.0, 2.0))
    service = _service(
        manager,
        uploader,
        operation_timeout_s=1.0,
        clock=lambda: next(readings, 2.0),
    )
    service.start()
    try:
        with pytest.raises(SandboxError, match="timed out"):
            service.queue_final(spec, manager.current, ())
    finally:
        service.close()

    assert manager.export_calls == []
    assert uploader.calls == []


def test_final_retries_stop_before_backoff_would_cross_operation_deadline() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    uploader = _Uploader(
        _config(),
        outcomes=[RuntimeError("first"), RuntimeError("second"), None],
    )
    now = 0.0
    delays: list[float] = []

    def clock() -> float:
        return now

    def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    service = _service(
        manager,
        uploader,
        max_upload_attempts=3,
        retry_delay_s=0.6,
        sleep=sleep,
        operation_timeout_s=1.0,
        clock=clock,
    )
    service.start()
    try:
        with pytest.raises(SandboxError, match="operation timed out") as excinfo:
            service.queue_final(spec, manager.current, ())
    finally:
        service.close()

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert len(uploader.calls) == 2
    assert [call["deadline"] for call in uploader.calls] == [1.0, 1.0]
    assert delays == [0.6]
    assert service.status()["failed"] == 1


class _Capture:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def capture(self, spec, inspection, members, *, upload, max_bytes, deadline) -> Any:
        self.calls.append(
            {
                "sandbox_id": spec.sandbox_id,
                "container_id": inspection.container_id,
                "members": members,
                "upload": upload,
                "max_bytes": max_bytes,
                "deadline": deadline,
            }
        )
        if self.error is not None:
            raise self.error
        return SimpleNamespace(size_bytes=1)


def _checkpoint_service(manager: _Manager, capture: _Capture) -> SandboxArchiveService:
    uploader = _Uploader(_config())
    return SandboxArchiveService(
        cast("ChatSandboxManager", manager),
        namespace=_NAMESPACE,
        uploader=cast("SandboxArchiveS3Uploader", uploader),
        config=uploader.config,
        operation_timeout_s=60,
        clock=_zero_clock,
        checkpoint_capture=cast("Any", capture),
    )


def test_checkpoint_capture_uses_the_exact_stopped_container_and_this_uploader() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    capture = _Capture()
    service = _checkpoint_service(manager, capture)
    service.start()

    service.capture_checkpoint(spec, manager.current, _members())

    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["container_id"] == _CONTAINER_ID
    assert call["members"] == _members()
    assert call["max_bytes"] == _config().max_archive_bytes
    assert call["deadline"] == 60
    assert call["upload"].__name__ == "upload_checkpoint"
    assert service.status()["phases"]["checkpoint"] == {
        "pending": 0,
        "uploaded": 1,
        "failed": 0,
    }


def test_checkpoint_capture_refuses_a_restarted_container_and_counts_failures() -> None:
    spec = _spec()
    manager = _Manager(spec, running=True)
    capture = _Capture()
    service = _checkpoint_service(manager, capture)
    service.start()

    with pytest.raises(SandboxError, match="exact stopped container"):
        service.capture_checkpoint(spec, _inspection(spec, running=False), _members())

    assert capture.calls == []
    assert service.status()["phases"]["checkpoint"]["failed"] == 1


def test_a_service_without_checkpoints_keeps_its_status_shape() -> None:
    spec = _spec()
    manager = _Manager(spec, running=False)
    service = _service(manager, _Uploader(_config()))
    service.start()

    assert set(service.status()["phases"]) == {"initial", "final"}
    with pytest.raises(SandboxError, match="not configured"):
        service.capture_checkpoint(spec, manager.current, _members())
