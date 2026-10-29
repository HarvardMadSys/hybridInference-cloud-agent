"""Local accounts against real Postgres: races, replay, versions and revocation.

Each case here is one only a real database settles: two transactions racing
for an address or a token, a sign-in that waited on a password reset or a
suspension, two administrators acting on one version, counters shared by
separate pools as separate workers would hold them, and the identity namespace
two modes could each try to claim.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
import pytest_asyncio
from tests.support.local_accounts import (
    LOCAL_SECRET,
    PASSWORD,
    act,
    close_pool,
    db_config,
    make_account,
    open_pool,
)

from cloud_agent.accounts import config, tokens
from cloud_agent.accounts.passwords import hash_password
from cloud_agent.domain.accounts import (
    AccountTransitionRefused,
    AccountVersionConflict,
    permissions_for,
)
from cloud_agent.identity_namespace import (
    IdentityNamespaceError,
    ensure_gateway_namespace,
    ensure_local_namespace,
)
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)


@pytest_asyncio.fixture
async def pool():
    created = await open_pool()
    try:
        yield created
    finally:
        await close_pool(created)


@pytest_asyncio.fixture
async def storage(pool):
    return Storage(pool)


async def _login_hash(storage: Storage, email: str) -> str:
    login = await storage.local_accounts.password_login(email)
    assert login is not None
    return login.password_hash


async def _open_session(storage: Storage, account_id: str, password_hash: str):
    token = tokens.new_session_token()
    record = await storage.local_accounts.create_session(
        session_id=tokens.new_session_id(),
        account_id=account_id,
        token_hash=tokens.session_hash(token),
        ttl_s=600,
        password_hash=password_hash,
    )
    return token, record


# ── Sign-up and emailed tokens ──────────────────────────────────────────


async def test_concurrent_sign_ups_for_one_address_create_exactly_one_account(storage, pool):
    hashed = await hash_password(PASSWORD)
    results = await asyncio.gather(
        *(
            storage.local_accounts.create_account(
                account_id=tokens.new_account_id(),
                email="Race@Example.org",
                email_normalized="race@example.org",
                password_hash=hashed,
            )
            for _ in range(8)
        )
    )
    assert results.count(True) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_local_accounts") == 1
        assert await conn.fetchval("SELECT count(*) FROM agent_local_admissions") == 1


async def test_a_verification_token_is_redeemed_once_even_concurrently(storage):
    account_id = await make_account(storage, "once@example.org", verified=False)
    token = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="verify_email",
        token_hash=tokens.email_token_hash(token),
        ttl_s=60,
    )
    redeemed = await asyncio.gather(
        *(
            storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(token))
            for _ in range(6)
        )
    )
    assert redeemed.count(account_id) == 1
    assert (await storage.local_accounts.get_account(account_id)).email_verified


async def test_a_newer_token_supersedes_and_a_reset_token_never_verifies(storage):
    account_id = await make_account(storage, "newer@example.org", verified=False)
    first, second = tokens.new_email_token(), tokens.new_email_token()
    for token in (first, second):
        await storage.local_accounts.issue_email_token(
            account_id=account_id,
            purpose="verify_email",
            token_hash=tokens.email_token_hash(token),
            ttl_s=60,
        )
    assert (
        await storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(first)) is None
    )
    reset = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="reset_password",
        token_hash=tokens.email_token_hash(reset),
        ttl_s=60,
    )
    assert (
        await storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(reset)) is None
    )
    assert await storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(second))


async def test_concurrent_issues_leave_one_open_token(storage, pool):
    account_id = await make_account(storage, "issue@example.org", verified=False)
    issued = [tokens.new_email_token() for _ in range(6)]
    await asyncio.gather(
        *(
            storage.local_accounts.issue_email_token(
                account_id=account_id,
                purpose="verify_email",
                token_hash=tokens.email_token_hash(token),
                ttl_s=60,
            )
            for token in issued
        )
    )
    async with pool.acquire() as conn:
        open_tokens = await conn.fetchval(
            "SELECT count(*) FROM agent_local_auth_tokens "
            "WHERE account_id = $1 AND purpose = 'verify_email' AND consumed_at IS NULL",
            account_id,
        )
    assert open_tokens == 1


async def test_an_expired_token_is_refused(storage, pool):
    account_id = await make_account(storage, "late@example.org", verified=False)
    token = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="verify_email",
        token_hash=tokens.email_token_hash(token),
        ttl_s=60,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_auth_tokens SET expires_at = NOW() - INTERVAL '1 second'"
        )
    assert (
        await storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(token)) is None
    )


# ── Sessions ────────────────────────────────────────────────────────────


async def test_a_reset_ends_every_session_and_every_open_link(storage):
    account_id = await make_account(storage, "reset@example.org")
    old_hash = await _login_hash(storage, "reset@example.org")
    token, record = await _open_session(storage, account_id, old_hash)
    assert record is not None
    reset = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="reset_password",
        token_hash=tokens.email_token_hash(reset),
        ttl_s=60,
    )
    new_hash = await hash_password("Another1Horse")
    assert await storage.local_accounts.reset_password(
        token_hash=tokens.email_token_hash(reset), password_hash=new_hash
    )
    assert await storage.local_accounts.session_owner(tokens.session_hash(token)) is None
    # Replayed, the link does nothing.
    assert (
        await storage.local_accounts.reset_password(
            token_hash=tokens.email_token_hash(reset), password_hash=old_hash
        )
        is None
    )
    assert await _login_hash(storage, "reset@example.org") == new_hash
    _, stale = await _open_session(storage, account_id, old_hash)
    assert stale is None


async def test_a_sign_in_waiting_on_a_reset_does_not_open_an_old_password_session(storage, pool):
    """The barrier the reviewers asked for: lock, change the hash, then let sign-in finish."""
    account_id = await make_account(storage, "barrier@example.org")
    old_hash = await _login_hash(storage, "barrier@example.org")
    new_hash = await hash_password("Another1Horse")
    conn = await asyncpg.connect(**db_config())
    try:
        transaction = conn.transaction()
        await transaction.start()
        await conn.execute(
            "SELECT 1 FROM agent_local_accounts WHERE id = $1 FOR UPDATE", account_id
        )
        signing_in = asyncio.create_task(_open_session(storage, account_id, old_hash))
        await asyncio.sleep(0.3)
        assert not signing_in.done()
        await conn.execute(
            "UPDATE agent_local_password_credentials SET password_hash = $2 WHERE account_id = $1",
            account_id,
            new_hash,
        )
        await conn.execute(
            "UPDATE agent_local_sessions SET revoked_at = NOW() WHERE account_id = $1", account_id
        )
        await transaction.commit()
        _, record = await asyncio.wait_for(signing_in, 5)
    finally:
        await conn.close()
    assert record is None
    async with pool.acquire() as check:
        assert (
            await check.fetchval(
                "SELECT count(*) FROM agent_local_sessions WHERE revoked_at IS NULL"
            )
            == 0
        )


async def test_suspension_and_sign_in_racing_leave_no_live_session(storage, pool):
    for round_no in range(5):
        email = f"race{round_no}@example.org"
        account_id = await make_account(storage, email, admission="approved")
        password_hash = await _login_hash(storage, email)
        await asyncio.gather(
            act(storage, account_id, "suspend"),
            *(_open_session(storage, account_id, password_hash) for _ in range(4)),
        )
        async with pool.acquire() as conn:
            live = await conn.fetchval(
                "SELECT count(*) FROM agent_local_sessions "
                "WHERE account_id = $1 AND revoked_at IS NULL",
                account_id,
            )
        assert live == 0


async def test_an_unverified_or_suspended_account_gets_no_session(storage):
    unverified = await make_account(storage, "unverified@example.org", verified=False)
    _, record = await _open_session(
        storage, unverified, await _login_hash(storage, "unverified@example.org")
    )
    assert record is None


# ── The waiting list and administration ─────────────────────────────────


async def test_applying_bumps_the_version_once_and_is_idempotent(storage):
    account_id = await make_account(storage, "apply@example.org")
    before = await storage.local_accounts.get_account(account_id)
    first = await storage.local_accounts.submit_application(account_id=account_id, purpose="why")
    again = await storage.local_accounts.submit_application(account_id=account_id, purpose="other")
    assert first.admission_status == again.admission_status == "pending"
    assert first.version == before.version + 1 == again.version
    assert again.purpose == "why"


async def test_a_stale_version_is_refused_and_one_of_two_racing_admins_wins(storage):
    account_id = await make_account(storage, "stale@example.org", admission="pending")
    record = await storage.local_accounts.get_account(account_id)
    results = await asyncio.gather(
        act(storage, account_id, "approve", expected_version=record.version),
        act(storage, account_id, "reject", expected_version=record.version),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert any(isinstance(result, AccountVersionConflict) for result in results)
    with pytest.raises(AccountVersionConflict):
        await act(storage, account_id, "revoke", expected_version=record.version)


async def test_an_admin_suspended_mid_request_changes_nothing(storage):
    admin = await make_account(storage, "admin@example.org", admission="approved", admin=True)
    target = await make_account(storage, "target@example.org", admission="pending")
    await act(storage, admin, "suspend")
    with pytest.raises(AccountTransitionRefused) as refused:
        await act(storage, target, "approve", actor_id=f"admin:{admin}", actor_account_id=admin)
    assert refused.value.code == "actor_not_admin"
    assert (await storage.local_accounts.get_account(target)).admission_status == "pending"


async def test_suspend_then_restore_leaves_no_access_until_approved_again(storage):
    admin = await make_account(storage, "was-admin@example.org", admission="approved", admin=True)
    await act(storage, admin, "suspend")
    result = await act(storage, admin, "restore")
    assert result.account.status == "active"
    assert result.account.admission_status == "revoked"
    assert permissions_for(result.account) == []
    # The suspension's cleanup is still open, so approval waits for it.
    with pytest.raises(AccountTransitionRefused) as refused:
        await act(storage, admin, "approve")
    assert refused.value.code == "access_cleanup_pending"


async def test_every_edit_is_audited_without_credentials(storage, pool):
    account_id = await make_account(storage, "audit@example.org", admission="approved")
    await act(storage, account_id, "revoke", reason="policy")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, actor_id, target_user_id, details FROM agent_admin_audit_log "
            "WHERE target_user_id = $1 ORDER BY id",
            account_id,
        )
    assert [row["action"] for row in rows] == [
        "agent.local_account.approve",
        "agent.local_account.revoke",
    ]
    text = " ".join(str(row["details"]) for row in rows)
    assert "audit@example.org" not in text and "argon2" not in text
    assert json.loads(rows[-1]["details"])["to"]["admission"] == "revoked"


async def test_a_revocation_opens_a_record_that_blocks_reapproval_until_closed(storage):
    account_id = await make_account(storage, "barrier2@example.org", admission="approved")
    revoked = await act(storage, account_id, "revoke")
    assert await storage.local_accounts.open_terminations(limit=10) == [
        (account_id, revoked.account.version)
    ]
    with pytest.raises(AccountTransitionRefused):
        await act(storage, account_id, "approve")
    revision = await storage.local_accounts.termination_revision(
        account_id=account_id, generation=revoked.account.version
    )
    assert await storage.local_accounts.record_termination_attempt(
        account_id=account_id,
        generation=revoked.account.version,
        revision=revision,
        complete=True,
        outcome="t",
    )
    async with storage.local_accounts.termination_lock(account_id) as held:
        assert held
        # A pass still running holds the lock: approval cannot commit under it.
        with pytest.raises(AccountTransitionRefused) as refused:
            await act(storage, account_id, "approve")
        assert refused.value.code == "access_cleanup_pending"
        async with storage.local_accounts.termination_lock(account_id) as second:
            assert second is False
    approved = await act(storage, account_id, "approve")
    assert permissions_for(approved.account) == ["agent.use"]


async def test_the_list_filters_by_admission_and_counts(storage):
    for index in range(3):
        await make_account(storage, f"p{index}@example.org", admission="pending")
    await make_account(storage, "n@example.org")
    pending, total = await storage.local_accounts.list_accounts(
        admission_status="pending", limit=2, offset=0
    )
    assert total == 3 and len(pending) == 2
    everyone, total_all = await storage.local_accounts.list_accounts(
        admission_status=None, limit=50, offset=0
    )
    assert total_all == 4 and len(everyone) == 4


async def test_a_reopen_bumps_the_revision_and_a_stale_close_leaves_it_open(storage):
    account_id = await make_account(storage, "revision@example.org", admission="approved")
    revoked = await act(storage, account_id, "revoke")
    generation = revoked.account.version
    accounts = storage.local_accounts
    observed = await accounts.termination_revision(account_id=account_id, generation=generation)
    assert observed == 1
    assert await accounts.reopen_termination(account_id)
    assert await accounts.termination_revision(account_id=account_id, generation=generation) == 2
    # A pass that read revision 1 cannot close revision 2.
    assert not await accounts.record_termination_attempt(
        account_id=account_id, generation=generation, revision=1, complete=True, outcome="x"
    )
    assert await accounts.open_terminations(limit=10) == [(account_id, generation)]
    assert await accounts.record_termination_attempt(
        account_id=account_id, generation=generation, revision=2, complete=True, outcome="x"
    )
    # Reopening a closed record opens it again, at a new revision.
    assert await accounts.reopen_termination(account_id)
    assert await accounts.termination_revision(account_id=account_id, generation=generation) == 3
    assert await accounts.open_terminations(limit=10) == [(account_id, generation)]


async def test_concurrent_reopens_and_a_stale_close_never_lose_an_intent(storage):
    account_id = await make_account(storage, "concurrent@example.org", admission="approved")
    revoked = await act(storage, account_id, "revoke")
    generation = revoked.account.version
    accounts = storage.local_accounts
    observed = await accounts.termination_revision(account_id=account_id, generation=generation)
    await asyncio.gather(
        *(accounts.reopen_termination(account_id) for _ in range(5)),
        accounts.record_termination_attempt(
            account_id=account_id,
            generation=generation,
            revision=observed,
            complete=True,
            outcome="x",
        ),
    )
    # Whatever the interleaving, at least one reopen is after the close's view.
    assert await accounts.termination_revision(account_id=account_id, generation=generation) == 6
    assert await accounts.open_terminations(limit=10) == [(account_id, generation)]


# ── The termination lock ────────────────────────────────────────────────


async def _lock_is_free(account_id: str) -> bool:
    """Whether a fresh session can take the account's termination lock right now."""
    from cloud_agent.storage.repositories.accounts import termination_lock_key

    conn = await asyncpg.connect(**db_config())
    try:
        key = termination_lock_key(account_id)
        free = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key)
        if free:
            await conn.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)
        return bool(free)
    finally:
        await conn.close()


