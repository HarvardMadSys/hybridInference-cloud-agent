"""The shared archive policy accepts bounded configuration and one optional access key."""

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


_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_access_key_replaces_bucket_owner_and_kms_and_stays_out_of_markers() -> None:
    policy = SandboxArchivePolicy.from_mapping(
        {
            **_enabled(),
            "kms_key_arn": "",
            "expected_bucket_owner": "",
            "aws_access_key_id": _ACCESS_KEY_ID,
            "aws_secret_access_key": _SECRET_ACCESS_KEY,
        }
    )

    assert policy.uses_access_key is True
    assert "aws_access_key_id" not in policy.as_dict()
    assert "aws_secret_access_key" not in policy.as_dict()
    assert policy.as_sandbox_manager_dict()["aws_access_key_id"] == _ACCESS_KEY_ID
    assert policy.as_sandbox_manager_dict()["aws_secret_access_key"] == _SECRET_ACCESS_KEY
    assert policy.as_storage_dict()["aws_secret_access_key"] == _SECRET_ACCESS_KEY
    restored = SandboxArchivePolicy.from_mapping(policy.as_dict())
    assert restored == policy
    assert restored.access_key_auth is True
    assert restored.aws_secret_access_key == ""
    assert not restored.equivalent(policy)
    assert SandboxArchivePolicy.from_mapping(policy.as_sandbox_manager_dict()).equivalent(policy)


def test_checkpoints_follow_archives_and_only_the_opt_out_is_written() -> None:
    default = SandboxArchivePolicy.from_mapping(_enabled())
    opted_out = SandboxArchivePolicy.from_mapping(_enabled(checkpoints_enabled=False))
    disabled = SandboxArchivePolicy.from_mapping({"enabled": False, "checkpoints_enabled": True})

    # Enabled archives checkpoint by default, and that default is written as
    # nothing, so it matches policies and markers that predate checkpoints.
    assert default.checkpoints_enabled is True
    assert "checkpoints_enabled" not in default.as_dict()
    assert "checkpoints_enabled" not in default.as_storage_dict()
    assert opted_out.checkpoints_enabled is False
    assert opted_out.as_dict()["checkpoints_enabled"] is False
    assert opted_out.as_storage_dict()["checkpoints_enabled"] is False
    assert SandboxArchivePolicy.from_mapping(opted_out.as_dict()) == opted_out
    assert opted_out != default
    # Checkpoints ride on archives: disabled archives never checkpoint.
    assert disabled.checkpoints_enabled is False
    assert "checkpoints_enabled" not in disabled.as_dict()
    with pytest.raises(SandboxArchivePolicyError, match="boolean"):
        SandboxArchivePolicy.from_mapping(_enabled(checkpoints_enabled="yes"))
