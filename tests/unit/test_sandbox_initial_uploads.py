"""The deferred initial archive: durable hand-off, background upload, and recovery."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.test_agent_sandbox_manager import _identity, _SharedSupervisor
from tests.unit.test_sandbox_archives import (
    _BASE_SHA,
    _CONTAINER_ID,
    _NAMESPACE,
    _PREPARATION_ID,
    _config,
    _Manager,
    _no_sleep,
    _spec,
    _Uploader,
    _zero_clock,
)

from cloud_agent_host import (
    sandbox_archive_runtime as archive_runtime,
    sandbox_initial_uploads as uploads_mod,
)
from cloud_agent_host.chat_sandbox import ChatSandboxManager, RetainedChatBackend
from cloud_agent_host.group_archive_policy import ServiceArchivePolicy
from cloud_agent_host.sandbox import ProcessBackend
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.sandbox_archives import SandboxArchiveService
from cloud_agent_host.sandbox_initial_uploads import (
    InitialUploadSpool,
    InitialUploadWorker,
    SpooledInitialUpload,
)
from cloud_agent_host.sandbox_manager import create_app
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveS3Config, SandboxArchiveS3Uploader

_WAIT_S = 5.0


def _service(
    manager: _Manager, uploader: _Uploader, spool: InitialUploadSpool | None
) -> SandboxArchiveService:
    service = SandboxArchiveService(
        cast("ChatSandboxManager", manager),
        namespace=_NAMESPACE,
        uploader=cast("SandboxArchiveS3Uploader", uploader),
        config=uploader.config,
        retry_delay_s=0,
        sleep=_no_sleep,
        clock=_zero_clock,
        initial_uploads=spool,
    )
    service.start()
    return service


def _spool(root: Path, *, budget_bytes: int = 8 * 1024**3) -> InitialUploadSpool:
    spool = InitialUploadSpool(root, budget_bytes=budget_bytes)
    spool.start()
    return spool


def _capture(service: SandboxArchiveService, manager: _Manager, **kwargs: Any) -> dict[str, Any]:
    return service.capture_initial(
        RetainedChatBackend(manager, _spec()),  # type: ignore[arg-type]
        expected_container_id=_CONTAINER_ID,
        preparation_id=_PREPARATION_ID,
        base_sha=_BASE_SHA,
        members=(),
        **kwargs,
    )


def _probe(service: SandboxArchiveService, manager: _Manager) -> dict[str, Any]:
    return service.probe_initial(
        RetainedChatBackend(manager, _spec()),  # type: ignore[arg-type]
        expected_container_id=_CONTAINER_ID,
        preparation_id=_PREPARATION_ID,
        members=(),
    )


def _bundle(entry: SpooledInitialUpload) -> bytes:
    return (entry.directory / "bundle.tar.gz").read_bytes()


def _wait_for(condition: Any) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.01)


def test_a_deferred_capture_returns_once_its_bundle_is_durable_and_marked(tmp_path) -> None:
    """The first prompt waits for the frozen bytes, not for S3."""
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config())
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)

    receipt = _capture(service, manager, defer_upload=True)

    [entry] = spool.entries()
    bundle = _bundle(entry)
    assert receipt == {
        "enabled": True,
        "stored": False,
        "upload": "pending",
        "size_bytes": len(bundle),
        "sha256": hashlib.sha256(bundle).hexdigest(),
    }
    assert uploader.calls == []
    # The container's marker names exactly the bytes the hand-off holds.
    assert manager.marker == entry.marker
    marker = json.loads(entry.marker)
    assert (marker["bundle_size_bytes"], marker["bundle_sha256"]) == (
        len(bundle),
        hashlib.sha256(bundle).hexdigest(),
    )
    # Neither stored nor failed until the background upload ends.
    assert service.status()["phases"]["initial"] == {"pending": 0, "uploaded": 0, "failed": 0}

    stored = service.upload_spooled_initial(entry)

    [upload] = uploader.calls
    assert upload["payload"] == bundle
    assert upload["phase"] == "initial"
    assert upload["sha256"] == marker["bundle_sha256"]
    assert stored["stored"] is True
    assert spool.entries() == []
    assert list(tmp_path.iterdir()) == []
    assert service.status()["phases"]["initial"] == {"pending": 0, "uploaded": 1, "failed": 0}
    service.close()


def test_the_marker_is_written_only_once_the_hand_off_is_published(tmp_path) -> None:
    """A marker never names bytes this host could still lose."""
    manager = _Manager(_spec(), running=True)
    spool = _spool(tmp_path)
    service = _service(manager, _Uploader(_config()), spool)
    seen: list[list[str]] = []
    write = manager.write_initial_archive_marker

    def observed(spec, payload: bytes, **kwargs: Any) -> None:
        [entry] = [child for child in tmp_path.iterdir() if not child.name.startswith(".")]
        seen.append(sorted(path.name for path in entry.iterdir()))
        write(spec, payload, **kwargs)

    manager.write_initial_archive_marker = observed  # type: ignore[method-assign]
    _capture(service, manager, defer_upload=True)

    # Published (bundle and marker on disk), not yet committed.
    assert seen == [["bundle.tar.gz", "marker.json"]]
    [entry] = spool.entries()
    assert sorted(path.name for path in entry.directory.iterdir()) == [
        "bundle.tar.gz",
        "committed",
        "marker.json",
    ]
    service.close()


def test_a_failed_marker_write_drops_the_hand_off(tmp_path) -> None:
    """Without a committed marker the old rules apply: the frozen workspace is the copy."""
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config())
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)

    def lost(*_args: Any, **_kwargs: Any) -> None:
        raise SandboxError("marker write timed out")

    manager.write_initial_archive_marker = lost  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="marker write timed out"):
        _capture(service, manager, defer_upload=True)

    assert list(tmp_path.iterdir()) == []
    assert uploader.calls == []
    assert service.status()["phases"]["initial"] == {"pending": 0, "uploaded": 0, "failed": 1}
    # The room it reserved is free again.
    manager.write_initial_archive_marker = _Manager.write_initial_archive_marker.__get__(manager)
    assert _capture(service, manager, defer_upload=True)["upload"] == "pending"
    service.close()


def test_a_probe_after_the_prompt_ran_uploads_the_hand_off_not_the_workspace(tmp_path) -> None:
    """Once the CLI has changed the workspace, only the hand-off still has the marked bytes."""
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config(), exists_outcomes=[False])
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)
    _capture(service, manager, defer_upload=True)
    [entry] = spool.entries()
    bundle = _bundle(entry)
    manager.payloads["/workspace"] = b"the agent edited the workspace"

    receipt = _probe(service, manager)

    assert receipt["prepared"] is True
    assert receipt["stored"] is True
    assert receipt["sha256"] == hashlib.sha256(bundle).hexdigest()
    [upload] = uploader.calls
    assert upload["payload"] == bundle
    # One export: the capture's. Nothing was rebuilt from the changed workspace.
    assert [call["path"] for call in manager.export_calls] == ["/workspace"]
    assert spool.entries() == []
    service.close()


def test_a_hand_off_that_lost_its_bytes_is_dropped_and_the_probe_rebuilds(tmp_path) -> None:
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config(), exists_outcomes=[False])
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)
    _capture(service, manager, defer_upload=True)
    [entry] = spool.entries()
    bundle = _bundle(entry)
    (entry.directory / "bundle.tar.gz").write_bytes(bundle[:-1])

    receipt = _probe(service, manager)

    # The frozen workspace still rebuilds the marked bundle, as before deferral.
    assert receipt["stored"] is True
    [upload] = uploader.calls
    assert upload["payload"] == bundle
    assert [call["path"] for call in manager.export_calls] == ["/workspace", "/workspace"]
    assert spool.entries() == []
    service.close()


def test_a_hand_off_a_probe_already_stored_is_not_uploaded_again(tmp_path) -> None:
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config(), exists_outcomes=[False])
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)
    _capture(service, manager, defer_upload=True)
    [entry] = spool.entries()
    _probe(service, manager)

    # The worker's view of the same entry, from before the probe removed it.
    assert service.upload_spooled_initial(entry) is None

    assert len(uploader.calls) == 1
    assert service.status()["phases"]["initial"] == {"pending": 0, "uploaded": 1, "failed": 0}
    service.close()


def test_a_probe_that_waited_out_the_background_upload_asks_s3_again(tmp_path) -> None:
    """The entry vanished while the probe waited: S3, not a rebuild, says it is stored."""
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config(), exists_outcomes=[False, True])
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)
    _capture(service, manager, defer_upload=True)
    [entry] = spool.entries()
    manager.payloads["/workspace"] = b"the agent edited the workspace"
    claim = spool.claim

    @contextmanager
    def worker_finished_first(entry_id: str, *, timeout: float) -> Iterator[None]:
        with claim(entry_id, timeout=timeout):
            spool.remove(entry)
            yield

    spool.claim = worker_finished_first  # type: ignore[method-assign]
    receipt = _probe(service, manager)

    assert receipt["stored"] is True
    assert receipt["prepared"] is True
    assert uploader.calls == []
    assert len(uploader.exists_calls) == 2
    assert [call["path"] for call in manager.export_calls] == ["/workspace"]
    service.close()


def test_a_probe_whose_hand_off_was_already_uploaded_asks_s3_before_rebuilding(tmp_path) -> None:
    """The worker stored the entry and removed it between the probe's HEAD and its lookup.

    The prompt has since changed the workspace, so a rebuild could only fail:
    S3 is asked again first, and it holds the marked object.
    """
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config(), exists_outcomes=[False, True])
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)
    _capture(service, manager, defer_upload=True)
    [entry] = spool.entries()
    manager.payloads["/workspace"] = b"the agent edited the workspace"
    spool.remove(entry)

    receipt = _probe(service, manager)

    assert receipt["stored"] is True
    assert receipt["prepared"] is True
    assert uploader.calls == []
    assert len(uploader.exists_calls) == 2
    assert [call["path"] for call in manager.export_calls] == ["/workspace"]
    service.close()


def test_the_worker_cannot_take_an_entry_before_its_commit_is_durable(
    tmp_path, monkeypatch
) -> None:
    """``committed`` is visible before the entry directory is synced; the worker must wait."""
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config())
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)
    sync = uploads_mod._sync_directory
    racers: list[threading.Thread] = []

    def worker_races_the_commit(directory: Path) -> None:
        if (directory / "committed").exists() and not racers:
            [entry] = spool.entries()
            racer = threading.Thread(target=service.upload_spooled_initial, args=(entry,))
            racers.append(racer)
            racer.start()
            # Unguarded, the worker uploads and removes the directory right here.
            racer.join(timeout=0.5)
        sync(directory)

    monkeypatch.setattr(uploads_mod, "_sync_directory", worker_races_the_commit)
    receipt = _capture(service, manager, defer_upload=True)
    [racer] = racers
    racer.join(_WAIT_S)

    assert receipt["upload"] == "pending"
    assert not racer.is_alive()
    assert len(uploader.calls) == 1
    assert spool.entries() == []
    service.close()


def test_without_room_in_the_spool_the_capture_stores_before_returning(tmp_path) -> None:
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config())
    # Less than one bundle's worst case.
    spool = _spool(tmp_path, budget_bytes=1024)
    service = _service(manager, uploader, spool)

    receipt = _capture(service, manager, defer_upload=True)

    assert receipt["stored"] is True
    assert "upload" not in receipt
    assert len(uploader.calls) == 1
    assert spool.entries() == []
    assert list(tmp_path.iterdir()) == []
    service.close()


def test_a_capture_not_asked_to_defer_stores_before_returning(tmp_path) -> None:
    manager = _Manager(_spec(), running=True)
    uploader = _Uploader(_config())
    spool = _spool(tmp_path)
    service = _service(manager, uploader, spool)

    assert _capture(service, manager)["stored"] is True
    assert len(uploader.calls) == 1
    assert spool.entries() == []
    service.close()


def test_a_marker_the_caller_read_is_not_read_again(tmp_path) -> None:
    """The controller already read it inside the same frozen boundary."""
    manager = _Manager(_spec(), running=True)
    service = _service(manager, _Uploader(_config()), _spool(tmp_path))
    reads: list[object] = []
    read = manager.read_initial_archive_marker

    def counted(*args: Any, **kwargs: Any) -> bytes | None:
        reads.append(args)
        return read(*args, **kwargs)

    manager.read_initial_archive_marker = counted  # type: ignore[method-assign]
    _capture(service, manager, marker_payload=None, defer_upload=True)
    assert reads == []
    _capture_marker = manager.marker
    # A probe handed the marker reads it no more than the capture did.
    manager.payloads["/workspace"] = b"changed"
    service.probe_initial(
        RetainedChatBackend(manager, _spec()),  # type: ignore[arg-type]
        expected_container_id=_CONTAINER_ID,
        preparation_id=_PREPARATION_ID,
        members=(),
        marker_payload=_capture_marker,
    )
    assert reads == []
    service.close()


def test_start_keeps_committed_hand_offs_and_drops_the_rest(tmp_path) -> None:
    manager = _Manager(_spec(), running=True)
    spool = _spool(tmp_path)
    service = _service(manager, _Uploader(_config()), spool)
    _capture(service, manager, defer_upload=True)
    service.close()
    [committed] = spool.entries()
    # A capture cut off between publishing and committing, and one mid-write.
    uncommitted = tmp_path / ("e" * 64)
    uncommitted.mkdir()
    (uncommitted / "bundle.tar.gz").write_bytes(b"bundle")
    (uncommitted / "marker.json").write_bytes(b"{}")
    staging = tmp_path / ".staging-cut"
    staging.mkdir()

    restarted = InitialUploadSpool(tmp_path)
    restarted.start()

    assert [entry.entry_id for entry in restarted.entries()] == [committed.entry_id]
    assert sorted(child.name for child in tmp_path.iterdir()) == [committed.entry_id]
    # The committed entry still counts against the budget, and blocks its key.
    assert restarted.reserve(committed.entry_id, 1) is None


class _Uploads:
    """An upload callback that fails as told, then stores."""

    def __init__(self, spool: InitialUploadSpool, *, failures: int = 0, ready: bool = True):
        self.spool = spool
        self.failures = failures
        self.ready = ready
        self.calls = 0

    def __call__(self, entry: SpooledInitialUpload) -> bool:
        if not self.ready:
            return False
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise SandboxError("S3 is unavailable")
        self.spool.remove(entry)
        return True


def _committed_entry(tmp_path: Path) -> InitialUploadSpool:
    manager = _Manager(_spec(), running=True)
    spool = _spool(tmp_path)
    service = _service(manager, _Uploader(_config()), spool)
    _capture(service, manager, defer_upload=True)
    service.close()
    return spool


def test_the_worker_retries_a_failed_upload_until_it_is_stored(tmp_path, caplog) -> None:
    spool = _committed_entry(tmp_path)
    uploads = _Uploads(spool, failures=2)
    worker = InitialUploadWorker(spool, uploads, retry_delays_s=(0.01, 0.02), poll_s=60)
    worker.start()
    try:
        _wait_for(lambda: spool.entries() == [])
    finally:
        worker.close()

    assert uploads.calls == 3
    retries = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "sandbox_initial_archive_deferred_retry"
    ]
    assert [record.__dict__["attempts"] for record in retries] == [1, 2]
    assert {record.__dict__["error_type"] for record in retries} == {"SandboxError"}


def test_the_worker_waits_for_a_policy_without_counting_failures(tmp_path, caplog) -> None:
    spool = _committed_entry(tmp_path)
    uploads = _Uploads(spool, ready=False)
    worker = InitialUploadWorker(spool, uploads, retry_delays_s=(0.01,), poll_s=60)
    worker.start()
    try:
        time.sleep(0.05)
        assert len(spool.entries()) == 1
        uploads.ready = True
        worker.wake()
        _wait_for(lambda: spool.entries() == [])
    finally:
        worker.close()

    assert uploads.calls == 1
    assert not [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "sandbox_initial_archive_deferred_retry"
    ]


def _fake_s3(monkeypatch) -> list[_Uploader]:
    uploaders: list[_Uploader] = []

    def uploader(config: SandboxArchiveS3Config) -> _Uploader:
        made = _Uploader(config)
        uploaders.append(made)
        return made

    monkeypatch.setattr(archive_runtime, "SandboxArchiveS3Uploader", uploader)
    return uploaders


def _policy() -> dict[str, Any]:
    return {**_config().as_policy().as_dict(), "revision": 1}


def _controller(manager: _Manager, state_dir: Path) -> SandboxArchiveController:
    return SandboxArchiveController(
        cast("ChatSandboxManager", manager),
        namespace=_NAMESPACE,
        initial_upload_dir=state_dir,
    )


def _controller_capture(controller: SandboxArchiveController, manager: _Manager) -> dict[str, Any]:
    return controller.capture_initial(
        RetainedChatBackend(manager, _spec()),  # type: ignore[arg-type]
        expected_container_id=_CONTAINER_ID,
        preparation_id=_PREPARATION_ID,
        base_sha=_BASE_SHA,
        members=(),
        defer_upload=True,
    )


def test_the_controller_uploads_a_deferred_capture_in_the_background(monkeypatch, tmp_path) -> None:
    uploaders = _fake_s3(monkeypatch)
    manager = _Manager(_spec(), running=True)
    controller = _controller(manager, tmp_path)
    controller.start()
    controller.apply(_policy())
    namespace_dir = tmp_path / hashlib.sha256(_NAMESPACE.encode()).hexdigest()
    try:
        receipt = _controller_capture(controller, manager)
        assert receipt["upload"] == "pending"
        _wait_for(lambda: any(each.calls for each in uploaders))
        _wait_for(lambda: list(namespace_dir.iterdir()) == [])
    finally:
        controller.close()

    [upload] = [call for each in uploaders for call in each.calls]
    marker = json.loads(manager.marker or b"{}")
    assert upload["sha256"] == marker["bundle_sha256"]
    assert upload["thread"] == "sandbox-initial-uploads"


def test_a_restart_uploads_what_was_committed_once_a_policy_arrives(monkeypatch, tmp_path) -> None:
    """The bundle outlives the Sandbox Manager; its credentials come with the policy."""
    uploaders = _fake_s3(monkeypatch)
    manager = _Manager(_spec(), running=True)
    namespace_dir = tmp_path / hashlib.sha256(_NAMESPACE.encode()).hexdigest()
    # The process that captured it stopped before its upload.
    _committed_entry(namespace_dir)
    assert len(InitialUploadSpool(namespace_dir).entries()) == 1

    controller = _controller(manager, tmp_path)
    controller.start()
    try:
        time.sleep(0.05)
        assert uploaders == []
        controller.apply(_policy())
        _wait_for(lambda: list(namespace_dir.iterdir()) == [])
    finally:
        controller.close()

    [upload] = [call for each in uploaders for call in each.calls]
    assert upload["phase"] == "initial"


def test_a_controller_without_a_hand_off_directory_stores_synchronously(monkeypatch) -> None:
    uploaders = _fake_s3(monkeypatch)
    manager = _Manager(_spec(), running=True)
    controller = SandboxArchiveController(cast("ChatSandboxManager", manager), namespace=_NAMESPACE)
    controller.start()
    controller.apply(_policy())
    try:
        receipt = _controller_capture(controller, manager)
    finally:
        controller.close()

    assert receipt["stored"] is True
    assert len([call for each in uploaders for call in each.calls]) == 1


def test_an_unusable_hand_off_directory_leaves_captures_synchronous(monkeypatch, tmp_path) -> None:
    _fake_s3(monkeypatch)
    manager = _Manager(_spec(), running=True)
    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"")
    controller = _controller(manager, blocked)
    controller.start()
    controller.apply(_policy())
    try:
        receipt = _controller_capture(controller, manager)
    finally:
        controller.close()

    assert receipt["stored"] is True


def test_a_closed_worker_never_starts_and_closes_again_quietly(tmp_path) -> None:
    spool = _committed_entry(tmp_path)
    uploads = _Uploads(spool)
    worker = InitialUploadWorker(spool, uploads)
    worker.close()
    worker.start()
    worker.close()

    assert not [
        thread for thread in threading.enumerate() if thread.name == "sandbox-initial-uploads"
    ]
    assert uploads.calls == 0
    assert len(spool.entries()) == 1


class _RecordingArchive:
    """An archive boundary that records how the route asked for the capture."""

    def __init__(self) -> None:
        self.captures: list[dict[str, Any]] = []

    def start(self) -> None:
        return

    def close(self) -> None:
        return

    def queue_final(self, *_args: Any, **_kwargs: Any) -> None:
        return

    def capture_initial(self, _backend: Any, **kwargs: Any) -> dict[str, object]:
        self.captures.append(kwargs)
        return {"enabled": True, "stored": False, "upload": "pending"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("asked", "deferred"), [({"defer_upload": True}, True), ({}, False)])
async def test_the_route_defers_only_when_the_runner_asks(asked, deferred) -> None:
    """An older runner omits the field and keeps waiting for the upload."""
    archive = _RecordingArchive()
    supervisor = _SharedSupervisor(archive_policy=ServiceArchivePolicy(archive))  # type: ignore[arg-type]

    def capture_initial_archive(_thread_id, _token, *, capture, **_kwargs):
        return capture(None, None, ())

    supervisor.capture_initial_archive = capture_initial_archive  # type: ignore[attr-defined]
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,  # type: ignore[arg-type]
        sandbox_archive_service=archive,  # type: ignore[arg-type]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.post(
            "/shared-sandboxes/athr_source/archive-initial",
            headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
            json={
                **_identity(),
                "token": "lease-token",
                "preparation_id": "ajob_initial",
                "base_sha": "a" * 40,
                **asked,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"enabled": True, "stored": False, "upload": "pending"}
    [capture] = archive.captures
    assert capture["defer_upload"] is deferred
