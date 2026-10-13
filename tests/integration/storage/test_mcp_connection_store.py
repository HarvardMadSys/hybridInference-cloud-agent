"""The generic MCP connection store, against a real database.

The route tests drive an in-memory fake, so they pin what the *code* does with
rows it is handed. What they cannot pin is everything that only exists in
Postgres, and each item below is a race or a scope that a fake cannot fail:

- **Every query is scoped by owner.** A missing ``WHERE user_id`` reads exactly
  like a correct one until two people have connected the same service, at which
  point one of them is reading or editing the other's connection. The fake
  cannot fail that way because it is keyed by id and checked in Python.
- **A disconnect clears the secret and moves the versions** in one statement.
  A tombstone that still holds a token, or one whose epoch did not move, leaves
  every approval made against the old credential valid.
- **A credential write is conditional on the authorization epoch.** A refresh
  that started before a disconnect must match no row, or it restores what the
  owner removed.
- **A refresh claim is a lease, not a version bump.** The version is unchanged
  by the claim, so a second worker that reads the row afterwards claims the same
  version and is refused by the lease alone; a write that lost its lease matches
  no row. Both are single ``UPDATE ... WHERE`` statements.
- **A flow is consumed exactly once, and an approved call is claimed exactly
  once.** Both are single ``UPDATE ... WHERE`` statements, which is the whole
  mutual-exclusion mechanism; a check-then-write passes every sequential test
  and loses these races.
- **A decision after the deadline does not revive a call.**
- **``0007`` maps the legacy provider-name list onto bindings**, and quarantines
  what it cannot map, instead of guessing which connection a name meant.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.mcp import refresh
from cloud_agent.mcp.errors import McpConnectionCreateConflict
from cloud_agent.mcp.registry import mcp_cipher, oauth_cipher
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-owner"
_OTHER = "user-other"
_URL = "https://mcp.example.com/mcp"
_PREVIOUS_REVISION = "0007_attempt_turn_resources"


def _db_config() -> dict[str, Any]:
    """The per-worker test database, refusing any name without ``_test_``."""
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in test_db_name:
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


@pytest_asyncio.fixture
async def pool():
    """A store over the test database, always left empty at head afterwards."""
    config = _db_config()
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=5)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    apply_migrations(config)
    async with pool.acquire() as conn:
        await reset_agent_tables(conn)
    try:
        yield pool
    finally:
        # These tests walk the schema backwards. Whatever happened, the shared
        # per-worker database is expected at head by every other test.
        apply_migrations(config)
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        await pool.close()


@pytest.fixture
def store(pool):
    """Assemble the storage ports over the test-owned pool."""
    return Storage(pool)


async def _connect(
    store: Storage,
    user_id: str,
    *,
    label: str = "GitHub",
    template: str | None = "github",
    auth_kind: str = "static",
    access_token: str | None = "ciphertext-token",
    refresh_token: str | None = None,
    client_secret: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """Store one connection and its credential, the way the flows will."""
    row = await store.mcp.create_mcp_connection(
        user_id=user_id,
        label=label,
        template=template,
        transport="streamable-http",
        location="remote",
        url=_URL,
        launch_spec=None,
        auth_kind=auth_kind,
        auth_config=(
            {"header_name": "Authorization", "scheme": "Bearer"}
            if auth_kind == "static"
            else {"client_id": "client-1"}
        ),
        account_label="octocat",
    )
    if access_token is not None or refresh_token is not None or client_secret is not None:
        written = await store.mcp.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=row["authorization_epoch"],
            access_token_ciphertext=access_token,
            refresh_token_ciphertext=refresh_token,
            client_secret_ciphertext=client_secret,
            expires_at=expires_at,
            issuer="https://issuer.example.com" if auth_kind == "oauth" else None,
            resource=_URL,
        )
        assert written, "the fixture could not store the credential it just created"
    stored = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert stored is not None
    return stored


async def _create_job(store: Storage, *, user_id: str = _OWNER) -> dict[str, Any]:
    """One queued job, so bindings and invocations have a real parent row."""
    return await store.conversation_commands.create_job(
        user_id=user_id,
        repo="example-org/example-repo",
        task_prompt="fix the flaky test",
        runtime="claude-code",
        model="glm-5.1",
        base_sha="deadbeef",
    )


async def _invocation(
    store: Storage,
    job: dict[str, Any],
    connection: dict[str, Any],
    *,
    expires_in: timedelta = timedelta(minutes=10),
    policy_revision: int = 1,
    arguments_ciphertext: str | None = None,
) -> dict[str, Any]:
    """One held call, pending and undecided."""
    return await store.mcp.create_mcp_invocation(
        job_id=str(job["id"]),
        attempt_id=None,
        lease_generation=0,
        thread_id=str(job["thread_id"]),
        connection_id=int(connection["id"]),
        capability_name="get_issue",
        arguments_digest="digest-1",
        arguments_ciphertext=arguments_ciphertext,
        config_revision=int(connection["config_revision"]),
        authorization_epoch=int(connection["authorization_epoch"]),
        capability_revision=int(connection["capability_revision"]),
        policy_revision=policy_revision,
        expires_at=datetime.now(timezone.utc) + expires_in,
    )


async def test_concurrent_setup_retries_resume_one_pending_connection(store):
    values = {
        "user_id": _OWNER,
        "label": "Notion",
        "transport": "streamable-http",
        "location": "remote",
        "url": _URL,
        "launch_spec": None,
        "auth_kind": "none",
        "auth_config": {},
        "reuse_pending": True,
    }
    results = await asyncio.gather(*(store.mcp.create_mcp_connection(**values) for _ in range(8)))
    assert len({row["id"] for row in results}) == 1
    assert len(await store.mcp.list_mcp_connections(user_id=_OWNER)) == 1

    first = results[0]
    updated = await store.mcp.update_mcp_connection(
        connection_id=first["id"],
        user_id=_OWNER,
        expected_config_revision=1,
        auth_kind="oauth",
        auth_config={"client_id": "registered-client"},
        reset_status=True,
    )
    assert await store.mcp.set_mcp_credential(
        connection_id=first["id"],
        expected_authorization_epoch=updated["authorization_epoch"],
        access_token_ciphertext=None,
        client_secret_ciphertext="registered-client-secret",
    )
    assert await store.mcp.set_mcp_connection_status(
        connection_id=first["id"], status="error", error_kind="needs_authorization"
    )
    await store.mcp.set_mcp_connection_enabled(
        connection_id=first["id"], user_id=_OWNER, enabled=False
    )
    before = await store.mcp.get_mcp_connection(connection_id=first["id"])
    # Re-importing the original no-auth URL keeps OAuth registration and pause
    # state, instead of resetting the connection or enabling a duplicate.
    assert await store.mcp.create_mcp_connection(**values) == before


async def test_bulk_create_rolls_back_when_a_later_credential_insert_is_rejected(
    store: Storage, pool
) -> None:
    """A database rejection cannot leave the import's earlier draft visible."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE FUNCTION agent_test_reject_mcp_credential() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.access_token_ciphertext = 'reject-this-credential' THEN
                    RAISE EXCEPTION 'injected credential rejection';
                END IF;
                RETURN NEW;
            END
            $$
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER trg_agent_test_reject_mcp_credential
            BEFORE INSERT ON agent_mcp_connections
            FOR EACH ROW EXECUTE FUNCTION agent_test_reject_mcp_credential()
            """
        )
    common = {
        "user_id": _OWNER,
        "transport": "streamable-http",
        "location": "remote",
        "launch_spec": None,
        "auth_config": {},
        "template": None,
        "reuse_pending": False,
    }
    connections = [
        {
            **common,
            "label": "Public",
            "url": "https://public.example.com/mcp",
            "auth_kind": "none",
        },
        {
            **common,
            "label": "Private",
            "url": "https://private.example.com/mcp",
            "auth_kind": "static",
            "access_token_ciphertext": "reject-this-credential",
        },
    ]
    try:
        with pytest.raises(asyncpg.PostgresError, match="injected credential rejection"):
            await store.mcp.create_mcp_connections(connections=connections)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS trg_agent_test_reject_mcp_credential "
                "ON agent_mcp_connections"
            )
            await conn.execute("DROP FUNCTION IF EXISTS agent_test_reject_mcp_credential()")

    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []
    retried = await store.mcp.create_mcp_connections(connections=connections)
    assert [row["label"] for row in retried] == ["Public", "Private"]
    assert retried[1]["access_token_ciphertext"] == "reject-this-credential"


@pytest.mark.parametrize("invalid_connector", ["other-owner", "revoked"])
async def test_bulk_create_rolls_back_when_a_later_connector_is_unavailable(
    store: Storage, invalid_connector: str
) -> None:
    """Connector ownership and revocation are checked inside the batch transaction."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OTHER if invalid_connector == "other-owner" else _OWNER,
        name="Laptop",
        registration_hash=f"registration-{invalid_connector}",
    )
    if invalid_connector == "revoked":
        assert await store.mcp.revoke_mcp_connector(
            connector_id=int(connector["id"]), user_id=_OWNER
        )
    common = {
        "user_id": _OWNER,
        "transport": "streamable-http",
        "location": "remote",
        "launch_spec": None,
        "auth_kind": "none",
        "auth_config": {},
        "template": None,
        "reuse_pending": False,
    }
    with pytest.raises(McpConnectionCreateConflict, match="connector is no longer available"):
        await store.mcp.create_mcp_connections(
            connections=[
                {
                    **common,
                    "label": "First",
                    "url": "https://first.example.com/mcp",
                },
                {
                    **common,
                    "label": "Second",
                    "transport": "stdio",
                    "location": "user-connector",
                    "url": None,
                    "launch_spec": {"command": "mcp-server", "args": [], "env": {}, "mounts": []},
                    "connector_id": int(connector["id"]),
                },
            ]
        )

    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []


async def test_direct_connector_create_rolls_back_if_authorization_insert_fails(
    store: Storage, pool
) -> None:
    """The route, encrypted config row, and reverse authority commit together."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Laptop",
        registration_hash="registration-atomic-direct-create",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE FUNCTION agent_test_reject_mcp_connector_mapping() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected connector mapping rejection';
            END
            $$
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER trg_agent_test_reject_mcp_connector_mapping
            BEFORE INSERT ON agent_mcp_connector_connections
            FOR EACH ROW EXECUTE FUNCTION agent_test_reject_mcp_connector_mapping()
            """
        )
    values = {
        "user_id": _OWNER,
        "label": "Local",
        "transport": "stdio",
        "location": "user-connector",
        "url": None,
        "launch_spec": {"command": "mcp-server", "args": [], "env": {}, "mounts": []},
        "auth_kind": "none",
        "auth_config": {},
        "connector_id": int(connector["id"]),
    }
    try:
        with pytest.raises(asyncpg.PostgresError, match="injected connector mapping rejection"):
            await store.mcp.create_mcp_connection(**values)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS trg_agent_test_reject_mcp_connector_mapping "
                "ON agent_mcp_connector_connections"
            )
            await conn.execute("DROP FUNCTION IF EXISTS agent_test_reject_mcp_connector_mapping()")

    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []
    assert await store.mcp.list_mcp_connector_connections(connector_id=int(connector["id"])) == []

    created = await store.mcp.create_mcp_connection(**values)
    assert created["connector_id"] == connector["id"]
    assert await store.mcp.list_mcp_connector_connections(connector_id=int(connector["id"])) == [
        created["id"]
    ]


