"""Account parity against real Postgres: identities, deletion, browsers, codes, policy.

Each case here is one only a real database settles: the migration carrying
existing accounts over and refusing a lossy downgrade, the shared access view
failing closed when a row is missing, identifier uniqueness under a race, a
browser's account limit and membership under its row lock, a phone code's
single use and attempt count, a deletion that opens the termination record and
leaves no session behind, and first-run setup succeeding exactly once.
"""

from __future__ import annotations

import asyncio

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
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.accounts import config, tokens
from cloud_agent.accounts.passwords import hash_password
from cloud_agent.domain.accounts import (
    AccountTransitionRefused,
    AccountVersionConflict,
    IdentifierTaken,
    SelfDeleteRefused,
    permissions_for,
)
from cloud_agent.ports.accounts import BrowserFull
from cloud_agent.storage._sql.accounts import local_access_ended
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
_ULID_B = "01ARZ3NDEKTSV4RRFFQ69G5FA2"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)
    config._derived.cache_clear()


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


async def _session(storage: Storage, account_id: str, **proof: str):
    token = tokens.new_session_token()
    session_id = tokens.new_session_id()
    record = await storage.local_accounts.create_session(
        session_id=session_id,
        account_id=account_id,
        token_hash=tokens.session_hash(token),
        ttl_s=600,
        **proof,
    )
    return token, session_id, record


async def _stopped(storage: Storage, account_id: str) -> None:
    """Run the real termination pass for the account's open record until it closes."""
    from cloud_agent.accounts.directory import LocalModeGrantClient
    from cloud_agent.application.owner_termination import terminate_owner_access

    for account, generation in await storage.local_accounts.open_terminations(limit=50):
        if account == account_id:
            assert await terminate_owner_access(
                storage, LocalModeGrantClient(), account_id, generation
            )
    assert not await storage.local_accounts.termination_open(account_id)


async def _hash(storage: Storage, account_id: str) -> str:
    stored = await storage.local_accounts.stored_password_hash(account_id)
    assert stored is not None
    return stored


# ── The migration keeps every account ────────────────────────────────────


async def test_the_migration_carries_accounts_over_and_refuses_a_lossy_downgrade(pool):
    database = db_config()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_local_accounts")
    downgrade_to(database, "0107_local_accounts")
    try:
        async with pool.acquire() as conn:
            for account_id, email, verified in (
                (_ULID_A, "Kept@Example.edu", True),
                (_ULID_B, "pending@example.edu", False),
            ):
                await conn.execute("INSERT INTO agent_local_accounts (id) VALUES ($1)", account_id)
                await conn.execute(
                    "INSERT INTO agent_local_password_logins "
                    "(account_id, email, email_normalized, password_hash, email_verified_at) "
                    "VALUES ($1, $2, lower($2), '$argon2id$kept', "
                    "CASE WHEN $3 THEN NOW() END)",
                    account_id,
                    email,
                    verified,
                )
                await conn.execute(
                    "INSERT INTO agent_local_admissions (account_id, status, submitted_at, "
                    "reviewed_at) VALUES ($1, 'approved', NOW(), NOW())",
                    account_id,
                )
        apply_migrations(database)
        accounts = Storage(pool).local_accounts
        kept = await accounts.get_account(_ULID_A)
        waiting = await accounts.get_account(_ULID_B)
        assert kept is not None and waiting is not None
        assert (kept.email, kept.identity_verified, kept.has_password) == (
            "Kept@Example.edu",
            True,
            True,
        )
        assert permissions_for(kept) == ["agent.use"]
        # An approved row whose email never verified still holds nothing.
        assert (waiting.identity_verified, permissions_for(waiting)) == (False, [])
        assert await accounts.stored_password_hash(_ULID_A) == "$argon2id$kept"

        # A phone identity has nowhere to go in the old table: refused, not dropped.
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_local_login_identities "
                "(account_id, kind, value, value_normalized, verified_at) "
                "VALUES ($1, 'phone', '+8613800138000', '+8613800138000', NOW())",
                _ULID_A,
            )
        with pytest.raises(Exception, match="refusing to downgrade"):
            downgrade_to(database, "0107_local_accounts")
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_local_login_identities WHERE kind = 'phone'")
        downgrade_to(database, "0107_local_accounts")
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT account_id, email, password_hash, email_verified_at IS NOT NULL AS v "
                "FROM agent_local_password_logins ORDER BY account_id"
            )
        assert [(r["account_id"], r["email"], r["password_hash"], r["v"]) for r in rows] == [
            (_ULID_A, "Kept@Example.edu", "$argon2id$kept", True),
            (_ULID_B, "pending@example.edu", "$argon2id$kept", False),
        ]
    finally:
        apply_migrations(database)


