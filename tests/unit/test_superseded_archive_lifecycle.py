"""A left-behind container's removal, through the archive lifecycle that stopped it.

Release asks a Host's Sandbox Manager for its containers, then discards the
stopped ones the control plane proved superseded (``_remove_superseded``).
These tests run that exchange against the real Sandbox Manager app, the real
shared supervisor, archive controller and service, checkpoint capture, and
deletion receipts. Only Docker and S3 are stand-ins: the supervisor fakes'
container and marker store, the archive tests' uploader writing to a local
object map, and the checkpoint tests' Docker for ``commit`` and ``save``.

Without archives the supervisor clears every lifecycle fence at stop, so a
development stack without S3 never reaches the states these tests drive. With
them, a clean stop leaves nothing for removal to wait for, and a network
cleanup that fails after the final archive and checkpoint are stored keeps the
group fenced; every removal is then refused with the same reason, and nothing
is deleted, until the cleanup succeeds. That is a characterization of a
fail-closed state the code can reach, not proof of what a Host refused in an
earlier incident.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx
import pytest
from tests.unit.test_sandbox_archives import _Uploader
from tests.unit.test_sandbox_checkpoints import BASE, NEW, _FakeDocker, _Image
from tests.unit.test_shared_session_supervisor import _FinalMarkerManager, _members

from cloud_agent.sandbox_manager_client import SandboxManagerClient, SandboxManagerError
from cloud_agent.storage._sql.hosts import superseded_removal_request_id
from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec, ChatSandboxStatus
from cloud_agent_host.sandbox import ProcessBackend, SandboxError
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.sandbox_archive_s3 import (
    SandboxArchiveReceipt,
    SandboxArchiveS3Config,
    SandboxArchiveS3Uploader,
    checkpoint_object_key,
    lifecycle_object_key,
)
from cloud_agent_host.sandbox_archives import SandboxArchiveService
from cloud_agent_host.sandbox_checkpoints import SandboxCheckpointCapture, SandboxCheckpointStore
from cloud_agent_host.sandbox_deletion_backup import SandboxDeletionBackupService
from cloud_agent_host.sandbox_manager import create_app
from cloud_agent_host.session_lifecycle import SessionLeaseLost
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

_TOKEN = "host-secret"
_HOST = "runner-a"
_NAMESPACE = "pool-east"
_BRIDGE = "agent-net"
_REFUSED = "sandbox lifecycle archive must finish before deletion"
_POLICY = SandboxArchivePolicy(
    enabled=True,
    region="us-east-1",
    kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc",
    expected_bucket_owner="123456789012",
    revision=1,
)


def _spec() -> ChatSandboxSpec:
    # The checkpoint Docker stand-in serves this base image.
    return ChatSandboxSpec(
        thread_id="athr_source",
        sandbox_id="asbx_shared",
        generation=1,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:base",
        resident_slot_capacity=4,
    )


class _Docker(_FinalMarkerManager):
    """Containers, their final marker, and their group bridge, as Docker keeps them.

    A start attaches the group bridge and a stop releases it, as
    ``ChatSandboxManager.stop`` does. ``release_outcomes`` scripts the next
    releases: an exception fails one, ``"hold"`` waits for ``release_gate``
    and then succeeds; with none left a release succeeds.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release_outcomes: list[BaseException | str] = []
        self.release_entered = threading.Event()
        self.release_gate = threading.Event()
        self.releases = 0
        self.removals = 0
        self.exports: list[str] = []

    def start(self, spec: ChatSandboxSpec, **kwargs: Any):
        started = super().start(spec, **kwargs)
        attached = replace(started, networks=(_BRIDGE,))
        self.items[spec.container_name] = attached
        return attached

    def stop(
        self,
        spec: ChatSandboxSpec,
        *,
        expected_container_id: str | None = None,
        release_networks: bool = True,
    ):
        current = self.items[spec.container_name]
        if expected_container_id is not None:
            assert current.container_id == expected_container_id
        if current.running:
            self.stop_count += 1
            current = replace(current, running=False, status=ChatSandboxStatus.STOPPED)
            self.items[spec.container_name] = current
        if not release_networks:
            return current
        return self.release_stopped_networks(spec, expected_container_id=current.container_id)

    def release_stopped_networks(self, spec: ChatSandboxSpec, *, expected_container_id: str):
        self.releases += 1
        outcome = self.release_outcomes.pop(0) if self.release_outcomes else None
        if outcome == "hold":
            self.release_entered.set()
            assert self.release_gate.wait(timeout=10)
        elif isinstance(outcome, BaseException):
            raise outcome
        return super().release_stopped_networks(spec, expected_container_id=expected_container_id)

    def export_archive(self, spec, path, destination, *, max_bytes, **_kwargs: Any) -> int:
        assert self.items[spec.container_name].running is False
        if path == "/home/agent/.freeinference-platform":
            raise FileNotFoundError(path)
        self.exports.append(path)
        payload = f"{path} as stopped".encode()
        assert len(payload) <= max_bytes
        return destination.write(payload)

    def require_stopped_archive(self, spec: ChatSandboxSpec, *, container_id: str):
        current = self.items.get(spec.container_name)
        if current is None or current.container_id != container_id:
            raise SessionLeaseLost("container identity changed")
        if current.running:
            raise SandboxError("sandbox archive requires a stopped container")
        return current

    def remove_stopped_archived(self, spec: ChatSandboxSpec, *, container_id: str) -> None:
        self.require_stopped_archive(spec, container_id=container_id)
        self.removals += 1
        del self.items[spec.container_name]


