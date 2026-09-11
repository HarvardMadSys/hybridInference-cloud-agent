"""A resent submission is the same submission; an administrator is a person.

Both halves of ``0002_request_keys_and_actors``: the key an owner sends with
a create or follow-up returns the job that key already made, refuses the key
under different content, and survives two sends racing; and the audit trail
carries who acted, not only from where.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import asyncpg
import pytest
import pytest_asyncio

from cloud_agent.domain.requests import request_fingerprint
from tests.integration.storage.test_agent_job_store import (
    _create_job,
    pool as _shared_pool,  # noqa: F401
    store as _shared_store,  # noqa: F401
)

if TYPE_CHECKING:
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture(name="store")
async def _store(_shared_store: Storage) -> Storage:  # noqa: F811
    """The clean store the job-store tests use, under the same name."""
    return _shared_store


@pytest_asyncio.fixture(name="pool")
async def _pool(_shared_pool):  # noqa: F811
    """That store's pool, for the racing insert these tests stage directly."""
    return _shared_pool


_CREATE = {"repo": "example-org/example-repo", "task_prompt": "fix the flaky test"}


async def test_a_resent_create_returns_the_job_the_first_send_made(pool, store: Storage):
    key = "submit-1"
    fingerprint = request_fingerprint("create", _CREATE)
    first = await _create_job(store, client_request_id=key, client_request_fingerprint=fingerprint)
    again = await _create_job(store, client_request_id=key, client_request_fingerprint=fingerprint)
    assert again["id"] == first["id"]
    assert again["thread_id"] == first["thread_id"]
    assert again["title"] == first["title"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_jobs") == 1
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_threads") == 1


async def test_the_same_key_under_different_content_is_refused(pool, store: Storage):
    key = "submit-1"
    first = await _create_job(
        store,
        client_request_id=key,
        client_request_fingerprint=request_fingerprint("create", _CREATE),
    )
    edited = await _create_job(
        store,
        task_prompt="fix the other test",
        client_request_id=key,
        client_request_fingerprint=request_fingerprint(
            "create", {**_CREATE, "task_prompt": "fix the other test"}
        ),
    )
    assert edited == {"error": "client_request_conflict"}
    async with pool.acquire() as conn:
        assert await conn.fetch("SELECT id FROM agent_jobs") == [(first["id"],)]


async def test_keys_are_scoped_to_their_owner(store: Storage):
    fingerprint = request_fingerprint("create", _CREATE)
    mine = await _create_job(store, client_request_id="k", client_request_fingerprint=fingerprint)
    theirs = await _create_job(
        store, user_id="user-2", client_request_id="k", client_request_fingerprint=fingerprint
    )
    assert theirs["id"] != mine["id"]


async def test_two_sends_racing_produce_one_job(pool, store: Storage):
    fingerprint = request_fingerprint("create", _CREATE)
    results = await asyncio.gather(
        *[
            _create_job(store, client_request_id="race", client_request_fingerprint=fingerprint)
            for _ in range(6)
        ]
    )
    assert len({job["id"] for job in results}) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_jobs") == 1
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_threads") == 1


async def test_a_resent_follow_up_returns_the_turn_the_first_send_made(pool, store: Storage):
    parent = await _create_job(store)
    payload = {"parent_job_id": parent["id"], "prompt": "and then"}
    fingerprint = request_fingerprint("follow_up", payload)
    first = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"],
        user_id="user-1",
        prompt="and then",
        client_request_id="turn-2",
        client_request_fingerprint=fingerprint,
    )
    again = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"],
        user_id="user-1",
        prompt="and then",
        client_request_id="turn-2",
        client_request_fingerprint=fingerprint,
    )
    assert first is not None and again is not None
    assert again["id"] == first["id"] and again["turn_no"] == 2
    edited = await store.conversation_commands.create_follow_up(
        parent_job_id=parent["id"],
        user_id="user-1",
        prompt="and then something else",
        client_request_id="turn-2",
        client_request_fingerprint=request_fingerprint(
            "follow_up", {**payload, "prompt": "and then something else"}
        ),
    )
    assert edited == {"error": "client_request_conflict"}
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_jobs") == 2


async def test_a_key_travels_with_its_fingerprint(pool, store: Storage):
    with pytest.raises(ValueError):
        await _create_job(store, client_request_id="alone")
    async with pool.acquire() as conn:
        job = await _create_job(store)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_jobs SET client_request_id = 'alone' WHERE id = $1", job["id"]
            )


async def test_an_admin_action_records_who_acted(pool, store: Storage):
    row_id = await store.audit.record_admin_action(
        action="agent.runner_host.drain",
        actor_id="admin-7",
        admin_ip="203.0.113.9",
        details={"host": "runner-a"},
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT actor_id, admin_ip, action FROM agent_admin_audit_log WHERE id = $1", row_id
        )
        assert dict(row) == {
            "actor_id": "admin-7",
            "admin_ip": "203.0.113.9",
            "action": "agent.runner_host.drain",
        }
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO agent_admin_audit_log (action, admin_ip) VALUES ('x', '203.0.113.9')"
            )
