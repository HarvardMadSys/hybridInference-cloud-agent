"""A model save waiting for the account lock must see the committed admission.

A locking JOIN at READ COMMITTED can combine the updated account tuple with
the admission row from the statement's older snapshot. This test starts the
real save while the account is locked, proves PostgreSQL is blocking its
SELECT, and only then changes the admission and commits. Locking the account
alone followed by a fresh admission statement must refuse both write paths.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from tests.integration.storage.test_model_connection_verified_save import _WORKS
from tests.support.local_accounts import LOCAL_SECRET, close_pool, make_account, open_pool
from tests.unit.test_model_connection_anthropic_check import _KEY as ROTATED_PROVIDER_KEY
from tests.unit.test_model_connection_compat_check import _KEY as PROVIDER_KEY

from cloud_agent.application.model_connections import model_connection_cipher
from cloud_agent.domain.model_connections import ModelConnectionAccessEnded
from cloud_agent.identity_namespace import ensure_local_namespace
from cloud_agent.storage.assembly import Storage

if TYPE_CHECKING:
    import asyncpg

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_CONNECTION_ID = "mconn_" + "a" * 32
_MODEL = "same-model"


@pytest_asyncio.fixture
async def local_model_store(monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_SESSION_SECRET", LOCAL_SECRET)
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", LOCAL_SECRET)
    # open_pool enforces the *_test_* database name, migrates and resets it.
    pool = await open_pool()
    try:
        await ensure_local_namespace(pool)
        yield pool, Storage(pool)
    finally:
        await close_pool(pool)


async def _rows(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch("SELECT * FROM agent_model_connections")]


async def _wait_for_account_lock(pool: asyncpg.Pool, blocker_pid: int, saving: asyncio.Task) -> int:
    """Observe the save's real blocked SELECT; no delay guesses whether it started."""
    async with asyncio.timeout(10), pool.acquire() as observer:
        while True:
            if saving.done():
                await saving
                pytest.fail("Model save completed without waiting for the account lock")
            waiter = await observer.fetchrow(
                """
                SELECT pid, query FROM pg_stat_activity
                WHERE datname = current_database() AND wait_event_type = 'Lock'
                  AND $1 = ANY(pg_blocking_pids(pid))
                  AND query ILIKE '%agent_local_accounts%'
                """,
                blocker_pid,
            )
            if waiter is not None:
                assert waiter["query"].lstrip().upper().startswith("SELECT")
                return int(waiter["pid"])


@pytest.mark.parametrize("operation", ["create", "verified_update"])
async def test_model_save_reads_fresh_admission_after_waiting_for_account_lock(
    local_model_store, operation
):
    pool, storage = local_model_store
    owner = await make_account(storage, "model-save-race@example.org", admission="approved")
    connections = storage.model_connections
    values = {
        "id": _CONNECTION_ID,
        "name": "Owner model",
        "base_url": "https://models.example/v1",
        "models": [_MODEL],
        "api_key_ciphertext": model_connection_cipher().encrypt(PROVIDER_KEY),
        # Verification itself is covered by the HTTP regressions; here it has
        # already succeeded and these are its native protocol facts.
        "model_checks": {_MODEL: _WORKS},
    }
    existing = None
    if operation == "verified_update":
        existing = await connections.create_model_connection(user_id=owner, values=values)
    before = await _rows(pool)

    async def save():
        if existing is None:
            return await connections.create_model_connection(user_id=owner, values=values)
        return await connections.commit_verified_update(
            user_id=owner,
            connection_id=existing["id"],
            expected=existing,
            changes={"api_key_ciphertext": model_connection_cipher().encrypt(ROTATED_PROVIDER_KEY)},
            model_checks={_MODEL: _WORKS},
        )

    saving = None
    try:
        async with pool.acquire() as revoking, revoking.transaction():
            assert await revoking.fetchval("SHOW transaction_isolation") == "read committed"
            assert (
                await revoking.fetchval(
                    "SELECT id FROM agent_local_accounts WHERE id = $1 FOR UPDATE", owner
                )
                == owner
            )
            blocker_pid = revoking.get_server_pid()
            saving = asyncio.create_task(save())
            waiter_pid = await _wait_for_account_lock(pool, blocker_pid, saving)
            assert waiter_pid != blocker_pid and not saving.done()

            # Match the admin writer's order: account first, then admission.
            # The save's SELECT has already started with admission=approved.
            await revoking.execute(
                "UPDATE agent_local_accounts SET version = version + 1, updated_at = NOW() "
                "WHERE id = $1",
                owner,
            )
            changed = await revoking.execute(
                "UPDATE agent_local_admissions SET status = 'revoked' "
                "WHERE account_id = $1 AND status = 'approved'",
                owner,
            )
            assert changed == "UPDATE 1"
            assert not saving.done(), "The account lock must stay held until revocation commits"

        with pytest.raises(ModelConnectionAccessEnded):
            await asyncio.wait_for(saving, timeout=5)
    finally:
        if saving is not None:
            if not saving.done():
                saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)

    account = await storage.local_accounts.get_account(owner)
    assert account is not None and account.admission_status == "revoked"
    assert await _rows(pool) == before, "The stale admission must not insert or rotate a key"
