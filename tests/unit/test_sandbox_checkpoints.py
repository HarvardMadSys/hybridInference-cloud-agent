"""Restorable checkpoints: export filtering, receipt storage, and capture."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveReceipt
from cloud_agent_host.sandbox_checkpoints import (
    CHECKPOINT_IMAGE_REPOSITORY,
    CheckpointContentUnavailable,
    ContainerdContentStore,
    SandboxCheckpointCapture,
    SandboxCheckpointError,
    SandboxCheckpointReceipt,
    SandboxCheckpointStore,
    _image_names,
    export_checkpoint_archive,
    export_from_content_store,
    read_base_layers,
)

_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_INDEX = "application/vnd.oci.image.index.v1+json"


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _descriptor(body: bytes, media_type: str = "application/octet-stream") -> dict[str, Any]:
    return {"mediaType": media_type, "digest": f"sha256:{_digest(body)}", "size": len(body)}


@dataclass
class _Image:
    """An OCI ``docker save`` stream built from layer bodies."""

    layers: list[bytes]
    extra_entries: list[tuple[tarfile.TarInfo, bytes | None]] | None = None
    nested_index: bool = False

    def manifest(self) -> bytes:
        return json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": _MANIFEST,
                "config": _descriptor(self.config(), "application/vnd.oci.image.config.v1+json"),
                "layers": [_descriptor(layer) for layer in self.layers],
            },
            sort_keys=True,
        ).encode()

    def config(self) -> bytes:
        return json.dumps(
            {"rootfs": {"type": "layers", "diff_ids": [_digest(x) for x in self.layers]}}
        ).encode()

    def stream(self, *, blobs: list[bytes] | None = None) -> io.BytesIO:
        manifest = self.manifest()
        index_target = manifest
        documents: list[bytes] = [manifest, self.config(), *self.layers]
        if self.nested_index:
            nested = json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": _INDEX,
                    "manifests": [_descriptor(manifest, _MANIFEST)],
                }
            ).encode()
            documents.append(nested)
            index_target = nested
        index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": _INDEX,
                "manifests": [
                    _descriptor(index_target, _INDEX if self.nested_index else _MANIFEST)
                ],
            }
        ).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for directory in ("blobs", "blobs/sha256"):
                entry = tarfile.TarInfo(directory)
                entry.type = tarfile.DIRTYPE
                archive.addfile(entry)
            # docker save orders blobs by digest, so index.json comes last.
            for body in sorted(blobs if blobs is not None else documents, key=_digest):
                entry = tarfile.TarInfo(f"blobs/sha256/{_digest(body)}")
                entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
            for name, body in (
                ("index.json", index),
                ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
            for entry, body in self.extra_entries or []:
                archive.addfile(entry, None if body is None else io.BytesIO(body))
        output.seek(0)
        return output


BASE = [b"base-layer-one" * 50, b"base-layer-two" * 50]
NEW = b"the sandbox's own changes" * 200


def _names(archive: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {member.name for member in tar.getmembers()}


def test_export_keeps_only_the_new_layer_and_the_image_description() -> None:
    image = _Image([*BASE, NEW])
    output = io.BytesIO()

    archive = export_checkpoint_archive(
        image.stream(),
        output,
        base_layers=frozenset(_digest(layer) for layer in BASE),
        max_bytes=10**6,
    )

    assert archive.new_layers == (_digest(NEW),)
    assert archive.base_layers == tuple(_digest(layer) for layer in BASE)
    assert archive.new_layer_bytes == len(NEW)
    assert archive.manifest_digest == _digest(image.manifest())
    assert archive.config_digest == _digest(image.config())
    names = _names(output.getvalue())
    assert f"blobs/sha256/{_digest(NEW)}" in names
    assert not {f"blobs/sha256/{_digest(layer)}" for layer in BASE} & names
    assert {"index.json", "oci-layout", f"blobs/sha256/{_digest(image.manifest())}"} <= names


def test_export_resolves_a_nested_index() -> None:
    image = _Image([*BASE, NEW], nested_index=True)

    archive = export_checkpoint_archive(
        image.stream(),
        io.BytesIO(),
        base_layers=frozenset(_digest(layer) for layer in BASE),
        max_bytes=10**6,
    )

    assert archive.new_layers == (_digest(NEW),)


def test_export_refuses_a_blob_that_does_not_match_its_digest() -> None:
    image = _Image([*BASE, NEW])
    forged = io.BytesIO()
    with (
        tarfile.open(fileobj=image.stream(), mode="r") as source,
        tarfile.open(fileobj=forged, mode="w") as target,
    ):
        for member in source.getmembers():
            handle = source.extractfile(member)
            body = handle.read() if handle is not None else None
            if member.name == f"blobs/sha256/{_digest(NEW)}":
                assert body is not None
                body = b"X" + body[1:]
            target.addfile(member, None if body is None else io.BytesIO(body))
    forged.seek(0)

    with pytest.raises(SandboxCheckpointError, match="does not match its digest"):
        export_checkpoint_archive(
            forged,
            io.BytesIO(),
            base_layers=frozenset(_digest(layer) for layer in BASE),
            max_bytes=10**6,
        )


@pytest.mark.parametrize(
    ("entry_type", "name", "message"),
    [
        (tarfile.SYMTYPE, "blobs/sha256/" + "a" * 64, "non-regular"),
        (tarfile.REGTYPE, "etc/passwd", "unexpected file"),
        (tarfile.DIRTYPE, "blobs/other", "unexpected directory"),
    ],
)
def test_export_refuses_entries_an_image_archive_never_holds(
    entry_type: bytes, name: str, message: str
) -> None:
    entry = tarfile.TarInfo(name)
    entry.type = entry_type
    body: bytes | None = None
    if entry_type == tarfile.SYMTYPE:
        entry.linkname = "/etc/shadow"
    elif entry_type == tarfile.REGTYPE:
        body = b"root"
        entry.size = len(body)
    image = _Image([*BASE, NEW], extra_entries=[(entry, body)])

    with pytest.raises(SandboxCheckpointError, match=message):
        export_checkpoint_archive(
            image.stream(),
            io.BytesIO(),
            base_layers=frozenset(_digest(layer) for layer in BASE),
            max_bytes=10**6,
        )


def test_export_requires_new_layers_on_top_of_the_base_image() -> None:
    base = frozenset(_digest(layer) for layer in BASE)
    with pytest.raises(SandboxCheckpointError, match="adds no layer"):
        export_checkpoint_archive(
            _Image(list(BASE)).stream(), io.BytesIO(), base_layers=base, max_bytes=10**6
        )
    with pytest.raises(SandboxCheckpointError, match="does not extend"):
        export_checkpoint_archive(
            _Image([NEW]).stream(), io.BytesIO(), base_layers=base, max_bytes=10**6
        )
    with pytest.raises(SandboxCheckpointError, match="interleave"):
        export_checkpoint_archive(
            _Image([BASE[0], NEW, BASE[1]]).stream(),
            io.BytesIO(),
            base_layers=base,
            max_bytes=10**6,
        )


def test_export_refuses_a_manifest_naming_a_missing_new_layer() -> None:
    image = _Image([*BASE, NEW])
    documents = [image.manifest(), image.config()]

    with pytest.raises(SandboxCheckpointError, match="missing a new layer"):
        export_checkpoint_archive(
            image.stream(blobs=documents),
            io.BytesIO(),
            base_layers=frozenset(_digest(layer) for layer in BASE),
            max_bytes=10**6,
        )


def test_export_enforces_the_archive_size_limit() -> None:
    with pytest.raises(SandboxCheckpointError, match="size limit"):
        export_checkpoint_archive(
            _Image([*BASE, NEW]).stream(),
            io.BytesIO(),
            base_layers=frozenset(_digest(layer) for layer in BASE),
            max_bytes=1024,
        )


def test_base_layers_skip_platform_manifests_that_were_never_pulled() -> None:
    image = _Image(list(BASE))
    manifest = image.manifest()
    absent = json.dumps({"schemaVersion": 2, "mediaType": _MANIFEST, "layers": []}).encode()
    nested = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": _INDEX,
            "manifests": [_descriptor(manifest, _MANIFEST), _descriptor(absent, _MANIFEST)],
        }
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for body in (manifest, image.config(), nested, *BASE):
            entry = tarfile.TarInfo(f"blobs/sha256/{_digest(body)}")
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
        index = json.dumps({"manifests": [_descriptor(nested, _INDEX)]}).encode()
        entry = tarfile.TarInfo("index.json")
        entry.size = len(index)
        archive.addfile(entry, io.BytesIO(index))
    output.seek(0)

    assert read_base_layers(output) == frozenset(_digest(layer) for layer in BASE)


def _receipt(sandbox_id: str = "asbx_one") -> SandboxCheckpointReceipt:
    return SandboxCheckpointReceipt(
        checkpoint_id="c" * 32,
        sandbox_id=sandbox_id,
        generation=3,
        container_id="container-1234567890",
        base_image="sha256:" + "b" * 64,
        runtime_id="claude-code",
        runtime_version="2.1.0",
        object_key="cloud-agent/sandbox/v2/" + "d" * 64 + "/g3/" + "c" * 32 + ".oci.tar",
        version_id="v1",
        sha256="e" * 64,
        size_bytes=4096,
        new_layer_bytes=2048,
        layer_count=3,
        created_at="2026-10-13T12:00:00+00:00",
        members=({"thread_id": "athr_one", "native_session_id": "n", "last_settled_job_id": "j"},),
        durations_ms={"commit": 10},
    )


def test_store_keeps_the_latest_receipt_per_sandbox(tmp_path) -> None:
    store = SandboxCheckpointStore(tmp_path / "checkpoints")

    assert store.latest("asbx_one") is None
    store.save(_receipt())

    assert store.latest("asbx_one") == _receipt()
    assert store.latest("asbx_two") is None
    assert (tmp_path / "checkpoints").stat().st_mode & 0o777 == 0o700


def test_store_reports_an_unreadable_receipt_as_absent(tmp_path) -> None:
    store = SandboxCheckpointStore(tmp_path)
    store.save(_receipt())
    path = next(tmp_path.glob("*.json"))
    path.write_text("{not json")

    assert store.latest("asbx_one") is None


class _FakeDocker:
    """Answer the four Docker commands a capture issues."""

    def __init__(self, base: _Image, committed: _Image) -> None:
        self.base = base
        self.committed = committed
        self.commands: list[list[str]] = []
        self.commit_returncode = 0

    def run(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(argv)
        returncode = self.commit_returncode if argv[1:3] == ["container", "commit"] else 0
        return subprocess.CompletedProcess(argv, returncode, "", "")

    def popen(self, argv: list[str], **_kwargs: Any) -> Any:
        self.commands.append(argv)
        stream = self.base.stream() if argv[-1] == "sha256:base" else self.committed.stream()
        return SimpleNamespace(
            stdout=stream,
            wait=lambda timeout=None: 0,
            poll=lambda: 0,
            kill=lambda: None,
        )


def _spec(**overrides: Any) -> Any:
    values = {
        "sandbox_id": "asbx_one",
        "generation": 3,
        "runtime_id": "claude-code",
        "runtime_version": "2.1.0",
        "sandbox_image_digest": "sha256:base",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _member() -> Any:
    return SimpleNamespace(
        thread_id="athr_one",
        native_session_id="native-1",
        last_settled_job_id="ajob_1",
        context_status="live",
    )


def _capture(tmp_path, docker: _FakeDocker) -> SandboxCheckpointCapture:
    return SandboxCheckpointCapture(
        docker_binary="docker",
        spool_dir=tmp_path / "spool",
        store=SandboxCheckpointStore(tmp_path / "checkpoints"),
        run=docker.run,
        popen=docker.popen,
        clock=lambda: 100.0,
        now=lambda: datetime(2026, 10, 13, 12, tzinfo=UTC),
    )


def test_capture_commits_exports_uploads_and_records_the_receipt(tmp_path) -> None:
    docker = _FakeDocker(_Image(list(BASE)), _Image([*BASE, NEW]))
    capture = _capture(tmp_path, docker)
    uploads: list[dict[str, Any]] = []

    def upload(**kwargs: Any) -> SandboxArchiveReceipt:
        body = kwargs["body"].read()
        assert len(body) == kwargs["size"]
        assert _digest(body) == kwargs["sha256"]
        uploads.append(kwargs | {"bytes": body})
        return SandboxArchiveReceipt(
            phase="checkpoint",
            bucket="freeinference",
            key="cloud-agent/sandbox/v2/key.oci.tar",
            size=kwargs["size"],
            sha256=kwargs["sha256"],
            etag='"etag"',
            version_id="version-1",
            s3_checksum_sha256=None,
            multipart=False,
        )

    receipt = capture.capture(
        _spec(),
        SimpleNamespace(container_id="container-1234567890", running=False, image="sha256:base"),
        [_member()],
        upload=upload,
        max_bytes=10**6,
        deadline=1_000.0,
    )

    commit = docker.commands[0]
    assert commit[:5] == ["docker", "container", "commit", "--pause=false", "container-1234567890"]
    reference = commit[5]
    assert reference.startswith(f"{CHECKPOINT_IMAGE_REPOSITORY}/")
    assert ["docker", "image", "save", "sha256:base"] in docker.commands
    assert ["docker", "image", "save", reference] in docker.commands
    assert docker.commands[-1] == ["docker", "image", "rm", reference]
    assert len(uploads) == 1
    assert uploads[0]["sandbox_id"] == "asbx_one"
    assert uploads[0]["generation"] == 3
    assert uploads[0]["checkpoint_id"] == receipt.checkpoint_id == reference.rsplit(":", 1)[1]
    assert f"blobs/sha256/{_digest(NEW)}" in _names(uploads[0]["bytes"])
    assert receipt.new_layer_bytes == len(NEW)
    assert receipt.layer_count == 3
    assert receipt.version_id == "version-1"
    assert receipt.members == (
        {
            "thread_id": "athr_one",
            "native_session_id": "native-1",
            "last_settled_job_id": "ajob_1",
            "context_status": "live",
        },
    )
    assert capture.store.latest("asbx_one") == receipt


def test_capture_scans_the_base_image_once(tmp_path) -> None:
    docker = _FakeDocker(_Image(list(BASE)), _Image([*BASE, NEW]))
    capture = _capture(tmp_path, docker)

    def upload(**kwargs: Any) -> SandboxArchiveReceipt:
        return SandboxArchiveReceipt(
            phase="checkpoint",
            bucket="freeinference",
            key="k",
            size=kwargs["size"],
            sha256=kwargs["sha256"],
            etag="e",
            version_id="v",
            s3_checksum_sha256=None,
            multipart=False,
        )

    inspection = SimpleNamespace(container_id="container-1234567890", running=False, image="i")
    for _ in range(2):
        capture.capture(_spec(), inspection, [], upload=upload, max_bytes=10**6, deadline=1e3)

    assert docker.commands.count(["docker", "image", "save", "sha256:base"]) == 1


def test_capture_removes_the_commit_image_when_upload_fails(tmp_path) -> None:
    docker = _FakeDocker(_Image(list(BASE)), _Image([*BASE, NEW]))
    capture = _capture(tmp_path, docker)

    def upload(**_kwargs: Any) -> SandboxArchiveReceipt:
        raise RuntimeError("S3 unavailable")

    with pytest.raises(RuntimeError, match="S3 unavailable"):
        capture.capture(
            _spec(),
            SimpleNamespace(container_id="container-1234567890", running=False, image="i"),
            [],
            upload=upload,
            max_bytes=10**6,
            deadline=1e3,
        )

    assert docker.commands[-1][:3] == ["docker", "image", "rm"]
    assert capture.store.latest("asbx_one") is None


def test_capture_refuses_a_running_container_and_a_failed_commit(tmp_path) -> None:
    docker = _FakeDocker(_Image(list(BASE)), _Image([*BASE, NEW]))
    capture = _capture(tmp_path, docker)

    with pytest.raises(SandboxCheckpointError, match="running"):
        capture.capture(
            _spec(),
            SimpleNamespace(container_id="c" * 12, running=True, image="i"),
            [],
            upload=lambda **_: None,  # type: ignore[arg-type,return-value]
            max_bytes=10**6,
            deadline=1e3,
        )
    assert docker.commands == []

    docker.commit_returncode = 1
    with pytest.raises(SandboxCheckpointError, match="commit failed"):
        capture.capture(
            _spec(),
            SimpleNamespace(container_id="c" * 12, running=False, image="i"),
            [],
            upload=lambda **_: None,  # type: ignore[arg-type,return-value]
            max_bytes=10**6,
            deadline=1e3,
        )
    assert [command[1:3] for command in docker.commands] == [["container", "commit"]]


def test_capture_removes_an_image_a_timed_out_commit_may_still_create(tmp_path) -> None:
    """Killing the Docker client does not stop the daemon finishing the commit."""
    docker = _FakeDocker(_Image(list(BASE)), _Image([*BASE, NEW]))
    run = docker.run

    def slow_commit(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["container", "commit"]:
            docker.commands.append(argv)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return run(argv, **kwargs)

    docker.run = slow_commit  # type: ignore[method-assign]
    capture = _capture(tmp_path, docker)

    with pytest.raises(subprocess.TimeoutExpired):
        capture.capture(
            _spec(),
            SimpleNamespace(container_id="c" * 12, running=False, image="i"),
            [],
            upload=lambda **_: None,  # type: ignore[arg-type,return-value]
            max_bytes=10**6,
            deadline=1e3,
        )

    reference = docker.commands[0][-1]
    assert docker.commands[-1] == ["docker", "image", "rm", reference]


class _PeakBytes(io.BytesIO):
    """Remember the largest size the spool ever reached."""

    peak = 0

    def write(self, data: Any) -> int:
        written = super().write(data)
        self.peak = max(self.peak, self.tell())
        return written


def test_export_refuses_an_oversized_layer_before_writing_it() -> None:
    limit = 1024 * 1024
    large = b"x" * (8 * 1024 * 1024)
    output = _PeakBytes()

    with pytest.raises(SandboxCheckpointError, match="size limit"):
        export_checkpoint_archive(
            _Image([*BASE, large]).stream(),
            output,
            base_layers=frozenset(_digest(layer) for layer in BASE),
            max_bytes=limit,
        )

    # The 8 MiB layer is refused from its declared size; scratch stays bounded.
    assert output.peak <= limit


class _BlockingStream:
    """A docker save pipe that never produces data until its process dies."""

    def __init__(self) -> None:
        self.killed = threading.Event()

    def read(self, _amount: int = -1) -> bytes:
        self.killed.wait(timeout=10)
        return b""

    def close(self) -> None:
        return


def test_a_stalled_export_is_killed_at_the_deadline_and_cleaned_up(tmp_path) -> None:
    docker = _FakeDocker(_Image(list(BASE)), _Image([*BASE, NEW]))
    stalled = _BlockingStream()
    kills: list[str] = []

    def popen(argv: list[str], **_kwargs: Any) -> Any:
        docker.commands.append(argv)
        if argv[-1] == "sha256:base":
            return SimpleNamespace(
                stdout=docker.base.stream(),
                wait=lambda timeout=None: 0,
                poll=lambda: 0,
                kill=lambda: None,
            )

        def kill() -> None:
            kills.append(argv[-1])
            stalled.killed.set()

        return SimpleNamespace(
            stdout=stalled,
            wait=lambda timeout=None: -9 if stalled.killed.is_set() else 0,
            poll=lambda: -9 if stalled.killed.is_set() else None,
            kill=kill,
        )

    capture = SandboxCheckpointCapture(
        docker_binary="docker",
        spool_dir=tmp_path / "spool",
        store=SandboxCheckpointStore(tmp_path / "checkpoints"),
        run=docker.run,
        popen=popen,
    )
    started = time.monotonic()

    with pytest.raises(SandboxCheckpointError, match="deadline expired"):
        capture.capture(
            _spec(),
            SimpleNamespace(container_id="container-1234567890", running=False, image="i"),
            [],
            upload=lambda **_: pytest.fail("a stalled export must not upload"),  # type: ignore[arg-type,return-value]
            max_bytes=10**6,
            deadline=time.monotonic() + 0.5,
        )

    assert time.monotonic() - started < 3
    assert kills, "the stalled docker save was never killed"
    assert docker.commands[-1][:3] == ["docker", "image", "rm"]


def _content_store(root: Any, *images: _Image) -> Any:
    """Write images' blobs into a containerd-style content store directory."""
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    for image in images:
        for body in (image.manifest(), image.config(), *image.layers):
            (blobs / _digest(body)).write_bytes(body)
    return root