# ── One access rule, fail closed ─────────────────────────────────────────


async def test_an_account_missing_its_rows_has_no_access_in_every_gate(storage, pool):
    account_id = await make_account(storage, "gate@example.edu", admission="approved")
    gate = local_access_ended("$1")
    async with pool.acquire() as conn:
        assert not await conn.fetchval(f"SELECT {gate}", account_id)
        await conn.execute(
            "DELETE FROM agent_local_login_identities WHERE account_id = $1", account_id
        )
        assert await conn.fetchval(f"SELECT {gate}", account_id)
        assert not await conn.fetchval(
            "SELECT has_use FROM agent_local_account_access WHERE account_id = $1", account_id
        )
    record = await storage.local_accounts.get_account(account_id)
    assert record is not None and permissions_for(record) == []
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_local_admissions WHERE account_id = $1", account_id)
        # The view still has the account, and still denies it.
        assert await conn.fetchval(f"SELECT {gate}", account_id)


async def test_a_login_name_account_signs_in_without_any_email(storage, pool):
    accounts = storage.local_accounts
    password_hash = await hash_password(PASSWORD)
    assert await accounts.create_login_name_account(
        account_id=_ULID_A, login_name="operator", password_hash=password_hash, user_name="Ops"
    )
    assert not await accounts.create_login_name_account(
        account_id=_ULID_B, login_name="operator", password_hash=password_hash
    )
    login = await accounts.find_login("login_name", "operator")
    assert login is not None and login.account.identity_verified and login.account.email is None
    _token, _session_id, record = await _session(storage, _ULID_A, password_hash=password_hash)
    assert record is not None and record.label == "operator"
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT last_login_at IS NOT NULL FROM agent_local_accounts WHERE id = $1", _ULID_A
        )


async def test_a_phone_account_opens_sessions_only_with_its_own_number(storage):
    accounts = storage.local_accounts
    assert await accounts.create_phone_account(
        account_id=_ULID_A, phone="+8613800138000", user_name=None, tos_accepted=True, purpose=""
    )
    record = await accounts.get_account(_ULID_A)
    assert record is not None and record.identity_verified and not record.has_password
    assert record.admission_status == "pending"
    assert (await _session(storage, _ULID_A, phone="+8613800138001"))[2] is None
    assert (await _session(storage, _ULID_A, password_hash="$argon2id$guess"))[2] is None
    assert (await _session(storage, _ULID_A, phone="+8613800138000"))[2] is not None


# ── Binding identifiers ──────────────────────────────────────────────────


async def test_two_accounts_racing_for_one_number_bind_it_once(storage):
    first = await make_account(storage, "first@example.edu")
    second = await make_account(storage, "second@example.edu")
    results = await asyncio.gather(
        storage.local_accounts.bind_phone(account_id=first, phone="+8613800138000"),
        storage.local_accounts.bind_phone(account_id=second, phone="+8613800138000"),
        return_exceptions=True,
    )
    assert sorted(type(result).__name__ for result in results) == ["IdentifierTaken", "bool"]
    owner = await storage.local_accounts.identifier_owner("phone", "+8613800138000")
    assert owner in (first, second)
    # A bound number is never replaced.
    assert not await storage.local_accounts.bind_phone(account_id=owner, phone="+8613800138009")


async def _bind_token(storage: Storage, account_id: str, email: str) -> bytes:
    token_hash = tokens.email_token_hash(tokens.new_email_token())
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="bind_email",
        token_hash=token_hash,
        ttl_s=600,
        pending_email=email,
        pending_email_normalized=email.lower(),
    )
    return token_hash


