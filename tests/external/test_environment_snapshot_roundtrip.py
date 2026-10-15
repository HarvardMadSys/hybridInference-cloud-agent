"""An installation snapshot survives commit, export, upload, download, checks and load.

Real Docker throughout; only S3 is a local file. The base image is small so a
run takes seconds: the mechanics do not depend on which image it is.

Run by hand with ``pytest -m external tests/external/test_environment_snapshot_roundtrip.py``
(``SNAPSHOT_TEST_BASE_IMAGE`` overrides the base; it must exist locally).
"""

from __future__ import annotations

import io
import os
import pathlib
import secrets
import shutil
import subprocess
import tarfile
import time
from types import SimpleNamespace

import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveReceipt
from cloud_agent_host.sandbox_checkpoints import (
    CheckpointArchiveInvalid,
    SandboxCheckpointCapture,
    SandboxCheckpointStore,
)
from cloud_agent_shared.environment_snapshots import (
    owner_hash,
    snapshot_image_reference,
    snapshot_object_key,
)

pytestmark = pytest.mark.external
BASE = os.environ.get("SNAPSHOT_TEST_BASE_IMAGE", "python:3.12-slim")
OWNER = owner_hash("snapshot-roundtrip")


def _docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kwargs)


@pytest.fixture
def docker_ready():
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


def _installed_container(containers: list[str]) -> str:
    """A stopped container as a test leaves it: an installation, no repository."""
    name = f"snapshot-roundtrip-{secrets.token_hex(4)}"
    containers.append(name)
    # The label a test container carries, which its snapshot image inherits.
    _docker(
        "create",
        "--name",
        name,
        "--label",
        "hybridinference.environment-test=run-1",
        BASE,
        "sleep",
        "600",
    )
    _docker("start", name)
    _docker(
        "exec",
        name,
        "sh",
        "-c",
        "mkdir -p /workspace/node_modules/left-pad /workspace/.cloud-agent-tools/npm "
        "&& echo installed > /workspace/node_modules/left-pad/index.js "
        "&& echo cached > /workspace/.cloud-agent-tools/npm/blob",
    )
    _docker("stop", "--time", "1", name)
    return name


def _capture() -> SandboxCheckpointCapture:
    spool = pathlib.Path(f"/tmp/snapshot-roundtrip-{secrets.token_hex(4)}")
    return SandboxCheckpointCapture(
        docker_binary="docker", spool_dir=spool, store=SandboxCheckpointStore(spool / "receipts")
    )


def test_a_snapshot_is_saved_loaded_and_extended_with_new_source(docker_ready, cleanup, tmp_path):
    containers, images = cleanup
    name = _installed_container(containers)
    container_id = _docker("inspect", "--format", "{{.Id}}", name).stdout.strip()
    snapshot_id = secrets.token_hex(16)
    reference = snapshot_image_reference(OWNER, snapshot_id)
    images.append(reference)
    stored = tmp_path / "object.oci.tar"

    def upload(*, owner, snapshot_id, body, size, sha256, deadline):
        with stored.open("wb") as out:
            shutil.copyfileobj(body, out)
        return SandboxArchiveReceipt(
            phase="checkpoint",
            bucket="bucket",
            key=snapshot_object_key(owner, snapshot_id),
            size=size,
            sha256=sha256,
            etag="etag",
            version_id="v1",
            s3_checksum_sha256=None,
            multipart=False,
        )

    capture = _capture()
    spec = ChatSandboxSpec(
        thread_id="athr_env_run-1",
        sandbox_id="aenv_run-1",
        generation=1,
        runtime_id="claude-code",
        sandbox_image_digest=BASE,
    )
    receipt = capture.capture_environment(
        spec,
        SimpleNamespace(container_id=container_id, running=False, image=BASE),
        owner=OWNER,
        snapshot_id=snapshot_id,
        upload=upload,
        max_bytes=2 * 1024**3,
        deadline=time.monotonic() + 600,
    )
    assert receipt["object_key"] == snapshot_object_key(OWNER, snapshot_id)
    assert receipt["size_bytes"] == stored.stat().st_size
    # The building host keeps the image for its next workspace.
    assert _docker("image", "inspect", reference, check=False).returncode == 0
    labels = _docker("image", "inspect", "--format", "{{json .Config.Labels}}", reference).stdout
    assert "hybridinference.environment-test" in labels

    # Another host has only the object.
    _docker("image", "rm", "--force", reference)
    loaded = capture._load(
        reference,
        BASE,
        lambda output: output.write(stored.read_bytes()),
        max_bytes=2 * 1024**3,
        deadline=time.monotonic() + 600,
    )
    assert loaded == reference

    workspace = f"snapshot-roundtrip-{secrets.token_hex(4)}"
    containers.append(workspace)
    _docker("create", "--name", workspace, reference, "sleep", "600")
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w") as archive:
        body = b"print('task source')\n"
        entry = tarfile.TarInfo("workspace/app.py")
        entry.size = len(body)
        archive.addfile(entry, io.BytesIO(body))
    subprocess.run(["docker", "cp", "-", f"{workspace}:/"], input=source.getvalue(), check=True)
    _docker("start", workspace)
    listing = _docker(
        "exec",
        workspace,
        "sh",
        "-c",
        "cat /workspace/node_modules/left-pad/index.js /workspace/.cloud-agent-tools/npm/blob "
        "/workspace/app.py",
    ).stdout
    assert listing.split() == ["installed", "cached", "print('task", "source')"]


def test_a_tampered_object_never_reaches_docker_load(docker_ready, cleanup, tmp_path):
    containers, images = cleanup
    name = _installed_container(containers)
    container_id = _docker("inspect", "--format", "{{.Id}}", name).stdout.strip()
    snapshot_id = secrets.token_hex(16)
    reference = snapshot_image_reference(OWNER, snapshot_id)
    images.append(reference)
    stored = tmp_path / "object.oci.tar"

    def upload(*, owner, snapshot_id, body, size, sha256, deadline):
        stored.write_bytes(body.read())
        return SandboxArchiveReceipt(
            phase="checkpoint",
            bucket="bucket",
            key=snapshot_object_key(owner, snapshot_id),
            size=size,
            sha256=sha256,
            etag="etag",
            version_id="v1",
            s3_checksum_sha256=None,
            multipart=False,
        )

    capture = _capture()
    spec = ChatSandboxSpec(
        thread_id="athr_env_run-2",
        sandbox_id="aenv_run-2",
        generation=1,
        runtime_id="claude-code",
        sandbox_image_digest=BASE,
    )
    capture.capture_environment(
        spec,
        SimpleNamespace(container_id=container_id, running=False, image=BASE),
        owner=OWNER,
        snapshot_id=snapshot_id,
        upload=upload,
        max_bytes=2 * 1024**3,
        deadline=time.monotonic() + 600,
    )
    _docker("image", "rm", "--force", reference)
    tampered = bytearray(stored.read_bytes())
    # Flip a byte deep inside the archive: inside a layer blob, not a header.
    tampered[len(tampered) // 2] ^= 0xFF
    with pytest.raises(CheckpointArchiveInvalid):
        capture._load(
            reference,
            BASE,
            lambda output: output.write(bytes(tampered)),
            max_bytes=2 * 1024**3,
            deadline=time.monotonic() + 600,
        )
    assert _docker("image", "inspect", reference, check=False).returncode != 0