def _descriptor_json(image: _Image) -> str:
    return json.dumps(_descriptor(image.manifest(), _MANIFEST))


def test_content_store_export_copies_only_the_new_layer_in_docker_save_layout(tmp_path) -> None:
    base, committed = _Image(list(BASE)), _Image([*BASE, NEW])
    store = ContainerdContentStore(_content_store(tmp_path, base, committed))
    output = io.BytesIO()

    archive = export_from_content_store(
        store,
        output,
        image=_descriptor(committed.manifest(), _MANIFEST),
        base=_descriptor(base.manifest(), _MANIFEST),
        reference="freeinference-checkpoint/abc:def",
        max_bytes=10**6,
    )

    assert archive.new_layers == (_digest(NEW),)
    assert archive.new_layer_bytes == len(NEW)
    with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as tar:
        names = set(tar.getnames())
        index = json.loads(tar.extractfile("index.json").read())  # type: ignore[union-attr]
        legacy = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
    assert {f"blobs/sha256/{_digest(layer)}" for layer in BASE}.isdisjoint(names)
    assert {
        f"blobs/sha256/{_digest(NEW)}",
        f"blobs/sha256/{_digest(committed.manifest())}",
        f"blobs/sha256/{_digest(committed.config())}",
        "index.json",
        "manifest.json",
        "oci-layout",
    } <= names
    assert index["manifests"][0]["digest"] == f"sha256:{_digest(committed.manifest())}"
    assert index["manifests"][0]["annotations"] == {
        "io.containerd.image.name": "docker.io/freeinference-checkpoint/abc:def",
        "org.opencontainers.image.ref.name": "def",
    }
    assert legacy[0]["RepoTags"] == ["freeinference-checkpoint/abc:def"]
    assert len(legacy[0]["Layers"]) == 3
    # The same structural checks the save path applies hold for this archive.
    assert export_checkpoint_archive(
        io.BytesIO(output.getvalue()),
        io.BytesIO(),
        base_layers=frozenset(_digest(layer) for layer in BASE),
        max_bytes=10**6,
    ).new_layers == (_digest(NEW),)