async def test_a_binding_link_replaces_the_email_once_and_spends_old_reset_links(storage):
    account_id = await make_account(storage, "old@example.edu")
    reset_hash = tokens.email_token_hash(tokens.new_email_token())
    await storage.local_accounts.issue_email_token(
        account_id=account_id, purpose="reset_password", token_hash=reset_hash, ttl_s=600
    )
    token_hash = await _bind_token(storage, account_id, "New@Example.edu")
    before = await storage.local_accounts.get_account(account_id)
    binding = await storage.local_accounts.bind_email(token_hash=token_hash)
    assert binding is not None
    assert (binding.previous_email, binding.email) == ("old@example.edu", "New@Example.edu")
    after = await storage.local_accounts.get_account(account_id)
    assert after is not None and before is not None
    assert (after.email, after.email_verified, after.version) == (
        "New@Example.edu",
        True,
        before.version + 1,
    )
    assert await storage.local_accounts.bind_email(token_hash=token_hash) is None
    assert (
        await storage.local_accounts.reset_password(
            token_hash=reset_hash, password_hash="$argon2id$x"
        )
        is None
    )
    assert await storage.local_accounts.password_login("old@example.edu") is None


async def test_a_binding_link_for_a_taken_address_changes_nothing_and_stays_usable(storage):
    account_id = await make_account(storage, "mine@example.edu")
    token_hash = await _bind_token(storage, account_id, "wanted@example.edu")
    other = await make_account(storage, "wanted@example.edu")
    with pytest.raises(IdentifierTaken):
        await storage.local_accounts.bind_email(token_hash=token_hash)
    record = await storage.local_accounts.get_account(account_id)
    assert record is not None and record.email == "mine@example.edu"
    await act(storage, other, "delete")
    await _stopped(storage, other)
    await storage.local_accounts.erase_account(
        account_id=other, actor_id="admin-token", actor_account_id=None, admin_ip="test"
    )
    assert await storage.local_accounts.bind_email(token_hash=token_hash) is not None


# ── Changing the password ────────────────────────────────────────────────


async def test_a_password_change_keeps_this_session_and_ends_every_other(storage):
    account_id = await make_account(storage, "change@example.edu")
    current = await _hash(storage, account_id)
    kept_token, kept_id, _ = await _session(storage, account_id, password_hash=current)
    other_token, _other_id, _ = await _session(storage, account_id, password_hash=current)
    new_hash = await hash_password("Another1Horse")
    assert not await storage.local_accounts.change_password(
        account_id=account_id,
        expected_hash="$argon2id$stale",
        new_hash=new_hash,
        keep_session_id=kept_id,
    )
    assert await storage.local_accounts.change_password(
        account_id=account_id, expected_hash=current, new_hash=new_hash, keep_session_id=kept_id
    )
    assert await storage.local_accounts.session_owner(tokens.session_hash(kept_token)) is not None
    assert await storage.local_accounts.session_owner(tokens.session_hash(other_token)) is None
    assert await _hash(storage, account_id) == new_hash


async def test_a_phone_account_sets_its_first_password_once(storage):
    accounts = storage.local_accounts
    await accounts.create_phone_account(
        account_id=_ULID_A, phone="+8613800138000", user_name=None, tos_accepted=True, purpose=None
    )
    first = await hash_password(PASSWORD)
    assert await accounts.change_password(
        account_id=_ULID_A, expected_hash=None, new_hash=first, keep_session_id=None
    )
    assert not await accounts.change_password(
        account_id=_ULID_A, expected_hash=None, new_hash=first, keep_session_id=None
    )
    record = await accounts.get_account(_ULID_A)
    assert record is not None and record.has_password


async def test_a_losing_rehash_reports_that_it_lost(storage):
    account_id = await make_account(storage, "rehash@example.edu")
    current = await _hash(storage, account_id)
    first, second = "$argon2id$first", "$argon2id$second"
    outcomes = await asyncio.gather(
        storage.local_accounts.rehash_password(
            account_id=account_id, old_hash=current, new_hash=first
        ),
        storage.local_accounts.rehash_password(
            account_id=account_id, old_hash=current, new_hash=second
        ),
    )
    assert sorted(outcomes) == [False, True]
    assert await _hash(storage, account_id) == (first if outcomes[0] else second)


# ── Deletion, resumption and erasure ─────────────────────────────────────


