"""S3 archive capabilities attest immutable complete bytes before deletion."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from cloud_agent import sandbox_archive, settings
from cloud_agent.sandbox_archive import (
    MAX_ARCHIVE_BYTES,
    UPLOAD_EXPIRES_SECONDS,
    S3SandboxArchiveStore,
    SandboxArchiveConfiguration,
    SandboxArchiveError,
    SandboxArchiveUnavailable,
    SandboxArchiveVerificationError,
)

DIGEST = hashlib.sha256(b"archive").hexdigest()
CHECKSUM = base64.b64encode(bytes.fromhex(DIGEST)).decode("ascii")
SIZE = 7
KEY = "sandbox-archives/sandbox-one/request-one/archive.tar"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    settings.reset_settings()
    for setting in settings.SETTINGS:
        if setting.key.startswith("sandbox_archive.") and setting.env:
            monkeypatch.delenv(setting.env, raising=False)
    yield
    settings.reset_settings()


def store(**configuration):
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://s3.example/scoped-upload"
    client.head_object.return_value = {
        "ContentLength": SIZE,
        "ChecksumSHA256": CHECKSUM,
        "ChecksumType": "FULL_OBJECT",
        "VersionId": "version-one",
    }
    return S3SandboxArchiveStore(
        SandboxArchiveConfiguration(bucket="archive-bucket", **configuration), client=client
    ), client


async def test_upload_binds_one_immutable_object_complete_checksum_and_byte_length():
    archives, client = store()
    upload = await archives.prepare_upload("sandbox-one", "request-one", DIGEST, SIZE)
    assert upload.key == archives.key_for("sandbox-one", "request-one") == KEY
    assert upload.expires_in_seconds == UPLOAD_EXPIRES_SECONDS
    assert upload.headers == {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(SIZE),
        "x-amz-checksum-sha256": CHECKSUM,
        "If-None-Match": "*",
    }
    client.generate_presigned_url.assert_called_once_with(
        "put_object",
        Params={
            "Bucket": "archive-bucket",
            "Key": KEY,
            "ContentType": "application/octet-stream",
            "ContentLength": SIZE,
            "ChecksumSHA256": CHECKSUM,
            "IfNoneMatch": "*",
        },
        ExpiresIn=UPLOAD_EXPIRES_SECONDS,
        HttpMethod="PUT",
    )
    assert archives.key_for("sandbox-one", "request-two") != upload.key
    assert archives.key_for("sandbox/two", "request/three") == (
        "sandbox-archives/sandbox%2Ftwo/request%2Fthree/archive.tar"
    )


async def test_sdk_signature_requires_all_upload_headers_without_any_s3_request():
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test-access",
        aws_secret_access_key="test-secret",
        config=Config(signature_version="s3v4"),
    )
    archives = S3SandboxArchiveStore(
        SandboxArchiveConfiguration(bucket="archive-bucket"), client=client
    )
    upload = await archives.prepare_upload("sandbox-one", "request-one", DIGEST, SIZE)
    query = parse_qs(urlsplit(upload.upload_url).query)
    assert query["X-Amz-SignedHeaders"] == [
        "content-length;content-type;host;if-none-match;x-amz-checksum-sha256"
    ]
    assert query["X-Amz-Expires"] == [str(UPLOAD_EXPIRES_SECONDS)]


@pytest.mark.parametrize("size", [0, -1, True, MAX_ARCHIVE_BYTES + 1])
async def test_single_put_cap_is_enforced_before_presigning(size):
    archives, client = store()
    with pytest.raises(SandboxArchiveError, match="5 GiB"):
        await archives.prepare_upload("sandbox-one", "request-one", DIGEST, size)
    client.generate_presigned_url.assert_not_called()


async def test_exact_five_gib_is_allowed():
    archives, client = store()
    upload = await archives.prepare_upload("sandbox-one", "request-one", DIGEST, MAX_ARCHIVE_BYTES)
    assert upload.headers["Content-Length"] == str(MAX_ARCHIVE_BYTES)
    assert (
        client.generate_presigned_url.call_args.kwargs["Params"]["ContentLength"]
        == MAX_ARCHIVE_BYTES
    )


@pytest.mark.parametrize("digest", ["", "not-a-digest", "a" * 63, "g" * 64])
async def test_upload_requires_an_actual_sha256_digest(digest):
    archives, client = store()
    with pytest.raises(SandboxArchiveError, match="SHA-256"):
        await archives.prepare_upload("sandbox-one", "request-one", digest, SIZE)
    client.generate_presigned_url.assert_not_called()


async def test_missing_configuration_is_unavailable_without_sdk_or_credential_lookup(monkeypatch):
    sdk = MagicMock(side_effect=AssertionError("SDK creation must stay lazy"))
    monkeypatch.setattr(sandbox_archive.boto3, "client", sdk)
    archives = S3SandboxArchiveStore.from_settings()
    assert not archives.available
    with pytest.raises(SandboxArchiveUnavailable):
        archives.key_for("sandbox-one", "request-one")
    with pytest.raises(SandboxArchiveUnavailable):
        await archives.prepare_upload("sandbox-one", "request-one", DIGEST, SIZE)
    with pytest.raises(SandboxArchiveUnavailable):
        await archives.verify_archive(KEY, DIGEST, SIZE)
    sdk.assert_not_called()


async def test_client_uses_only_the_control_planes_standard_aws_chain(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_ARCHIVE_S3_BUCKET", "archive-bucket")
    monkeypatch.setenv("AGENT_SANDBOX_ARCHIVE_S3_REGION", "us-east-2")
    monkeypatch.setenv("AGENT_SANDBOX_ARCHIVE_S3_ENDPOINT", "https://s3.example")
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://s3.example/scoped-upload"
    sdk = MagicMock(return_value=client)
    monkeypatch.setattr(sandbox_archive.boto3, "client", sdk)
    archives = S3SandboxArchiveStore.from_settings()
    assert archives.available
    sdk.assert_not_called()
    await archives.prepare_upload("sandbox-one", "request-one", DIGEST, SIZE)
    assert sdk.call_args.args == ("s3",)
    kwargs = sdk.call_args.kwargs
    assert set(kwargs) == {"region_name", "endpoint_url", "config"}
    assert kwargs["region_name"] == "us-east-2"
    assert kwargs["endpoint_url"] == "https://s3.example"
    assert kwargs["config"].signature_version == "s3v4"
    assert kwargs["config"].s3["addressing_style"] == "path"


async def test_head_attests_checksum_and_size_and_returns_a_durable_receipt():
    archives, client = store()
    receipt = await archives.verify_archive(KEY, DIGEST, SIZE)
    client.head_object.assert_called_once_with(
        Bucket="archive-bucket", Key=KEY, ChecksumMode="ENABLED"
    )
    assert receipt.archive_uri == f"s3://archive-bucket/{KEY}"
    assert (
        receipt.bucket,
        receipt.key,
        receipt.sha256,
        receipt.size_bytes,
        receipt.version_id,
    ) == ("archive-bucket", KEY, DIGEST, SIZE, "version-one")


@pytest.mark.parametrize(
    "remote",
    [
        {"ContentLength": SIZE, "ChecksumSHA256": base64.b64encode(b"x" * 32).decode()},
        {"ContentLength": SIZE + 1, "ChecksumSHA256": CHECKSUM},
        {"ContentLength": True, "ChecksumSHA256": CHECKSUM},
        {"ContentLength": SIZE, "Metadata": {"sha256": DIGEST}, "ETag": DIGEST},
        {"ContentLength": SIZE, "ChecksumSHA256": CHECKSUM, "ChecksumType": "COMPOSITE"},
        {"ChecksumSHA256": CHECKSUM},
    ],
)
async def test_mismatched_incomplete_or_metadata_only_objects_cannot_permit_deletion(remote):
    archives, client = store()
    client.head_object.return_value = remote
    with pytest.raises(SandboxArchiveVerificationError, match="checksum or size"):
        await archives.verify_archive(KEY, DIGEST, SIZE)


async def test_head_failure_cannot_return_a_receipt_or_echo_provider_sensitive_text():
    archives, client = store()
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "provider-sensitive-detail"}}, "HeadObject"
    )
    with pytest.raises(SandboxArchiveVerificationError, match="Unable to verify") as error:
        await archives.verify_archive(KEY, DIGEST, SIZE)
    assert "provider-sensitive-detail" not in str(error.value)


@pytest.mark.parametrize(
    "key",
    [
        "other-prefix/sandbox-one/request-one/archive.tar",
        "sandbox-archives/sandbox-one/request-one/other-object",
        "sandbox-archives/sandbox-one/extra/request-one/archive.tar",
        "sandbox-archives/../request-one/archive.tar",
        "s3://user:secret@archive-bucket/sandbox-one/request-one/archive.tar",
        f"s3://archive-bucket/{KEY}?secret=value",
        f"s3://archive-bucket/{KEY}#fragment",
    ],
)
async def test_unknown_scope_and_malformed_receipt_uri_are_rejected_before_s3(key):
    archives, client = store()
    with pytest.raises(SandboxArchiveVerificationError):
        await archives.verify_archive(key, DIGEST, SIZE)
    client.head_object.assert_not_called()


async def test_persisted_receipt_keeps_original_bucket_and_key_when_settings_change():
    archives, client = store(prefix="replacement-prefix")
    old_key = "previous-prefix/sandbox-one/request-one/archive.tar"
    receipt = await archives.verify_archive(f"s3://original-bucket/{old_key}", DIGEST, SIZE)
    client.head_object.assert_called_once_with(
        Bucket="original-bucket", Key=old_key, ChecksumMode="ENABLED"
    )
    assert receipt.archive_uri == f"s3://original-bucket/{old_key}"
    assert receipt.bucket == "original-bucket"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sandbox_archive.bucket", "s3://archive-bucket"),
        ("sandbox_archive.bucket", "127.0.0.1"),
        ("sandbox_archive.bucket", "invalid..bucket"),
        ("sandbox_archive.region", "region with spaces"),
        ("sandbox_archive.endpoint", "http://s3.example"),
        ("sandbox_archive.endpoint", "https://s3.example?secret=value"),
        ("sandbox_archive.prefix", "prefix/../elsewhere"),
        ("sandbox_archive.prefix", "prefix//elsewhere"),
    ],
)
def test_archive_settings_refuse_unsafe_or_unusable_values(key, value):
    with pytest.raises(ValueError, match="Invalid value for"):
        settings.encode_changes({key: value})


def test_saved_configuration_wins_over_later_environment_changes(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_ARCHIVE_S3_BUCKET", "initial-bucket")
    settings.apply_settings(1, settings.initial_settings())
    monkeypatch.setenv("AGENT_SANDBOX_ARCHIVE_S3_BUCKET", "later-environment-bucket")
    assert S3SandboxArchiveStore.from_settings().configuration.bucket == "initial-bucket"
    settings.apply_settings(2, settings.encode_changes({"sandbox_archive.bucket": "saved-bucket"}))
    assert S3SandboxArchiveStore.from_settings().configuration.bucket == "saved-bucket"