async def test_direct_connector_create_rejects_a_non_connector_location(
    store: Storage,
) -> None:
    """Repository callers cannot bypass the API's location validation."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Laptop",
        registration_hash="registration-invalid-direct-location",
    )
    with pytest.raises(McpConnectionCreateConflict, match="can only serve"):
        await store.mcp.create_mcp_connection(
            user_id=_OWNER,
            label="Remote",
            transport="streamable-http",
            location="remote",
            url=_URL,
            launch_spec=None,
            auth_kind="none",
            auth_config={},
            connector_id=int(connector["id"]),
        )

    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []
    assert await store.mcp.list_mcp_connector_connections(connector_id=int(connector["id"])) == []


@pytest.mark.parametrize(
    "different",
    [
        {"user_id": _OTHER},
        {"label": "Work Notion"},
        {"account_label": "work"},
        {"url": "https://other.example.com/mcp"},
        {"transport": "http-sse"},
        {"request_timeout_s": 120},
    ],
)
async def test_pending_setup_reuse_keeps_distinct_connections_separate(store, different):
    values = {
        "user_id": _OWNER,
        "label": "Notion",
        "transport": "streamable-http",
        "location": "remote",
        "url": _URL,
        "launch_spec": None,
        "auth_kind": "oauth",
        "auth_config": {},
        "reuse_pending": True,
    }
    first = await store.mcp.create_mcp_connection(**values)
    second = await store.mcp.create_mcp_connection(**{**values, **different})
    assert second["id"] != first["id"]
    assert await store.mcp.create_mcp_connection(**values) == first


@pytest.mark.parametrize("state", ["authenticated", "disconnected", "static"])
async def test_setup_reuse_does_not_replace_an_account_or_revive_a_disconnect(store, state):
    values = {
        "user_id": _OWNER,
        "label": "Notion",
        "transport": "streamable-http",
        "location": "remote",
        "url": _URL,
        "launch_spec": None,
        "auth_kind": "static" if state == "static" else "oauth",
        "auth_config": {},
        "reuse_pending": True,
    }
    first = await store.mcp.create_mcp_connection(**values)
    if state == "authenticated":
        assert await store.mcp.set_mcp_credential(
            connection_id=first["id"],
            expected_authorization_epoch=1,
            access_token_ciphertext="account-one-token",
        )
    elif state == "disconnected":
        assert await store.mcp.disconnect_mcp_connection(connection_id=first["id"], user_id=_OWNER)
    before = await store.mcp.get_mcp_connection(connection_id=first["id"])
    second = await store.mcp.create_mcp_connection(**values)
    assert second["id"] != first["id"]
    assert await store.mcp.get_mcp_connection(connection_id=first["id"]) == before


# -- owner scope -------------------------------------------------------


async def test_a_connection_is_only_ever_read_or_written_by_its_owner(
    store: Storage,
) -> None:
    """Two users, the same service: no query may blur them together.

    The relay is the one reader that looks a connection up by id alone, and it
    does so only after the fence named the owner; every owner-facing call has to
    carry the scope in its own SQL, which is what this checks.
    """
    mine = await _connect(store, _OWNER, label="Mine")
    theirs = await _connect(store, _OTHER, label="Theirs", access_token="ciphertext-theirs")

    assert [row["id"] for row in await store.mcp.list_mcp_connections(user_id=_OWNER)] == [
        mine["id"]
    ]
    assert await store.mcp.list_mcp_connections(user_id="nobody") == []
    # By id alone, as the relay reads it.
    assert (await store.mcp.get_mcp_connection(connection_id=theirs["id"]))["user_id"] == _OTHER
    assert await store.mcp.get_mcp_connection(connection_id=theirs["id"], user_id=_OWNER) is None

    # A write without its owner scope would edit somebody else's connection.
    assert (
        await store.mcp.update_mcp_connection(
            connection_id=theirs["id"],
            user_id=_OWNER,
            expected_config_revision=1,
            label="Stolen",
        )
        is None
    )
    assert (await store.mcp.get_mcp_connection(connection_id=theirs["id"]))["label"] == "Theirs"

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=theirs["id"], user_id=_OWNER)
        is False
    )
    assert (await store.mcp.get_mcp_connection(connection_id=theirs["id"]))["deleted_at"] is None


async def test_the_same_service_twice_is_two_connections(store: Storage) -> None:
    """The constraint this revision removed: one account per provider.

    Two accounts on one service are two connections, distinguished by id. A
    label is display only and resolves nothing, which is why both rows survive
    an identical template.
    """
    work = await _connect(store, _OWNER, label="Work")
    personal = await _connect(store, _OWNER, label="Personal")

    assert work["id"] != personal["id"]
    rows = await store.mcp.list_mcp_connections(user_id=_OWNER)
    assert [row["label"] for row in rows] == ["Personal", "Work"]
    # Untested is the honest starting state for a connection nobody has used.
    assert {row["status"] for row in rows} == {"draft"}


async def test_editing_the_target_moves_the_versions_a_decision_is_fenced_on(
    store: Storage,
) -> None:
    """A different endpoint or credential is a different server to approve.

    The revision moves only for a security-relevant edit: renaming a connection
    must not make every session re-approve the tools it already has.
    """
    row = await _connect(store, _OWNER)
    edited = await store.mcp.update_mcp_connection(
        connection_id=row["id"],
        user_id=_OWNER,
        expected_config_revision=row["config_revision"],
        url="https://other.example.com/mcp",
    )
    assert edited is not None
    assert edited["config_revision"] == row["config_revision"] + 1
    assert edited["authorization_epoch"] == row["authorization_epoch"] + 1

    renamed = await store.mcp.update_mcp_connection(
        connection_id=row["id"],
        user_id=_OWNER,
        expected_config_revision=edited["config_revision"],
        label="Renamed",
    )
    assert renamed is not None
    assert renamed["label"] == "Renamed"
    assert renamed["config_revision"] == edited["config_revision"]
    assert renamed["authorization_epoch"] == edited["authorization_epoch"]

    # A stale editor loses rather than silently merging into the newer row.
    assert (
        await store.mcp.update_mcp_connection(
            connection_id=row["id"],
            user_id=_OWNER,
            expected_config_revision=row["config_revision"],
            label="Raced",
        )
        is None
    )


# -- credentials and disconnects ---------------------------------------


async def test_disconnecting_clears_the_secret_and_bumps_the_epoch(
    store: Storage,
) -> None:
    """A tombstone that still holds a token is not a disconnect.

    The row is kept so a binding can say *this was disconnected* rather than
    *this never existed*, and every version a decision was fenced against moves
    so approvals made against the old credential stop resolving.
    """
    row = await _connect(
        store,
        _OWNER,
        refresh_token="ciphertext-refresh",
        client_secret="ciphertext-client-secret",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    gone = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert gone is not None
    assert gone["deleted_at"] is not None
    assert gone["status"] == "disabled"
    assert gone["access_token_ciphertext"] is None
    assert gone["refresh_token_ciphertext"] is None
    assert gone["client_secret_ciphertext"] is None
    assert gone["expires_at"] is None
    assert gone["authorization_epoch"] == row["authorization_epoch"] + 1
    assert gone["credential_version"] == row["credential_version"] + 1
    assert gone["oauth_generation"] == row["oauth_generation"] + 1

    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []
    assert len(await store.mcp.list_mcp_connections(user_id=_OWNER, include_deleted=True)) == 1
    # Disconnecting twice reports the second as a no-op, so a retry cannot bump
    # the epoch again and invalidate bindings that were re-approved meanwhile.
    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is False
    )
    assert (await store.mcp.get_mcp_connection(connection_id=row["id"]))[
        "authorization_epoch"
    ] == gone["authorization_epoch"]


async def test_a_credential_write_conditional_on_a_stale_epoch_loses(
    store: Storage,
) -> None:
    """A refresh that started before a disconnect must not restore the secret.

    This is the whole reason the write carries an epoch: a late OAuth callback
    or a slow refresh is indistinguishable from a legitimate one except by the
    version it read, and only one of them may win.
    """
    row = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="ciphertext-refresh")
    stale_epoch = row["authorization_epoch"]

    assert (
        await store.mcp.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True
    )

    assert (
        await store.mcp.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=stale_epoch,
            access_token_ciphertext="ciphertext-restored",
        )
        is False
    )
    assert (await store.mcp.get_mcp_connection(connection_id=row["id"]))[
        "access_token_ciphertext"
    ] is None


async def test_a_manual_replacement_moves_the_epoch_and_fences_the_old_one(
    store: Storage,
) -> None:
    """Replacing a credential may change the account behind it.

    Every approval made against the old identity has to be re-made, which is
    what ``bump_epoch`` is for: a refresh keeps the epoch and moves only the
    credential version, while a manual replacement moves both.
    """
    row = await _connect(store, _OWNER, access_token="ciphertext-old")
    old_epoch = row["authorization_epoch"]

    assert (
        await store.mcp.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=old_epoch,
            access_token_ciphertext="ciphertext-new",
            bump_epoch=True,
        )
        is True
    )

    replaced = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert replaced is not None
    assert replaced["authorization_epoch"] == old_epoch + 1
    assert replaced["credential_version"] > row["credential_version"]
    assert (
        await store.mcp.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=old_epoch,
            access_token_ciphertext="ciphertext-stale",
        )
        is False
    )
    assert (await store.mcp.get_mcp_connection(connection_id=row["id"]))[
        "access_token_ciphertext"
    ] == "ciphertext-new"


# -- one winner --------------------------------------------------------


async def test_consuming_an_oauth_flow_has_exactly_one_winner(store: Storage) -> None:
    """Two callbacks racing on one state value: one proceeds, one gets nothing.

    The ``consumed_at IS NULL`` predicate is inside the ``UPDATE``, which is the
    whole mechanism. A read-then-write would let both exchanges run, and the
    second would overwrite the credential the first just stored.
    """
    row = await _connect(store, _OWNER, auth_kind="oauth", access_token=None)
    created_generation = await store.mcp.create_mcp_oauth_flow(
        state_hash="state-hash",
        user_id=_OWNER,
        connection_id=row["id"],
        config_revision=row["config_revision"],
        authorization_epoch=row["authorization_epoch"],
        # Starting a flow moves the generation; the row records the new one.
        expected_oauth_generation=row["oauth_generation"],
        code_verifier_ciphertext="ciphertext-verifier",
        client_id="client-1",
        client_secret_ciphertext=None,
        client_auth_method="none",
        registration="dcr",
        issuer="https://issuer.example.com",
        resource=_URL,
        redirect_uri="https://app.example.com/callback",
        scopes=["read"],
        authorization_endpoint="https://issuer.example.com/authorize",
        token_endpoint="https://issuer.example.com/token",
        revocation_endpoint=None,
        registration_endpoint=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert created_generation == row["oauth_generation"] + 1

    # Presented by a browser signed in as somebody else, the state matches
    # nothing -- and the flow is left for the account that started it.
    assert await store.mcp.consume_mcp_oauth_flow(state_hash="state-hash", user_id=_OTHER) is None

    first, second = await asyncio.gather(
        store.mcp.consume_mcp_oauth_flow(state_hash="state-hash", user_id=_OWNER),
        store.mcp.consume_mcp_oauth_flow(state_hash="state-hash", user_id=_OWNER),
    )

    winners = [consumed for consumed in (first, second) if consumed is not None]
    assert len(winners) == 1
    assert winners[0]["connection_id"] == row["id"]
    assert winners[0]["scopes"] == ["read"]
    assert await store.mcp.consume_mcp_oauth_flow(state_hash="state-hash", user_id=_OWNER) is None
    # And starting a flow again moves the generation the row was fenced to.
    assert (await store.mcp.get_mcp_connection(connection_id=row["id"]))["oauth_generation"] == row[
        "oauth_generation"
    ] + 1


def _oauth_start(row: dict[str, Any], *, state_hash: str) -> dict[str, Any]:
    """Arguments for one start, fenced to the generation the caller observed."""
    return {
        "state_hash": state_hash,
        "user_id": _OWNER,
        "connection_id": row["id"],
        "config_revision": row["config_revision"],
        "authorization_epoch": row["authorization_epoch"],
        "expected_oauth_generation": row["oauth_generation"],
        "code_verifier_ciphertext": "ciphertext-verifier",
        "client_id": "client-1",
        "client_secret_ciphertext": None,
        "client_auth_method": "none",
        "registration": "dcr",
        "issuer": "https://issuer.example.com",
        "resource": _URL,
        "redirect_uri": "https://app.example.com/callback",
        "scopes": ["read"],
        "authorization_endpoint": "https://issuer.example.com/authorize",
        "token_endpoint": "https://issuer.example.com/token",
        "revocation_endpoint": None,
        "registration_endpoint": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    }


async def _pending_flows(pool: asyncpg.Pool, connection_id: int) -> list[dict[str, Any]]:
    """Unconsumed authorization flows for one connection."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT state_hash, oauth_generation
            FROM agent_mcp_oauth_flows
            WHERE connection_id = $1 AND consumed_at IS NULL
            ORDER BY state_hash
            """,
            connection_id,
        )
    return [dict(row) for row in rows]


async def test_concurrent_oauth_starts_leave_one_flow_at_the_allocated_generation(
    store: Storage, pool
) -> None:
    """Two starts that both read generation X must not both insert a flow for X+1.

    The survivor's generation is the value the connection update returned, so
    the callback's generation check can still accept that URL. The loser does
    not delete it.
    """
    row = await _connect(store, _OWNER, auth_kind="oauth", access_token=None)
    observed = int(row["oauth_generation"])
    first, second = await asyncio.gather(
        store.mcp.create_mcp_oauth_flow(**_oauth_start(row, state_hash="state-a")),
        store.mcp.create_mcp_oauth_flow(**_oauth_start(row, state_hash="state-b")),
    )
    allocated = [generation for generation in (first, second) if generation is not None]
    assert allocated == [observed + 1]
    current = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert current is not None
    assert int(current["oauth_generation"]) == observed + 1
    flows = await _pending_flows(pool, row["id"])
    assert len(flows) == 1
    assert int(flows[0]["oauth_generation"]) == int(current["oauth_generation"])
    consumed = await store.mcp.consume_mcp_oauth_flow(
        state_hash=str(flows[0]["state_hash"]), user_id=_OWNER
    )
    assert consumed is not None
    assert int(consumed["oauth_generation"]) == int(current["oauth_generation"])


async def test_a_stale_oauth_start_cannot_delete_a_newer_flow(store: Storage, pool) -> None:
    """A start that lost the generation fence must not remove the flow that won."""
    row = await _connect(store, _OWNER, auth_kind="oauth", access_token=None)
    allocated = await store.mcp.create_mcp_oauth_flow(**_oauth_start(row, state_hash="newer"))
    assert allocated == int(row["oauth_generation"]) + 1
    stale = dict(_oauth_start(row, state_hash="stale"))
    assert await store.mcp.create_mcp_oauth_flow(**stale) is None
    current = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert current is not None
    assert int(current["oauth_generation"]) == allocated
    flows = await _pending_flows(pool, row["id"])
    assert [str(item["state_hash"]) for item in flows] == ["newer"]
    assert int(flows[0]["oauth_generation"]) == allocated


async def test_claiming_an_approved_call_has_exactly_one_winner(store: Storage) -> None:
    """Two dispatchers arriving together: one sends, one must not.

    The call may already have produced an external side effect, so the loser
    reporting "somebody else has it" is the only safe answer -- a second send is
    how the same issue gets filed twice.
    """
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    invocation = await _invocation(store, job, connection)
    approved = await store.mcp.decide_mcp_invocation(
        invocation_id=invocation["id"], approve=True, decided_by=_OWNER
    )
    assert approved is not None and approved["status"] == "approved"

    first, second = await asyncio.gather(
        store.mcp.claim_mcp_invocation(invocation_id=invocation["id"]),
        store.mcp.claim_mcp_invocation(invocation_id=invocation["id"]),
    )

    winners = [claimed for claimed in (first, second) if claimed is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == "dispatching"
    assert winners[0]["dispatch_generation"] == 1
    assert await store.mcp.claim_mcp_invocation(invocation_id=invocation["id"]) is None


async def test_finishing_a_call_is_fenced_by_its_dispatch_generation(
    store: Storage,
) -> None:
    """A superseded worker cannot rewrite the outcome of a call it no longer owns."""
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    invocation = await _invocation(store, job, connection)
    await store.mcp.decide_mcp_invocation(
        invocation_id=invocation["id"], approve=True, decided_by=_OWNER
    )
    claimed = await store.mcp.claim_mcp_invocation(invocation_id=invocation["id"])
    assert claimed is not None

    assert (
        await store.mcp.finish_mcp_invocation(
            invocation_id=invocation["id"],
            dispatch_generation=int(claimed["dispatch_generation"]) + 1,
            status="completed",
        )
        is False
    )
    assert (
        await store.mcp.finish_mcp_invocation(
            invocation_id=invocation["id"],
            dispatch_generation=int(claimed["dispatch_generation"]),
            status="outcome_unknown",
            result_summary="the response was lost",
        )
        is True
    )
    stored = await store.mcp.get_mcp_invocation(invocation_id=invocation["id"])
    assert stored is not None
    assert stored["status"] == "outcome_unknown"


async def test_rebinding_a_connection_replaces_its_authorized_connector(
    pool,
    store: Storage,
) -> None:
    """A connector decides *where* a local process runs, so it is a target edit.

    The design lists it beside the command, the mount and the auth method for
    that reason. A connection whose connector changed is not the one a session
    was approved against, so both versions move -- and the connector's own
    authorized set is written in the same transaction, because the two halves
    disagreeing is how a connector ends up serving something the connection no
    longer routes to it.
    """
    first = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="My laptop", registration_hash="hash-1"
    )
    second = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="My other laptop", registration_hash="hash-2"
    )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Local files",
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "npx", "args": ["server@1.0.0"], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
    )
    before = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert before is not None

    bound = await store.mcp.bind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=first["id"]
    )
    assert bound is not None
    assert bound["connector_id"] == first["id"]
    assert bound["config_revision"] == before["config_revision"] + 1
    assert bound["authorization_epoch"] == before["authorization_epoch"] + 1
    assert await store.mcp.list_mcp_connector_connections(connector_id=first["id"]) == [
        connection["id"]
    ]

    rebound = await store.mcp.bind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=second["id"]
    )
    assert rebound is not None
    assert rebound["connector_id"] == second["id"]
    assert rebound["config_revision"] == bound["config_revision"] + 1
    assert rebound["authorization_epoch"] == bound["authorization_epoch"] + 1

    # This request started while the route still named the first connector.
    # The rebind won the race, so the stale unbind must match no row and must
    # not remove the second connector's authorization mapping.
    assert (
        await store.mcp.unbind_mcp_connection_connector(
            connection_id=connection["id"],
            user_id=_OWNER,
            connector_id=first["id"],
        )
        is None
    )
    still_routed = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert still_routed is not None
    assert still_routed["connector_id"] == second["id"]
    assert still_routed["config_revision"] == rebound["config_revision"]
    assert still_routed["authorization_epoch"] == rebound["authorization_epoch"]

    async with pool.acquire() as conn:
        route = await conn.fetchval(
            "SELECT connector_id FROM agent_mcp_connections WHERE id = $1",
            connection["id"],
        )
        mappings = await conn.fetch(
            """
            SELECT connector_id, connection_id
            FROM agent_mcp_connector_connections
            WHERE connection_id = $1
            ORDER BY connector_id
            """,
            connection["id"],
        )
    assert route == second["id"]
    assert [(row["connector_id"], row["connection_id"]) for row in mappings] == [
        (second["id"], connection["id"])
    ]
    assert await store.mcp.list_mcp_connector_connections(connector_id=first["id"]) == []
    assert await store.mcp.list_mcp_connector_connections(connector_id=second["id"]) == [
        connection["id"]
    ]

    unbound = await store.mcp.unbind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=second["id"]
    )
    assert unbound is not None
    assert unbound["connector_id"] is None
    assert unbound["config_revision"] == rebound["config_revision"] + 1
    assert await store.mcp.list_mcp_connector_connections(connector_id=second["id"]) == []


async def test_location_edit_away_from_user_connector_unbinds_atomically(
    store: Storage,
) -> None:
    """Changing the run location removes the singular connector authority."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Laptop",
        registration_hash="hash-location-edit-unbind",
    )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Local files",
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "mcp-server", "args": [], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
        connector_id=int(connector["id"]),
    )

    moved = await store.mcp.update_mcp_connection(
        connection_id=int(connection["id"]),
        user_id=_OWNER,
        expected_config_revision=int(connection["config_revision"]),
        transport="streamable-http",
        location="remote",
        url=_URL,
    )

    assert moved is not None
    assert moved["location"] == "remote"
    assert moved["connector_id"] is None
    assert moved["config_revision"] == connection["config_revision"] + 1
    assert moved["authorization_epoch"] == connection["authorization_epoch"] + 1
    assert await store.mcp.list_mcp_connector_connections(connector_id=int(connector["id"])) == []