async def test_a_pass_cancelled_while_holding_the_lock_releases_it(storage, pool):
    account_id = await make_account(storage, "cancel@example.org")
    entered = asyncio.Event()

    async def hold() -> None:
        async with storage.local_accounts.termination_lock(account_id) as held:
            assert held
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(hold())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _lock_is_free(account_id)
    async with storage.local_accounts.termination_lock(account_id) as held:
        assert held


async def test_a_failed_unlock_retires_the_connection_instead_of_pooling_the_lock(
    storage, pool, monkeypatch
):
    from cloud_agent.storage._sql import advisory

    account_id = await make_account(storage, "unlock@example.org")
    real = advisory.unlock_or_reset

    class LostUnlock:
        """The pooled connection, except that its unlock answer is lost."""

        def __init__(self, conn) -> None:
            self._conn = conn

        async def fetchval(self, query, *args):
            if "pg_advisory_unlock" in query:
                raise ConnectionError("unlock lost")
            return await self._conn.fetchval(query, *args)

        async def reset(self) -> None:
            await self._conn.reset()

        async def close(self) -> None:
            await self._conn.close()

    async def failing_unlock(conn, key):
        await real(LostUnlock(conn), key)

    monkeypatch.setattr(advisory, "unlock_or_reset", failing_unlock)
    async with storage.local_accounts.termination_lock(account_id) as held:
        assert held
    assert await _lock_is_free(account_id)