class _Bucket:
    """The archive bucket: every object any uploader stored, by key."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def uploader(self, config: SandboxArchiveS3Config) -> _BucketUploader:
        return _BucketUploader(config, self)

    def keys(self, phase: str) -> list[str]:
        suffix = {"final": "/final.tar.gz", "checkpoint": ".oci.tar"}[phase]
        return sorted(key for key in self.objects if key.endswith(suffix))


class _BucketUploader(_Uploader):
    """The archive tests' uploader, storing under the keys S3 would name."""

    def __init__(self, config: SandboxArchiveS3Config, bucket: _Bucket) -> None:
        super().__init__(config)
        self.bucket = bucket

    def upload(self, **kwargs: Any) -> SandboxArchiveReceipt:
        receipt = super().upload(**kwargs)
        key = lifecycle_object_key(
            namespace_sha256=hashlib.sha256(kwargs["namespace"].encode()).hexdigest(),
            sandbox_sha256=hashlib.sha256(kwargs["sandbox_id"].encode()).hexdigest(),
            generation=kwargs["generation"],
            phase=kwargs["phase"],
        )
        self.bucket.objects[key] = self.calls[-1]["payload"]
        return replace(receipt, key=key)

    def upload_checkpoint(self, **kwargs: Any) -> SandboxArchiveReceipt:
        body = kwargs["body"].read()
        assert len(body) == kwargs["size"]
        assert hashlib.sha256(body).hexdigest() == kwargs["sha256"]
        key = checkpoint_object_key(
            sandbox_id=kwargs["sandbox_id"],
            generation=kwargs["generation"],
            checkpoint_id=kwargs["checkpoint_id"],
        )
        self.bucket.objects[key] = body
        return SandboxArchiveReceipt(
            phase="checkpoint",
            bucket=self.config.bucket,
            key=key,
            size=len(body),
            sha256=kwargs["sha256"],
            etag='"etag"',
            version_id="version-1",
            s3_checksum_sha256=None,
            multipart=False,
        )


@dataclass
class _Host:
    """One Sandbox Manager process: its supervisor, archives, and app."""

    supervisor: SharedChatSessionSupervisor
    controller: SandboxArchiveController
    checkpoints: SandboxCheckpointStore
    app: Any
    now: list[float]

    def drain_recovery(self) -> None:
        """Wait for the recoveries listings queued; later listings queue again."""
        self.supervisor._recovery.quiesce()
        self.supervisor._recovery.start()

    def close(self) -> None:
        self.supervisor.close()
        self.controller.close()


