"""Restorable checkpoints: a stopped sandbox's changes over its image, kept in S3.

A checkpoint is ``docker commit`` of a cleanly stopped chat container, exported
as an OCI image archive that omits every layer the sandbox image already
provides. A host holding the same base image can load it and recreate the
container, which is what will let a chat move between hosts. This module is the
Docker capture and restore orchestration; the archive codec is in
:mod:`cloud_agent_host.sandbox_checkpoint_oci` and the receipt store in
:mod:`cloud_agent_host.sandbox_checkpoint_store`.

Docker has no command that saves a single layer. When containerd's content
store is mounted read-only into the broker, the new layers are read straight
from it: ``docker image inspect`` names the committed manifest, and the
manifest names its blobs. Otherwise the whole committed image is streamed
through ``docker image save`` and the base image's blobs are dropped on the
way, which reads the entire image for every checkpoint.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import secrets
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import UTC, datetime
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Final, TypeVar

from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveDownloadError
from cloud_agent_host.sandbox_checkpoint_oci import (
    _DIGEST_RE,
    _MAX_METADATA_BYTES,
    CheckpointArchive,
    CheckpointContentUnavailable,
    _descriptor_of,
    _image_names as _image_names,
    _manifest_layers,
    _walk_manifests,
    export_checkpoint_archive,
    export_from_content_store,
    prepare_restore_archive,
    read_base_layers,
)
from cloud_agent_host.sandbox_checkpoint_store import (
    CheckpointArchiveInvalid,
    SandboxCheckpointError,
    SandboxCheckpointStore,
)
from cloud_agent_shared.environment_snapshots import (
    SNAPSHOT_IMAGE_REPOSITORY,
    snapshot_image_reference,
    valid_snapshot_id,
)
from cloud_agent_shared.sandbox_restore import (
    CHECKPOINT_FORMAT_VERSION,
    CheckpointRestore,
    SandboxCheckpointMember,
    SandboxCheckpointReceipt,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from cloud_agent_host.chat_sandbox import ChatSandboxInspection, ChatSandboxSpec
    from cloud_agent_host.sandbox_archive_s3 import SandboxArchiveReceipt
    from cloud_agent_host.shared_session_supervisor import SharedMemberManifest
    from cloud_agent_shared.environment_snapshots import EnvironmentSnapshotRef

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Where host Compose mounts containerd's content store, read-only.
DEFAULT_CONTAINERD_CONTENT_DIR: Final = "/var/lib/hybridinference/containerd-content"
CHECKPOINT_IMAGE_REPOSITORY: Final = "freeinference-checkpoint"
_COPY_CHUNK_BYTES: Final = 1024 * 1024
_COMMIT_TIMEOUT_S: Final = 600.0
_IMAGE_REMOVE_TIMEOUT_S: Final = 120.0
_PULL_TIMEOUT_S: Final = 1800.0
_LOAD_TIMEOUT_S: Final = 1800.0


class ContainerdContentStore:
    """A read-only view of containerd's content store, whose blobs are digest-named.

    Containerd writes a blob under its digest only after verifying it, and this
    view verifies every read again, so a wrong mount or a damaged file is
    refused instead of copied.
    """

    def __init__(self, root: Path) -> None:
        self._blobs = root / "blobs" / "sha256"

    def available(self) -> bool:
        """Whether the mount looks like a content store at all."""
        try:
            return self._blobs.is_dir()
        except OSError:
            return False

    def _path(self, digest: str) -> Path:
        if _DIGEST_RE.fullmatch(digest) is None:
            raise SandboxCheckpointError("content store digest is invalid")
        return self._blobs / digest

    def size(self, digest: str) -> int | None:
        """Return a blob's size, or ``None`` when the store does not hold it."""
        try:
            return self._path(digest).stat().st_size
        except FileNotFoundError:
            return None

    def read(self, digest: str) -> bytes | None:
        """Return a small, verified blob such as a manifest, index or config."""
        size = self.size(digest)
        if size is None:
            return None
        if size > _MAX_METADATA_BYTES:
            raise SandboxCheckpointError("content store metadata blob is too large")
        body = self._path(digest).read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise SandboxCheckpointError("content store blob does not match its digest")
        return body

    def open(self, digest: str) -> BinaryIO:
        """Open a blob for streaming; the caller verifies what it copies."""
        return self._path(digest).open("rb")