async def test_disconnect_removes_connector_route_and_mapping(
    store: Storage,
) -> None:
    """A soft-deleted connection is no longer authorized on the connector."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Laptop",
        registration_hash="hash-disconnect-unbind",
    )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Local files",
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "mcp-server", "args": [], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
        connector_id=int(connector["id"]),
    )

    assert await store.mcp.disconnect_mcp_connection(
        connection_id=int(connection["id"]), user_id=_OWNER
    )

    tombstone = await store.mcp.get_mcp_connection(
        connection_id=int(connection["id"]), user_id=_OWNER
    )
    assert tombstone is not None
    assert tombstone["deleted_at"] is not None
    assert tombstone["connector_id"] is None
    assert await store.mcp.list_mcp_connector_connections(connector_id=int(connector["id"])) == []


async def test_remote_connection_cannot_be_bound_by_repository_caller(
    store: Storage,
) -> None:
    """The storage boundary preserves location even when the API is bypassed."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="Laptop",
        registration_hash="hash-reject-remote-bind",
    )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Remote",
        transport="streamable-http",
        location="remote",
        url=_URL,
        launch_spec=None,
        auth_kind="none",
        auth_config={},
    )

    assert (
        await store.mcp.bind_mcp_connection_connector(
            connection_id=int(connection["id"]),
            user_id=_OWNER,
            connector_id=int(connector["id"]),
        )
        is None
    )
    unchanged = await store.mcp.get_mcp_connection(connection_id=int(connection["id"]))
    assert unchanged is not None and unchanged["connector_id"] is None
    assert await store.mcp.list_mcp_connector_connections(connector_id=int(connector["id"])) == []


async def _stdio_connection(store: Storage, *, label: str = "Local files") -> dict[str, Any]:
    """One connection that runs on the user's own machine."""
    return await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label=label,
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "npx", "args": ["server@1.0.0"], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
    )


async def _mapping_pairs(pool: asyncpg.Pool, connection_id: int) -> list[tuple[int, int]]:
    """Every connector authorization row for one connection."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT connector_id, connection_id
            FROM agent_mcp_connector_connections
            WHERE connection_id = $1
            ORDER BY connector_id
            """,
            connection_id,
        )
    return [(int(row["connector_id"]), int(row["connection_id"])) for row in rows]


async def test_unbinding_another_connector_leaves_the_binding(store: Storage, pool) -> None:
    """DELETE names a connector, and the row update has to name the same one.

    Checking that connector A exists and then clearing connection C is a stale
    path detaching B. The mismatch matches no row, so C's route, versions, and
    B's authorization row stay where they were.
    """
    left = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="Laptop A", registration_hash="hash-unbind-a"
    )
    right = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="Laptop B", registration_hash="hash-unbind-b"
    )
    connection = await _stdio_connection(store)
    bound = await store.mcp.bind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=right["id"]
    )
    assert bound is not None

    assert (
        await store.mcp.unbind_mcp_connection_connector(
            connection_id=connection["id"], user_id=_OWNER, connector_id=left["id"]
        )
        is None
    )

    current = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert current is not None
    assert current["connector_id"] == right["id"]
    assert current["config_revision"] == bound["config_revision"]
    assert current["authorization_epoch"] == bound["authorization_epoch"]
    assert await store.mcp.list_mcp_connector_connections(connector_id=left["id"]) == []
    assert await store.mcp.list_mcp_connector_connections(connector_id=right["id"]) == [
        connection["id"]
    ]
    assert await _mapping_pairs(pool, connection["id"]) == [(right["id"], connection["id"])]


async def test_a_stale_unbind_cannot_clear_a_connector_the_connection_moved_to(
    store: Storage, pool
) -> None:
    """Unbind and rebind serialize on the connection, and the loser does not win.

    An unbind that still names A must not clear the route after a rebind to B
    has committed, and it must not delete B's authorization row. Whichever
    statement acquires the row first, B is the route that remains: the unbind
    only matches while the connection still names A, so a successful rebind is
    last only when it ran after that unbind.
    """
    left = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="Laptop A", registration_hash="hash-unbind-race-a"
    )
    right = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="Laptop B", registration_hash="hash-unbind-race-b"
    )
    connection = await _stdio_connection(store, label="Racing files")
    assert (
        await store.mcp.bind_mcp_connection_connector(
            connection_id=connection["id"], user_id=_OWNER, connector_id=left["id"]
        )
        is not None
    )

    rebound, unbound = await asyncio.gather(
        store.mcp.bind_mcp_connection_connector(
            connection_id=connection["id"], user_id=_OWNER, connector_id=right["id"]
        ),
        store.mcp.unbind_mcp_connection_connector(
            connection_id=connection["id"], user_id=_OWNER, connector_id=left["id"]
        ),
    )

    current = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert current is not None
    assert rebound is not None
    assert current["connector_id"] == right["id"]
    assert await store.mcp.list_mcp_connector_connections(connector_id=left["id"]) == []
    assert await store.mcp.list_mcp_connector_connections(connector_id=right["id"]) == [
        connection["id"]
    ]
    assert await _mapping_pairs(pool, connection["id"]) == [(right["id"], connection["id"])]
    if unbound is not None:
        # The unbind matched A, so the rebind ran after it and moved both versions
        # one step further than that clear.
        assert current["config_revision"] == unbound["config_revision"] + 1
        assert current["authorization_epoch"] == unbound["authorization_epoch"] + 1


async def test_revoking_a_connector_detaches_what_routed_through_it(
    store: Storage,
) -> None:
    """One decision, one transaction: a revoked connector serves nothing.

    Checked in the database rather than in the handler, because the failure that
    matters is a half-applied revoke -- a connector marked revoked while a
    connection still names it, which jobs would keep resolving to.
    """
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="My laptop", registration_hash="hash-2"
    )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Local files",
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "npx", "args": ["server@1.0.0"], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
    )
    await store.mcp.bind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=connector["id"]
    )
    routed = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert routed is not None

    assert await store.mcp.revoke_mcp_connector(connector_id=connector["id"], user_id=_OWNER)

    detached = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert detached is not None
    assert detached["connector_id"] is None
    assert detached["authorization_epoch"] == routed["authorization_epoch"] + 1
    assert await store.mcp.list_mcp_connector_connections(connector_id=connector["id"]) == []
    rows = {row["id"]: row for row in await store.mcp.list_mcp_connectors(user_id=_OWNER)}
    assert rows[connector["id"]]["status"] == "revoked"
    # And it cannot be routed to again.
    assert (
        await store.mcp.bind_mcp_connection_connector(
            connection_id=connection["id"], user_id=_OWNER, connector_id=connector["id"]
        )
        is None
    )


