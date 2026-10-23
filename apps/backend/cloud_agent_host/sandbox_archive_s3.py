"""Bounded S3 storage for compressed sandbox archives.

The sandbox manager is the trust boundary for this module.  It may export an
already-built ``tar.gz`` stream, but a sandbox never receives the S3 client or
its workload credentials.  Object names are generated here instead of being
accepted from a caller.  The API has no object-body read, restore, list, or
delete operation; its only read is an exact-key metadata check after an
immutable initial write loses an ``If-None-Match`` race.

``boto3`` is imported only when an upload needs a default client.  Tests and
other callers can inject the small protocol below.  A deployed sandbox manager uses a
saved access key when the policy has one, and otherwise the SDK's ordinary
workload-identity/default credential chain.  There is still no custom endpoint
option: archive bytes go to the fixed AWS bucket.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import math
import os
import queue
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, BinaryIO, Final, Literal, Protocol, cast

from cloud_agent_shared.environment_snapshots import snapshot_object_key
from cloud_agent_shared.sandbox_archive_settings import (
    DEFAULT_MULTIPART_PART_BYTES,
    MAX_MULTIPART_PARTS,
    SANDBOX_ARCHIVE_BUCKET,
    SANDBOX_ARCHIVE_PREFIX,
    SANDBOX_ARCHIVE_S3_URI,
    SandboxArchivePolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from concurrent.futures import Future

S3_CONNECT_TIMEOUT_S: Final = 5
S3_READ_TIMEOUT_S: Final = 30
S3_TOTAL_MAX_ATTEMPTS: Final = 2
S3_INITIAL_CONFLICT_MAX_ATTEMPTS: Final = 3

ArchivePhase = Literal["initial", "final", "checkpoint"]
_ARCHIVE_PHASES: Final = frozenset(("initial", "final"))
# Phases written exactly once: a retry may only find byte-identical content.
_IMMUTABLE_PHASES: Final = frozenset(("initial", "checkpoint"))
_CHECKPOINT_ID_RE: Final = re.compile(r"[0-9a-f]{32}")
_CHECKPOINT_KEY_RE: Final = re.compile(
    re.escape(SANDBOX_ARCHIVE_PREFIX) + r"/v2/([0-9a-f]{64})/g[1-9][0-9]*/[0-9a-f]{32}\.oci\.tar"
)
_DOWNLOAD_CHUNK_BYTES: Final = 1024 * 1024
# Parts of one multipart upload in flight at once. Each stays in memory until
# S3 answers it, so an upload holds at most eight parts: 64 MiB at the default
# part size, 512 MiB at the largest. Eight connections also fit botocore's
# default pool of ten.
_PART_UPLOAD_WORKERS: Final = 8
CHECKPOINT_FORMAT: Final = "oci-layer-archive-v1"
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_MAX_IDENTITY_BYTES: Final = 1024


class SandboxArchiveConfigError(ValueError):
    """The host's sandbox-archive destination is incomplete or unsafe."""


class SandboxArchiveUploadError(RuntimeError):
    """A sandbox archive could not be uploaded without violating its bounds."""


class SandboxArchiveDeadlineExceeded(SandboxArchiveUploadError):
    """The caller's archive work deadline has elapsed between bounded calls."""


class SandboxArchiveDownloadError(RuntimeError):
    """A checkpoint object could not be read back exactly as recorded."""