async def test_deleting_ends_sessions_browsers_and_links_and_opens_the_termination(storage, pool):
    account_id = await make_account(storage, "leaving@example.edu", admission="approved")
    current = await _hash(storage, account_id)
    token, session_id, _ = await _session(storage, account_id, password_hash=current)
    browser_token = tokens.new_browser_token()
    await storage.local_accounts.create_browser(
        browser_id="browser-1", token_hash=tokens.browser_hash(browser_token), ttl_s=600
    )
    assert await storage.local_accounts.attach_browser_account(
        browser_id="browser-1",
        account_id=account_id,
        session_id=session_id,
        max_accounts=5,
        new_token_hash=tokens.browser_hash(browser_token),
        ttl_s=600,
    )
    record = await storage.local_accounts.get_account(account_id)
    assert record is not None
    with pytest.raises(AccountVersionConflict):
        await storage.local_accounts.delete_own_account(
            account_id=account_id, expected_version=record.version - 1, admin_ip="test"
        )
    deleted = await storage.local_accounts.delete_own_account(
        account_id=account_id, expected_version=record.version, admin_ip="test"
    )
    assert (deleted.status, deleted.admission_status, deleted.deleted_at is not None) == (
        "deleted",
        "revoked",
        True,
    )
    assert await storage.local_accounts.session_owner(tokens.session_hash(token)) is None
    browser = await storage.local_accounts.browser_state(tokens.browser_hash(browser_token))
    assert browser is not None and browser.accounts == [] and browser.active_account_id is None
    assert await storage.local_accounts.termination_open(account_id)
    async with pool.acquire() as conn:
        action = await conn.fetchval(
            "SELECT action FROM agent_local_access_terminations WHERE account_id = $1",
            account_id,
        )
        audit = await conn.fetchval(
            "SELECT count(*) FROM agent_admin_audit_log "
            "WHERE action = 'agent.local_account.self_delete' AND target_user_id = $1",
            account_id,
        )
    assert (action, audit) == ("delete", 1)
    assert (await _session(storage, account_id, password_hash=current))[2] is None


async def test_an_administrator_cannot_delete_their_own_account(storage):
    admin = await make_account(storage, "admin@example.edu", admission="approved", admin=True)
    record = await storage.local_accounts.get_account(admin)
    assert record is not None
    with pytest.raises(SelfDeleteRefused):
        await storage.local_accounts.delete_own_account(
            account_id=admin, expected_version=record.version, admin_ip="test"
        )


async def test_resume_waits_for_the_cleanup_and_restores_sign_in_only(storage, pool):
    account_id = await make_account(storage, "back@example.edu", admission="approved")
    await act(storage, account_id, "delete")
    with pytest.raises(AccountTransitionRefused) as waiting:
        await act(storage, account_id, "resume")
    assert waiting.value.code == "access_cleanup_pending"
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_access_terminations SET completed_at = NOW() WHERE account_id = $1",
            account_id,
        )
    resumed = (await act(storage, account_id, "resume")).account
    assert (resumed.status, resumed.admission_status, resumed.deleted_at) == (
        "active",
        "revoked",
        None,
    )
    assert permissions_for(resumed) == []


async def test_erasing_frees_the_identifiers_and_keeps_a_tombstone(storage, pool):
    account_id = await make_account(storage, "gone@example.edu")
    with pytest.raises(AccountTransitionRefused) as active:
        await storage.local_accounts.erase_account(
            account_id=account_id, actor_id="admin-token", actor_account_id=None, admin_ip="t"
        )
    assert active.value.code == "erase_blocked"
    await act(storage, account_id, "delete")
    with pytest.raises(AccountTransitionRefused):
        # Its termination is still open: nothing proves its work has stopped.
        await storage.local_accounts.erase_account(
            account_id=account_id, actor_id="admin-token", actor_account_id=None, admin_ip="t"
        )
    await _stopped(storage, account_id)
    erased = await storage.local_accounts.erase_account(
        account_id=account_id, actor_id="admin-token", actor_account_id=None, admin_ip="t"
    )
    assert (erased.email, erased.erased_at is not None, erased.has_password) == (None, True, False)
    assert await storage.local_accounts.identifier_owner("email", "gone@example.edu") is None
    with pytest.raises(AccountTransitionRefused):
        await act(storage, account_id, "resume")
    await make_account(storage, "gone@example.edu")


