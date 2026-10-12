"""Source-control authorization, against a real database.

The unit tier drives these through fakes, so it pins what the *code* does with
rows it is handed. What only Postgres can settle is the pair of properties the
capability exists for:

- **Every read and write is scoped to its owner.** A missing ``WHERE user_id``
  reads exactly like a correct one until two people are connected, at which
  point one is publishing with the other's installation or reading the other's
  GitLab token. A fake keyed by user by construction cannot fail that way.
- **The uniqueness is the database's, not the caller's.** Repo grants are
  unique per ``(user_id, installation_id)`` and a GitLab connection is unique
  per ``user_id``; both are ``ON CONFLICT`` clauses, so reconnecting has to
  replace rather than accumulate.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from tests.support.migrations import apply_migrations

import cloud_agent.source_control as source_control
from cloud_agent.source_control import (
    GitLabOAuthClient,
    GitLabOAuthConfig,
    SourceControlCipher,
    SourceControlError,
)
from cloud_agent.storage.repositories.source_control import PostgresSourceControlRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-owner"
_OTHER = "user-other"


def _test_db_config() -> dict[str, Any]:
    """Resolve and safety-check the shared test database configuration."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest_asyncio.fixture
async def pool():
    """A pool over a clean shared test database."""
    import asyncpg

    db_config = _test_db_config()
    try:
        pool = await asyncpg.create_pool(**db_config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(db_config)

    async def _clear() -> None:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_repo_grants")
            await conn.execute("DELETE FROM agent_github_accounts")
            await conn.execute("DELETE FROM agent_oauth_states")
            await conn.execute("DELETE FROM agent_gitlab_connections")

    await _clear()
    try:
        yield pool
    finally:
        await _clear()
        await pool.close()


@pytest_asyncio.fixture
async def store(pool):
    """The capability under test, on the repository that owns it."""
    return PostgresSourceControlRepository(pool)


async def _wait_for_lock_wait(pool, sql_fragment: str) -> None:
    """Wait until a repository statement is genuinely blocked on a row/session lock."""
    async with pool.acquire() as observer:
        for _ in range(200):
            blocked = await observer.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                      AND query LIKE $1
                )
                """,
                f"%{sql_fragment}%",
            )
            if blocked:
                return
            await asyncio.sleep(0.01)
    pytest.fail(f"statement never waited on its lock: {sql_fragment}")


async def _connect_gitlab(
    store: PostgresSourceControlRepository, *, user_id: str, token: str = "at"
) -> int:
    """Store one GitLab identity the way the OAuth callback will."""
    return await store.upsert_gitlab_connection(
        user_id=user_id,
        external_user_id=42,
        username="octocat",
        display_name="Octo Cat",
        web_url="https://gitlab.com/octocat",
        access_token_ciphertext=token,
        refresh_token_ciphertext="rt",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _gitlab_client() -> GitLabOAuthClient:
    return GitLabOAuthClient(
        GitLabOAuthConfig("client-id", "client-secret", "https://example.com/callback"),
        cipher=SourceControlCipher("test-source-control-key"),
    )


async def _connect_refreshable_gitlab(
    store: PostgresSourceControlRepository,
    client: GitLabOAuthClient,
    *,
    external_user_id: int = 42,
    username: str = "octocat",
    access_token: str = "access-old",
    refresh_token: str = "refresh-old",
    expires_in: timedelta = -timedelta(minutes=1),
) -> int:
    return await store.upsert_gitlab_connection(
        user_id=_OWNER,
        external_user_id=external_user_id,
        username=username,
        display_name=username,
        web_url=f"https://gitlab.com/{username}",
        access_token_ciphertext=client.cipher.encrypt(access_token),
        refresh_token_ciphertext=client.cipher.encrypt(refresh_token),
        expires_at=datetime.now(UTC) + expires_in,
    )


# ── The GitHub account ────────────────────────────────────────────────


async def _connect_github(
    store: PostgresSourceControlRepository,
    *,
    user_id: str = _OWNER,
    login: str = "octocat",
    installations: tuple[tuple[int, str | None], ...] = (),
) -> None:
    """Record a GitHub connection the way the OAuth callback will."""
    await store.connect_github_account(
        user_id=user_id,
        github_user_id=583231,
        login=login,
        installations=[
            {"installation_id": installation_id, "account_login": account}
            for installation_id, account in installations
        ],
    )


async def test_a_github_account_is_recorded_with_no_installation(
    store: PostgresSourceControlRepository,
) -> None:
    """Connecting and having repository access are separate states."""
    await _connect_github(store)

    account = await store.get_github_account(user_id=_OWNER)
    assert account is not None
    assert (account["github_user_id"], account["login"]) == (583231, "octocat")
    assert await store.list_repo_grants(user_id=_OWNER) == []
    assert await store.get_github_account(user_id=_OTHER) is None


async def test_connecting_makes_the_grants_exactly_what_github_reported(
    store: PostgresSourceControlRepository,
) -> None:
    """A reconnect drops an installation the user lost, keeps and relabels the rest."""
    await _connect_github(store, installations=((7, "acme"), (8, "octocat")))
    await _connect_github(store, installations=((8, "octocat-renamed"), (9, "neworg")))

    assert await store.list_repo_grants(user_id=_OWNER) == [
        {"installation_id": 8, "account_login": "octocat-renamed"},
        {"installation_id": 9, "account_login": "neworg"},
    ]


async def test_connecting_leaves_another_users_grants_and_account_alone(
    store: PostgresSourceControlRepository,
) -> None:
    """The replacement is scoped by user: one person reconnecting revokes nobody else."""
    await _connect_github(store, user_id=_OTHER, login="other", installations=((7, "acme"),))
    await _connect_github(store, installations=((8, "octocat"),))

    assert await store.list_repo_grants(user_id=_OTHER) == [
        {"installation_id": 7, "account_login": "acme"}
    ]
    other = await store.get_github_account(user_id=_OTHER)
    assert other is not None and other["login"] == "other"


async def test_reconnecting_as_another_github_account_replaces_it(
    store: PostgresSourceControlRepository,
) -> None:
    """One account per user; the old account's installations go with it."""
    await _connect_github(store, login="octocat", installations=((7, "octocat"),))
    await _connect_github(store, login="hubot", installations=((9, "hubot"),))

    account = await store.get_github_account(user_id=_OWNER)
    assert account is not None and account["login"] == "hubot"
    assert await store.list_repo_grants(user_id=_OWNER) == [
        {"installation_id": 9, "account_login": "hubot"}
    ]


