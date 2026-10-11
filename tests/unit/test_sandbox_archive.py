"""Archival integrity, admission barriers, and crash-safe deletion receipts."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from cloud_agent_host.chat_sandbox import (
    ChatSandboxInspection,
    ChatSandboxSpec,
    ChatSandboxStatus,
)
from cloud_agent_host.sandbox import ProcessBackend, SandboxError
from cloud_agent_host.sandbox_archive import ArchiveIdentity, SandboxArchiveService
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import (
    SharedChatSessionSupervisor,
    SharedMemberManifest,
)
from cloud_agent_host.workspace_broker import create_app


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
    service = SandboxArchiveService(supervisor, state_dir=tmp_path)
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


def test_complete_native_backup_keeps_container_until_verified_commit(archive_setup, monkeypatch):
    manager, supervisor, service, identity, spec, members = archive_setup
    receipt = service.prepare(identity)
    assert manager.exports == 1
    assert manager.removals == 0
    archive = service._directory(identity) / "archive.tar"
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive) as bundle:
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
    assert manager.removals == 0
    result = service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert result["deleted"]
    assert manager.removals == 1
    assert not supervisor.owns("thread-origin")
    assert not supervisor.owns("thread-peer")
    assert not archive.exists()
    assert (service._directory(identity) / "deleted.json").exists()
    assert (
        service.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
        == result
    )


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
    restarted = SandboxArchiveService(
        SharedChatSessionSupervisor(manager), state_dir=service._state_dir
    )
    result = restarted.commit(identity, sha256=receipt["sha256"], size_bytes=receipt["size_bytes"])
    assert result["deleted"]
    assert manager.removals == 1


@pytest.mark.asyncio
async def test_internal_archive_routes_require_host_broker_auth(archive_setup):
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
        transport=httpx.ASGITransport(app=app), base_url="http://broker"
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
