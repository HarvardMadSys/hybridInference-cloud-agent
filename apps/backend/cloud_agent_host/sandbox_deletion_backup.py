"""Stopped sandbox backups and durable receipts for verified S3 deletion."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.sandbox_deletion_diagnostics import log_discard_refusal
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost
from cloud_agent_host.upload_import import forget_deleted_container_uploads

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor

# Single-object S3 PUT maximum. A larger sandbox needs a separate multipart
# workflow; it must remain retained instead of silently creating a partial backup.
MAX_ARCHIVE_BYTES = 5 * 1024**3
# Preserve existing deletion receipts across the Sandbox Manager service rename.
DEFAULT_ARCHIVE_STATE_DIR = "/var/lib/hybridinference/broker-archives"
_HASH_CHUNK_BYTES = 1024 * 1024
# The archive is a tar of ``manifest.json`` and the root filesystem export. A
# prepared request keeps the export and the tar's bytes before it, not a second
# copy of the export inside a whole archive: the upload sends those bytes, the
# export, and the tar's end in turn. A request that an earlier version prepared
# keeps the whole archive as ``archive.tar``.
_ROOTFS_MEMBER = "container-rootfs.tar"
_ARCHIVE_HEAD = "archive.head"
# COMPAT(remove-after=2026-12-31): no host keeps a request an earlier version
# prepared; reading ``archive.tar`` goes then, with this name.
_LEGACY_ARCHIVE = "archive.tar"
_ARCHIVE_FILES = ("rootfs.partial", _ROOTFS_MEMBER, _ARCHIVE_HEAD, _LEGACY_ARCHIVE)


@dataclass(frozen=True)
class ArchiveIdentity:
    """Control-plane fence and immutable destination of one deletion request."""

    thread_id: str
    request_id: str
    generation: int
    expected_container_id: str
    archive_key: str


class SandboxDeletionBackupService:
    """Prepare, upload, and remove a stopped group through explicit receipts.

    Each phase appends its own immutable JSON record. The commit record is
    fsynced before Docker removal, allowing a lost response or sandbox manager restart
    to finish the exact operation safely. S3 verification is the authenticated
    control plane's responsibility and must happen before it requests commit.
    """

    def __init__(
        self,
        supervisor: SharedChatSessionSupervisor,
        *,
        state_dir: Path | None = None,
        max_bytes: int = MAX_ARCHIVE_BYTES,
    ) -> None:
        self._supervisor = supervisor
        self._state_dir = state_dir or Path(
            os.environ.get("AGENT_SANDBOX_ARCHIVE_STATE_DIR") or DEFAULT_ARCHIVE_STATE_DIR
        )
        self._max_bytes = min(max_bytes, MAX_ARCHIVE_BYTES)
        self._lock = threading.RLock()

    def _directory(self, identity: ArchiveIdentity) -> Path:
        request_hash = hashlib.sha256(identity.request_id.encode()).hexdigest()
        return self._state_dir / request_hash

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError("invalid receipt")
            return value
        except (OSError, ValueError) as exc:
            raise SandboxError("sandbox archive receipt is unreadable") from exc

    @staticmethod
    def _append(path: Path, value: dict[str, Any]) -> None:
        """Atomically append a phase without replacing an existing receipt."""
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise SessionLeaseLost("sandbox archive receipt changed") from None
            _sync_directory(path.parent)
        finally:
            os.unlink(temporary)

    @staticmethod
    def _identity_dict(identity: ArchiveIdentity) -> dict[str, Any]:
        return {
            "thread_id": identity.thread_id,
            "request_id": identity.request_id,
            "generation": identity.generation,
            "container_id": identity.expected_container_id,
            "archive_key": identity.archive_key,
        }

    def _prepared(self, identity: ArchiveIdentity) -> tuple[Path, dict[str, Any]]:
        directory = self._directory(identity)
        record = self._read(directory / "prepare.json")
        if record is None:
            raise SessionLeaseLost("sandbox archive must be prepared before this phase")
        if record.get("identity") != self._identity_dict(identity):
            raise SessionLeaseLost("sandbox archive request identity changed")
        if (directory / "abort.json").exists():
            raise SessionLeaseLost("sandbox archive request was aborted")
        return directory, record

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
        return digest.hexdigest()

    def _seal(self, directory: Path, rootfs: Path, manifest: bytes) -> tuple[str, int]:
        """Durably keep what the archive is sent from, and return its SHA-256 and size.

        The archive is never written out whole: it is the tar's bytes before
        the export, the export, and the tar's end, hashed here and sent in turn.
        """
        head = _archive_head(manifest, rootfs)
        rootfs_bytes = rootfs.stat().st_size
        tail = _archive_tail(len(head), rootfs_bytes)
        size_bytes = len(head) + rootfs_bytes + len(tail)
        if size_bytes > self._max_bytes:
            raise SandboxError("sandbox archive exceeds the S3 upload size limit")
        digest = hashlib.sha256()
        for chunk in _archive_chunks(head, rootfs, tail):
            digest.update(chunk)
        with (directory / _ARCHIVE_HEAD).open("wb") as output:
            os.chmod(directory / _ARCHIVE_HEAD, 0o600)
            output.write(head)
            output.flush()
            os.fsync(output.fileno())
        os.replace(rootfs, directory / _ROOTFS_MEMBER)
        # Both are durable, under these names, before a receipt names their archive.
        _sync_directory(directory)
        return digest.hexdigest(), size_bytes

    @staticmethod
    def _stored_archive(directory: Path) -> tuple[tuple[bytes | Path, ...], int | None]:
        """Return the pieces a prepared archive is sent from, and its size if they agree."""
        legacy = directory / _LEGACY_ARCHIVE
        if legacy.exists():
            return (legacy,), legacy.stat().st_size
        head = (directory / _ARCHIVE_HEAD).read_bytes()
        rootfs = directory / _ROOTFS_MEMBER
        rootfs_bytes = rootfs.stat().st_size
        if _declared_rootfs_bytes(head) != rootfs_bytes:
            return (), None
        tail = _archive_tail(len(head), rootfs_bytes)
        return (head, rootfs, tail), len(head) + rootfs_bytes + len(tail)

    @staticmethod
    def _remove_archive(directory: Path) -> None:
        for name in _ARCHIVE_FILES:
            (directory / name).unlink(missing_ok=True)

    @staticmethod
    def _match_receipt(record: dict[str, Any], sha256: str, size_bytes: int) -> None:
        receipt = record["receipt"]
        if receipt["sha256"] != sha256 or receipt["size_bytes"] != size_bytes:
            raise SessionLeaseLost("sandbox archive checksum or size changed")

    def prepare(self, identity: ArchiveIdentity) -> dict[str, Any]:
        """Export a complete filesystem and native-state bundle while stopped."""
        with self._lock:
            directory = self._directory(identity)
            if (directory / "abort.json").exists():
                raise SessionLeaseLost("sandbox archive request was aborted")
            if (directory / "prepare.json").exists():
                _directory, record = self._prepared(identity)
                if not (directory / "deleted.json").exists():
                    self._supervisor.begin_archive(
                        identity.thread_id,
                        request_id=identity.request_id,
                        generation=identity.generation,
                        container_id=identity.expected_container_id,
                    )
                return dict(record["receipt"])
            self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            pending = sum(
                1
                for candidate in self._state_dir.iterdir()
                if candidate.is_dir()
                and (candidate / "prepare.json").exists()
                and not (candidate / "deleted.json").exists()
                and not (candidate / "abort.json").exists()
            )
            if pending >= 4:
                raise SessionBusy("host already has four pending sandbox archives")
            directory.mkdir(mode=0o700, exist_ok=True)
            spec, manifest = self._supervisor.begin_archive(
                identity.thread_id,
                request_id=identity.request_id,
                generation=identity.generation,
                container_id=identity.expected_container_id,
            )
            rootfs = directory / "rootfs.partial"
            try:
                # Preserve the full immutable spec only in the private host
                # receipt. The S3 recovery manifest omits arbitrary labels,
                # mounts and network/host-path metadata.
                receipt_spec = manifest["spec"]
                manifest["spec"] = {**receipt_spec, "labels": {}}
                manifest.pop("inspection", None)
                with rootfs.open("wb") as output:
                    os.chmod(rootfs, 0o600)
                    docker_metadata = self._supervisor.backend(
                        identity.thread_id
                    ).manager.export_stopped_archive(
                        spec,
                        container_id=identity.expected_container_id,
                        output=output,
                        max_bytes=max(0, self._max_bytes - _HASH_CHUNK_BYTES),
                    )
                    # The export is part of the archive the receipt names.
                    output.flush()
                    os.fsync(output.fileno())
                manifest.update(
                    {
                        "format": "cloud-agent-sandbox-archive-v1",
                        "request_id": identity.request_id,
                        "docker_inspect": docker_metadata,
                        "rootfs_sha256": self._digest(rootfs),
                        "rootfs_size_bytes": rootfs.stat().st_size,
                        "credentials_tmpfs_excluded": True,
                    }
                )
                payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                sha256, size_bytes = self._seal(directory, rootfs, payload)
                receipt = {
                    "request_id": identity.request_id,
                    "container_id": identity.expected_container_id,
                    "archive_key": identity.archive_key,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                }
                self._append(
                    directory / "prepare.json",
                    {
                        "identity": self._identity_dict(identity),
                        "spec": receipt_spec,
                        "receipt": receipt,
                    },
                )
                return receipt
            except BaseException:
                self._remove_archive(directory)
                self._supervisor.abort_archive(
                    spec,
                    request_id=identity.request_id,
                    container_id=identity.expected_container_id,
                )
                raise
            finally:
                rootfs.unlink(missing_ok=True)

    def upload(
        self,
        identity: ArchiveIdentity,
        *,
        sha256: str,
        size_bytes: int,
        upload_url: str,
        upload_headers: dict[str, str],
    ) -> dict[str, Any]:
        """PUT the checksum-bound immutable object using a presigned URL only."""
        with self._lock:
            directory, record = self._prepared(identity)
            self._match_receipt(record, sha256, size_bytes)
            if (directory / "upload.json").exists():
                return {**record["receipt"], "uploaded": True}
            self._supervisor.begin_archive(
                identity.thread_id,
                request_id=identity.request_id,
                generation=identity.generation,
                container_id=identity.expected_container_id,
            )
            parsed = urlsplit(upload_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("sandbox archive upload requires an HTTPS presigned URL")
            headers = {key.lower(): value for key, value in upload_headers.items()}
            expected_headers = {
                "if-none-match": "*",
                "content-length": str(size_bytes),
                "content-type": "application/octet-stream",
                "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(sha256)).decode(),
            }
            if headers != expected_headers:
                raise ValueError("sandbox archive upload headers must bind checksum and size")
            pieces, stored_bytes = self._stored_archive(directory)
            # Not hashed again: the signed request carries the receipt's SHA-256,
            # and S3 refuses a body that does not match it.
            if stored_bytes != size_bytes:
                raise SandboxError("prepared sandbox archive changed on disk")
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(600, connect=30), follow_redirects=False
                ) as client:
                    response = client.put(
                        upload_url, headers=upload_headers, content=_archive_chunks(*pieces)
                    )
            except httpx.HTTPError:
                # Do not expose the signed URL or response body to logs/UI.
                raise SandboxError("sandbox archive S3 upload failed") from None
            if response.status_code not in (200, 201, 204, 412):
                raise SandboxError("sandbox archive S3 upload was rejected")
            # 412 means an immutable object already exists after a lost answer.
            # It still requires independent control-plane HEAD verification.
            self._append(directory / "upload.json", {"receipt": record["receipt"]})
            return {**record["receipt"], "uploaded": True}

    def commit(self, identity: ArchiveIdentity, *, sha256: str, size_bytes: int) -> dict[str, Any]:
        """Persist verified authorization, then remove only the stopped original."""
        with self._lock:
            directory, record = self._prepared(identity)
            self._match_receipt(record, sha256, size_bytes)
            if (directory / "deleted.json").exists():
                return {**record["receipt"], "deleted": True}
            if not (directory / "upload.json").exists():
                raise SessionLeaseLost("sandbox archive has not been uploaded")
            self._append(directory / "commit.json", {"receipt": record["receipt"]})
            self._supervisor.commit_archive(
                ChatSandboxSpec(**record["spec"]),
                request_id=identity.request_id,
                container_id=identity.expected_container_id,
            )
            self._append(directory / "deleted.json", {"receipt": record["receipt"]})
            self._remove_archive(directory)
            return {**record["receipt"], "deleted": True}

    def abort(self, identity: ArchiveIdentity) -> dict[str, Any]:
        """Release a failed preverification request while retaining its receipt."""
        with self._lock:
            directory = self._directory(identity)
            record = self._read(directory / "prepare.json")
            if record is None:
                # The abort may arrive before a delayed prepare or after a
                # crash while exporting. Persist its exact identity so that a
                # later arrival cannot install a barrier behind the control
                # plane's acknowledged cleanup.
                self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory.mkdir(mode=0o700, exist_ok=True)
                self._append(directory / "abort.json", {"identity": self._identity_dict(identity)})
                self._remove_archive(directory)
                return {
                    "request_id": identity.request_id,
                    "container_id": identity.expected_container_id,
                    "archive_key": identity.archive_key,
                    "aborted": True,
                }
            if record.get("identity") != self._identity_dict(identity):
                raise SessionLeaseLost("sandbox archive request identity changed")
            if (directory / "commit.json").exists():
                raise SessionLeaseLost("verified sandbox deletion cannot be aborted")
            self._supervisor.abort_archive(
                ChatSandboxSpec(**record["spec"]),
                request_id=identity.request_id,
                container_id=identity.expected_container_id,
            )
            self._append(directory / "abort.json", {"receipt": record["receipt"]})
            self._remove_archive(directory)
            return {
                "request_id": identity.request_id,
                "container_id": identity.expected_container_id,
                "archive_key": identity.archive_key,
                "aborted": True,
            }

    def discard(self, identity: ArchiveIdentity, *, sandbox_id: str) -> dict[str, Any]:
        """Remove the exact stopped container without exporting or uploading it.

        A refusal, or a receipt write the filesystem failed, is logged with the
        group state the fences read (:func:`log_discard_refusal`) and raised
        unchanged.
        """
        with self._lock:
            try:
                return self._discard(identity, sandbox_id=sandbox_id)
            except (SandboxError, ValueError, OSError) as exc:
                log_discard_refusal(
                    self._supervisor,
                    identity,
                    sandbox_id=sandbox_id,
                    directory=self._directory(identity),
                    exc=exc,
                )
                raise

    def _discard(self, identity: ArchiveIdentity, *, sandbox_id: str) -> dict[str, Any]:  # noqa: PLR0912 complexity baseline, #771
        """Remove the exact stopped container; :meth:`discard` reports a refusal.

        The authorization record is fsynced before Docker removal, so a lost
        response retries the same request. A request that already prepared an
        archive cannot be discarded. The durable sandbox id also lets it
        complete an already-absent target, but only after the supervisor proves
        both its exact id and stable name are absent.
        """
        if identity.archive_key:
            raise ValueError("sandbox discard does not upload an archive")
        with self._lock:
            directory = self._directory(identity)
            finished = self._read(directory / "discarded.json")
            if finished is not None:
                if finished.get("identity") != self._identity_dict(identity):
                    raise SessionLeaseLost("sandbox discard request identity changed")
                # Only a target-absent discard records the sandbox id, so a
                # receipt for a container this removed normally has none.
                recorded_sandbox_id = finished.get("sandbox_id")
                if recorded_sandbox_id is not None and recorded_sandbox_id != sandbox_id:
                    raise SessionLeaseLost("sandbox discard request identity changed")
                return self._discard_receipt(identity)
            if any(
                (directory / name).exists()
                for name in (
                    "prepare.json",
                    "upload.json",
                    "commit.json",
                    "deleted.json",
                    "abort.json",
                )
            ):
                raise SessionLeaseLost("sandbox archive request cannot be discarded")
            record = self._read(directory / "discard.json")
            if record is None:
                self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory.mkdir(mode=0o700, exist_ok=True)
                try:
                    spec, _manifest = self._supervisor.begin_archive(
                        identity.thread_id,
                        request_id=identity.request_id,
                        generation=identity.generation,
                        container_id=identity.expected_container_id,
                    )
                except SandboxError:
                    if not self._supervisor.admin_deletion_target_absent(
                        identity.thread_id,
                        sandbox_id=sandbox_id,
                        container_id=identity.expected_container_id,
                    ):
                        raise
                    record = {
                        "identity": self._identity_dict(identity),
                        "sandbox_id": sandbox_id,
                        "target_absent": True,
                    }
                else:
                    record = {"identity": self._identity_dict(identity), "spec": asdict(spec)}
                self._append(directory / "discard.json", record)
            elif record.get("identity") != self._identity_dict(identity):
                raise SessionLeaseLost("sandbox discard request identity changed")
            absent_sandbox_id: str | None = None
            if record.get("target_absent") is True:
                recorded_sandbox_id = record.get("sandbox_id")
                if (
                    not isinstance(recorded_sandbox_id, str)
                    or not recorded_sandbox_id
                    or recorded_sandbox_id != sandbox_id
                ):
                    raise SessionLeaseLost("sandbox discard request identity changed")
                if not self._supervisor.admin_deletion_target_absent(
                    identity.thread_id,
                    sandbox_id=recorded_sandbox_id,
                    container_id=identity.expected_container_id,
                    on_absent=forget_deleted_container_uploads,
                ):
                    raise SessionLeaseLost("sandbox discard target is no longer absent")
                absent_sandbox_id = recorded_sandbox_id
            else:
                self._supervisor.commit_archive(
                    ChatSandboxSpec(**record["spec"]),
                    request_id=identity.request_id,
                    container_id=identity.expected_container_id,
                )
            discarded: dict[str, Any] = {"identity": self._identity_dict(identity)}
            if absent_sandbox_id is not None:
                discarded["sandbox_id"] = absent_sandbox_id
            self._append(directory / "discarded.json", discarded)
            return self._discard_receipt(identity)

    @staticmethod
    def _discard_receipt(identity: ArchiveIdentity) -> dict[str, Any]:
        return {
            "request_id": identity.request_id,
            "container_id": identity.expected_container_id,
            "deleted": True,
        }


def _sync_directory(directory: Path) -> None:
    """Make the entries just made in ``directory`` durable."""
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _archive_head(manifest: bytes, rootfs: Path) -> bytes:
    """Return the archive's bytes before the export's content.

    They are what ``tarfile`` writes for ``manifest.json`` and then the
    export's own header, so with the export and :func:`_archive_tail` the
    archive is byte for byte the tar ``tarfile`` writes whole.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        info.mode = 0o600
        bundle.addfile(info, io.BytesIO(manifest))
        member = bundle.gettarinfo(rootfs, arcname=_ROOTFS_MEMBER)
        # The header ``bundle.add`` would write. Closing then ends the tar in
        # the buffer, which is left out: that end follows the export instead.
        return buffer.getvalue() + member.tobuf(bundle.format, bundle.encoding, bundle.errors)


def _declared_rootfs_bytes(head: bytes) -> int | None:
    """Return the export's size as its header at the end of ``head`` declares it."""
    try:
        member = tarfile.TarInfo.frombuf(
            head[-tarfile.BLOCKSIZE :], tarfile.ENCODING, "surrogateescape"
        )
    except tarfile.HeaderError:
        return None
    return member.size


def _archive_tail(head_bytes: int, rootfs_bytes: int) -> bytes:
    """Return the archive's bytes after the export: its padding and the tar's end."""
    padding = -rootfs_bytes % tarfile.BLOCKSIZE
    end = head_bytes + rootfs_bytes + padding + 2 * tarfile.BLOCKSIZE
    return bytes(padding + 2 * tarfile.BLOCKSIZE + -end % tarfile.RECORDSIZE)


def _archive_chunks(*pieces: bytes | Path) -> Iterator[bytes]:
    """Yield the archive's pieces in order, a file's a bounded chunk at a time."""
    for piece in pieces:
        if isinstance(piece, bytes):
            if piece:
                yield piece
            continue
        with piece.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                yield chunk
