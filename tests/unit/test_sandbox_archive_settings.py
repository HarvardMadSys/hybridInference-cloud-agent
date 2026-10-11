"""The shared archive policy accepts only bounded, non-credential configuration."""

from __future__ import annotations

import pytest

from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_S3_URI,
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)


def _enabled(**changes) -> dict:
    values = {
        "enabled": True,
        "region": "us-east-1",
        "kms_key_arn": (
            "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
        ),
        "expected_bucket_owner": "123456789012",
        "max_archive_bytes": 2 * 1024**3,
        "multipart_threshold_bytes": 8 * 1024**2,
        "multipart_part_bytes": 8 * 1024**2,
        "revision": 3,
    }
    values.update(changes)
    return values


def test_policy_has_one_fixed_destination_and_storage_omits_wire_metadata() -> None:
    policy = SandboxArchivePolicy.from_mapping(_enabled())

    assert policy.as_dict()["s3_uri"] == SANDBOX_ARCHIVE_S3_URI
    assert policy.as_dict()["revision"] == 3
    assert "s3_uri" not in policy.as_storage_dict()
    assert "revision" not in policy.as_storage_dict()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("s3_uri", "s3://attacker-bucket/elsewhere"),
        ("aws_access_key_id", "AKIA_NOT_ALLOWED"),
        ("aws_secret_access_key", "not-allowed"),
        ("aws_session_token", "not-allowed"),
        ("endpoint_url", "https://attacker.invalid"),
    ],
)
def test_policy_rejects_destination_and_credential_injection(field: str, value: str) -> None:
    with pytest.raises(SandboxArchivePolicyError):
        SandboxArchivePolicy.from_mapping({**_enabled(), field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"region": ""},
        {"kms_key_arn": ""},
        {"expected_bucket_owner": ""},
        {"expected_bucket_owner": "123"},
        {"region": "us-west-2"},
        {"expected_bucket_owner": "999999999999"},
        {"kms_key_arn": "arn:aws:kms:us-east-1:123456789012:alias/archive"},
        {"max_archive_bytes": True},
        {"multipart_part_bytes": 1024},
        {"revision": True},
    ],
)
def test_enabled_policy_rejects_incomplete_mismatched_or_unbounded_values(changes: dict) -> None:
    with pytest.raises(SandboxArchivePolicyError):
        SandboxArchivePolicy.from_mapping({**_enabled(), **changes})


def test_disabled_default_is_valid_and_contains_no_credentials() -> None:
    policy = SandboxArchivePolicy.from_mapping(None)

    assert policy.enabled is False
    assert policy.revision == 0
    assert not any("credential" in key or "access_key" in key for key in policy.as_dict())
