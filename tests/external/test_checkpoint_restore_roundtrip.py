"""A chat moved to another host starts from its checkpoint with its files and tools.

Real Docker throughout: the chat container, the Sandbox Manager's archive
markers inside it, the stop's final-archive receipt, the checkpoint's commit,
export and load, and the archive controller's start fence before the restored
container first runs. Only S3 is a local file. Unit tests stand in for Docker,
and a stand-in never commits its markers into an image: every restore on
staging failed on exactly that (2026-10-19) while the whole suite passed.

Run by hand with ``pytest -m external tests/external/test_checkpoint_restore_roundtrip.py``
(``AGENT_RESTORE_TEST_IMAGE`` names the base image, ``python:3.12-slim`` by
default; it must exist locally).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
import time

import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
from cloud_agent_host.chat_sandbox_models import CHAT_CHECKPOINT_LABEL
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.sandbox_archive_runtime import SandboxArchiveController
from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveReceipt
from cloud_agent_host.sandbox_checkpoints import (
    SandboxCheckpointCapture,
    SandboxCheckpointStore,
    checkpoint_image_reference,
)
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

pytestmark = pytest.mark.external
BASE = os.environ.get("AGENT_RESTORE_TEST_IMAGE", "python:3.12-slim")
NAMESPACE = "restore-roundtrip"
SANDBOX_ID = f"asbx_roundtrip{secrets.token_hex(4)}"


def _docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kwargs)


@pytest.fixture
def docker_ready() -> None:
    if shutil.which("docker") is None or _docker("image", "inspect", BASE, check=False).returncode:
        pytest.skip(f"needs Docker and the local image {BASE}")


@pytest.fixture
def cleanup():
    containers: list[str] = []
    images: list[str] = []
    yield containers, images
    for container in containers:
        _docker("rm", "--force", container, check=False)
    for image in images:
        _docker("image", "rm", "--force", image, check=False)


class _FinalUpload:
    """The final-archive upload a clean stop makes, recorded instead of sent."""

    def __init__(self, config) -> None:
        self.config = config

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def status(self) -> dict[str, object]:
        return {"enabled": True, "running": True, "pending": 0}

    def queue_final(self, spec, _inspection, _members) -> SandboxArchiveReceipt:
        return SandboxArchiveReceipt(
            phase="final",
            bucket="freeinference",
            key=(
                f"cloud-agent/sandbox/v1/{hashlib.sha256(NAMESPACE.encode()).hexdigest()}/"
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


def _policy() -> dict[str, object]:
    return SandboxArchivePolicy(
        enabled=True,
        region="us-east-1",
        kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc",
        expected_bucket_owner="123456789012",
        max_archive_bytes=2 * 1024**3,
        revision=1,
    ).as_dict()


def _spec(generation: int) -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_roundtrip",
        sandbox_id=SANDBOX_ID,
        generation=generation,
        runtime_id="claude-code",
        sandbox_image_digest=BASE,
        shared_live=True,
    )


def _create(spec: ChatSandboxSpec, image: str, *labels: str) -> None:
    flags = [f"--label={key}={value}" for key, value in spec.docker_labels().items()]
    flags += [f"--label={label}" for label in labels]
    _docker("create", "--name", spec.container_name, *flags, image, "sleep", "600")


def _run(container: str, script: str) -> str:
    return _docker("exec", container, "sh", "-c", script).stdout


def test_a_restored_chat_starts_on_its_new_host_with_its_files(docker_ready, cleanup, tmp_path):
    containers, images = cleanup
    manager = ChatSandboxManager(ContainerBackend(image=BASE, sandbox_namespace=NAMESPACE))
    controller = SandboxArchiveController(
        manager, namespace=NAMESPACE, service_factory=_FinalUpload
    )
    controller.start()
    controller.apply(_policy())
    capture = SandboxCheckpointCapture(
        docker_binary="docker",
        spool_dir=tmp_path / "spool",
        store=SandboxCheckpointStore(tmp_path / "receipts"),
    )

    # Generation 1 on the leaving host: work in and outside the workspace.
    first = _spec(1)
    containers.append(first.container_name)
    _create(first, BASE)
    _docker("start", first.container_name)
    _run(
        first.container_name,
        "mkdir -p /workspace && printf 'ABC123\\n' > /workspace/hello.txt "
        "&& chown 10001:10001 /workspace/hello.txt && chmod 640 /workspace/hello.txt "
        "&& printf '#!/bin/sh\\necho tool-ok\\n' > /usr/local/bin/roundtrip-tool "
        "&& chmod 755 /usr/local/bin/roundtrip-tool",
    )
    running = manager.inspect(first.container_name)
    assert running is not None and running.running

    # A clean stop: fence, stop, final archive, then the checkpoint, as the
    # Sandbox Manager does. Its markers are in the container when it commits.
    controller.prepare_final(first, running)
    _docker("stop", "--time", "1", first.container_name)
    stopped = manager.inspect(first.container_name)
    assert stopped is not None and not stopped.running
    controller.queue_final(first, stopped, ())
    marker = manager.read_final_archive_marker(first, expected_container_id=stopped.container_id)
    assert b'"state":"uploaded"' in marker

    stored = tmp_path / "checkpoint.oci.tar"

    def upload(*, sandbox_id, generation, checkpoint_id, body, size, sha256, deadline):
        with stored.open("wb") as out:
            shutil.copyfileobj(body, out)
        return SandboxArchiveReceipt(
            phase="checkpoint",
            bucket="bucket",
            key=f"cloud-agent/sandbox/v2/{sandbox_id}/g{generation}/{checkpoint_id}.oci.tar",
            size=size,
            sha256=sha256,
            etag="etag",
            version_id="v1",
            s3_checksum_sha256=None,
            multipart=False,
        )

    receipt = capture.capture(
        first,
        stopped,
        (),
        upload=upload,
        max_bytes=2 * 1024**3,
        deadline=time.monotonic() + 600,
    )
    reference = checkpoint_image_reference(SANDBOX_ID, receipt.checkpoint_id)
    images.append(reference)

    # The new host has only the object: neither the container nor its image.
    _docker("rm", "--force", first.container_name)
    _docker("image", "rm", "--force", reference)
    # The download and the checks before ``docker load``, as ``restore`` runs
    # them; its receipt would demand a registry digest this local base lacks.
    loaded = capture._load(
        reference,
        BASE,
        lambda output: output.write(stored.read_bytes()),
        max_bytes=2 * 1024**3,
        deadline=time.monotonic() + 600,
    )
    assert loaded == reference

    # Generation 2, created stopped from the checkpoint, carries generation
    # 1's marker. Its first start is fenced like any other start.
    second = _spec(2)
    containers.append(second.container_name)
    _create(second, reference, f"{CHAT_CHECKPOINT_LABEL}={receipt.checkpoint_id}")
    created = manager.inspect(second.container_name)
    assert created is not None and not created.running
    inherited = manager.read_stopped_final_archive_marker_optional(
        second, expected_container_id=created.container_id
    )
    assert inherited == marker

    controller.prepare_start(second, created)
    _docker("start", second.container_name)
    restarted = manager.inspect(second.container_name)
    assert restarted is not None and restarted.running
    controller.complete_start(second, restarted)

    # The container's own lifecycle: no marker left once it runs.
    assert (
        manager.read_running_final_archive_marker(
            second, expected_container_id=restarted.container_id
        )
        is None
    )
    assert _run(second.container_name, "cat /workspace/hello.txt") == "ABC123\n"
    assert (
        _run(second.container_name, "stat -c '%u:%g %a' /workspace/hello.txt")
        == "10001:10001 640\n"
    )
    assert _run(second.container_name, "roundtrip-tool") == "tool-ok\n"
    controller.close()


def test_a_marker_of_another_group_still_refuses_the_restored_start(docker_ready, cleanup):
    """Only the group's own earlier generation passes: a foreign marker fails closed."""
    containers, _images = cleanup
    manager = ChatSandboxManager(ContainerBackend(image=BASE, sandbox_namespace=NAMESPACE))
    controller = SandboxArchiveController(
        manager, namespace=NAMESPACE, service_factory=_FinalUpload
    )
    controller.start()
    controller.apply(_policy())
    other = ChatSandboxSpec(
        thread_id="athr_other",
        sandbox_id=f"asbx_other{secrets.token_hex(4)}",
        generation=1,
        runtime_id="claude-code",
        sandbox_image_digest=BASE,
        shared_live=True,
    )
    containers.append(other.container_name)
    _create(other, BASE)
    _docker("start", other.container_name)
    foreign = manager.inspect(other.container_name)
    assert foreign is not None
    controller.prepare_final(other, foreign)
    payload = manager.read_running_final_archive_marker(
        other, expected_container_id=foreign.container_id
    )
    assert payload is not None

    target = _spec(2)
    containers.append(target.container_name)
    _create(target, BASE)
    created = manager.inspect(target.container_name)
    assert created is not None
    manager.write_stopped_final_archive_marker(
        target, payload, expected_container_id=created.container_id
    )

    with pytest.raises(SandboxError, match="does not match the sandbox"):
        controller.prepare_start(target, created)
    controller.close()