# ── The user's GitHub token ───────────────────────────────────────────


async def _connect_with_token(
    store: PostgresSourceControlRepository,
    *,
    user_id: str = _OWNER,
    access: str = "ct-access",
    expires_in: timedelta = timedelta(minutes=2),
) -> None:
    await store.connect_github_account(
        user_id=user_id,
        github_user_id=583231,
        login="octocat",
        installations=[{"installation_id": 7, "account_login": "acme"}],
        access_token_ciphertext=access,
        access_expires_at=datetime.now(UTC) + expires_in,
        refresh_token_ciphertext="ct-refresh",
        refresh_expires_at=datetime.now(UTC) + timedelta(days=180),
    )


async def test_one_caller_at_a_time_holds_the_refresh_lease(pool, store) -> None:
    """A single-use refresh token must be spent by exactly one caller."""
    await _connect_with_token(store)

    first = await store.claim_github_token_refresh(
        user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )
    second = await store.claim_github_token_refresh(
        user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )

    assert first is not None and first["refresh_token_ciphertext"] == "ct-refresh"
    assert second is None
    # A holder that died leaves a lease, not a lock.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_github_accounts SET refresh_lease_expires_at = NOW() - INTERVAL '1 second'"
        )
    third = await store.claim_github_token_refresh(
        user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )
    assert third is not None and third["refresh_generation"] > first["refresh_generation"]


async def test_a_token_that_does_not_need_it_is_not_claimed(store) -> None:
    await _connect_with_token(store, expires_in=timedelta(hours=8))

    assert (
        await store.claim_github_token_refresh(user_id=_OWNER, lease_seconds=30, margin_seconds=300)
        is None
    )


async def test_only_the_lease_holder_writes_the_renewed_pair(store) -> None:
    """A superseded refresher cannot overwrite what its successor wrote."""
    await _connect_with_token(store)
    claim = await store.claim_github_token_refresh(
        user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )
    assert claim is not None

    stale = await store.store_github_tokens(
        user_id=_OWNER,
        refresh_generation=claim["refresh_generation"] - 1,
        access_token_ciphertext="ct-stale",
        access_expires_at=datetime.now(UTC) + timedelta(hours=8),
        refresh_token_ciphertext="ct-stale-refresh",
        refresh_expires_at=None,
    )
    fresh = await store.store_github_tokens(
        user_id=_OWNER,
        refresh_generation=claim["refresh_generation"],
        access_token_ciphertext="ct-fresh",
        access_expires_at=datetime.now(UTC) + timedelta(hours=8),
        refresh_token_ciphertext="ct-fresh-refresh",
        refresh_expires_at=None,
    )

    account = await store.get_github_account(user_id=_OWNER)
    assert (stale, fresh) == (False, True)
    assert account is not None
    assert (account["access_token_ciphertext"], account["refresh_token_ciphertext"]) == (
        "ct-fresh",
        "ct-fresh-refresh",
    )
    assert account["refresh_lease_expires_at"] is None


