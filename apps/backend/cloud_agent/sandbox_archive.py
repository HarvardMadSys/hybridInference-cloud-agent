"""Control-plane S3 storage for immutable, verified sandbox deletion archives.

AWS credentials are resolved only by the control plane's standard SDK chain.
The host receives a short-lived upload capability for one checksum-bound object,
never an AWS credential or a caller-selected bucket/key.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import re
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import quote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from cloud_agent.settings import text_setting

MAX_ARCHIVE_BYTES = 5 * 1024**3
UPLOAD_EXPIRES_SECONDS = 900
ARCHIVE_CONTENT_TYPE = "application/octet-stream"
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")


class SandboxArchiveError(RuntimeError):
    """An archive could not be prepared or verified; deletion must not proceed."""


class SandboxArchiveUnavailable(SandboxArchiveError):
    """No archive bucket is configured for this deployment."""


class SandboxArchiveVerificationError(SandboxArchiveError):
    """The stored object does not attest the expected complete archive."""


class _S3Client(Protocol):
    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, Any],
        ExpiresIn: int,
        HttpMethod: str,
    ) -> str: ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SandboxArchiveConfiguration:
    """Nonsecret archive settings; AWS credentials remain deployment-owned."""

    bucket: str = ""
    region: str = ""
    endpoint: str = ""
    prefix: str = "sandbox-archives"

    @classmethod
    def from_settings(cls) -> SandboxArchiveConfiguration:
        """Read the synchronized admin settings without contacting AWS."""
        return cls(
            bucket=text_setting("sandbox_archive.bucket"),
            region=text_setting("sandbox_archive.region"),
            endpoint=text_setting("sandbox_archive.endpoint"),
            prefix=text_setting("sandbox_archive.prefix"),
        )


@dataclass(frozen=True)
class ArchiveUpload:
    """One PUT capability; its required headers must be sent unchanged."""

    upload_url: str
    key: str
    headers: dict[str, str]
    expires_in_seconds: int = UPLOAD_EXPIRES_SECONDS


@dataclass(frozen=True)
class ArchiveReceipt:
    """Verified S3 identity and content digest safe to persist as an audit receipt."""

    archive_uri: str
    bucket: str
    key: str
    sha256: str
    size_bytes: int
    version_id: str | None = None


def _archive_digest(sha256: str, size_bytes: int) -> tuple[str, str]:
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise SandboxArchiveError("A complete SHA-256 digest is required for the archive")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
        or size_bytes > MAX_ARCHIVE_BYTES
    ):
        raise SandboxArchiveError("The archive must be between 1 byte and 5 GiB")
    return sha256.lower(), base64.b64encode(bytes.fromhex(sha256)).decode("ascii")


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or len(value.encode("utf-8")) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SandboxArchiveError("A valid sandbox and archive request identity are required")
    return quote(value, safe="")


class S3SandboxArchiveStore:
    """Prepare conditional S3 PUTs and verify service-computed SHA-256 checksums."""

    def __init__(
        self,
        configuration: SandboxArchiveConfiguration,
        *,
        client: _S3Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._client = client

    @classmethod
    def from_settings(cls) -> S3SandboxArchiveStore:
        """Snapshot current configuration; SDK creation is deferred until an action."""
        return cls(SandboxArchiveConfiguration.from_settings())

    @property
    def available(self) -> bool:
        """Whether a bucket is configured, independent of remote reachability."""
        return bool(self.configuration.bucket)

    def _require_available(self) -> None:
        if not self.available:
            raise SandboxArchiveUnavailable(
                "Configure the sandbox archive S3 bucket before deletion"
            )

    def _get_client(self) -> _S3Client:
        if self._client is None:
            self._client = cast(
                "_S3Client",
                boto3.client(
                    "s3",
                    region_name=self.configuration.region or None,
                    endpoint_url=self.configuration.endpoint or None,
                    config=Config(
                        signature_version="s3v4",
                        connect_timeout=5,
                        read_timeout=30,
                        retries={"max_attempts": 2, "mode": "standard"},
                        s3={"addressing_style": "path" if self.configuration.endpoint else "auto"},
                    ),
                ),
            )
        return self._client

    def key_for(self, sandbox_id: str, request_id: str) -> str:
        """Name the request object before the host produces its archive bytes."""
        self._require_available()
        key = "/".join(
            filter(
                None,
                (
                    self.configuration.prefix.strip("/"),
                    _identifier(sandbox_id),
                    _identifier(request_id),
                    "archive.tar",
                ),
            )
        )
        if len(key.encode("utf-8")) > 1024:
            raise SandboxArchiveError("The configured archive object key exceeds the S3 limit")
        return key

    async def prepare_upload(
        self, sandbox_id: str, request_id: str, sha256: str, size_bytes: int
    ) -> ArchiveUpload:
        """Sign one immutable, size-bound, checksum-bound single PUT archive."""
        self._require_available()
        _, checksum = _archive_digest(sha256, size_bytes)
        key = self.key_for(sandbox_id, request_id)
        headers = {
            "Content-Type": ARCHIVE_CONTENT_TYPE,
            "Content-Length": str(size_bytes),
            "x-amz-checksum-sha256": checksum,
            "If-None-Match": "*",
        }

        def prepare() -> str:
            return self._get_client().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.configuration.bucket,
                    "Key": key,
                    "ContentType": ARCHIVE_CONTENT_TYPE,
                    "ContentLength": size_bytes,
                    "ChecksumSHA256": checksum,
                    "IfNoneMatch": "*",
                },
                ExpiresIn=UPLOAD_EXPIRES_SECONDS,
                HttpMethod="PUT",
            )

        try:
            upload_url = await asyncio.to_thread(prepare)
        except (BotoCoreError, ClientError):
            raise SandboxArchiveError("Unable to prepare the sandbox archive upload") from None
        return ArchiveUpload(upload_url=upload_url, key=key, headers=headers)

    async def verify_archive(self, key: str, sha256: str, size_bytes: int) -> ArchiveReceipt:
        """Verify a current object key or an already trusted, persisted S3 receipt URI.

        A persisted URI keeps its original bucket and prefix when settings change.
        Callers must obtain that URI from the control-plane deletion record, never
        from a browser or host-selected location.
        """
        digest, checksum = _archive_digest(sha256, size_bytes)
        bucket = self.configuration.bucket
        persisted_uri = key.startswith("s3://")
        if persisted_uri:
            parsed = urlsplit(key)
            if (
                not _BUCKET.fullmatch(parsed.netloc)
                or parsed.query
                or parsed.fragment
                or not parsed.path.startswith("/")
            ):
                raise SandboxArchiveVerificationError("The persisted S3 archive URI is invalid")
            bucket = parsed.netloc
            key = parsed.path[1:]
        else:
            self._require_available()
        prefix = self.configuration.prefix.strip("/")
        relative = key if persisted_uri else (key.removeprefix(f"{prefix}/") if prefix else key)
        segments = relative.split("/")
        if (
            (not persisted_uri and prefix and not key.startswith(f"{prefix}/"))
            or (len(segments) < 3 if persisted_uri else len(segments) != 3)
            or not all(segments)
            or any(segment in {".", ".."} for segment in segments[:-1])
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
            or segments[-1] != "archive.tar"
            or len(key.encode("utf-8")) > 1024
        ):
            raise SandboxArchiveVerificationError("The archive object is outside the request scope")

        def inspect() -> dict[str, Any]:
            return self._get_client().head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")

        try:
            remote = await asyncio.to_thread(inspect)
        except (BotoCoreError, ClientError):
            raise SandboxArchiveVerificationError(
                "Unable to verify the sandbox archive in S3"
            ) from None
        actual_checksum = remote.get("ChecksumSHA256")
        actual_size = remote.get("ContentLength")
        if (
            not isinstance(actual_checksum, str)
            or not hmac.compare_digest(actual_checksum, checksum)
            or not isinstance(actual_size, int)
            or isinstance(actual_size, bool)
            or actual_size != size_bytes
            or remote.get("ChecksumType") not in {None, "FULL_OBJECT"}
        ):
            raise SandboxArchiveVerificationError("The S3 archive checksum or size does not match")
        version = remote.get("VersionId")
        return ArchiveReceipt(
            archive_uri=f"s3://{bucket}/{key}",
            bucket=bucket,
            key=key,
            sha256=digest,
            size_bytes=size_bytes,
            version_id=version if isinstance(version, str) else None,
        )
