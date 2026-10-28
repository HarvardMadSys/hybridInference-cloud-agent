"""Real PostgreSQL behaviour of owners' model connections and the turns that name them."""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
import asyncio

import pytest
from tests.integration.storage.test_agent_job_store import (  # noqa: F401
    _SHARED_HOST_CAPABILITIES,
    _claim,
    _create_job,
    _reserve_shared_chat,
    _shared_claim_attestation,
    pool,
    store,
)
from tests.support.follow_ups import append_follow_up

from cloud_agent.application.model_connections import (
    MAX_SCAN_KEYS,
    ScanMaterialOverflow,
    bind_residency_access,
    connection_access,
    known_connection_secrets,
    model_connection_cipher,
    new_connection_id,
)
from cloud_agent.domain.model_connections import ModelConnectionConflict
from cloud_agent_shared.model_connections import ProtocolPlan

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def _connection(store, *, owner="user-1", name="Mine", models=("gpt-4.1",)) -> dict:
    return await store.model_connections.create_model_connection(
        user_id=owner,
        values={
            "id": new_connection_id(),
            "name": name,
            "base_url": "https://models.example/v1",
            "protocols": ["chat_completions", "responses"],
            "models": list(models),
            "api_key_ciphertext": "ciphertext-1",
        },
    )


async def test_connections_are_private_to_their_owner_and_names_are_unique_per_owner(store):
    mine = await _connection(store)
    with pytest.raises(ModelConnectionConflict):
        await _connection(store, name="mine")
    theirs = await _connection(store, owner="user-2")

    assert [
        row["id"] for row in await store.model_connections.list_model_connections(user_id="user-1")
    ] == [mine["id"]]
    assert (
        await store.model_connections.get_model_connection(
            user_id="user-2", connection_id=mine["id"]
        )
        is None
    )
    assert (
        await store.model_connections.update_model_connection(
            user_id="user-2", connection_id=mine["id"], changes={"enabled": False}
        )
        is None
    )
    assert (
        await store.model_connections.delete_model_connection(
            user_id="user-2", connection_id=mine["id"]
        )
        is None
    )
    assert theirs["user_id"] == "user-2"
    assert mine["credential_revision"] == 1 and mine["enabled"] is True


async def test_credential_changes_and_disabling_raise_the_revision_and_clear_the_check(store):
    row = await _connection(store)
    assert await store.model_connections.record_model_connection_check(
        user_id="user-1", connection_id=row["id"], config_revision=1, result={"status": "supported"}
    )

    renamed = await store.model_connections.update_model_connection(
        user_id="user-1", connection_id=row["id"], changes={"name": "Renamed"}
    )
    assert renamed["credential_revision"] == 1 and renamed["config_revision"] == 1
    assert renamed["last_check"] == {"status": "supported"}

    # A result is about the models it was checked with, too.
    remodelled = await store.model_connections.update_model_connection(
        user_id="user-1", connection_id=row["id"], changes={"models": ["gpt-4.1", "o4-mini"]}
    )
    assert remodelled["credential_revision"] == 1 and remodelled["config_revision"] == 2
    assert remodelled["last_check"] is None
    assert remodelled["models"] == ["gpt-4.1", "o4-mini"]

    rotated = await store.model_connections.update_model_connection(
        user_id="user-1", connection_id=row["id"], changes={"api_key_ciphertext": "ciphertext-2"}
    )
    assert rotated["credential_revision"] == 2
    assert rotated["last_check"] is None

    disabled = await store.model_connections.update_model_connection(
        user_id="user-1", connection_id=row["id"], changes={"enabled": False}
    )
    enabled = await store.model_connections.update_model_connection(
        user_id="user-1", connection_id=row["id"], changes={"enabled": True}
    )
    assert disabled["credential_revision"] == 3
    assert enabled["credential_revision"] == 3 and enabled["enabled"] is True


