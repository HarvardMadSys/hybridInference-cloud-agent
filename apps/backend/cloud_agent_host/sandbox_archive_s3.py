"""Bounded S3 storage for compressed sandbox archives.

The workspace broker is the trust boundary for this module.  It may export an
already-built ``tar.gz`` stream, but a sandbox never receives the S3 client or
its workload credentials.  Object names are generated here instead of being
accepted from a caller.  The API has no object-body read, restore, list, or
delete operation; its only read is an exact-key metadata check after an
immutable initial write loses an ``If-None-Match`` race.

``boto3`` is imported only when an upload needs a default client.  Tests and
other callers can inject the small protocol below, while a deployed broker uses
the SDK's ordinary workload-identity/default credential chain.  No static AWS
credential or endpoint option exists in this configuration.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Final, Literal, Protocol, cast

from cloud_agent_shared.sandbox_archive_settings import (
    DEFAULT_MAX_ARCHIVE_BYTES,
    DEFAULT_MULTIPART_PART_BYTES,
    DEFAULT_MULTIPART_THRESHOLD_BYTES,
    MAX_ARCHIVE_BYTES,
    MAX_MULTIPART_PART_BYTES,
    MAX_SINGLE_UPLOAD_BYTES,
    MIN_MULTIPART_PART_BYTES,
    SANDBOX_ARCHIVE_BUCKET,
    SANDBOX_ARCHIVE_PREFIX,
    SANDBOX_ARCHIVE_S3_URI,
    SandboxArchivePolicy,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

TIB: Final = 1024 * 1024 * 1024 * 1024
# Archive construction temporarily holds both the raw Docker tar streams and
# the compressed outer object. Keep the default modest enough for an ordinary
# host and cap operator tuning so one hostile workspace cannot consume an
# unbounded amount of broker scratch space.
MAX_S3_OBJECT_BYTES: Final = 5 * TIB
MAX_MULTIPART_PARTS: Final = 10_000
S3_CONNECT_TIMEOUT_S: Final = 5
S3_READ_TIMEOUT_S: Final = 30
S3_TOTAL_MAX_ATTEMPTS: Final = 2
S3_INITIAL_CONFLICT_MAX_ATTEMPTS: Final = 3

ArchivePhase = Literal["initial", "final"]
_ARCHIVE_PHASES: Final = frozenset(("initial", "final"))
_ACCOUNT_ID_RE: Final = re.compile(r"[0-9]{12}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_MAX_IDENTITY_BYTES: Final = 1024
_KMS_KEY_ARN_RE: Final = re.compile(
    r"arn:(?P<partition>[^:]+):kms:(?P<region>[a-z0-9-]+):"
    r"(?P<account>[0-9]{12}):key/(?P<key>[A-Za-z0-9-]+)"
)


class SandboxArchiveConfigError(ValueError):
    """The host's sandbox-archive destination is incomplete or unsafe."""


class SandboxArchiveUploadError(RuntimeError):
    """A sandbox archive could not be uploaded without violating its bounds."""


class SandboxArchiveDeadlineExceeded(SandboxArchiveUploadError):
    """The caller's archive work deadline has elapsed between bounded calls."""


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


