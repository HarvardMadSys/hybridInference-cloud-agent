"""Recreating a moved group from its checkpoint on another host."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.test_sandbox_checkpoints import BASE, NEW, _digest, _Image
from tests.unit.test_shared_session_supervisor import _Manager, _members, _spec

from cloud_agent_host.chat_sandbox import (
    CHAT_CHECKPOINT_LABEL,
    ChatSandboxInspection,
    ChatSandboxManager,
    ChatSandboxSpec,
    ChatSandboxStatus,
    SandboxRestoreFailed,
)
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.sandbox_archive_s3 import (
    SandboxArchiveDownloadError,
    SandboxArchiveS3Config,
    SandboxArchiveS3Uploader,
    checkpoint_object_key,
)
from cloud_agent_host.sandbox_checkpoints import (
    CheckpointRestore,
    SandboxCheckpointCapture,
    SandboxCheckpointError,
    SandboxCheckpointStore,
    checkpoint_image_reference,
    export_checkpoint_archive,
    prepare_restore_archive,
)
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

_BASE_IMAGE = "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64
_CHECKPOINT = "c" * 32
_REFERENCE = checkpoint_image_reference("asbx_shared", _CHECKPOINT)
_BASE_LAYERS = frozenset(_digest(layer) for layer in BASE)


def _checkpoint_archive(image: _Image | None = None) -> bytes:
    """A checkpoint exactly as a capture uploads it."""
    output = io.BytesIO()
    export_checkpoint_archive(
        (image or _Image([*BASE, NEW])).stream(),
        output,
        base_layers=_BASE_LAYERS,
        max_bytes=10**6,
    )
    return output.getvalue()


def _entries(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar
            if member.isreg()
        }


def _rewrite(archive: bytes, change: Any) -> bytes:
    """Rebuild ``archive`` after ``change`` edits its {name: body} entries."""
    entries = _entries(archive)
    change(entries)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, body in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            tar.addfile(entry, io.BytesIO(body))
    return output.getvalue()


def _prepare(archive: bytes, **overrides: Any) -> tuple[Any, bytes]:
    output = io.BytesIO()
    arguments: dict[str, Any] = {
        "reference": _REFERENCE,
        "base_layers": _BASE_LAYERS,
        "max_bytes": 10**6,
    }
    arguments.update(overrides)
    result = prepare_restore_archive(io.BytesIO(archive), output, **arguments)
    return result, output.getvalue()


# -- the archive a restoring host loads --------------------------------------


def test_a_checkpoint_is_rewritten_under_its_own_name_only():
    forged = _rewrite(
        _checkpoint_archive(),
        lambda entries: entries.update(
            {
                "manifest.json": json.dumps(
                    [{"Config": "x", "RepoTags": ["ghcr.io/harvardmadsys/cloud-agent:runner"]}]
                ).encode(),
                "repositories": b'{"ghcr.io/harvardmadsys/cloud-agent": {"runner": "x"}}',
            }
        ),
    )

    archive, prepared = _prepare(forged)

    assert archive.new_layers == (_digest(NEW),)
    entries = _entries(prepared)
    assert "repositories" not in entries
    assert json.loads(entries["manifest.json"])[0]["RepoTags"] == [_REFERENCE]
    index = json.loads(entries["index.json"])
    assert index["manifests"][0]["annotations"]["io.containerd.image.name"] == (
        f"docker.io/{_REFERENCE}"
    )
    # The rewritten archive is still exactly the checkpoint's image.
    assert {f"blobs/sha256/{_digest(NEW)}"} <= set(entries)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("foreign_layer", "missing a new layer"),
        ("foreign_base", "does not extend the sandbox image"),
        ("missing_layer", "missing a new layer"),
        ("extra_blob", "blobs its image does not use"),
        ("corrupt_layer", "does not match its digest"),
        ("two_images", "exactly one image"),
    ],
)
def test_a_forged_or_damaged_checkpoint_is_refused(damage: str, message: str):
    archive = _checkpoint_archive()
    foreign = b"a layer of someone else's image on the restoring host" * 20
    if damage in {"foreign_layer", "foreign_base"}:
        # The manifest names a layer the archive does not carry and the base
        # image does not have, hoping to borrow it from the restoring host.
        layers = [*BASE, foreign, NEW] if damage == "foreign_layer" else [foreign, *BASE[1:], NEW]
        output = io.BytesIO()
        export_checkpoint_archive(
            _Image(layers).stream(),
            output,
            base_layers=_BASE_LAYERS | {_digest(foreign)},
            max_bytes=10**6,
        )
        archive = output.getvalue()
    elif damage == "missing_layer":
        archive = _rewrite(archive, lambda e: e.pop(f"blobs/sha256/{_digest(NEW)}"))
    elif damage == "extra_blob":
        stray = b"a blob no manifest names"
        archive = _rewrite(archive, lambda e: e.update({f"blobs/sha256/{_digest(stray)}": stray}))
    elif damage == "corrupt_layer":
        archive = _rewrite(
            archive, lambda e: e.update({f"blobs/sha256/{_digest(NEW)}": b"X" + NEW[1:]})
        )
    elif damage == "two_images":
        other = _Image([*BASE, b"second image layer" * 30])

        def add_second(entries: dict[str, bytes]) -> None:
            index = json.loads(entries["index.json"])
            index["manifests"].append(
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": f"sha256:{_digest(other.manifest())}",
                    "size": len(other.manifest()),
                }
            )
            entries["index.json"] = json.dumps(index).encode()
            entries[f"blobs/sha256/{_digest(other.manifest())}"] = other.manifest()

        archive = _rewrite(archive, add_second)

    with pytest.raises(SandboxCheckpointError, match=message):
        _prepare(archive)


def test_a_checkpoint_over_the_size_limit_is_refused():
    with pytest.raises(SandboxCheckpointError, match="size limit"):
        _prepare(_checkpoint_archive(), max_bytes=4096)


# -- downloading the object the receipt names --------------------------------


class _S3:
    def __init__(self, body: bytes, *, length: int | None = None) -> None:
        self.body = body
        self.length = len(body) if length is None else length
        self.requests: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return {"Body": io.BytesIO(self.body), "ContentLength": self.length}


def _uploader(client: _S3) -> SandboxArchiveS3Uploader:
    policy = SandboxArchivePolicy(
        enabled=True,
        region="us-east-1",
        aws_access_key_id="A" * 20,
        aws_secret_access_key="s" * 40,
        revision=1,
    )
    config = SandboxArchiveS3Config.from_policy(
        policy,
        aws_access_key_id=policy.aws_access_key_id,
        aws_secret_access_key=policy.aws_secret_access_key,
    )
    return SandboxArchiveS3Uploader(config, client=client)  # type: ignore[arg-type]


def _download(uploader: SandboxArchiveS3Uploader, body: bytes, **overrides: Any) -> bytes:
    output = io.BytesIO()
    arguments: dict[str, Any] = {
        "sandbox_id": "asbx_shared",
        "key": checkpoint_object_key(
            sandbox_id="asbx_shared", generation=1, checkpoint_id=_CHECKPOINT
        ),
        "version_id": "version-1",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "output": output,
    }
    arguments.update(overrides)
    uploader.download_checkpoint(**arguments)
    return output.getvalue()


def test_a_checkpoint_downloads_the_exact_version_its_receipt_names():
    body = b"checkpoint bytes" * 100
    client = _S3(body)

    assert _download(_uploader(client), body) == body
    assert client.requests[0]["VersionId"] == "version-1"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("other_sandbox", "does not name this sandbox"),
        ("not_a_checkpoint", "does not name this sandbox"),
        ("short", "shorter than its receipt"),
        ("long", "larger than its receipt"),
        ("replaced", "does not match its digest"),
    ],
)
def test_a_checkpoint_object_that_is_not_the_receipts_is_refused(case: str, message: str):
    body = b"checkpoint bytes" * 100
    overrides: dict[str, Any] = {}
    served = body
    if case == "other_sandbox":
        overrides["key"] = checkpoint_object_key(
            sandbox_id="asbx_other", generation=1, checkpoint_id=_CHECKPOINT
        )
    elif case == "not_a_checkpoint":
        overrides["key"] = "cloud-agent/sandbox/v1/elsewhere.tar.gz"
    elif case == "short":
        served = body[:-1]
        overrides["sha256"] = hashlib.sha256(body).hexdigest()
    elif case == "long":
        served = body + b"!"
    elif case == "replaced":
        served = b"X" + body[1:]
    # The object's own size header agrees with the receipt; only the bytes differ.
    client = _S3(served, length=len(body))

    with pytest.raises(SandboxArchiveDownloadError, match=message):
        _download(_uploader(client), body, **overrides)


# -- turning the download into a local image ----------------------------------


class _Docker:
    """Answer the Docker commands a restore issues."""

    def __init__(self, *, present: set[str], load_ok: bool = True) -> None:
        self.present = set(present)
        self.load_ok = load_ok
        self.commands: list[list[str]] = []
        self.loaded: list[bytes] = []

    def run(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(argv)
        verb = argv[1:3]
        if verb == ["image", "inspect"]:
            image = argv[-1]
            if image == _BASE_IMAGE and "--format" in argv and "{{json .Descriptor}}" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "no content store")
            return subprocess.CompletedProcess(argv, 0 if image in self.present else 1, "", "")
        if verb == ["image", "pull"]:
            self.present.add(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == ["image", "load"]:
            with open(argv[-1], "rb") as handle:
                self.loaded.append(handle.read())
            if self.load_ok:
                self.present.add(_REFERENCE)
            return subprocess.CompletedProcess(argv, 0 if self.load_ok else 1, "", "")
        raise AssertionError(argv)

    def popen(self, argv: list[str], **_kwargs: Any) -> Any:
        self.commands.append(argv)
        assert argv[1:3] == ["image", "save"] and argv[-1] == _BASE_IMAGE
        return SimpleNamespace(
            stdout=_Image(list(BASE)).stream(),
            wait=lambda timeout=None: 0,
            poll=lambda: 0,
            kill=lambda: None,
        )


def _restore() -> CheckpointRestore:
    archive = _checkpoint_archive()
    return CheckpointRestore(
        checkpoint_id=_CHECKPOINT,
        object_key=checkpoint_object_key(
            sandbox_id="asbx_shared", generation=1, checkpoint_id=_CHECKPOINT
        ),
        version_id="version-1",
        sha256=hashlib.sha256(archive).hexdigest(),
        size_bytes=len(archive),
        base_image=_BASE_IMAGE,
    )


def _capture(tmp_path, docker: _Docker) -> SandboxCheckpointCapture:
    return SandboxCheckpointCapture(
        docker_binary="docker",
        spool_dir=tmp_path / "spool",
        store=SandboxCheckpointStore(tmp_path / "checkpoints"),
        run=docker.run,
        popen=docker.popen,
        clock=lambda: 100.0,
    )


def _serve(archive: bytes) -> Any:
    def download(*, output: Any, **_kwargs: Any) -> None:
        output.write(archive)

    return download


def test_a_restore_pulls_the_base_by_digest_and_loads_only_the_checked_archive(tmp_path):
    docker = _Docker(present=set())
    archive = _checkpoint_archive()

    reference = _capture(tmp_path, docker).restore(
        "asbx_shared", _restore(), download=_serve(archive), max_bytes=10**6, deadline=1e3
    )

    assert reference == _REFERENCE
    assert ["docker", "image", "pull", "--quiet", _BASE_IMAGE] in docker.commands
    assert len(docker.loaded) == 1
    assert json.loads(_entries(docker.loaded[0])["manifest.json"])[0]["RepoTags"] == [_REFERENCE]
    assert not list((tmp_path / "spool").iterdir())


def test_a_restore_reuses_an_image_already_loaded_here(tmp_path):
    docker = _Docker(present={_REFERENCE})

    def unreachable(**_kwargs: Any) -> None:
        raise AssertionError("downloaded again")

    reference = _capture(tmp_path, docker).restore(
        "asbx_shared", _restore(), download=unreachable, max_bytes=10**6, deadline=1e3
    )

    assert reference == _REFERENCE


def test_a_failed_load_is_a_failed_restore(tmp_path):
    docker = _Docker(present={_BASE_IMAGE}, load_ok=False)

    with pytest.raises(SandboxCheckpointError, match="could not be loaded"):
        _capture(tmp_path, docker).restore(
            "asbx_shared",
            _restore(),
            download=_serve(_checkpoint_archive()),
            max_bytes=10**6,
            deadline=1e3,
        )


def test_a_restore_needs_a_pullable_base_image():
    with pytest.raises(SandboxCheckpointError, match="pullable"):
        replace(_restore(), base_image="cloud-agent-sandbox@sha256:" + "a" * 64)


# -- the container a restore creates ------------------------------------------


def _moved_spec() -> ChatSandboxSpec:
    return replace(_spec(), generation=2, sandbox_image_digest=_BASE_IMAGE)


def _inspection(spec: ChatSandboxSpec, *, image: str, labels: dict[str, str]):
    return ChatSandboxInspection(
        container_id="container-restored",
        name=spec.container_name,
        status=ChatSandboxStatus.STOPPED,
        running=False,
        image=image,
        labels=labels,
        mounts=(),
        networks=(),
    )


def test_a_restored_container_runs_its_checkpoint_image_and_says_so():
    spec = _moved_spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))

    command = manager.build_create_command(spec, image=_REFERENCE, checkpoint_id=_CHECKPOINT)

    assert f"{CHAT_CHECKPOINT_LABEL}={_CHECKPOINT}" in command
    assert _REFERENCE in command
    with pytest.raises(SandboxError, match="does not match its checkpoint"):
        manager.build_create_command(spec, image=_BASE_IMAGE, checkpoint_id=_CHECKPOINT)


def test_identity_accepts_a_checkpoint_image_only_for_the_checkpoint_it_names():
    spec = _moved_spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    labelled = {**spec.docker_labels(), CHAT_CHECKPOINT_LABEL: _CHECKPOINT}

    manager._verify_identity(_inspection(spec, image=_REFERENCE, labels=labelled), spec)
    for image, labels in (
        # A checkpoint image without the label that says which checkpoint.
        (_REFERENCE, spec.docker_labels()),
        # Another checkpoint's image under this one's label.
        (checkpoint_image_reference("asbx_shared", "d" * 32), labelled),
    ):
        with pytest.raises(SandboxError, match="outside this chat/generation"):
            manager._verify_identity(_inspection(spec, image=image, labels=labels), spec)


# -- the supervisor's restore path ----------------------------------------------


class _RestoringManager(_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.created: list[dict[str, Any]] = []
        self.superseded: list[str] = []

    def create(self, spec, *, image=None, checkpoint_id=None, facts=None, observer=None):
        self.created.append({"image": image, "checkpoint_id": checkpoint_id})
        return super().create(spec, facts=facts, observer=observer)

    def remove_superseded(self, spec):
        self.superseded.append(spec.container_name)
        return False


def test_a_moved_group_is_created_only_from_its_checkpoint():
    manager = _RestoringManager()
    supervisor = SharedChatSessionSupervisor(manager)

    with pytest.raises(SandboxError, match="only be recreated from its checkpoint"):
        supervisor.register_manifest(_moved_spec(), _members())

    assert manager.created == []


def test_a_moved_group_is_recreated_from_the_image_its_checkpoint_loads():
    manager = _RestoringManager()
    hooked: list[tuple[str, str]] = []

    def restore(sandbox_id: str, checkpoint: CheckpointRestore) -> str:
        hooked.append((sandbox_id, checkpoint.checkpoint_id))
        return _REFERENCE

    supervisor = SharedChatSessionSupervisor(manager, checkpoint_restore_hook=restore)

    supervisor.register_manifest(_moved_spec(), _members(), restore=_restore(), start_seq=7)

    assert hooked == [("asbx_shared", _CHECKPOINT)]
    assert manager.created == [{"image": _REFERENCE, "checkpoint_id": _CHECKPOINT}]
    assert manager.superseded == [_moved_spec().container_name]


def test_a_failed_restore_says_so_without_losing_the_chat():
    manager = _RestoringManager()

    def broken(_sandbox_id: str, _checkpoint: CheckpointRestore) -> str:
        raise SandboxCheckpointError("checkpoint blob does not match its digest")

    supervisor = SharedChatSessionSupervisor(manager, checkpoint_restore_hook=broken)

    with pytest.raises(SandboxRestoreFailed, match="does not match its digest"):
        supervisor.register_manifest(_moved_spec(), _members(), restore=_restore())
    assert manager.created == []
    # Nothing is left registered: the next attempt starts from the checkpoint again.
    assert supervisor.start_seq_for("asbx_shared") is None
    with pytest.raises(SandboxRestoreFailed, match="cannot restore"):
        SharedChatSessionSupervisor(_RestoringManager()).register_manifest(
            _moved_spec(), _members(), restore=_restore()
        )


def test_a_checkpoint_names_the_start_that_actually_ran():
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
        start_seq=4,
    )
    assert lease is not None
    assert supervisor.start_seq_for("asbx_shared") == 4

    # A later request names its fence, but nothing has started for it yet:
    # a checkpoint of a stop before then still belongs to the earlier run.
    supervisor.register_manifest(
        _spec(), _members(), expected_container_id="container-shared", start_seq=5
    )
    assert supervisor.start_seq_for("asbx_shared") == 4