async def test_erasing_waits_while_a_turn_of_the_account_is_still_queued(storage, pool):
    account_id = await make_account(storage, "busy@example.edu", admission="approved")
    await storage.conversation_commands.create_job(
        user_id=account_id, repo=None, task_prompt="p", runtime="codex", model="m"
    )
    await act(storage, account_id, "delete")
    async with pool.acquire() as conn:
        # As if the pass had acknowledged everything but the queued turn.
        await conn.execute(
            "UPDATE agent_local_access_terminations SET completed_at = NOW() WHERE account_id = $1",
            account_id,
        )
    with pytest.raises(AccountTransitionRefused) as busy:
        await storage.local_accounts.erase_account(
            account_id=account_id, actor_id="admin-token", actor_account_id=None, admin_ip="t"
        )
    assert busy.value.code == "erase_blocked"


# ── Browsers ─────────────────────────────────────────────────────────────


async def _browser_with(storage: Storage, count: int) -> tuple[str, list[str]]:
    token = tokens.new_browser_token()
    await storage.local_accounts.create_browser(
        browser_id="browser-cap", token_hash=tokens.browser_hash(token), ttl_s=600
    )
    accounts = []
    for index in range(count):
        account_id = await make_account(storage, f"member{index}@example.edu")
        _t, session_id, _ = await _session(
            storage, account_id, password_hash=await _hash(storage, account_id)
        )
        new_token = tokens.new_browser_token()
        assert await storage.local_accounts.attach_browser_account(
            browser_id="browser-cap",
            account_id=account_id,
            session_id=session_id,
            max_accounts=5,
            new_token_hash=tokens.browser_hash(new_token),
            ttl_s=600,
        )
        token = new_token
        accounts.append(account_id)
    return token, accounts


async def test_a_browser_holds_five_accounts_and_rotates_its_cookie_on_each(storage):
    token, members = await _browser_with(storage, 5)
    state = await storage.local_accounts.browser_state(tokens.browser_hash(token))
    assert state is not None
    assert [m.account.id for m in state.accounts] == members
    assert state.active_account_id == members[-1]
    extra = await make_account(storage, "sixth@example.edu")
    _t, session_id, _ = await _session(storage, extra, password_hash=await _hash(storage, extra))
    with pytest.raises(BrowserFull):
        await storage.local_accounts.attach_browser_account(
            browser_id="browser-cap",
            account_id=extra,
            session_id=session_id,
            max_accounts=5,
            new_token_hash=b"x" * 32,
            ttl_s=600,
        )
    # Signing an account in again takes its own place.
    _t, again, _ = await _session(
        storage, members[0], password_hash=await _hash(storage, members[0])
    )
    assert await storage.local_accounts.attach_browser_account(
        browser_id="browser-cap",
        account_id=members[0],
        session_id=again,
        max_accounts=5,
        new_token_hash=tokens.browser_hash(token),
        ttl_s=600,
    )


async def test_switching_and_signing_out_keep_the_browser_consistent(storage):
    token, members = await _browser_with(storage, 2)
    accounts = storage.local_accounts
    browser = await accounts.browser_state(tokens.browser_hash(token))
    assert browser is not None
    assert await accounts.set_browser_active(browser_id=browser.id, account_id=members[0])
    assert not await accounts.set_browser_active(browser_id=browser.id, account_id=_ULID_A)
    assert await accounts.detach_browser_account(browser_id=browser.id, account_id=members[0]) == 1
    state = await accounts.browser_state(tokens.browser_hash(token))
    assert state is not None and state.active_account_id is None
    assert [m.account.id for m in state.accounts] == [members[1]]
    assert await accounts.detach_browser_account(browser_id=browser.id, account_id=members[1]) == 0
    assert await accounts.browser_state(tokens.browser_hash(token)) is None


