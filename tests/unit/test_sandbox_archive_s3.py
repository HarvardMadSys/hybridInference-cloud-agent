"""Focused tests for the host-side S3 archive boundary."""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host import sandbox_archive_s3 as archive_s3
from cloud_agent_host.sandbox_archive_s3 import (
    SandboxArchiveAlreadyExists,
    SandboxArchiveConfigError,
    SandboxArchiveDeadlineExceeded,
    SandboxArchiveReceipt,
    SandboxArchiveS3Config,
    SandboxArchiveS3Uploader,
    SandboxArchiveUploadError,
    checkpoint_object_key,
)
from cloud_agent_shared.sandbox_archive_settings import (
    MAX_ARCHIVE_BYTES,
    MIN_MULTIPART_PART_BYTES,
    SANDBOX_ARCHIVE_S3_URI,
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_KMS_KEY = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc"
_NAMESPACE = "pool-east"
_SANDBOX_ID = "sandbox-123"
_GENERATION = 7
_NAMESPACE_HASH = hashlib.sha256(_NAMESPACE.encode()).hexdigest()
_SANDBOX_HASH = hashlib.sha256(_SANDBOX_ID.encode()).hexdigest()


class PreconditionFailure(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }
        super().__init__("precondition failed")


class ConditionalConflict(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "ConditionalRequestConflict"},
            "ResponseMetadata": {"HTTPStatusCode": 409},
        }
        super().__init__("conditional request conflict")


class ObjectNotFound(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        super().__init__("object not found")


class BucketNotFound(RuntimeError):
    def __init__(self) -> None:
        self.response = {
            "Error": {"Code": "NoSuchBucket"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        super().__init__("bucket not found")


class RecordingS3Client:
    def __init__(
        self,
        *,
        fail_part: int | None = None,
        fail_complete: bool = False,
        put_error: Exception | None = None,
        complete_error: Exception | None = None,
        head_response: dict[str, Any] | None = None,
        head_error: Exception | None = None,
        versioning_response: dict[str, Any] | None = None,
        versioning_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_part = fail_part
        self.fail_complete = fail_complete
        self.put_error = put_error
        self.complete_error = complete_error
        self.head_response = head_response
        self.head_error = head_error
        self.versioning_response = (
            {"Status": "Enabled"} if versioning_response is None else versioning_response
        )
        self.versioning_error = versioning_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_bucket_versioning", kwargs))
        if self.versioning_error is not None:
            raise self.versioning_error
        return self.versioning_response

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("head_object", kwargs))
        if self.head_error is not None:
            raise self.head_error
        if self.head_response is None:
            raise AssertionError("test did not configure an exact-key HEAD response")
        return self.head_response

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        if self.put_error is not None:
            raise self.put_error
        return {
            "ETag": '"single-etag"',
            "VersionId": "version-1",
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
        }

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_multipart_upload", kwargs))
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("upload_part", kwargs))
        if kwargs["PartNumber"] == self.fail_part:
            raise RuntimeError("simulated part failure")
        return {
            "ETag": f'"part-{kwargs["PartNumber"]}"',
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
        }

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("complete_multipart_upload", kwargs))
        if self.complete_error is not None:
            raise self.complete_error
        if self.fail_complete:
            raise RuntimeError("simulated completion failure")
        return {
            "ETag": '"multipart-etag"',
            "VersionId": "version-2",
            "ChecksumSHA256": "composite-checksum-2",
        }

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("abort_multipart_upload", kwargs))
        return {}

    def one_call(self, name: str) -> dict[str, Any]:
        calls = [kwargs for method, kwargs in self.calls if method == name]
        assert len(calls) == 1
        return calls[0]


def _config(**overrides: Any) -> SandboxArchiveS3Config:
    return SandboxArchiveS3Config.from_policy(_policy(**{"revision": 0, **overrides}))


def _policy(**overrides: Any) -> SandboxArchivePolicy:
    values: dict[str, Any] = {
        "enabled": True,
        "region": _REGION,
        "kms_key_arn": _KMS_KEY,
        "expected_bucket_owner": _ACCOUNT,
        "revision": 7,
    }
    values.update(overrides)
    return SandboxArchivePolicy(**values)


def _key(phase: str, *, generation: int = _GENERATION) -> str:
    return f"cloud-agent/sandbox/v1/{_NAMESPACE_HASH}/{_SANDBOX_HASH}/g{generation}/{phase}.tar.gz"


def _matching_head(payload: bytes) -> dict[str, Any]:
    return {
        "ContentLength": len(payload),
        "ContentType": "application/gzip",
        "Metadata": {
            "archive-phase": "initial",
            "archive-format": "tar-gzip-v1",
            "archive-size-bytes": str(len(payload)),
            "archive-sha256": hashlib.sha256(payload).hexdigest(),
        },
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": _KMS_KEY,
        "VersionId": "version-existing",
    }


def _upload(
    uploader: SandboxArchiveS3Uploader,
    *,
    phase: Any,
    body: io.BytesIO,
    size: int,
    namespace: str = _NAMESPACE,
    sandbox_id: str = _SANDBOX_ID,
    generation: int = _GENERATION,
    deadline: float | None = None,
) -> SandboxArchiveReceipt:
    return uploader.upload(
        namespace=namespace,
        sandbox_id=sandbox_id,
        generation=generation,
        phase=phase,
        body=body,
        size=size,
        deadline=deadline,
    )


def test_enabled_policy_builds_config_for_the_exact_fixed_destination() -> None:
    config = SandboxArchiveS3Config.from_policy(_policy())

    assert config.uri == SANDBOX_ARCHIVE_S3_URI
    assert config.bucket == "freeinference"
    assert config.prefix == "cloud-agent/sandbox"
    assert config.max_archive_bytes == 2 * 1024 * 1024 * 1024


def test_disabled_policy_cannot_create_an_s3_service_config() -> None:
    with pytest.raises(SandboxArchiveConfigError, match="disabled"):
        SandboxArchiveS3Config.from_policy(SandboxArchivePolicy())


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"expected_bucket_owner": "123"}, "12-digit"),
        (
            {"kms_key_arn": "arn:aws:kms:us-west-2:123456789012:key/12345678"},
            "region",
        ),
        (
            {"kms_key_arn": "arn:aws:kms:us-east-1:999999999999:key/12345678"},
            "account",
        ),
        ({"max_archive_bytes": -1}, "between"),
        ({"max_archive_bytes": MAX_ARCHIVE_BYTES + 1}, "between"),
    ],
)
def test_config_rejects_destination_and_bound_weakening(
    updates: dict[str, Any], message: str
) -> None:
    """A config holds a parsed policy, so weakening it is refused while parsing.

    The destination is no field of either: a config always writes to the fixed
    bucket, and a policy naming any other ``s3_uri`` does not parse.
    """
    with pytest.raises(SandboxArchivePolicyError, match=message):
        SandboxArchiveS3Config.from_policy(_policy(**updates))