async def test_a_deleted_connection_keeps_a_keyless_tombstone_and_frees_its_name(store):
    row = await _connection(store)
    deleted = await store.model_connections.delete_model_connection(
        user_id="user-1", connection_id=row["id"]
    )

    assert deleted["api_key_ciphertext"] is None
    assert deleted["enabled"] is False and deleted["deleted_at"] is not None
    assert deleted["credential_revision"] == 2
    assert await store.model_connections.list_model_connections(user_id="user-1") == []
    tombstone = await store.model_connections.get_model_connection(
        user_id="user-1", connection_id=row["id"]
    )
    assert tombstone["deleted_at"] is not None, "a turn naming it reads as deleted"
    assert (
        await store.model_connections.update_model_connection(
            user_id="user-1", connection_id=row["id"], changes={"enabled": True}
        )
        is None
    ), "a tombstone is never resurrected"
    assert (await _connection(store))["id"] != row["id"]


async def test_turns_carry_their_source_through_follow_ups(pool, store):
    connection = await _connection(store)
    first = await _create_job(
        store,
        runtime="opencode",
        model="gpt-4.1",
        model_connection_id=connection["id"],
    )
    assert first["model_connection_id"] == connection["id"]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'cancelled' WHERE id = $1", first["id"])

    inherited = await append_follow_up(
        pool, parent_job_id=first["id"], user_id="user-1", prompt="go on"
    )
    assert inherited["model"] == "gpt-4.1"
    assert inherited["model_connection_id"] == connection["id"]

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_jobs SET state = 'cancelled' WHERE id = $1", inherited["id"]
        )
    # The same model id named without a source is the Gateway's, not a reuse.
    gateway = await append_follow_up(
        pool,
        parent_job_id=inherited["id"],
        user_id="user-1",
        prompt="on the gateway",
        model="gpt-4.1",
        reasoning_effort="high",
    )
    assert gateway["model_connection_id"] is None
    assert gateway["reasoning_effort"] == "high"

    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_jobs SET state = 'cancelled' WHERE id = $1", gateway["id"])
    # Back onto the connection: the Gateway turn's effort does not come along.
    back = await append_follow_up(
        pool,
        parent_job_id=gateway["id"],
        user_id="user-1",
        prompt="back",
        model="gpt-4.1",
        model_connection_id=connection["id"],
    )
    assert back["model_connection_id"] == connection["id"]
    assert back["reasoning_effort"] is None

    preference = await store.model_preferences.get_model_preference("user-1")
    assert preference.last_model == "gpt-4.1"
    assert preference.last_model_connection_id == connection["id"]


async def test_a_waiting_message_keeps_the_source_sent_with_its_model(store):
    connection = await _connection(store)
    job, _claim, _sandbox = await _reserve_shared_chat(store)
    submitted = await store.pending_messages.submit_message(
        thread_id=job["thread_id"],
        user_id="user-1",
        prompt="later",
        skill_ids=None,
        attachment_ids=None,
        model="gpt-4.1",
        reasoning_effort=None,
        model_connection_id=connection["id"],
    )
    message = submitted["message"]
    assert message["model_connection_id"] == connection["id"]

    edited = await store.pending_messages.edit_message(
        thread_id=job["thread_id"],
        user_id="user-1",
        message_id=message["id"],
        revision=message["revision"],
        changes={"model": "gpt-4.1", "model_connection_id": None},
    )
    assert edited["message"]["model_connection_id"] is None

    unsourced = await store.pending_messages.submit_message(
        thread_id=job["thread_id"],
        user_id="user-1",
        prompt="inherit",
        skill_ids=None,
        attachment_ids=None,
        model=None,
        reasoning_effort=None,
        model_connection_id=connection["id"],
    )
    assert unsourced["message"]["model_connection_id"] is None, "no model, no source"


async def _membership(pool, thread_id: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT m.residency_status, m.residency_generation, "
            "b.connection_id AS residency_model_connection_id, "
            "b.credential_revision AS residency_model_connection_revision "
            "FROM agent_thread_sandbox_memberships m "
            "LEFT JOIN agent_model_connection_residencies b "
            "ON b.thread_id = m.thread_id AND b.residency_generation = m.residency_generation "
            "WHERE m.thread_id = $1",
            thread_id,
        )
    return dict(row)