async def test_signing_a_browser_out_of_everything_ends_every_session(storage, pool):
    token, members = await _browser_with(storage, 3)
    browser = await storage.local_accounts.browser_state(tokens.browser_hash(token))
    assert browser is not None
    await storage.local_accounts.revoke_browser(browser_id=browser.id)
    assert await storage.local_accounts.browser_state(tokens.browser_hash(token)) is None
    async with pool.acquire() as conn:
        live = await conn.fetchval(
            "SELECT count(*) FROM agent_local_sessions "
            "WHERE id = ANY($1::text[]) AND revoked_at IS NULL",
            [member.session_id for member in browser.accounts],
        )
    assert (len(browser.accounts), live) == (len(members), 0)


# ── Phone codes ──────────────────────────────────────────────────────────


async def _reserve(storage: Storage, *, purpose="login", account=None, code="123456", cool=60):
    phone = "+8613800138000"
    challenge = tokens.new_session_id()
    retry = await storage.local_accounts.reserve_phone_code(
        phone_hash=tokens.phone_hash(phone),
        purpose=purpose,
        account_id=account,
        challenge_id=challenge,
        code_hash=tokens.phone_code_hash(phone, purpose, account, code),
        expire_s=300,
        cooldown_s=cool,
    )
    return challenge, retry


async def _consume(storage: Storage, code: str, *, purpose="login", account=None, attempts=5):
    phone = "+8613800138000"
    return await storage.local_accounts.consume_phone_code(
        phone_hash=tokens.phone_hash(phone),
        purpose=purpose,
        account_id=account,
        code_hash=tokens.phone_code_hash(phone, purpose, account, code),
        max_attempts=attempts,
    )


async def test_a_code_works_only_once_activated_and_only_once(storage):
    challenge, retry = await _reserve(storage)
    assert retry == 0
    assert not await _consume(storage, "123456")
    assert await storage.local_accounts.activate_phone_code(challenge)
    assert await _consume(storage, "123456")
    assert not await _consume(storage, "123456")


async def test_wrong_checks_use_up_the_code(storage):
    challenge, _ = await _reserve(storage)
    await storage.local_accounts.activate_phone_code(challenge)
    assert not await _consume(storage, "000000", attempts=2)
    assert not await _consume(storage, "000001", attempts=2)
    assert not await _consume(storage, "123456", attempts=2)


async def test_concurrent_checks_of_one_code_admit_one(storage):
    challenge, _ = await _reserve(storage)
    await storage.local_accounts.activate_phone_code(challenge)
    results = await asyncio.gather(*(_consume(storage, "123456") for _ in range(5)))
    assert results.count(True) == 1


async def test_a_number_cools_down_across_purposes_and_a_deleted_owner_is_refused(storage):
    _challenge, first = await _reserve(storage, purpose="signup")
    _challenge, second = await _reserve(storage, purpose="login")
    assert first == 0 and second is not None and second > 0
    account_id = await make_account(storage, "coded@example.edu")
    await act(storage, account_id, "delete")
    _challenge, refused = await _reserve(storage, purpose="bind", account=account_id, cool=0)
    assert refused is None


async def test_a_new_code_replaces_the_open_one(storage):
    old, _ = await _reserve(storage, code="111111", cool=0)
    await storage.local_accounts.activate_phone_code(old)
    new, _ = await _reserve(storage, code="222222", cool=0)
    assert not await storage.local_accounts.activate_phone_code(old)
    await storage.local_accounts.activate_phone_code(new)
    assert not await _consume(storage, "111111")
    assert await _consume(storage, "222222")


# ── Sign-in events, domains and setup ────────────────────────────────────


async def test_sign_in_events_are_recorded_and_purged_by_account_or_age(storage, pool):
    account_id = await make_account(storage, "events@example.edu")
    for outcome in ("failure", "success"):
        await storage.local_accounts.record_login_event(
            account_id=account_id,
            method="password",
            identifier="events@example.edu",
            outcome=outcome,
            failure_reason="invalid_password" if outcome == "failure" else None,
            ip="203.0.113.1",
            user_agent="test",
        )
    await storage.local_accounts.record_login_event(
        account_id=None,
        method="password",
        identifier="raw:nobody",
        outcome="failure",
        failure_reason="user_not_found",
        ip="203.0.113.1",
        user_agent=None,
    )
    events = await storage.local_accounts.login_events(account_id=account_id, limit=10)
    assert [event["outcome"] for event in events] == ["success", "failure"]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_login_events SET created_at = NOW() - INTERVAL '40 days' "
            "WHERE account_id IS NULL"
        )
    assert await storage.local_accounts.purge_login_events(older_than_days=30) == 1
    assert await storage.local_accounts.purge_login_events(account_id=account_id) == 2


