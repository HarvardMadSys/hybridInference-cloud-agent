"""A password reset cannot leave a session opened with the previous password.

At READ COMMITTED, a locking JOIN may wait on the account row and then return
the updated account alongside a login row from its older statement snapshot.
Checking that login's old hash would open a session after reset revoked them.
The reset and session calls here are real repository methods; PostgreSQL's
lock state, rather than a delay, decides when the reset may continue.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.integration.storage.test_host_pool_store import db as db, store as store

if TYPE_CHECKING:
    import asyncpg

    from cloud_agent.ports import LocalAccountRepository
    from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ACCOUNT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
# The repository compares already-verified hash fingerprints; hashing itself
# is outside this storage regression. These are synthetic, never credentials.
_OLD_HASH = "$argon2id$storage-test-old"
_NEW_HASH = "$argon2id$storage-test-new"
_VERIFY_TOKEN = b"v" * 32
_RESET_TOKEN = b"r" * 32
_EXISTING_SESSION = b"e" * 32
_RACING_SESSION = b"o" * 32
_NEW_SESSION = b"n" * 32


async def _verified_account(store: Storage) -> LocalAccountRepository:
    accounts = store.local_accounts
    assert await accounts.create_account(
        account_id=_ACCOUNT_ID,
        email="session-race@example.org",
        email_normalized="session-race@example.org",
        password_hash=_OLD_HASH,
    )
    await accounts.issue_email_token(
        account_id=_ACCOUNT_ID, purpose="verify_email", token_hash=_VERIFY_TOKEN, ttl_s=3600
    )
    assert await accounts.verify_email(token_hash=_VERIFY_TOKEN) == _ACCOUNT_ID
    await accounts.issue_email_token(
        account_id=_ACCOUNT_ID, purpose="reset_password", token_hash=_RESET_TOKEN, ttl_s=3600
    )
    return accounts


async def _session(accounts: LocalAccountRepository, token_hash: bytes, password_hash: str):
    return await accounts.create_session(
        session_id=token_hash.hex(),
        account_id=_ACCOUNT_ID,
        token_hash=token_hash,
        ttl_s=3600,
        password_hash=password_hash,
    )


async def _wait_for_session_select(db: asyncpg.Pool, reset_pid: int, opening: asyncio.Task) -> int:
    """Wait until the real session SELECT is blocked by the reset transaction."""
    async with asyncio.timeout(10), db.acquire() as observer:
        while True:
            if opening.done():
                result = await opening
                pytest.fail(f"Session creation escaped the reset's account lock: {result!r}")
            waiter = await observer.fetchrow(
                """
                SELECT pid, query FROM pg_stat_activity
                WHERE datname = current_database() AND wait_event_type = 'Lock'
                  AND $1 = ANY(pg_blocking_pids(pid))
                  AND query ILIKE '%agent_local_accounts%'
                """,
                reset_pid,
            )
            if waiter is not None:
                assert waiter["query"].lstrip().upper().startswith("SELECT")
                return int(waiter["pid"])
            # Each query yields to the other tasks. No sleep guesses whether
            # create_session has started or reached the contested row.


async def test_old_password_cannot_open_a_session_while_reset_commits(
    db: asyncpg.Pool, store: Storage, monkeypatch: pytest.MonkeyPatch
):
    """A reset that owns the account lock wins over an old-password session."""
    accounts = await _verified_account(store)
    assert await _session(accounts, _EXISTING_SESSION, _OLD_HASH) is not None
    assert await accounts.session_owner(_EXISTING_SESSION) is not None

    reset_locked = asyncio.Event()
    continue_reset = asyncio.Event()
    reset_pid = None
    consume_token = accounts._consume_token

    async def pause_after_reset_lock(conn, token_hash, purpose):
        nonlocal reset_pid
        account_id = await consume_token(conn, token_hash, purpose)
        assert account_id == _ACCOUNT_ID and purpose == "reset_password"
        assert await conn.fetchval("SHOW transaction_isolation") == "read committed"
        reset_pid = int(await conn.fetchval("SELECT pg_backend_pid()"))
        reset_locked.set()
        await continue_reset.wait()
        return account_id

    # Pause inside the real reset transaction after its account/token locks.
    # Its password update, revoke-all, version bump and commit remain intact.
    monkeypatch.setattr(accounts, "_consume_token", pause_after_reset_lock)
    resetting = asyncio.create_task(
        accounts.reset_password(token_hash=_RESET_TOKEN, password_hash=_NEW_HASH)
    )
    opening = None
    try:
        async with asyncio.timeout(10):
            await reset_locked.wait()
            assert reset_pid is not None
            opening = asyncio.create_task(_session(accounts, _RACING_SESSION, _OLD_HASH))
            session_pid = await _wait_for_session_select(db, reset_pid, opening)
            assert session_pid != reset_pid and not resetting.done()
            continue_reset.set()
            reset, opened_account = await asyncio.gather(resetting, opening)
    finally:
        # A failed assertion or lock timeout must not strand a transaction in
        # the shared test pool and block the fixture's schema cleanup.
        continue_reset.set()
        tasks = [task for task in (resetting, opening) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert reset is not None and reset.account_id == _ACCOUNT_ID
    login = await accounts.password_login("session-race@example.org")
    assert login is not None and login.password_hash == _NEW_HASH
    assert await accounts.session_owner(_EXISTING_SESSION) is None
    assert opened_account is None, "The stale password opened a session after reset revoked all"
    assert await accounts.session_owner(_RACING_SESSION) is None
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_local_sessions WHERE account_id = $1 "
                "AND revoked_at IS NULL",
                _ACCOUNT_ID,
            )
            == 0
        )


async def test_new_password_can_open_a_session_after_reset(store: Storage):
    """Refusing a stale password must not refuse the newly committed password."""
    accounts = await _verified_account(store)
    reset = await accounts.reset_password(token_hash=_RESET_TOKEN, password_hash=_NEW_HASH)
    assert reset is not None and reset.account_id == _ACCOUNT_ID
    assert await _session(accounts, _RACING_SESSION, _OLD_HASH) is None
    opened = await _session(accounts, _NEW_SESSION, _NEW_HASH)
    assert opened is not None and opened.id == _ACCOUNT_ID
    owner = await accounts.session_owner(_NEW_SESSION)
    assert owner is not None and owner.account.id == _ACCOUNT_ID
