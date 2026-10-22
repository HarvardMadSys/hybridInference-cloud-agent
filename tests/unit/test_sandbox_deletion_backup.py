"""Archival integrity, admission barriers, and crash-safe deletion receipts."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from cloud_agent_host import sandbox_deletion_backup as deletion_backup
from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.sandbox import ProcessBackend, SandboxError
from cloud_agent_host.sandbox_deletion_backup import ArchiveIdentity, SandboxDeletionBackupService
from cloud_agent_host.sandbox_manager import create_app
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
)


class _Manager:
    def __init__(self, spec):
        self.backend = SimpleNamespace(uid=10001, gid=10001, image="image", runtime="runc")
        self.current = ChatSandboxInspection(
            container_id="container-1",
            name=spec.container_name,
            status=ChatSandboxStatus.STOPPED,
            running=False,
            image="image",
            labels=spec.docker_labels(),
            mounts=(),
            networks=(),
        )
        self.exports = 0
        self.removals = 0

    def require_existing(self, spec, *, container_id):
        assert self.current is not None
        if self.current.container_id != container_id:
            raise SessionLeaseLost("container identity changed")
        return self.current

    def require_stopped_archive(self, spec, *, container_id):
        current = self.require_existing(spec, container_id=container_id)
        if current.running:
            raise SandboxError("sandbox archive requires a stopped container")
        return current

    def inspect(self, name):
        if self.current is None:
            return None
        if name in (self.current.container_id, self.current.name):
            return self.current
        return None

    def export_stopped_archive(self, spec, *, container_id, output, max_bytes):
        self.require_stopped_archive(spec, container_id=container_id)
        self.exports += 1
        with tarfile.open(fileobj=output, mode="w") as bundle:
            for path, payload in {
                "workspace/repository/file.py": b"retained changes",
                "agent-state/claude/projects/native-session.json": b"native session state",
            }.items():
                info = tarfile.TarInfo(path)
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
        return {"Id": container_id, "Config": {"Image": "image", "User": "10001:10001"}}

    def remove_stopped_archived(self, spec, *, container_id):
        self.require_stopped_archive(spec, container_id=container_id)
        self.removals += 1
        self.current = None


@pytest.fixture
def archive_setup(tmp_path):
    spec = ChatSandboxSpec(
        thread_id="thread-origin", sandbox_id="sandbox-1", generation=2, runtime_id="claude-code"
    )
    manager = _Manager(spec)
    supervisor = SharedChatSessionSupervisor(manager)
    members = [
        SharedMemberManifest(
            thread_id="thread-origin", native_session_id="native-origin", resident_slot_id=0
        ),
        SharedMemberManifest(
            thread_id="thread-peer", native_session_id="native-peer", resident_slot_id=1
        ),
    ]
    supervisor.register_manifest(spec, members, expected_container_id="container-1")
    identity = ArchiveIdentity(
        "thread-origin", "delete-1", 2, "container-1", "archives/delete-1.tar"
    )
    service = SandboxDeletionBackupService(supervisor, state_dir=tmp_path)
    return manager, supervisor, service, identity, spec, members


def _upload(service, identity, receipt, monkeypatch, *, status=200, handler=None):
    client_type = httpx.Client
    calls = []

    def receive(request):
        calls.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(status)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(receive), **kwargs),
    )
    headers = {
        "If-None-Match": "*",
        "Content-Length": str(receipt["size_bytes"]),
        "Content-Type": "application/octet-stream",
        "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(receipt["sha256"])).decode(),
    }
    result = service.upload(
        identity,
        sha256=receipt["sha256"],
        size_bytes=receipt["size_bytes"],
        upload_url="https://archive.s3.amazonaws.com/object?signature=private",
        upload_headers=headers,
    )
    return calls, result


def test_discard_removes_stopped_container_without_export_and_replays(archive_setup):
    manager, supervisor, service, identity, _spec, _members = archive_setup
    discard = replace(identity, request_id="discard-1", archive_key="")
    result = service.discard(discard)
    assert result == {
        "request_id": "discard-1",
        "container_id": "container-1",
        "deleted": True,
    }
    assert manager.exports == 0
    assert manager.removals == 1
    assert not supervisor.owns("thread-origin")
    assert not supervisor.owns("thread-peer")
    directory = service._directory(discard)
    assert (directory / "discard.json").exists()
    assert (directory / "discarded.json").exists()
    assert not (directory / "archive.tar").exists()
    (directory / "discarded.json").unlink()
    assert service.discard(discard)["deleted"] is True
    assert manager.removals == 1


def test_discard_refuses_a_request_that_already_prepared_an_archive(archive_setup):
    manager, _supervisor, service, identity, _spec, _members = archive_setup
    service.prepare(identity)
    with pytest.raises(SessionLeaseLost, match="cannot be discarded"):
        service.discard(replace(identity, archive_key=""))
    assert manager.removals == 0
    assert manager.exports == 1


@pytest.mark.parametrize("discard", [False, True])
@pytest.mark.parametrize("remaining", ["absent", "replacement", "renamed", "unavailable"])
def test_deletion_retry_checks_absence_after_status_marks_container_lost(
    archive_setup, monkeypatch, discard, remaining
):
    manager, supervisor, service, identity, _spec, _members = archive_setup
    original = manager.current
    if discard:
        identity = replace(identity, request_id="discard-1", archive_key="")

        def delete():
            return service.discard(identity)

    else:
        receipt = service.prepare(identity)
        _upload(service, identity, receipt, monkeypatch)

        def delete():
            return service.commit(
                identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"]
            )

    remove = manager.remove_stopped_archived

    def lost_removal_response(spec, *, container_id):
        remove(spec, container_id=container_id)
        raise SandboxError("Docker removal response was lost")

    monkeypatch.setattr(manager, "remove_stopped_archived", lost_removal_response)
    with pytest.raises(SandboxError, match="response was lost"):
        delete()
    assert manager.current is None
    with pytest.raises(SandboxError, match="container is missing"):
        supervisor.list_for_thread(identity.thread_id)

    if remaining == "replacement":
        manager.current = replace(original, container_id="replacement")
    elif remaining == "renamed":
        manager.current = replace(original, name="renamed-original")
    elif remaining == "unavailable":

        def unavailable(_container):
            raise SandboxError("Docker inspection unavailable")

        monkeypatch.setattr(manager, "inspect", unavailable)

    if remaining == "absent":
        assert delete()["deleted"] is True
        assert not supervisor.owns("thread-origin")
        assert not supervisor.owns("thread-peer")
    else:
        with pytest.raises(SandboxError):
            delete()
        assert supervisor.owns("thread-origin")
        assert supervisor.owns("thread-peer")
    assert manager.removals == 1


def test_discard_completes_stale_membership_after_proving_container_absent(archive_setup):
    manager, _supervisor, service, identity, spec, _members = archive_setup
    manager.current = None
    restarted = SandboxDeletionBackupService(
        SharedChatSessionSupervisor(manager), state_dir=service._state_dir
    )
    discard = replace(identity, request_id="discard-absent", archive_key="")

    result = restarted.discard(discard, sandbox_id=spec.sandbox_id)

    assert result == {
        "request_id": "discard-absent",
        "container_id": "container-1",
        "deleted": True,
    }
    directory = restarted._directory(discard)
    assert json.loads((directory / "discard.json").read_text())["target_absent"] is True
    assert (directory / "discarded.json").exists()
    assert manager.removals == 0
    # Replay from the authorization record after a crash before the final
    # acknowledgement; absence is proved again before completing.
    (directory / "discarded.json").unlink()
    with pytest.raises(SessionLeaseLost, match="identity changed"):
        restarted.discard(discard, sandbox_id="another-sandbox")
    assert not (directory / "discarded.json").exists()
    # An older control-plane retry uses the identity in the durable record.
    assert restarted.discard(discard) == result
    assert json.loads((directory / "discarded.json").read_text())["sandbox_id"] == spec.sandbox_id


@pytest.mark.parametrize("replacement", [False, True])
def test_discard_does_not_mistake_an_unregistered_container_for_absence(archive_setup, replacement):
    manager, _supervisor, service, identity, spec, _members = archive_setup
    restarted = SandboxDeletionBackupService(
        SharedChatSessionSupervisor(manager), state_dir=service._state_dir
    )
    discard = replace(identity, request_id="discard-unregistered", archive_key="")
    if replacement:
        manager.current = replace(manager.current, container_id="replacement-container")

    with pytest.raises(SandboxError, match="no shared sandbox membership"):
        restarted.discard(discard, sandbox_id=spec.sandbox_id)

    assert not restarted._directory(discard).joinpath("discard.json").exists()
    assert manager.removals == 0


def test_complete_native_backup_keeps_container_until_verified_commit(archive_setup, monkeypatch):
    manager, supervisor, service, identity, spec, members = archive_setup
    receipt = service.prepare(identity)
    assert manager.exports == 1
    assert manager.removals == 0
    # The archive is uploaded from the export and the tar bytes around it,
    # kept private until the request ends.
    kept = [
        service._directory(identity) / name for name in ("archive.head", "container-rootfs.tar")
    ]
    assert [path.stat().st_mode & 0o777 for path in kept] == [0o600, 0o600]
    for action in (
        lambda: supervisor.operation_opened("thread-peer"),
        lambda: supervisor.terminal_opened("thread-origin"),
        lambda: supervisor.register_manifest(spec, members, expected_container_id="container-1"),
    ):
        with pytest.raises(SessionBusy):
            action()
    calls, uploaded = _upload(service, identity, receipt, monkeypatch)
    assert uploaded["uploaded"]
    assert hashlib.sha256(calls[0].content).hexdigest() == receipt["sha256"]
    assert len(calls[0].content) == receipt["size_bytes"]
    with tarfile.open(fileobj=io.BytesIO(calls[0].content)) as bundle:
        manifest = json.load(bundle.extractfile("manifest.json"))
        rootfs = bundle.extractfile("container-rootfs.tar").read()
    assert manifest["rootfs_sha256"] == hashlib.sha256(rootfs).hexdigest()
    assert manifest["credentials_tmpfs_excluded"] is True
    assert {row["native_session_id"] for row in manifest["members"]} == {
        "native-origin",
        "native-peer",
    }
    with tarfile.open(fileobj=io.BytesIO(rootfs)) as bundle:
        assert bundle.extractfile("workspace/repository/file.py").read() == b"retained changes"
        assert (
            bundle.extractfile("agent-state/claude/projects/native-session.json").read()
            == b"native session state"
        )
        assert not any(path.startswith("run/agent-members") for path in bundle.getnames())
    assert manager.removals == 0
    result = service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert result["deleted"]
    assert manager.removals == 1
    assert not supervisor.owns("thread-origin")
    assert not supervisor.owns("thread-peer")
    assert not any(path.exists() for path in kept)
    assert (service._directory(identity) / "deleted.json").exists()
    assert (
        service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
        == result
    )


def _whole_tar(manifest: bytes, rootfs) -> bytes:
    """The archive as ``tarfile`` writes it whole, as prepare once did to ``archive.tar``."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        info.mode = 0o600
        bundle.addfile(info, io.BytesIO(manifest))
        bundle.add(rootfs, arcname="container-rootfs.tar", recursive=False)
    return output.getvalue()