def test_content_store_export_reports_missing_blobs_as_unavailable(tmp_path) -> None:
    base, committed = _Image(list(BASE)), _Image([*BASE, NEW])
    root = _content_store(tmp_path, base, committed)
    store = ContainerdContentStore(root)
    (root / "blobs" / "sha256" / _digest(NEW)).unlink()

    with pytest.raises(CheckpointContentUnavailable):
        export_from_content_store(
            store,
            io.BytesIO(),
            image=_descriptor(committed.manifest(), _MANIFEST),
            base=_descriptor(base.manifest(), _MANIFEST),
            reference="freeinference-checkpoint/abc:def",
            max_bytes=10**6,
        )
    with pytest.raises(CheckpointContentUnavailable):
        export_from_content_store(
            store,
            io.BytesIO(),
            image=_descriptor(committed.manifest(), _MANIFEST),
            base=_descriptor(b"an image this daemon never pulled", _MANIFEST),
            reference="freeinference-checkpoint/abc:def",
            max_bytes=10**6,
        )


def test_content_store_export_refuses_a_damaged_layer_instead_of_falling_back(tmp_path) -> None:
    base, committed = _Image(list(BASE)), _Image([*BASE, NEW])
    root = _content_store(tmp_path, base, committed)
    (root / "blobs" / "sha256" / _digest(NEW)).write_bytes(b"X" + NEW[1:])

    with pytest.raises(SandboxCheckpointError, match="does not match its digest") as excinfo:
        export_from_content_store(
            ContainerdContentStore(root),
            io.BytesIO(),
            image=_descriptor(committed.manifest(), _MANIFEST),
            base=_descriptor(base.manifest(), _MANIFEST),
            reference="freeinference-checkpoint/abc:def",
            max_bytes=10**6,
        )
    assert not isinstance(excinfo.value, CheckpointContentUnavailable)