async def test_revoked_connector_status_is_terminal(store: Storage) -> None:
    """Heartbeat-style status changes cannot resurrect a revoked connector."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="My laptop", registration_hash="hash-terminal-revoke"
    )
    for status in ("online", "offline", "pending"):
        assert await store.mcp.set_mcp_connector_status(connector_id=connector["id"], status=status)
    with pytest.raises(ValueError, match="invalid connector status"):
        await store.mcp.set_mcp_connector_status(connector_id=connector["id"], status="revoked")
    assert await store.mcp.revoke_mcp_connector(connector_id=connector["id"], user_id=_OWNER)
    for status in ("pending", "online", "offline"):
        assert not await store.mcp.set_mcp_connector_status(
            connector_id=connector["id"], status=status
        )

    rows = {row["id"]: row for row in await store.mcp.list_mcp_connectors(user_id=_OWNER)}
    assert rows[connector["id"]]["status"] == "revoked"
    assert rows[connector["id"]]["revoked_at"] is not None


async def test_revocation_wins_against_a_late_connector_bind(pool, store: Storage) -> None:
    """A bind waiting behind revoke cannot restore its route or authorization."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="My laptop", registration_hash="hash-revoke-race"
    )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Local files",
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "npx", "args": ["server@1.0.0"], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
    )
    assert await store.mcp.bind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=connector["id"]
    )

    late_pool = await asyncpg.create_pool(**_db_config(), min_size=1, max_size=1)
    late_store = Storage(late_pool)
    async with late_pool.acquire() as late_conn:
        late_pid = await late_conn.fetchval("SELECT pg_backend_pid()")
    revoke: asyncio.Task[bool] | None = None
    bind: asyncio.Task[dict[str, Any] | None] | None = None
    try:
        async with pool.acquire() as blocker, blocker.transaction():
            blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
            await blocker.fetchval(
                "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR UPDATE",
                connection["id"],
            )
            revoke = asyncio.create_task(
                store.mcp.revoke_mcp_connector(connector_id=connector["id"], user_id=_OWNER)
            )
            revoke_blocked = False
            for _ in range(100):
                revoke_blocked = await blocker.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                    blocker_pid,
                )
                if revoke_blocked:
                    break
                await asyncio.sleep(0.01)
            assert revoke_blocked, "revoke did not reach its connection lock"

            bind = asyncio.create_task(
                late_store.mcp.bind_mcp_connection_connector(
                    connection_id=connection["id"],
                    user_id=_OWNER,
                    connector_id=connector["id"],
                )
            )
            bind_blocked = False
            for _ in range(100):
                bind_blocked = await blocker.fetchval(
                    """
                    SELECT wait_event_type = 'Lock'
                    FROM pg_stat_activity
                    WHERE pid = $1
                    """,
                    late_pid,
                )
                if bind_blocked:
                    break
                await asyncio.sleep(0.01)
            assert bind_blocked, "bind did not wait behind the connector revoke"

        revoked, rebound = await asyncio.gather(revoke, bind)
        assert revoked is True
        assert rebound is None
    finally:
        pending = [task for task in (revoke, bind) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await late_pool.close()

    detached = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert detached is not None
    assert detached["connector_id"] is None
    assert await store.mcp.list_mcp_connector_connections(connector_id=connector["id"]) == []


async def test_revocation_wins_against_a_late_direct_connector_create(pool, store: Storage) -> None:
    """Create waits on the connector lock and cannot authorize after revoke."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OWNER,
        name="My laptop",
        registration_hash="hash-direct-create-revoke-race",
    )
    existing = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Existing local files",
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "mcp-server", "args": [], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
        connector_id=int(connector["id"]),
    )
    late_pool = await asyncpg.create_pool(**_db_config(), min_size=1, max_size=1)
    late_store = Storage(late_pool)

    revoke: asyncio.Task[bool] | None = None
    create: asyncio.Task[dict[str, Any]] | None = None
    try:
        async with pool.acquire() as blocker, blocker.transaction():
            blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
            await blocker.fetchval(
                "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR UPDATE",
                existing["id"],
            )
            revoke = asyncio.create_task(
                store.mcp.revoke_mcp_connector(connector_id=int(connector["id"]), user_id=_OWNER)
            )
            revoke_blocked = False
            for _ in range(100):
                revoke_blocked = await blocker.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                    blocker_pid,
                )
                if revoke_blocked:
                    break
                await asyncio.sleep(0.01)
            assert revoke_blocked, "revoke did not reach its connection lock"

            create = asyncio.create_task(
                late_store.mcp.create_mcp_connection(
                    user_id=_OWNER,
                    label="Late local files",
                    transport="stdio",
                    location="user-connector",
                    url=None,
                    launch_spec={
                        "command": "mcp-server",
                        "args": [],
                        "env": {},
                        "mounts": [],
                    },
                    auth_kind="none",
                    auth_config={},
                    connector_id=int(connector["id"]),
                )
            )
            create_blocked = False
            for _ in range(100):
                create_blocked = await blocker.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND pid <> pg_backend_pid()
                          AND wait_event_type = 'Lock'
                          AND query LIKE '%agent_mcp_connectors%'
                    )
                    """
                )
                if create_blocked:
                    break
                await asyncio.sleep(0.01)
            assert create_blocked, (
                "create did not wait behind the connector revoke; "
                f"done={create.done()} "
                f"exception={create.exception() if create.done() else None!r}"
            )

        assert await revoke is True
        with pytest.raises(McpConnectionCreateConflict, match="no longer available"):
            await create
    finally:
        pending = [task for task in (revoke, create) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await late_pool.close()

    rows = await store.mcp.list_mcp_connections(user_id=_OWNER)
    assert [row["label"] for row in rows] == ["Existing local files"]
    assert rows[0]["connector_id"] is None
    assert await store.mcp.list_mcp_connector_connections(connector_id=int(connector["id"])) == []


async def test_rebinding_drops_the_previous_connector_authorization(store: Storage, pool) -> None:
    """Moving C from A to B leaves one route and one authorization row.

    Updating ``connector_id`` and inserting the new pair without deleting the
    old one leaves A authorized for a connection that no longer routes there.
    The listing, the connection, and the mapping table have to name B only,
    and both versions still move with the rebind.
    """
    left = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="Laptop A", registration_hash="hash-rebind-a"
    )
    right = await store.mcp.create_mcp_connector(
        user_id=_OWNER, name="Laptop B", registration_hash="hash-rebind-b"
    )
    connection = await _stdio_connection(store, label="Rebound files")
    first = await store.mcp.bind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=left["id"]
    )
    assert first is not None
    rebound = await store.mcp.bind_mcp_connection_connector(
        connection_id=connection["id"], user_id=_OWNER, connector_id=right["id"]
    )
    assert rebound is not None
    assert rebound["connector_id"] == right["id"]
    assert rebound["config_revision"] == first["config_revision"] + 1
    assert rebound["authorization_epoch"] == first["authorization_epoch"] + 1
    assert await store.mcp.list_mcp_connector_connections(connector_id=left["id"]) == []
    assert await store.mcp.list_mcp_connector_connections(connector_id=right["id"]) == [
        connection["id"]
    ]
    assert await _mapping_pairs(pool, connection["id"]) == [(right["id"], connection["id"])]


async def test_a_connection_and_its_credential_commit_together(store: Storage) -> None:
    """The secret is not a second request that can fail after the row is visible."""
    created = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="GitHub",
        transport="streamable-http",
        location="remote",
        url=_URL,
        launch_spec={
            "command": "npx",
            "args": ["server@1.0.0"],
            "env": {"API_KEY": "secret://API_KEY"},
        },
        auth_kind="static",
        auth_config={"header_name": "Authorization", "scheme": "Bearer"},
        access_token_ciphertext="ciphertext-token",
        auth_config_patch={"hint": "ci****en"},
        sealed_launch_spec={
            "command": "npx",
            "args": ["server@1.0.0"],
            "env": {"API_KEY": "secret://API_KEY"},
            "env_secrets": {"API_KEY": "ciphertext-env"},
        },
    )
    assert created["access_token_ciphertext"] == "ciphertext-token"
    assert created["credential_version"] == 2
    assert created["auth_config"]["hint"] == "ci****en"
    assert created["auth_config"]["scheme"] == "Bearer"
    assert created["launch_spec"]["env_secrets"]["API_KEY"] == "ciphertext-env"
    assert [row["id"] for row in await store.mcp.list_mcp_connections(user_id=_OWNER)] == [
        created["id"]
    ]


async def test_a_rejected_credential_batch_leaves_no_connection(store: Storage) -> None:
    """A later draft's credential failure rolls back the drafts already inserted."""

    class _Unsealable:
        """A patch value ``json.dumps`` refuses after the insert has run."""

    first = {
        "user_id": _OWNER,
        "label": "Wiki",
        "transport": "streamable-http",
        "location": "remote",
        "url": _URL,
        "launch_spec": None,
        "auth_kind": "static",
        "auth_config": {"header_name": "Authorization", "scheme": "Bearer"},
        "access_token_ciphertext": "ciphertext-wiki",
        "auth_config_patch": {"hint": "wi****ki"},
    }
    second = {
        **first,
        "label": "Tracker",
        "url": "https://tracker.example.com/mcp",
        "access_token_ciphertext": "ciphertext-tracker",
        "auth_config_patch": {"hint": _Unsealable()},
    }
    with pytest.raises(TypeError):
        await store.mcp.create_mcp_connections(connections=[first, second])
    assert await store.mcp.list_mcp_connections(user_id=_OWNER) == []


async def test_a_connector_cannot_be_bound_across_owners(store: Storage) -> None:
    """The binding is the authorization step, so it has to check whose it is."""
    connector = await store.mcp.create_mcp_connector(
        user_id=_OTHER, name="Theirs", registration_hash="hash-3"
    )
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Local files",
        transport="stdio",
        location="user-connector",
        url=None,
        launch_spec={"command": "npx", "args": ["server@1.0.0"], "env": {}, "mounts": []},
        auth_kind="none",
        auth_config={},
    )
    assert (
        await store.mcp.bind_mcp_connection_connector(
            connection_id=connection["id"], user_id=_OWNER, connector_id=connector["id"]
        )
        is None
    )


async def test_a_decision_after_the_deadline_does_not_revive_a_call(
    store: Storage,
) -> None:
    """The owner is approving something they were shown, and it is gone.

    Which is why the deadline lives in the predicate rather than in a read: a
    check-then-write lets exactly the late approval this exists to refuse
    through, and the expiry is recorded rather than left pending.
    """
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    invocation = await _invocation(store, job, connection, expires_in=timedelta(seconds=-1))

    assert (
        await store.mcp.decide_mcp_invocation(
            invocation_id=invocation["id"], approve=True, decided_by=_OWNER
        )
        is None
    )

    stored = await store.mcp.get_mcp_invocation(invocation_id=invocation["id"])
    assert stored is not None
    assert stored["status"] == "expired"
    assert await store.mcp.claim_mcp_invocation(invocation_id=invocation["id"]) is None