def test_the_archive_sent_in_pieces_is_byte_for_byte_the_whole_tar(tmp_path):
    manifest = b'{"format":"cloud-agent-sandbox-archive-v1"}'
    rootfs = tmp_path / "rootfs.partial"
    rootfs.write_bytes(b"")
    head_bytes = len(deletion_backup._archive_head(manifest, rootfs))
    # An export that ends the tar exactly on a record boundary, and others
    # around every block and record edge.
    exact = -(head_bytes + 2 * tarfile.BLOCKSIZE) % tarfile.RECORDSIZE
    sizes = {0, 1, 511, 512, 513, exact - 1, exact, exact + 1, tarfile.RECORDSIZE + 7}
    for size in sorted(sizes):
        rootfs.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
        head = deletion_backup._archive_head(manifest, rootfs)
        tail = deletion_backup._archive_tail(len(head), size)
        assert head + rootfs.read_bytes() + tail == _whole_tar(manifest, rootfs), size


def test_prepare_receipt_names_the_exact_archive_the_upload_sends(archive_setup, monkeypatch):
    _manager, _supervisor, service, identity, *_ = archive_setup
    receipt = service.prepare(identity)
    directory = service._directory(identity)

    calls, _ = _upload(service, identity, receipt, monkeypatch)

    with tarfile.open(fileobj=io.BytesIO(calls[0].content)) as bundle:
        manifest = bundle.extractfile("manifest.json").read()
    assert calls[0].content == _whole_tar(manifest, directory / "container-rootfs.tar")
    assert receipt["size_bytes"] == len(calls[0].content)
    assert receipt["sha256"] == hashlib.sha256(calls[0].content).hexdigest()