@pytest.mark.parametrize("missing", ["expected_bucket_owner", "kms_key_arn"])
def test_workload_identity_needs_the_kms_key_and_bucket_owner(missing: str) -> None:
    """Credentials resolved for a pinned policy can leave an access-key policy without a key."""
    policy = _policy(
        **{missing: ""},
        aws_access_key_id=_ACCESS_KEY_ID,
        aws_secret_access_key=_SECRET_ACCESS_KEY,
    )
    assert SandboxArchiveS3Config.from_policy(policy).aws_access_key_id == _ACCESS_KEY_ID
    with pytest.raises(SandboxArchiveConfigError):
        SandboxArchiveS3Config.from_policy(policy, aws_access_key_id="", aws_secret_access_key="")


@pytest.mark.parametrize("missing", [(), ("expected_bucket_owner", "kms_key_arn")])
def test_a_config_given_no_credentials_keeps_the_policys_access_key(
    missing: tuple[str, ...],
) -> None:
    """Built directly, a config uses the policy's own key, as ``from_policy`` does.

    With the KMS key and owner it is not workload identity, and without them
    it is not refused.
    """
    policy = _policy(
        **dict.fromkeys(missing, ""),
        aws_access_key_id=_ACCESS_KEY_ID,
        aws_secret_access_key=_SECRET_ACCESS_KEY,
    )

    config = SandboxArchiveS3Config(policy)

    assert config == SandboxArchiveS3Config.from_policy(policy)
    assert config.aws_access_key_id == _ACCESS_KEY_ID
    assert config.aws_secret_access_key == _SECRET_ACCESS_KEY
    assert config.as_policy().uses_access_key
    assert _SECRET_ACCESS_KEY not in repr(config)


def test_single_upload_uses_opaque_key_sha256_kms_and_expected_owner() -> None:
    client = RecordingS3Client()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)
    payload = b"compressed sandbox bytes"

    receipt = _upload(uploader, phase="initial", body=io.BytesIO(payload), size=len(payload))

    request = client.one_call("put_object")
    expected_checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    assert request == {
        "Bucket": "freeinference",
        "Key": _key("initial"),
        "ContentType": "application/gzip",
        "Metadata": {
            "archive-phase": "initial",
            "archive-format": "tar-gzip-v1",
            "archive-size-bytes": str(len(payload)),
            "archive-sha256": hashlib.sha256(payload).hexdigest(),
        },
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": _KMS_KEY,
        "ExpectedBucketOwner": _ACCOUNT,
        "IfNoneMatch": "*",
        "Body": payload,
        "ContentLength": len(payload),
        "ChecksumAlgorithm": "SHA256",
        "ChecksumSHA256": expected_checksum,
    }
    assert "ACL" not in request
    assert receipt.phase == "initial"
    assert receipt.key == request["Key"]
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.version_id == "version-1"
    assert receipt.multipart is False


@pytest.mark.parametrize("versioning", [{}, {"Status": "Suspended"}])
def test_upload_fails_closed_without_enabled_bucket_versioning(
    versioning: dict[str, Any],
) -> None:
    client = RecordingS3Client(versioning_response=versioning)
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(SandboxArchiveUploadError, match="versioning must be enabled"):
        _upload(uploader, phase="final", body=io.BytesIO(b"x"), size=1)

    assert client.one_call("get_bucket_versioning") == {
        "Bucket": "freeinference",
        "ExpectedBucketOwner": _ACCOUNT,
    }
    assert not any(name == "put_object" for name, _kwargs in client.calls)


@pytest.mark.parametrize(
    "version_id",
    [None, "", "null", "NULL", " version ", "v" * 1025],
)
def test_upload_requires_a_bounded_non_null_version_id(version_id: object) -> None:
    class NullVersionClient(RecordingS3Client):
        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            response = super().put_object(**kwargs)
            response["VersionId"] = version_id
            return response

    client = NullVersionClient()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(SandboxArchiveUploadError, match="VersionId"):
        _upload(uploader, phase="final", body=io.BytesIO(b"x"), size=1)


def test_completed_multipart_without_a_version_id_is_not_aborted() -> None:
    class MissingVersionClient(RecordingS3Client):
        def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
            response = super().complete_multipart_upload(**kwargs)
            response.pop("VersionId")
            return response

    client = MissingVersionClient()
    uploader = SandboxArchiveS3Uploader(
        _config(
            max_archive_bytes=MIN_MULTIPART_PART_BYTES,
            multipart_threshold_bytes=1,
            multipart_part_bytes=MIN_MULTIPART_PART_BYTES,
        ),
        client=client,
    )

    with pytest.raises(SandboxArchiveUploadError, match="VersionId"):
        _upload(uploader, phase="final", body=io.BytesIO(b"xx"), size=2)

    assert client.one_call("complete_multipart_upload")["Key"] == _key("final")
    assert not [call for call in client.calls if call[0] == "abort_multipart_upload"]


def test_keys_are_deterministic_opaque_and_generation_scoped() -> None:
    client = RecordingS3Client()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    initial = _upload(uploader, phase="initial", body=io.BytesIO(b"a"), size=1)
    final = _upload(uploader, phase="final", body=io.BytesIO(b"b"), size=1)
    refreshed = _upload(uploader, phase="final", body=io.BytesIO(b"c"), size=1)
    next_generation = _upload(
        uploader,
        phase="final",
        body=io.BytesIO(b"d"),
        size=1,
        generation=_GENERATION + 1,
    )

    assert initial.key == _key("initial")
    assert final.key == refreshed.key == _key("final")
    assert next_generation.key == _key("final", generation=_GENERATION + 1)
    assert next_generation.key != final.key
    assert _NAMESPACE not in initial.key
    assert _SANDBOX_ID not in initial.key
    put_calls = [kwargs for name, kwargs in client.calls if name == "put_object"]
    assert put_calls[0]["IfNoneMatch"] == "*"
    assert all("IfNoneMatch" not in request for request in put_calls[1:])
    assert not hasattr(uploader, "download")
    assert not hasattr(uploader, "delete")
    assert not hasattr(uploader, "restore")