async def test_passes_for_many_accounts_cannot_exhaust_a_small_pool():
    from cloud_agent.application.owner_termination import terminate_owner_access

    small = await asyncpg.create_pool(**db_config(), min_size=1, max_size=4)
    try:
        storage = Storage(small)
        accounts = [
            await make_account(storage, f"many{index}@example.org", admission="approved")
            for index in range(10)
        ]
        for account_id in accounts:
            await act(storage, account_id, "revoke")
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    terminate_owner_access(storage, None, account_id, None)
                    for account_id in accounts
                )
            ),
            timeout=30,
        )
    finally:
        await small.close()
    assert results == [True] * 10


# ── Abuse counters shared across workers ────────────────────────────────


async def test_counters_are_shared_by_separate_pools(storage):
    other = await asyncpg.create_pool(**db_config(), min_size=1, max_size=2)
    try:
        second = Storage(other)
        key = tokens.rate_limit_key("login_ip", "192.0.2.1")
        waits = []
        for index in range(6):
            repo = (storage if index % 2 else second).local_accounts
            waits.append(
                await repo.hit_rate_limit(bucket="login_ip", key_hash=key, window_s=600, limit=4)
            )
    finally:
        await other.close()
    assert waits[:4] == [0.0] * 4
    assert all(wait > 0 for wait in waits[4:])