def test_the_receipt_follows_the_durable_archive_it_names(archive_setup, monkeypatch):
    _manager, _supervisor, service, identity, *_ = archive_setup
    events: list[str] = []
    fsync, link, rename = os.fsync, os.link, os.replace

    def recording_fsync(descriptor):
        events.append("fsync " + os.path.basename(os.readlink(f"/proc/self/fd/{descriptor}")))
        fsync(descriptor)

    def recording_link(source, target, *args, **kwargs):
        events.append("link " + os.path.basename(target))
        link(source, target, *args, **kwargs)

    def recording_rename(source, target, *args, **kwargs):
        events.append("rename " + os.path.basename(target))
        rename(source, target, *args, **kwargs)

    monkeypatch.setattr(deletion_backup.os, "fsync", recording_fsync)
    monkeypatch.setattr(deletion_backup.os, "link", recording_link)
    monkeypatch.setattr(deletion_backup.os, "replace", recording_rename)

    service.prepare(identity)

    receipt = events.index("link prepare.json")
    renamed = events.index("rename container-rootfs.tar")
    assert "fsync rootfs.partial" in events[:renamed]
    assert "fsync archive.head" in events[:receipt]
    # The new names are durable too before the receipt that relies on them.
    assert f"fsync {service._directory(identity).name}" in events[renamed:receipt]


