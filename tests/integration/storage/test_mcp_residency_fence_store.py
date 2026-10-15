"""The residency fence survives in-flight MCP requests and transactional writes in Postgres."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.integration.storage.test_agent_job_store import _reserve_shared_chat
from tests.integration.storage.test_credential_queries_store import (
    credentials as credentials,
    db as db,
    store as store,
)
from tests.integration.storage.test_mcp_connection_store import _connect, _permission_binding

from cloud_agent import memory_tool
from cloud_agent.agent_memory import AgentMemoryTaskEnded, MemoryTask
from cloud_agent.api import mcp_relay
from cloud_agent.api.deps import get_storage
from cloud_agent.tokens import mint_residency_mcp_token

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


def _fence(job, attempt, sandbox):
    return {
        "job_id": job["id"],
        "attempt_id": attempt["attempt_id"],
        "lease_generation": attempt["lease_generation"],
        "residency_generation": sandbox["residency_generation"],
    }


async def _revoke(db, store, job, attempt, sandbox, revocation):
    if revocation == "release":
        assert (
            await store.residency.release_residency(
                thread_id=job["thread_id"],
                generation=sandbox["residency_generation"],
                host="shared-runner",
            )
            is not None
        )
    elif revocation == "rotate":
        replacement = await store.residency.establish_residency(
            thread_id=job["thread_id"],
            attempt_id=attempt["attempt_id"],
            lease_generation=attempt["lease_generation"],
            host="shared-runner",
            runtime_id="claude-code",
            runtime_version=sandbox["runtime_version"],
            sandbox_id=sandbox["sandbox_id"],
            ttl_seconds=1800,
        )
        assert replacement["generation"] > sandbox["residency_generation"]
    else:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_thread_sandbox_memberships "
                "SET residency_lease_expires_at = clock_timestamp() - INTERVAL '1 second' "
                "WHERE thread_id = $1",
                job["thread_id"],
            )


@pytest.mark.parametrize("revocation", ["release", "rotate", "expire"])
async def test_revoked_residency_cannot_commit_memory_after_body_wait(
    db, store, credentials, monkeypatch, revocation
):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "residency-fence-test-signing-key")
    job, attempt, sandbox = await _reserve_shared_chat(store)
    token = mint_residency_mcp_token(
        thread_id=job["thread_id"],
        residency_generation=sandbox["residency_generation"],
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    app = FastAPI()
    app.include_router(mcp_relay.router)
    app.dependency_overrides[get_storage] = lambda: store
    reading = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed_body():
        reading.set()
        await proceed.wait()
        yield json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": memory_tool.SAVE,
                    "arguments": {"content": "This request used a revoked residency."},
                },
            }
        ).encode()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        task = asyncio.create_task(
            client.post("/v1/agent/mcp", content=delayed_body(), headers=headers)
        )
        await asyncio.wait_for(reading.wait(), 2)
        await _revoke(db, store, job, attempt, sandbox, revocation)
        # The residency can end independently of the still-running worker attempt.
        assert (
            await credentials.resolve_model_credential(
                job_id=job["id"],
                attempt_id=attempt["attempt_id"],
                lease_generation=attempt["lease_generation"],
            )
            is not None
        )
        proceed.set()
        response = await asyncio.wait_for(task, 3)
    assert response.status_code == 401
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_memories") == 0


@pytest.mark.parametrize("revocation", ["release", "rotate", "expire"])
async def test_storage_rechecks_residency_for_memory_and_web_search(
    db, store, credentials, revocation
):
    job, attempt, sandbox = await _reserve_shared_chat(store)
    fence = _fence(job, attempt, sandbox)
    task = MemoryTask(user_id=job["user_id"], **fence)
    assert await credentials.resolve_model_credential(**fence) is not None
    saved = await store.agent_memory.save_agent_memory(task, "A live memory.", all_projects=False)
    admission = await store.web_search.acquire_web_search(**fence)
    assert admission.reason == "admitted"
    assert await store.web_search.release_web_search(admission.call_id)

    await _revoke(db, store, job, attempt, sandbox, revocation)
    assert await credentials.resolve_model_credential(**fence) is None
    with pytest.raises(AgentMemoryTaskEnded):
        await store.agent_memory.save_agent_memory(task, "A revoked memory.", all_projects=False)
    with pytest.raises(AgentMemoryTaskEnded):
        await store.agent_memory.archive_agent_memory(task, saved["id"])
    admission = await store.web_search.acquire_web_search(**fence)
    assert admission.reason == "stale_fence"
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_web_search_admissions") == 1
        assert (
            await conn.fetchval("SELECT status FROM agent_memories WHERE id = $1", saved["id"])
            == "active"
        )


async def _approved(store, job, attempt, sandbox):
    connection = await _connect(store, job["user_id"])
    await store.mcp.replace_mcp_bindings(
        thread_id=job["thread_id"], bindings=[_permission_binding(connection, "github")]
    )
    binding = (await store.mcp.list_mcp_bindings(thread_id=job["thread_id"]))[0]
    fence = {
        **_fence(job, attempt, sandbox),
        "config_revision": connection["config_revision"],
        "authorization_epoch": connection["authorization_epoch"],
        "capability_revision": connection["capability_revision"],
        "policy_revision": binding["policy_revision"],
    }
    invocation = await store.mcp.create_mcp_invocation(
        **fence,
        thread_id=job["thread_id"],
        connection_id=connection["id"],
        capability_name="get_issue",
        arguments_digest="residency-call",
        arguments_ciphertext=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert invocation["residency_generation"] == sandbox["residency_generation"]
    assert (
        await store.mcp.decide_mcp_invocation(
            invocation_id=invocation["id"], approve=True, decided_by=job["user_id"]
        )
        is not None
    )
    return invocation, fence


@pytest.mark.parametrize("revocation", ["release", "rotate", "expire"])
async def test_approved_call_cannot_be_claimed_by_a_revoked_residency(db, store, revocation):
    job, attempt, sandbox = await _reserve_shared_chat(store)
    invocation, fence = await _approved(store, job, attempt, sandbox)
    await _revoke(db, store, job, attempt, sandbox, revocation)
    fence.pop("job_id")
    assert await store.mcp.claim_mcp_invocation(invocation_id=invocation["id"], **fence) is None
    assert (await store.mcp.get_mcp_invocation(invocation_id=invocation["id"]))[
        "status"
    ] == "approved"


async def test_approval_cannot_cross_residency_generations_or_token_kinds(store):
    job, attempt, sandbox = await _reserve_shared_chat(store)
    invocation, fence = await _approved(store, job, attempt, sandbox)
    lookup = {
        "connection_id": invocation["connection_id"],
        "capability_name": invocation["capability_name"],
        "arguments_digest": invocation["arguments_digest"],
    }
    for generation in (None, sandbox["residency_generation"] + 1):
        other = {**fence, "residency_generation": generation}
        assert await store.mcp.find_approved_mcp_invocation(**lookup, **other) is None
        other.pop("job_id")
        assert await store.mcp.claim_mcp_invocation(invocation_id=invocation["id"], **other) is None

    assert await store.mcp.find_approved_mcp_invocation(**lookup, **fence) is not None
    fence.pop("job_id")
    assert await store.mcp.claim_mcp_invocation(invocation_id=invocation["id"], **fence) is not None


async def test_memory_waiting_for_the_membership_lock_observes_revocation(db, store):
    job, attempt, sandbox = await _reserve_shared_chat(store)
    task = MemoryTask(user_id=job["user_id"], **_fence(job, attempt, sandbox))
    async with db.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT thread_id FROM agent_thread_sandbox_memberships "
            "WHERE thread_id = $1 FOR UPDATE",
            job["thread_id"],
        )
        saving = asyncio.create_task(
            store.agent_memory.save_agent_memory(task, "After revocation.", all_projects=False)
        )
        waiting = False
        for _ in range(100):
            waiting = await blocker.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                blocker_pid,
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting, "the memory write did not lock its residency"
        await blocker.execute(
            "UPDATE agent_thread_sandbox_memberships SET residency_status = 'none' "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    with pytest.raises(AgentMemoryTaskEnded):
        await asyncio.wait_for(saving, 2)
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_memories") == 0


async def test_captured_attempt_is_rejected_when_membership_changes_turn(db, store, credentials):
    job, attempt, sandbox = await _reserve_shared_chat(store)
    fence = _fence(job, attempt, sandbox)
    assert await credentials.resolve_model_credential(**fence) is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships SET active_attempt_id = NULL "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    assert await credentials.resolve_model_credential(**fence) is None
    fence.pop("residency_generation")
    assert await credentials.resolve_model_credential(**fence) is not None


async def test_approval_rechecks_residency_expiry_after_waiting_for_connection(db, store):
    job, attempt, sandbox = await _reserve_shared_chat(store)
    invocation, fence = await _approved(store, job, attempt, sandbox)
    fence.pop("job_id")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships "
            "SET residency_lease_expires_at = clock_timestamp() + INTERVAL '400 milliseconds' "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    # Set the short lease before the dispatcher starts, then block its later
    # connection check, after it has read and locked the live membership.
    async with db.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR UPDATE",
            invocation["connection_id"],
        )
        claiming = asyncio.create_task(
            store.mcp.claim_mcp_invocation(invocation_id=invocation["id"], **fence)
        )
        waiting = False
        for _ in range(100):
            waiting = await blocker.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                blocker_pid,
            )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting, "the dispatcher did not wait for connection authority"
        await asyncio.sleep(0.55)
    assert await asyncio.wait_for(claiming, 2) is None
    assert (await store.mcp.get_mcp_invocation(invocation_id=invocation["id"]))[
        "status"
    ] == "approved"