@dataclass(frozen=True)
class SandboxArchiveS3Config:
    """Validated server-owned configuration for the one archive prefix."""

    region: str
    kms_key_arn: str
    expected_bucket_owner: str
    uri: str = SANDBOX_ARCHIVE_S3_URI
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    multipart_threshold_bytes: int = DEFAULT_MULTIPART_THRESHOLD_BYTES
    multipart_part_bytes: int = DEFAULT_MULTIPART_PART_BYTES
    revision: int = 0

    def __post_init__(self) -> None:
        """Reject unsafe direct construction as strictly as database policy parsing."""
        if self.uri != SANDBOX_ARCHIVE_S3_URI:
            raise SandboxArchiveConfigError(
                f"sandbox archive S3 URI must be exactly {SANDBOX_ARCHIVE_S3_URI}"
            )
        if not self.region or self.region.strip() != self.region:
            raise SandboxArchiveConfigError("sandbox archive region must be non-empty")
        if not _ACCOUNT_ID_RE.fullmatch(self.expected_bucket_owner):
            raise SandboxArchiveConfigError(
                "sandbox archive expected bucket owner must be a 12-digit AWS account id"
            )
        kms_match = _KMS_KEY_ARN_RE.fullmatch(self.kms_key_arn)
        if kms_match is None:
            raise SandboxArchiveConfigError(
                "sandbox archive KMS key must be a key ARN, not an alias"
            )
        if kms_match.group("region") != self.region:
            raise SandboxArchiveConfigError("the KMS key ARN and archive region must match")
        if kms_match.group("account") != self.expected_bucket_owner:
            raise SandboxArchiveConfigError(
                "the KMS key ARN account and expected bucket owner must match"
            )

        _validate_bounded_int(
            "sandbox archive max bytes",
            self.max_archive_bytes,
            minimum=1,
            maximum=min(MAX_ARCHIVE_BYTES, MAX_S3_OBJECT_BYTES),
        )
        _validate_bounded_int(
            "sandbox archive multipart threshold bytes",
            self.multipart_threshold_bytes,
            minimum=1,
            maximum=min(self.max_archive_bytes, MAX_SINGLE_UPLOAD_BYTES),
        )
        _validate_bounded_int(
            "sandbox archive multipart part bytes",
            self.multipart_part_bytes,
            minimum=MIN_MULTIPART_PART_BYTES,
            maximum=MAX_MULTIPART_PART_BYTES,
        )
        maximum_parts = (
            self.max_archive_bytes + self.multipart_part_bytes - 1
        ) // self.multipart_part_bytes
        if maximum_parts > MAX_MULTIPART_PARTS:
            raise SandboxArchiveConfigError(
                "archive max bytes and multipart part bytes could require more than "
                f"{MAX_MULTIPART_PARTS} S3 parts"
            )
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise SandboxArchiveConfigError(
                "sandbox archive policy revision must be a non-negative integer"
            )
        if self.revision < 0:
            raise SandboxArchiveConfigError(
                "sandbox archive policy revision must be a non-negative integer"
            )

    @property
    def bucket(self) -> str:
        """Return the fixed bucket; it is never caller-selected."""
        return SANDBOX_ARCHIVE_BUCKET

    @property
    def prefix(self) -> str:
        """Return the fixed object prefix; it is never caller-selected."""
        return SANDBOX_ARCHIVE_PREFIX

    @classmethod
    def from_policy(cls, policy: SandboxArchivePolicy) -> SandboxArchiveS3Config:
        """Build the broker's S3 client configuration from one validated DB policy."""
        if not policy.enabled:
            raise SandboxArchiveConfigError("sandbox archive policy is disabled")
        return cls(
            uri=SANDBOX_ARCHIVE_S3_URI,
            region=policy.region,
            kms_key_arn=policy.kms_key_arn,
            expected_bucket_owner=policy.expected_bucket_owner,
            max_archive_bytes=policy.max_archive_bytes,
            multipart_threshold_bytes=policy.multipart_threshold_bytes,
            multipart_part_bytes=policy.multipart_part_bytes,
            revision=policy.revision,
        )

    def as_policy(self) -> SandboxArchivePolicy:
        """Reconstruct the exact enabled policy carried by this immutable config."""
        return SandboxArchivePolicy(
            enabled=True,
            region=self.region,
            kms_key_arn=self.kms_key_arn,
            expected_bucket_owner=self.expected_bucket_owner,
            max_archive_bytes=self.max_archive_bytes,
            multipart_threshold_bytes=self.multipart_threshold_bytes,
            multipart_part_bytes=self.multipart_part_bytes,
            revision=self.revision,
        )


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
        Larger streams use consecutive multipart chunks.  Any failure after a
        multipart upload is created attempts an abort before the original
        exception is re-raised, unless the operation deadline already elapsed
        and another synchronous network request would extend it.
        """
        _validate_deadline(deadline)
        self._check_deadline(deadline)
        checked_phase = _validate_phase(phase)
        namespace_hash = _identity_hash("namespace", namespace)
        sandbox_hash = _identity_hash("sandbox id", sandbox_id)
        _validate_generation(generation)
        _validate_upload_size(size, maximum=self.config.max_archive_bytes)
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
        key = self._object_key(
            namespace_hash=namespace_hash,
            sandbox_hash=sandbox_hash,
            generation=generation,
            phase=checked_phase,
        )
        attempts = S3_INITIAL_CONFLICT_MAX_ATTEMPTS if checked_phase == "initial" else 1
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
        initial write but the broker or its caller lost the response.  A
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
        key = self._object_key(
            namespace_hash=namespace_hash,
            sandbox_hash=sandbox_hash,
            generation=generation,
            phase="initial",
        )
        self._require_bucket_versioning(deadline)
        try:
            existing = self._get_client().head_object(
                Bucket=self.config.bucket,
                Key=key,
                ExpectedBucketOwner=self.config.expected_bucket_owner,
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

    @staticmethod
    def _object_key(
        *,
        namespace_hash: str,
        sandbox_hash: str,
        generation: int,
        phase: ArchivePhase,
    ) -> str:
        return (
            f"{SANDBOX_ARCHIVE_PREFIX}/v1/{namespace_hash}/{sandbox_hash}/"
            f"g{generation}/{phase}.tar.gz"
        )

    def _check_deadline(self, deadline: float | None) -> None:
        _check_deadline(deadline, self._clock)

    def _require_bucket_versioning(self, deadline: float | None) -> None:
        """Fail closed before a write could replace stop history in place."""
        self._check_deadline(deadline)
        response = self._get_client().get_bucket_versioning(
            Bucket=self.config.bucket,
            ExpectedBucketOwner=self.config.expected_bucket_owner,
        )
        self._check_deadline(deadline)
        if response.get("Status") != "Enabled":
            raise SandboxArchiveUploadError("sandbox archive bucket versioning must be enabled")

    def _get_client(self) -> S3ArchiveClient:
        with self._client_lock:
            if self._closed:
                raise SandboxArchiveUploadError("sandbox archive S3 uploader is closed")
            if self._client is None:
                self._client = _default_s3_client(self.config.region)
            return self._client

    def _base_object_arguments(
        self,
        *,
        key: str,
        phase: ArchivePhase,
        size: int,
        digest: str,
    ) -> dict[str, Any]:
        return {
            "Bucket": self.config.bucket,
            "Key": key,
            "ContentType": "application/gzip",
            "Metadata": {
                "archive-phase": phase,
                "archive-format": "tar-gzip-v1",
                "archive-size-bytes": str(size),
                "archive-sha256": digest,
            },
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": self.config.kms_key_arn,
            "ExpectedBucketOwner": self.config.expected_bucket_owner,
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
            digest = hashlib.sha256()
            parts: list[dict[str, object]] = []
            remaining = size
            part_number = 1
            while remaining:
                self._check_deadline(deadline)
                chunk = _read_exact(
                    body,
                    min(remaining, self.config.multipart_part_bytes),
                    deadline=deadline,
                    clock=self._clock,
                )
                remaining -= len(chunk)
                digest.update(chunk)
                part_checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
                uploaded = client.upload_part(
                    Bucket=self.config.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                    ContentLength=len(chunk),
                    ChecksumAlgorithm="SHA256",
                    ChecksumSHA256=part_checksum,
                    ExpectedBucketOwner=self.config.expected_bucket_owner,
                )
                self._check_deadline(deadline)
                returned_checksum = _optional_response_string(uploaded, "ChecksumSHA256")
                if returned_checksum is not None and returned_checksum != part_checksum:
                    raise SandboxArchiveUploadError(
                        f"S3 returned a different SHA-256 checksum for part {part_number}"
                    )
                parts.append(
                    {
                        "ETag": _required_response_string(uploaded, "ETag"),
                        "PartNumber": part_number,
                        "ChecksumSHA256": part_checksum,
                    }
                )
                part_number += 1
            _require_eof(body, deadline=deadline, clock=self._clock)
            completed_digest = digest.digest()
            if completed_digest.hex() != expected_digest:
                raise SandboxArchiveUploadError("sandbox archive SHA-256 changed before upload")
            try:
                self._check_deadline(deadline)
                response = client.complete_multipart_upload(
                    Bucket=self.config.bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    ExpectedBucketOwner=self.config.expected_bucket_owner,
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
                        ExpectedBucketOwner=self.config.expected_bucket_owner,
                    )
                    self._check_deadline(deadline)
                except Exception:
                    # Preserve the failure that made the archive incomplete.
                    # A bucket lifecycle rule should also reap abandoned MPUs.
                    pass
            raise

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
        if phase != "initial" or not _is_precondition_failure(exc):
            return
        self._check_deadline(deadline)
        existing = self._get_client().head_object(
            Bucket=self.config.bucket,
            Key=key,
            ExpectedBucketOwner=self.config.expected_bucket_owner,
        )
        self._check_deadline(deadline)
        if not self._matching_initial_head(existing, size=size, digest=digest):
            raise SandboxArchiveUploadError(
                "existing initial sandbox archive does not match this retry"
            ) from exc
        raise SandboxArchiveAlreadyExists(bucket=self.config.bucket, key=key) from exc

    def _matching_initial_head(
        self,
        existing: Mapping[str, Any],
        *,
        size: int,
        digest: str,
    ) -> bool:
        metadata = existing.get("Metadata")
        try:
            _required_version_id(existing)
        except SandboxArchiveUploadError:
            return False
        return (
            isinstance(metadata, dict)
            and metadata.get("archive-phase") == "initial"
            and metadata.get("archive-format") == "tar-gzip-v1"
            and metadata.get("archive-size-bytes") == str(size)
            and metadata.get("archive-sha256") == digest
            and existing.get("ContentLength") == size
            and existing.get("ContentType") == "application/gzip"
            and existing.get("ServerSideEncryption") == "aws:kms"
            and existing.get("SSEKMSKeyId") == self.config.kms_key_arn
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


def _default_s3_client(region: str) -> S3ArchiveClient:
    try:
        boto3 = importlib.import_module("boto3")
        botocore_config = importlib.import_module("botocore.config")
    except ModuleNotFoundError as exc:
        raise SandboxArchiveUploadError(
            "boto3 and botocore are required when no sandbox archive S3 client is injected"
        ) from exc
    # Credentials still come exclusively from the SDK's workload-identity /
    # default chain. Explicit transport bounds prevent one S3 request from
    # outliving the broker's shutdown window indefinitely; service-level
    # retries remain separately bounded.
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
    return cast(
        "S3ArchiveClient",
        boto3.client("s3", region_name=region, config=sdk_config),
    )


def _validate_bounded_int(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SandboxArchiveConfigError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )


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
    return {"IfNoneMatch": "*"} if phase == "initial" else {}


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
    payload = bytearray()
    while len(payload) < size:
        _check_deadline(deadline, clock)
        chunk = body.read(size - len(payload))
        _check_deadline(deadline, clock)
        if chunk is None:
            raise SandboxArchiveUploadError("sandbox archive stream returned no bytes")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise SandboxArchiveUploadError("sandbox archive stream must return bytes")
        if len(chunk) == 0:
            raise SandboxArchiveUploadError("sandbox archive stream ended before its declared size")
        if len(chunk) > size - len(payload):
            raise SandboxArchiveUploadError("sandbox archive stream exceeded its requested read")
        payload.extend(chunk)
    return bytes(payload)


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
    "SandboxArchiveReceipt",
    "SandboxArchiveS3Config",
    "SandboxArchiveS3Uploader",
    "SandboxArchiveUploadError",
    "is_sandbox_archive_conditional_conflict",
]
