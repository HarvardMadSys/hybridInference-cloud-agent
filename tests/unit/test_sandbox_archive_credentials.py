"""The archive access key is encrypted before it reaches the database."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cloud_agent.sandbox_archive_credentials import seal_archive_storage, stored_archive_policy
from cloud_agent_shared.sandbox_archive_settings import (
    SandboxArchivePolicy,
    SandboxArchivePolicyError,
)

_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_KEY = "AKIAIOSFODNN7EXAMPLE"


def _row(settings: Any, *, revision: int = 0) -> dict[str, Any]:
    """An ``agent_runner_policy`` row as asyncpg returns it: JSONB as its text."""
    return {"sandbox_archive_settings": json.dumps(settings), "sandbox_archive_revision": revision}


def test_seal_round_trips_and_hides_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "x" * 32)
    sealed = seal_archive_storage(
        {"region": "us-east-1", "aws_access_key_id": _KEY, "aws_secret_access_key": _SECRET}
    )

    encoded = json.dumps(sealed)
    assert _SECRET not in encoded
    assert set(sealed["aws_secret_access_key"]) == {"ciphertext"}
    policy = stored_archive_policy(_row({**sealed, "enabled": True}, revision=4))
    assert policy.aws_secret_access_key == _SECRET
    assert policy.uses_access_key is True
    assert policy.revision == 4


def test_plaintext_secret_at_rest_is_rejected() -> None:
    with pytest.raises(SandboxArchivePolicyError, match="not encrypted"):
        stored_archive_policy(_row({"aws_secret_access_key": _SECRET}))


def test_a_key_sealed_under_another_settings_key_does_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "x" * 32)
    row = _row(seal_archive_storage({"aws_access_key_id": _KEY, "aws_secret_access_key": _SECRET}))
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "y" * 32)

    with pytest.raises(SandboxArchivePolicyError, match="could not be decrypted"):
        stored_archive_policy(row)


def test_sealing_without_an_encryption_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("AGENT_SESSION_SECRET", raising=False)
    with pytest.raises(SandboxArchivePolicyError, match="encryption key"):
        seal_archive_storage({"aws_secret_access_key": _SECRET})


def test_a_missing_row_stores_the_default_policy() -> None:
    assert stored_archive_policy(None) == SandboxArchivePolicy()


@pytest.mark.parametrize("decode", [json.dumps, dict], ids=["text", "object"])
def test_the_settings_read_as_text_or_object_at_the_rows_revision(decode: Any) -> None:
    """asyncpg hands JSONB back as text; the revision is a column of its own."""
    row = {"sandbox_archive_settings": decode({"enabled": False}), "sandbox_archive_revision": 7}

    assert stored_archive_policy(row) == SandboxArchivePolicy(revision=7)


@pytest.mark.parametrize("stored", ["[]", '"archives"', "null"])
def test_settings_that_are_not_an_object_do_not_parse(stored: str) -> None:
    with pytest.raises(SandboxArchivePolicyError, match="not an object"):
        stored_archive_policy({"sandbox_archive_settings": stored, "sandbox_archive_revision": 0})
