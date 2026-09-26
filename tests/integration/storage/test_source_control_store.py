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

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from tests.support.migrations import apply_migrations

from cloud_agent.storage.repositories.source_control import PostgresSourceControlRepository

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-owner"
_OTHER = "user-other"


@pytest_asyncio.fixture
async def pool():
    """A pool over a clean shared test database."""
    import asyncpg

    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in (test_db_name or ""):
        pytest.fail(f"SAFETY: TEST_DB_NAME='{test_db_name}' is not a test database.")

    db_config = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }
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

    await store.disconnect_github_account(user_id=_OWNER)

    assert await store.get_github_account(user_id=_OWNER) is None
    assert await store.list_repo_grants(user_id=_OWNER) == []
    assert await store.get_github_account(user_id=_OTHER) is not None
    assert await store.list_repo_grants(user_id=_OTHER) == [
        {"installation_id": 7, "account_login": "acme"}
    ]


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

    assert (
        await store.update_gitlab_tokens(
            connection_id=connection_id,
            user_id=_OTHER,
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


async def test_deleting_a_gitlab_connection_reports_and_is_scoped(
    store: PostgresSourceControlRepository,
) -> None:
    """Disconnecting twice is not an error, and never reaches another user."""
    connection_id = await _connect_gitlab(store, user_id=_OWNER)

    assert (
        await store.delete_gitlab_connection(user_id=_OTHER, connection_id=connection_id) is False
    )
    assert await store.delete_gitlab_connection(user_id=_OWNER, connection_id=connection_id) is True
    assert (
        await store.delete_gitlab_connection(user_id=_OWNER, connection_id=connection_id) is False
    )