def _host(tmp_path, docker: _Docker, bucket: _Bucket) -> _Host:
    """Wire one Sandbox Manager as ``create_app`` does for a dynamic archive policy."""
    store = SandboxCheckpointStore(tmp_path / "archives" / "checkpoints")
    image_docker = _FakeDocker(_Image(list(BASE)), _Image([*BASE, NEW]))
    capture = SandboxCheckpointCapture(
        docker_binary="docker",
        spool_dir=tmp_path / "archives" / "checkpoint-spool",
        store=store,
        run=image_docker.run,
        popen=image_docker.popen,
    )
    manager = cast("ChatSandboxManager", docker)

    def service(config: SandboxArchiveS3Config) -> SandboxArchiveService:
        return SandboxArchiveService(
            manager,
            namespace=_NAMESPACE,
            uploader=cast("SandboxArchiveS3Uploader", bucket.uploader(config)),
            config=config,
            retry_delay_s=0,
            sleep=lambda _delay: None,
            checkpoint_capture=capture,
        )

    controller = SandboxArchiveController(
        manager, namespace=_NAMESPACE, service_factory=service, checkpoint_capture=capture
    )
    controller.start()
    now = [1_000.0]
    supervisor = SharedChatSessionSupervisor(
        manager,
        idle_timeout_s=60,
        # The test sweeps itself; the background sweep never comes round.
        sweep_interval_s=3_600,
        clock=lambda: now[0],
        stopped_archive_prepare_hook=controller.prepare_final,
        stopped_archive_hook=controller.queue_final,
    )
    capture.start_seq_for = supervisor.start_seq_for
    supervisor.checkpoint_seq_for = capture.receipt_start_seq
    app = create_app(
        token=_TOKEN,
        backend=ProcessBackend(),
        shared_session_supervisor=supervisor,
        sandbox_archive_controller=controller,
    )
    app.state.sandbox_checkpoint_store = store
    app.state.sandbox_deletion_archive_service = SandboxDeletionBackupService(
        supervisor, state_dir=tmp_path / "archives"
    )
    return _Host(supervisor, controller, store, app, now)


async def _deliver_policy(http: httpx.AsyncClient) -> None:
    """The runner's policy delivery, which also starts the supervisor."""
    response = await http.post(
        "/sandbox-archive-settings",
        headers={"X-Agent-Workspace-Token": _TOKEN},
        json={"settings": _POLICY.as_dict()},
    )
    assert response.status_code == 200, response.text


def _idle_stop_after_files(host: _Host, spec: ChatSandboxSpec) -> None:
    """Files opens the stopped group and closes; the idle sweep then stops it."""
    host.supervisor.register_manifest(spec, _members(), expected_container_id="container-shared")
    token, _ = host.supervisor.operation_opened(spec.thread_id, start_seq=7)
    host.supervisor.operation_closed(spec.thread_id, token)
    host.now[0] += 61
    host.supervisor.sweep()


async def _release_pass(client: SandboxManagerClient) -> dict[str, Any]:
    """One pass of ``_remove_superseded``: list, then discard each left-behind container."""
    (row,) = [row for row in await client.list_sandboxes() if row["running"] is False]
    return await _discard(client, row)


async def _discard(client: SandboxManagerClient, row: dict[str, Any]) -> dict[str, Any]:
    return await client.discard_sandbox_deletion(
        row["origin_thread_id"],
        {
            "request_id": superseded_removal_request_id(_HOST, row),
            "generation": row["generation"],
            "expected_container_id": row["container_id"],
            "sandbox_id": row["sandbox_id"],
        },
    )


def _client(http: httpx.AsyncClient) -> SandboxManagerClient:
    return SandboxManagerClient("http://sandbox-manager", _TOKEN, client=http)


def _http(host: _Host) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="http://sandbox-manager"
    )


def _stopped_docker(spec: ChatSandboxSpec) -> _Docker:
    """A group created earlier and cleanly stopped: no bridge, no final marker."""
    docker = _Docker()
    docker.items[spec.container_name] = replace(
        docker._inspection(spec, running=False), networks=()
    )
    return docker


def _assert_archived_once(docker: _Docker, bucket: _Bucket, host: _Host, spec) -> None:
    """The stop stored one final archive and one checkpoint, of this exact container."""
    assert len(bucket.keys("final")) == 1
    assert len(bucket.keys("checkpoint")) == 1
    assert docker.exports == ["/workspace", "/agent-state"]
    receipt = host.checkpoints.latest(spec.sandbox_id)
    assert receipt is not None
    assert receipt.container_id == "container-shared"
    assert receipt.generation == spec.generation


def _discard_receipts(tmp_path) -> list[str]:
    return sorted(path.name for path in (tmp_path / "archives").glob("*/discard*.json"))


@pytest.mark.asyncio
async def test_a_cleanly_stopped_and_checkpointed_group_is_removed_in_one_pass(tmp_path):
    spec = _spec()
    docker = _stopped_docker(spec)
    bucket = _Bucket()
    host = _host(tmp_path, docker, bucket)
    try:
        async with _http(host) as http:
            await _deliver_policy(http)
            _idle_stop_after_files(host, spec)

            current = docker.items[spec.container_name]
            assert current.running is False
            assert current.networks == ()
            _assert_archived_once(docker, bucket, host, spec)
            (row,) = await _client(http).list_sandboxes()
            assert row["checkpoint"]["container_id"] == "container-shared"

            receipt = await _release_pass(_client(http))

        assert receipt["deleted"] is True
        assert receipt["container_id"] == "container-shared"
        assert docker.removals == 1
        assert spec.container_name not in docker.items
        assert not host.supervisor.owns(spec.thread_id)
        assert _discard_receipts(tmp_path) == ["discard.json", "discarded.json"]
    finally:
        host.close()


