"""Real PostgreSQL behaviour of owners' SSH servers and their per-chat selection."""

from __future__ import annotations

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
import uuid

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import pool, store  # noqa: F401
from tests.support.ssh_keys import host_key

from cloud_agent.domain.ssh_servers import SshServerConflict, SshServerUnavailable

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_HOST_KEY = host_key()


async def _server(store, *, owner="alice", name="gpu", ready=True) -> dict:
    row = await store.ssh_servers.create_ssh_server(
        user_id=owner,
        values={
            "id": str(uuid.uuid4()),
            "name": name,
            "description": "",
            "address": "gpu.example.org",
            "port": 22,
            "username": "ubuntu",
            "public_key": "ssh-ed25519 AAAA cloud-agent:gpu",
            "private_key_ciphertext": "ciphertext-current",
        },
    )
    if ready:
        row = await store.ssh_servers.update_ssh_server(
            user_id=owner,
            server_id=row["id"],
            revision=row["revision"],
            values={
                "status": "ready",
                "pinned_ipv4": "93.184.216.34",
                "host_key_type": "ssh-ed25519",
                "host_key": _HOST_KEY,
            },
            event="verified",
        )
    return row


async def _chat(store, *, owner="alice", ssh_server_ids=None) -> dict:
    return await store.conversation_commands.create_job(
        user_id=owner,
        repo="org/repo",
        task_prompt="work",
        runtime="claude-code",
        model="test-model",
        ssh_server_ids=ssh_server_ids,
    )


async def _events(pool, server_id: str) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind FROM agent_ssh_server_events WHERE server_id = $1::uuid ORDER BY id",
            server_id,
        )
    return [row["kind"] for row in rows]


async def test_names_are_unique_among_an_owners_live_servers(store, pool):
    first = await _server(store)
    with pytest.raises(SshServerConflict):
        await _server(store)
    assert (await _server(store, owner="bob"))["name"] == "gpu"
    assert await store.ssh_servers.get_ssh_server(user_id="bob", server_id=first["id"]) is None

    archived = await store.ssh_servers.archive_ssh_server(user_id="alice", server_id=first["id"])

    assert archived["private_key_ciphertext"] is None and archived["status"] == "pending"
    assert await store.ssh_servers.get_ssh_server(user_id="alice", server_id=first["id"]) is None
    assert (await _server(store))["id"] != first["id"], "an archived name can be used again"
    assert await _events(pool, first["id"]) == ["created", "verified", "archived"]


async def test_updates_are_compare_and_set_and_keep_json(store):
    server = await _server(store, ready=False)
    scanned = [{"type": "ssh-ed25519", "key": _HOST_KEY, "fingerprint": "SHA256:x"}]

    updated = await store.ssh_servers.update_ssh_server(
        user_id="alice",
        server_id=server["id"],
        revision=server["revision"],
        values={"scanned_ipv4": "93.184.216.34", "scanned_host_keys": scanned},
        event="updated",
    )
    stale = await store.ssh_servers.update_ssh_server(
        user_id="alice",
        server_id=server["id"],
        revision=server["revision"],
        values={"description": "late"},
        event="updated",
    )

    assert updated["scanned_host_keys"] == scanned
    assert updated["revision"] == server["revision"] + 1
    assert stale is None
    await _server(store, name="other")
    with pytest.raises(SshServerConflict):
        await store.ssh_servers.update_ssh_server(
            user_id="alice",
            server_id=server["id"],
            revision=updated["revision"],
            values={"name": "other"},
            event="updated",
        )


async def test_a_ready_server_must_have_its_pin_and_key(store, pool):
    server = await _server(store, ready=False)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_ssh_servers SET status = 'ready' WHERE id = $1::uuid", server["id"]
            )


async def test_checks_are_counted_per_owner(store):
    server = await _server(store, ready=False)
    for kind in ("scanned", "check_failed", "verified"):
        await store.ssh_servers.record_ssh_check(
            user_id="alice", server_id=server["id"], kind=kind, detail={}
        )
    assert await store.ssh_servers.count_recent_checks(user_id="alice", seconds=3600) == 3
    assert await store.ssh_servers.count_recent_checks(user_id="bob", seconds=3600) == 0


