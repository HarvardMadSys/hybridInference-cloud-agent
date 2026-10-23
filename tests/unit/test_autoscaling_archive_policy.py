"""The autoscaler reads the stored archive policy the way a claim does (#984).

An administrator's AWS access key is stored sealed, as ``{"ciphertext": ...}``.
A parse that does not open it first refuses the whole policy, and with it the
archive readiness of every Host. These tests run the real observation over a
connection that answers its queries with one queued turn and one fresh Host.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent.sandbox_archive_credentials import seal_archive_storage
from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent_shared.sandbox_archive_settings import (
    SANDBOX_ARCHIVE_CAPABILITY,
    SandboxArchivePolicy,
)

# AWS's documented example key pair, which no account accepts.
_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_SETTINGS_KEY = "test-settings-root-key-over-thirty-two-bytes"
_NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

_ACCESS_KEY = SandboxArchivePolicy(
    enabled=True,
    region="us-east-1",
    aws_access_key_id=_KEY_ID,
    aws_secret_access_key=_SECRET,
    revision=1,
)
_WORKLOAD_IDENTITY = SandboxArchivePolicy(
    enabled=True,
    region="us-east-1",
    kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
    expected_bucket_owner="123456789012",
    revision=1,
)


class _Connection:
    """Answer the observation's queries: one queued turn and one fresh Host."""

    def __init__(self, policy: SandboxArchivePolicy, *, applied_revision: int) -> None:
        # The row set_sandbox_archive_settings writes: the secret sealed, the
        # revision in a column of its own.
        self.policy_row = {
            "active_host": None,
            "sandbox_archive_settings": json.dumps(seal_archive_storage(policy.as_storage_dict())),
            "sandbox_archive_revision": policy.revision,
        }
        self.host_row = {
            "host": "runner-a",
            "status": "online",
            "enrolled": True,
            "launch_error": None,
            "last_heartbeat_at": _NOW - timedelta(seconds=5),
            "capabilities": json.dumps(
                [
                    "shared-sandbox-v1",
                    "native-fork-at-anchor-v1",
                    "shared-sandbox-deployment:test-deployment",
                    SANDBOX_ARCHIVE_CAPABILITY,
                ]
            ),
            "sandbox_runtime": None,
            "sandbox_archive_settings_applied_revision": applied_revision,
            "slots_total": 2,
            "slots_desired": None,
            "slots_used": 0,
            "allocation_id": None,
            "autoscaler_owned": False,
            "retained_groups": 0,
        }

    @asynccontextmanager
    async def transaction(self, **_options: Any):
        yield

    async def execute(self, *_args: Any) -> None:
        """Take the shared topology lock, which nothing else contends for here."""

    async def fetchval(self, *_args: Any) -> datetime:
        return _NOW

    async def fetchrow(self, query: str, *_args: Any) -> dict[str, Any]:
        if "FROM agent_runner_policy" in query:
            return self.policy_row
        # The admission check: no allocation owns the Host and nothing holds it.
        return {"id": None, "held": False, "all_held": False}

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        if "SELECT t.execution_profile" in query:
            return [{"execution_profile": "trusted-v1", "requires": "[]", "claimable": True}]
        if "FROM agent_runner_hosts h" in query:
            return [self.host_row]
        # No environment run is queued, holds a slot, or blocks the Host.
        return []


async def _observe(connection: _Connection) -> dict[str, Any]:
    @asynccontextmanager
    async def acquire():
        yield connection

    repository = PostgresHostPoolRepository(SimpleNamespace(acquire=acquire))
    return await repository.autoscaling_observation(expected_deployment_sha="test-deployment")


@pytest.mark.parametrize(
    "policy", [_ACCESS_KEY, _WORKLOAD_IDENTITY], ids=["access-key", "workload-identity"]
)
async def test_a_host_that_applied_the_current_revision_is_archive_ready(
    monkeypatch: pytest.MonkeyPatch, policy: SandboxArchivePolicy
) -> None:
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", _SETTINGS_KEY)

    applied = await _observe(_Connection(policy, applied_revision=policy.revision))
    assert applied["fresh_effective_free_slots"] == 2
    assert applied["unmatched_new_group_jobs"] == 0

    behind = await _observe(_Connection(policy, applied_revision=policy.revision - 1))
    assert behind["fresh_effective_free_slots"] == 0
    assert behind["unmatched_new_group_jobs"] == 1


async def test_a_key_kept_while_archives_are_off_holds_no_host_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning archives off keeps a saved key; every Host is archive-ready all the same."""
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", _SETTINGS_KEY)
    kept = SandboxArchivePolicy(
        aws_access_key_id=_KEY_ID, aws_secret_access_key=_SECRET, revision=2
    )

    observation = await _observe(_Connection(kept, applied_revision=0))
    assert observation["fresh_effective_free_slots"] == 2
    assert observation["unmatched_new_group_jobs"] == 0


async def test_a_stored_key_that_does_not_open_leaves_no_host_archive_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claims refuse every Host while the stored key does not open, so none counts here.

    A parse that skipped the key would count this Host as capacity that no
    claim can use.
    """
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", _SETTINGS_KEY)
    connection = _Connection(_ACCESS_KEY, applied_revision=_ACCESS_KEY.revision)
    # The settings key changed without re-encrypting what it had sealed.
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "another-test-settings-key-over-32-bytes")

    observation = await _observe(connection)
    assert observation["fresh_effective_free_slots"] == 0
    assert observation["unmatched_new_group_jobs"] == 1