@pytest.mark.asyncio
async def test_a_failed_bridge_release_after_the_checkpoint_is_retried_until_removal_succeeds(
    tmp_path,
):
    spec = _spec()
    docker = _stopped_docker(spec)
    bucket = _Bucket()
    host = _host(tmp_path, docker, bucket)
    docker.release_outcomes = [SandboxError("bridge release failed"), "hold"]
    try:
        async with _http(host) as http:
            await _deliver_policy(http)
            # The sweep logs the failed stop; the archive and checkpoint are stored.
            _idle_stop_after_files(host, spec)
            _assert_archived_once(docker, bucket, host, spec)
            assert docker.items[spec.container_name].networks == (_BRIDGE,)

            # Wait until the listing's retry holds the bridge cleanup, then
            # issue discard. Thread scheduling must not decide this assertion.
            client = _client(http)
            (row,) = await client.list_sandboxes()
            assert await asyncio.to_thread(docker.release_entered.wait, 5)
            with pytest.raises(SandboxManagerError) as refused:
                await _discard(client, row)
            assert (refused.value.status_code, refused.value.message) == (409, _REFUSED)
            assert docker.removals == 0
            assert _discard_receipts(tmp_path) == []

            docker.release_gate.set()
            host.drain_recovery()
            assert docker.items[spec.container_name].networks == ()

            receipt = await _release_pass(_client(http))

        assert receipt["deleted"] is True
        assert docker.removals == 1
        # The retry finished the stop it had; nothing was archived again.
        assert docker.releases == 2
        _assert_archived_once(docker, bucket, host, spec)
    finally:
        docker.release_gate.set()
        host.close()


@pytest.mark.asyncio
async def test_a_bridge_release_that_keeps_failing_refuses_every_removal_and_deletes_nothing(
    tmp_path,
):
    spec = _spec()
    docker = _stopped_docker(spec)
    bucket = _Bucket()
    host = _host(tmp_path, docker, bucket)
    failure = SandboxError("bridge release failed")
    docker.release_outcomes = [failure] * 4
    try:
        async with _http(host) as http:
            await _deliver_policy(http)
            _idle_stop_after_files(host, spec)

            refusals = []
            for _ in range(3):
                with pytest.raises(SandboxManagerError) as refused:
                    await _release_pass(_client(http))
                refusals.append((refused.value.status_code, refused.value.message))
                host.drain_recovery()

        assert refusals == [(409, _REFUSED)] * 3
        # The stop's own release and one retry per listing, all failed.
        assert docker.releases == 4
        assert docker.removals == 0
        assert docker.items[spec.container_name].networks == (_BRIDGE,)
        assert _discard_receipts(tmp_path) == []
        _assert_archived_once(docker, bucket, host, spec)
    finally:
        host.close()


@pytest.mark.parametrize("still_failing", [False, True], ids=["cleared", "still_failing"])
@pytest.mark.asyncio
async def test_a_restarted_sandbox_manager_finishes_or_keeps_refusing_by_its_bridge(
    tmp_path, still_failing: bool
):
    """A restart finds the stopped container with its bridge and uploaded marker."""
    spec = _spec()
    docker = _stopped_docker(spec)
    bucket = _Bucket()
    first = _host(tmp_path, docker, bucket)
    docker.release_outcomes = [SandboxError("bridge release failed")]
    try:
        async with _http(first) as http:
            await _deliver_policy(http)
            _idle_stop_after_files(first, spec)
    finally:
        first.close()
    assert docker.items[spec.container_name].networks == (_BRIDGE,)
    if still_failing:
        docker.release_outcomes = [SandboxError("bridge release failed")] * 3

    restarted = _host(tmp_path, docker, bucket)
    try:
        async with _http(restarted) as http:
            # Startup reconciliation runs before the first request is admitted.
            await _deliver_policy(http)
            if still_failing:
                with pytest.raises(SandboxManagerError) as refused:
                    await _release_pass(_client(http))
                assert (refused.value.status_code, refused.value.message) == (409, _REFUSED)
                restarted.drain_recovery()
                assert docker.removals == 0
                assert _discard_receipts(tmp_path) == []
            else:
                receipt = await _release_pass(_client(http))
                assert receipt["deleted"] is True
                assert docker.removals == 1
        _assert_archived_once(docker, bucket, restarted, spec)
    finally:
        restarted.close()
