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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.session_lifecycle import SessionBusy, SessionLeaseLost

if TYPE_CHECKING:
    from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor

# Single-object S3 PUT maximum. A larger sandbox needs a separate multipart
# workflow; it must remain retained instead of silently creating a partial backup.
MAX_ARCHIVE_BYTES = 5 * 1024**3
DEFAULT_ARCHIVE_STATE_DIR = "/var/lib/hybridinference/broker-archives"
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ArchiveIdentity:
    """Control-plane fence and immutable destination of one deletion request."""

    thread_id: str
    request_id: str
    generation: int
    expected_container_id: str
    archive_key: str


class SandboxArchiveService:
    """Prepare, upload, and remove a stopped group through explicit receipts.

    Each phase appends its own immutable JSON record. The commit record is
    fsynced before Docker removal, allowing a lost response or broker restart
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
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
            archive = directory / "archive.tar"
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
                with archive.open("wb") as output:
                    os.chmod(archive, 0o600)
                    with tarfile.open(fileobj=output, mode="w") as bundle:
                        info = tarfile.TarInfo("manifest.json")
                        info.size = len(payload)
                        info.mode = 0o600
                        bundle.addfile(info, io.BytesIO(payload))
                        bundle.add(rootfs, arcname="container-rootfs.tar", recursive=False)
                    output.flush()
                    os.fsync(output.fileno())
                size_bytes = archive.stat().st_size
                if size_bytes > self._max_bytes:
                    raise SandboxError("sandbox archive exceeds the S3 upload size limit")
                receipt = {
                    "request_id": identity.request_id,
                    "container_id": identity.expected_container_id,
                    "archive_key": identity.archive_key,
                    "sha256": self._digest(archive),
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
                archive.unlink(missing_ok=True)
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
            archive = directory / "archive.tar"
            if archive.stat().st_size != size_bytes or self._digest(archive) != sha256:
                raise SandboxError("prepared sandbox archive changed on disk")
            try:
                with (
                    archive.open("rb") as stream,
                    httpx.Client(
                        timeout=httpx.Timeout(600, connect=30), follow_redirects=False
                    ) as client,
                ):
                    response = client.put(upload_url, headers=upload_headers, content=stream)
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
            (directory / "archive.tar").unlink(missing_ok=True)
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
                (directory / "rootfs.partial").unlink(missing_ok=True)
                (directory / "archive.tar").unlink(missing_ok=True)
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
            (directory / "archive.tar").unlink(missing_ok=True)
            return {
                "request_id": identity.request_id,
                "container_id": identity.expected_container_id,
                "archive_key": identity.archive_key,
                "aborted": True,
            }