async def test_a_chat_selects_only_its_owners_ready_servers(store, pool):
    ready = await _server(store)
    pending = await _server(store, name="lab", ready=False)
    foreign = await _server(store, owner="bob", name="bobs")
    chat = await _chat(store)

    with pytest.raises(SshServerUnavailable) as refused:
        await store.ssh_servers.replace_thread_ssh_bindings(
            user_id="alice", thread_id=chat["thread_id"], server_ids=[pending["id"], foreign["id"]]
        )
    assert set(refused.value.server_ids) == {pending["id"], foreign["id"]}
    assert (
        await store.ssh_servers.replace_thread_ssh_bindings(
            user_id="bob", thread_id=chat["thread_id"], server_ids=[]
        )
        is None
    )

    chosen = await store.ssh_servers.replace_thread_ssh_bindings(
        user_id="alice", thread_id=chat["thread_id"], server_ids=[ready["id"]]
    )
    cleared = await store.ssh_servers.replace_thread_ssh_bindings(
        user_id="alice", thread_id=chat["thread_id"], server_ids=[]
    )

    assert chosen == [{"server_id": ready["id"], "enabled": True, "name": "gpu", "status": "ready"}]
    assert cleared[0]["enabled"] is False
    assert await _events(pool, ready["id"]) == ["created", "verified", "enabled", "disabled"]


async def test_a_server_on_for_a_chat_stays_selectable_while_it_is_checked_again(store):
    gpu = await _server(store)
    lab = await _server(store, name="lab")
    fresh = await _server(store, name="fresh", ready=False)
    chat = await _chat(store, ssh_server_ids=[gpu["id"]])
    await store.ssh_servers.update_ssh_server(
        user_id="alice",
        server_id=gpu["id"],
        revision=gpu["revision"],
        values={"status": "pending", "pinned_ipv4": None, "host_key": None},
        event="updated",
    )

    kept = await store.ssh_servers.replace_thread_ssh_bindings(
        user_id="alice", thread_id=chat["thread_id"], server_ids=[gpu["id"], lab["id"]]
    )
    with pytest.raises(SshServerUnavailable) as refused:
        await store.ssh_servers.replace_thread_ssh_bindings(
            user_id="alice", thread_id=chat["thread_id"], server_ids=[gpu["id"], fresh["id"]]
        )

    assert {(row["name"], row["enabled"]) for row in kept} == {("gpu", True), ("lab", True)}
    assert refused.value.server_ids == [fresh["id"]]
    state = await store.ssh_servers.ssh_group_state(thread_id=chat["thread_id"])
    (member,) = state["members"]
    assert [server["name"] for server in member["servers"]] == ["lab"]


async def test_a_new_chat_gets_its_servers_in_the_creating_transaction(store, pool):
    ready = await _server(store)
    pending = await _server(store, name="lab", ready=False)

    chat = await _chat(store, ssh_server_ids=[ready["id"]])
    with pytest.raises(SshServerUnavailable):
        await _chat(store, ssh_server_ids=[ready["id"], pending["id"]])

    bindings = await store.ssh_servers.list_thread_ssh_bindings(
        user_id="alice", thread_id=chat["thread_id"]
    )
    assert [binding["server_id"] for binding in bindings] == [ready["id"]]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM agent_threads") == 1


