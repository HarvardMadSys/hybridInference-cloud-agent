"""Claim-query bindings that asyncpg must be able to encode."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from cloud_agent.storage.repositories import attempt_commands

if TYPE_CHECKING:
    import pytest


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Connection:
    def __init__(self, capabilities: list[str]) -> None:
        self._capabilities = capabilities
        self.job_capabilities_bind: object | None = None

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, sql: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if "FROM agent_jobs j" in sql:
            self.job_capabilities_bind = args[0]
            return None
        if "FROM agent_runner_policy" in sql:
            return {
                "active_host": None,
                "runtime_versions": json.dumps({}),
                "sandbox_archive_settings": json.dumps({}),
                "sandbox_archive_revision": 0,
            }
        if "FROM agent_runner_hosts" in sql:
            return {
                "status": "online",
                "capabilities": json.dumps(self._capabilities),
                "slots_total": 1,
                "slots_used": 0,
                "slots_desired": None,
                "sandbox_archive_settings_applied_revision": None,
                "credential_matches": True,
            }
        raise AssertionError(f"unexpected query: {sql}")


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


async def test_claim_encodes_capabilities_for_the_jsonb_job_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decoded capability membership must not leak a Python list into asyncpg."""
    declared = ["shared-sandbox-v1", "git"]
    connection = _Connection(declared)
    monkeypatch.setattr(attempt_commands, "lock_compute_topology", AsyncMock())
    monkeypatch.setattr(
        attempt_commands,
        "host_compute_admission",
        AsyncMock(return_value=(True, True)),
    )
    monkeypatch.setattr(attempt_commands, "shared_sandbox_claim_attested", lambda *a, **k: True)
    monkeypatch.setattr(
        attempt_commands,
        "compatible_shared_sandbox_followup_attested",
        lambda *a, **k: True,
    )
    repository = attempt_commands.PostgresAttemptCommands(_Pool(connection))  # type: ignore[arg-type]

    assert (
        await repository.claim_job(
            worker_id="worker-1",
            lease_ttl_seconds=30,
            host="runner-a",
            host_credential_hash="credential-hash",
            claim_attestation={"runner_capabilities": declared},
        )
        is None
    )
    assert isinstance(connection.job_capabilities_bind, str)
    assert json.loads(connection.job_capabilities_bind) == declared