def test_invalid_phase_size_and_stream_length_never_reach_s3() -> None:
    client = RecordingS3Client()
    config = _config(max_archive_bytes=8, multipart_threshold_bytes=8)
    uploader = SandboxArchiveS3Uploader(config, client=client)

    with pytest.raises(SandboxArchiveUploadError, match="phase"):
        _upload(uploader, phase="middle", body=io.BytesIO(b"x"), size=1)
    with pytest.raises(SandboxArchiveUploadError, match="size"):
        _upload(uploader, phase="initial", body=io.BytesIO(b"x" * 9), size=9)
    with pytest.raises(SandboxArchiveUploadError, match="ended"):
        _upload(uploader, phase="initial", body=io.BytesIO(b"x"), size=2)
    with pytest.raises(SandboxArchiveUploadError, match="exceeds"):
        _upload(uploader, phase="initial", body=io.BytesIO(b"xx"), size=1)

    assert client.calls == []


def test_multipart_upload_streams_bounded_parts_and_completes_with_checksums() -> None:
    part_size = MIN_MULTIPART_PART_BYTES
    payload = b"a" * part_size + b"final"
    client = RecordingS3Client()
    uploader = SandboxArchiveS3Uploader(
        _config(
            max_archive_bytes=2 * part_size,
            multipart_threshold_bytes=1,
            multipart_part_bytes=part_size,
        ),
        client=client,
    )

    receipt = _upload(uploader, phase="final", body=io.BytesIO(payload), size=len(payload))

    created = client.one_call("create_multipart_upload")
    assert created["Bucket"] == "freeinference"
    assert created["ServerSideEncryption"] == "aws:kms"
    assert created["SSEKMSKeyId"] == _KMS_KEY
    assert created["ExpectedBucketOwner"] == _ACCOUNT
    assert created["ChecksumAlgorithm"] == "SHA256"

    # Parts are sent several at once, so S3 can see them in any order.
    part_calls = sorted(
        (kwargs for name, kwargs in client.calls if name == "upload_part"),
        key=lambda call: call["PartNumber"],
    )
    assert [call["PartNumber"] for call in part_calls] == [1, 2]
    assert [len(call["Body"]) for call in part_calls] == [part_size, 5]
    for call in part_calls:
        expected = base64.b64encode(hashlib.sha256(call["Body"]).digest()).decode("ascii")
        assert call["ChecksumAlgorithm"] == "SHA256"
        assert call["ChecksumSHA256"] == expected
        assert call["ExpectedBucketOwner"] == _ACCOUNT

    completed = client.one_call("complete_multipart_upload")
    assert completed["ExpectedBucketOwner"] == _ACCOUNT
    assert "IfNoneMatch" not in completed
    assert completed["MultipartUpload"] == {
        "Parts": [
            {
                "ETag": '"part-1"',
                "PartNumber": 1,
                "ChecksumSHA256": part_calls[0]["ChecksumSHA256"],
            },
            {
                "ETag": '"part-2"',
                "PartNumber": 2,
                "ChecksumSHA256": part_calls[1]["ChecksumSHA256"],
            },
        ]
    }
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.multipart is True
    assert receipt.version_id == "version-2"
    assert not [call for call in client.calls if call[0] == "abort_multipart_upload"]


