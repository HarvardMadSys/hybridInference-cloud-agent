"""Per-user MCP connections, against a real database (#49).

The unit tests drive resolution through a fake store, so they pin what the
*code* does with rows it is handed. What they cannot pin is the two things that
only exist in Postgres, and both of them are the feature's safety argument:

- **Every query is scoped by user.** A missing ``WHERE user_id`` reads exactly
  like a correct one until two people have connected the same server, at which
  point one of them is spending the other's credential. The fake store cannot
  fail that way because it is keyed by user by construction.
- **The 0006 migration produces the schema the store is written against.**
  Including the widened ``agent_oauth_states`` provider check: the baseline
  declared it inline and unnamed, so a drop-by-guessed-name would leave the old
  enum in place and every future MCP connection attempt would be rejected long
  after the migration ran green.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cloud_agent.storage.store import AgentJobStore
from tests.support.migrations import apply_migrations

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-owner"
_OTHER = "user-other"


@pytest_asyncio.fixture
async def store():
    """Provide a clean AgentJobStore over the shared test database."""
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

    job_store = AgentJobStore(pool)
    apply_migrations(db_config)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_mcp_connections")
        await conn.execute("DELETE FROM agent_oauth_states")
    try:
        yield job_store
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_mcp_connections")
            await conn.execute("DELETE FROM agent_oauth_states")
        await pool.close()


async def _connect(store: AgentJobStore, user_id: str, server: str, token: str) -> int:
    """Store one connection the way the OAuth flow will."""
    return await store.upsert_mcp_connection(
        user_id=user_id,
        server=server,
        account_label="octocat",
        access_token_ciphertext=token,
        refresh_token_ciphertext=None,
        expires_at=None,
    )


async def test_a_connection_is_only_ever_read_by_its_owner(store: AgentJobStore) -> None:
    """Two users, the same server: the query must not blur them together."""
    await _connect(store, _OWNER, "github", "ciphertext-owner")
    await _connect(store, _OTHER, "github", "ciphertext-other")

    mine = await store.list_mcp_connections(user_id=_OWNER)
    assert [(row["server"], row["access_token_ciphertext"]) for row in mine] == [
        ("github", "ciphertext-owner")
    ]

    theirs = await store.list_mcp_connections(user_id=_OTHER)
    assert [row["access_token_ciphertext"] for row in theirs] == ["ciphertext-other"]

    assert await store.list_mcp_connections(user_id="nobody") == []


async def test_reconnecting_replaces_the_credential_rather_than_adding_one(
    store: AgentJobStore,
) -> None:
    """One row per user per server, or which credential is used is row order."""
    first = await _connect(store, _OWNER, "github", "ciphertext-old")
    second = await _connect(store, _OWNER, "github", "ciphertext-new")

    assert first == second
    rows = await store.list_mcp_connections(user_id=_OWNER)
    assert [row["access_token_ciphertext"] for row in rows] == ["ciphertext-new"]


async def test_reconnecting_does_not_disturb_another_users_row(store: AgentJobStore) -> None:
    """The uniqueness constraint is on the pair, not on the server alone."""
    await _connect(store, _OTHER, "github", "ciphertext-other")
    await _connect(store, _OWNER, "github", "ciphertext-owner")
    await _connect(store, _OWNER, "github", "ciphertext-owner-again")

    theirs = await store.list_mcp_connections(user_id=_OTHER)
    assert [row["access_token_ciphertext"] for row in theirs] == ["ciphertext-other"]


async def test_disconnecting_removes_only_the_callers_row(store: AgentJobStore) -> None:
    """A delete without its user scope would disconnect everyone at once."""
    await _connect(store, _OWNER, "github", "ciphertext-owner")
    await _connect(store, _OTHER, "github", "ciphertext-other")

    assert await store.delete_mcp_connection(user_id=_OWNER, server="github") is True

    assert await store.list_mcp_connections(user_id=_OWNER) == []
    assert len(await store.list_mcp_connections(user_id=_OTHER)) == 1


async def test_disconnecting_something_not_connected_reports_it(store: AgentJobStore) -> None:
    """Distinguishable from a successful delete, so the API can 404."""
    assert await store.delete_mcp_connection(user_id=_OWNER, server="github") is False


async def test_a_connection_may_carry_no_refresh_token_or_expiry(store: AgentJobStore) -> None:
    """Providers vary, and a NULL here means "nothing to refresh".

    GitLab's columns are NOT NULL; these deliberately are not. A schema that
    demanded an expiry would force one to be invented for a provider that
    issues long-lived tokens.
    """
    await _connect(store, _OWNER, "github", "ciphertext-owner")
    row = (await store.list_mcp_connections(user_id=_OWNER))[0]
    assert row["refresh_token_ciphertext"] is None
    assert row["expires_at"] is None


async def test_a_stored_expiry_round_trips_as_an_aware_timestamp(store: AgentJobStore) -> None:
    """Resolution compares this against ``now``; a naive value there raises.

    The column is ``TIMESTAMPTZ`` and asyncpg gives back an aware datetime, so
    the comparison in ``_has_expired`` is safe — but that is a property of the
    column type, which is exactly the kind of thing a unit test with a
    hand-built dict cannot check.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await store.upsert_mcp_connection(
        user_id=_OWNER,
        server="github",
        account_label="octocat",
        access_token_ciphertext="ciphertext",
        refresh_token_ciphertext="refresh-ciphertext",
        expires_at=expires_at,
    )

    row = (await store.list_mcp_connections(user_id=_OWNER))[0]
    assert row["expires_at"].tzinfo is not None
    assert abs((row["expires_at"] - expires_at).total_seconds()) < 1
    assert row["refresh_token_ciphertext"] == "refresh-ciphertext"


async def test_oauth_state_accepts_an_mcp_provider_name(store: AgentJobStore) -> None:
    """The widened check, tested against the constraint rather than its name.

    The baseline's ``CHECK (provider IN ('github','gitlab'))`` was unnamed, so
    0006 drops it by definition. If that lookup missed, this insert raises and
    the connect flow would have been dead on arrival — with every other test in
    the suite still green.
    """
    await store.create_oauth_state(
        state_hash="mcp-state-hash",
        user_id=_OWNER,
        provider="github-mcp",
        code_verifier_ciphertext=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    consumed = await store.consume_oauth_state(
        state_hash="mcp-state-hash", user_id=_OWNER, provider="github-mcp"
    )
    assert consumed is not None