def test_content_store_export_is_sized_before_anything_is_written(tmp_path) -> None:
    large = b"y" * (2 * 1024 * 1024)
    base, committed = _Image(list(BASE)), _Image([*BASE, large])
    output = _PeakBytes()

    with pytest.raises(SandboxCheckpointError, match="size limit"):
        export_from_content_store(
            ContainerdContentStore(_content_store(tmp_path, base, committed)),
            output,
            image=_descriptor(committed.manifest(), _MANIFEST),
            base=_descriptor(base.manifest(), _MANIFEST),
            reference="freeinference-checkpoint/abc:def",
            max_bytes=1024 * 1024,
        )
    assert output.peak == 0


class _InspectingDocker(_FakeDocker):
    """Also answer `docker image inspect --format {{json .Descriptor}}`."""

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["image", "inspect"]:
            self.commands.append(argv)
            image = self.base if argv[-1] == "sha256:base" else self.committed
            return subprocess.CompletedProcess(argv, 0, _descriptor_json(image), "")
        return super().run(argv, **kwargs)


def _direct_capture(tmp_path, docker: _FakeDocker, content_dir: Any) -> SandboxCheckpointCapture:
    return SandboxCheckpointCapture(
        docker_binary="docker",
        spool_dir=tmp_path / "spool",
        store=SandboxCheckpointStore(tmp_path / "checkpoints"),
        run=docker.run,
        popen=docker.popen,
        clock=lambda: 100.0,
        now=lambda: datetime(2026, 10, 13, 12, tzinfo=UTC),
        content_dir=content_dir,
    )