async def test_a_wildcard_matches_subdomains_only(storage):
    accounts = storage.local_accounts
    assert await accounts.signup_domain_policy("lab.example.edu") == (True, False)
    assert await accounts.add_signup_domain(
        domain="example.edu", is_wildcard=True, actor_id="admin-token", admin_ip="t"
    )
    assert not await accounts.add_signup_domain(
        domain="example.edu", is_wildcard=True, actor_id="admin-token", admin_ip="t"
    )
    assert await accounts.signup_domain_policy("lab.example.edu") == (False, True)
    assert await accounts.signup_domain_policy("example.edu") == (False, False)
    assert await accounts.signup_domain_policy("badexample.edu") == (False, False)
    await accounts.add_signup_domain(
        domain="harvard.edu", is_wildcard=False, actor_id="admin-token", admin_ip="t"
    )
    assert await accounts.signup_domain_policy("harvard.edu") == (False, True)
    assert await accounts.remove_signup_domain(
        domain="harvard.edu", is_wildcard=False, actor_id="admin-token", admin_ip="t"
    )
    assert [row.domain for row in await accounts.list_signup_domains()] == ["example.edu"]


async def test_setup_creates_exactly_one_administrator(storage, pool):
    accounts = storage.local_accounts
    code = await accounts.setup_code("AAAA-BBBB-CCCC")
    assert code == "AAAA-BBBB-CCCC"
    assert await accounts.setup_code("DDDD-EEEE-FFFF") == code
    password_hash = await hash_password(PASSWORD)

    async def attempt(account_id: str, name: str):
        return await accounts.complete_setup(
            account_id=account_id,
            login_name=name,
            password_hash=password_hash,
            user_name=None,
            admin_ip="t",
        )

    results = await asyncio.gather(attempt(_ULID_A, "first"), attempt(_ULID_B, "second"))
    created = [result for result in results if result is not None]
    assert len(created) == 1
    admin = created[0]
    assert permissions_for(admin) == ["agent.use", "agent.admin"]
    assert await accounts.setup_code("GGGG-HHHH-JJJJ") is None


async def test_a_deployment_with_an_administrator_is_already_set_up(storage):
    await make_account(storage, "existing@example.edu", admission="approved", admin=True)
    assert await storage.local_accounts.setup_code("AAAA-BBBB-CCCC") is None


async def test_a_signed_up_account_does_not_close_setup(storage):
    await make_account(storage, "stranger@example.edu")
    assert await storage.local_accounts.setup_code("AAAA-BBBB-CCCC") == "AAAA-BBBB-CCCC"


async def test_search_finds_identifiers_names_and_id_prefixes(storage):
    named = await make_account(storage, "Searchable@Example.edu")
    await storage.local_accounts.create_login_name_account(
        account_id=_ULID_A,
        login_name="ops-lead",
        password_hash=await hash_password(PASSWORD),
        user_name="Grace Hopper",
    )
    accounts = storage.local_accounts

    async def found(query: str) -> list[str]:
        records, total = await accounts.list_accounts(
            admission_status=None, limit=10, offset=0, query=query
        )
        assert total == len(records)
        return [record.id for record in records]

    assert await found("searchable@") == [named]
    assert await found("OPS-") == [_ULID_A]
    assert await found("hopper") == [_ULID_A]
    assert await found(_ULID_A[:10]) == [_ULID_A]
    assert await found("100%") == []
    counts = await accounts.account_counts()
    assert counts["all"] == 2 and counts["status:active"] == 2


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("login_name", "unverified-name"),
        ("phone", "+8613800138000"),
        ("login_name", "has@sign"),
        ("email", "Upper@Example.edu"),
    ],
)
async def test_the_database_refuses_an_identity_no_flow_may_write(storage, pool, kind, value):
    account_id = await make_account(storage, "holder@example.edu")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO agent_local_login_identities (account_id, kind, value, "
                "value_normalized) VALUES ($1, $2, $3, $3)",
                account_id,
                kind,
                value,
            )
