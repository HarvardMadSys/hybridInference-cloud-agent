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
- **A flow is consumed exactly once, and an approved call is claimed exactly
  once.** Both are single ``UPDATE ... WHERE`` statements, which is the whole
  mutual-exclusion mechanism; a check-then-write passes every sequential test
  and loses these races.
- **A decision after the deadline does not revive a call.**
- **``0005`` maps the legacy provider-name list onto bindings**, and quarantines
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

from cloud_agent.storage.store import AgentJobStore
from tests.support.db import reset_agent_tables
from tests.support.migrations import apply_migrations, downgrade_to

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ALLOWED_TEST_DB_PATTERN = "_test_"
_OWNER = "user-owner"
_OTHER = "user-other"
_URL = "https://mcp.example.com/mcp"
_PREVIOUS_REVISION = "0004_residency_on_membership"


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
async def store():
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
        yield AgentJobStore(pool)
    finally:
        # These tests walk the schema backwards. Whatever happened, the shared
        # per-worker database is expected at head by every other test.
        apply_migrations(config)
        async with pool.acquire() as conn:
            await reset_agent_tables(conn)
        await pool.close()


async def _connect(
    store: AgentJobStore,
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
    row = await store.create_mcp_connection(
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
        written = await store.set_mcp_credential(
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
    stored = await store.get_mcp_connection(connection_id=row["id"])
    assert stored is not None
    return stored


async def _create_job(store: AgentJobStore, *, user_id: str = _OWNER) -> dict[str, Any]:
    """One queued job, so bindings and invocations have a real parent row."""
    return await store.create_job(
        user_id=user_id,
        repo="example-org/example-repo",
        task_prompt="fix the flaky test",
        runtime="claude-code",
        model="glm-5.1",
        base_sha="deadbeef",
    )


async def _invocation(
    store: AgentJobStore,
    job: dict[str, Any],
    connection: dict[str, Any],
    *,
    expires_in: timedelta = timedelta(minutes=10),
) -> dict[str, Any]:
    """One held call, pending and undecided."""
    return await store.create_mcp_invocation(
        job_id=str(job["id"]),
        attempt_id=None,
        lease_generation=0,
        thread_id=str(job["thread_id"]),
        connection_id=int(connection["id"]),
        capability_name="get_issue",
        arguments_digest="digest-1",
        arguments_ciphertext=None,
        config_revision=int(connection["config_revision"]),
        authorization_epoch=int(connection["authorization_epoch"]),
        capability_revision=int(connection["capability_revision"]),
        policy_revision=1,
        expires_at=datetime.now(timezone.utc) + expires_in,
    )


# -- owner scope -------------------------------------------------------


async def test_a_connection_is_only_ever_read_or_written_by_its_owner(
    store: AgentJobStore,
) -> None:
    """Two users, the same service: no query may blur them together.

    The relay is the one reader that looks a connection up by id alone, and it
    does so only after the fence named the owner; every owner-facing call has to
    carry the scope in its own SQL, which is what this checks.
    """
    mine = await _connect(store, _OWNER, label="Mine")
    theirs = await _connect(store, _OTHER, label="Theirs", access_token="ciphertext-theirs")

    assert [row["id"] for row in await store.list_mcp_connections(user_id=_OWNER)] == [mine["id"]]
    assert await store.list_mcp_connections(user_id="nobody") == []
    # By id alone, as the relay reads it.
    assert (await store.get_mcp_connection(connection_id=theirs["id"]))["user_id"] == _OTHER
    assert await store.get_mcp_connection(connection_id=theirs["id"], user_id=_OWNER) is None

    # A write without its owner scope would edit somebody else's connection.
    assert (
        await store.update_mcp_connection(
            connection_id=theirs["id"],
            user_id=_OWNER,
            expected_config_revision=1,
            label="Stolen",
        )
        is None
    )
    assert (await store.get_mcp_connection(connection_id=theirs["id"]))["label"] == "Theirs"

    assert (
        await store.disconnect_mcp_connection(connection_id=theirs["id"], user_id=_OWNER) is False
    )
    assert (await store.get_mcp_connection(connection_id=theirs["id"]))["deleted_at"] is None


async def test_the_same_service_twice_is_two_connections(store: AgentJobStore) -> None:
    """The constraint this revision removed: one account per provider.

    Two accounts on one service are two connections, distinguished by id. A
    label is display only and resolves nothing, which is why both rows survive
    an identical template.
    """
    work = await _connect(store, _OWNER, label="Work")
    personal = await _connect(store, _OWNER, label="Personal")

    assert work["id"] != personal["id"]
    rows = await store.list_mcp_connections(user_id=_OWNER)
    assert [row["label"] for row in rows] == ["Personal", "Work"]
    # Untested is the honest starting state for a connection nobody has used.
    assert {row["status"] for row in rows} == {"draft"}


async def test_editing_the_target_moves_the_versions_a_decision_is_fenced_on(
    store: AgentJobStore,
) -> None:
    """A different endpoint or credential is a different server to approve.

    The revision moves only for a security-relevant edit: renaming a connection
    must not make every session re-approve the tools it already has.
    """
    row = await _connect(store, _OWNER)
    edited = await store.update_mcp_connection(
        connection_id=row["id"],
        user_id=_OWNER,
        expected_config_revision=row["config_revision"],
        url="https://other.example.com/mcp",
    )
    assert edited is not None
    assert edited["config_revision"] == row["config_revision"] + 1
    assert edited["authorization_epoch"] == row["authorization_epoch"] + 1

    renamed = await store.update_mcp_connection(
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
        await store.update_mcp_connection(
            connection_id=row["id"],
            user_id=_OWNER,
            expected_config_revision=row["config_revision"],
            label="Raced",
        )
        is None
    )


# -- credentials and disconnects ---------------------------------------


async def test_disconnecting_clears_the_secret_and_bumps_the_epoch(
    store: AgentJobStore,
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

    assert await store.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True

    gone = await store.get_mcp_connection(connection_id=row["id"])
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

    assert await store.list_mcp_connections(user_id=_OWNER) == []
    assert len(await store.list_mcp_connections(user_id=_OWNER, include_deleted=True)) == 1
    # Disconnecting twice reports the second as a no-op, so a retry cannot bump
    # the epoch again and invalidate bindings that were re-approved meanwhile.
    assert await store.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is False
    assert (await store.get_mcp_connection(connection_id=row["id"]))["authorization_epoch"] == gone[
        "authorization_epoch"
    ]


async def test_a_credential_write_conditional_on_a_stale_epoch_loses(
    store: AgentJobStore,
) -> None:
    """A refresh that started before a disconnect must not restore the secret.

    This is the whole reason the write carries an epoch: a late OAuth callback
    or a slow refresh is indistinguishable from a legitimate one except by the
    version it read, and only one of them may win.
    """
    row = await _connect(store, _OWNER, auth_kind="oauth", refresh_token="ciphertext-refresh")
    stale_epoch = row["authorization_epoch"]

    assert await store.disconnect_mcp_connection(connection_id=row["id"], user_id=_OWNER) is True

    assert (
        await store.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=stale_epoch,
            access_token_ciphertext="ciphertext-restored",
        )
        is False
    )
    assert (await store.get_mcp_connection(connection_id=row["id"]))[
        "access_token_ciphertext"
    ] is None


async def test_a_manual_replacement_moves_the_epoch_and_fences_the_old_one(
    store: AgentJobStore,
) -> None:
    """Replacing a credential may change the account behind it.

    Every approval made against the old identity has to be re-made, which is
    what ``bump_epoch`` is for: a refresh keeps the epoch and moves only the
    credential version, while a manual replacement moves both.
    """
    row = await _connect(store, _OWNER, access_token="ciphertext-old")
    old_epoch = row["authorization_epoch"]

    assert (
        await store.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=old_epoch,
            access_token_ciphertext="ciphertext-new",
            bump_epoch=True,
        )
        is True
    )

    replaced = await store.get_mcp_connection(connection_id=row["id"])
    assert replaced is not None
    assert replaced["authorization_epoch"] == old_epoch + 1
    assert replaced["credential_version"] > row["credential_version"]
    assert (
        await store.set_mcp_credential(
            connection_id=row["id"],
            expected_authorization_epoch=old_epoch,
            access_token_ciphertext="ciphertext-stale",
        )
        is False
    )
    assert (await store.get_mcp_connection(connection_id=row["id"]))[
        "access_token_ciphertext"
    ] == "ciphertext-new"


# -- one winner --------------------------------------------------------


async def test_consuming_an_oauth_flow_has_exactly_one_winner(store: AgentJobStore) -> None:
    """Two callbacks racing on one state value: one proceeds, one gets nothing.

    The ``consumed_at IS NULL`` predicate is inside the ``UPDATE``, which is the
    whole mechanism. A read-then-write would let both exchanges run, and the
    second would overwrite the credential the first just stored.
    """
    row = await _connect(store, _OWNER, auth_kind="oauth", access_token=None)
    await store.create_mcp_oauth_flow(
        state_hash="state-hash",
        user_id=_OWNER,
        connection_id=row["id"],
        config_revision=row["config_revision"],
        authorization_epoch=row["authorization_epoch"],
        # Starting a flow moves the generation; the row records the new one.
        oauth_generation=row["oauth_generation"] + 1,
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

    first, second = await asyncio.gather(
        store.consume_mcp_oauth_flow(state_hash="state-hash"),
        store.consume_mcp_oauth_flow(state_hash="state-hash"),
    )

    winners = [consumed for consumed in (first, second) if consumed is not None]
    assert len(winners) == 1
    assert winners[0]["connection_id"] == row["id"]
    assert winners[0]["scopes"] == ["read"]
    assert await store.consume_mcp_oauth_flow(state_hash="state-hash") is None
    # And starting a flow again moves the generation the row was fenced to.
    assert (await store.get_mcp_connection(connection_id=row["id"]))["oauth_generation"] == row[
        "oauth_generation"
    ] + 1


async def test_claiming_an_approved_call_has_exactly_one_winner(store: AgentJobStore) -> None:
    """Two dispatchers arriving together: one sends, one must not.

    The call may already have produced an external side effect, so the loser
    reporting "somebody else has it" is the only safe answer -- a second send is
    how the same issue gets filed twice.
    """
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    invocation = await _invocation(store, job, connection)
    approved = await store.decide_mcp_invocation(
        invocation_id=invocation["id"], approve=True, decided_by=_OWNER
    )
    assert approved is not None and approved["status"] == "approved"

    first, second = await asyncio.gather(
        store.claim_mcp_invocation(invocation_id=invocation["id"]),
        store.claim_mcp_invocation(invocation_id=invocation["id"]),
    )

    winners = [claimed for claimed in (first, second) if claimed is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == "dispatching"
    assert winners[0]["dispatch_generation"] == 1
    assert await store.claim_mcp_invocation(invocation_id=invocation["id"]) is None


async def test_finishing_a_call_is_fenced_by_its_dispatch_generation(
    store: AgentJobStore,
) -> None:
    """A superseded worker cannot rewrite the outcome of a call it no longer owns."""
    job = await _create_job(store)
    connection = await _connect(store, _OWNER)
    invocation = await _invocation(store, job, connection)
    await store.decide_mcp_invocation(
        invocation_id=invocation["id"], approve=True, decided_by=_OWNER
    )
    claimed = await store.claim_mcp_invocation(invocation_id=invocation["id"])
    assert claimed is not None

    assert (
        await store.finish_mcp_invocation(
            invocation_id=invocation["id"],
            dispatch_generation=int(claimed["dispatch_generation"]) + 1,
            status="completed",
        )
        is False
    )
    assert (
        await store.finish_mcp_invocation(
            invocation_id=invocation["id"],
            dispatch_generation=int(claimed["dispatch_generation"]),
            status="outcome_unknown",
            result_summary="the response was lost",
        )
        is True
    )
    stored = await store.get_mcp_invocation(invocation_id=invocation["id"])
    assert stored is not None
    assert stored["status"] == "outcome_unknown"


async def test_a_decision_after_the_deadline_does_not_revive_a_call(
    store: AgentJobStore,
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
        await store.decide_mcp_invocation(
            invocation_id=invocation["id"], approve=True, decided_by=_OWNER
        )
        is None
    )

    stored = await store.get_mcp_invocation(invocation_id=invocation["id"])
    assert stored is not None
    assert stored["status"] == "expired"
    assert await store.claim_mcp_invocation(invocation_id=invocation["id"]) is None


# -- bindings and snapshots --------------------------------------------


async def test_replacing_a_threads_bindings_is_wholesale(store: AgentJobStore) -> None:
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

    await store.replace_mcp_bindings(
        thread_id=str(job["thread_id"]),
        bindings=[binding(github, "github"), binding(wiki, "deepwiki")],
    )
    listed = await store.list_mcp_bindings(thread_id=str(job["thread_id"]))
    assert [row["alias"] for row in listed] == ["deepwiki", "github"]
    by_alias = {row["alias"]: row for row in listed}
    assert by_alias["github"]["rules"]["get_issue"] == {"policy": "allow", "fingerprint": "abc"}
    assert by_alias["github"]["resource_rules"] == {"docs": "confirm"}
    # The join carries display data, so a page needs no second query.
    assert by_alias["deepwiki"]["label"] == "DeepWiki"
    assert by_alias["deepwiki"]["connection_status"] == "draft"

    await store.replace_mcp_bindings(
        thread_id=str(job["thread_id"]), bindings=[binding(wiki, "deepwiki")]
    )
    remaining = await store.list_mcp_bindings(thread_id=str(job["thread_id"]))
    assert [row["alias"] for row in remaining] == ["deepwiki"]


async def test_saving_capabilities_moves_the_revision_it_is_fenced_against(
    store: AgentJobStore,
) -> None:
    """Discovery and its revision are written in one transaction.

    A binding's fence compares the connection's capability revision, so a
    snapshot stored without moving it would be a catalogue that appears without
    the approvals against the old one being invalidated.
    """
    row = await _connect(store, _OWNER)
    assert await store.get_mcp_capabilities(connection_id=row["id"]) is None

    revision = await store.save_mcp_capabilities(
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
    moved = await store.get_mcp_connection(connection_id=row["id"])
    assert moved is not None and moved["capability_revision"] == revision
    stored = await store.get_mcp_capabilities(connection_id=row["id"])
    assert stored is not None
    assert stored["capability_revision"] == revision
    assert [tool["name"] for tool in stored["tools"]] == ["get_issue"]

    # A second discovery replaces the snapshot and moves the revision again,
    # so an approval of the first catalogue cannot survive it.
    second = await store.save_mcp_capabilities(
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
    replaced = await store.get_mcp_capabilities(connection_id=row["id"])
    assert replaced is not None
    assert [tool["name"] for tool in replaced["tools"]] == ["list_issues"]


# -- the 0005 migration ------------------------------------------------


async def test_the_0005_migration_maps_legacy_server_names_onto_bindings() -> None:
    """Build the pre-0005 shape, upgrade over it, and read what it decided.

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
            still_there = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'agent_threads' AND column_name = 'mcp_servers'"
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
        assert still_there == 0
    finally:
        # Always leave the shared per-worker database at head and empty. An
        # interrupted downgrade can leave the predecessor's unique constraint
        # missing, and the upgrade drops it by name before anything else.
        async with pool.acquire() as conn:
            at_previous_revision = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'agent_mcp_connections' AND column_name = 'server'"
            )
            if at_previous_revision:
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
    """The one shape ``0005`` quarantines as ambiguous cannot exist at ``0004``.

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