def _recording_upload(uploads: list[bytes]) -> Any:
    def upload(**kwargs: Any) -> SandboxArchiveReceipt:
        uploads.append(kwargs["body"].read())
        return SandboxArchiveReceipt(
            phase="checkpoint",
            bucket="freeinference",
            key="k",
            size=kwargs["size"],
            sha256=kwargs["sha256"],
            etag="e",
            version_id="v",
            s3_checksum_sha256=None,
            multipart=False,
        )

    return upload


def test_capture_reads_the_new_layer_from_the_content_store_without_saving(tmp_path) -> None:
    base, committed = _Image(list(BASE)), _Image([*BASE, NEW])
    docker = _InspectingDocker(base, committed)
    content = _content_store(tmp_path / "content", base, committed)
    uploads: list[bytes] = []

    receipt = _direct_capture(tmp_path, docker, content).capture(
        _spec(),
        SimpleNamespace(container_id="container-1234567890", running=False, image="i"),
        [],
        upload=_recording_upload(uploads),
        max_bytes=10**6,
        deadline=1e3,
    )

    assert not any(command[1:3] == ["image", "save"] for command in docker.commands)
    assert "base_scan" not in receipt.durations_ms
    assert receipt.new_layer_bytes == len(NEW)
    assert f"blobs/sha256/{_digest(NEW)}" in _names(uploads[0])
    assert docker.commands[-1][:3] == ["docker", "image", "rm"]