class SandboxArchiveAlreadyExists(SandboxArchiveUploadError):
    """The deterministic initial key already holds the exact same archive."""

    def __init__(self, *, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.key = key
        super().__init__(f"initial sandbox archive already exists at s3://{bucket}/{key}")


class S3ArchiveClient(Protocol):
    """The narrow subset of the boto3 S3 client used by the uploader."""

    def get_bucket_versioning(self, **kwargs: Any) -> Mapping[str, Any]:
        """Read versioning state for the one fixed archive bucket."""

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Read metadata for one exact immutable-initial key."""

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Read one exact object version, as a checkpoint restore does."""

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Upload one object in a single request."""

    def create_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]:
        """Begin a multipart object upload."""

    def upload_part(self, **kwargs: Any) -> Mapping[str, Any]:
        """Upload one consecutively numbered multipart chunk."""

    def complete_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]:
        """Commit all uploaded chunks as one object."""

    def abort_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]:
        """Discard a multipart upload that did not complete."""

    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Delete one exact object version, as snapshot cleanup does."""


@dataclass(frozen=True)
class SandboxArchiveS3Config:
    """An enabled archive policy, and the credentials its uploads use.

    The policy was validated when it was parsed, so the only rule here is the
    one the resolved credentials add: without an access key, uploads use the
    host's workload identity, which needs the KMS key and the bucket owner.
    ``policy`` is kept with those credentials, as markers record it.
    """

    policy: SandboxArchivePolicy
    # A credential left as None is the policy's own.
    aws_access_key_id: str | None = field(default=None, repr=False)
    aws_secret_access_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Refuse a disabled policy, or workload identity without its KMS key and owner."""
        if not isinstance(self.policy, SandboxArchivePolicy) or not self.policy.enabled:
            raise SandboxArchiveConfigError("sandbox archive policy is disabled")
        key_id = (
            self.policy.aws_access_key_id
            if self.aws_access_key_id is None
            else self.aws_access_key_id
        )
        secret = (
            self.policy.aws_secret_access_key
            if self.aws_secret_access_key is None
            else self.aws_secret_access_key
        )
        if not key_id and not self.policy.expected_bucket_owner:
            raise SandboxArchiveConfigError(
                "sandbox archive expected bucket owner must be a 12-digit AWS account id"
            )
        if not key_id and not self.policy.kms_key_arn:
            raise SandboxArchiveConfigError(
                "sandbox archive KMS key must be a key ARN, not an alias"
            )
        object.__setattr__(self, "aws_access_key_id", key_id)
        object.__setattr__(self, "aws_secret_access_key", secret)
        # Validated by the policy's own rules, and compared with the config.
        object.__setattr__(
            self,
            "policy",
            replace(
                self.policy,
                access_key_auth=False,
                aws_access_key_id=key_id,
                aws_secret_access_key=secret,
            ),
        )

    @property
    def uri(self) -> str:
        """Return the fixed destination; it is never caller-selected."""
        return SANDBOX_ARCHIVE_S3_URI

    @property
    def bucket(self) -> str:
        """Return the fixed bucket; it is never caller-selected."""
        return SANDBOX_ARCHIVE_BUCKET

    @property
    def prefix(self) -> str:
        """Return the fixed object prefix; it is never caller-selected."""
        return SANDBOX_ARCHIVE_PREFIX

    @property
    def region(self) -> str:
        """Return the archive bucket's region."""
        return self.policy.region

    @property
    def kms_key_arn(self) -> str:
        """Return the KMS key new objects are encrypted with, if any."""
        return self.policy.kms_key_arn

    @property
    def expected_bucket_owner(self) -> str:
        """Return the account every request requires to own the bucket, if any."""
        return self.policy.expected_bucket_owner

    @property
    def max_archive_bytes(self) -> int:
        """Return the largest object this policy stores."""
        return self.policy.max_archive_bytes

    @property
    def multipart_threshold_bytes(self) -> int:
        """Return the size above which an upload is multipart."""
        return self.policy.multipart_threshold_bytes

    @property
    def multipart_part_bytes(self) -> int:
        """Return the size of each multipart part."""
        return self.policy.multipart_part_bytes

    @property
    def revision(self) -> int:
        """Return the policy revision this config was built from."""
        return self.policy.revision

    @classmethod
    def from_policy(
        cls,
        policy: SandboxArchivePolicy,
        *,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ) -> SandboxArchiveS3Config:
        """Build the config for one enabled policy, with its own or the given credentials."""
        return cls(
            policy=policy,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

    def as_policy(self) -> SandboxArchivePolicy:
        """Return the policy with this config's credentials, as markers record it."""
        return self.policy


@dataclass(frozen=True)
class SandboxArchiveReceipt:
    """Integrity and S3 identity returned after a completed archive upload."""

    phase: ArchivePhase
    bucket: str
    key: str
    size: int
    sha256: str
    etag: str
    version_id: str | None
    s3_checksum_sha256: str | None
    multipart: bool


class SandboxArchiveS3Uploader:
    """Write compressed sandbox archives to the fixed, KMS-protected prefix."""

    def __init__(
        self,
        config: SandboxArchiveS3Config,
        *,
        client: S3ArchiveClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(clock):
            raise TypeError("sandbox archive clock must be callable")
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._client_lock = threading.Lock()
        self._closed = False
        self._clock = clock

    def close(self) -> None:
        """Close an SDK client created by this uploader, including its pools."""
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
            owned = self._owns_client
        if client is None or not owned:
            return
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def upload(
        self,
        *,
        namespace: str,
        sandbox_id: str,
        generation: int,
        phase: ArchivePhase,
        body: BinaryIO,
        size: int,
        sha256: str | None = None,
        deadline: float | None = None,
    ) -> SandboxArchiveReceipt:
        """Upload exactly ``size`` bytes with transfer and application checksums.

        Streams at or below the configured threshold use one ``PutObject``.
        Larger streams use consecutively numbered multipart chunks, several
        sent at once.  Any failure after a multipart upload is created
        attempts an abort before the original exception is re-raised, unless
        the operation deadline already elapsed and another synchronous network
        request would extend it.
        """
        _validate_deadline(deadline)
        self._check_deadline(deadline)
        checked_phase = _validate_phase(phase)
        namespace_hash = _identity_hash("namespace", namespace)
        sandbox_hash = _identity_hash("sandbox id", sandbox_id)
        _validate_generation(generation)
        _validate_upload_size(size, maximum=self.config.max_archive_bytes)
        key = lifecycle_object_key(
            namespace_sha256=namespace_hash,
            sandbox_sha256=sandbox_hash,
            generation=generation,
            phase=checked_phase,
        )
        return self._upload_object(
            key=key,
            phase=checked_phase,
            body=body,
            size=size,
            sha256=sha256,
            deadline=deadline,
        )

    def upload_checkpoint(
        self,
        *,
        sandbox_id: str,
        generation: int,
        checkpoint_id: str,
        body: BinaryIO,
        size: int,
        sha256: str,
        deadline: float | None = None,
    ) -> SandboxArchiveReceipt:
        """Write one immutable checkpoint, treating an identical retry as success.

        A checkpoint key names a fresh random id, so an existing object can only
        be this capture's own earlier write whose response was lost. Its bytes
        must match exactly; the receipt then comes from that object's metadata.
        """
        _validate_deadline(deadline)
        self._check_deadline(deadline)
        _validate_upload_size(size, maximum=self.config.max_archive_bytes)
        digest = _validate_sha256(sha256)
        key = checkpoint_object_key(
            sandbox_id=sandbox_id,
            generation=generation,
            checkpoint_id=checkpoint_id,
        )
        return self._put_once(
            key=key,
            body=body,
            size=size,
            sha256=digest,
            deadline=deadline,
            what="sandbox checkpoint",
        )

    def download_checkpoint(
        self,
        *,
        sandbox_id: str,
        key: str,
        version_id: str | None,
        size: int,
        sha256: str,
        output: BinaryIO,
        deadline: float | None = None,
    ) -> None:
        """Copy one checkpoint object into ``output``, exactly as its receipt describes it.

        The size and digest come from the control plane's record of the
        receipt, so a missing, replaced or truncated object fails here rather
        than reaching ``docker load``. The named version is read when the
        receipt has one; checkpoint keys are written once either way.
        """
        _validate_deadline(deadline)
        self._check_deadline(deadline)
        _validate_upload_size(size, maximum=self.config.max_archive_bytes)
        _validate_sha256(sha256)
        matched = _CHECKPOINT_KEY_RE.fullmatch(key)
        if matched is None or matched.group(1) != _identity_hash("sandbox id", sandbox_id):
            raise SandboxArchiveDownloadError("sandbox checkpoint key does not name this sandbox")
        self._download_exact(
            key=key,
            version_id=version_id,
            size=size,
            sha256=sha256,
            output=output,
            deadline=deadline,
            what="sandbox checkpoint",
        )

    def _download_exact(
        self,
        *,
        key: str,
        version_id: str | None,
        size: int,
        sha256: str,
        output: BinaryIO,
        deadline: float | None,
        what: str,
    ) -> None:
        """Stream one object version, refusing any size or digest its record does not give."""
        _validate_deadline(deadline)
        self._check_deadline(deadline)
        _validate_upload_size(size, maximum=self.config.max_archive_bytes)
        digest = _validate_sha256(sha256)
        arguments: dict[str, Any] = {"Bucket": self.config.bucket, "Key": key}
        if version_id:
            arguments["VersionId"] = version_id
        response = self._get_client().get_object(**arguments, **self._owner_arguments())
        body = response.get("Body")
        if body is None or response.get("ContentLength") not in (None, size):
            raise SandboxArchiveDownloadError(f"{what} object has the wrong size")
        hasher = hashlib.sha256()
        copied = 0
        try:
            while True:
                self._check_deadline(deadline)
                chunk = body.read(min(_DOWNLOAD_CHUNK_BYTES, size - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > size:
                    raise SandboxArchiveDownloadError(f"{what} object is larger than its receipt")
                hasher.update(chunk)
                output.write(chunk)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if copied != size:
            raise SandboxArchiveDownloadError(f"{what} object is shorter than its receipt")
        if hasher.hexdigest() != digest:
            raise SandboxArchiveDownloadError(f"{what} object does not match its digest")

    def upload_environment_snapshot(
        self,
        *,
        owner: str,
        snapshot_id: str,
        body: BinaryIO,
        size: int,
        sha256: str,
        deadline: float | None = None,
    ) -> SandboxArchiveReceipt:
        """Write one immutable installation snapshot under its owner's key.

        Like a checkpoint, the key names a fresh id, so an existing object can
        only be this capture's own earlier write; it must match exactly.
        """
        _validate_deadline(deadline)
        self._check_deadline(deadline)
        _validate_upload_size(size, maximum=self.config.max_archive_bytes)
        digest = _validate_sha256(sha256)
        try:
            key = snapshot_object_key(owner, snapshot_id)
        except ValueError as exc:
            raise SandboxArchiveUploadError(str(exc)) from None
        return self._put_once(
            key=key,
            body=body,
            size=size,
            sha256=digest,
            deadline=deadline,
            what="environment snapshot",
        )

    def download_environment_snapshot(
        self,
        *,
        owner: str,
        snapshot_id: str,
        key: str,
        version_id: str | None,
        size: int,
        sha256: str,
        output: BinaryIO,
        deadline: float | None = None,
    ) -> None:
        """Copy one snapshot object into ``output``, exactly as its record describes it."""
        try:
            expected = snapshot_object_key(owner, snapshot_id)
        except ValueError:
            expected = None
        if key != expected:
            raise SandboxArchiveDownloadError("environment snapshot key does not name its owner")
        self._download_exact(
            key=key,
            version_id=version_id,
            size=size,
            sha256=sha256,
            output=output,
            deadline=deadline,
            what="environment snapshot",
        )

    def delete_environment_snapshot(
        self, *, owner: str, snapshot_id: str, key: str, version_id: str | None
    ) -> None:
        """Delete one snapshot object, its exact version when the record names one."""
        try:
            expected = snapshot_object_key(owner, snapshot_id)
        except ValueError:
            expected = None
        if key != expected:
            raise SandboxArchiveUploadError("environment snapshot key does not name its owner")
        arguments: dict[str, Any] = {"Bucket": self.config.bucket, "Key": key}
        if version_id:
            arguments["VersionId"] = version_id
        self._get_client().delete_object(**arguments, **self._owner_arguments())

    def _put_once(
        self,
        *,
        key: str,
        body: BinaryIO,
        size: int,
        sha256: str,
        deadline: float | None,
        what: str,
    ) -> SandboxArchiveReceipt:
        """Write an object whose key names a fresh id, treating an identical retry as success.

        An existing object under such a key can only be this capture's own
        earlier write whose response was lost. Its bytes must match exactly;
        the receipt then comes from that object's metadata.
        """
        try:
            return self._upload_object(
                key=key,
                phase="checkpoint",
                body=body,
                size=size,
                sha256=sha256,
                deadline=deadline,
            )
        except SandboxArchiveAlreadyExists:
            self._check_deadline(deadline)
            existing = self._get_client().head_object(
                Bucket=self.config.bucket,
                Key=key,
                **self._owner_arguments(),
            )
            self._check_deadline(deadline)
            if not self._matching_initial_head(
                existing, size=size, digest=sha256, phase="checkpoint"
            ):
                raise SandboxArchiveUploadError(
                    f"existing {what} does not match this retry"
                ) from None
            return SandboxArchiveReceipt(
                phase="checkpoint",
                bucket=self.config.bucket,
                key=key,
                size=size,
                sha256=sha256,
                etag=_required_response_string(existing, "ETag"),
                version_id=_required_version_id(existing),
                s3_checksum_sha256=_optional_response_string(existing, "ChecksumSHA256"),
                multipart=False,
            )

    def _upload_object(
        self,
        *,
        key: str,
        phase: ArchivePhase,
        body: BinaryIO,
        size: int,
        sha256: str | None,
        deadline: float | None,
    ) -> SandboxArchiveReceipt:
        """Upload one validated object, retrying only bounded write conflicts."""
        checked_phase = phase
        try:
            body_start = body.tell()
        except (AttributeError, OSError) as exc:
            raise SandboxArchiveUploadError("sandbox archive stream must be seekable") from exc
        expected_digest = (
            _validate_sha256(sha256)
            if sha256 is not None
            else _digest_and_rewind(body, size, deadline=deadline, clock=self._clock)
        )
        self._check_deadline(deadline)
        part_count = (size + self.config.multipart_part_bytes - 1) // (
            self.config.multipart_part_bytes
        )
        if part_count > MAX_MULTIPART_PARTS:
            raise SandboxArchiveUploadError(
                f"archive would require more than {MAX_MULTIPART_PARTS} multipart chunks"
            )
        self._require_bucket_versioning(deadline)
        attempts = S3_INITIAL_CONFLICT_MAX_ATTEMPTS if checked_phase in _IMMUTABLE_PHASES else 1
        for attempt in range(1, attempts + 1):
            try:
                if size <= self.config.multipart_threshold_bytes:
                    return self._single_upload(
                        phase=checked_phase,
                        body=body,
                        size=size,
                        key=key,
                        expected_digest=expected_digest,
                        deadline=deadline,
                    )
                return self._multipart_upload(
                    phase=checked_phase,
                    body=body,
                    size=size,
                    key=key,
                    expected_digest=expected_digest,
                    deadline=deadline,
                )
            except Exception as exc:
                # S3 distinguishes an existing conditional-write target (412,
                # handled by exact-key HEAD above) from a concurrent operation
                # (409). AWS requires retrying PutObject, or re-initiating and
                # fully re-uploading an MPU. Keep that race bounded by both an
                # attempt ceiling and the caller's work deadline. A synchronous
                # SDK request already in flight retains its own transport bound.
                if not is_sandbox_archive_conditional_conflict(exc) or attempt == attempts:
                    raise
                self._check_deadline(deadline)
                try:
                    body.seek(body_start)
                except (AttributeError, OSError) as seek_exc:
                    raise SandboxArchiveUploadError(
                        "sandbox archive stream could not be rewound after an S3 conflict"
                    ) from seek_exc
                self._check_deadline(deadline)
        raise AssertionError("unreachable S3 archive retry state")

    def initial_exists(
        self,
        *,
        namespace: str,
        sandbox_id: str,
        generation: int,
        size: int,
        sha256: str,
        deadline: float | None = None,
    ) -> bool:
        """Return whether the exact immutable baseline is already in S3.

        This metadata-only probe supports recovery after S3 committed an
        initial write but the sandbox manager or its caller lost the response.  A
        missing key is distinct from an object whose immutable identity does
        not match: absence returns ``False``; a mismatch fails closed.
        """
        _validate_deadline(deadline)
        self._check_deadline(deadline)
        namespace_hash = _identity_hash("namespace", namespace)
        sandbox_hash = _identity_hash("sandbox id", sandbox_id)
        _validate_generation(generation)
        _validate_upload_size(size, maximum=self.config.max_archive_bytes)
        digest = _validate_sha256(sha256)
        key = lifecycle_object_key(
            namespace_sha256=namespace_hash,
            sandbox_sha256=sandbox_hash,
            generation=generation,
            phase="initial",
        )
        self._require_bucket_versioning(deadline)
        try:
            existing = self._get_client().head_object(
                Bucket=self.config.bucket,
                Key=key,
                **self._owner_arguments(),
            )
            self._check_deadline(deadline)
        except Exception as exc:
            self._check_deadline(deadline)
            if _is_not_found(exc):
                return False
            raise
        if not self._matching_initial_head(existing, size=size, digest=digest):
            raise SandboxArchiveUploadError(
                "existing initial sandbox archive does not match its prepared marker"
            )
        return True

    def _check_deadline(self, deadline: float | None) -> None:
        _check_deadline(deadline, self._clock)

    def _require_bucket_versioning(self, deadline: float | None) -> None:
        """Fail closed before a write could replace stop history in place."""
        self._check_deadline(deadline)
        response = self._get_client().get_bucket_versioning(
            Bucket=self.config.bucket,
            **self._owner_arguments(),
        )
        self._check_deadline(deadline)
        if response.get("Status") != "Enabled":
            raise SandboxArchiveUploadError("sandbox archive bucket versioning must be enabled")

    def _get_client(self) -> S3ArchiveClient:
        with self._client_lock:
            if self._closed:
                raise SandboxArchiveUploadError("sandbox archive S3 uploader is closed")
            if self._client is None:
                self._client = _default_s3_client(
                    self.config.region,
                    aws_access_key_id=self.config.policy.aws_access_key_id,
                    aws_secret_access_key=self.config.policy.aws_secret_access_key,
                )
            return self._client

    def _owner_arguments(self) -> dict[str, str]:
        if not self.config.expected_bucket_owner:
            return {}
        return {"ExpectedBucketOwner": self.config.expected_bucket_owner}

    def _encryption_arguments(self) -> dict[str, str]:
        if self.config.kms_key_arn:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self.config.kms_key_arn,
            }
        return {"ServerSideEncryption": "AES256"}

    def _base_object_arguments(
        self,
        *,
        key: str,
        phase: ArchivePhase,
        size: int,
        digest: str,
    ) -> dict[str, Any]:
        content_type, archive_format = _object_format(phase)
        return {
            "Bucket": self.config.bucket,
            "Key": key,
            "ContentType": content_type,
            "Metadata": {
                "archive-phase": phase,
                "archive-format": archive_format,
                "archive-size-bytes": str(size),
                "archive-sha256": digest,
            },
            **self._encryption_arguments(),
            **self._owner_arguments(),
        }

    def _single_upload(
        self,
        *,
        phase: ArchivePhase,
        body: BinaryIO,
        size: int,
        key: str,
        expected_digest: str,
        deadline: float | None,
    ) -> SandboxArchiveReceipt:
        self._check_deadline(deadline)
        payload = _read_exact(body, size, deadline=deadline, clock=self._clock)
        _require_eof(body, deadline=deadline, clock=self._clock)
        digest = hashlib.sha256(payload).digest()
        if digest.hex() != expected_digest:
            raise SandboxArchiveUploadError("sandbox archive SHA-256 changed before upload")
        checksum = base64.b64encode(digest).decode("ascii")
        try:
            self._check_deadline(deadline)
            response = self._get_client().put_object(
                **self._base_object_arguments(
                    key=key,
                    phase=phase,
                    size=size,
                    digest=expected_digest,
                ),
                **_initial_write_condition(phase),
                Body=payload,
                ContentLength=size,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=checksum,
            )
            self._check_deadline(deadline)
        except Exception as exc:
            self._check_deadline(deadline)
            self._raise_if_matching_initial(
                exc,
                phase=phase,
                key=key,
                size=size,
                digest=expected_digest,
                deadline=deadline,
            )
            raise
        response_checksum = _optional_response_string(response, "ChecksumSHA256")
        if response_checksum is not None and response_checksum != checksum:
            raise SandboxArchiveUploadError("S3 returned a different SHA-256 checksum")
        return self._receipt(
            phase=phase,
            key=key,
            size=size,
            digest=digest,
            response=response,
            multipart=False,
        )

    def _multipart_upload(
        self,
        *,
        phase: ArchivePhase,
        body: BinaryIO,
        size: int,
        key: str,
        expected_digest: str,
        deadline: float | None,
    ) -> SandboxArchiveReceipt:
        self._check_deadline(deadline)
        client = self._get_client()
        self._check_deadline(deadline)
        created = client.create_multipart_upload(
            **self._base_object_arguments(
                key=key,
                phase=phase,
                size=size,
                digest=expected_digest,
            ),
            ChecksumAlgorithm="SHA256",
        )
        upload_id = _required_response_string(created, "UploadId")
        completed = False
        try:
            self._check_deadline(deadline)
            parts, completed_digest = self._upload_parts(
                client, key=key, upload_id=upload_id, body=body, size=size, deadline=deadline
            )
            if completed_digest.hex() != expected_digest:
                raise SandboxArchiveUploadError("sandbox archive SHA-256 changed before upload")
            try:
                self._check_deadline(deadline)
                response = client.complete_multipart_upload(
                    Bucket=self.config.bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    **self._owner_arguments(),
                    **_initial_write_condition(phase),
                )
                # Mark the MPU complete before checking time: a successful S3
                # commit cannot be aborted even when the response arrives late.
                completed = True
                self._check_deadline(deadline)
            except Exception as exc:
                self._check_deadline(deadline)
                self._raise_if_matching_initial(
                    exc,
                    phase=phase,
                    key=key,
                    size=size,
                    digest=expected_digest,
                    deadline=deadline,
                )
                raise
            return self._receipt(
                phase=phase,
                key=key,
                size=size,
                digest=completed_digest,
                response=response,
                multipart=True,
            )
        except BaseException:
            if not completed and _before_deadline(deadline, self._clock):
                try:
                    self._check_deadline(deadline)
                    client.abort_multipart_upload(
                        Bucket=self.config.bucket,
                        Key=key,
                        UploadId=upload_id,
                        **self._owner_arguments(),
                    )
                    self._check_deadline(deadline)
                except Exception:
                    # Preserve the failure that made the archive incomplete.
                    # A bucket lifecycle rule should also reap abandoned MPUs.
                    pass
            raise

    def _upload_parts(
        self,
        client: S3ArchiveClient,
        *,
        key: str,
        upload_id: str,
        body: BinaryIO,
        size: int,
        deadline: float | None,
    ) -> tuple[list[dict[str, object]], bytes]:
        """Send ``body`` as numbered parts, several at once; return them in order.

        Each worker reads its own part by offset, then checksums and sends it.
        This thread hashes every part as it is read, in order, so the object's
        SHA-256 is of exactly the bytes sent. A part starts only while fewer
        than ``_PART_UPLOAD_WORKERS`` are in flight. After a failure no further
        part starts, and the ones in flight finish before this raises, so an
        abort never races a part upload.
        """
        source = _PartReader(body)
        part_bytes = self.config.multipart_part_bytes
        digest = hashlib.sha256()
        handoff: queue.SimpleQueue[bytes | BaseException] = queue.SimpleQueue()
        stop = threading.Event()
        uploaded: dict[int, dict[str, object]] = {}
        in_flight: set[Future[tuple[int, dict[str, object]]]] = set()

        def settle(until: str) -> None:
            nonlocal in_flight
            done, in_flight = wait(in_flight, return_when=until)
            for future in done:
                number, part = future.result()
                uploaded[number] = part

        with ThreadPoolExecutor(
            max_workers=_PART_UPLOAD_WORKERS, thread_name_prefix="sandbox-archive-part"
        ) as pool:
            try:
                for number, offset in enumerate(range(0, size, part_bytes), start=1):
                    if len(in_flight) >= _PART_UPLOAD_WORKERS:
                        settle(FIRST_COMPLETED)
                    self._check_deadline(deadline)
                    sending = pool.submit(
                        self._send_part,
                        client,
                        key=key,
                        upload_id=upload_id,
                        number=number,
                        read=partial(source.read, offset, min(part_bytes, size - offset)),
                        handoff=handoff.put,
                        deadline=deadline,
                        stop=stop,
                    )
                    in_flight.add(sending)
                    digest.update(_handed_over(handoff.get()))
                source.require_end(size)
                while in_flight:
                    settle(FIRST_EXCEPTION)
            except BaseException:
                stop.set()
                raise
        return [uploaded[number] for number in sorted(uploaded)], digest.digest()

    def _send_part(
        self,
        client: S3ArchiveClient,
        *,
        key: str,
        upload_id: str,
        number: int,
        read: Callable[[], bytes],
        handoff: Callable[[bytes | BaseException], None],
        deadline: float | None,
        stop: threading.Event,
    ) -> tuple[int, dict[str, object]]:
        """Read one part, hand it over to be hashed, then checksum and send it."""
        try:
            chunk = read()
        except BaseException as exc:
            handoff(exc)
            raise
        handoff(chunk)
        part_checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
        if stop.is_set():
            raise SandboxArchiveUploadError("sandbox archive multipart upload already failed")
        self._check_deadline(deadline)
        uploaded = client.upload_part(
            Bucket=self.config.bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=number,
            Body=chunk,
            ContentLength=len(chunk),
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=part_checksum,
            **self._owner_arguments(),
        )
        self._check_deadline(deadline)
        returned_checksum = _optional_response_string(uploaded, "ChecksumSHA256")
        if returned_checksum is not None and returned_checksum != part_checksum:
            raise SandboxArchiveUploadError(
                f"S3 returned a different SHA-256 checksum for part {number}"
            )
        return number, {
            "ETag": _required_response_string(uploaded, "ETag"),
            "PartNumber": number,
            "ChecksumSHA256": part_checksum,
        }

    def _raise_if_matching_initial(
        self,
        exc: Exception,
        *,
        phase: ArchivePhase,
        key: str,
        size: int,
        digest: str,
        deadline: float | None,
    ) -> None:
        """Turn a 412 into idempotent success only for byte-identical state."""
        if phase not in _IMMUTABLE_PHASES or not _is_precondition_failure(exc):
            return
        self._check_deadline(deadline)
        existing = self._get_client().head_object(
            Bucket=self.config.bucket,
            Key=key,
            **self._owner_arguments(),
        )
        self._check_deadline(deadline)
        if not self._matching_initial_head(existing, size=size, digest=digest, phase=phase):
            raise SandboxArchiveUploadError(
                f"existing {phase} sandbox archive does not match this retry"
            ) from exc
        raise SandboxArchiveAlreadyExists(bucket=self.config.bucket, key=key) from exc

    def _matching_initial_head(
        self,
        existing: Mapping[str, Any],
        *,
        size: int,
        digest: str,
        phase: ArchivePhase = "initial",
    ) -> bool:
        metadata = existing.get("Metadata")
        try:
            _required_version_id(existing)
        except SandboxArchiveUploadError:
            return False
        content_type, archive_format = _object_format(phase)
        return (
            isinstance(metadata, dict)
            and metadata.get("archive-phase") == phase
            and metadata.get("archive-format") == archive_format
            and metadata.get("archive-size-bytes") == str(size)
            and metadata.get("archive-sha256") == digest
            and existing.get("ContentLength") == size
            and existing.get("ContentType") == content_type
            and (
                existing.get("ServerSideEncryption") == "aws:kms"
                and existing.get("SSEKMSKeyId") == self.config.kms_key_arn
                if self.config.kms_key_arn
                else existing.get("ServerSideEncryption") == "AES256"
            )
        )

    def _receipt(
        self,
        *,
        phase: ArchivePhase,
        key: str,
        size: int,
        digest: bytes,
        response: Mapping[str, Any],
        multipart: bool,
    ) -> SandboxArchiveReceipt:
        return SandboxArchiveReceipt(
            phase=phase,
            bucket=self.config.bucket,
            key=key,
            size=size,
            sha256=digest.hex(),
            etag=_required_response_string(response, "ETag"),
            # Bucket versioning was checked immediately before this write. A
            # missing VersionId means S3 did not provide proof that replacing
            # the deterministic final key retained the previous stop.
            version_id=_required_version_id(response),
            s3_checksum_sha256=_optional_response_string(response, "ChecksumSHA256"),
            multipart=multipart,
        )


def _default_s3_client(
    region: str,
    *,
    aws_access_key_id: str = "",
    aws_secret_access_key: str = "",
) -> S3ArchiveClient:
    try:
        boto3 = importlib.import_module("boto3")
        botocore_config = importlib.import_module("botocore.config")
    except ModuleNotFoundError as exc:
        raise SandboxArchiveUploadError(
            "boto3 and botocore are required when no sandbox archive S3 client is injected"
        ) from exc
    # Without a saved access key, credentials come from the SDK's
    # workload-identity / default chain. Explicit transport bounds prevent one
    # S3 request from outliving the sandbox manager's shutdown window indefinitely;
    # service-level retries remain separately bounded.
    sdk_config = botocore_config.Config(
        connect_timeout=S3_CONNECT_TIMEOUT_S,
        read_timeout=S3_READ_TIMEOUT_S,
        retries={"mode": "standard", "total_max_attempts": S3_TOTAL_MAX_ATTEMPTS},
        tcp_keepalive=True,
        # The archive destination is an AWS security boundary, not merely a
        # bucket/key convention.  Ignore AWS_ENDPOINT_URL[_S3] and shared
        # config endpoint overrides inherited from the host; otherwise an
        # unrelated development setting could redirect session archives to an
        # arbitrary S3-compatible service that accepts these request fields.
        ignore_configured_endpoint_urls=True,
    )
    client_kwargs: dict[str, Any] = {"region_name": region, "config": sdk_config}
    if aws_access_key_id:
        client_kwargs["aws_access_key_id"] = aws_access_key_id
        client_kwargs["aws_secret_access_key"] = aws_secret_access_key
    return cast("S3ArchiveClient", boto3.client("s3", **client_kwargs))


def _validate_phase(phase: str) -> ArchivePhase:
    if phase not in _ARCHIVE_PHASES:
        raise SandboxArchiveUploadError("sandbox archive phase must be 'initial' or 'final'")
    return cast("ArchivePhase", phase)


def _identity_hash(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise SandboxArchiveUploadError(f"sandbox archive {name} must be a string")
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > _MAX_IDENTITY_BYTES:
        raise SandboxArchiveUploadError(
            f"sandbox archive {name} must contain 1 to {_MAX_IDENTITY_BYTES} UTF-8 bytes"
        )
    return hashlib.sha256(encoded).hexdigest()


def _validate_generation(generation: int) -> None:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise SandboxArchiveUploadError("sandbox archive generation must be a positive integer")


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SandboxArchiveUploadError(
            "sandbox archive SHA-256 must be 64 lowercase hexadecimal characters"
        )
    return value


def _digest_and_rewind(
    body: BinaryIO,
    size: int,
    *,
    deadline: float | None,
    clock: Callable[[], float],
) -> str:
    """Hash one bounded seekable body before S3 metadata is committed."""
    try:
        start = body.tell()
    except (AttributeError, OSError) as exc:
        raise SandboxArchiveUploadError("sandbox archive stream must be seekable") from exc
    digest = hashlib.sha256()
    remaining = size
    try:
        while remaining:
            _check_deadline(deadline, clock)
            chunk = _read_exact(
                body,
                min(remaining, DEFAULT_MULTIPART_PART_BYTES),
                deadline=deadline,
                clock=clock,
            )
            remaining -= len(chunk)
            digest.update(chunk)
        _require_eof(body, deadline=deadline, clock=clock)
    finally:
        try:
            body.seek(start)
        except (AttributeError, OSError) as exc:
            raise SandboxArchiveUploadError("sandbox archive stream could not be rewound") from exc
    return digest.hexdigest()


def _initial_write_condition(phase: ArchivePhase) -> dict[str, str]:
    return {"IfNoneMatch": "*"} if phase in _IMMUTABLE_PHASES else {}


def _object_format(phase: ArchivePhase) -> tuple[str, str]:
    """Return the content type and format tag a phase's objects carry."""
    if phase == "checkpoint":
        return "application/x-tar", CHECKPOINT_FORMAT
    return "application/gzip", "tar-gzip-v1"


def lifecycle_object_key(
    *, namespace_sha256: str, sandbox_sha256: str, generation: int, phase: str
) -> str:
    """Return the key of one lifecycle point's archive: a host's sandbox, at one generation."""
    return (
        f"{SANDBOX_ARCHIVE_PREFIX}/v1/{namespace_sha256}/{sandbox_sha256}/"
        f"g{generation}/{phase}.tar.gz"
    )


def checkpoint_object_key(*, sandbox_id: str, generation: int, checkpoint_id: str) -> str:
    """Return the per-sandbox checkpoint key; any host may later restore it.

    Unlike lifecycle archives, the key omits the host namespace: a checkpoint
    belongs to the sandbox, not to the machine that captured it.
    """
    sandbox_hash = _identity_hash("sandbox id", sandbox_id)
    _validate_generation(generation)
    if not isinstance(checkpoint_id, str) or _CHECKPOINT_ID_RE.fullmatch(checkpoint_id) is None:
        raise SandboxArchiveUploadError("sandbox checkpoint id must be 32 lowercase hex digits")
    return f"{SANDBOX_ARCHIVE_PREFIX}/v2/{sandbox_hash}/g{generation}/{checkpoint_id}.oci.tar"


def _is_precondition_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = error.get("Code") if isinstance(error, dict) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return code == "PreconditionFailed" or status == 412


def is_sandbox_archive_conditional_conflict(exc: Exception) -> bool:
    """Recognize S3's retryable race for an If-None-Match write."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = error.get("Code") if isinstance(error, dict) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return code == "ConditionalRequestConflict" or status == 409


def _is_not_found(exc: Exception) -> bool:
    """Recognize an exact-key HEAD whose object does not exist."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = error.get("Code") if isinstance(error, dict) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    # An explicit service code is more authoritative than the generic HTTP
    # status. In particular, NoSuchBucket is also a 404 but must fail closed;
    # it is not proof that this exact object alone is absent.
    if code is not None:
        return code in {"404", "NoSuchKey", "NotFound"}
    return status == 404


def _validate_upload_size(size: int, *, maximum: int) -> None:
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= maximum:
        raise SandboxArchiveUploadError(
            f"sandbox archive size must be between 0 and {maximum} bytes"
        )


def _validate_deadline(deadline: float | None) -> None:
    if deadline is None:
        return
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise SandboxArchiveUploadError(
            "sandbox archive deadline must be a finite monotonic timestamp"
        )


def _check_deadline(deadline: float | None, clock: Callable[[], float]) -> None:
    if deadline is None:
        return
    now = clock()
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise SandboxArchiveUploadError("sandbox archive clock returned a non-finite timestamp")
    if now >= deadline:
        raise SandboxArchiveDeadlineExceeded("sandbox archive operation deadline exceeded")


def _before_deadline(deadline: float | None, clock: Callable[[], float]) -> bool:
    try:
        _check_deadline(deadline, clock)
    except SandboxArchiveDeadlineExceeded:
        return False
    return True


def _read_exact(
    body: BinaryIO,
    size: int,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """Read exactly ``size`` bytes, without copying a read that returns them all."""
    pieces: list[bytes | bytearray | memoryview] = []
    received = 0
    while received < size:
        _check_deadline(deadline, clock)
        chunk = body.read(size - received)
        _check_deadline(deadline, clock)
        if chunk is None:
            raise SandboxArchiveUploadError("sandbox archive stream returned no bytes")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise SandboxArchiveUploadError("sandbox archive stream must return bytes")
        if len(chunk) == 0:
            raise SandboxArchiveUploadError("sandbox archive stream ended before its declared size")
        if len(chunk) > size - received:
            raise SandboxArchiveUploadError("sandbox archive stream exceeded its requested read")
        pieces.append(chunk)
        received += len(chunk)
    if len(pieces) == 1 and isinstance(pieces[0], bytes):
        return pieces[0]
    return b"".join(pieces)


def _require_eof(
    body: BinaryIO,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    _check_deadline(deadline, clock)
    trailing = body.read(1)
    _check_deadline(deadline, clock)
    if trailing is None:
        raise SandboxArchiveUploadError("sandbox archive stream returned no bytes")
    if not isinstance(trailing, (bytes, bytearray, memoryview)):
        raise SandboxArchiveUploadError("sandbox archive stream must return bytes")
    if trailing:
        raise SandboxArchiveUploadError("sandbox archive stream exceeds its declared size")


class _PartReader:
    """Read a body's parts by offset, from several threads at once.

    A file is read with ``os.pread``, which leaves its shared position alone,
    so a part is one read and no copy. Any other stream is read under a lock.
    """

    def __init__(self, body: BinaryIO) -> None:
        self._body = body
        self._lock = threading.Lock()
        self._start = body.tell()
        try:
            # Bytes still in a writer's buffer are not yet in the file.
            body.flush()
            self._fd: int | None = body.fileno()
        except (AttributeError, OSError):
            self._fd = None

    def read(self, offset: int, length: int) -> bytes:
        """Return the ``length`` bytes at ``offset`` past where the body started."""
        if self._fd is None:
            with self._lock:
                self._body.seek(self._start + offset)
                return _read_exact(self._body, length)
        pieces: list[bytes] = []
        while length:
            piece = os.pread(self._fd, length, self._start + offset)
            if not piece:
                raise SandboxArchiveUploadError(
                    "sandbox archive stream ended before its declared size"
                )
            pieces.append(piece)
            offset += len(piece)
            length -= len(piece)
        return pieces[0] if len(pieces) == 1 else b"".join(pieces)

    def require_end(self, size: int) -> None:
        """Refuse a body that has bytes past its declared ``size``."""
        if self._fd is None:
            with self._lock:
                self._body.seek(self._start + size)
                _require_eof(self._body)
        elif os.pread(self._fd, 1, self._start + size):
            raise SandboxArchiveUploadError("sandbox archive stream exceeds its declared size")


def _handed_over(read: bytes | BaseException) -> bytes:
    """Return a part a worker read, or raise what stopped it reading."""
    if isinstance(read, BaseException):
        raise read
    return read


def _required_response_string(response: Mapping[str, Any], name: str) -> str:
    value = _optional_response_string(response, name)
    if value is None:
        raise SandboxArchiveUploadError(f"S3 response is missing {name}")
    return value


def _required_version_id(response: Mapping[str, Any]) -> str:
    """Require proof that S3 committed a non-null object version."""
    value = _required_response_string(response, "VersionId")
    if value.strip() != value or len(value) > 1024 or value.lower() == "null":
        raise SandboxArchiveUploadError(
            "S3 returned an invalid or null VersionId for a versioned sandbox archive"
        )
    return value


def _optional_response_string(response: Mapping[str, Any], name: str) -> str | None:
    value = response.get(name)
    return value if isinstance(value, str) and value else None


__all__ = [
    "ArchivePhase",
    "S3ArchiveClient",
    "SandboxArchiveAlreadyExists",
    "SandboxArchiveConfigError",
    "SandboxArchiveDeadlineExceeded",
    "SandboxArchiveDownloadError",
    "SandboxArchiveReceipt",
    "SandboxArchiveS3Config",
    "SandboxArchiveS3Uploader",
    "SandboxArchiveUploadError",
    "is_sandbox_archive_conditional_conflict",
    "lifecycle_object_key",
]
