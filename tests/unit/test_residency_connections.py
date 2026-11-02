"""A residency holds its connection's key, bound to its own generation, including Fork retries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from tests.support.owners import CONNECTION_ID, TEST_PROVIDER_KEY, connection_row

from cloud_agent.api import worker
from cloud_agent.application import session_fork
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.schemas import WorkerSandboxResidencyRequest
from cloud_agent.tokens import ENV_SIGNING_SECRET

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv(ENV_SIGNING_SECRET, "residency-connection-test-secret")
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "residency-connection-test-key")


def _store(*, establish: Any, connection_id: str | None = CONNECTION_ID) -> SimpleNamespace:
    bindings: list[tuple[str, int]] = []

    async def bind_residency(*, thread_id, generation, **_kwargs):
        bindings.append((thread_id, generation))
        return True

    return SimpleNamespace(
        bindings=bindings,
        conversation_queries=SimpleNamespace(),
        host_pool=SimpleNamespace(host_for_job=AsyncMock(return_value={"host": "runner-a"})),
        residency=SimpleNamespace(
            establish_residency=establish, release_residency=AsyncMock(return_value=True)
        ),
        model_connections=SimpleNamespace(
            get_model_connection=AsyncMock(
                return_value=connection_row("owner", models=("model-1",))
            ),
            bind_residency=bind_residency,
        ),
        attempt_commands=SimpleNamespace(transition=AsyncMock(return_value=True)),
        sandbox=SimpleNamespace(record_shared_thread_residency=AsyncMock(return_value=True)),
    )


def _worker_claims(monkeypatch) -> None:
    monkeypatch.setattr(
        worker,
        "_worker_claims",
        lambda _authorization: {"job_id": "ajob_1", "attempt_id": "aatt_1", "lease_generation": 1},
    )
    monkeypatch.setattr(worker, "_worker_thread_id", AsyncMock(return_value="athr_1"))


def _establish(connection_id: str | None) -> Any:
    generation = 0

    async def establish(**_kwargs):
        nonlocal generation
        generation += 1
        return {
            "thread_id": "athr_1",
            "user_id": "owner",
            "model": "model-1",
            "runtime": "claude-code",
            "model_connection_id": connection_id,
            "generation": generation,
            "establishing_attempt_id": "aatt_1",
        }

    return establish


async def test_a_replaced_process_binds_its_own_generation_to_the_connection(monkeypatch):
    _worker_claims(monkeypatch)
    store = _store(establish=_establish(CONNECTION_ID))
    body = WorkerSandboxResidencyRequest(sandbox_id="asbx_1", runtime_id="claude-code")

    first = await worker.worker_establish_shared_sandbox_residency(
        "ajob_1", body, authorization="worker-token", store=store
    )
    replacement = await worker.worker_establish_shared_sandbox_residency(
        "ajob_1", body, authorization="worker-token", store=store
    )

    assert replacement.generation > first.generation
    assert store.bindings == [("athr_1", first.generation), ("athr_1", replacement.generation)]
    assert replacement.credential == first.credential == TEST_PROVIDER_KEY
    assert store.sandbox.record_shared_thread_residency.await_count == 2


async def test_a_residency_with_no_connection_is_released_before_anything_holds_a_key(monkeypatch):
    _worker_claims(monkeypatch)
    store = _store(establish=_establish(None))
    body = WorkerSandboxResidencyRequest(sandbox_id="asbx_1", runtime_id="claude-code")

    with pytest.raises(HTTPException) as refused:
        await worker.worker_establish_shared_sandbox_residency(
            "ajob_1", body, authorization="worker-token", store=store
        )

    assert refused.value.status_code == 403
    store.residency.release_residency.assert_awaited_once_with(
        thread_id="athr_1", generation=1, host="runner-a"
    )
    store.attempt_commands.transition.assert_awaited_once()
    assert store.bindings == []
    store.sandbox.record_shared_thread_residency.assert_not_awaited()


async def test_a_residency_the_connection_will_not_bind_gets_no_key(monkeypatch):
    _worker_claims(monkeypatch)
    store = _store(establish=_establish(CONNECTION_ID))
    store.model_connections.bind_residency = AsyncMock(return_value=False)
    body = WorkerSandboxResidencyRequest(sandbox_id="asbx_1", runtime_id="claude-code")

    with pytest.raises(HTTPException):
        await worker.worker_establish_shared_sandbox_residency(
            "ajob_1", body, authorization="worker-token", store=store
        )

    store.residency.release_residency.assert_awaited_once()
    store.sandbox.record_shared_thread_residency.assert_not_awaited()


_FORK = {
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
    "source_job": {
        "user_id": "owner",
        "model": "model-1",
        "runtime": "claude-code",
        "thread_id": "athr_source",
        "model_connection_id": CONNECTION_ID,
    },
}


def _fork_store() -> SimpleNamespace:
    generation = 0

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
            }
            for role in ("source", "target")
        }

    store = _store(establish=None)
    store.session_fork = SimpleNamespace(
        # Model takeover after the coordinator's lease expired.
        claim_session_fork_materialization=AsyncMock(return_value=True),
        establish_session_fork_residencies=establish,
        record_session_fork_materialized=AsyncMock(return_value={"state": "materialized"}),
    )
    return store


async def test_a_fork_retried_after_a_lost_host_answer_binds_both_members_again(monkeypatch):
    """Takeover of the same operation binds each member's new generation to the connection."""
    store = _fork_store()
    launches: list[tuple[str, str]] = []

    async def fork(_source_thread_id, **kwargs):
        launches.append(
            tuple(kwargs[f"{role}_launch"]["credential"] for role in ("source", "target"))
        )
        if len(launches) == 1:
            raise SandboxManagerError(503, "host response was lost")
        return {"operation_id": kwargs["operation_id"]}

    monkeypatch.setattr(
        session_fork,
        "sandbox_manager_for_job",
        AsyncMock(return_value=SimpleNamespace(fork_shared_session=fork)),
    )

    with pytest.raises(HTTPException):
        await session_fork.materialize_session_fork(_FORK, job_id="ajob_1", job_store=store)
    await session_fork.materialize_session_fork(_FORK, job_id="ajob_1", job_store=store)

    assert launches == [(TEST_PROVIDER_KEY, TEST_PROVIDER_KEY)] * 2
    assert store.bindings == [
        ("athr_source", 1),
        ("athr_target", 1),
        ("athr_source", 2),
        ("athr_target", 2),
    ]
    store.session_fork.record_session_fork_materialized.assert_awaited_once()


async def test_a_fork_whose_turn_has_no_connection_is_refused_before_any_host_call(monkeypatch):
    store = _fork_store()
    fork = AsyncMock()
    monkeypatch.setattr(
        session_fork,
        "sandbox_manager_for_job",
        AsyncMock(return_value=SimpleNamespace(fork_shared_session=fork)),
    )
    source_job = {**_FORK["source_job"], "model_connection_id": None}

    with pytest.raises(HTTPException):
        await session_fork.materialize_session_fork(
            {**_FORK, "source_job": source_job}, job_id="ajob_1", job_store=store
        )

    fork.assert_not_awaited()
    store.session_fork.claim_session_fork_materialization.assert_not_awaited()