async def test_a_residency_binds_only_to_its_owners_current_connection(pool, store):
    connection = await _connection(store)
    job, claim, sandbox = await _reserve_shared_chat(store)
    bind = {
        "connection_id": connection["id"],
        "thread_id": job["thread_id"],
        "generation": sandbox["residency_generation"],
        "scan_key_ciphertext": "scan-1",
    }

    assert not await store.model_connections.bind_residency(user_id="user-2", revision=1, **bind)
    assert not await store.model_connections.bind_residency(user_id="user-1", revision=2, **bind)
    assert await store.model_connections.bind_residency(user_id="user-1", revision=1, **bind)
    member = await _membership(pool, job["thread_id"])
    assert member["residency_model_connection_id"] == connection["id"]
    assert member["residency_model_connection_revision"] == 1

    # A current binding is not stale: nothing is released.
    assert (
        await store.model_connections.release_stale_residencies(
            user_id="user-1", connection_id=connection["id"]
        )
        == []
    )

    # A fresh generation starts unbound: the binding names one process.
    rotated = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        sandbox_id=sandbox["sandbox_id"],
        runtime_id="claude-code",
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
    )
    member = await _membership(pool, job["thread_id"])
    assert member["residency_generation"] == rotated["generation"]
    assert member["residency_model_connection_id"] is None
    assert rotated["model_connection_id"] is None and rotated["runtime"] == "claude-code"

    # A key rotation must not stop a newer Gateway process merely because an
    # older generation of this member used the key.
    await store.model_connections.update_model_connection(
        user_id="user-1",
        connection_id=connection["id"],
        changes={"api_key_ciphertext": "ciphertext-2"},
    )
    assert (
        await store.model_connections.release_stale_residencies(
            user_id="user-1", connection_id=connection["id"]
        )
        == []
    )
    assert (await _membership(pool, job["thread_id"]))["residency_status"] == "hot"


@pytest.mark.parametrize("change", ["rotate", "disable", "delete"])
async def test_a_change_to_the_connection_releases_the_residencies_started_before_it(
    pool, store, change
):
    connection = await _connection(store)
    job, _claim, sandbox = await _reserve_shared_chat(store)
    assert await store.model_connections.bind_residency(
        user_id="user-1",
        connection_id=connection["id"],
        revision=1,
        thread_id=job["thread_id"],
        generation=sandbox["residency_generation"],
        scan_key_ciphertext="scan-1",
    )
    if change == "rotate":
        await store.model_connections.update_model_connection(
            user_id="user-1",
            connection_id=connection["id"],
            changes={"api_key_ciphertext": "ciphertext-2"},
        )
    elif change == "disable":
        await store.model_connections.update_model_connection(
            user_id="user-1", connection_id=connection["id"], changes={"enabled": False}
        )
    else:
        await store.model_connections.delete_model_connection(
            user_id="user-1", connection_id=connection["id"]
        )

    # Another owner cannot release it, even naming the same connection id.
    assert (
        await store.model_connections.release_stale_residencies(
            user_id="user-2", connection_id=connection["id"]
        )
        == []
    )
    released = await store.model_connections.release_stale_residencies(
        user_id="user-1", connection_id=connection["id"]
    )

    assert released == [
        {
            "thread_id": job["thread_id"],
            "generation": sandbox["residency_generation"],
            "host": "shared-runner",
            "grant_id": "",
        }
    ]
    assert (await _membership(pool, job["thread_id"]))["residency_status"] == "none"
    assert not await store.model_connections.bind_residency(
        user_id="user-1",
        connection_id=connection["id"],
        revision=1,
        thread_id=job["thread_id"],
        generation=sandbox["residency_generation"],
        scan_key_ciphertext="scan-1",
    ), "a released residency cannot be bound again"


_CIPHER_SECRET = "model-connection-store-test-key-0123456789abcdef"


async def _keyed_connection(store, *, name: str, key: str) -> dict:
    row = await store.model_connections.create_model_connection(
        user_id="user-1",
        values={
            "id": new_connection_id(),
            "name": name,
            "base_url": "https://models.example/v1",
            "protocols": ["chat_completions"],
            "models": ["gpt-4.1"],
            "api_key_ciphertext": model_connection_cipher().encrypt(key),
        },
    )
    return row