def test_deadline_expiring_between_multipart_parts_stops_without_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One part in flight at a time, so part 2 can only start after part 1;
    # with several at once, see the in-flight deadline test below.
    monkeypatch.setattr(archive_s3, "_PART_UPLOAD_WORKERS", 1)
    part_size = MIN_MULTIPART_PART_BYTES
    payload = b"a" * part_size + b"final"
    now = 0.0

    class ExpiringClient(RecordingS3Client):
        def upload_part(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal now
            response = super().upload_part(**kwargs)
            if kwargs["PartNumber"] == 1:
                now = 2.0
            return response

    client = ExpiringClient()
    uploader = SandboxArchiveS3Uploader(
        _config(
            max_archive_bytes=2 * part_size,
            multipart_threshold_bytes=1,
            multipart_part_bytes=part_size,
        ),
        client=client,
        clock=lambda: now,
    )

    with pytest.raises(SandboxArchiveDeadlineExceeded, match="deadline exceeded"):
        _upload(
            uploader,
            phase="final",
            body=io.BytesIO(payload),
            size=len(payload),
            deadline=1.0,
        )

    assert [kwargs["PartNumber"] for name, kwargs in client.calls if name == "upload_part"] == [1]
    assert not [call for call in client.calls if call[0] == "complete_multipart_upload"]
    # Once the absolute deadline has elapsed, even cleanup must not add another
    # synchronous network request; bucket lifecycle handles the abandoned MPU.
    assert not [call for call in client.calls if call[0] == "abort_multipart_upload"]


def _parts_payload(parts: int) -> bytes:
    """``parts`` minimum-size parts, each of its own byte, so a misplaced part shows."""
    return b"".join(bytes([65 + number]) * MIN_MULTIPART_PART_BYTES for number in range(parts))


def _multipart_uploader(
    client: RecordingS3Client, *, parts: int, **kwargs: Any
) -> SandboxArchiveS3Uploader:
    return SandboxArchiveS3Uploader(
        _config(
            max_archive_bytes=parts * MIN_MULTIPART_PART_BYTES,
            multipart_threshold_bytes=1,
            multipart_part_bytes=MIN_MULTIPART_PART_BYTES,
        ),
        client=client,
        **kwargs,
    )


def _part_numbers(client: RecordingS3Client) -> list[int]:
    return sorted(kwargs["PartNumber"] for name, kwargs in client.calls if name == "upload_part")


def test_parts_are_sent_at_once_and_completed_in_part_order() -> None:
    payload = _parts_payload(3)

    class LastPartFirstClient(RecordingS3Client):
        """Answer part 1 only once the parts sent after it have been answered."""

        def __init__(self) -> None:
            super().__init__()
            self.answered: list[int] = []
            self.lock = threading.Lock()
            self.later_parts_answered = threading.Event()

        def upload_part(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["PartNumber"] == 1:
                # One part at a time would wait here for good: parts 2 and 3
                # are sent only while part 1 is still in flight.
                assert self.later_parts_answered.wait(timeout=10)
            response = super().upload_part(**kwargs)
            with self.lock:
                self.answered.append(kwargs["PartNumber"])
                if {2, 3} <= set(self.answered):
                    self.later_parts_answered.set()
            return response

    client = LastPartFirstClient()

    receipt = _upload(
        _multipart_uploader(client, parts=3),
        phase="final",
        body=io.BytesIO(payload),
        size=len(payload),
    )

    assert client.answered[-1] == 1
    part_size = MIN_MULTIPART_PART_BYTES
    checksums = [
        base64.b64encode(hashlib.sha256(payload[start : start + part_size]).digest()).decode()
        for start in range(0, len(payload), part_size)
    ]
    assert client.one_call("complete_multipart_upload")["MultipartUpload"] == {
        "Parts": [
            {"ETag": f'"part-{number}"', "PartNumber": number, "ChecksumSHA256": checksum}
            for number, checksum in enumerate(checksums, start=1)
        ]
    }
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.multipart is True


def test_parts_beyond_the_workers_are_neither_read_nor_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_s3, "_PART_UPLOAD_WORKERS", 2)
    payload = _parts_payload(4)
    body = io.BytesIO(payload)
    read_while_busy: list[int] = []

    class CountingClient(RecordingS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.in_flight = 0
            self.most_in_flight = 0
            # Parts 1 and 2 are answered only once both are in flight; the
            # action sees how much of the body has been read by then.
            self.both_sent = threading.Barrier(
                2, action=lambda: read_while_busy.append(body.tell()), timeout=10
            )

        def upload_part(self, **kwargs: Any) -> dict[str, Any]:
            with self.lock:
                self.in_flight += 1
                self.most_in_flight = max(self.most_in_flight, self.in_flight)
            try:
                if kwargs["PartNumber"] <= 2:
                    self.both_sent.wait()
                return super().upload_part(**kwargs)
            finally:
                with self.lock:
                    self.in_flight -= 1

    client = CountingClient()

    receipt = _upload(
        _multipart_uploader(client, parts=4), phase="final", body=body, size=len(payload)
    )

    assert client.most_in_flight == 2
    # Memory holds the parts in flight: the third is read once one finishes.
    assert read_while_busy == [2 * MIN_MULTIPART_PART_BYTES]
    assert _part_numbers(client) == [1, 2, 3, 4]
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()


def test_no_part_starts_once_the_deadline_passes_with_parts_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_s3, "_PART_UPLOAD_WORKERS", 2)
    payload = _parts_payload(4)
    now = 0.0

    def expire() -> None:
        nonlocal now
        now = 2.0

    class ExpiringClient(RecordingS3Client):
        def __init__(self) -> None:
            super().__init__()
            # The two parts in flight are answered after the deadline.
            self.both_sent = threading.Barrier(2, action=expire, timeout=10)

        def upload_part(self, **kwargs: Any) -> dict[str, Any]:
            response = super().upload_part(**kwargs)
            self.both_sent.wait()
            return response

    client = ExpiringClient()

    with pytest.raises(SandboxArchiveDeadlineExceeded, match="deadline exceeded"):
        _upload(
            _multipart_uploader(client, parts=4, clock=lambda: now),
            phase="final",
            body=io.BytesIO(payload),
            size=len(payload),
            deadline=1.0,
        )

    assert _part_numbers(client) == [1, 2]
    assert not [call for call in client.calls if call[0] == "complete_multipart_upload"]
    assert not [call for call in client.calls if call[0] == "abort_multipart_upload"]


def test_a_failed_part_stops_later_parts_and_aborts_after_the_parts_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_s3, "_PART_UPLOAD_WORKERS", 2)
    payload = _parts_payload(4)

    class FailingClient(RecordingS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.part_two_sent = threading.Event()
            self.part_two_answered = threading.Event()
            self.answered_before_abort: bool | None = None

        def upload_part(self, **kwargs: Any) -> dict[str, Any]:
            response = super().upload_part(**kwargs)
            if kwargs["PartNumber"] == 1:
                assert self.part_two_sent.wait(timeout=10)
                raise RuntimeError("simulated part failure")
            self.part_two_sent.set()
            # Still in flight when part 1 fails.
            time.sleep(0.05)
            self.part_two_answered.set()
            return response

        def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
            self.answered_before_abort = self.part_two_answered.is_set()
            return super().abort_multipart_upload(**kwargs)

    client = FailingClient()

    with pytest.raises(RuntimeError, match="simulated part failure"):
        _upload(
            _multipart_uploader(client, parts=4),
            phase="final",
            body=io.BytesIO(payload),
            size=len(payload),
        )

    assert _part_numbers(client) == [1, 2]
    assert not [call for call in client.calls if call[0] == "complete_multipart_upload"]
    # S3 may keep a part that lands after an abort; this one finished first.
    assert client.one_call("abort_multipart_upload")["UploadId"] == "upload-1"
    assert client.answered_before_abort is True


def test_a_multipart_body_unlike_its_digest_is_aborted_not_completed() -> None:
    payload = _parts_payload(2)
    client = RecordingS3Client()

    with pytest.raises(SandboxArchiveUploadError, match="changed before upload"):
        _multipart_uploader(client, parts=2).upload(
            namespace=_NAMESPACE,
            sandbox_id=_SANDBOX_ID,
            generation=_GENERATION,
            phase="final",
            body=io.BytesIO(payload),
            size=len(payload),
            sha256=hashlib.sha256(payload[::-1]).hexdigest(),
        )

    assert _part_numbers(client) == [1, 2]
    assert not [call for call in client.calls if call[0] == "complete_multipart_upload"]
    assert client.one_call("abort_multipart_upload")["UploadId"] == "upload-1"


def test_a_spool_file_is_read_by_offset_without_moving_its_position(tmp_path) -> None:
    payload = _parts_payload(3)
    client = RecordingS3Client()

    with (tmp_path / "spool").open("w+b") as spool:
        spool.write(payload)
        spool.seek(0)
        receipt = _upload(
            _multipart_uploader(client, parts=3), phase="final", body=spool, size=len(payload)
        )
        # Each worker read its own part with os.pread; no read moved the file.
        assert spool.tell() == 0

    part_calls = sorted(
        (kwargs for name, kwargs in client.calls if name == "upload_part"),
        key=lambda call: call["PartNumber"],
    )
    assert b"".join(call["Body"] for call in part_calls) == payload
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("stored", "message"),
    [("longer", "exceeds its declared size"), ("shorter", "ended before its declared size")],
)
def test_a_spool_file_unlike_its_declared_size_is_aborted_not_completed(
    tmp_path, stored: str, message: str
) -> None:
    payload = _parts_payload(2)
    client = RecordingS3Client()

    with (tmp_path / "spool").open("w+b") as spool:
        spool.write(payload + b"trailing" if stored == "longer" else payload[:-1])
        spool.seek(0)
        with pytest.raises(SandboxArchiveUploadError, match=message):
            _multipart_uploader(client, parts=3).upload(
                namespace=_NAMESPACE,
                sandbox_id=_SANDBOX_ID,
                generation=_GENERATION,
                phase="final",
                body=spool,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )

    assert not [call for call in client.calls if call[0] == "complete_multipart_upload"]
    assert client.one_call("abort_multipart_upload")["UploadId"] == "upload-1"