async def test_decision_rechecks_deadline_after_a_plain_invocation_row_lock(
    pool, store: Storage
) -> None:
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    invocation = await _invocation(store, job, connection)
    async with pool.acquire() as setup:
        await setup.execute(
            "UPDATE agent_mcp_invocations "
            "SET expires_at = clock_timestamp() + INTERVAL '400 milliseconds' "
            "WHERE id = $1",
            invocation["id"],
        )

    deciding = None
    async with pool.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT id FROM agent_mcp_invocations WHERE id = $1 FOR UPDATE",
            invocation["id"],
        )
        deciding = asyncio.create_task(
            store.mcp.decide_mcp_invocation(
                invocation_id=invocation["id"],
                approve=True,
                decided_by=_OWNER,
            )
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
        assert waiting, "decision did not wait on the invocation row lock"
        await asyncio.sleep(0.55)

    assert deciding is not None
    assert await asyncio.wait_for(deciding, timeout=2) is None
    stored = await store.mcp.get_mcp_invocation(invocation_id=invocation["id"])
    assert stored is not None and stored["status"] == "expired"


async def test_dispatch_rechecks_attempt_expiry_after_waiting_on_connection_authority(
    pool, store: Storage
) -> None:
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    await store.mcp.replace_mcp_bindings(
        thread_id=str(job["thread_id"]),
        bindings=[_permission_binding(connection, "github")],
    )
    binding = (await store.mcp.list_mcp_bindings(thread_id=str(job["thread_id"])))[0]
    async with pool.acquire() as setup:
        attempt_id = await setup.fetchval(
            """
            INSERT INTO agent_attempts (
                job_id, thread_id, attempt_no, lease_owner, lease_generation,
                lease_expires_at, status
            ) VALUES (
                $1, $2, 1, 'mcp-clock-test', 1,
                clock_timestamp() + INTERVAL '400 milliseconds', 'running'
            ) RETURNING id
            """,
            job["id"],
            job["thread_id"],
        )
        await setup.execute(
            "UPDATE agent_jobs SET state = 'running', current_attempt_id = $2 WHERE id = $1",
            job["id"],
            attempt_id,
        )
    invocation = await store.mcp.create_mcp_invocation(
        job_id=str(job["id"]),
        attempt_id=attempt_id,
        lease_generation=1,
        thread_id=str(job["thread_id"]),
        connection_id=int(connection["id"]),
        capability_name="get_issue",
        arguments_digest="clock-wait",
        arguments_ciphertext=None,
        config_revision=int(connection["config_revision"]),
        authorization_epoch=int(connection["authorization_epoch"]),
        capability_revision=int(connection["capability_revision"]),
        policy_revision=int(binding["policy_revision"]),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    approved = await store.mcp.decide_mcp_invocation(
        invocation_id=invocation["id"], approve=True, decided_by=_OWNER
    )
    assert approved is not None

    dispatching = None
    async with pool.acquire() as blocker, blocker.transaction():
        blocker_pid = await blocker.fetchval("SELECT pg_backend_pid()")
        await blocker.fetchval(
            "SELECT id FROM agent_mcp_connections WHERE id = $1 FOR UPDATE",
            connection["id"],
        )
        dispatching = asyncio.create_task(
            store.mcp.claim_mcp_invocation(
                invocation_id=invocation["id"],
                config_revision=int(connection["config_revision"]),
                authorization_epoch=int(connection["authorization_epoch"]),
                capability_revision=int(connection["capability_revision"]),
                policy_revision=int(binding["policy_revision"]),
                attempt_id=attempt_id,
                lease_generation=1,
            )
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
        assert waiting, "dispatch did not lock the connection authority row"
        await asyncio.sleep(0.55)

    assert dispatching is not None
    assert await asyncio.wait_for(dispatching, timeout=2) is None
    stored = await store.mcp.get_mcp_invocation(invocation_id=invocation["id"])
    assert stored is not None and stored["status"] == "approved"


# -- bindings and snapshots --------------------------------------------


async def test_replacing_a_threads_bindings_is_wholesale(store: Storage) -> None:
    """A selection is a decision about the *set*, not about one row.

    Leaving an unmentioned binding in place would mean removing a connection
    from the dialog did not remove its tools from the session.
    """
    job = await _create_job(store)
    github = await _connect(store, _OWNER, label="GitHub")
    wiki = await _connect(store, _OWNER, label="DeepWiki", template="deepwiki")

    def binding(connection: dict[str, Any], alias: str) -> dict[str, Any]:
        return {
            "connection_id": connection["id"],
            "alias": alias,
            "enabled": True,
            "config_revision": connection["config_revision"],
            "authorization_epoch": connection["authorization_epoch"],
            "capability_revision": connection["capability_revision"],
            "policy_revision": 1,
            "rules": {"get_issue": {"policy": "allow", "fingerprint": "abc"}},
            "resource_rules": {"docs": "confirm"},
            "prompt_rules": {},
        }

    await store.mcp.replace_mcp_bindings(
        thread_id=str(job["thread_id"]),
        bindings=[binding(github, "github"), binding(wiki, "deepwiki")],
    )
    listed = await store.mcp.list_mcp_bindings(thread_id=str(job["thread_id"]))
    assert [row["alias"] for row in listed] == ["deepwiki", "github"]
    by_alias = {row["alias"]: row for row in listed}
    assert by_alias["github"]["rules"]["get_issue"] == {"policy": "allow", "fingerprint": "abc"}
    assert by_alias["github"]["resource_rules"] == {"docs": "confirm"}
    # The join carries display data, so a page needs no second query.
    assert by_alias["deepwiki"]["label"] == "DeepWiki"
    assert by_alias["deepwiki"]["connection_status"] == "draft"

    await store.mcp.replace_mcp_bindings(
        thread_id=str(job["thread_id"]), bindings=[binding(wiki, "deepwiki")]
    )
    remaining = await store.mcp.list_mcp_bindings(thread_id=str(job["thread_id"]))
    assert [row["alias"] for row in remaining if row["enabled"]] == ["deepwiki"]
    assert next(row for row in remaining if row["alias"] == "github")["enabled"] is False


@pytest.mark.parametrize(
    "change",
    [
        "tool",
        "resource",
        "prompt",
        "alias",
        "enabled",
        "config_revision",
        "authorization_epoch",
        "capability_revision",
        "remove",
    ],
)
async def test_replacing_permissions_invalidates_old_approvals(store: Storage, change) -> None:
    """Changing or reattaching a binding cannot revive an earlier approval."""
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    thread_id = str(job["thread_id"])
    selection = {
        "connection_id": connection["id"],
        "alias": "github",
        "enabled": True,
        "config_revision": connection["config_revision"],
        "authorization_epoch": connection["authorization_epoch"],
        "capability_revision": connection["capability_revision"],
        "policy_revision": 1,
        "rules": {"get_issue": {"policy": "confirm", "fingerprint": "abc"}},
        "resource_rules": {"docs": "allow"},
        "prompt_rules": {},
    }
    await store.mcp.replace_mcp_bindings(thread_id=thread_id, bindings=[selection])
    original = (await store.mcp.list_mcp_bindings(thread_id=thread_id))[0]
    invocation = await store.mcp.create_mcp_invocation(
        job_id=str(job["id"]),
        attempt_id=None,
        lease_generation=0,
        thread_id=thread_id,
        connection_id=connection["id"],
        capability_name="get_issue",
        arguments_digest="digest",
        arguments_ciphertext=None,
        config_revision=connection["config_revision"],
        authorization_epoch=connection["authorization_epoch"],
        capability_revision=connection["capability_revision"],
        policy_revision=original["policy_revision"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    await store.mcp.decide_mcp_invocation(
        invocation_id=invocation["id"], approve=True, decided_by=_OWNER
    )
    pending = await _invocation(store, job, connection, policy_revision=original["policy_revision"])
    dispatched = await _invocation(
        store, job, connection, policy_revision=original["policy_revision"]
    )
    await store.mcp.decide_mcp_invocation(
        invocation_id=dispatched["id"], approve=True, decided_by=_OWNER
    )
    assert await store.mcp.claim_mcp_invocation(invocation_id=dispatched["id"]) is not None
    if change == "remove":
        await store.mcp.replace_mcp_bindings(thread_id=thread_id, bindings=[])
    elif change == "tool":
        selection["rules"] = {"get_issue": {"policy": "deny", "fingerprint": "abc"}}
    elif change == "resource":
        selection["resource_rules"] = {"docs": "deny"}
    elif change == "prompt":
        selection["prompt_rules"] = {"summary": "confirm"}
    elif change == "alias":
        selection["alias"] = "other"
    elif change == "enabled":
        selection["enabled"] = False
    else:
        selection[change] += 1
    await store.mcp.replace_mcp_bindings(thread_id=thread_id, bindings=[selection])
    changed = (await store.mcp.list_mcp_bindings(thread_id=thread_id))[0]
    assert await store.mcp.claim_mcp_invocation(invocation_id=invocation["id"]) is None
    assert (await store.mcp.get_mcp_invocation(invocation_id=invocation["id"]))[
        "status"
    ] == "expired"
    assert changed["policy_revision"] > original["policy_revision"]
    assert (await store.mcp.get_mcp_invocation(invocation_id=pending["id"]))["status"] == "expired"
    assert (await store.mcp.get_mcp_invocation(invocation_id=dispatched["id"]))[
        "status"
    ] == "dispatching"


def _permission_binding(connection, alias):
    """A selection with a tool held for approval."""
    return {
        "connection_id": connection["id"],
        "alias": alias,
        "enabled": True,
        "config_revision": connection["config_revision"],
        "authorization_epoch": connection["authorization_epoch"],
        "capability_revision": connection["capability_revision"],
        "rules": {"get_issue": {"policy": "confirm", "fingerprint": "abc"}},
        "resource_rules": {},
        "prompt_rules": {},
    }


async def test_unchanged_permissions_keep_their_approvals_and_revision(store: Storage):
    """Saving another connection must not interrupt this connection's held call."""
    job = await _create_job(store)
    first, second = await _connect(store, _OWNER), await _connect(store, _OWNER)
    thread_id = str(job["thread_id"])
    selections = [_permission_binding(first, "first"), _permission_binding(second, "second")]
    await store.mcp.replace_mcp_bindings(thread_id=thread_id, bindings=selections)
    original = await store.mcp.list_mcp_bindings(thread_id=thread_id)
    invocation = await _invocation(
        store, job, first, policy_revision=original[0]["policy_revision"]
    )
    await store.mcp.replace_mcp_bindings(thread_id=thread_id, bindings=selections)
    unchanged = await store.mcp.list_mcp_bindings(thread_id=thread_id)
    assert [row["policy_revision"] for row in unchanged] == [
        row["policy_revision"] for row in original
    ]
    selections[1]["enabled"] = False
    await store.mcp.replace_mcp_bindings(thread_id=thread_id, bindings=selections)
    changed = await store.mcp.list_mcp_bindings(thread_id=thread_id)
    assert changed[0]["policy_revision"] == original[0]["policy_revision"]
    assert changed[1]["policy_revision"] > original[1]["policy_revision"]
    assert (await store.mcp.get_mcp_invocation(invocation_id=invocation["id"]))[
        "status"
    ] == "pending"


async def test_concurrent_replacements_cannot_merge_two_distinct_selections(store: Storage):
    """The thread lock also serializes writes when there are no bindings yet."""
    job = await _create_job(store)
    first, second = await _connect(store, _OWNER), await _connect(store, _OWNER)
    thread_id = str(job["thread_id"])
    await asyncio.gather(
        *[
            store.mcp.replace_mcp_bindings(
                thread_id=thread_id, bindings=[_permission_binding(row, alias)]
            )
            for row, alias in [(first, "first"), (second, "second")]
        ]
    )
    assert (
        len(
            [
                row
                for row in await store.mcp.list_mcp_bindings(thread_id=thread_id)
                if row["enabled"]
            ]
        )
        == 1
    )


async def test_policy_sequence_migration_starts_above_previous_approval_revisions(store, pool):
    """Removing bindings cannot hide the largest historical approval revision."""
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    await _invocation(store, job, connection, policy_revision=5000)
    downgrade_to(_db_config(), "0009_mcp_refresh_lease")
    apply_migrations(_db_config())
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT nextval('agent_mcp_policy_revision_seq')") > 5000


async def test_saving_capabilities_moves_the_revision_it_is_fenced_against(
    store: Storage,
) -> None:
    """Discovery and its revision are written in one transaction.

    A binding's fence compares the connection's capability revision, so a
    snapshot stored without moving it would be a catalogue that appears without
    the approvals against the old one being invalidated.
    """
    row = await _connect(store, _OWNER)
    assert await store.mcp.get_mcp_capabilities(connection_id=row["id"]) is None

    revision = await store.mcp.save_mcp_capabilities(
        connection_id=row["id"],
        config_revision=row["config_revision"],
        protocol_version="2026-07-28",
        server_name="github",
        server_version="1.0",
        tools=[{"name": "get_issue", "description": "Fetch one", "input_schema": {}}],
        resources=[],
        resource_templates=[],
        prompts=[],
        support={},
    )

    assert revision == row["capability_revision"] + 1
    moved = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert moved is not None and moved["capability_revision"] == revision
    stored = await store.mcp.get_mcp_capabilities(connection_id=row["id"])
    assert stored is not None
    assert stored["capability_revision"] == revision
    assert [tool["name"] for tool in stored["tools"]] == ["get_issue"]

    # A second discovery replaces the snapshot and moves the revision again,
    # so an approval of the first catalogue cannot survive it.
    second = await store.mcp.save_mcp_capabilities(
        connection_id=row["id"],
        config_revision=row["config_revision"],
        protocol_version="2026-07-28",
        server_name="github",
        server_version="1.1",
        tools=[{"name": "list_issues", "description": "List", "input_schema": {}}],
        resources=[],
        resource_templates=[],
        prompts=[],
        support={},
    )
    assert second == revision + 1
    replaced = await store.mcp.get_mcp_capabilities(connection_id=row["id"])
    assert replaced is not None
    assert [tool["name"] for tool in replaced["tools"]] == ["list_issues"]


# -- the 0007 migration ------------------------------------------------


@pytest.mark.parametrize(
    "changed",
    ["config_revision", "authorization_epoch", "capability_revision", "credential_version"],
)
async def test_late_discovery_and_status_cannot_overwrite_changed_connection(store, pool, changed):
    """The discovery fence and status fence each refuse a newer connection state."""
    row = await _connect(store, _OWNER)
    if changed == "credential_version":
        assert await store.mcp.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=row["authorization_epoch"],
            access_token_ciphertext="rotated-ciphertext",
        )
    else:
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE agent_mcp_connections SET {changed} = {changed} + 1, status = 'draft' WHERE id = $1",
                row["id"],
            )
    revision = await store.mcp.save_mcp_capabilities(
        connection_id=row["id"],
        config_revision=row["config_revision"],
        expected_authorization_epoch=row["authorization_epoch"],
        expected_capability_revision=row["capability_revision"],
        expected_credential_version=row["credential_version"],
        protocol_version="2025-11-25",
        server_name="old-server",
        server_version="1",
        tools=[{"name": "old-tool"}],
        resources=[],
        resource_templates=[],
        prompts=[],
        support={},
    )
    assert revision == 0
    assert await store.mcp.get_mcp_capabilities(connection_id=row["id"]) is None
    for status in ("active", "error"):
        assert not await store.mcp.set_mcp_connection_status(
            connection_id=row["id"],
            status=status,
            expected_config_revision=row["config_revision"],
            expected_authorization_epoch=row["authorization_epoch"],
            expected_capability_revision=row["capability_revision"],
            expected_credential_version=row["credential_version"],
        )
    current = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert current["status"] == "draft"
    assert current[changed] == row[changed] + 1


@pytest.mark.parametrize("operation", ["refresh", "reauthorize", "stale_reauthorize"])
async def test_credential_and_authorization_status_share_one_fence(store, operation):
    row = await _connect(store, _OWNER, auth_kind="oauth")
    await store.mcp.set_mcp_connection_status(
        connection_id=row["id"],
        status="error",
        error_kind="needs_authorization",
        error_detail="old credential expired",
    )
    before = await store.mcp.get_mcp_connection(connection_id=row["id"])
    written = await store.mcp.set_mcp_credential(
        connection_id=row["id"],
        expected_authorization_epoch=row["authorization_epoch"],
        expected_oauth_generation=row["oauth_generation"] + (operation == "stale_reauthorize"),
        access_token_ciphertext="new-ciphertext",
        reset_status=operation != "refresh",
    )
    assert written is (operation != "stale_reauthorize")
    current = await store.mcp.get_mcp_connection(connection_id=row["id"])
    assert current["credential_version"] == row["credential_version"] + written
    if operation == "reauthorize":
        assert current["status"] == "draft"
        assert current["error_kind"] == "none"
        assert current["error_detail"] is None
        assert current["last_checked_at"] is None
    else:
        for key in ("status", "error_kind", "error_detail", "last_checked_at"):
            assert current[key] == before[key]


@pytest.mark.parametrize("operation", ["rename", "move", "change_auth", "stale_move"])
async def test_target_edit_invalidates_only_its_previous_discovery_and_flow(store, operation):
    row = await _connect(store, _OWNER, auth_kind="oauth")
    await store.mcp.save_mcp_capabilities(
        connection_id=row["id"],
        config_revision=row["config_revision"],
        protocol_version="2025-11-25",
        server_name="previous",
        server_version="1",
        tools=[],
        resources=[],
        resource_templates=[],
        prompts=[],
        support={},
    )
    await store.mcp.set_mcp_connection_status(connection_id=row["id"], status="active")
    created_generation = await store.mcp.create_mcp_oauth_flow(
        state_hash="before-edit",
        user_id=_OWNER,
        connection_id=row["id"],
        config_revision=row["config_revision"],
        authorization_epoch=row["authorization_epoch"],
        expected_oauth_generation=row["oauth_generation"],
        code_verifier_ciphertext="ciphertext-verifier",
        client_id="client",
        client_secret_ciphertext=None,
        client_auth_method="none",
        registration="dcr",
        issuer="https://issuer.example.com",
        resource=_URL,
        redirect_uri="https://app.example.com/callback",
        scopes=["read"],
        authorization_endpoint="https://issuer.example.com/authorize",
        token_endpoint="https://issuer.example.com/token",
        revocation_endpoint=None,
        registration_endpoint=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert created_generation == row["oauth_generation"] + 1
    changes = {
        "rename": {"label": "New label"},
        "move": {"url": "https://new.example.com/mcp"},
        "change_auth": {"auth_kind": "none"},
        "stale_move": {"url": "https://new.example.com/mcp"},
    }[operation]
    updated = await store.mcp.update_mcp_connection(
        connection_id=row["id"],
        user_id=_OWNER,
        expected_config_revision=row["config_revision"] + (operation == "stale_move"),
        **changes,
    )
    assert (updated is None) is (operation == "stale_move")
    current = await store.mcp.get_mcp_connection(connection_id=row["id"])
    snapshot = await store.mcp.get_mcp_capabilities(connection_id=row["id"])
    flow = await store.mcp.consume_mcp_oauth_flow(state_hash="before-edit", user_id=_OWNER)
    invalidated = operation in ("move", "change_auth")
    assert current["status"] == ("draft" if invalidated else "active")
    assert current["config_revision"] == row["config_revision"] + invalidated
    assert (snapshot is None) is invalidated
    assert (flow is None) is invalidated


async def test_concurrent_diagnostics_publish_one_consistent_catalogue(store):
    """A late discovery cannot replace a result already stored under the same fence."""
    row = await _connect(store, _OWNER)

    async def save(name):
        return await store.mcp.save_mcp_capabilities(
            connection_id=row["id"],
            config_revision=row["config_revision"],
            expected_authorization_epoch=row["authorization_epoch"],
            expected_capability_revision=row["capability_revision"],
            protocol_version="2025-11-25",
            server_name=name,
            server_version="1",
            tools=[{"name": name}],
            resources=[],
            resource_templates=[],
            prompts=[],
            support={},
        )

    revisions = await asyncio.gather(save("first"), save("second"))
    assert sorted(revisions) == [0, row["capability_revision"] + 1]
    snapshot = await store.mcp.get_mcp_capabilities(connection_id=row["id"])
    assert snapshot["server_name"] == ("first" if revisions[0] else "second")
    assert [tool["name"] for tool in snapshot["tools"]] == [snapshot["server_name"]]
    assert await store.mcp.set_mcp_connection_status(
        connection_id=row["id"],
        status="active",
        expected_config_revision=row["config_revision"],
        expected_authorization_epoch=row["authorization_epoch"],
        expected_capability_revision=snapshot["capability_revision"],
    )


async def test_an_edit_is_conditional_on_the_revision_that_was_read(
    store: Storage,
) -> None:
    """A stale edit matches no row rather than merging into somebody else's.

    The predicate is inside the ``UPDATE``, which is what makes two tabs -- or
    two instances -- produce one winner and one refusal instead of a row that
    reflects neither of them.
    """
    connection = await _connect(store, _OWNER)
    revision = int(connection["config_revision"])

    # A label is not a change of target, so nothing a decision is fenced on
    # moves -- which also means a second label edit at the same revision is
    # still valid rather than stale.
    renamed = await store.mcp.update_mcp_connection(
        connection_id=connection["id"],
        user_id=_OWNER,
        expected_config_revision=revision,
        label="Renamed",
    )
    assert renamed is not None
    assert renamed["config_revision"] == revision
    assert renamed["authorization_epoch"] == connection["authorization_epoch"]

    # A new target does move both, which is what invalidates the sessions
    # approved against the old one.
    moved = await store.mcp.update_mcp_connection(
        connection_id=connection["id"],
        user_id=_OWNER,
        expected_config_revision=revision,
        url="https://elsewhere.example.com/mcp",
    )
    assert moved is not None
    assert moved["config_revision"] == revision + 1
    assert moved["authorization_epoch"] == connection["authorization_epoch"] + 1

    # And now the revision the caller read at the start is stale.
    stale = await store.mcp.update_mcp_connection(
        connection_id=connection["id"],
        user_id=_OWNER,
        expected_config_revision=revision,
        label="Someone else's edit",
    )
    assert stale is None
    unchanged = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert unchanged is not None
    assert unchanged["label"] == "Renamed"


async def test_a_connection_belonging_to_somebody_else_cannot_be_edited(
    store: Storage,
) -> None:
    """Ownership is part of the write, not a check before it."""
    connection = await _connect(store, _OWNER)
    assert (
        await store.mcp.update_mcp_connection(
            connection_id=connection["id"],
            user_id=_OTHER,
            expected_config_revision=int(connection["config_revision"]),
            label="Theirs now",
        )
        is None
    )


async def test_a_disconnected_connection_is_listed_only_when_asked_for(
    store: Storage,
) -> None:
    """The page shows what is usable; the tombstone is for the record.

    A disconnect that removed the row would lose the reason a session's binding
    stopped resolving, and one that stayed in the ordinary list would offer a
    server nobody can reach.
    """
    live = await _connect(store, _OWNER, label="GitHub")
    gone = await _connect(store, _OWNER, label="DeepWiki", template="deepwiki")
    assert await store.mcp.disconnect_mcp_connection(connection_id=gone["id"], user_id=_OWNER)

    listed = await store.mcp.list_mcp_connections(user_id=_OWNER)
    assert [row["id"] for row in listed] == [live["id"]]
    everything = await store.mcp.list_mcp_connections(user_id=_OWNER, include_deleted=True)
    assert sorted(row["id"] for row in everything) == sorted([live["id"], gone["id"]])


async def test_a_live_refresh_lease_is_exclusive_and_an_expired_one_is_not(
    store: Storage,
    pool,
) -> None:
    """The race a version fence cannot see, because it is sequential.

    A claims the row and goes to the token endpoint. B arrives while A is on the
    network, reads the row *after* A's claim committed, and claims the version
    it just read. Under a version-only fence that is a winning claim -- the
    version B carries is the one A left behind -- and both workers then spend
    the same refresh token, which a rotating issuer invalidates the moment it is
    used twice. Only the lease refuses B.

    (Two claims issued at the same instant are not the interesting case: they
    take the row lock in turn and the second re-evaluates its predicate against
    the committed row, so the version fence alone settles that one.)

    A crashed winner is the other half: the lease expires, and the row is
    claimable again at the next generation rather than frozen until somebody
    notices.
    """
    connection = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="rt")
    version = int(connection["credential_version"])
    winner = await store.mcp.claim_mcp_refresh(
        connection_id=connection["id"],
        expected_credential_version=version,
        lease_seconds=60,
    )
    assert winner is not None
    assert winner["refresh_generation"] == int(connection["refresh_generation"]) + 1
    # Nothing has been written yet, so the version has not moved: it reports a
    # credential change, and a claim is not one.
    assert winner["credential_version"] == version

    seen = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert (
        await store.mcp.claim_mcp_refresh(
            connection_id=connection["id"],
            expected_credential_version=int(seen["credential_version"]),
            lease_seconds=60,
        )
        is None
    )

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_mcp_connections "
            "SET refresh_lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
            connection["id"],
        )
    replacement = await store.mcp.claim_mcp_refresh(
        connection_id=connection["id"],
        expected_credential_version=int(seen["credential_version"]),
        lease_seconds=60,
    )
    assert replacement is not None
    assert replacement["refresh_generation"] == winner["refresh_generation"] + 1


async def test_a_superseded_refresh_cannot_write_over_its_successor(
    store: Storage,
    pool,
) -> None:
    """The slow worker's answer came from a token that is already replaced."""
    connection = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="rt")
    version = int(connection["credential_version"])
    first = await store.mcp.claim_mcp_refresh(
        connection_id=connection["id"],
        expected_credential_version=version,
        lease_seconds=60,
    )
    assert first is not None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_mcp_connections "
            "SET refresh_lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
            connection["id"],
        )
    successor = await store.mcp.claim_mcp_refresh(
        connection_id=connection["id"],
        expected_credential_version=version,
        lease_seconds=60,
    )
    assert successor is not None

    assert not await store.mcp.set_mcp_credential(
        connection_id=connection["id"],
        expected_authorization_epoch=int(first["authorization_epoch"]),
        access_token_ciphertext="stale-access",
        expected_refresh_generation=int(first["refresh_generation"]),
    )
    assert await store.mcp.set_mcp_credential(
        connection_id=connection["id"],
        expected_authorization_epoch=int(successor["authorization_epoch"]),
        access_token_ciphertext="fresh-access",
        expected_refresh_generation=int(successor["refresh_generation"]),
    )
    row = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert row["access_token_ciphertext"] == "fresh-access"
    # The write that landed was the lease holder's, and it released the lease on
    # its way out: the next refresh does not wait for a timer.
    assert row["refresh_lease_expires_at"] is None
    assert row["credential_version"] == version + 1