async def _group(pool, members: list[tuple[dict, int | None]]) -> str:
    origin = members[0][0]
    sandbox_id = f"asbx_{uuid.uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_sandboxes
                (id, user_id, origin_thread_id, host, container_name, runtime_id,
                 execution_profile, state)
            VALUES ($1, $2, $3, 'machine', $4, 'claude-code', 'trusted-v1', 'running')
            """,
            sandbox_id,
            origin["user_id"],
            origin["thread_id"],
            f"container-{sandbox_id}",
        )
        for job, slot in members:
            await conn.execute(
                """
                INSERT INTO agent_thread_sandbox_memberships
                    (thread_id, user_id, sandbox_id, resident_slot_id)
                VALUES ($1, $2, $3, $4)
                """,
                job["thread_id"],
                job["user_id"],
                sandbox_id,
                slot,
            )
    return sandbox_id


async def test_a_group_state_lists_every_members_usable_servers_at_a_new_revision(store, pool):
    gpu = await _server(store)
    lab = await _server(store, name="lab")
    plain = await _chat(store)
    assert await store.ssh_servers.ssh_group_state(thread_id=plain["thread_id"]) is None

    origin = await _chat(store, ssh_server_ids=[gpu["id"], lab["id"]])
    fork = await _chat(store)
    sandbox_id = await _group(pool, [(origin, 0), (fork, None)])
    await store.ssh_servers.archive_ssh_server(user_id="alice", server_id=lab["id"])

    first = await store.ssh_servers.ssh_group_state(thread_id=fork["thread_id"])
    second = await store.ssh_servers.ssh_group_state(thread_id=origin["thread_id"])

    assert second["revision"] > first["revision"]
    assert first["sandbox_id"] == sandbox_id
    members = {member["thread_id"]: member for member in first["members"]}
    assert members[origin["thread_id"]]["slot_id"] == 0
    assert [server["name"] for server in members[origin["thread_id"]]["servers"]] == ["gpu"]
    assert members[fork["thread_id"]] == {
        "thread_id": fork["thread_id"],
        "slot_id": None,
        "servers": [],
    }

    await store.ssh_servers.replace_thread_ssh_bindings(
        user_id="alice", thread_id=origin["thread_id"], server_ids=[]
    )
    emptied = await store.ssh_servers.ssh_group_state(thread_id=origin["thread_id"])
    assert emptied is not None, "a group that ever selected a server keeps receiving its state"
    assert all(member["servers"] == [] for member in emptied["members"])


async def test_a_deleted_chat_no_longer_opens_its_servers_to_the_group(store, pool):
    gpu = await _server(store)
    origin = await _chat(store, ssh_server_ids=[gpu["id"]])
    fork = await _chat(store)
    await _group(pool, [(origin, 0), (fork, 1)])
    # The owner deletes the chat that had gpu on; a failed Fork's placeholder
    # is tombstoned the same way. Neither runs again, and neither can be
    # switched off any more: the bindings routes no longer find the chat.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_threads SET deleted_at = NOW() WHERE id = $1", origin["thread_id"]
        )
    assert (
        await store.ssh_servers.list_thread_ssh_bindings(
            user_id="alice", thread_id=origin["thread_id"]
        )
        is None
    )

    state = await store.ssh_servers.ssh_group_state(thread_id=fork["thread_id"])

    assert state is not None, "the group is still told, so the host closes what was open"
    assert all(member["servers"] == [] for member in state["members"])


async def test_a_change_reaches_the_running_groups_of_its_chats_and_servers(store, pool):
    gpu = await _server(store)
    origin = await _chat(store, ssh_server_ids=[gpu["id"]])
    other = await _chat(store)
    sandbox_id = await _group(pool, [(origin, 1)])
    await _group(pool, [(other, 0)])

    by_server = await store.ssh_servers.running_groups_for_ssh_change(server_id=gpu["id"])
    by_thread = await store.ssh_servers.running_groups_for_ssh_change(
        thread_ids=[origin["thread_id"]]
    )

    assert by_server == [
        {"sandbox_id": sandbox_id, "thread_id": origin["thread_id"], "job_id": origin["id"]}
    ]
    assert by_thread == by_server
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agent_sandboxes SET state = 'stopped' WHERE id = $1", sandbox_id)
    assert await store.ssh_servers.running_groups_for_ssh_change(server_id=gpu["id"]) == []


async def test_a_fork_starts_with_its_sources_ssh_selection(store, pool):
    from tests.integration.storage.test_agent_job_store import _reserve_shared_chat

    job, claim, sandbox = await _reserve_shared_chat(store)
    owner = job["user_id"]
    gpu = await _server(store, owner=owner)
    lab = await _server(store, owner=owner, name="lab")
    await store.ssh_servers.replace_thread_ssh_bindings(
        user_id=owner, thread_id=job["thread_id"], server_ids=[gpu["id"], lab["id"]]
    )
    await store.ssh_servers.replace_thread_ssh_bindings(
        user_id=owner, thread_id=job["thread_id"], server_ids=[gpu["id"]]
    )
    fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **fence, container_id="container-ssh-fork", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **fence,
        residency_generation=sandbox["residency_generation"],
        job_id=job["id"],
        context_status="live",
        native_session_id="native-source",
        idle_timeout_seconds=1800,
        base_sha=job["base_sha"],
    )
    assert await store.attempt_commands.transition(
        job_id=job["id"],
        attempt_id=claim["attempt_id"],
        lease_generation=claim["lease_generation"],
        from_states=("running",),
        to_state="succeeded",
    )

    operation = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"], user_id=owner, idempotency_key="ssh-fork"
    )

    assert operation is not None and operation.get("error") is None
    async with pool.acquire() as conn:
        copied = await conn.fetch(
            """
            SELECT server_id::text AS server_id, enabled FROM agent_ssh_thread_bindings
            WHERE thread_id = $1 ORDER BY enabled
            """,
            operation["target_thread_id"],
        )
    assert [(row["server_id"], row["enabled"]) for row in copied] == [
        (lab["id"], False),
        (gpu["id"], True),
    ]