# ── The identity namespace ──────────────────────────────────────────────


async def test_local_mode_binds_an_empty_database_and_gateway_mode_then_refuses(pool):
    await ensure_local_namespace(pool)
    await ensure_local_namespace(pool)
    with pytest.raises(IdentityNamespaceError):
        await ensure_gateway_namespace(pool, "https://gateway.example")


async def test_a_gateway_bound_database_refuses_local_mode(pool):
    await ensure_gateway_namespace(pool, "https://gateway.example")
    with pytest.raises(IdentityNamespaceError):
        await ensure_local_namespace(pool)


async def test_owner_data_without_a_binding_refuses_local_mode(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_admin_audit_log (action, actor_id, admin_ip, target_user_id) "
            "VALUES ('x', 'a', 'ip', 'someone')"
        )
    with pytest.raises(IdentityNamespaceError):
        await ensure_local_namespace(pool)


async def test_concurrent_mixed_mode_startups_bind_exactly_one_namespace(pool):
    results = await asyncio.gather(
        *(
            ensure_local_namespace(pool)
            if index % 2
            else ensure_gateway_namespace(pool, "https://gateway.example")
            for index in range(8)
        ),
        return_exceptions=True,
    )
    async with pool.acquire() as conn:
        gateway = await conn.fetchval("SELECT count(*) FROM agent_identity_namespace")
        local = await conn.fetchval("SELECT count(*) FROM agent_local_identity_namespace")
    assert gateway + local == 1
    failures = [result for result in results if isinstance(result, Exception)]
    assert all(isinstance(failure, IdentityNamespaceError) for failure in failures)
    assert len(failures) == 4