@pytest.mark.parametrize("prepare", ["no-mount", "empty-mount", "other-daemon"])
def test_capture_falls_back_to_image_save_without_a_usable_content_store(
    tmp_path, prepare: str
) -> None:
    base, committed = _Image(list(BASE)), _Image([*BASE, NEW])
    docker = _InspectingDocker(base, committed)
    content = tmp_path / "content"
    if prepare == "empty-mount":
        content.mkdir()
    elif prepare == "other-daemon":
        _content_store(content, _Image([b"unrelated"]))
    uploads: list[bytes] = []

    receipt = _direct_capture(tmp_path, docker, content).capture(
        _spec(),
        SimpleNamespace(container_id="container-1234567890", running=False, image="i"),
        [],
        upload=_recording_upload(uploads),
        max_bytes=10**6,
        deadline=1e3,
    )

    assert any(command[1:3] == ["image", "save"] for command in docker.commands)
    assert receipt.new_layer_bytes == len(NEW)
    # A partial content-store attempt never leaks into the uploaded archive.
    assert export_checkpoint_archive(
        io.BytesIO(uploads[0]),
        io.BytesIO(),
        base_layers=frozenset(_digest(layer) for layer in BASE),
        max_bytes=10**6,
    ).new_layers == (_digest(NEW),)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("freeinference-checkpoint/abc:def", "docker.io/freeinference-checkpoint/abc:def"),
        ("ubuntu:24.04", "docker.io/library/ubuntu:24.04"),
        ("ghcr.io/org/image:tag", "ghcr.io/org/image:tag"),
        ("localhost/image:tag", "localhost/image:tag"),
    ],
)
def test_image_names_match_dockers_normalisation(reference: str, expected: str) -> None:
    assert _image_names(reference) == (expected, expected.rsplit(":", 1)[1])