async def test_a_refresh_does_not_overwrite_a_newer_authorization(
    store: Storage,
) -> None:
    """The callback fence, from the other side.

    A refresh spends the same time on the network as an authorization-code
    exchange, and the owner can start a new authorization inside that window.
    Both writes carry the same epoch -- re-authorizing is not a change of target
    -- so the OAuth generation the refresh claimed is what says its answer
    describes the account the owner has just replaced. Without it the slower
    write wins.
    """
    connection = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="rt")
    claim = await store.mcp.claim_mcp_refresh(
        connection_id=connection["id"],
        expected_credential_version=int(connection["credential_version"]),
        lease_seconds=60,
    )
    assert claim is not None
    # A new authorization begins while the refresh is still on the network.
    assert await store.mcp.set_mcp_client_registration(
        connection_id=connection["id"],
        user_id=_OWNER,
        client_id="client-2",
        client_secret_ciphertext=None,
    )
    assert not await store.mcp.set_mcp_credential(
        connection_id=connection["id"],
        expected_authorization_epoch=int(claim["authorization_epoch"]),
        access_token_ciphertext="stale-access",
        expected_oauth_generation=int(claim["oauth_generation"]),
        expected_config_revision=int(claim["config_revision"]),
        expected_refresh_generation=int(claim["refresh_generation"]),
    )
    row = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert row["access_token_ciphertext"] != "stale-access"
    # A refused write does not release the lease; the refresher still owns it.
    assert row["refresh_lease_expires_at"] is not None


async def test_releasing_a_refresh_claim_is_fenced_on_its_generation(
    store: Storage,
) -> None:
    """A failed attempt hands the row back; a late one must not hand it further."""
    connection = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="rt")
    version = int(connection["credential_version"])
    first = await store.mcp.claim_mcp_refresh(
        connection_id=connection["id"],
        expected_credential_version=version,
        lease_seconds=60,
    )
    assert first is not None
    assert await store.mcp.release_mcp_refresh_claim(
        connection_id=connection["id"], refresh_generation=int(first["refresh_generation"])
    )
    # Released, so the row is claimable now rather than when the lease ran out.
    second = await store.mcp.claim_mcp_refresh(
        connection_id=connection["id"],
        expected_credential_version=version,
        lease_seconds=60,
    )
    assert second is not None
    # The first worker's lease is superseded, so its release must not clear the
    # live one and leave a third caller free to spend the same token.
    assert not await store.mcp.release_mcp_refresh_claim(
        connection_id=connection["id"], refresh_generation=int(first["refresh_generation"])
    )
    live = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert live["refresh_lease_expires_at"] is not None


async def test_a_refresh_cannot_claim_a_connection_that_was_disconnected(
    store: Storage,
) -> None:
    """A refresh in flight must not resurrect what its owner removed."""
    connection = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="rt")
    await store.mcp.disconnect_mcp_connection(connection_id=connection["id"], user_id=_OWNER)
    refreshed = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert refreshed is not None
    assert (
        await store.mcp.claim_mcp_refresh(
            connection_id=connection["id"],
            expected_credential_version=int(refreshed["credential_version"]),
            lease_seconds=60,
        )
        is None
    )


async def test_recording_a_client_registration_moves_the_oauth_generation(
    store: Storage,
) -> None:
    """A flow started by the previous client must not complete the new one.

    The target is unchanged, so neither the config revision nor the
    authorization epoch moves: a client id says who is asking, not what is being
    reached.
    """
    connection = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="rt")
    assert await store.mcp.set_mcp_client_registration(
        connection_id=connection["id"],
        user_id=_OWNER,
        client_id="our-client",
        client_secret_ciphertext="wrapped-secret",
    )
    updated = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert updated is not None
    assert updated["auth_config"]["client_id"] == "our-client"
    assert updated["client_secret_ciphertext"] == "wrapped-secret"
    assert updated["oauth_generation"] == connection["oauth_generation"] + 1
    assert updated["config_revision"] == connection["config_revision"]
    assert updated["authorization_epoch"] == connection["authorization_epoch"]


