"""The archive access key is encrypted before it reaches the database."""

from __future__ import annotations

import json

import pytest

from cloud_agent.sandbox_archive_credentials import (
    load_stored_archive_policy,
    open_archive_secret,
    seal_archive_storage,
)
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicyError

_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_KEY = "AKIAIOSFODNN7EXAMPLE"


def test_seal_round_trips_and_hides_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "x" * 32)
    sealed = seal_archive_storage(
        {"region": "us-east-1", "aws_access_key_id": _KEY, "aws_secret_access_key": _SECRET}
    )

    encoded = json.dumps(sealed)
    assert _SECRET not in encoded
    assert set(sealed["aws_secret_access_key"]) == {"ciphertext"}
    opened = open_archive_secret(sealed)
    assert opened["aws_secret_access_key"] == _SECRET
    policy = load_stored_archive_policy(
        {
            "enabled": True,
            "region": "us-east-1",
            "aws_access_key_id": _KEY,
            "aws_secret_access_key": sealed["aws_secret_access_key"],
        }
    )
    assert policy.aws_secret_access_key == _SECRET
    assert policy.uses_access_key is True


def test_plaintext_secret_at_rest_is_rejected() -> None:
    with pytest.raises(SandboxArchivePolicyError, match="not encrypted"):
        open_archive_secret({"aws_secret_access_key": _SECRET})


def test_sealing_without_an_encryption_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    with pytest.raises(SandboxArchivePolicyError, match="encryption key"):
        seal_archive_storage({"aws_secret_access_key": _SECRET})