def test_a_read_that_returns_every_byte_is_used_without_a_copy() -> None:
    whole = b"x" * 4096

    class OneRead:
        def read(self, _amount: int) -> bytes:
            return whole

    class Trickle:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def read(self, amount: int) -> bytes:
            piece, self.data = self.data[: min(amount, 3)], self.data[3:]
            return piece

    assert archive_s3._read_exact(OneRead(), len(whole)) is whole  # type: ignore[arg-type]
    # Short reads are still put together into exactly the bytes asked for.
    assert archive_s3._read_exact(Trickle(b"abcdefgh"), 8) == b"abcdefgh"  # type: ignore[arg-type]


def test_deadline_is_checked_before_and_after_a_single_s3_request() -> None:
    now = 0.0

    class ExpiringClient(RecordingS3Client):
        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal now
            response = super().put_object(**kwargs)
            now = 2.0
            return response

    client = ExpiringClient()
    uploader = SandboxArchiveS3Uploader(_config(), client=client, clock=lambda: now)

    with pytest.raises(SandboxArchiveDeadlineExceeded, match="deadline exceeded"):
        _upload(
            uploader,
            phase="final",
            body=io.BytesIO(b"x"),
            size=1,
            deadline=1.0,
        )
    assert len([call for call in client.calls if call[0] == "put_object"]) == 1

    client.calls.clear()
    with pytest.raises(SandboxArchiveDeadlineExceeded, match="deadline exceeded"):
        _upload(
            uploader,
            phase="final",
            body=io.BytesIO(b"x"),
            size=1,
            deadline=1.0,
        )
    assert client.calls == []


def test_initial_single_precondition_failure_is_a_distinct_idempotent_outcome() -> None:
    payload = b"x"
    client = RecordingS3Client(
        put_error=PreconditionFailure(),
        head_response=_matching_head(payload),
    )
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(SandboxArchiveAlreadyExists) as excinfo:
        _upload(uploader, phase="initial", body=io.BytesIO(payload), size=len(payload))

    assert excinfo.value.bucket == "freeinference"
    assert excinfo.value.key == _key("initial")
    assert client.one_call("put_object")["IfNoneMatch"] == "*"
    assert client.one_call("head_object") == {
        "Bucket": "freeinference",
        "Key": _key("initial"),
        "ExpectedBucketOwner": _ACCOUNT,
    }


def test_initial_multipart_precondition_failure_aborts_and_is_idempotent() -> None:
    payload = b"abc"
    client = RecordingS3Client(
        complete_error=PreconditionFailure(),
        head_response=_matching_head(payload),
    )
    uploader = SandboxArchiveS3Uploader(
        _config(
            max_archive_bytes=MIN_MULTIPART_PART_BYTES,
            multipart_threshold_bytes=1,
            multipart_part_bytes=MIN_MULTIPART_PART_BYTES,
        ),
        client=client,
    )

    with pytest.raises(SandboxArchiveAlreadyExists) as excinfo:
        _upload(uploader, phase="initial", body=io.BytesIO(payload), size=len(payload))

    assert excinfo.value.key == _key("initial")
    assert client.one_call("complete_multipart_upload")["IfNoneMatch"] == "*"
    assert client.one_call("abort_multipart_upload")["Key"] == _key("initial")