def test_upload_leaves_the_checksum_to_s3_instead_of_hashing_again(archive_setup, monkeypatch):
    manager, _supervisor, service, identity, *_ = archive_setup
    receipt = service.prepare(identity)
    export = service._directory(identity) / "container-rootfs.tar"
    original = export.read_bytes()

    def s3(request: httpx.Request) -> httpx.Response:
        digest = base64.b64encode(hashlib.sha256(request.content).digest()).decode()
        return httpx.Response(200 if digest == request.headers["x-amz-checksum-sha256"] else 400)

    def unhashed(_path):
        raise AssertionError("the upload hashed the archive again")

    monkeypatch.setattr(service, "_digest", unhashed)
    # The same size, other bytes: only S3's check of the signed checksum
    # stands between them and the object.
    export.write_bytes(b"X" + original[1:])
    with monkeypatch.context() as patch, pytest.raises(SandboxError, match="rejected"):
        _upload(service, identity, receipt, patch, handler=s3)
    export.write_bytes(original + b"!")
    with monkeypatch.context() as patch, pytest.raises(SandboxError, match="changed on disk"):
        _upload(service, identity, receipt, patch, handler=s3)
    export.write_bytes(original)
    _calls, uploaded = _upload(service, identity, receipt, monkeypatch, handler=s3)
    assert uploaded["uploaded"]
    assert manager.removals == 0