async def test_a_client_identity_written_with_a_credential_reaches_the_refresh(
    store: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One field, written by the authorization and read by the renewal.

    The API test asserts the callback passes the identity it authorized with;
    what only Postgres can show is that the fenced write keeps it *and* that the
    refresh path reads it back -- two statements the route tests model rather
    than execute, which is how a dynamically registered client ended up with a
    token and no client id.
    """
    key = "mcp-store-client-identity-key"
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", key)
    connection = await _connect(store, _OWNER, auth_kind="oauth", access_token=None)
    assert await store.mcp.set_mcp_credential(
        connection_id=connection["id"],
        expected_authorization_epoch=connection["authorization_epoch"],
        access_token_ciphertext=mcp_cipher(key).encrypt("dynamic-access"),
        refresh_token_ciphertext=mcp_cipher(key).encrypt("dynamic-refresh"),
        client_secret_ciphertext=oauth_cipher(key).encrypt("dynamic-secret"),
        client_auth_method="client_secret_basic",
        registration="dcr",
        issuer="https://issuer.example.com",
        resource=_URL,
        token_endpoint="https://issuer.example.com/token",
        auth_config_patch={"client_id": "review-dynamic-client"},
    )
    stored = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert stored is not None
    assert stored["auth_config"]["client_id"] == "review-dynamic-client"

    request = refresh._build_request(stored, cipher=mcp_cipher(key))
    assert request.client.client_id == "review-dynamic-client"
    assert request.client.client_secret == "dynamic-secret"
    assert request.client.auth_method == "client_secret_basic"


async def test_expiring_held_calls_leaves_the_settled_ones_alone(
    store: Storage,
) -> None:
    """A reaper that rewrote a completed call would erase what happened."""
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    overdue = await _invocation(store, job, connection, expires_in=timedelta(seconds=-1))
    pending = await _invocation(store, job, connection)

    assert await store.mcp.expire_mcp_invocations() == 1

    settled = await store.mcp.get_mcp_invocation(invocation_id=int(overdue["id"]))
    assert settled is not None
    assert settled["status"] == "expired"
    still_open = await store.mcp.get_mcp_invocation(invocation_id=int(pending["id"]))
    assert still_open is not None
    assert still_open["status"] == "pending"


async def test_an_expired_call_no_longer_keeps_its_arguments(store: Storage) -> None:
    """Arguments are held for a decision; once none can be made, they are dropped.

    Every expiry path shares one assignment, so this walks four of them: the
    waiting client leaving, a late decision, the deadline sweep, and the owner
    turning the connection off. A call that was decided keeps its arguments as
    the record of what was refused.
    """
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    paused = await _connect(store, _OWNER, label="Second GitHub")
    abandoned = await _invocation(store, job, connection, arguments_ciphertext="sealed-1")
    late = await _invocation(
        store, job, connection, expires_in=timedelta(seconds=-1), arguments_ciphertext="sealed-2"
    )
    overdue = await _invocation(
        store, job, connection, expires_in=timedelta(seconds=-1), arguments_ciphertext="sealed-3"
    )
    switched_off = await _invocation(store, job, paused, arguments_ciphertext="sealed-4")
    denied = await _invocation(store, job, connection, arguments_ciphertext="sealed-5")
    assert await store.mcp.decide_mcp_invocation(
        invocation_id=denied["id"], approve=False, decided_by=_OWNER
    )

    assert await store.mcp.expire_mcp_invocation(invocation_id=abandoned["id"])
    assert (
        await store.mcp.decide_mcp_invocation(
            invocation_id=late["id"], approve=True, decided_by=_OWNER
        )
        is None
    )
    assert await store.mcp.expire_mcp_invocations() == 1
    assert await store.mcp.set_mcp_connection_enabled(
        connection_id=int(paused["id"]), user_id=_OWNER, enabled=False
    )

    for invocation in (abandoned, late, overdue, switched_off):
        stored = await store.mcp.get_mcp_invocation(invocation_id=int(invocation["id"]))
        assert stored is not None
        assert (stored["status"], stored["arguments_ciphertext"]) == ("expired", None)
    kept = await store.mcp.get_mcp_invocation(invocation_id=int(denied["id"]))
    assert kept is not None
    assert (kept["status"], kept["arguments_ciphertext"]) == ("denied", "sealed-5")


async def test_listing_calls_filters_by_job_and_state(store: Storage) -> None:
    """The approvals page asks for what is waiting; the detail view asks for one
    job's history, and neither should have to filter in Python."""
    job = await _create_job(store)
    other = await _create_job(store)
    connection = await _connect(store, _OWNER)
    mine = await _invocation(store, job, connection)
    await _invocation(store, other, connection)

    by_job = await store.mcp.list_mcp_invocations(job_id=str(job["id"]))
    assert [row["id"] for row in by_job] == [mine["id"]]
    waiting = await store.mcp.list_mcp_invocations(job_id=str(job["id"]), statuses=["pending"])
    assert [row["id"] for row in waiting] == [mine["id"]]
    assert await store.mcp.list_mcp_invocations(job_id=str(job["id"]), statuses=["approved"]) == []


async def test_owner_pending_inbox_excludes_foreign_and_elapsed_calls_without_writes(
    store: Storage,
) -> None:
    """The global inbox remains owner-scoped even before the expiry sweep."""
    mine = await _create_job(store)
    other = await _create_job(store, user_id=_OTHER)
    connection = await _connect(store, _OWNER)
    foreign_connection = await _connect(store, _OTHER)
    pending = await _invocation(store, mine, connection)
    elapsed = await _invocation(store, mine, connection, expires_in=timedelta(seconds=-1))
    await _invocation(store, other, foreign_connection)

    thread = await store.conversation_queries.get_thread(str(mine["thread_id"]))
    assert thread is not None and thread["title"] == "fix the flaky test"
    inbox = await store.mcp.list_mcp_invocations(user_id=_OWNER, statuses=["pending"])
    assert [row["id"] for row in inbox] == [pending["id"]]
    assert (
        await store.mcp.list_mcp_invocations(
            user_id=_OTHER, job_id=str(mine["id"]), statuses=["pending"]
        )
        == []
    )
    assert (
        await store.mcp.list_mcp_invocations(
            user_id=_OTHER, thread_id=str(mine["thread_id"]), statuses=["pending"]
        )
        == []
    )
    stored = await store.mcp.get_mcp_invocation(invocation_id=int(elapsed["id"]))
    assert stored is not None and stored["status"] == "pending"
    history = await store.mcp.list_mcp_invocations(user_id=_OWNER, job_id=str(mine["id"]))
    assert [row["id"] for row in history] == [elapsed["id"], pending["id"]]


async def test_invocation_keyset_continuation_is_stable_under_insertions(store: Storage) -> None:
    """New calls cannot shift the owner's continuation or introduce duplicates."""
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    calls = [await _invocation(store, job, connection) for _ in range(4)]
    first = await store.mcp.list_mcp_invocations(user_id=_OWNER, statuses=["pending"], limit=2)
    assert [row["id"] for row in first] == [calls[3]["id"], calls[2]["id"]]
    newest = await _invocation(store, job, connection)
    second = await store.mcp.list_mcp_invocations(
        user_id=_OWNER, statuses=["pending"], limit=2, before_id=int(first[-1]["id"])
    )
    assert [row["id"] for row in second] == [calls[1]["id"], calls[0]["id"]]
    assert newest["id"] not in [row["id"] for row in first + second]


async def test_deleted_conversation_invocations_are_hidden_from_owners_but_retained_for_audit(
    store: Storage, pool: asyncpg.Pool
) -> None:
    """Soft deletion hides inbox and history rows without destroying the ledger."""
    visible = await _create_job(store)
    deleted = await _create_job(store)
    connection = await _connect(store, _OWNER)
    visible_call = await _invocation(store, visible, connection)
    deleted_pending = await _invocation(store, deleted, connection)
    deleted_completed = await _invocation(store, deleted, connection)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_mcp_invocations SET status = 'completed' WHERE id = $1",
            int(deleted_completed["id"]),
        )
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", str(deleted["thread_id"])
        )

    for statuses in (None, ["pending"]):
        owned = await store.mcp.list_mcp_invocations(user_id=_OWNER, statuses=statuses)
        assert [row["id"] for row in owned] == [visible_call["id"]]
        assert (
            await store.mcp.list_mcp_invocations(
                user_id=_OWNER, job_id=str(deleted["id"]), statuses=statuses
            )
            == []
        )
        assert (
            await store.mcp.list_mcp_invocations(
                user_id=_OWNER, thread_id=str(deleted["thread_id"]), statuses=statuses
            )
            == []
        )
    audit = await store.mcp.list_mcp_invocations(job_id=str(deleted["id"]))
    assert [row["id"] for row in audit] == [deleted_completed["id"], deleted_pending["id"]]
    retained = await store.mcp.get_mcp_invocation(invocation_id=int(deleted_pending["id"]))
    assert retained is not None and retained["status"] == "pending"


async def test_capabilities_cannot_be_saved_for_a_disconnected_connection(
    store: Storage,
) -> None:
    """Discovery that finishes after a disconnect writes nothing.

    Returning zero rather than raising is deliberate: the caller is a diagnostic
    that has already dialled, and the honest answer is "that connection is gone",
    which the row's own status now says.
    """
    connection = await _connect(store, _OWNER)
    await store.mcp.disconnect_mcp_connection(connection_id=connection["id"], user_id=_OWNER)
    revision = await store.mcp.save_mcp_capabilities(
        connection_id=connection["id"],
        config_revision=1,
        protocol_version="2025-11-25",
        server_name="github",
        server_version="1",
        tools=[],
        resources=[],
        resource_templates=[],
        prompts=[],
        support={},
    )
    assert revision == 0


async def test_a_transport_session_records_who_owns_it(store: Storage) -> None:
    """The row names the versions a session was opened under.

    That is what lets a superseded owner's session be found from another
    instance -- the socket cannot be handed back by a table, but the fact that
    somebody still holds one can be.
    """
    connection = await _connect(store, _OWNER)
    await store.mcp.open_mcp_transport_session(
        handle="mcp1.abc",
        connection_id=int(connection["id"]),
        member="ajob_1:5",
        job_id="ajob_1",
        attempt_id=5,
        owner_lease_generation=2,
        protocol_version="2025-11-25",
        upstream_session_id="upstream-1",
        config_revision=int(connection["config_revision"]),
        authorization_epoch=int(connection["authorization_epoch"]),
    )
    rows = await store.mcp.list_mcp_transport_sessions(connection_id=int(connection["id"]))
    assert len(rows) == 1
    assert rows[0]["handle"] == "mcp1.abc"
    assert rows[0]["member"] == "ajob_1:5"
    assert rows[0]["owner_lease_generation"] == 2
    assert rows[0]["protocol_version"] == "2025-11-25"
    # Re-opening the same handle updates rather than duplicating: one session,
    # one record.
    await store.mcp.open_mcp_transport_session(
        handle="mcp1.abc",
        connection_id=int(connection["id"]),
        member="ajob_1:5",
        job_id="ajob_1",
        attempt_id=5,
        owner_lease_generation=3,
        protocol_version="2025-11-25",
        upstream_session_id="upstream-2",
        config_revision=int(connection["config_revision"]),
        authorization_epoch=int(connection["authorization_epoch"]),
    )
    rows = await store.mcp.list_mcp_transport_sessions(connection_id=int(connection["id"]))
    assert len(rows) == 1
    assert rows[0]["owner_lease_generation"] == 3
    assert await store.mcp.close_mcp_transport_session(handle="mcp1.abc")
    assert not await store.mcp.close_mcp_transport_session(handle="mcp1.abc")


async def test_reaping_a_member_keeps_exactly_the_new_session(store: Storage) -> None:
    """A takeover removes the old session and leaves the new one alone.

    Scoped by connection *and* member, so reaping one job's session cannot reach
    another job's session on the same server -- which would be the platform
    closing a connection somebody else is still using.
    """
    connection = await _connect(store, _OWNER)
    other = await _connect(store, _OWNER, label="DeepWiki", template="deepwiki")

    async def open_on(target: int, handle: str, member: str) -> None:
        await store.mcp.open_mcp_transport_session(
            handle=handle,
            connection_id=target,
            member=member,
            job_id=member.split(":")[0],
            attempt_id=int(member.split(":")[1]),
            owner_lease_generation=1,
            protocol_version="2025-11-25",
            upstream_session_id=None,
            config_revision=1,
            authorization_epoch=1,
        )

    await open_on(int(connection["id"]), "mcp1.old", "ajob_1:5")
    await open_on(int(connection["id"]), "mcp1.new", "ajob_1:5")
    await open_on(int(connection["id"]), "mcp1.other-job", "ajob_2:9")
    await open_on(int(other["id"]), "mcp1.other-conn", "ajob_1:5")

    reaped = await store.mcp.reap_mcp_transport_sessions(
        connection_id=int(connection["id"]), member="ajob_1:5", keep_handle="mcp1.new"
    )
    assert [row["handle"] for row in reaped] == ["mcp1.old"]
    remaining = {
        row["handle"]
        for row in await store.mcp.list_mcp_transport_sessions(connection_id=int(connection["id"]))
    }
    assert remaining == {"mcp1.new", "mcp1.other-job"}
    # The other connection's row for the same member is untouched.
    assert [
        row["handle"]
        for row in await store.mcp.list_mcp_transport_sessions(connection_id=int(other["id"]))
    ] == ["mcp1.other-conn"]