def test_initial_exists_probes_only_the_exact_matching_key() -> None:
    payload = b"prepared baseline"
    client = RecordingS3Client(head_response=_matching_head(payload))
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    assert uploader.initial_exists(
        namespace=_NAMESPACE,
        sandbox_id=_SANDBOX_ID,
        generation=_GENERATION,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert client.one_call("head_object") == {
        "Bucket": "freeinference",
        "Key": _key("initial"),
        "ExpectedBucketOwner": _ACCOUNT,
    }


def test_initial_exists_returns_false_only_for_authoritative_not_found() -> None:
    client = RecordingS3Client(head_error=ObjectNotFound())
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    assert not uploader.initial_exists(
        namespace=_NAMESPACE,
        sandbox_id=_SANDBOX_ID,
        generation=_GENERATION,
        size=1,
        sha256=hashlib.sha256(b"x").hexdigest(),
    )

    missing_bucket = BucketNotFound()
    uploader = SandboxArchiveS3Uploader(
        _config(), client=RecordingS3Client(head_error=missing_bucket)
    )
    with pytest.raises(BucketNotFound) as excinfo:
        uploader.initial_exists(
            namespace=_NAMESPACE,
            sandbox_id=_SANDBOX_ID,
            generation=_GENERATION,
            size=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
        )
    assert excinfo.value is missing_bucket


def test_initial_exists_rejects_mismatch_and_transient_head_failure() -> None:
    payload = b"x"
    mismatch = _matching_head(payload)
    mismatch["Metadata"]["archive-sha256"] = "0" * 64
    uploader = SandboxArchiveS3Uploader(_config(), client=RecordingS3Client(head_response=mismatch))
    with pytest.raises(SandboxArchiveUploadError, match="prepared marker"):
        uploader.initial_exists(
            namespace=_NAMESPACE,
            sandbox_id=_SANDBOX_ID,
            generation=_GENERATION,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    denied = RuntimeError("head denied")
    uploader = SandboxArchiveS3Uploader(_config(), client=RecordingS3Client(head_error=denied))
    with pytest.raises(RuntimeError) as excinfo:
        uploader.initial_exists(
            namespace=_NAMESPACE,
            sandbox_id=_SANDBOX_ID,
            generation=_GENERATION,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    assert excinfo.value is denied


def test_initial_single_conditional_conflict_rewinds_and_retries() -> None:
    payload = b"retry the same bytes"

    class ConflictOnceClient(RecordingS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.conflicts = 1

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            if self.conflicts:
                self.conflicts -= 1
                self.calls.append(("put_object", kwargs))
                raise ConditionalConflict()
            return super().put_object(**kwargs)

    client = ConflictOnceClient()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    receipt = _upload(
        uploader,
        phase="initial",
        body=io.BytesIO(payload),
        size=len(payload),
    )

    puts = [kwargs for name, kwargs in client.calls if name == "put_object"]
    assert len(puts) == 2
    assert [request["Body"] for request in puts] == [payload, payload]
    assert all(request["IfNoneMatch"] == "*" for request in puts)
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()


def test_initial_multipart_conditional_conflict_reinitiates_and_reuploads() -> None:
    payload = b"multipart retry"

    class ConflictOnceClient(RecordingS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.creates = 0
            self.conflicts = 1

        def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
            self.creates += 1
            self.calls.append(("create_multipart_upload", kwargs))
            return {"UploadId": f"upload-{self.creates}"}

        def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
            if self.conflicts:
                self.conflicts -= 1
                self.calls.append(("complete_multipart_upload", kwargs))
                raise ConditionalConflict()
            return super().complete_multipart_upload(**kwargs)

    client = ConflictOnceClient()
    uploader = SandboxArchiveS3Uploader(
        _config(
            max_archive_bytes=MIN_MULTIPART_PART_BYTES,
            multipart_threshold_bytes=1,
            multipart_part_bytes=MIN_MULTIPART_PART_BYTES,
        ),
        client=client,
    )

    receipt = _upload(
        uploader,
        phase="initial",
        body=io.BytesIO(payload),
        size=len(payload),
    )

    assert len([call for call in client.calls if call[0] == "create_multipart_upload"]) == 2
    parts = [kwargs for name, kwargs in client.calls if name == "upload_part"]
    assert [request["UploadId"] for request in parts] == ["upload-1", "upload-2"]
    assert [request["Body"] for request in parts] == [payload, payload]
    completes = [kwargs for name, kwargs in client.calls if name == "complete_multipart_upload"]
    assert [request["UploadId"] for request in completes] == ["upload-1", "upload-2"]
    assert client.one_call("abort_multipart_upload")["UploadId"] == "upload-1"
    assert receipt.multipart is True


def test_initial_conditional_conflict_retry_count_is_bounded() -> None:
    class AlwaysConflictClient(RecordingS3Client):
        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("put_object", kwargs))
            raise ConditionalConflict()

    client = AlwaysConflictClient()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(ConditionalConflict):
        _upload(uploader, phase="initial", body=io.BytesIO(b"x"), size=1)

    assert len([call for call in client.calls if call[0] == "put_object"]) == 3


@pytest.mark.parametrize(
    "field, value",
    [
        ("ContentLength", 2),
        ("ContentType", "application/octet-stream"),
        ("ServerSideEncryption", "AES256"),
        ("SSEKMSKeyId", "arn:aws:kms:us-east-1:123456789012:key/other"),
        ("Metadata.archive-sha256", "0" * 64),
        ("Metadata.archive-size-bytes", "2"),
        ("Metadata.archive-phase", "final"),
        ("Metadata.archive-format", "other"),
        ("VersionId", None),
        ("VersionId", "null"),
        ("VersionId", "NULL"),
        ("VersionId", " version "),
        ("VersionId", "v" * 1025),
    ],
)
def test_initial_precondition_rejects_any_different_existing_object(
    field: str,
    value: object,
) -> None:
    payload = b"x"
    head = _matching_head(payload)
    if field.startswith("Metadata."):
        head["Metadata"][field.removeprefix("Metadata.")] = value
    else:
        head[field] = value
    client = RecordingS3Client(
        put_error=PreconditionFailure(),
        head_response=head,
    )
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(SandboxArchiveUploadError, match="does not match this retry"):
        _upload(uploader, phase="initial", body=io.BytesIO(payload), size=len(payload))


def test_initial_precondition_requires_metadata_read_permission() -> None:
    denied = RuntimeError("head denied")
    client = RecordingS3Client(
        put_error=PreconditionFailure(),
        head_error=denied,
    )
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(RuntimeError) as excinfo:
        _upload(uploader, phase="initial", body=io.BytesIO(b"x"), size=1)

    assert excinfo.value is denied


@pytest.mark.parametrize(
    "phase, error",
    [
        ("initial", RuntimeError("other S3 failure")),
        ("final", PreconditionFailure()),
    ],
)
def test_only_an_initial_precondition_failure_gets_idempotent_outcome(
    phase: str, error: Exception
) -> None:
    client = RecordingS3Client(put_error=error)
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(type(error)) as excinfo:
        _upload(uploader, phase=phase, body=io.BytesIO(b"x"), size=1)

    assert excinfo.value is error


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"namespace": ""}, "namespace"),
        ({"sandbox_id": ""}, "sandbox id"),
        ({"generation": 0}, "generation"),
    ],
)
def test_invalid_archive_identity_never_reaches_s3(overrides: dict[str, Any], message: str) -> None:
    client = RecordingS3Client()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)
    identity: dict[str, Any] = {
        "namespace": _NAMESPACE,
        "sandbox_id": _SANDBOX_ID,
        "generation": _GENERATION,
    }
    identity.update(overrides)

    with pytest.raises(SandboxArchiveUploadError, match=message):
        uploader.upload(
            **identity,
            phase="initial",
            body=io.BytesIO(b"x"),
            size=1,
        )

    assert client.calls == []


@pytest.mark.parametrize("failure", ["part", "complete", "trailing"])
def test_every_incomplete_multipart_path_attempts_abort(failure: str) -> None:
    client = RecordingS3Client(
        fail_part=1 if failure == "part" else None,
        fail_complete=failure == "complete",
    )
    payload = b"abc" + (b"extra" if failure == "trailing" else b"")
    declared_size = 3
    uploader = SandboxArchiveS3Uploader(
        _config(
            max_archive_bytes=MIN_MULTIPART_PART_BYTES,
            multipart_threshold_bytes=1,
            multipart_part_bytes=MIN_MULTIPART_PART_BYTES,
        ),
        client=client,
    )

    with pytest.raises((RuntimeError, SandboxArchiveUploadError)):
        _upload(uploader, phase="final", body=io.BytesIO(payload), size=declared_size)

    if failure == "trailing":
        # The bounded pre-hash catches a lying length before opening an MPU.
        assert client.calls == []
        return
    aborted = client.one_call("abort_multipart_upload")
    assert aborted == {
        "Bucket": "freeinference",
        "Key": _key("final"),
        "UploadId": "upload-1",
        "ExpectedBucketOwner": _ACCOUNT,
    }