async def _second_shared_chat(store, sandbox_id: str) -> tuple[dict, dict, dict]:
    """Another of user-1's chats, in a sandbox group of its own on the same host."""
    job = await _create_job(store, repo=None, base_sha="b" * 40, task_prompt=sandbox_id)
    claim = await _claim(
        store,
        worker_id=f"worker-{sandbox_id}",
        lease_ttl_seconds=60,
        host="shared-runner",
        host_credential_hash="test-credential-hash",
        claim_attestation=_shared_claim_attestation(),
    )
    assert claim is not None and claim["id"] == job["id"]
    sandbox = await store.sandbox.reserve_thread_sandbox(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        sandbox_id=sandbox_id,
        container_name=f"freeinference-{sandbox_id}",
        runtime_id="claude-code",
        runtime_version=claim.get("runtime_version"),
        sandbox_image_digest=claim.get("sandbox_image_digest"),
        execution_profile=job["execution_profile"],
    )
    residency = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        sandbox_id=sandbox_id,
        runtime_id="claude-code",
        runtime_version=sandbox["runtime_version"],
        ttl_seconds=1800,
    )
    assert residency is not None
    return job, claim, {**sandbox, "residency_generation": residency["generation"]}


async def _bind(store, row: dict, job: dict, generation: int) -> bool:
    current = await store.model_connections.get_model_connection(
        user_id="user-1", connection_id=row["id"]
    )
    return await bind_residency_access(
        store.model_connections,
        user_id="user-1",
        access=connection_access(
            current,
            ProtocolPlan("chat_completions", "chat_completions"),
            model_connection_cipher(),
            user_id="user-1",
            thread_id=job["thread_id"],
            model=job["model"],
        ),
        thread_id=job["thread_id"],
        generation=generation,
    )


async def test_a_groups_scan_holds_only_the_keys_its_own_members_were_handed(store, monkeypatch):
    """Two connections, two groups of one owner: neither scan sees the other's key."""
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _CIPHER_SECRET)
    first = await _keyed_connection(store, name="A", key="key-for-group-a-1111")
    second = await _keyed_connection(store, name="B", key="key-for-group-b-2222")
    unused = await _keyed_connection(store, name="Unused", key="key-never-used-3333")
    job_a, _claim_a, group_a = await _reserve_shared_chat(store)
    job_b, _claim_b, group_b = await _second_shared_chat(store, "asbx_second_group")

    assert await _bind(store, first, job_a, group_a["residency_generation"])
    assert await _bind(store, second, job_b, group_b["residency_generation"])

    in_a = await known_connection_secrets(store.model_connections, sandbox_id=group_a["sandbox_id"])
    in_b = await known_connection_secrets(store.model_connections, sandbox_id=group_b["sandbox_id"])
    assert set(in_a.values()) == {"key-for-group-a-1111"}
    assert set(in_b.values()) == {"key-for-group-b-2222"}
    assert "key-never-used-3333" not in {*in_a.values(), *in_b.values()}
    assert unused["id"] not in {first["id"], second["id"]}