class _Watchdog:
    """Kill a child process if the deadline passes while its output is read.

    Reading ``docker image save`` blocks; a timeout on ``wait`` alone would only
    start after the read returned. Killing the process ends the read with EOF,
    so the caller can release its capture slot and finish the stop.
    """

    def __init__(self, process: subprocess.Popen[bytes], seconds: float) -> None:
        self.fired = threading.Event()
        self._process = process
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        self.fired.set()
        with contextlib.suppress(OSError):
            self._process.kill()

    def __enter__(self) -> _Watchdog:
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._timer.cancel()


def checkpoint_image_reference(sandbox_id: str, checkpoint_id: str) -> str:
    """The one local name a checkpoint image has, on the host that took it or restores it."""
    sandbox_hash = hashlib.sha256(sandbox_id.encode("utf-8")).hexdigest()[:32]
    return f"{CHECKPOINT_IMAGE_REPOSITORY}/{sandbox_hash}:{checkpoint_id}"


class SandboxCheckpointCapture:
    """Commit, export and upload one stopped group through the Docker CLI."""

    def __init__(
        self,
        *,
        docker_binary: str,
        spool_dir: Path,
        store: SandboxCheckpointStore,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        content_dir: Path | None = None,
    ) -> None:
        self._docker = docker_binary
        self._spool_dir = spool_dir
        self.store = store
        self._content = None if content_dir is None else ContainerdContentStore(content_dir)
        self._run = run
        self._popen = popen
        self._clock = clock
        self._now = now
        self._base_layers: dict[str, frozenset[str]] = {}
        self._base_lock = threading.Lock()
        #: Where a receipt learns its group's start fence; the sandbox manager
        #: wires its supervisor in once both exist.
        self.start_seq_for: Callable[[str], int | None] = lambda _sandbox_id: None

    def receipt_start_seq(
        self, spec: ChatSandboxSpec, inspection: ChatSandboxInspection
    ) -> int | None:
        """The start fence of this host's last checkpoint of exactly this container."""
        receipt = self.store.latest(spec.sandbox_id)
        if (
            receipt is None
            or receipt.generation != spec.generation
            or receipt.container_id != inspection.container_id
        ):
            return None
        return receipt.start_seq

    def capture(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        members: Iterable[SharedMemberManifest],
        *,
        upload: Callable[..., SandboxArchiveReceipt],
        max_bytes: int,
        deadline: float,
    ) -> SandboxCheckpointReceipt:
        """Checkpoint one exact stopped container and remember its receipt."""
        if inspection.running:
            raise SandboxCheckpointError("refusing to checkpoint a running sandbox")
        base_image = spec.sandbox_image_digest or inspection.image
        if not base_image:
            raise SandboxCheckpointError("sandbox checkpoint needs its base image identity")
        start_seq = self.start_seq_for(spec.sandbox_id)
        checkpoint_id = secrets.token_hex(16)
        reference = checkpoint_image_reference(spec.sandbox_id, checkpoint_id)
        durations: dict[str, int] = {}
        uploaded, archive = self._commit_and_upload(
            inspection.container_id,
            reference,
            base_image,
            lambda body, size, sha256: upload(
                sandbox_id=spec.sandbox_id,
                generation=spec.generation,
                checkpoint_id=checkpoint_id,
                body=body,
                size=size,
                sha256=sha256,
                deadline=deadline,
            ),
            max_bytes=max_bytes,
            deadline=deadline,
            durations=durations,
            keep_image=False,
        )
        receipt = SandboxCheckpointReceipt(
            checkpoint_id=checkpoint_id,
            sandbox_id=spec.sandbox_id,
            generation=spec.generation,
            container_id=inspection.container_id,
            base_image=base_image,
            runtime_id=spec.runtime_id,
            runtime_version=spec.runtime_version,
            object_key=uploaded.key,
            version_id=uploaded.version_id,
            sha256=uploaded.sha256,
            size_bytes=uploaded.size,
            new_layer_bytes=archive.new_layer_bytes,
            layer_count=len(archive.base_layers) + len(archive.new_layers),
            created_at=self._now(),
            members=[
                SandboxCheckpointMember(
                    thread_id=member.thread_id,
                    native_session_id=member.native_session_id,
                    last_settled_job_id=member.last_settled_job_id,
                    context_status=member.context_status,
                )
                for member in members
            ],
            durations_ms=durations,
            start_seq=start_seq,
        )
        self.store.save(receipt)
        return receipt

    def capture_environment(
        self,
        spec: ChatSandboxSpec,
        inspection: ChatSandboxInspection,
        *,
        owner: str,
        snapshot_id: str,
        upload: Callable[..., SandboxArchiveReceipt],
        max_bytes: int,
        deadline: float,
    ) -> dict[str, Any]:
        """Save one stopped environment run's container as an installation snapshot.

        The same commit, export and upload as a checkpoint, under the
        snapshot's own image name and key. The committed image is kept: this
        host is the likeliest to start the next workspace from it.
        """
        if inspection.running:
            raise SandboxCheckpointError("refusing to snapshot a running sandbox")
        base_image = spec.sandbox_image_digest or inspection.image
        if not base_image:
            raise SandboxCheckpointError("environment snapshot needs its base image identity")
        reference = snapshot_image_reference(owner, snapshot_id)
        durations: dict[str, int] = {}
        uploaded, archive = self._commit_and_upload(
            inspection.container_id,
            reference,
            base_image,
            lambda body, size, sha256: upload(
                owner=owner,
                snapshot_id=snapshot_id,
                body=body,
                size=size,
                sha256=sha256,
                deadline=deadline,
            ),
            max_bytes=max_bytes,
            deadline=deadline,
            durations=durations,
            keep_image=True,
        )
        return {
            "snapshot_id": snapshot_id,
            "object_key": uploaded.key,
            "version_id": uploaded.version_id,
            "sha256": uploaded.sha256,
            "size_bytes": uploaded.size,
            "new_layer_bytes": archive.new_layer_bytes,
            "base_image": base_image,
            "durations_ms": durations,
        }

    def _commit_and_upload(
        self,
        container_id: str,
        reference: str,
        base_image: str,
        upload: Callable[[BinaryIO, int, str], SandboxArchiveReceipt],
        *,
        max_bytes: int,
        deadline: float,
        durations: dict[str, int],
        keep_image: bool,
    ) -> tuple[SandboxArchiveReceipt, CheckpointArchive]:
        """Commit a stopped container as ``reference`` and upload its new layers.

        The committed image is removed once uploaded unless ``keep_image``,
        and whenever the export or upload fails.
        """
        started = self._clock()
        timeout = min(_COMMIT_TIMEOUT_S, self._remaining(deadline))
        try:
            committed = self._run(
                [self._docker, "container", "commit", "--pause=false", container_id, reference],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except BaseException:
            # A timeout kills only the client: the daemon may still finish the
            # commit and tag the image, which nothing else would ever remove.
            self._remove_image(reference)
            raise
        if committed.returncode != 0:
            raise SandboxCheckpointError("sandbox checkpoint commit failed")
        durations["commit"] = self._elapsed_ms(started)
        try:
            self._spool_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=self._spool_dir, prefix="checkpoint-") as spool:
                archive = self._export_from_content(
                    reference, base_image, spool, max_bytes, deadline, durations
                )
                if archive is None:
                    base_layers = self._base_image_layers(base_image, deadline, durations)
                    started = self._clock()
                    archive = self._export(reference, spool, base_layers, max_bytes, deadline)
                    durations["export"] = self._elapsed_ms(started)
                size = spool.tell()
                spool.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: spool.read(_COPY_CHUNK_BYTES), b""):
                    digest.update(chunk)
                spool.seek(0)
                started = self._clock()
                uploaded = upload(spool, size, digest.hexdigest())
                durations["upload"] = self._elapsed_ms(started)
        except BaseException:
            self._remove_image(reference)
            raise
        if not keep_image:
            self._remove_image(reference)
        return uploaded, archive

    def _export_from_content(
        self,
        reference: str,
        base_image: str,
        spool: BinaryIO,
        max_bytes: int,
        deadline: float,
        durations: dict[str, int],
    ) -> CheckpointArchive | None:
        """Export through the content store, or return ``None`` to use image save."""
        if self._content is None or not self._content.available():
            return None
        started = self._clock()
        try:
            archive = export_from_content_store(
                self._content,
                spool,
                image=self._image_descriptor(reference, deadline),
                base=self._image_descriptor(base_image, deadline),
                reference=reference,
                max_bytes=max_bytes,
            )
        except CheckpointContentUnavailable as exc:
            logger.warning(
                "sandbox checkpoint falls back to image save",
                extra={"event": "sandbox_checkpoint_content_unavailable", "detail": str(exc)},
            )
            spool.seek(0)
            spool.truncate()
            return None
        durations["export"] = self._elapsed_ms(started)
        return archive

    def restore(
        self,
        sandbox_id: str,
        restore: CheckpointRestore,
        *,
        download: Callable[..., None],
        max_bytes: int,
        deadline: float,
    ) -> str:
        """Make ``restore``'s checkpoint a local image and return its name.

        The base image is pulled by digest when this host lacks it. The object
        is downloaded against its receipt, checked and rewritten by
        :func:`prepare_restore_archive`, and only then loaded. An image this
        host already holds under the checkpoint's name came from that same
        path (or is the original commit) and is reused.
        """
        try:
            restore.require_supported()
        except ValueError as exc:
            raise SandboxCheckpointError(str(exc)) from exc
        return self._load(
            checkpoint_image_reference(sandbox_id, restore.checkpoint_id),
            restore.base_image,
            lambda output: download(
                sandbox_id=sandbox_id,
                key=restore.object_key,
                version_id=restore.version_id,
                size=restore.size_bytes,
                sha256=restore.sha256,
                output=output,
                deadline=deadline,
            ),
            max_bytes=max_bytes,
            deadline=deadline,
        )

    def load_environment(
        self,
        snapshot: EnvironmentSnapshotRef,
        *,
        download: Callable[..., None],
        max_bytes: int,
        deadline: float,
    ) -> str:
        """Make an installation snapshot a local image and return its name.

        Checked and loaded exactly as a checkpoint restore, under the one name
        the snapshot may have, so its archive cannot retag another image.
        """
        return self._load(
            snapshot_image_reference(snapshot.owner, snapshot.snapshot_id),
            snapshot.base_image,
            lambda output: download(
                owner=snapshot.owner,
                snapshot_id=snapshot.snapshot_id,
                key=snapshot.object_key,
                version_id=snapshot.version_id,
                size=snapshot.size_bytes,
                sha256=snapshot.sha256,
                output=output,
                deadline=deadline,
            ),
            max_bytes=max_bytes,
            deadline=deadline,
        )

    def remove_environment_image(self, owner: str, snapshot_id: str) -> None:
        """Drop a deleted snapshot's local image, if this host has it."""
        self._remove_image(snapshot_image_reference(owner, snapshot_id))

    def environment_images(self) -> list[str]:
        """The snapshot ids of the installation snapshot images this host holds."""
        try:
            listed = self._run(
                [
                    self._docker,
                    "image",
                    "ls",
                    "--format",
                    "{{.Tag}}",
                    "--filter",
                    f"reference={SNAPSHOT_IMAGE_REPOSITORY}/*:*",
                ],
                capture_output=True,
                text=True,
                timeout=_IMAGE_REMOVE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxCheckpointError("snapshot images could not be listed") from exc
        if listed.returncode != 0:
            raise SandboxCheckpointError("snapshot images could not be listed")
        return sorted({tag for tag in listed.stdout.split() if valid_snapshot_id(tag)})

    def _load(
        self,
        reference: str,
        base_image: str,
        download_into: Callable[[IO[bytes]], None],
        *,
        max_bytes: int,
        deadline: float,
    ) -> str:
        if self._image_present(reference, deadline):
            return reference
        if not self._image_present(base_image, deadline):
            pulled = self._run(
                [self._docker, "image", "pull", "--quiet", base_image],
                capture_output=True,
                text=True,
                timeout=min(_PULL_TIMEOUT_S, self._remaining(deadline)),
                check=False,
            )
            if pulled.returncode != 0:
                raise SandboxCheckpointError("sandbox image could not be pulled for restore")
        base_layers = self.base_layers(base_image, deadline)
        self._spool_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Unnamed, like a capture's spool: the spool directory is durable, and
        # a process killed mid-restore runs no cleanup, so a named file would
        # stay there for good. Docker reads the checked archive on stdin.
        with (
            tempfile.TemporaryFile(dir=self._spool_dir, prefix="restore-") as downloaded,
            tempfile.TemporaryFile(dir=self._spool_dir, prefix="restore-") as prepared,
        ):
            try:
                download_into(downloaded)
            except SandboxArchiveDownloadError as exc:
                raise CheckpointArchiveInvalid(str(exc)) from exc
            downloaded.flush()
            downloaded.seek(0)
            try:
                prepare_restore_archive(
                    downloaded,
                    prepared,
                    reference=reference,
                    base_layers=base_layers,
                    max_bytes=max_bytes,
                )
            except SandboxCheckpointError as exc:
                raise CheckpointArchiveInvalid(str(exc)) from exc
            prepared.flush()
            prepared.seek(0)
            loaded = self._run(
                [self._docker, "image", "load", "--quiet"],
                stdin=prepared,
                capture_output=True,
                text=True,
                timeout=min(_LOAD_TIMEOUT_S, self._remaining(deadline)),
                check=False,
            )
        if loaded.returncode != 0 or not self._image_present(reference, deadline):
            raise SandboxCheckpointError("sandbox checkpoint could not be loaded")
        return reference

    def _image_present(self, image: str, deadline: float) -> bool:
        inspected = self._run(
            [self._docker, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=min(_IMAGE_REMOVE_TIMEOUT_S, self._remaining(deadline)),
            check=False,
        )
        return inspected.returncode == 0

    def _image_descriptor(self, image: str, deadline: float) -> dict[str, Any]:
        """Ask Docker which manifest or index an image is, as containerd stores it."""
        inspected = self._run(
            [self._docker, "image", "inspect", "--format", "{{json .Descriptor}}", image],
            capture_output=True,
            text=True,
            timeout=min(_IMAGE_REMOVE_TIMEOUT_S, self._remaining(deadline)),
            check=False,
        )
        if inspected.returncode != 0:
            raise CheckpointContentUnavailable("image descriptor is unavailable")
        try:
            descriptor = json.loads(inspected.stdout)
        except ValueError as exc:
            raise CheckpointContentUnavailable("image descriptor is not JSON") from exc
        return _descriptor_of(descriptor, "image")

    def base_layers(self, base_image: str, deadline: float) -> frozenset[str]:
        """Every layer digest ``base_image`` has on this host, for checking a restore.

        Read from containerd's content store when it is mounted, else from one
        ``docker image save`` of the image, remembered per image either way.
        """
        with self._base_lock:
            cached = self._base_layers.get(base_image)
        if cached is not None:
            return cached
        if self._content is not None and self._content.available():
            try:
                found = _walk_manifests(
                    [self._image_descriptor(base_image, deadline)], self._content.read
                )
            except CheckpointContentUnavailable:
                found = []
            layers = frozenset(
                layer for _digest, manifest in found for layer in _manifest_layers(manifest)
            )
            if layers:
                with self._base_lock:
                    self._base_layers[base_image] = layers
                return layers
        return self._base_image_layers(base_image, deadline, {})

    def _base_image_layers(
        self,
        base_image: str,
        deadline: float,
        durations: dict[str, int],
    ) -> frozenset[str]:
        with self._base_lock:
            cached = self._base_layers.get(base_image)
            if cached is not None:
                return cached
            started = self._clock()
            process = self._popen(
                [self._docker, "image", "save", base_image],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                assert process.stdout is not None
                stdout = process.stdout
                layers = self._read_before_deadline(
                    process,
                    deadline,
                    lambda: read_base_layers(stdout),
                    "sandbox image scan failed",
                )
            finally:
                self._reap(process)
            if not layers:
                raise SandboxCheckpointError("sandbox image has no layers")
            self._base_layers[base_image] = layers
            durations["base_scan"] = self._elapsed_ms(started)
            return layers

    def _export(
        self,
        reference: str,
        spool: BinaryIO,
        base_layers: frozenset[str],
        max_bytes: int,
        deadline: float,
    ) -> CheckpointArchive:
        process = self._popen(
            [self._docker, "image", "save", reference],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdout is not None
            stdout = process.stdout
            return self._read_before_deadline(
                process,
                deadline,
                lambda: export_checkpoint_archive(
                    stdout,
                    spool,
                    base_layers=base_layers,
                    max_bytes=max_bytes,
                ),
                "sandbox checkpoint export failed",
            )
        finally:
            self._reap(process)

    def _read_before_deadline(
        self,
        process: subprocess.Popen[bytes],
        deadline: float,
        read: Callable[[], _T],
        message: str,
    ) -> _T:
        """Consume a child's output and exit status, or kill it at the deadline."""
        with _Watchdog(process, self._remaining(deadline)) as watchdog:
            try:
                result = read()
                self._wait(process, deadline, message)
            except (tarfile.TarError, EOFError, OSError, SandboxCheckpointError) as exc:
                if watchdog.fired.is_set():
                    raise SandboxCheckpointError("sandbox checkpoint deadline expired") from exc
                if isinstance(exc, SandboxCheckpointError):
                    raise
                raise SandboxCheckpointError(message) from exc
        if watchdog.fired.is_set():
            raise SandboxCheckpointError("sandbox checkpoint deadline expired")
        return result

    def _wait(self, process: subprocess.Popen[bytes], deadline: float, message: str) -> None:
        try:
            returncode = process.wait(timeout=self._remaining(deadline))
        except subprocess.TimeoutExpired as exc:
            raise SandboxCheckpointError("sandbox checkpoint deadline expired") from exc
        if returncode != 0:
            raise SandboxCheckpointError(message)

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()

    def _remove_image(self, reference: str) -> None:
        try:
            removed = self._run(
                [self._docker, "image", "rm", reference],
                capture_output=True,
                text=True,
                timeout=_IMAGE_REMOVE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            removed = None
        if removed is None or removed.returncode != 0:
            logger.warning(
                "sandbox checkpoint image could not be removed",
                extra={"event": "sandbox_checkpoint_image_remove_failed"},
            )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise SandboxCheckpointError("sandbox checkpoint deadline expired")
        return remaining

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))


__all__ = [
    "CHECKPOINT_FORMAT_VERSION",
    "CHECKPOINT_IMAGE_REPOSITORY",
    "DEFAULT_CONTAINERD_CONTENT_DIR",
    "CheckpointArchive",
    "CheckpointContentUnavailable",
    "CheckpointRestore",
    "ContainerdContentStore",
    "SandboxCheckpointCapture",
    "SandboxCheckpointError",
    "SandboxCheckpointReceipt",
    "SandboxCheckpointStore",
    "checkpoint_image_reference",
    "export_checkpoint_archive",
    "export_from_content_store",
    "prepare_restore_archive",
    "read_base_layers",
]
