"""Grant mint identities follow the resident generation, including Fork retries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from cloud_agent.api import worker
from cloud_agent.application import residency, session_fork
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.schemas import WorkerSandboxResidencyRequest
from cloud_agent.tokens import ENV_SIGNING_SECRET


class IdempotentGrants:
    """The gateway keeps a minted grant even when that grant was revoked."""

    def __init__(self) -> None:
        self.issued: dict[tuple[str, str], dict[str, str]] = {}
        self.revoked: set[str] = set()

    async def mint(self, *, job_id: str, attempt_id: str, **_kwargs: Any) -> dict[str, str]:
        key = (job_id, attempt_id)
        if key not in self.issued:
            number = len(self.issued) + 1
            self.issued[key] = {"grant_id": f"grant-{number}", "token": f"token-{number}"}
        return self.issued[key]

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.add(grant_id)
        return True

    def live(self, token: str) -> bool:
        return any(
            grant["token"] == token and grant["grant_id"] not in self.revoked
            for grant in self.issued.values()
        )


@pytest.mark.parametrize("attempt_id", ["aatt_1", "fork-source:asfork_1", "fork-target:asfork_1"])
async def test_mint_retries_reuse_a_grant_but_a_new_resident_generation_does_not(attempt_id):
    """A rotated resident cannot reuse the capability its predecessor lost."""
    grants = IdempotentGrants()
    store = SimpleNamespace(link_residency_grant=AsyncMock(return_value=True))
    row = {
        "thread_id": "athr_1",
        "user_id": "owner",
        "model": "model-1",
        "generation": 1,
        "establishing_attempt_id": attempt_id,
    }

    first = await residency._mint_residency_grant(store, grants, row=row)
    repeated = await residency._mint_residency_grant(store, grants, row=row)
    assert repeated == first
    assert len(grants.issued) == 1
    assert grants.revoked == set()
    await grants.revoke(next(iter(grants.issued.values()))["grant_id"])

    replacement = await residency._mint_residency_grant(store, grants, row={**row, "generation": 2})

    assert replacement != first
    assert grants.live(replacement)
    assert not grants.live(first)
    assert len(grants.issued) == 2


async def test_a_grant_the_residency_will_not_link_is_revoked_and_not_returned():
    """Expiry and takeover both refuse the link; the minted token must not leave.

    ``link_residency_grant`` reports either failure as false. The mint path
    revokes that grant and returns no credential, including when the refusal
    is an already-expired lease rather than a generation change.
    """
    grants = IdempotentGrants()
    store = SimpleNamespace(link_residency_grant=AsyncMock(return_value=False))
    row = {
        "thread_id": "athr_1",
        "user_id": "owner",
        "model": "model-1",
        "generation": 1,
        "establishing_attempt_id": "aatt_1",
    }

    assert await residency._mint_residency_grant(store, grants, row=row) is None

    issued = next(iter(grants.issued.values()))
    assert grants.revoked == {issued["grant_id"]}
    assert not grants.live(issued["token"])


async def test_a_failed_orphan_revoke_still_never_returns_rejected_grant():
    """A cleanup failure cannot turn a rejected late link into authority."""
    grants = IdempotentGrants()
    grants.revoke = AsyncMock(side_effect=RuntimeError("gateway is down"))
    store = SimpleNamespace(link_residency_grant=AsyncMock(return_value=False))
    row = {
        "thread_id": "athr_1",
        "user_id": "owner",
        "model": "model-1",
        "generation": 1,
        "establishing_attempt_id": "aatt_1",
    }

    credential = await residency._mint_residency_grant(store, grants, row=row)

    assert credential is None
    grant_id = next(iter(grants.issued.values()))["grant_id"]
    grants.revoke.assert_awaited_once_with(grant_id)


@pytest.mark.parametrize("revoke_raises", [False, True])
async def test_worker_replacement_revokes_only_its_previous_resident_grant(
    monkeypatch, revoke_raises
):
    """Replacing a process within one attempt ends its predecessor's grant."""
    # The answer also carries the residency's MCP credential, which is signed.
    monkeypatch.setenv(ENV_SIGNING_SECRET, "residency-grant-test-secret")
    grants = IdempotentGrants()
    linked: str | None = None
    generation = 0

    async def establish(**_kwargs):
        nonlocal generation
        generation += 1
        return {
            "thread_id": "athr_1",
            "user_id": "owner",
            "model": "model-1",
            "generation": generation,
            "establishing_attempt_id": "aatt_1",
            "previous_grant_id": linked,
        }

    async def link(*, thread_id, generation, grant_id):
        nonlocal linked
        linked = grant_id
        return True

    if revoke_raises:
        grants.revoke = AsyncMock(side_effect=RuntimeError("gateway is down"))
    store = SimpleNamespace(
        conversation_queries=SimpleNamespace(),
        host_pool=SimpleNamespace(host_for_job=AsyncMock(return_value={"host": "runner-a"})),
        residency=SimpleNamespace(establish_residency=establish),
        grants=SimpleNamespace(link_residency_grant=link),
        sandbox=SimpleNamespace(record_shared_thread_residency=AsyncMock(return_value=True)),
    )
    monkeypatch.setattr(
        worker,
        "_worker_claims",
        lambda _authorization: {"job_id": "ajob_1", "attempt_id": "aatt_1", "lease_generation": 1},
    )
    monkeypatch.setattr(worker, "_worker_thread_id", AsyncMock(return_value="athr_1"))
    body = WorkerSandboxResidencyRequest(sandbox_id="asbx_1", runtime_id="claude-code")

    first = await worker.worker_establish_shared_sandbox_residency(
        "ajob_1", body, authorization="worker-token", store=store, grants=grants
    )
    previous_grant = linked
    replacement = await worker.worker_establish_shared_sandbox_residency(
        "ajob_1", body, authorization="worker-token", store=store, grants=grants
    )

    assert replacement.generation > first.generation
    assert replacement.credential != first.credential
    assert grants.live(replacement.credential)
    if revoke_raises:
        grants.revoke.assert_awaited_once_with(previous_grant)
    else:
        assert grants.revoked == {previous_grant}
        assert not grants.live(first.credential)