def test_default_client_is_lazy_and_has_bounded_transport_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingS3Client()
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    def make_client(service: str, **kwargs: Any) -> RecordingS3Client:
        calls.append((service, kwargs))
        return client

    fake_boto3 = SimpleNamespace(client=make_client)
    fake_botocore_config = SimpleNamespace(Config=FakeConfig)
    imported: list[str] = []

    def import_module(name: str) -> Any:
        imported.append(name)
        if name == "boto3":
            return fake_boto3
        assert name == "botocore.config"
        return fake_botocore_config

    monkeypatch.setattr(archive_s3.importlib, "import_module", import_module)
    uploader = SandboxArchiveS3Uploader(_config())

    assert imported == []
    _upload(uploader, phase="initial", body=io.BytesIO(b"x"), size=1)

    assert imported == ["boto3", "botocore.config"]
    assert len(calls) == 1
    service, kwargs = calls[0]
    assert service == "s3"
    assert set(kwargs) == {"region_name", "config"}
    assert kwargs["region_name"] == _REGION
    assert kwargs["config"].kwargs == {
        "connect_timeout": archive_s3.S3_CONNECT_TIMEOUT_S,
        "read_timeout": archive_s3.S3_READ_TIMEOUT_S,
        "retries": {
            "mode": "standard",
            "total_max_attempts": archive_s3.S3_TOTAL_MAX_ATTEMPTS,
        },
        "tcp_keepalive": True,
        "ignore_configured_endpoint_urls": True,
    }
    uploader.close()
    uploader.close()
    assert client.close_calls == 1


def test_injected_s3_client_is_not_owned_or_closed() -> None:
    client = RecordingS3Client()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    uploader.close()
    uploader.close()

    assert client.close_calls == 0
    with pytest.raises(SandboxArchiveUploadError, match="closed"):
        _upload(uploader, phase="final", body=io.BytesIO(b"x"), size=1)
    assert client.calls == []


def test_numeric_tuning_is_loaded_from_policy_without_an_sdk_dependency() -> None:
    config = SandboxArchiveS3Config.from_policy(
        _policy(
            max_archive_bytes=2 * MIN_MULTIPART_PART_BYTES,
            multipart_threshold_bytes=1024,
            multipart_part_bytes=MIN_MULTIPART_PART_BYTES,
        )
    )

    assert config.max_archive_bytes == 2 * MIN_MULTIPART_PART_BYTES
    assert config.multipart_threshold_bytes == 1024
    assert config.multipart_part_bytes == MIN_MULTIPART_PART_BYTES


_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_access_key_upload_uses_sse_s3_and_omits_the_bucket_owner() -> None:
    client = RecordingS3Client()
    config = _config(
        kms_key_arn="",
        expected_bucket_owner="",
        aws_access_key_id=_ACCESS_KEY_ID,
        aws_secret_access_key=_SECRET_ACCESS_KEY,
    )
    uploader = SandboxArchiveS3Uploader(config, client=client)
    payload = b"access-key archive"

    _upload(uploader, phase="final", body=io.BytesIO(payload), size=len(payload))

    request = client.one_call("put_object")
    assert request["ServerSideEncryption"] == "AES256"
    assert "SSEKMSKeyId" not in request
    assert "ExpectedBucketOwner" not in request
    assert "aws_access_key_id" not in request
    versioning = client.one_call("get_bucket_versioning")
    assert "ExpectedBucketOwner" not in versioning


def test_access_key_is_passed_only_to_the_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingS3Client()
    calls: list[dict[str, Any]] = []

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    def make_client(service: str, **kwargs: Any) -> RecordingS3Client:
        calls.append(kwargs)
        return client

    monkeypatch.setattr(
        archive_s3.importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(client=make_client)
            if name == "boto3"
            else SimpleNamespace(Config=FakeConfig)
        ),
    )
    uploader = SandboxArchiveS3Uploader(
        _config(
            aws_access_key_id=_ACCESS_KEY_ID,
            aws_secret_access_key=_SECRET_ACCESS_KEY,
        )
    )
    _upload(uploader, phase="final", body=io.BytesIO(b"x"), size=1)

    assert calls[0]["aws_access_key_id"] == _ACCESS_KEY_ID
    assert calls[0]["aws_secret_access_key"] == _SECRET_ACCESS_KEY
    assert "aws_access_key_id" not in uploader.config.as_policy().as_dict()


_CHECKPOINT_ID = "f" * 32


def _checkpoint_key(*, generation: int = _GENERATION) -> str:
    return f"cloud-agent/sandbox/v2/{_SANDBOX_HASH}/g{generation}/{_CHECKPOINT_ID}.oci.tar"