async def test_touching_a_session_keeps_a_live_one_visible(store: Storage) -> None:
    """``last_used_at`` is what tells a diagnosis which sessions are live."""
    connection = await _connect(store, _OWNER)
    await store.mcp.open_mcp_transport_session(
        handle="mcp1.abc",
        connection_id=int(connection["id"]),
        member="ajob_1:5",
        job_id="ajob_1",
        attempt_id=5,
        owner_lease_generation=1,
        protocol_version="2025-11-25",
        upstream_session_id=None,
        config_revision=1,
        authorization_epoch=1,
    )
    before = (await store.mcp.list_mcp_transport_sessions(connection_id=int(connection["id"])))[0]
    assert await store.mcp.touch_mcp_transport_session(handle="mcp1.abc")
    after = (await store.mcp.list_mcp_transport_sessions(connection_id=int(connection["id"])))[0]
    assert after["last_used_at"] >= before["last_used_at"]
    assert not await store.mcp.touch_mcp_transport_session(handle="mcp1.gone")


async def test_a_per_service_timeout_is_stored_without_moving_the_versions(
    store: Storage,
) -> None:
    """Patience is not a target change, so nothing a decision is fenced on moves.

    The design asks for a bounded timeout per service rather than one constant
    for all of them -- a repository search and a single-record fetch are not the
    same wait -- and lists the security-relevant fields as the command, the
    mount, the connector and the auth method. A timeout is none of those, so
    raising it must not invalidate the sessions a user already approved.
    """
    connection = await store.mcp.create_mcp_connection(
        user_id=_OWNER,
        label="Slow search",
        transport="streamable-http",
        location="remote",
        url=_URL,
        launch_spec=None,
        auth_kind="none",
        auth_config={},
        request_timeout_s=45.0,
    )
    assert connection["request_timeout_s"] == 45.0

    updated = await store.mcp.update_mcp_connection(
        connection_id=connection["id"],
        user_id=_OWNER,
        expected_config_revision=int(connection["config_revision"]),
        request_timeout_s=300.0,
    )
    assert updated is not None
    assert updated["request_timeout_s"] == 300.0
    assert updated["config_revision"] == connection["config_revision"]
    assert updated["authorization_epoch"] == connection["authorization_epoch"]

    # And an edit that does not mention it leaves it alone, like every other
    # COALESCE field.
    relabelled = await store.mcp.update_mcp_connection(
        connection_id=connection["id"],
        user_id=_OWNER,
        expected_config_revision=int(updated["config_revision"]),
        label="Renamed",
    )
    assert relabelled is not None
    assert relabelled["request_timeout_s"] == 300.0

    # A connection that never set one has none, which is what every existing
    # row got from the migration.
    plain = await _connect(store, _OWNER)
    assert plain["request_timeout_s"] is None


async def test_the_0007_migration_maps_legacy_server_names_onto_bindings() -> None:
    """Build the pre-0007 shape, upgrade over it, and read what it decided.

    A legacy entry maps only when the thread's owner has exactly one connection
    for that provider. Anything else is quarantined rather than guessed at: a
    connection created later must not inherit a session that was authorized
    against a different one. The mapped binding is created ``selection_pending``
    so every call is confirmed once instead of inheriting an approval nobody
    gave.
    """
    config = _db_config()
    apply_migrations(config)
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    try:
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        downgrade_to(config, _PREVIOUS_REVISION)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_threads (id, user_id, repo, title, execution_profile, "
                "mcp_servers) VALUES ($1, $2, 'example-org/example-repo', 'Legacy', "
                "'trusted-v1', $3)",
                "athr_legacy",
                _OWNER,
                ["github", "sentry"],
            )
            await conn.execute(
                "INSERT INTO agent_threads (id, user_id, repo, title, execution_profile, "
                "mcp_servers) VALUES ($1, $2, 'example-org/example-repo', 'Other', "
                "'trusted-v1', $3)",
                "athr_other",
                _OTHER,
                ["github"],
            )
            owner_github = await conn.fetchval(
                "INSERT INTO agent_mcp_connections (user_id, server, account_label, "
                "access_token_ciphertext) VALUES ($1, 'github', 'octocat', 'ciphertext-mine') "
                "RETURNING id",
                _OWNER,
            )
            other_github = await conn.fetchval(
                "INSERT INTO agent_mcp_connections (user_id, server, account_label, "
                "access_token_ciphertext) VALUES ($1, 'github', 'someone-else', "
                "'ciphertext-theirs') RETURNING id",
                _OTHER,
            )
            # 'sentry' exists only for the other user: the legacy thread must not
            # reach across owners to find it, so its entry has to be quarantined
            # rather than silently dropped.
            await conn.execute(
                "INSERT INTO agent_mcp_connections (user_id, server, account_label, "
                "access_token_ciphertext) VALUES ($1, 'sentry', 'someone-else', "
                "'ciphertext-sentry')",
                _OTHER,
            )

        apply_migrations(config)

        async with pool.acquire() as conn:
            mapped = await conn.fetch(
                "SELECT connection_id, alias, enabled, selection_pending "
                "FROM agent_mcp_thread_bindings WHERE thread_id = 'athr_legacy'"
            )
            quarantined = await conn.fetch(
                "SELECT server, reason, user_id FROM agent_mcp_legacy_bindings "
                "WHERE thread_id = 'athr_legacy' ORDER BY server"
            )
            other_mapped = await conn.fetch(
                "SELECT connection_id, alias FROM agent_mcp_thread_bindings "
                "WHERE thread_id = 'athr_other'"
            )
            legacy_selection = await conn.fetchval(
                "SELECT mcp_servers FROM agent_threads WHERE id = 'athr_legacy'"
            )
            # Compatibility is a read shape, never another authorization path.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE agent_threads SET mcp_servers = ARRAY['github'] "
                    "WHERE id = 'athr_legacy'"
                )

        assert [(row["alias"], row["enabled"], row["selection_pending"]) for row in mapped] == [
            ("github", True, True)
        ]
        assert mapped[0]["connection_id"] == owner_github
        assert [(row["server"], row["reason"], row["user_id"]) for row in quarantined] == [
            ("sentry", "no_connection", _OWNER)
        ]
        # The same provider name in another conversation maps to *that* owner's
        # connection, which is the property a name-keyed migration gets wrong.
        assert [(row["connection_id"], row["alias"]) for row in other_mapped] == [
            (other_github, "github")
        ]
        assert legacy_selection == []
    finally:
        # Always leave the shared per-worker database at head and empty. An
        # interrupted downgrade can leave the predecessor's unique constraint
        # missing, and the upgrade drops it by name before anything else.
        async with pool.acquire() as conn:
            revision = await conn.fetchval("SELECT version_num FROM alembic_version")
            if revision == _PREVIOUS_REVISION:
                present = await conn.fetchval(
                    "SELECT count(*) FROM pg_constraint WHERE conname = "
                    "'uq_agent_mcp_connections_server'"
                )
                if not present:
                    await conn.execute(
                        "ALTER TABLE agent_mcp_connections ADD CONSTRAINT "
                        "uq_agent_mcp_connections_server UNIQUE (user_id, server)"
                    )
        apply_migrations(config)
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        await pool.close()


async def test_the_ambiguous_quarantine_branch_cannot_be_reached_from_the_old_schema() -> None:
    """The one shape ``0007`` quarantines as ambiguous cannot exist at ``0006``.

    The migration maps a legacy name only when exactly one connection matches,
    and quarantines it as ``ambiguous_connection`` otherwise. But duplicates for
    one owner and provider are what ``uq_agent_mcp_connections_server`` forbade,
    and the upgrade drops that constraint by name as its first statement -- so a
    database the upgrade can start on cannot contain them. The branch is
    defensive, and this pins why no test can drive it: the insert raises.
    """
    config = _db_config()
    apply_migrations(config)
    try:
        pool = await asyncpg.create_pool(**config, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
        return  # unreachable

    try:
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        downgrade_to(config, _PREVIOUS_REVISION)
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO agent_mcp_connections (user_id, server, account_label, "
                    "access_token_ciphertext) VALUES ($1, 'github', 'one', 'ciphertext-a'), "
                    "($1, 'github', 'two', 'ciphertext-b')",
                    _OWNER,
                )
    finally:
        apply_migrations(config)
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        await pool.close()


async def test_public_catalog_snapshot_uses_settings_and_database_generation_fences(pool):
    """A stale process cannot replace the current Registry's complete snapshot."""
    from cloud_agent.storage.repositories.mcp_catalog import PostgresMcpCatalogRepository

    old_url = "https://old-registry.example"
    new_url = "https://new-registry.example"
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_settings (id, revision, "values")
               VALUES (TRUE, 1, jsonb_build_object('mcp_registry_url', $1::text))""",
            old_url,
        )
    catalog = PostgresMcpCatalogRepository(pool)
    assert await catalog.load_mcp_catalog() is None
    stale_source = await catalog.begin_mcp_catalog_sync()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE agent_settings
               SET revision = revision + 1,
                   "values" = jsonb_set("values", '{mcp_registry_url}', to_jsonb($1::text))
               WHERE id = TRUE""",
            new_url,
        )
    older_scan = await catalog.begin_mcp_catalog_sync()
    newer_scan = await catalog.begin_mcp_catalog_sync()
    saved = await catalog.save_mcp_catalog([{"name": "com.example/new"}], newer_scan)
    assert saved.saved_at is not None
    rejected_older = await catalog.save_mcp_catalog([{"name": "com.example/older"}], older_scan)
    assert rejected_older.saved_at is None
    assert rejected_older.registry_url == new_url
    assert rejected_older.settings_revision == newer_scan.settings_revision
    rejected_stale = await catalog.save_mcp_catalog([{"name": "com.example/stale"}], stale_source)
    assert rejected_stale.saved_at is None
    assert rejected_stale.registry_url == new_url
    assert rejected_stale.settings_revision == newer_scan.settings_revision
    restarted = PostgresMcpCatalogRepository(pool)
    assert await restarted.load_mcp_catalog() == {
        "entries": [{"name": "com.example/new"}],
        "synced_at": saved.saved_at,
        "registry_url": new_url,
        "settings_revision": newer_scan.settings_revision,
        "generation": newer_scan.generation,
    }


async def test_servers_automatically_become_available_only_in_the_owners_conversations(store):
    from cloud_agent.mcp.authorization import decide, visible_capabilities
    from cloud_agent.mcp.models import Capability, CapabilityKind
    from cloud_agent.mcp.registry import binding_from_row, connection_from_row

    job = await _create_job(store)
    mine = await _connect(store, _OWNER)
    await _connect(store, _OTHER)
    gone = await _connect(store, _OWNER)
    await store.mcp.disconnect_mcp_connection(connection_id=gone["id"], user_id=_OWNER)
    rows = await store.mcp.list_mcp_bindings(thread_id=job["thread_id"])
    assert [row["connection_id"] for row in rows] == [mine["id"]]
    binding = binding_from_row(rows[0])
    tool = Capability(kind=CapabilityKind.TOOL, name="search", description="Search pages")
    assert binding.enabled and binding.selection_pending
    assert visible_capabilities(binding, [tool], CapabilityKind.TOOL) == [tool]
    decision = decide(
        connection=connection_from_row(mine),
        binding=binding,
        capability=tool,
        user_id=_OWNER,
        name="search",
        kind=CapabilityKind.TOOL,
    )
    assert decision.outcome == "confirm"
    assert await store.mcp.list_mcp_bindings(thread_id=job["thread_id"]) == rows


async def test_new_servers_join_existing_conversations_without_reenabling_opt_outs(store):
    job = await _create_job(store)
    first = await _connect(store, _OWNER)
    thread = job["thread_id"]
    initial = (await store.mcp.list_mcp_bindings(thread_id=thread))[0]
    await store.mcp.replace_mcp_bindings(
        thread_id=thread, bindings=[{**initial, "enabled": False}], merge=True
    )
    second = await _connect(store, _OWNER)
    results = await asyncio.gather(
        *(store.mcp.list_mcp_bindings(thread_id=thread) for _ in range(3))
    )
    for rows in results:
        assert len(rows) == 2
        assert len({row["alias"] for row in rows}) == 2
        by_id = {row["connection_id"]: row for row in rows}
        assert not by_id[first["id"]]["enabled"]
        assert by_id[second["id"]]["enabled"]
        assert by_id[second["id"]]["selection_pending"]
    other_job = await _create_job(store)
    assert all(
        row["enabled"]
        for row in await store.mcp.list_mcp_bindings(thread_id=other_job["thread_id"])
    )


async def test_pausing_server_keeps_credentials_and_diagnostics_cannot_reenable_it(store):
    from cloud_agent.mcp.registry import resolve_connection

    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    pending = await _invocation(store, job, connection)
    assert (
        await store.mcp.set_mcp_connection_enabled(
            connection_id=connection["id"], user_id=_OTHER, enabled=False
        )
        is None
    )
    paused = await store.mcp.set_mcp_connection_enabled(
        connection_id=connection["id"], user_id=_OWNER, enabled=False
    )
    assert paused["access_token_ciphertext"] == connection["access_token_ciphertext"]
    assert paused["authorization_epoch"] > connection["authorization_epoch"]
    assert (await store.mcp.get_mcp_invocation(invocation_id=pending["id"]))["status"] == "expired"
    await store.mcp.set_mcp_connection_status(connection_id=connection["id"], status="active")
    current = await store.mcp.get_mcp_connection(connection_id=connection["id"])
    assert not current["enabled"]
    assert not (await resolve_connection(store.mcp, row=current)).usable
    resumed = await store.mcp.set_mcp_connection_enabled(
        connection_id=connection["id"], user_id=_OWNER, enabled=True
    )
    assert resumed["enabled"]
    assert resumed["access_token_ciphertext"] == connection["access_token_ciphertext"]