@pytest.mark.parametrize("then", ["rotate", "delete"])
async def test_a_key_a_group_used_stays_in_its_scan_after_rotation_or_deletion(
    store, monkeypatch, then
):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _CIPHER_SECRET)
    row = await _keyed_connection(store, name="A", key="first-key-aaaa")
    job, claim, group = await _reserve_shared_chat(store)
    assert await _bind(store, row, job, group["residency_generation"])

    await store.model_connections.update_model_connection(
        user_id="user-1",
        connection_id=row["id"],
        changes={"api_key_ciphertext": model_connection_cipher().encrypt("second-key-bbbb")},
    )
    # The next process gets the new key, under a new residency generation.
    renewed = await store.residency.establish_residency(
        thread_id=job["thread_id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        host="shared-runner",
        sandbox_id=group["sandbox_id"],
        runtime_id="claude-code",
        runtime_version=group["runtime_version"],
        ttl_seconds=1800,
    )
    assert await _bind(store, row, job, renewed["generation"])
    if then == "delete":
        await store.model_connections.delete_model_connection(
            user_id="user-1", connection_id=row["id"]
        )

    scanned = await known_connection_secrets(
        store.model_connections, sandbox_id=group["sandbox_id"]
    )
    assert set(scanned.values()) == {"first-key-aaaa", "second-key-bbbb"}
    if then == "delete":
        # The scan copies never stand in for the execution key.
        tombstone = await store.model_connections.get_model_connection(
            user_id="user-1", connection_id=row["id"]
        )
        assert tombstone["api_key_ciphertext"] is None


async def test_a_group_handed_more_keys_than_a_scan_covers_is_not_scanned_partly(
    pool, store, monkeypatch
):
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", _CIPHER_SECRET)
    row = await _keyed_connection(store, name="A", key="key-a")
    _job, _attempt, group = await _reserve_shared_chat(store)
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO agent_model_connection_scan_keys "
            "(sandbox_id, connection_id, credential_revision, thread_id, key_ciphertext) "
            "VALUES ($1, $2, $3, 'athr_x', 'unreadable-on-purpose')",
            [(group["sandbox_id"], row["id"], revision) for revision in range(MAX_SCAN_KEYS + 1)],
        )
    with pytest.raises(ScanMaterialOverflow):
        await known_connection_secrets(store.model_connections, sandbox_id=group["sandbox_id"])


async def test_an_expired_residency_cannot_be_bound(pool, store):
    connection = await _connection(store)
    job, _attempt, sandbox = await _reserve_shared_chat(store)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_thread_sandbox_memberships "
            "SET residency_lease_expires_at = clock_timestamp() - INTERVAL '1 second' "
            "WHERE thread_id = $1",
            job["thread_id"],
        )
    assert not await store.model_connections.bind_residency(
        user_id="user-1",
        connection_id=connection["id"],
        revision=1,
        thread_id=job["thread_id"],
        generation=sandbox["residency_generation"],
        scan_key_ciphertext="scan-1",
    )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_model_connection_scan_keys") == 0


async def test_two_concurrent_reservations_cannot_both_take_the_last_check(pool, store):
    row = await _connection(store)
    reserve = {"user_id": "user-1", "connection_id": row["id"], "seconds": 3600}
    for _ in range(2):
        assert "connection" in await store.model_connections.reserve_model_connection_check(
            limit=3, **reserve
        )
    results = await asyncio.gather(
        *(
            store.model_connections.reserve_model_connection_check(limit=3, **reserve)
            for _ in range(2)
        )
    )
    assert sorted("connection" in result for result in results) == [False, True]
    assert {"error": "limited"} in results
    other = await _connection(store, owner="user-2")
    assert "connection" in await store.model_connections.reserve_model_connection_check(
        user_id="user-2", connection_id=other["id"], seconds=3600, limit=3
    ), "each owner's limit is their own"
    assert await store.model_connections.reserve_model_connection_check(
        user_id="user-2", connection_id=row["id"], seconds=3600, limit=3
    ) == {"error": "not_found"}


async def test_a_check_result_is_kept_only_for_the_revision_it_checked(store):
    row = await _connection(store)
    reserved = await store.model_connections.reserve_model_connection_check(
        user_id="user-1", connection_id=row["id"], seconds=3600, limit=20
    )
    checked = reserved["connection"]
    await store.model_connections.update_model_connection(
        user_id="user-1", connection_id=row["id"], changes={"base_url": "https://other.example"}
    )
    assert not await store.model_connections.record_model_connection_check(
        user_id="user-1",
        connection_id=row["id"],
        config_revision=checked["config_revision"],
        result={"status": "supported"},
    )
    current = await store.model_connections.get_model_connection(
        user_id="user-1", connection_id=row["id"]
    )
    assert current["last_check"] is None
    assert await store.model_connections.record_model_connection_check(
        user_id="user-1",
        connection_id=row["id"],
        config_revision=current["config_revision"],
        result={"status": "supported"},
    )