def _upload_checkpoint(uploader: SandboxArchiveS3Uploader, payload: bytes) -> SandboxArchiveReceipt:
    return uploader.upload_checkpoint(
        sandbox_id=_SANDBOX_ID,
        generation=_GENERATION,
        checkpoint_id=_CHECKPOINT_ID,
        body=io.BytesIO(payload),
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_checkpoint_upload_writes_once_under_the_sandbox_not_the_host() -> None:
    payload = b"oci-archive"
    client = RecordingS3Client()
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    receipt = _upload_checkpoint(uploader, payload)

    put = client.one_call("put_object")
    assert put["Key"] == _checkpoint_key()
    assert _NAMESPACE_HASH not in put["Key"]
    assert put["IfNoneMatch"] == "*"
    assert put["ContentType"] == "application/x-tar"
    assert put["Metadata"] == {
        "archive-phase": "checkpoint",
        "archive-format": "oci-layer-archive-v1",
        "archive-size-bytes": str(len(payload)),
        "archive-sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert put["ServerSideEncryption"] == "aws:kms"
    assert receipt.phase == "checkpoint"
    assert receipt.key == _checkpoint_key()
    assert receipt.version_id == "version-1"


def test_checkpoint_retry_after_a_lost_response_returns_the_existing_receipt() -> None:
    payload = b"oci-archive"
    head = _matching_head(payload) | {
        "ContentType": "application/x-tar",
        "ETag": '"existing"',
        "Metadata": {
            "archive-phase": "checkpoint",
            "archive-format": "oci-layer-archive-v1",
            "archive-size-bytes": str(len(payload)),
            "archive-sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    client = RecordingS3Client(put_error=PreconditionFailure(), head_response=head)
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    receipt = _upload_checkpoint(uploader, payload)

    assert receipt.key == _checkpoint_key()
    assert receipt.version_id == "version-existing"
    assert receipt.etag == '"existing"'


def test_checkpoint_retry_refuses_different_existing_bytes() -> None:
    payload = b"oci-archive"
    head = _matching_head(b"something else") | {"ContentType": "application/x-tar"}
    client = RecordingS3Client(put_error=PreconditionFailure(), head_response=head)
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(SandboxArchiveUploadError, match="does not match"):
        _upload_checkpoint(uploader, payload)


@pytest.mark.parametrize("kind", ["checkpoint", "environment snapshot"])
def test_an_existing_object_found_after_the_deadline_is_not_accepted(kind: str) -> None:
    """A retry that finds its own earlier write still answers within its deadline.

    The refused put is followed by two HEADs of the existing object: one that
    proves it is this upload's, then the one the receipt is read from. Only
    the second is slow here.
    """
    payload = b"oci-archive"
    now = 0.0

    class SlowHeadClient(RecordingS3Client):
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal now
            response = super().head_object(**kwargs)
            if [call[0] for call in self.calls].count("head_object") == 2:
                now = 2.0
            return response

    head = _matching_head(payload) | {
        "ContentType": "application/x-tar",
        "Metadata": {
            "archive-phase": "checkpoint",
            "archive-format": "oci-layer-archive-v1",
            "archive-size-bytes": str(len(payload)),
            "archive-sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    client = SlowHeadClient(put_error=PreconditionFailure(), head_response=head)
    uploader = SandboxArchiveS3Uploader(_config(), client=client, clock=lambda: now)
    upload = {
        "body": io.BytesIO(payload),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "deadline": 1.0,
    }

    with pytest.raises(SandboxArchiveDeadlineExceeded, match="deadline exceeded"):
        if kind == "checkpoint":
            uploader.upload_checkpoint(
                sandbox_id=_SANDBOX_ID,
                generation=_GENERATION,
                checkpoint_id=_CHECKPOINT_ID,
                **upload,
            )
        else:
            uploader.upload_environment_snapshot(owner="a" * 64, snapshot_id="b" * 32, **upload)


@pytest.mark.parametrize("checkpoint_id", ["", "F" * 32, "f" * 31, "../" + "f" * 29])
def test_checkpoint_key_refuses_a_malformed_id(checkpoint_id: str) -> None:
    with pytest.raises(SandboxArchiveUploadError, match="checkpoint id"):
        checkpoint_object_key(sandbox_id=_SANDBOX_ID, generation=1, checkpoint_id=checkpoint_id)


def test_upload_does_not_accept_the_checkpoint_phase() -> None:
    uploader = SandboxArchiveS3Uploader(_config(), client=RecordingS3Client())

    with pytest.raises(SandboxArchiveUploadError, match="phase"):
        _upload(uploader, phase="checkpoint", body=io.BytesIO(b"x"), size=1)


def test_config_carries_the_checkpoint_switch_back_into_its_policy() -> None:
    policy = _policy(checkpoints_enabled=True)

    assert SandboxArchiveS3Config.from_policy(policy).as_policy() == policy


_SNAPSHOT_OWNER = "a" * 64
_SNAPSHOT_ID = "b" * 32


def _snapshot_key() -> str:
    return f"cloud-agent/sandbox/env/v1/{_SNAPSHOT_OWNER}/{_SNAPSHOT_ID}.oci.tar"


def _snapshot_head(payload: bytes) -> dict[str, Any]:
    return _matching_head(payload) | {
        "ContentType": "application/x-tar",
        "ETag": '"existing"',
        "Metadata": {
            "archive-phase": "checkpoint",
            "archive-format": "oci-layer-archive-v1",
            "archive-size-bytes": str(len(payload)),
            "archive-sha256": hashlib.sha256(payload).hexdigest(),
        },
    }


def _upload_environment_snapshot(
    uploader: SandboxArchiveS3Uploader,
    payload: bytes,
    *,
    deadline: float | None = None,
) -> SandboxArchiveReceipt:
    return uploader.upload_environment_snapshot(
        owner=_SNAPSHOT_OWNER,
        snapshot_id=_SNAPSHOT_ID,
        body=io.BytesIO(payload),
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        deadline=deadline,
    )


def test_environment_snapshot_retry_after_a_lost_response_returns_the_existing_receipt() -> None:
    payload = b"env-snapshot"
    client = RecordingS3Client(
        put_error=PreconditionFailure(), head_response=_snapshot_head(payload)
    )
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    receipt = _upload_environment_snapshot(uploader, payload)

    assert receipt.key == _snapshot_key()
    assert receipt.version_id == "version-existing"
    assert receipt.etag == '"existing"'


def test_environment_snapshot_retry_refuses_different_existing_bytes() -> None:
    payload = b"env-snapshot"
    head = _snapshot_head(b"something else")
    client = RecordingS3Client(put_error=PreconditionFailure(), head_response=head)
    uploader = SandboxArchiveS3Uploader(_config(), client=client)

    with pytest.raises(SandboxArchiveUploadError, match="does not match"):
        _upload_environment_snapshot(uploader, payload)


def test_environment_snapshot_retry_refuses_a_head_that_crosses_the_deadline() -> None:
    payload = b"env-snapshot"
    now = 0.0

    class ExpiringClient(RecordingS3Client):
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal now
            response = super().head_object(**kwargs)
            now += 0.6
            return response

    client = ExpiringClient(put_error=PreconditionFailure(), head_response=_snapshot_head(payload))
    uploader = SandboxArchiveS3Uploader(_config(), client=client, clock=lambda: now)

    with pytest.raises(SandboxArchiveDeadlineExceeded, match="deadline exceeded"):
        _upload_environment_snapshot(uploader, payload, deadline=1.0)

    # Both the retry probe and the receipt's own HEAD ran; the second must not
    # be allowed to hand back a receipt once the deadline has elapsed.
    assert len([call for call in client.calls if call[0] == "head_object"]) == 2


def test_checkpoint_retry_also_refuses_a_head_that_crosses_the_deadline() -> None:
    payload = b"oci-archive"
    now = 0.0

    class ExpiringClient(RecordingS3Client):
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal now
            response = super().head_object(**kwargs)
            now += 0.6
            return response

    client = ExpiringClient(put_error=PreconditionFailure(), head_response=_snapshot_head(payload))
    uploader = SandboxArchiveS3Uploader(_config(), client=client, clock=lambda: now)

    with pytest.raises(SandboxArchiveDeadlineExceeded, match="deadline exceeded"):
        uploader.upload_checkpoint(
            sandbox_id=_SANDBOX_ID,
            generation=_GENERATION,
            checkpoint_id=_CHECKPOINT_ID,
            body=io.BytesIO(payload),
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            deadline=1.0,
        )

    assert len([call for call in client.calls if call[0] == "head_object"]) == 2