async def test_github_refresh_store_rechecks_wall_clock_after_a_row_lock_wait(pool, store) -> None:
    """A transaction started before expiry cannot store after the lease expires."""
    await _connect_with_token(store)
    claim = await store.claim_github_token_refresh(
        user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )
    assert claim is not None
    pending = None
    try:
        async with pool.acquire() as blocker, blocker.transaction():
            await blocker.execute(
                """
                UPDATE agent_github_accounts
                SET refresh_lease_expires_at = clock_timestamp() + INTERVAL '600 milliseconds'
                WHERE user_id = $1
                """,
                _OWNER,
            )
            pending = asyncio.create_task(
                store.store_github_tokens(
                    user_id=_OWNER,
                    refresh_generation=claim["refresh_generation"],
                    access_token_ciphertext="ct-too-late",
                    access_expires_at=datetime.now(UTC) + timedelta(hours=8),
                    refresh_token_ciphertext="ct-refresh-too-late",
                    refresh_expires_at=None,
                )
            )
            await _wait_for_lock_wait(pool, "agent_github_accounts WHERE user_id")
            await asyncio.sleep(0.7)
        assert await pending is False
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    current = await store.get_github_account(user_id=_OWNER)
    assert current is not None
    assert current["access_token_ciphertext"] == "ct-access"


async def test_a_reconnect_takes_the_lease_from_a_refresh_in_flight(store) -> None:
    """The new pair from connecting again is not overwritten by the old one's renewal."""
    await _connect_with_token(store)
    claim = await store.claim_github_token_refresh(
        user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )
    assert claim is not None
    await _connect_with_token(store, access="ct-reconnected", expires_in=timedelta(hours=8))

    wrote = await store.store_github_tokens(
        user_id=_OWNER,
        refresh_generation=claim["refresh_generation"],
        access_token_ciphertext="ct-renewed-old",
        access_expires_at=datetime.now(UTC) + timedelta(hours=8),
        refresh_token_ciphertext=None,
        refresh_expires_at=None,
    )

    account = await store.get_github_account(user_id=_OWNER)
    assert wrote is False
    assert account is not None and account["access_token_ciphertext"] == "ct-reconnected"


async def test_forgetting_a_refused_token_spares_a_newer_one_and_other_users(store) -> None:
    await _connect_with_token(store, access="ct-refused")
    await _connect_with_token(store, user_id=_OTHER, access="ct-refused")

    await _connect_with_token(store, access="ct-newer")
    assert (
        await store.clear_github_tokens(user_id=_OWNER, access_token_ciphertext="ct-refused")
        is False
    )
    assert (
        await store.clear_github_tokens(user_id=_OTHER, access_token_ciphertext="ct-refused")
        is True
    )

    mine = await store.get_github_account(user_id=_OWNER)
    theirs = await store.get_github_account(user_id=_OTHER)
    assert mine is not None and mine["access_token_ciphertext"] == "ct-newer"
    assert theirs is not None
    assert (theirs["access_token_ciphertext"], theirs["refresh_token_ciphertext"]) == (None, None)
    # The account and its grants stay: connecting again is how the token returns.
    assert await store.list_repo_grants(user_id=_OTHER) == [
        {"installation_id": 7, "account_login": "acme"}
    ]


async def test_disconnecting_github_removes_only_this_users_connection(store) -> None:
    await _connect_with_token(store)
    await _connect_with_token(store, user_id=_OTHER)

    account = await store.disconnect_github_account(user_id=_OWNER)
    assert account is not None
    assert account["access_token_ciphertext"] == "ct-access"

    assert await store.get_github_account(user_id=_OWNER) is None
    assert await store.list_repo_grants(user_id=_OWNER) == []
    assert await store.get_github_account(user_id=_OTHER) is not None
    assert await store.list_repo_grants(user_id=_OTHER) == [
        {"installation_id": 7, "account_login": "acme"}
    ]


async def test_github_disconnect_returns_the_current_reconnected_account(store) -> None:
    """The locked delete returns the exact current token snapshot it removed."""
    await _connect_with_token(store, access="ct-old")
    await _connect_with_token(store, access="ct-new")
    deleted = await store.disconnect_github_account(user_id=_OWNER)

    assert deleted is not None
    assert deleted["access_token_ciphertext"] == "ct-new"
    assert await store.get_github_account(user_id=_OWNER) is None
    assert await store.list_repo_grants(user_id=_OWNER) == []