def test_a_whole_archive_an_earlier_version_prepared_still_uploads(archive_setup, monkeypatch):
    _manager, _supervisor, service, identity, *_ = archive_setup
    receipt = service.prepare(identity)
    directory = service._directory(identity)
    head = directory / "archive.head"
    export = directory / "container-rootfs.tar"
    with tarfile.open(fileobj=io.BytesIO(head.read_bytes()), mode="r|") as bundle:
        manifest = bundle.extractfile(bundle.next()).read()
    whole = _whole_tar(manifest, export)
    (directory / "archive.tar").write_bytes(whole)
    head.unlink()
    export.unlink()

    calls, uploaded = _upload(service, identity, receipt, monkeypatch)

    assert uploaded["uploaded"]
    assert calls[0].content == whole
    service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert not (directory / "archive.tar").exists()


@pytest.mark.parametrize("change", ["running", "generation", "container"])
def test_prepare_rejects_running_or_stale_identity(archive_setup, change):
    manager, _supervisor, service, identity, *_ = archive_setup
    if change == "running":
        manager.current = replace(manager.current, running=True, status=ChatSandboxStatus.RUNNING)
    elif change == "generation":
        identity = replace(identity, generation=3)
    else:
        identity = replace(identity, expected_container_id="replacement")
    with pytest.raises(SandboxError):
        service.prepare(identity)
    assert manager.exports == manager.removals == 0


@pytest.mark.parametrize(
    "flag",
    [
        "initial_archive_preservation_pending",
        "stop_archive_pending",
        "stop_archive_blocked",
        "orphan_finalization_pending",
        "start_archive_completion_pending",
    ],
)
def test_deletion_preserves_lifecycle_archive_fences(archive_setup, monkeypatch, flag):
    manager, supervisor, service, identity, spec, _members = archive_setup
    group = supervisor._groups[spec.sandbox_id]
    setattr(group, flag, True)
    with pytest.raises(SessionBusy, match="lifecycle archive"):
        service.prepare(identity)
    assert manager.exports == 0
    setattr(group, flag, False)
    receipt = service.prepare(identity)
    _upload(service, identity, receipt, monkeypatch)
    # The lifecycle barrier is rechecked on receipt replay and commit, even
    # though the matching admin request already holds its own admission fence.
    setattr(group, flag, True)
    with pytest.raises(SessionBusy, match="lifecycle archive"):
        service.prepare(identity)
    with pytest.raises(SessionBusy, match="lifecycle archive"):
        service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert manager.removals == 0


def test_upload_failure_never_deletes_and_abort_releases_barrier(archive_setup, monkeypatch):
    manager, supervisor, service, identity, spec, members = archive_setup
    receipt = service.prepare(identity)
    with pytest.raises(SandboxError, match="rejected"):
        _upload(service, identity, receipt, monkeypatch, status=503)
    with pytest.raises(SessionLeaseLost, match="not been uploaded"):
        service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert manager.removals == 0
    aborted = service.abort(identity)
    assert aborted == {
        "request_id": identity.request_id,
        "container_id": identity.expected_container_id,
        "archive_key": identity.archive_key,
        "aborted": True,
    }
    # A control-plane crash or lost abort acknowledgment replays this exact
    # receipt before releasing its durable admission fence.
    assert service.abort(identity) == aborted
    supervisor.register_manifest(spec, members, expected_container_id="container-1")
    assert not (service._directory(identity) / "archive.tar").exists()
    assert not (service._directory(identity) / "archive.head").exists()
    assert not (service._directory(identity) / "container-rootfs.tar").exists()
    with pytest.raises(SessionLeaseLost, match="aborted"):
        service.prepare(identity)