async def test_fork_after_an_unacknowledged_host_failure_mints_two_live_replacements(monkeypatch):
    """Takeover of the same Fork operation rotates both revoked credentials."""
    grants = IdempotentGrants()
    linked: dict[str, str] = {}
    generation = 0

    async def link(*, thread_id, generation, grant_id):
        linked[thread_id] = grant_id
        return True

    async def establish(**_kwargs):
        nonlocal generation
        generation += 1
        return {
            role: {
                "thread_id": f"athr_{role}",
                "user_id": "owner",
                "model": "model-1",
                "generation": generation,
                "establishing_attempt_id": f"fork-{role}:asfork_1",
                "previous_grant_id": linked.get(f"athr_{role}"),
            }
            for role in ("source", "target")
        }

    store = SimpleNamespace(
        host_pool=SimpleNamespace(),
        grants=SimpleNamespace(link_residency_grant=link),
        session_fork=SimpleNamespace(
            # Model takeover after the coordinator's lease expired.
            claim_session_fork_materialization=AsyncMock(return_value=True),
            establish_session_fork_residencies=establish,
            record_session_fork_materialized=AsyncMock(return_value={"state": "materialized"}),
        ),
    )
    credentials: list[tuple[str, str]] = []

    async def fork(_source_thread_id, **kwargs):
        current = tuple(kwargs[f"{role}_launch"]["credential"] for role in ("source", "target"))
        credentials.append(current)
        if len(credentials) == 1:
            raise SandboxManagerError(503, "host response was lost")
        assert all(grants.live(token) for token in current)
        return {"operation_id": kwargs["operation_id"]}

    monkeypatch.setattr(
        session_fork,
        "sandbox_manager_for_job",
        AsyncMock(return_value=SimpleNamespace(fork_shared_session=fork)),
    )
    operation = {
        "id": "asfork_1",
        "source_thread_id": "athr_source",
        "target_thread_id": "athr_target",
        "host": "runner-a",
        "sandbox_id": "asbx_1",
        "sandbox_generation": 1,
        "container_id": "container-1",
        "origin_thread_id": "athr_source",
        "runtime_id": "claude-code",
        "resident_slot_id": 0,
        "target_resident_slot_id": 1,
        "resident_slot_capacity": 8,
        "memory_limit": "4g",
        "cpu_limit": "2",
        "stop_timeout_seconds": 10,
        "source_native_session_id": "native-source",
        "source_native_turn_no": 1,
        "group_members": [{"thread_id": "athr_source"}],
    }

    with pytest.raises(HTTPException):
        await session_fork.materialize_session_fork(
            operation, job_id="ajob_1", job_store=store, grants=grants
        )
    await session_fork.materialize_session_fork(
        operation, job_id="ajob_1", job_store=store, grants=grants
    )

    assert set(credentials[0]).isdisjoint(credentials[1])
    assert all(not grants.live(token) for token in credentials[0])
    assert all(grants.live(token) for token in credentials[1])
    assert len(grants.issued) == 4
    store.session_fork.record_session_fork_materialized.assert_awaited_once()