@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_account_serialization_blocks_across_pool_connections(pool, store, provider) -> None:
    """The authorization lock coordinates independent control-plane connections."""
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first() -> None:
        serialize = getattr(store, f"serialize_{provider}_account")
        async with serialize(user_id=_OWNER):
            first_entered.set()
            await release_first.wait()

    async def enter_second() -> None:
        serialize = getattr(store, f"serialize_{provider}_account")
        async with serialize(user_id=_OWNER):
            second_entered.set()

    first = asyncio.create_task(hold_first())
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        second = asyncio.create_task(enter_second())
        await _wait_for_lock_wait(pool, "SELECT pg_advisory_lock(")
        assert not second_entered.is_set()

        release_first.set()
        await asyncio.gather(first, second)
        assert second_entered.is_set()
    finally:
        release_first.set()
        pending = [task for task in (first, second) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_cancelling_an_account_lock_holder_releases_its_session_lock(store, provider) -> None:
    """Cancellation cannot return a lock-holding session to the shared pool."""
    entered = asyncio.Event()
    never = asyncio.Event()
    serialize = getattr(store, f"serialize_{provider}_account")

    async def hold() -> None:
        async with serialize(user_id=_OWNER):
            entered.set()
            await never.wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with asyncio.timeout(1), serialize(user_id=_OWNER):
        pass


async def test_serialized_account_operations_reuse_a_single_pool_connection(pool, store) -> None:
    """The lock holder must not wait forever for a second pooled connection."""
    import asyncpg

    single_pool = await asyncpg.create_pool(**_test_db_config(), min_size=1, max_size=1)
    single_store = PostgresSourceControlRepository(single_pool)

    async def round_trip() -> None:
        async with single_store.serialize_github_account(user_id=_OWNER) as account_store:
            await account_store.connect_github_account(
                user_id=_OWNER,
                github_user_id=583231,
                login="octocat",
                installations=[{"installation_id": 7, "account_login": "acme"}],
                access_token_ciphertext="ct-single",
            )
            account = await account_store.disconnect_github_account(user_id=_OWNER)
            assert account is not None
            assert account["access_token_ciphertext"] == "ct-single"

    try:
        await asyncio.wait_for(round_trip(), timeout=2)
    finally:
        await single_pool.close()

    assert await store.get_github_account(user_id=_OWNER) is None
    assert await store.list_repo_grants(user_id=_OWNER) == []


# ── Repository grants ─────────────────────────────────────────────────


async def test_a_repo_grant_is_only_read_by_its_owner(
    store: PostgresSourceControlRepository,
) -> None:
    """Two users, the same installation id: the query must not blur them."""
    await store.record_repo_grant(user_id=_OWNER, installation_id=7, account_login="mine")
    await store.record_repo_grant(user_id=_OTHER, installation_id=7, account_login="theirs")

    assert await store.list_repo_grants(user_id=_OWNER) == [
        {"installation_id": 7, "account_login": "mine"}
    ]
    assert await store.list_repo_grants(user_id=_OTHER) == [
        {"installation_id": 7, "account_login": "theirs"}
    ]
    assert await store.list_repo_grants(user_id="nobody") == []


async def test_recording_the_same_installation_replaces_rather_than_duplicates(
    store: PostgresSourceControlRepository,
) -> None:
    """The uniqueness is the constraint's, so a reconnect cannot accumulate."""
    await store.record_repo_grant(user_id=_OWNER, installation_id=7, account_login="old")
    await store.record_repo_grant(user_id=_OWNER, installation_id=7, account_login="new")

    assert await store.list_repo_grants(user_id=_OWNER) == [
        {"installation_id": 7, "account_login": "new"}
    ]


async def test_a_grant_may_name_no_account(store: PostgresSourceControlRepository) -> None:
    """An installation whose account could not be read is still a grant."""
    await store.record_repo_grant(user_id=_OWNER, installation_id=9)

    assert await store.list_repo_grants(user_id=_OWNER) == [
        {"installation_id": 9, "account_login": None}
    ]


async def test_revoking_reports_whether_it_removed_anything(
    store: PostgresSourceControlRepository,
) -> None:
    """Uninstalling twice is not an error, but it is not a removal either."""
    await store.record_repo_grant(user_id=_OWNER, installation_id=7)

    assert await store.revoke_repo_grant(user_id=_OWNER, installation_id=7) is True
    assert await store.revoke_repo_grant(user_id=_OWNER, installation_id=7) is False
    assert await store.list_repo_grants(user_id=_OWNER) == []


async def test_revoking_leaves_another_users_grant_alone(
    store: PostgresSourceControlRepository,
) -> None:
    """The delete is scoped by user, which is what stops cross-tenant revocation."""
    await store.record_repo_grant(user_id=_OWNER, installation_id=7)
    await store.record_repo_grant(user_id=_OTHER, installation_id=7)

    assert await store.revoke_repo_grant(user_id=_OWNER, installation_id=7) is True
    assert await store.list_repo_grants(user_id=_OTHER) == [
        {"installation_id": 7, "account_login": None}
    ]


# ── OAuth state ───────────────────────────────────────────────────────


async def test_oauth_state_is_consumed_exactly_once(store: PostgresSourceControlRepository) -> None:
    """A replayed callback must find nothing to consume."""
    await store.create_oauth_state(
        state_hash="hash-one",
        user_id=_OWNER,
        provider="github",
        code_verifier_ciphertext="sealed",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    consumed = await store.consume_oauth_state(
        state_hash="hash-one", user_id=_OWNER, provider="github"
    )
    assert consumed == {"code_verifier_ciphertext": "sealed"}
    assert (
        await store.consume_oauth_state(state_hash="hash-one", user_id=_OWNER, provider="github")
        is None
    )


async def test_oauth_state_is_scoped_to_its_user_and_provider(
    store: PostgresSourceControlRepository,
) -> None:
    """Someone else's state, or the other provider's, is not this callback's."""
    await store.create_oauth_state(
        state_hash="hash-two",
        user_id=_OWNER,
        provider="github",
        code_verifier_ciphertext=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    assert (
        await store.consume_oauth_state(state_hash="hash-two", user_id=_OTHER, provider="github")
        is None
    )
    assert (
        await store.consume_oauth_state(state_hash="hash-two", user_id=_OWNER, provider="gitlab")
        is None
    )
    assert (
        await store.consume_oauth_state(state_hash="hash-two", user_id=_OWNER, provider="github")
        is not None
    )


async def test_an_expired_oauth_state_cannot_be_consumed(
    store: PostgresSourceControlRepository,
) -> None:
    """The expiry is the query's, not the caller's."""
    await store.create_oauth_state(
        state_hash="hash-old",
        user_id=_OWNER,
        provider="github",
        code_verifier_ciphertext=None,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert (
        await store.consume_oauth_state(state_hash="hash-old", user_id=_OWNER, provider="github")
        is None
    )


async def test_oauth_state_rechecks_wall_clock_after_waiting_for_its_row_lock(pool, store) -> None:
    """A callback transaction started before expiry cannot consume after expiry."""
    await store.create_oauth_state(
        state_hash="hash-expiring",
        user_id=_OWNER,
        provider="github",
        code_verifier_ciphertext="sealed",
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    pending = None
    try:
        async with pool.acquire() as blocker, blocker.transaction():
            await blocker.execute(
                """
                UPDATE agent_oauth_states
                SET expires_at = clock_timestamp() + INTERVAL '600 milliseconds'
                WHERE state_hash = 'hash-expiring'
                """
            )
            pending = asyncio.create_task(
                store.consume_oauth_state(
                    state_hash="hash-expiring", user_id=_OWNER, provider="github"
                )
            )
            await _wait_for_lock_wait(pool, "agent_oauth_states")
            await asyncio.sleep(0.7)
        assert await pending is None
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_creating_a_state_prunes_the_ones_that_expired_yesterday(
    pool,
    store: PostgresSourceControlRepository,
) -> None:
    """The table is swept on write, so it cannot grow without bound."""
    await store.create_oauth_state(
        state_hash="hash-ancient",
        user_id=_OWNER,
        provider="github",
        code_verifier_ciphertext=None,
        expires_at=datetime.now(UTC) - timedelta(days=2),
    )
    await store.create_oauth_state(
        state_hash="hash-fresh",
        user_id=_OWNER,
        provider="github",
        code_verifier_ciphertext=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    async with pool.acquire() as conn:
        remaining = await conn.fetch("SELECT state_hash FROM agent_oauth_states")
    assert [row["state_hash"] for row in remaining] == ["hash-fresh"]


async def test_a_provider_is_a_non_empty_name_rather_than_a_whitelist(
    store: PostgresSourceControlRepository,
) -> None:
    """What 0006 gave the column, and why the MCP relay's provider fits.

    The constraint is ``provider <> ''``, not a list. That is the property
    test_mcp_connection_store relies on when it connects ``github-mcp``: a
    provider the checkout code has never heard of is storable, and the empty
    string -- which would make every scoped read match by accident -- is not.
    """
    import asyncpg

    await store.create_oauth_state(
        state_hash="hash-other",
        user_id=_OWNER,
        provider="bitbucket",
        code_verifier_ciphertext=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    assert (
        await store.consume_oauth_state(
            state_hash="hash-other", user_id=_OWNER, provider="bitbucket"
        )
        is not None
    )

    with pytest.raises(asyncpg.CheckViolationError):
        await store.create_oauth_state(
            state_hash="hash-empty",
            user_id=_OWNER,
            provider="",
            code_verifier_ciphertext=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )


# ── GitLab connections ────────────────────────────────────────────────


async def test_concurrent_gitlab_refreshes_spend_one_rotating_token(store, monkeypatch):
    client = _gitlab_client()
    await _connect_refreshable_gitlab(store, client)
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None
    started = asyncio.Event()
    release = asyncio.Event()
    refreshed_with: list[str] = []

    async def refresh(refresh_token: str, *, timeout_s: float) -> dict:
        assert timeout_s == client.request_timeout_s
        refreshed_with.append(refresh_token)
        started.set()
        await release.wait()
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "created_at": datetime.now(UTC).timestamp(),
            "expires_in": 3600,
        }

    monkeypatch.setattr(client, "_refresh", refresh)
    monkeypatch.setattr(source_control, "_GITLAB_REFRESH_WAIT_INTERVAL_S", 0.01)
    first = asyncio.create_task(client.access_token_for_connection(store, connection))
    await started.wait()
    claimed_connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert claimed_connection is not None
    second = asyncio.create_task(client.access_token_for_connection(store, claimed_connection))
    await asyncio.sleep(0.02)
    assert not second.done(), "the loser waits for the refresh lease holder"
    release.set()

    assert await asyncio.gather(first, second) == ["access-new", "access-new"]
    assert refreshed_with == ["refresh-old"]
    current = await store.get_gitlab_connection(user_id=_OWNER)
    assert current is not None
    assert client.cipher.decrypt(current["access_token_ciphertext"]) == "access-new"
    assert current["refresh_lease_expires_at"] is None


async def test_gitlab_refresh_store_rechecks_wall_clock_after_a_row_lock_wait(pool, store) -> None:
    """A transaction started before expiry cannot rotate after the lease expires."""
    client = _gitlab_client()
    connection_id = await _connect_refreshable_gitlab(store, client)
    claim = await store.claim_gitlab_token_refresh(
        connection_id=connection_id,
        user_id=_OWNER,
        lease_seconds=30,
        margin_seconds=300,
    )
    assert claim is not None
    pending = None
    try:
        async with pool.acquire() as blocker, blocker.transaction():
            await blocker.execute(
                """
                UPDATE agent_gitlab_connections
                SET refresh_lease_expires_at = clock_timestamp() + INTERVAL '600 milliseconds'
                WHERE id = $1
                """,
                connection_id,
            )
            pending = asyncio.create_task(
                store.update_gitlab_tokens(
                    connection_id=connection_id,
                    user_id=_OWNER,
                    refresh_generation=claim["refresh_generation"],
                    access_token_ciphertext=client.cipher.encrypt("access-too-late"),
                    refresh_token_ciphertext=client.cipher.encrypt("refresh-too-late"),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await _wait_for_lock_wait(pool, "agent_gitlab_connections WHERE id")
            await asyncio.sleep(0.7)
        assert await pending is False
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    current = await store.get_gitlab_connection(user_id=_OWNER)
    assert current is not None
    assert client.cipher.decrypt(current["access_token_ciphertext"]) == "access-old"


async def test_transient_gitlab_refresh_failure_releases_lock_but_keeps_committed_lease(
    store, monkeypatch
) -> None:
    client = _gitlab_client()
    await _connect_refreshable_gitlab(store, client)
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None

    async def fail(_refresh_token: str, *, timeout_s: float) -> dict:
        assert timeout_s == client.request_timeout_s
        raise SourceControlError("temporary provider failure")

    monkeypatch.setattr(client, "_refresh", fail)
    with pytest.raises(SourceControlError, match="temporary provider failure"):
        await client.access_token_for_connection(store, connection)

    current = await store.get_gitlab_connection(user_id=_OWNER)
    assert current is not None
    assert current["refresh_lease_expires_at"] is not None
    async with asyncio.timeout(1), store.serialize_gitlab_account(user_id=_OWNER):
        pass


async def test_gitlab_refresh_first_then_disconnect_removes_the_refreshed_token(store, monkeypatch):
    client = _gitlab_client()
    connection_id = await _connect_refreshable_gitlab(store, client)
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh(_refresh_token: str, *, timeout_s: float) -> dict:
        assert timeout_s == client.request_timeout_s
        started.set()
        await release.wait()
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "created_at": datetime.now(UTC).timestamp(),
            "expires_in": 3600,
        }

    monkeypatch.setattr(client, "_refresh", refresh)
    pending_refresh = asyncio.create_task(client.access_token_for_connection(store, connection))
    await started.wait()

    async def disconnect() -> dict[str, Any] | None:
        async with store.serialize_gitlab_account(user_id=_OWNER) as account_store:
            return await account_store.delete_gitlab_connection(
                user_id=_OWNER, connection_id=connection_id
            )

    pending_disconnect = asyncio.create_task(disconnect())
    await asyncio.sleep(0.02)
    assert not pending_disconnect.done(), "disconnect bypassed the in-flight refresh"
    release.set()

    assert await pending_refresh == "access-new"
    deleted = await pending_disconnect
    assert deleted is not None
    assert client.cipher.decrypt(deleted["access_token_ciphertext"]) == "access-new"
    assert await store.get_gitlab_connection(user_id=_OWNER) is None


async def test_gitlab_disconnect_first_blocks_then_stale_refresh_observes_no_connection(
    store, monkeypatch
):
    client = _gitlab_client()
    connection_id = await _connect_refreshable_gitlab(store, client)
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()
    monkeypatch.setattr(source_control, "_GITLAB_REFRESH_WAIT_INTERVAL_S", 0)

    async def disconnect() -> dict[str, Any] | None:
        async with store.serialize_gitlab_account(user_id=_OWNER) as account_store:
            deleted = await account_store.delete_gitlab_connection(
                user_id=_OWNER, connection_id=connection_id
            )
            disconnect_started.set()
            await release_disconnect.wait()
            return deleted

    pending_disconnect = asyncio.create_task(disconnect())
    await disconnect_started.wait()
    pending_refresh = asyncio.create_task(client.access_token_for_connection(store, connection))
    await asyncio.sleep(0.02)
    assert not pending_refresh.done(), "refresh bypassed the in-flight disconnect"
    release_disconnect.set()

    assert await pending_disconnect is not None
    with pytest.raises(SourceControlError, match="connection changed"):
        await pending_refresh
    assert await store.get_gitlab_connection(user_id=_OWNER) is None


async def test_reconnecting_gitlab_replaces_the_stored_identity(
    store: PostgresSourceControlRepository,
) -> None:
    """One identity per platform user, enforced by the unique index."""
    first = await _connect_gitlab(store, user_id=_OWNER, token="at-one")
    second = await _connect_gitlab(store, user_id=_OWNER, token="at-two")

    assert first == second
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None
    assert connection["access_token_ciphertext"] == "at-two"


async def test_a_gitlab_connection_is_only_read_by_its_owner(
    store: PostgresSourceControlRepository,
) -> None:
    """No connection, and no token, crosses to another user."""
    await _connect_gitlab(store, user_id=_OWNER)

    assert await store.get_gitlab_connection(user_id=_OTHER) is None


async def test_rotating_tokens_cannot_cross_ownership(
    store: PostgresSourceControlRepository,
) -> None:
    """The update is keyed by id *and* owner, so a guessed id is not enough."""
    connection_id = await _connect_gitlab(store, user_id=_OWNER)
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None

    assert (
        await store.update_gitlab_tokens(
            connection_id=connection_id,
            user_id=_OTHER,
            refresh_generation=connection["refresh_generation"],
            access_token_ciphertext="stolen",
            refresh_token_ciphertext="stolen",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        is False
    )
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None
    assert connection["access_token_ciphertext"] == "at"


async def test_a_stored_gitlab_expiry_round_trips_as_an_aware_timestamp(
    store: PostgresSourceControlRepository,
) -> None:
    """The column is TIMESTAMPTZ, which is what the refresh comparison assumes."""
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    await store.upsert_gitlab_connection(
        user_id=_OWNER,
        external_user_id=42,
        username="octocat",
        display_name=None,
        web_url=None,
        access_token_ciphertext="at",
        refresh_token_ciphertext="rt",
        expires_at=expires_at,
    )

    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None
    assert connection["expires_at"].tzinfo is not None
    assert abs((connection["expires_at"] - expires_at).total_seconds()) < 1


async def test_gitlab_disconnect_returns_the_current_reconnected_identity(
    store: PostgresSourceControlRepository,
) -> None:
    """The locked delete returns exactly the current token snapshot it removed."""
    connection_id = await _connect_gitlab(store, user_id=_OWNER, token="at-a")
    reconnected_id = await store.upsert_gitlab_connection(
        user_id=_OWNER,
        external_user_id=84,
        username="new-account",
        display_name="New Account",
        web_url="https://gitlab.com/new-account",
        access_token_ciphertext="at-b",
        refresh_token_ciphertext="rt-b",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert reconnected_id == connection_id

    deleted = await store.delete_gitlab_connection(user_id=_OWNER, connection_id=connection_id)
    assert deleted is not None
    assert deleted["external_user_id"] == 84
    assert deleted["username"] == "new-account"
    assert deleted["access_token_ciphertext"] == "at-b"
    assert await store.get_gitlab_connection(user_id=_OWNER) is None


async def test_deleting_a_gitlab_connection_reports_and_is_scoped(
    store: PostgresSourceControlRepository,
) -> None:
    """Disconnecting twice is not an error, and never reaches another user."""
    connection_id = await _connect_gitlab(store, user_id=_OWNER)
    assert await store.delete_gitlab_connection(user_id=_OTHER, connection_id=connection_id) is None
    deleted = await store.delete_gitlab_connection(user_id=_OWNER, connection_id=connection_id)
    assert deleted is not None and deleted["access_token_ciphertext"] == "at"
    assert await store.delete_gitlab_connection(user_id=_OWNER, connection_id=connection_id) is None


async def _connect_due_gitlab(
    store: PostgresSourceControlRepository,
    *,
    user_id: str = _OWNER,
    external_user_id: int = 11,
    username: str = "alice",
    display_name: str = "Alice",
    web_url: str = "https://gitlab.com/alice",
    access_token_ciphertext: str = "cipher-a-access",
    refresh_token_ciphertext: str = "cipher-a-refresh",
    expires_in: timedelta = timedelta(minutes=2),
) -> int:
    """Store one GitLab identity whose access token is inside the refresh margin."""
    return await store.upsert_gitlab_connection(
        user_id=user_id,
        external_user_id=external_user_id,
        username=username,
        display_name=display_name,
        web_url=web_url,
        access_token_ciphertext=access_token_ciphertext,
        refresh_token_ciphertext=refresh_token_ciphertext,
        expires_at=datetime.now(UTC) + expires_in,
    )


async def test_a_gitlab_token_that_does_not_need_refresh_is_not_claimed(
    store: PostgresSourceControlRepository,
) -> None:
    connection_id = await _connect_gitlab(store, user_id=_OWNER)

    assert (
        await store.claim_gitlab_token_refresh(
            connection_id=connection_id, user_id=_OWNER, lease_seconds=30, margin_seconds=300
        )
        is None
    )


async def test_one_gitlab_refresh_claim_wins_and_the_loser_cannot_write(
    store: PostgresSourceControlRepository,
) -> None:
    """Two claims of one rotating token: only the holder persists."""
    connection_id = await _connect_due_gitlab(store)
    first, second = await asyncio.gather(
        store.claim_gitlab_token_refresh(
            connection_id=connection_id, user_id=_OWNER, lease_seconds=30, margin_seconds=300
        ),
        store.claim_gitlab_token_refresh(
            connection_id=connection_id, user_id=_OWNER, lease_seconds=30, margin_seconds=300
        ),
    )

    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    winner = claims[0]
    assert winner["refresh_token_ciphertext"] == "cipher-a-refresh"
    held = await store.get_gitlab_connection(user_id=_OWNER)
    assert held is not None and held["refresh_lease_expires_at"] is not None

    stale = await store.update_gitlab_tokens(
        connection_id=connection_id,
        user_id=_OWNER,
        refresh_generation=winner["refresh_generation"] - 1,
        access_token_ciphertext="cipher-loser-access",
        refresh_token_ciphertext="cipher-loser-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    assert stale is False
    unchanged = await store.get_gitlab_connection(user_id=_OWNER)
    assert unchanged is not None
    assert unchanged["access_token_ciphertext"] == "cipher-a-access"
    assert unchanged["refresh_token_ciphertext"] == "cipher-a-refresh"
    assert unchanged["refresh_lease_expires_at"] is not None

    fresh = await store.update_gitlab_tokens(
        connection_id=connection_id,
        user_id=_OWNER,
        refresh_generation=winner["refresh_generation"],
        access_token_ciphertext="cipher-winner-access",
        refresh_token_ciphertext="cipher-winner-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    assert fresh is True
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert connection is not None
    assert (connection["access_token_ciphertext"], connection["refresh_token_ciphertext"]) == (
        "cipher-winner-access",
        "cipher-winner-refresh",
    )
    assert connection["refresh_lease_expires_at"] is None


async def test_reconnecting_gitlab_during_a_refresh_keeps_the_new_account(
    store: PostgresSourceControlRepository,
) -> None:
    """Account A's in-flight refresh cannot pair B's identity with A's tokens."""
    connection_id = await _connect_due_gitlab(store)
    claim = await store.claim_gitlab_token_refresh(
        connection_id=connection_id, user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )
    assert claim is not None and claim["refresh_token_ciphertext"] == "cipher-a-refresh"

    reconnected = await store.upsert_gitlab_connection(
        user_id=_OWNER,
        external_user_id=22,
        username="bob",
        display_name="Bob",
        web_url="https://gitlab.com/bob",
        access_token_ciphertext="cipher-b-access",
        refresh_token_ciphertext="cipher-b-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    # Same row: the generation, not a new id, is what makes A's write miss.
    assert reconnected == connection_id

    wrote = await store.update_gitlab_tokens(
        connection_id=connection_id,
        user_id=_OWNER,
        refresh_generation=claim["refresh_generation"],
        access_token_ciphertext="cipher-a-refreshed-access",
        refresh_token_ciphertext="cipher-a-refreshed-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    connection = await store.get_gitlab_connection(user_id=_OWNER)
    assert wrote is False
    assert connection is not None
    assert connection["external_user_id"] == 22
    assert connection["username"] == "bob"
    assert connection["display_name"] == "Bob"
    assert connection["web_url"] == "https://gitlab.com/bob"
    assert connection["access_token_ciphertext"] == "cipher-b-access"
    assert connection["refresh_token_ciphertext"] == "cipher-b-refresh"
    assert connection["access_token_ciphertext"] != "cipher-a-refreshed-access"
    assert connection["refresh_token_ciphertext"] != "cipher-a-refreshed-refresh"


async def test_deleting_gitlab_during_a_refresh_makes_the_write_miss(
    store: PostgresSourceControlRepository,
) -> None:
    """The row is gone, so the refresh that already claimed it cannot land."""
    connection_id = await _connect_due_gitlab(store)
    claim = await store.claim_gitlab_token_refresh(
        connection_id=connection_id, user_id=_OWNER, lease_seconds=30, margin_seconds=300
    )
    assert claim is not None
    assert (
        await store.delete_gitlab_connection(user_id=_OWNER, connection_id=connection_id)
        is not None
    )

    wrote = await store.update_gitlab_tokens(
        connection_id=connection_id,
        user_id=_OWNER,
        refresh_generation=claim["refresh_generation"],
        access_token_ciphertext="cipher-a-refreshed-access",
        refresh_token_ciphertext="cipher-a-refreshed-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    assert wrote is False
    assert await store.get_gitlab_connection(user_id=_OWNER) is None