def test_abort_before_prepare_also_returns_complete_identity_receipt(archive_setup):
    manager, supervisor, service, identity, spec, members = archive_setup
    expected = {
        "request_id": identity.request_id,
        "container_id": identity.expected_container_id,
        "archive_key": identity.archive_key,
        "aborted": True,
    }
    assert service.abort(identity) == expected
    assert service.abort(identity) == expected
    with pytest.raises(SessionLeaseLost, match="aborted"):
        service.prepare(identity)
    assert manager.exports == 0
    supervisor.register_manifest(spec, members, expected_container_id="container-1")


@pytest.mark.parametrize("change", ["running", "replacement", "checksum", "request", "key"])
def test_commit_refuses_changes_after_backup(archive_setup, monkeypatch, change):
    manager, _supervisor, service, identity, *_ = archive_setup
    receipt = service.prepare(identity)
    _upload(service, identity, receipt, monkeypatch)
    digest = receipt["sha256"]
    if change == "running":
        manager.current = replace(manager.current, running=True, status=ChatSandboxStatus.RUNNING)
    elif change == "replacement":
        manager.current = replace(manager.current, container_id="replacement")
    elif change == "checksum":
        digest = "0" * 64
    elif change == "request":
        identity = replace(identity, request_id="other-delete")
    else:
        identity = replace(identity, archive_key="other-key")
    with pytest.raises(SandboxError):
        service.commit(identity, sha256=digest, size_bytes=receipt["size_bytes"])
    assert manager.removals == 0


def test_oversized_backup_releases_barrier_without_upload_or_delete(archive_setup):
    manager, supervisor, service, identity, spec, members = archive_setup
    service._max_bytes = 1
    with pytest.raises(SandboxError, match="size limit"):
        service.prepare(identity)
    assert manager.removals == 0
    supervisor.register_manifest(spec, members, expected_container_id="container-1")
    assert not (service._directory(identity) / "archive.tar").exists()
    assert not list(service._directory(identity).iterdir())


def test_lost_upload_receipt_412_still_needs_verified_commit(archive_setup, monkeypatch):
    manager, _supervisor, service, identity, *_ = archive_setup
    receipt = service.prepare(identity)
    calls, result = _upload(service, identity, receipt, monkeypatch, status=412)
    assert result["uploaded"]
    assert manager.removals == 0
    assert len(calls) == 1


def test_durable_commit_receipt_recovers_crash_after_container_removal(archive_setup, monkeypatch):
    manager, _supervisor, service, identity, *_ = archive_setup
    receipt = service.prepare(identity)
    _upload(service, identity, receipt, monkeypatch)
    append = service._append

    def fail_after_removal(path, value):
        if path.name == "deleted.json":
            raise OSError("simulated crash after docker rm")
        append(path, value)

    monkeypatch.setattr(service, "_append", fail_after_removal)
    with pytest.raises(OSError, match="simulated crash"):
        service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert manager.current is None
    assert (service._directory(identity) / "commit.json").exists()
    restarted = SandboxDeletionBackupService(
        SharedChatSessionSupervisor(manager), state_dir=service._state_dir
    )
    result = restarted.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert result["deleted"]
    assert manager.removals == 1


@pytest.mark.asyncio
async def test_internal_archive_routes_require_host_sandbox_manager_auth(archive_setup):
    manager, supervisor, service, identity, *_ = archive_setup
    app = create_app(
        token="host-secret",
        backend=ProcessBackend(),
        shared_session_supervisor=supervisor,
    )
    app.state.sandbox_deletion_archive_service = service
    body = {
        "request_id": identity.request_id,
        "generation": identity.generation,
        "expected_container_id": identity.expected_container_id,
        "archive_key": identity.archive_key,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        response = await client.post(
            "/shared-sandboxes/thread-origin/admin-delete/prepare", json=body
        )
        assert response.status_code == 401
        response = await client.post(
            "/shared-sandboxes/thread-origin/admin-delete/prepare",
            headers={"x-agent-workspace-token": "host-secret"},
            json=body,
        )
        assert response.status_code == 200
        assert response.json()["container_id"] == "container-1"
    assert manager.removals == 0
