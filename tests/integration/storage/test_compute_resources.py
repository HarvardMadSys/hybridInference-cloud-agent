"""Compute lifecycle invariants against deployed Alembic schema and real locks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.integration.storage.test_admin_sandbox_queries import member, seed
from tests.integration.storage.test_host_pool_store import (
    _claim,
    _create_job,
    _enrol,
    db as db,
    hosts as hosts,
    store as store,
)

from cloud_agent.host_pool import hash_secret
from cloud_agent.storage._sql.compute_admission import (
    host_compute_admission,
    lock_compute_entry,
    lock_compute_topology,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def resource(store, hosts=None, *, name="machine", provision=False):
    """Create one observed resource; optionally bind an enrolled ready Host."""
    repo = store.compute_resources
    request = str(uuid4())
    op = await repo.create_allocation_operation(
        request_id=request,
        fingerprint=request,
        kind="add",
        allocation={
            "name": name,
            "provider_id": "cloudlab",
            "mode": "provision" if provision else "attach",
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
        },
        parameters={},
        requested_by="admin",
    )
    allocation_id = op["allocation_id"]
    claimed = await repo.claim_operation(worker_id="fixture")
    assert claimed["id"] == op["id"]
    node = await repo.upsert_node(
        allocation_id=allocation_id,
        node={"provider_node_id": "node-1", "instance_identity": request, "name": name},
    )
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "external_id": request,
            "observed_state": "ready",
            "expiration_known": True,
            "expires_at": datetime.now(UTC) + timedelta(hours=4),
            "scope_complete": True,
        },
    )
    if hosts:
        await repo.prepare_bootstrap_binding(node_id=node["id"], host=name, operation_id=op["id"])
        await hosts.register_host(
            host=name, credential_hash=hash_secret("cred-" + name), slots_total=2
        )
        await hosts.record_host_heartbeat(host=name)
        assert await repo.clear_bootstrap_hold(node_id=node["id"], host=name, operation_id=op["id"])
    assert await repo.finish_operation(
        operation_id=op["id"],
        generation=claimed["generation"],
        worker_id="fixture",
        state="succeeded",
        step="ready",
    )
    return allocation_id, node, op


async def operation(repo, allocation_id, kind="retire"):
    request = str(uuid4())
    await repo.create_operation(
        request_id=request,
        fingerprint=request,
        kind=kind,
        allocation_id=allocation_id,
        parameters={},
        requested_by="admin",
    )
    return await repo.claim_operation(worker_id="worker")


def fence(op):
    return {"operation_id": op["id"], "generation": op["generation"], "worker_id": "worker"}


async def complete_admin_deletion(store, sandbox_id: str) -> None:
    """Drive one stopped retained group through the durable deletion protocol."""
    detail = await store.admin_sandboxes.detail(sandbox_id)
    assert detail is not None
    assert (
        await store.sandbox.request_admin_delete(
            sandbox_id=sandbox_id,
            generation=detail.sandbox.generation,
            container_id=detail.sandbox.container_id,
            last_activity_at=detail.sandbox.last_activity_at,
            actor_id="admin:scale-in-test",
            admin_ip="127.0.0.1",
        )
        == "accepted"
    )
    claim = await store.sandbox.claim_due_admin_sandbox_deletion(ttl_seconds=3600)
    assert claim is not None and claim["id"] == sandbox_id
    worker_fence = {
        "sandbox_id": sandbox_id,
        "request_id": claim["deletion_request_id"],
        "claim_token": claim["deletion_claim_token"],
        "generation": claim["generation"],
        "container_id": claim["container_id"],
    }
    assert await store.sandbox.record_admin_sandbox_archive(
        **worker_fence,
        archive_key=f"s3://sandbox-archive-bucket/{sandbox_id}.tar",
        sha256="d" * 64,
        size_bytes=4096,
    )
    assert await store.sandbox.complete_admin_sandbox_deletion(**worker_fence)


async def retained_group_counts(store, *, allocation_id: str, host: str) -> tuple[int, int, int]:
    """Read the three payloads that feed autoscaling and retirement decisions."""
    observation = await store.host_pool.autoscaling_observation(expected_deployment_sha="")
    observed_host = next(row for row in observation["hosts"] if row["host"] == host)
    allocation = await store.compute_resources.get_allocation(allocation_id)
    assert allocation is not None
    allocation_host = next(row for row in allocation["hosts"] if row["host"] == host)
    inventory = await store.compute_resources.server_inventory()
    inventory_host = next(row for row in inventory["hosts"] if row["host"] == host)
    return (
        observed_host["retained_groups"],
        allocation_host["retained_groups"],
        inventory_host["retained_groups"],
    )


async def autoscale_acquisition(
    db,
    store,
    *,
    request_id: str,
    state: str | None = "blocked",
    result: dict | None = None,
):
    """Create and optionally finish one owned acquisition without a provider call."""
    repo = store.compute_resources
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_compute_connections
               (id,name,provider_id,secret_ciphertext,status)
               VALUES ('cancel-cloudlab','CloudLab','cloudlab','ciphertext','verified'),
                      ('cancel-ssh','SSH','existing_ssh','ciphertext','verified')"""
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=1,
                   connection_id='cancel-cloudlab',ssh_connection_id='cancel-ssh',
                   deployment_profiles=ARRAY['cancel-route']"""
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot)
               VALUES ('cancel-route','{"id":"cancel-route"}')"""
        )
    created = await repo.create_autoscale_allocation_operation(
        expected_revision=0,
        expected_generation=0,
        request_id=request_id,
        fingerprint=request_id,
        allocation={"name": "cancel candidate"},
        parameters={},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_up"},
        cooldown_seconds=60,
    )
    claimed = await repo.claim_operation(worker_id="worker")
    assert claimed["id"] == created["operation"]["id"]
    if state is not None:
        assert await repo.finish_operation(
            **fence(claimed),
            state=state,
            step="pending",
            message="provider rejected before submission",
            result=result or {},
        )
    return created["operation"]["allocation_id"], claimed


SLOT_CAPACITY = {
    "cpu_logical_count": 16,
    "memory_total_bytes": 64 * 1024**3,
}


async def autoscaling_slot_member(db, store, hosts, *, name: str):
    allocation_id, _, _ = await resource(store, hosts, name=name)
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=1"""
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,$2,'active')""",
            allocation_id,
            name + "-route",
        )
        sampled_at = await conn.fetchval(
            """UPDATE agent_runner_hosts
               SET slots_total=6,slots_desired=6,last_heartbeat_at=NOW(),
                   launch_error=NULL,
                   capacity_snapshot=jsonb_build_object(
                       'cpu_logical_count',16,
                       'memory_total_bytes',$2::bigint
                   ),
                   capacity_received_at=NOW(),capacity_sampled_at=NOW()
               WHERE host=$1 RETURNING capacity_sampled_at""",
            name,
            SLOT_CAPACITY["memory_total_bytes"],
        )
        await conn.execute(
            """INSERT INTO agent_host_admission_holds
               (host,allocation_id,purpose,source_id,scope)
               VALUES ($1,$2,'autoscaling_slots',$2,'all_claims')""",
            name,
            allocation_id,
        )
    return allocation_id, sampled_at


async def wait_until_blocked_by(db, blocker_pid: int) -> None:
    for _ in range(200):
        async with db.acquire() as conn:
            blocked = await conn.fetchval(
                """SELECT EXISTS (
                       SELECT 1 FROM pg_stat_activity activity
                       WHERE $1=ANY(pg_blocking_pids(activity.pid))
                   )""",
                blocker_pid,
            )
        if blocked:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("slot target transaction did not reach the expected database lock")


async def test_provider_defaults_are_isolated_and_replace_atomically(store):
    repo = store.compute_resources
    assert await repo.provider_defaults("cloudlab") == {}

    saved = {"project": "research", "duration": 12}
    assert await repo.set_provider_defaults("cloudlab", saved) == saved
    assert await repo.set_provider_defaults("future-provider", {"region": "test"}) == {
        "region": "test"
    }
    assert await repo.provider_defaults("cloudlab") == saved
    assert await repo.provider_defaults("future-provider") == {"region": "test"}


async def test_concurrent_request_replay_creates_one_allocation_and_conflicts_on_changed_payload(
    store,
):
    repo = store.compute_resources
    kwargs = {
        "request_id": "same",
        "fingerprint": "same",
        "kind": "add",
        "allocation": {"name": "new", "provider_id": "existing_ssh", "mode": "attach"},
        "parameters": {},
        "requested_by": "admin",
    }
    first, second = await asyncio.gather(
        repo.create_allocation_operation(**kwargs), repo.create_allocation_operation(**kwargs)
    )
    assert first["id"] == second["id"]
    assert len(await repo.list_allocations()) == 1
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await repo.create_allocation_operation(**{**kwargs, "fingerprint": "changed"})


async def test_connection_ciphertext_is_not_in_list_and_revision_prevents_lost_update(store):
    repo = store.compute_resources
    kwargs = {
        "connection_id": "account",
        "provider_id": "cloudlab",
        "name": "research",
        "configuration": {},
        "secret_ciphertext": "encrypted-fixture",
        "masked_summary": "configured",
    }
    await repo.save_connection(**kwargs)
    rows = await repo.list_connections()
    assert "secret_ciphertext" not in rows[0]
    await repo.save_connection(**kwargs, expected_revision=1)
    with pytest.raises(ValueError, match="revision_conflict"):
        await repo.save_connection(**kwargs, expected_revision=1)


async def test_external_uuid_cannot_be_adopted_through_alias_connection(db, store):
    import asyncpg

    repo = store.compute_resources
    one, _, _ = await resource(store)
    two, _, _ = await resource(store)
    existing = await repo.get_allocation(one)
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_compute_allocations SET external_id=NULL WHERE id=$1", two)
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.observe_allocation(
            allocation_id=two,
            observation={"external_id": existing["external_id"], "observed_state": "ready"},
        )


async def test_expired_worker_cannot_overwrite_observation_and_resume_keeps_fixed_receipt(
    db, store
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store)
    first = await operation(repo, allocation_id, "extend")
    assert await repo.checkpoint_operation(
        **fence(first), step="extending", result={"target": "fixed"}
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",
            first["id"],
        )
    second = await repo.claim_operation(worker_id="other")
    assert second["id"] == first["id"] and second["generation"] == first["generation"] + 1
    assert second["step"] == "extending" and second["result"]["target"] == "fixed"
    with pytest.raises(ValueError, match="operation_lease_lost"):
        await repo.observe_allocation(
            allocation_id=allocation_id,
            observation={"observed_state": "terminated"},
            **fence(first),
        )
    assert not await repo.finish_operation(**fence(first), state="succeeded", step="done")
    assert (await repo.get_allocation(allocation_id))["observed_state"] == "ready"


async def test_unknown_external_call_blocks_another_mutation_until_recovered(store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store)
    op = await operation(repo, allocation_id, "extend")
    await repo.finish_operation(**fence(op), state="unknown", step="extending")
    await repo.create_operation(
        request_id="next",
        fingerprint="next",
        kind="retire",
        allocation_id=allocation_id,
        parameters={},
        requested_by="admin",
    )
    assert await repo.claim_operation(worker_id="another") is None
    await repo.retry_operation(op["id"])
    reclaimed = await repo.claim_operation(worker_id="another")
    assert reclaimed["id"] == op["id"]
    assert reclaimed["step"] == "extending"


async def test_bootstrap_and_drain_are_independent_and_node_identity_cannot_change(store, hosts):
    repo = store.compute_resources
    allocation_id, node, _op = await resource(store, hosts)
    await repo.prepare_bootstrap_binding(
        node_id=node["id"], host="machine", operation_id="second-boot"
    )
    await _create_job(store)
    assert await _claim(store, worker_id="runner", lease_ttl_seconds=90, host="machine") is None
    await hosts.set_host_status(host="machine", status="draining")
    assert not await repo.clear_bootstrap_hold(
        node_id=node["id"], host="machine", operation_id="second-boot"
    )
    assert (await hosts.get_host(host="machine"))["status"] == "draining"
    with pytest.raises(ValueError, match="resource_identity_changed"):
        await repo.upsert_node(
            allocation_id=allocation_id,
            node={
                "provider_node_id": "node-1",
                "instance_identity": "replacement",
                "name": "machine",
            },
        )
    assert await hosts.forget_runner_host(host="machine") == "resource_bound"


async def test_autoscaling_bootstrap_keeps_claims_closed_until_slots_are_applied(db, store, hosts):
    repo = store.compute_resources
    allocation_id, node, _ = await resource(store, name="autoscale-sizing")
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile) VALUES ($1,'sizing-route')""",
            allocation_id,
        )

    await repo.prepare_bootstrap_binding(
        node_id=node["id"],
        host="autoscale-sizing",
        operation_id="autoscale-bootstrap",
    )
    await hosts.register_host(
        host="autoscale-sizing",
        credential_hash=hash_secret("autoscale-sizing-credential"),
        slots_total=2,
    )
    await hosts.record_host_heartbeat(host="autoscale-sizing")
    purposes = {hold["purpose"] for hold in (await repo.get_allocation(allocation_id))["holds"]}
    assert purposes == {"bootstrap", "autoscaling_slots"}

    assert await repo.clear_bootstrap_hold(
        node_id=node["id"],
        host="autoscale-sizing",
        operation_id="autoscale-bootstrap",
    )
    holds = (await repo.get_allocation(allocation_id))["holds"]
    assert [(hold["purpose"], hold["scope"]) for hold in holds] == [
        ("autoscaling_slots", "all_claims")
    ]

    revision = holds[0]["revision"]
    for _ in range(2):
        await repo.set_admission_hold(
            allocation_id=allocation_id,
            purpose="autoscaling_slots",
            source_id=allocation_id,
            scope="all_claims",
        )
    reasserted = (await repo.get_allocation(allocation_id))["holds"]
    assert len(reasserted) == 1
    assert reasserted[0]["revision"] == revision
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "autoscale-sizing") == (
            False,
            False,
        )

    with pytest.raises(ValueError, match="autoscaling_slot_fence_required"):
        await repo.set_admission_hold(
            allocation_id=allocation_id,
            purpose="autoscaling_slots",
            source_id=allocation_id,
            scope=None,
        )
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_cloudlab_autoscaling_policy SET enabled=TRUE WHERE id")
        sampled_at = await conn.fetchval(
            """UPDATE agent_runner_hosts
               SET slots_total=6,slots_desired=6,last_heartbeat_at=NOW(),
                   launch_error=NULL,
                   capacity_snapshot=jsonb_build_object(
                       'cpu_logical_count',16,
                       'memory_total_bytes',$2::bigint
                   ),
                   capacity_received_at=NOW(),capacity_sampled_at=NOW()
               WHERE host=$1 RETURNING capacity_sampled_at""",
            "autoscale-sizing",
            SLOT_CAPACITY["memory_total_bytes"],
        )
    cleared = await repo.apply_cloudlab_autoscaling_slot_target(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        host="autoscale-sizing",
        target_slots=6,
        capacity_sampled_at=sampled_at,
        capacity_snapshot=SLOT_CAPACITY,
    )
    assert cleared["outcome"] == "ready"
    assert (await repo.get_allocation(allocation_id))["holds"] == []


async def test_autoscaling_slot_target_stays_held_until_the_host_confirms(db, store, hosts):
    repo = store.compute_resources
    allocation_id, sampled_at = await autoscaling_slot_member(db, store, hosts, name="slot-apply")
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_runner_hosts
               SET slots_total=2,slots_desired=NULL WHERE host='slot-apply'"""
        )

    awaiting = await repo.apply_cloudlab_autoscaling_slot_target(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        host="slot-apply",
        target_slots=6,
        capacity_sampled_at=sampled_at,
        capacity_snapshot=SLOT_CAPACITY,
    )

    assert awaiting["outcome"] == "awaiting_host_apply"
    assert awaiting["slots_desired"] == 6
    assert (await repo.get_allocation(allocation_id))["holds"][0]["scope"] == "all_claims"
    async with db.acquire() as conn:
        await conn.execute("UPDATE agent_runner_hosts SET slots_total=6 WHERE host='slot-apply'")

    ready = await repo.apply_cloudlab_autoscaling_slot_target(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        host="slot-apply",
        target_slots=6,
        capacity_sampled_at=sampled_at,
        capacity_snapshot=SLOT_CAPACITY,
    )

    assert ready["outcome"] == "ready"
    assert (await repo.get_allocation(allocation_id))["holds"] == []


async def test_pause_committing_before_slot_clear_keeps_all_claims_held(db, store, hosts):
    repo = store.compute_resources
    allocation_id, sampled_at = await autoscaling_slot_member(
        db, store, hosts, name="slot-pause-race"
    )
    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_host_admission_holds WHERE allocation_id=$1",
            allocation_id,
        )
    slot_task = None
    async with db.acquire() as admin:
        transaction = admin.transaction()
        await transaction.start()
        try:
            await lock_compute_entry(admin, exclusive=True)
            blocker_pid = await admin.fetchval("SELECT pg_backend_pid()")
            await admin.execute(
                """UPDATE agent_cloudlab_autoscaling_policy
                   SET enabled=FALSE,revision=revision+1,updated_at=NOW() WHERE id"""
            )
            await admin.execute(
                """UPDATE agent_cloudlab_autoscaling_runtime
                   SET generation=generation+1,updated_at=NOW() WHERE id"""
            )
            slot_task = asyncio.create_task(
                repo.apply_cloudlab_autoscaling_slot_target(
                    expected_revision=0,
                    expected_generation=0,
                    allocation_id=allocation_id,
                    host="slot-pause-race",
                    target_slots=6,
                    capacity_sampled_at=sampled_at,
                    capacity_snapshot=SLOT_CAPACITY,
                )
            )
            await wait_until_blocked_by(db, blocker_pid)
            await transaction.commit()
        except BaseException:
            if slot_task is not None:
                slot_task.cancel()
                await asyncio.gather(slot_task, return_exceptions=True)
            await transaction.rollback()
            raise

    assert slot_task is not None
    refused = await slot_task
    assert refused["blocker_code"] == "autoscaling_revision_conflict"
    holds = (await repo.get_allocation(allocation_id))["holds"]
    assert holds == []

    await repo.set_admission_hold(
        allocation_id=allocation_id,
        purpose="autoscaling_slots",
        source_id=allocation_id,
        scope="all_claims",
    )
    still_refused = await repo.apply_cloudlab_autoscaling_slot_target(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        host="slot-pause-race",
        target_slots=6,
        capacity_sampled_at=sampled_at,
        capacity_snapshot=SLOT_CAPACITY,
    )
    assert still_refused["blocker_code"] == "autoscaling_revision_conflict"
    holds = (await repo.get_allocation(allocation_id))["holds"]
    assert [(hold["purpose"], hold["scope"]) for hold in holds] == [
        ("autoscaling_slots", "all_claims")
    ]


async def test_pin_committing_before_slot_clear_keeps_all_claims_held(db, store, hosts):
    repo = store.compute_resources
    allocation_id, sampled_at = await autoscaling_slot_member(
        db, store, hosts, name="slot-pin-race"
    )
    await hosts.register_host(
        host="manual-pin",
        credential_hash=hash_secret("manual-pin-credential"),
        slots_total=2,
    )
    await hosts.record_host_heartbeat(host="manual-pin")

    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_host_admission_holds WHERE allocation_id=$1",
            allocation_id,
        )
    slot_task = None
    async with db.acquire() as admin:
        transaction = admin.transaction()
        await transaction.start()
        try:
            await admin.execute("SELECT active_host FROM agent_runner_policy WHERE id FOR UPDATE")
            await lock_compute_topology(admin)
            blocker_pid = await admin.fetchval("SELECT pg_backend_pid()")
            await admin.execute(
                """UPDATE agent_runner_policy
                   SET active_host='manual-pin',updated_at=NOW() WHERE id"""
            )
            slot_task = asyncio.create_task(
                repo.apply_cloudlab_autoscaling_slot_target(
                    expected_revision=0,
                    expected_generation=0,
                    allocation_id=allocation_id,
                    host="slot-pin-race",
                    target_slots=6,
                    capacity_sampled_at=sampled_at,
                    capacity_snapshot=SLOT_CAPACITY,
                )
            )
            await wait_until_blocked_by(db, blocker_pid)
            await transaction.commit()
        except BaseException:
            if slot_task is not None:
                slot_task.cancel()
                await asyncio.gather(slot_task, return_exceptions=True)
            await transaction.rollback()
            raise

    assert slot_task is not None
    refused = await slot_task
    assert refused["blocker_code"] == "autoscaling_placement_pinned"
    holds = (await repo.get_allocation(allocation_id))["holds"]
    assert holds == []

    await repo.set_admission_hold(
        allocation_id=allocation_id,
        purpose="autoscaling_slots",
        source_id=allocation_id,
        scope="all_claims",
    )
    still_refused = await repo.apply_cloudlab_autoscaling_slot_target(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        host="slot-pin-race",
        target_slots=6,
        capacity_sampled_at=sampled_at,
        capacity_snapshot=SLOT_CAPACITY,
    )
    assert still_refused["blocker_code"] == "autoscaling_placement_pinned"
    holds = (await repo.get_allocation(allocation_id))["holds"]
    assert [(hold["purpose"], hold["scope"]) for hold in holds] == [
        ("autoscaling_slots", "all_claims")
    ]


async def test_autoscaling_ownership_committing_before_admin_slot_edit_refuses_write(
    db, store, hosts
):
    allocation_id, _, _ = await resource(store, hosts, name="slot-admin-race")
    assert await hosts.set_host_slots_desired(host="slot-admin-race", slots=2) == "ok"

    slot_task = None
    async with db.acquire() as owner:
        transaction = owner.transaction()
        await transaction.start()
        try:
            await lock_compute_entry(owner, exclusive=True)
            blocker_pid = await owner.fetchval("SELECT pg_backend_pid()")
            await owner.execute(
                """INSERT INTO agent_cloudlab_autoscaling_members
                   (allocation_id,deployment_profile,state)
                   VALUES ($1,'slot-admin-race-route','active')""",
                allocation_id,
            )
            slot_task = asyncio.create_task(
                hosts.set_host_slots_desired(host="slot-admin-race", slots=8)
            )
            await wait_until_blocked_by(db, blocker_pid)
            await transaction.commit()
        except BaseException:
            if slot_task is not None:
                slot_task.cancel()
                await asyncio.gather(slot_task, return_exceptions=True)
            await transaction.rollback()
            raise

    assert slot_task is not None
    assert await slot_task == "autoscaling_managed"
    assert (await hosts.get_host(host="slot-admin-race"))["slots_desired"] == 2


async def test_expiry_is_enforced_by_claim_even_without_worker_and_stale_observation_is_not_eternal(
    db, store, hosts
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    await _create_job(store)
    # No renewal worker runs. A known deadline alone prevents dispatch.
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "observed_state": "ready",
            "expiration_known": True,
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
            "scope_complete": True,
        },
    )
    assert await _claim(store, worker_id="runner", lease_ttl_seconds=90, host="machine") is None
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (False, False)
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "observed_state": "ready",
            "expiration_known": True,
            "expires_at": datetime.now(UTC) + timedelta(hours=4),
            "scope_complete": True,
        },
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET observed_at=NOW()-INTERVAL '6 minutes' WHERE id=$1",
            allocation_id,
        )
    before = await repo.get_allocation(allocation_id)
    await repo.record_observation_error(allocation_id=allocation_id, message="temporary_timeout")
    after = await repo.get_allocation(allocation_id)
    assert (
        before["expires_at"] == after["expires_at"]
        and before["observed_at"] == after["observed_at"]
    )
    assert await _claim(store, worker_id="runner", lease_ttl_seconds=90, host="machine") is None


async def test_retirement_blocks_new_claims_binding_registration_and_pin_but_renewal_continues(
    store, hosts
):
    repo = store.compute_resources
    allocation_id, node, _ = await resource(store, hosts)
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    await _create_job(store)
    assert await _claim(store, worker_id="runner", lease_ttl_seconds=90, host="machine") is None
    assert not await hosts.set_active_runner_host(host="machine")
    with pytest.raises(ValueError, match="resource_retiring"):
        await repo.prepare_bootstrap_binding(
            node_id=node["id"], host="another", operation_id="late"
        )
    with pytest.raises(ValueError, match="resource_retiring"):
        await hosts.register_host(host="machine", credential_hash="late")
    await repo.finish_operation(**fence(op), state="blocked", step="retained")
    renewal = await operation(repo, allocation_id, "extend")
    assert renewal["kind"] == "extend"
    await repo.finish_operation(**fence(renewal), state="succeeded", step="extended")
    assert (await repo.get_allocation(allocation_id))["desired_state"] == "absent"
    assert not await hosts.set_active_runner_host(host="machine")
    await repo.cancel_operation(op["id"])
    assert await hosts.set_active_runner_host(host="machine")


async def test_stopped_retained_group_blocks_release_even_with_zero_slots(db, store, hosts):
    repo = store.compute_resources
    allocation_id, _node, _ = await resource(store, hosts)
    job = await _create_job(store)
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_sandboxes
            (id,user_id,origin_thread_id,host,container_name,runtime_id,execution_profile,state)
            VALUES ('retained','u1',$1,'machine','old-container','claude-code','trusted-v1','stopped')""",
            job["thread_id"],
        )
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    assert await repo.record_inventory(
        allocation_id=allocation_id, hosts=["machine"], empty=True, **fence(op)
    )
    impact = await repo.commit_release(allocation_id=allocation_id, **fence(op))
    assert "retained_groups_present" in impact["blocked_reasons"]
    assert not impact["release_committed"]
    assert (await hosts.get_host(host="machine"))["enrolled"]
    assert await hosts.forget_runner_host(host="machine") == "retained_groups_present"


async def test_completed_admin_deletion_makes_autoscaled_host_scale_down_eligible(db, store, hosts):
    repo = store.compute_resources
    first, _, _ = await resource(store, hosts, name="autoscale-retained-peer")
    candidate, _, _ = await resource(store, hosts, name="autoscale-retained-candidate")
    origin = await seed(
        db,
        store,
        "autoscale-retained-group",
        state="stopped",
        host="autoscale-retained-candidate",
    )
    await member(db, "autoscale-retained-group", origin, None)
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=1,max_hosts=2,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY['retained-peer','retained-candidate']"""
        )
        for allocation_id, profile in (
            (first, "retained-peer"),
            (candidate, "retained-candidate"),
        ):
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_members
                   (allocation_id,deployment_profile,state) VALUES ($1,$2,'active')""",
                allocation_id,
                profile,
            )
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
                   (deployment_profile,profile_snapshot,allocation_id)
                   VALUES ($1,jsonb_build_object('id',$1::text),$2)""",
                profile,
                allocation_id,
            )

    assert await retained_group_counts(
        store, allocation_id=candidate, host="autoscale-retained-candidate"
    ) == (1, 1, 1)
    with pytest.raises(ValueError, match="autoscaling_member_not_idle"):
        await repo.queue_autoscale_retire(
            expected_revision=0,
            expected_generation=0,
            allocation_id=candidate,
            request_id="retire-retained-live",
            fingerprint="retire-retained-live",
            parameters={"allocation_id": candidate},
            requested_by="system:cloudlab-autoscaler",
            decision={"action": "scale_down_idle", "allocation_id": candidate},
            cooldown_seconds=60,
        )

    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes
               SET state='lost',deletion_state='failed'
               WHERE id='autoscale-retained-group'"""
        )
    assert await retained_group_counts(
        store, allocation_id=candidate, host="autoscale-retained-candidate"
    ) == (1, 1, 1)
    with pytest.raises(ValueError, match="autoscaling_member_not_idle"):
        await repo.queue_autoscale_retire(
            expected_revision=0,
            expected_generation=0,
            allocation_id=candidate,
            request_id="retire-retained-lost-incomplete",
            fingerprint="retire-retained-lost-incomplete",
            parameters={"allocation_id": candidate},
            requested_by="system:cloudlab-autoscaler",
            decision={"action": "scale_down_idle", "allocation_id": candidate},
            cooldown_seconds=60,
        )

    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes
               SET state='stopped',deletion_state='none'
               WHERE id='autoscale-retained-group'"""
        )
    await complete_admin_deletion(store, "autoscale-retained-group")

    assert await retained_group_counts(
        store, allocation_id=candidate, host="autoscale-retained-candidate"
    ) == (0, 0, 0)
    # The deleted audit row no longer counts as retained capacity. Forgetting
    # the pool row is still refused while the live allocation owns the host.
    assert await hosts.forget_runner_host(host="autoscale-retained-candidate") == "resource_bound"
    queued = await repo.queue_autoscale_retire(
        expected_revision=0,
        expected_generation=0,
        allocation_id=candidate,
        request_id="retire-after-admin-deletion",
        fingerprint="retire-after-admin-deletion",
        parameters={"allocation_id": candidate},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_down_idle", "allocation_id": candidate},
        cooldown_seconds=60,
    )
    assert queued["operation"]["kind"] == "retire"
    assert queued["operation"]["allocation_id"] == candidate


async def test_completed_admin_deletion_unblocks_resource_retirement(db, store, hosts):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name="retirement-after-deletion")
    origin = await seed(
        db,
        store,
        "retirement-retained-group",
        state="stopped",
        host="retirement-after-deletion",
    )
    await member(db, "retirement-retained-group", origin, None)
    op = await operation(repo, allocation_id)

    impact = await repo.begin_retirement(allocation_id=allocation_id, **fence(op))
    assert impact["hosts"][0]["retained_groups"] == 1
    assert [group["id"] for group in impact["retained_groups"]] == ["retirement-retained-group"]
    assert "retained_groups_present" in impact["blocked_reasons"]

    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes
               SET state='lost',deletion_state='failed'
               WHERE id='retirement-retained-group'"""
        )
    lost = await repo.retirement_impact(allocation_id)
    assert lost["hosts"][0]["retained_groups"] == 1
    assert [group["id"] for group in lost["retained_groups"]] == ["retirement-retained-group"]
    assert "retained_groups_present" in lost["blocked_reasons"]

    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes
               SET state='stopped',deletion_state='none'
               WHERE id='retirement-retained-group'"""
        )
    await complete_admin_deletion(store, "retirement-retained-group")

    deleted = await repo.retirement_impact(allocation_id)
    assert deleted["hosts"][0]["retained_groups"] == 0
    assert deleted["retained_groups"] == []
    assert "retained_groups_present" not in deleted["blocked_reasons"]
    assert await hosts.forget_runner_host(host="retirement-after-deletion") == "resource_bound"
    assert await repo.record_inventory(
        allocation_id=allocation_id,
        hosts=["retirement-after-deletion"],
        empty=True,
        **fence(op),
    )
    committed = await repo.commit_release(allocation_id=allocation_id, **fence(op))
    assert committed["release_committed"]
    assert committed["retained_groups"] == []
    assert committed["blocked_reasons"] == []


async def test_completed_sandbox_deletion_lets_an_unbound_host_be_forgotten(db, store, hosts):
    """A finished deletion is history, so Forget can drop the pool row.

    The sandbox row keeps the host name. A stopped group, and one whose
    deletion failed, still blocks. Only ``deletion_state='deleted'`` does not.
    """
    await _enrol(store, "retired-host", hosts=hosts)
    origin = await seed(db, store, "retired-host-group", state="stopped", host="retired-host")
    await member(db, "retired-host-group", origin, None)
    assert await hosts.forget_runner_host(host="retired-host") == "retained_groups_present"

    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes
               SET state='stopped', deletion_state='failed'
               WHERE id='retired-host-group'"""
        )
    assert await hosts.forget_runner_host(host="retired-host") == "retained_groups_present"

    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_sandboxes
               SET deletion_state='none'
               WHERE id='retired-host-group'"""
        )
    await complete_admin_deletion(store, "retired-host-group")
    assert await hosts.forget_runner_host(host="retired-host") == "deleted"
    assert await hosts.get_host(host="retired-host") is None
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT host, deletion_state FROM agent_sandboxes WHERE id='retired-host-group'"
        )
    assert row["host"] == "retired-host"
    assert row["deletion_state"] == "deleted"


async def test_terminal_provider_state_commits_release_and_revokes_host_despite_retention(
    db, store, hosts
):
    repo = store.compute_resources
    allocation_id, _node, _ = await resource(store, hosts, name="terminal-release")
    job = await _create_job(store)
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_sandboxes
            (id,user_id,origin_thread_id,host,container_name,runtime_id,execution_profile,state)
            VALUES ('terminal-retained','u1',$1,'terminal-release','terminal-container',
                    'claude-code','trusted-v1','stopped')""",
            job["thread_id"],
        )
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, **fence(op))
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
        **fence(op),
    )

    committed = await repo.commit_release(allocation_id=allocation_id, **fence(op))

    assert committed["release_committed"]
    assert committed["blocked_reasons"] == []
    assert len(committed["retained_groups"]) == 1
    host = await hosts.get_host(host="terminal-release")
    assert host["status"] == "disabled"
    assert not host["enrolled"]


async def test_complete_inventory_and_pin_are_rechecked_at_irreversible_commit(store, hosts):
    repo = store.compute_resources
    allocation_id, _node, _ = await resource(store, hosts)
    assert await hosts.set_active_runner_host(host="machine")
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    assert not await repo.record_inventory(
        allocation_id=allocation_id, hosts=[], empty=True, **fence(op)
    )
    assert await repo.record_inventory(
        allocation_id=allocation_id, hosts=["machine"], empty=True, **fence(op)
    )
    impact = await repo.commit_release(allocation_id=allocation_id, **fence(op))
    assert impact["blocked_reasons"] == ["placement_pinned"]
    await hosts.set_active_runner_host(host=None)
    committed, pinned = await asyncio.gather(
        repo.commit_release(allocation_id=allocation_id, **fence(op)),
        hosts.set_active_runner_host(host="machine"),
    )
    assert committed["release_committed"] and not pinned
    assert (await hosts.get_host(host="machine"))["status"] == "disabled"
    assert not (await hosts.get_host(host="machine"))["enrolled"]
    await repo.finish_operation(**fence(op), state="unknown", step="releasing")
    with pytest.raises(ValueError, match="release_committed"):
        await repo.cancel_operation(op["id"])
    with pytest.raises(ValueError, match="release_committed"):
        await repo.create_operation(
            request_id="too-late",
            fingerprint="too-late",
            kind="extend",
            allocation_id=allocation_id,
            parameters={},
            requested_by="admin",
        )


async def test_release_commit_waits_for_active_deployment_drain(store, hosts):
    repo = store.compute_resources
    allocation_id, _node, _ = await resource(store, hosts, name="drain-safe-release")
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, **fence(op))
    assert await repo.record_inventory(
        allocation_id=allocation_id,
        hosts=["drain-safe-release"],
        empty=True,
        **fence(op),
    )
    host = await hosts.get_host(host="drain-safe-release")
    token = uuid4()
    outcome, _status_before, _epoch = await hosts.acquire_host_drain_fence(
        host="drain-safe-release",
        expected_fence_token=None,
        expected_fence_epoch=host["drain_fence_epoch"],
        fence_token=token,
    )
    assert outcome == "ok"

    impact = await repo.commit_release(allocation_id=allocation_id, **fence(op))

    assert impact["blocked_reasons"] == ["deployment_drain_active"]
    assert not impact["release_committed"]
    allocation = await repo.get_allocation(allocation_id)
    assert allocation["release_committed_at"] is None
    assert (await hosts.get_host(host="drain-safe-release"))["enrolled"]

    released, _epoch = await hosts.release_host_drain_fence(
        host="drain-safe-release", fence_token=token
    )
    assert released == "ok"

    committed = await repo.commit_release(allocation_id=allocation_id, **fence(op))
    assert committed["release_committed"]


async def test_failed_bootstrap_can_retire_after_full_trusted_node_scan(store):
    repo = store.compute_resources
    allocation_id, node, _ = await resource(store, provision=True)
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    assert not await repo.record_inventory(
        allocation_id=allocation_id, hosts=[], empty=True, **fence(op)
    )
    assert await repo.record_inventory(
        allocation_id=allocation_id,
        hosts=[],
        verified_node_ids=[node["id"]],
        empty=True,
        **fence(op),
    )
    assert (await repo.commit_release(allocation_id=allocation_id, **fence(op)))[
        "release_committed"
    ]


async def test_server_inventory_reports_legacy_and_managed_actual_distribution(store, hosts):
    await resource(store, hosts)
    await hosts.register_host(host="legacy", credential_hash="legacy")
    inventory = await store.compute_resources.server_inventory()
    assert {h["host"] for h in inventory["hosts"]} == {"legacy", "machine"}
    legacy = next(h for h in inventory["hosts"] if h["host"] == "legacy")
    assert legacy["allocation_id"] is None
    assert inventory["allocations"][0]["nodes"]


async def test_waiting_external_call_serializes_until_positive_confirmation(store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store)
    op = await operation(repo, allocation_id, "extend")
    await repo.finish_operation(
        **fence(op),
        state="waiting",
        step="extending",
        result={"external_pending": True},
        retry_after_seconds=3600,
    )
    await repo.create_operation(
        request_id="other-extend",
        fingerprint="other-extend",
        kind="extend",
        allocation_id=allocation_id,
        parameters={},
        requested_by="admin",
    )
    assert await repo.claim_operation(worker_id="other") is None


@pytest.mark.parametrize("renewal_state", ["waiting", "unknown", "running"])
async def test_recovery_rotation_can_pass_only_a_waiting_extension(store, renewal_state):
    """A lost renewal reply must not prevent moving the Host before expiry.

    An in-flight local call or an unknown internal outcome remains exclusive;
    retirement also stays blocked by an unresolved provider mutation.
    """
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store)
    renewal = await operation(repo, allocation_id, "extend")
    if renewal_state != "running":
        await repo.finish_operation(
            **fence(renewal),
            state=renewal_state,
            step="confirming_extension",
            result={"external_pending": True},
            retry_after_seconds=3600,
        )
    rotation = await repo.create_operation(
        request_id="recover-unconfirmed-renewal",
        fingerprint="recover-unconfirmed-renewal",
        kind="rotate",
        allocation_id=allocation_id,
        parameters={"trigger": "renewal_unconfirmed"},
        requested_by="compute-rotation",
    )
    claimed = await repo.claim_operation(worker_id="rotation-worker")
    if renewal_state != "waiting":
        assert claimed is None
        return
    assert claimed["id"] == rotation["id"]
    # No second provider call is dispatched while rotation itself is running.
    assert await repo.claim_operation(worker_id="other") is None
    await repo.finish_operation(
        operation_id=claimed["id"],
        generation=claimed["generation"],
        worker_id="rotation-worker",
        state="succeeded",
        step="rotated",
    )
    await repo.create_operation(
        request_id="retire-old-node",
        fingerprint="retire-old-node",
        kind="retire",
        allocation_id=allocation_id,
        parameters={},
        requested_by="compute-rotation",
    )
    assert await repo.claim_operation(worker_id="release-worker") is None


async def test_retirement_revision_check_is_atomic(store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store)
    await repo.update_policy(
        allocation_id=allocation_id, expected_revision=1, policy={"enabled": False}
    )
    with pytest.raises(ValueError, match="revision_conflict"):
        await repo.create_operation(
            request_id="old-preview",
            fingerprint="old-preview",
            kind="retire",
            allocation_id=allocation_id,
            parameters={"expected_revision": 1},
            requested_by="admin",
        )
    assert await repo.list_operations(request_id="old-preview") == []


async def test_imported_resource_is_adopted_once_without_duplicate_identity(store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store)
    observed = await repo.get_allocation(allocation_id)
    kwargs = {
        "kind": "acquire",
        "allocation": {
            "name": "adopted",
            "provider_id": "cloudlab",
            "mode": "attach",
            "external_id": observed["external_id"],
            "deployment_profile": {"id": "profile-a"},
        },
        "parameters": {},
        "requested_by": "admin",
    }
    adopted = await repo.create_allocation_operation(
        request_id="adopt", fingerprint="adopt", **kwargs
    )
    assert adopted["allocation_id"] == allocation_id
    assert len(await repo.list_allocations()) == 1
    with pytest.raises(ValueError, match="resource_already_managed"):
        await repo.create_allocation_operation(
            request_id="duplicate", fingerprint="duplicate", **kwargs
        )
    with pytest.raises(ValueError, match="deployment_profile_in_use"):
        await repo.create_allocation_operation(
            request_id="profile-collision",
            fingerprint="profile-collision",
            kind="acquire",
            allocation={**kwargs["allocation"], "external_id": "different-resource"},
            parameters={},
            requested_by="admin",
        )


async def test_admin_resolution_keeps_operation_identity_and_does_not_claim_resource_ready(store):
    repo = store.compute_resources
    op = await repo.create_allocation_operation(
        request_id="ambiguous",
        fingerprint="ambiguous",
        kind="acquire",
        allocation={"name": "unknown", "provider_id": "cloudlab", "mode": "provision"},
        parameters={},
        requested_by="admin",
    )
    claimed = await repo.claim_operation(worker_id="worker")
    await repo.finish_operation(**fence(claimed), state="unknown", step="acquiring")
    resolved = await repo.resolve_acquisition(operation_id=op["id"], external_id="explicit-uuid")
    assert resolved["id"] == op["id"] and resolved["step"] == "acquiring"
    assert resolved["result"]["resolved_external_id"] == "explicit-uuid"
    allocation = await repo.get_allocation(op["allocation_id"])
    assert (
        allocation["external_id"] == "explicit-uuid" and allocation["observed_state"] == "unknown"
    )


async def test_retirement_freezes_fork_membership_at_source_group(store, hosts):
    from tests.integration.storage.test_agent_job_store import _reserve_shared_chat

    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name="shared-runner")
    job, claim, sandbox = await _reserve_shared_chat(store)
    attempt_fence = {
        "thread_id": job["thread_id"],
        "attempt_id": claim["attempt_id"],
        "lease_generation": claim["lease_generation"],
        "sandbox_generation": sandbox["generation"],
        "activation_generation": sandbox["activation_generation"],
    }
    assert await store.sandbox.mark_thread_sandbox_running(
        **attempt_fence, container_id="fork-container", context_status="new"
    )
    assert await store.sandbox.admit_thread_sandbox_turn(
        **attempt_fence, residency_generation=sandbox["residency_generation"], job_id=job["id"]
    )
    assert await store.sandbox.settle_thread_sandbox(
        **attempt_fence,
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
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    fork = await store.session_fork.prepare_session_fork(
        source_job_id=job["id"], user_id=job["user_id"], idempotency_key="late-fork"
    )
    assert fork == {"error": "resource_admission_blocked"}


async def test_claim_and_retirement_serialize_dependency_before_release(store, hosts):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    await _create_job(store)
    op = await operation(repo, allocation_id)
    claimed, _impact = await asyncio.gather(
        _claim(store, worker_id="runner", lease_ttl_seconds=90, host="machine"),
        repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"]),
    )
    final = await repo.retirement_impact(allocation_id)
    assert claimed is None or "active_attempts_present" in final["blocked_reasons"]
    assert await _claim(store, worker_id="runner2", lease_ttl_seconds=90, host="machine") is None


async def test_existing_ssh_unmonitored_lease_can_claim_but_managed_unknown_expiry_cannot(
    db, store, hosts
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "observed_state": "ready",
            "scope_complete": True,
            "expiration_known": False,
            "configuration": {"expected_machine_identity": "verified-machine"},
        },
    )
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (False, True)
    # Existing SSH explicitly has no provider-managed lease. Unknown does not
    # pretend to be an infinite lease; it permits execution after readiness.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET provider_id='existing_ssh',lease_managed=FALSE WHERE id=$1",
            allocation_id,
        )
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (True, True)
    assert (await repo.get_allocation(allocation_id))["configuration"][
        "expected_machine_identity"
    ] == "verified-machine"
    await _create_job(store)
    assert (
        await _claim(store, worker_id="ssh-runner", lease_ttl_seconds=90, host="machine")
        is not None
    )


async def test_unknown_expiry_observation_preserves_known_hard_deadline(db, store, hosts):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "ready", "expires_at": expired, "expiration_known": True},
    )
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "unknown", "expires_at": None, "expiration_known": False},
    )
    row = await repo.get_allocation(allocation_id)
    assert row["expires_at"] == expired and not row["expiration_known"]
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (False, False)


async def test_compute_admission_uses_wall_time_in_an_aged_transaction(db, store, hosts):
    allocation_id, _, _ = await resource(store, hosts)
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_compute_allocations
            SET expires_at = clock_timestamp() + INTERVAL '400 milliseconds',
                policy = '{"stop_new_groups_before_seconds": 0,
                           "stop_claims_before_seconds": 0}'::jsonb
            WHERE id = $1
            """,
            allocation_id,
        )
    async with db.acquire() as conn, conn.transaction():
        started_at = await conn.fetchval("SELECT NOW()")
        await asyncio.sleep(0.55)
        await lock_compute_entry(conn)
        assert await conn.fetchval("SELECT NOW() = $1", started_at)
        assert await host_compute_admission(conn, "machine") == (False, False)


async def test_stale_worker_cannot_clear_retirement_or_bootstrap_hold(db, store, hosts):
    repo = store.compute_resources
    allocation_id, node, _ = await resource(store, hosts)
    op = await operation(repo, allocation_id, "inspect")
    await repo.set_admission_hold(
        allocation_id=allocation_id,
        purpose="lease_deadline",
        source_id=allocation_id,
        scope="all_claims",
        **fence(op),
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",
            op["id"],
        )
    with pytest.raises(ValueError, match="operation_lease_lost"):
        await repo.set_admission_hold(
            allocation_id=allocation_id,
            purpose="lease_deadline",
            source_id=allocation_id,
            scope=None,
            **fence(op),
        )
    with pytest.raises(ValueError, match="operation_lease_lost"):
        await repo.prepare_bootstrap_binding(node_id=node["id"], host="machine", **fence(op))
    assert (await repo.get_allocation(allocation_id))["holds"]


async def test_uncreated_allocation_releases_without_a_provider_observation(store):
    repo = store.compute_resources
    request = str(uuid4())
    created = await repo.create_allocation_operation(
        request_id=request,
        fingerprint=request,
        kind="add",
        allocation={
            "name": "never-created",
            "provider_id": "cloudlab",
            "mode": "provision",
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
        },
        parameters={},
        requested_by="admin",
    )
    claimed = await repo.claim_operation(worker_id="fixture")
    assert claimed["id"] == created["id"]
    assert await repo.finish_operation(
        operation_id=created["id"],
        generation=claimed["generation"],
        worker_id="fixture",
        state="succeeded",
        step="created",
    )
    allocation_id = created["allocation_id"]
    blocked = await repo.create_operation(
        request_id=str(uuid4()),
        fingerprint=str(uuid4()),
        kind="acquire",
        allocation_id=allocation_id,
        parameters={},
        requested_by="admin",
    )
    claimed = await repo.claim_operation(worker_id="fixture")
    assert claimed["id"] == blocked["id"]
    assert await repo.finish_operation(
        operation_id=blocked["id"],
        generation=claimed["generation"],
        worker_id="fixture",
        state="blocked",
        step="acquiring",
    )
    retire = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, **fence(retire))
    impact = await repo.retirement_impact(allocation_id)
    assert "unmanaged_scope" not in impact["blocked_reasons"]
    assert "observation_stale" not in impact["blocked_reasons"]
    assert "inventory_unverified" not in impact["blocked_reasons"]
    committed = await repo.commit_release(allocation_id=allocation_id, **fence(retire))
    assert committed["release_committed"] is True
    assert (await repo.get_operation(blocked["id"]))["state"] == "cancelled"
    row = await repo.get_allocation(allocation_id)
    assert row["external_id"] is None
    assert row["nodes"] == []
    assert row["release_committed_at"] is not None


async def test_incomplete_scope_still_blocks_release_when_a_node_exists(db, store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, provision=True)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET scope_complete=FALSE, observed_at=NULL WHERE id=$1",
            allocation_id,
        )
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, **fence(op))
    impact = await repo.retirement_impact(allocation_id)
    assert "unmanaged_scope" in impact["blocked_reasons"]
    assert "observation_stale" in impact["blocked_reasons"]


async def test_failed_bootstrap_with_reserved_unregistered_host_can_retire_by_full_node_proof(
    store,
):
    repo = store.compute_resources
    allocation_id, node, acquire = await resource(store, provision=True)
    await repo.prepare_bootstrap_binding(
        node_id=node["id"], host="never-registered", operation_id=acquire["id"]
    )
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    before = await repo.retirement_impact(allocation_id)
    assert "host_observation_stale" in before["blocked_reasons"]
    assert await repo.record_inventory(
        allocation_id=allocation_id,
        hosts=["never-registered"],
        verified_node_ids=[node["id"]],
        empty=True,
        **fence(op),
    )
    assert (await repo.commit_release(allocation_id=allocation_id, **fence(op)))[
        "release_committed"
    ]


async def test_forget_cleans_verified_terminated_history_only_after_pending_operations_finish(
    store, hosts
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    await repo.record_inventory(
        allocation_id=allocation_id, hosts=["machine"], empty=True, **fence(op)
    )
    assert (await repo.commit_release(allocation_id=allocation_id, **fence(op)))[
        "release_committed"
    ]
    await repo.observe_allocation(
        allocation_id=allocation_id, observation={"observed_state": "terminated"}, **fence(op)
    )
    assert await hosts.forget_runner_host(host="machine") == "pending_compute_operations"
    await repo.finish_operation(**fence(op), state="succeeded", step="released")
    assert await hosts.forget_runner_host(host="machine") == "deleted"
    assert (await repo.get_allocation(allocation_id))["observed_state"] == "terminated"


async def test_attached_machine_remove_and_forget_only_deregisters_local_identity(db, store, hosts):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET provider_id='existing_ssh',release_supported=FALSE,lease_managed=FALSE,management_mode='observe' WHERE id=$1",
            allocation_id,
        )
    assert await hosts.forget_runner_host(host="machine") == "resource_bound"
    op = await operation(repo, allocation_id, "inspect")
    assert await hosts.remove_host(host="machine") == "ok"
    assert await hosts.forget_runner_host(host="machine") == "pending_compute_operations"
    await repo.finish_operation(**fence(op), state="succeeded", step="inspected")
    assert await hosts.forget_runner_host(host="machine") == "deleted"
    allocation = await repo.get_allocation(allocation_id)
    assert (
        allocation["observed_state"] == "absent" and allocation["provider_state"] == "deregistered"
    )
    assert allocation["release_committed_at"] is None
    assert {op["kind"] for op in await repo.list_operations(allocation_id=allocation_id)} == {
        "add",
        "inspect",
    }


async def test_allocator_identity_is_immutable_and_resolution_cannot_replace_journalled_resource(
    store,
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store)
    with pytest.raises(ValueError, match="resource_identity_changed"):
        await repo.observe_allocation(
            allocation_id=allocation_id, observation={"external_id": "replacement"}
        )
    op = await repo.create_allocation_operation(
        request_id="known-journal",
        fingerprint="known-journal",
        kind="acquire",
        allocation={"name": "known", "provider_id": "cloudlab", "mode": "provision"},
        parameters={},
        requested_by="admin",
    )
    claimed = await repo.claim_operation(worker_id="worker")
    await repo.finish_operation(
        **fence(claimed),
        state="unknown",
        step="acquiring",
        result={"resource_ref": {"external_id": "journalled-uuid"}},
    )
    with pytest.raises(ValueError, match="operation_not_resolvable"):
        await repo.resolve_acquisition(operation_id=op["id"], external_id="unrelated-uuid")


async def test_failed_latest_inspection_stops_new_groups_before_snapshot_age_limit(
    db, store, hosts
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    deadline = (await repo.get_allocation(allocation_id))["expires_at"]
    await repo.record_observation_error(allocation_id=allocation_id, message="credentials_expired")
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (False, True)
    await _create_job(store)
    assert await _claim(store, worker_id="new-group", lease_ttl_seconds=90, host="machine") is None
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "observed_state": "ready",
            "expiration_known": True,
            "expires_at": deadline,
            "scope_complete": True,
        },
    )
    assert (
        await _claim(store, worker_id="after-recovery", lease_ttl_seconds=90, host="machine")
        is not None
    )


async def test_node_identity_validation_and_ready_observation_commit_atomically(db, store, hosts):
    import asyncpg

    repo = store.compute_resources
    allocation_id, node, _ = await resource(store, hosts)
    await repo.record_observation_error(
        allocation_id=allocation_id, message="previous_inspection_failed"
    )
    before = await repo.get_allocation(allocation_id)
    op = await operation(repo, allocation_id, "inspect")
    base = {
        "observed_state": "ready",
        "expiration_known": True,
        "scope_complete": True,
        "expires_at": datetime.now(UTC) + timedelta(hours=8),
    }
    with pytest.raises(ValueError, match="resource_identity_changed"):
        await repo.observe_allocation(
            allocation_id=allocation_id,
            observation={
                **base,
                "nodes": [
                    {
                        "provider_node_id": node["provider_node_id"],
                        "instance_identity": "replacement",
                        "name": "replacement",
                    }
                ],
            },
            **fence(op),
        )
    with pytest.raises(ValueError, match="resource_identity_changed"):
        await repo.observe_allocation(
            allocation_id=allocation_id, observation={**base, "nodes": []}, **fence(op)
        )
    # A later node write can itself fail. Even an earlier node update and the
    # readiness/lease update must roll back with it, rather than partly land.
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.observe_allocation(
            allocation_id=allocation_id,
            observation={
                **base,
                "nodes": [
                    {
                        "provider_node_id": node["provider_node_id"],
                        "instance_identity": node["instance_identity"],
                        "name": "changed-before-failure",
                    },
                    {
                        "provider_node_id": "another-node",
                        "instance_identity": node["instance_identity"],
                        "name": "identity-collision",
                    },
                ],
            },
            **fence(op),
        )
    after = await repo.get_allocation(allocation_id)
    assert (
        after["expires_at"] == before["expires_at"]
        and after["observed_at"] == before["observed_at"]
    )
    assert after["observation_error"] == "previous_inspection_failed"
    assert after["nodes"] == before["nodes"]
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (False, True)
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            **base,
            "nodes": [
                {
                    "provider_node_id": node["provider_node_id"],
                    "instance_identity": node["instance_identity"],
                    "name": "verified-name",
                }
            ],
        },
        **fence(op),
    )
    successful = await repo.get_allocation(allocation_id)
    assert successful["observation_error"] is None
    assert successful["expires_at"] == base["expires_at"]
    assert successful["nodes"][0]["name"] == "verified-name"


async def test_partial_ready_scope_does_not_reopen_new_group_admission(db, store, hosts):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "observed_state": "ready",
            "expiration_known": True,
            "expires_at": datetime.now(UTC) + timedelta(hours=4),
            "scope_complete": False,
            "nodes": [],
        },
    )
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (False, True)


async def test_committed_release_accepts_disappearing_manifest_without_rebinding(db, store, hosts):
    repo = store.compute_resources
    allocation_id, node, _ = await resource(store, hosts)
    op = await operation(repo, allocation_id)
    await repo.begin_retirement(allocation_id=allocation_id, operation_id=op["id"])
    pending = {"observed_state": "pending", "scope_complete": False, "nodes": []}
    with pytest.raises(ValueError, match="resource_identity_changed"):
        await repo.observe_allocation(allocation_id=allocation_id, observation=pending, **fence(op))
    assert await repo.record_inventory(
        allocation_id=allocation_id, hosts=["machine"], empty=True, **fence(op)
    )
    assert (await repo.commit_release(allocation_id=allocation_id, **fence(op)))[
        "release_committed"
    ]
    before = await repo.get_allocation(allocation_id)
    await repo.observe_allocation(allocation_id=allocation_id, observation=pending, **fence(op))
    after = await repo.get_allocation(allocation_id)
    assert after["observed_state"] == "pending" and not after["scope_complete"]
    assert after["nodes"] == before["nodes"]
    assert after["expires_at"] == before["expires_at"]
    async with db.acquire() as conn, conn.transaction():
        await lock_compute_entry(conn)
        assert await host_compute_admission(conn, "machine") == (False, False)
    # A partial manifest never authorizes accepting a replacement instance,
    # changing the allocation identity, or treating a complete missing node as safe.
    invalid = [
        {**pending, "external_id": "different-experiment"},
        {**pending, "scope_complete": True},
        {
            **pending,
            "nodes": [
                {"provider_node_id": node["provider_node_id"], "instance_identity": "replacement"}
            ],
        },
    ]
    for observation in invalid:
        with pytest.raises(ValueError, match="resource_identity_changed"):
            await repo.observe_allocation(
                allocation_id=allocation_id, observation=observation, **fence(op)
            )
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "observed_state": "ready",
            "scope_complete": True,
            "nodes": [
                {
                    "provider_node_id": node["provider_node_id"],
                    "instance_identity": node["instance_identity"],
                    "name": "must-not-change-after-commit",
                }
            ],
        },
        **fence(op),
    )
    assert (await repo.get_allocation(allocation_id))["nodes"] == before["nodes"]
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={**pending, "observed_state": "terminated"},
        **fence(op),
    )
    terminal = await repo.get_allocation(allocation_id)
    assert terminal["observed_state"] == "terminated"
    assert terminal["nodes"] == before["nodes"]


async def test_manual_allocation_rejects_an_autoscaling_reserved_profile(db, store):
    repo = store.compute_resources
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot) VALUES ('reserved','{"id":"reserved"}')"""
        )

    with pytest.raises(ValueError, match="deployment_profile_reserved"):
        await repo.create_allocation_operation(
            request_id="manual-reserved",
            fingerprint="manual-reserved",
            kind="acquire",
            allocation={
                "name": "manual",
                "provider_id": "cloudlab",
                "mode": "provision",
                "deployment_profile": {"id": "reserved"},
            },
            parameters={},
            requested_by="admin",
        )


async def test_default_autoscaling_snapshot_preserves_array_shapes(store):
    snapshot = await store.compute_resources.get_cloudlab_autoscaling()

    assert snapshot["runtime"]["slot_status"] == []


async def test_autoscaling_snapshot_projects_blocked_acquire_before_provider_ready(db, store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, name="provider-ready-blocked-acquire")
    acquire = await operation(repo, allocation_id, "acquire")
    assert await repo.finish_operation(
        **fence(acquire),
        state="blocked",
        step="pending",
        message="provider rejected the request",
        result={"external_pending": False},
    )
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'blocked-route','provisioning')""",
            allocation_id,
        )

    snapshot = await repo.get_cloudlab_autoscaling()

    assert snapshot["members"][0]["state"] == "blocked"
    assert snapshot["members"][0]["latest_acquire_state"] == "blocked"


async def test_autoscaling_snapshot_keeps_persisted_retiring_ahead_of_acquire_failure(db, store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, name="provider-ready-retiring")
    acquire = await operation(repo, allocation_id, "acquire")
    assert await repo.finish_operation(
        **fence(acquire),
        state="failed",
        step="pending",
        message="old acquisition failure",
        result={"external_pending": False},
    )
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'retiring-route','retiring')""",
            allocation_id,
        )

    snapshot = await repo.get_cloudlab_autoscaling()

    assert snapshot["members"][0]["state"] == "retiring"
    assert snapshot["members"][0]["latest_acquire_state"] == "failed"


async def test_terminal_observation_releases_blocked_identified_autoscale_acquire(db, store):
    repo = store.compute_resources
    allocation_id, acquire = await autoscale_acquisition(
        db,
        store,
        request_id="terminal-blocked-identified-acquire",
        result={
            "external_pending": False,
            "resource_ref": {"provider_id": "cloudlab", "external_id": "experiment-terminal"},
        },
    )

    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "external_id": "experiment-terminal",
            "observed_state": "terminated",
            "scope_complete": True,
        },
    )

    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] == "released"
    assert snapshot["profile_reservations"][0]["allocation_id"] is None
    assert (await repo.get_operation(acquire["id"]))["state"] == "blocked"
    with pytest.raises(ValueError, match="operation_not_retryable"):
        await repo.retry_operation(acquire["id"])
    with pytest.raises(ValueError, match="release_committed"):
        await repo.cancel_operation(acquire["id"])


@pytest.mark.parametrize("operation_state", ["blocked", "unknown"])
async def test_terminal_observation_releases_nonrunning_autoscale_retire_history(
    db, store, hosts, operation_state
):
    repo = store.compute_resources
    name = f"terminal-retire-{operation_state}"
    profile = name + "-route"
    allocation_id, _, _ = await resource(store, hosts, name=name)
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state) VALUES ($1,$2,'active')""",
            allocation_id,
            profile,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ($1,jsonb_build_object('id',$1::text),$2)""",
            profile,
            allocation_id,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=1,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY[$1::text]""",
            profile,
        )

    queued = await repo.queue_autoscale_retire(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        request_id=name,
        fingerprint=name,
        parameters={"allocation_id": allocation_id},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_down_idle", "allocation_id": allocation_id},
        cooldown_seconds=60,
    )
    claimed = await repo.claim_operation(worker_id="worker")
    assert claimed["id"] == queued["operation"]["id"]
    assert await repo.finish_operation(
        **fence(claimed),
        state=operation_state,
        step="release",
        result={"external_pending": operation_state == "unknown"},
    )

    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
    )

    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] == "released"
    assert snapshot["profile_reservations"][0]["allocation_id"] is None
    with pytest.raises(ValueError, match="operation_not_retryable"):
        await repo.retry_operation(claimed["id"])
    with pytest.raises(ValueError, match="release_committed"):
        await repo.cancel_operation(claimed["id"])


async def test_unknown_finish_after_terminal_observation_fences_autoscale_retire(db, store, hosts):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name="terminal-retire-finish")
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'terminal-retire-finish-route','active')""",
            allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ('terminal-retire-finish-route',
                       '{"id":"terminal-retire-finish-route"}',$1)""",
            allocation_id,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=1,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY['terminal-retire-finish-route']"""
        )

    queued = await repo.queue_autoscale_retire(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        request_id="terminal-retire-finish",
        fingerprint="terminal-retire-finish",
        parameters={"allocation_id": allocation_id},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_down_idle", "allocation_id": allocation_id},
        cooldown_seconds=60,
    )
    claimed = await repo.claim_operation(worker_id="worker")
    assert claimed["id"] == queued["operation"]["id"]
    await repo.begin_retirement(allocation_id=allocation_id, **fence(claimed))
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
        **fence(claimed),
    )
    before = await repo.get_cloudlab_autoscaling()
    assert before["profile_reservations"][0]["allocation_id"] == allocation_id
    assert before["members"][0]["state"] == "blocked"
    assert before["members"][0]["latest_operation_state"] == "running"
    assert (await repo.get_allocation(allocation_id))["release_committed_at"] is None
    assert (await hosts.get_host(host="terminal-retire-finish"))["enrolled"]

    assert await repo.finish_operation(
        **fence(claimed),
        state="unknown",
        step="checking_retention",
    )

    allocation = await repo.get_allocation(allocation_id)
    after = await repo.get_cloudlab_autoscaling()
    host = await hosts.get_host(host="terminal-retire-finish")
    assert allocation["desired_state"] == "absent"
    assert allocation["release_committed_at"] is not None
    assert host["status"] == "disabled"
    assert not host["enrolled"]
    assert after["members"][0]["state"] == "released"
    assert after["profile_reservations"][0]["allocation_id"] is None
    with pytest.raises(ValueError, match="operation_not_retryable"):
        await repo.retry_operation(claimed["id"])


async def test_natural_terminal_autoscale_observation_revokes_host_before_profile_reuse(
    db, store, hosts
):
    repo = store.compute_resources
    name = "natural-terminal-autoscale"
    profile = name + "-route"
    allocation_id, _, _ = await resource(store, hosts, name=name)
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state) VALUES ($1,$2,'active')""",
            allocation_id,
            profile,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ($1,jsonb_build_object('id',$1::text),$2)""",
            profile,
            allocation_id,
        )

        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET deployment_profiles=ARRAY[$1::text]""",
            profile,
        )
    credential_hash = hash_secret("cred-" + name)
    assert await hosts.authenticate_host(host=name, credential_hash=credential_hash)
    inspected = await operation(repo, allocation_id, "inspect")

    observed = await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
        **fence(inspected),
    )

    assert observed["desired_state"] == "absent"
    assert observed["release_committed_at"] is not None
    assert await hosts.authenticate_host(host=name, credential_hash=credential_hash) is None
    host = await hosts.get_host(host=name)
    assert host["status"] == "disabled"
    assert not host["enrolled"]
    with pytest.raises(ValueError, match="resource_retiring"):
        await hosts.register_host(host=name, credential_hash=hash_secret("replacement"))
    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] == "released"
    assert snapshot["profile_reservations"][0]["allocation_id"] is None
    assert await repo.finish_operation(**fence(inspected), state="succeeded", step="observed")


async def test_terminal_observation_fences_running_autoscale_extension(db, store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, name="terminal-autoscale-extend")
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'terminal-autoscale-extend-route','active')""",
            allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ('terminal-autoscale-extend-route',
                       '{"id":"terminal-autoscale-extend-route"}',$1)""",
            allocation_id,
        )

    extending = await operation(repo, allocation_id, "extend")
    observed = await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
        **fence(extending),
    )

    assert observed["release_committed_at"] is not None
    assert await repo.finish_operation(
        **fence(extending), state="blocked", step="observed_terminal"
    )
    with pytest.raises(ValueError, match="operation_not_retryable"):
        await repo.retry_operation(extending["id"])
    assert await repo.claim_operation(worker_id="other") is None


@pytest.mark.parametrize("release", ["autoscaled_termination", "retirement_commit"])
async def test_a_terminal_release_takes_its_hosts_attempts_before_their_host_rows(
    db, store, hosts, release
):
    """Revoking a released server's Host waits for a turn ending there, holding nothing.

    Ending an attempt -- the reaper's pass, a finish, a hand-back -- takes the
    attempt, writes its group and gives its slot back on the host row last.
    The provider's terminal answer for an autoscaled server, and a retirement
    committing once the provider has ended the server, locked the host row
    first and then brought the Host's attempt leases forward, so a turn the
    reaper was ending on that Host at the same moment deadlocked with them.
    """
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts)
    if release == "autoscaled_termination":
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_members
                   (allocation_id,deployment_profile,state)
                   VALUES ($1,'terminal-release-route','active')""",
                allocation_id,
            )
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
                   (deployment_profile,profile_snapshot,allocation_id)
                   VALUES ('terminal-release-route','{"id":"terminal-release-route"}',$1)""",
                allocation_id,
            )
    await _create_job(store)
    claim = await _claim(store, worker_id="runner", lease_ttl_seconds=90, host="machine")
    assert claim is not None
    if release == "autoscaled_termination":
        releasing_call = repo.observe_allocation(
            allocation_id=allocation_id,
            observation={"observed_state": "terminated", "scope_complete": True},
        )
    else:
        # The provider ended the server while it was being retired.
        retire = await operation(repo, allocation_id, "retire")
        assert retire is not None and retire["kind"] == "retire"
        await repo.begin_retirement(allocation_id=allocation_id, **fence(retire))
        await repo.observe_allocation(
            allocation_id=allocation_id,
            observation={"observed_state": "terminated", "scope_complete": True},
            **fence(retire),
        )
        releasing_call = repo.commit_release(allocation_id=allocation_id, **fence(retire))

    releasing = None
    async with db.acquire() as reaper:
        try:
            async with reaper.transaction():
                # The reaper ending this attempt: the attempt first.
                await reaper.execute(
                    "SELECT id FROM agent_attempts WHERE id = $1 FOR UPDATE", claim["attempt_id"]
                )
                releasing = asyncio.create_task(releasing_call)
                reaper_pid = await reaper.fetchval("SELECT pg_backend_pid()")
                async with asyncio.timeout(10):
                    while not await reaper.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE NOT granted AND $1 = ANY(pg_blocking_pids(pid)))",
                        reaper_pid,
                    ):
                        await asyncio.sleep(0.01)
                # Its slot back last, which the waiting release must not hold.
                await reaper.execute(
                    "SELECT host FROM agent_runner_hosts WHERE host = 'machine' FOR UPDATE NOWAIT"
                )
            settled = await asyncio.wait_for(releasing, timeout=10)
        finally:
            if releasing is not None and not releasing.done():
                releasing.cancel()
                await asyncio.gather(releasing, return_exceptions=True)

    assert (await repo.get_allocation(allocation_id))["release_committed_at"] is not None
    if release == "autoscaled_termination":
        snapshot = await repo.get_cloudlab_autoscaling()
        assert snapshot["members"][0]["state"] == "released"
        assert all(
            reservation["allocation_id"] != allocation_id
            for reservation in snapshot["profile_reservations"]
        )
    else:
        assert settled["release_committed"] is True
    revoked = await hosts.get_host(host="machine")
    assert revoked is not None and revoked["status"] == "disabled"


@pytest.mark.parametrize("operation_state", ["blocked", "unknown"])
async def test_terminal_observation_releases_nonrunning_autoscale_discard_history(
    db, store, operation_state
):
    repo = store.compute_resources
    profile = f"terminal-discard-{operation_state}-route"
    allocation_id, _, _ = await resource(store, name=f"terminal-discard-{operation_state}")
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state) VALUES ($1,$2,'retiring')""",
            allocation_id,
            profile,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ($1,jsonb_build_object('id',$1::text),$2)""",
            profile,
            allocation_id,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET deployment_profiles=ARRAY[$1::text]""",
            profile,
        )

    discard = await operation(repo, allocation_id, "discard")
    assert await repo.finish_operation(
        **fence(discard),
        state=operation_state,
        step="release",
        result={"external_pending": operation_state == "unknown"},
    )

    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
    )

    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] == "released"
    assert snapshot["profile_reservations"][0]["allocation_id"] is None
    with pytest.raises(ValueError, match="operation_not_retryable"):
        await repo.retry_operation(discard["id"])
    with pytest.raises(ValueError, match="release_committed"):
        await repo.cancel_operation(discard["id"])


async def test_autoscaling_policy_propagates_renewal_only_to_live_owned_allocations(db, store):
    repo = store.compute_resources
    first, _, _ = await resource(store, name="renew-owned-active")
    second, _, _ = await resource(store, name="renew-owned-retiring")
    manual, _, _ = await resource(store, name="renew-manual")
    old_policy = {"enabled": False, "reason": "allocation-specific"}
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_allocations
               SET policy='{"enabled":false,"reason":"allocation-specific"}'::jsonb
               WHERE id=ANY($1::text[])""",
            [first, second, manual],
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'renew-route-one','active'),
                      ($2,'renew-route-two','retiring')""",
            first,
            second,
        )
    renewal = {
        "enabled": True,
        "renew_before_seconds": 7200,
        "extend_to_horizon_seconds": 28800,
        "stop_new_groups_before_seconds": 3600,
        "stop_claims_before_seconds": 600,
        "reason": "keep autoscaled staging capacity available",
        "rotation_enabled": False,
        "max_node_age_seconds": 604800,
        "node_types": [],
        "node_type_binding": "phystype",
    }
    policy = {
        "enabled": False,
        "min_hosts": 0,
        "max_hosts": 2,
        "connection_id": None,
        "ssh_connection_id": None,
        "configuration": {},
        "deployment_profiles": ["renew-route-one", "renew-route-two"],
        "sandbox_storage_path": None,
        "renewal_policy": renewal,
        "slot_policy": {},
        "scale_up_sustain_seconds": 120,
        "scale_down_idle_seconds": 3600,
        "cooldown_seconds": 600,
        "scale_down_enabled": False,
    }

    saved = await repo.update_cloudlab_autoscaling(
        expected_revision=0,
        policy=policy,
        profile_snapshots={
            "renew-route-one": {"id": "renew-route-one"},
            "renew-route-two": {"id": "renew-route-two"},
        },
        requested_by="admin",
    )

    assert saved["policy"]["renewal_policy"] == renewal
    assert (await repo.get_allocation(first))["policy"] == renewal
    assert (await repo.get_allocation(second))["policy"] == renewal
    assert (await repo.get_allocation(manual))["policy"] == old_policy


async def test_terminal_owned_profile_removed_from_policy_returns_to_manual_pool(db, store):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, name="autoscaled")
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_allocations
               SET deployment_profile='{"id":"released-route"}'::jsonb WHERE id=$1""",
            allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile) VALUES ($1,'released-route')""",
            allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ('released-route','{"id":"released-route"}',$1)""",
            allocation_id,
        )

    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
    )

    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] == "released"
    assert snapshot["profile_reservations"] == []
    operation = await repo.create_allocation_operation(
        request_id="manual-after-terminal",
        fingerprint="manual-after-terminal",
        kind="acquire",
        allocation={
            "name": "manual replacement",
            "provider_id": "cloudlab",
            "mode": "provision",
            "deployment_profile": {"id": "released-route"},
        },
        parameters={},
        requested_by="admin",
    )
    assert operation["allocation_id"] != allocation_id


@pytest.mark.parametrize(
    ("lifecycle_kind", "finish_state"),
    [
        ("acquire", "succeeded"),
        ("acquire", "failed"),
        ("retire", "succeeded"),
        ("discard", "succeeded"),
    ],
)
async def test_terminal_profile_stays_reserved_until_lifecycle_operation_finishes(
    db, store, lifecycle_kind, finish_state
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, name="autoscaled-retiring")
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_allocations
               SET deployment_profile='{"id":"retiring-route"}'::jsonb WHERE id=$1""",
            allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'retiring-route','retiring')""",
            allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ('retiring-route','{"id":"retiring-route"}',$1)""",
            allocation_id,
        )

    lifecycle = await operation(repo, allocation_id, lifecycle_kind)
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
        **fence(lifecycle),
    )

    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["profile_reservations"][0]["allocation_id"] == allocation_id
    with pytest.raises(ValueError, match="deployment_profile_reserved"):
        await repo.create_allocation_operation(
            request_id="manual-before-retire-completes",
            fingerprint="manual-before-retire-completes",
            kind="acquire",
            allocation={
                "name": "premature replacement",
                "provider_id": "cloudlab",
                "mode": "provision",
                "deployment_profile": {"id": "retiring-route"},
            },
            parameters={},
            requested_by="admin",
        )

    assert await repo.finish_operation(
        operation_id=lifecycle["id"],
        generation=lifecycle["generation"],
        worker_id="worker",
        state=finish_state,
        step="released",
    )
    assert (await repo.get_cloudlab_autoscaling())["profile_reservations"] == []
    if lifecycle_kind == "acquire" and finish_state == "failed":
        with pytest.raises(ValueError, match="operation_not_retryable"):
            await repo.retry_operation(lifecycle["id"])
        assert (await repo.get_operation(lifecycle["id"]))["state"] == "failed"

    replacement = await repo.create_allocation_operation(
        request_id="manual-after-retire-completes",
        fingerprint="manual-after-retire-completes",
        kind="acquire",
        allocation={
            "name": "safe replacement",
            "provider_id": "cloudlab",
            "mode": "provision",
            "deployment_profile": {"id": "retiring-route"},
        },
        parameters={},
        requested_by="admin",
    )
    assert replacement["allocation_id"] != allocation_id


async def test_autoscale_retire_rechecks_busy_host_under_lock(db, store, hosts):
    repo = store.compute_resources
    first, _, _ = await resource(store, hosts, name="autoscale-one")
    second, _, _ = await resource(store, hosts, name="autoscale-two")
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=1,max_hosts=2,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY['route-one','route-two']"""
        )
        for allocation_id, profile in ((first, "route-one"), (second, "route-two")):
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_members
                   (allocation_id,deployment_profile,state) VALUES ($1,$2,'active')""",
                allocation_id,
                profile,
            )
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
                   (deployment_profile,profile_snapshot,allocation_id)
                   VALUES ($1,jsonb_build_object('id',$1::text),$2)""",
                profile,
                allocation_id,
            )
        await conn.execute("UPDATE agent_runner_hosts SET slots_used=1 WHERE host='autoscale-two'")

    with pytest.raises(ValueError, match="autoscaling_member_not_idle"):
        await repo.queue_autoscale_retire(
            expected_revision=0,
            expected_generation=0,
            allocation_id=second,
            request_id="autoscale-retire-busy",
            fingerprint="autoscale-retire-busy",
            parameters={"allocation_id": second},
            requested_by="system:cloudlab-autoscaler",
            decision={"action": "scale_down_idle", "allocation_id": second},
            cooldown_seconds=60,
        )
    assert await repo.list_operations(allocation_id=second, kinds=["retire"]) == []


async def test_unchanged_enabled_policy_can_pause_with_unverified_connections(db, store):
    repo = store.compute_resources
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_compute_connections
               (id,name,provider_id,secret_ciphertext,status)
               VALUES ('pause-cloudlab','CloudLab','cloudlab','ciphertext','unverified'),
                      ('pause-ssh','SSH','existing_ssh','ciphertext','unverified')"""
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=1,max_hosts=1,
                   connection_id='pause-cloudlab',ssh_connection_id='pause-ssh',
                   configuration='{"project":"research"}'::jsonb,
                   deployment_profiles=ARRAY['pause-route']"""
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot)
               VALUES ('pause-route','{"id":"pause-route"}')"""
        )
    paused_policy = {
        "enabled": False,
        "min_hosts": 1,
        "max_hosts": 1,
        "connection_id": "pause-cloudlab",
        "ssh_connection_id": "pause-ssh",
        "configuration": {"project": "research"},
        "deployment_profiles": ["pause-route"],
        "sandbox_storage_path": None,
        "renewal_policy": {},
        "slot_policy": {},
        "scale_up_sustain_seconds": 120,
        "scale_down_idle_seconds": 3600,
        "cooldown_seconds": 600,
        "scale_down_enabled": False,
    }

    saved = await repo.update_cloudlab_autoscaling(
        expected_revision=0,
        policy=paused_policy,
        profile_snapshots={},
        requested_by="admin",
    )

    assert saved["policy"]["enabled"] is False
    assert saved["policy"]["revision"] == 1
    assert [reservation["deployment_profile"] for reservation in saved["profile_reservations"]] == [
        "pause-route"
    ]
    with pytest.raises(ValueError, match="autoscaling_connection_unverified"):
        await repo.update_cloudlab_autoscaling(
            expected_revision=1,
            policy={
                **paused_policy,
                "configuration": {"project": "changed-while-paused"},
            },
            profile_snapshots={"pause-route": {"id": "pause-route"}},
            requested_by="admin",
        )


@pytest.mark.parametrize("degraded_connection", ["scale-cloudlab", "scale-ssh"])
async def test_autoscale_create_rechecks_connection_health_after_enable(
    db, store, degraded_connection
):
    repo = store.compute_resources
    for connection_id, provider_id in (
        ("scale-cloudlab", "cloudlab"),
        ("scale-ssh", "existing_ssh"),
    ):
        await repo.save_connection(
            connection_id=connection_id,
            name=connection_id,
            provider_id=provider_id,
            configuration={},
            secret_ciphertext="ciphertext",
            masked_summary="configured",
            status="verified",
        )
    policy = {
        "enabled": True,
        "min_hosts": 0,
        "max_hosts": 1,
        "connection_id": "scale-cloudlab",
        "ssh_connection_id": "scale-ssh",
        "configuration": {},
        "deployment_profiles": ["scale-route"],
        "sandbox_storage_path": None,
        "renewal_policy": {},
        "slot_policy": {},
        "scale_up_sustain_seconds": 120,
        "scale_down_idle_seconds": 3600,
        "cooldown_seconds": 600,
        "scale_down_enabled": False,
    }
    await repo.update_cloudlab_autoscaling(
        expected_revision=0,
        policy=policy,
        profile_snapshots={"scale-route": {"id": "scale-route"}},
        requested_by="admin",
    )
    # Simulate a legacy/degraded row that predates the update fence.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_connections SET status='unverified' WHERE id=$1",
            degraded_connection,
        )

    with pytest.raises(ValueError, match="autoscaling_connection_unverified"):
        await repo.create_autoscale_allocation_operation(
            expected_revision=1,
            expected_generation=1,
            request_id=f"degraded-{degraded_connection}",
            fingerprint=f"degraded-{degraded_connection}",
            allocation={"name": "must not provision"},
            parameters={},
            requested_by="system:cloudlab-autoscaler",
            decision={"action": "scale_up"},
            cooldown_seconds=60,
        )

    assert await repo.list_allocations() == []
    assert (await repo.get_cloudlab_autoscaling())["profile_reservations"][0][
        "allocation_id"
    ] is None


async def test_connection_rotation_requires_pause_and_blocked_acquire(db, store):
    repo = store.compute_resources
    for connection_id, provider_id in (
        ("rotate-cloudlab", "cloudlab"),
        ("rotate-ssh", "existing_ssh"),
    ):
        await repo.save_connection(
            connection_id=connection_id,
            name=connection_id,
            provider_id=provider_id,
            configuration={},
            secret_ciphertext="ciphertext-v1",
            masked_summary="configured",
            status="verified",
        )
    policy = {
        "enabled": True,
        "min_hosts": 0,
        "max_hosts": 1,
        "connection_id": "rotate-cloudlab",
        "ssh_connection_id": "rotate-ssh",
        "configuration": {},
        "deployment_profiles": ["rotate-route"],
        "sandbox_storage_path": None,
        "renewal_policy": {},
        "slot_policy": {},
        "scale_up_sustain_seconds": 120,
        "scale_down_idle_seconds": 3600,
        "cooldown_seconds": 600,
        "scale_down_enabled": False,
    }
    await repo.update_cloudlab_autoscaling(
        expected_revision=0,
        policy=policy,
        profile_snapshots={"rotate-route": {"id": "rotate-route"}},
        requested_by="admin",
    )
    queued = await repo.create_autoscale_allocation_operation(
        expected_revision=1,
        expected_generation=1,
        request_id="rotate-before-provider",
        fingerprint="rotate-before-provider",
        allocation={"name": "queued autoscale"},
        parameters={},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_up"},
        cooldown_seconds=60,
    )
    claimed = await repo.claim_operation(worker_id="worker")
    assert claimed["id"] == queued["operation"]["id"]
    with pytest.raises(ValueError, match="autoscaling_connection_in_use"):
        await repo.save_connection(
            connection_id="rotate-cloudlab",
            name="rotated",
            provider_id="cloudlab",
            configuration={},
            secret_ciphertext="ciphertext-v2",
            masked_summary="configured",
            status="unverified",
            expected_revision=1,
        )

    await repo.update_cloudlab_autoscaling(
        expected_revision=1,
        policy={**policy, "enabled": False},
        profile_snapshots={},
        requested_by="admin",
    )
    # Pausing allows repair only after the pre-submit operation is no longer
    # pending or running, closing the check-to-provider-call rotation window.
    with pytest.raises(ValueError, match="autoscaling_connection_in_use"):
        await repo.save_connection(
            connection_id="rotate-cloudlab",
            name="rotated",
            provider_id="cloudlab",
            configuration={},
            secret_ciphertext="ciphertext-v2",
            masked_summary="configured",
            status="unverified",
            expected_revision=1,
        )
    assert await repo.finish_operation(
        **fence(claimed),
        state="blocked",
        step="pending",
        result={"external_pending": False},
    )
    repaired = await repo.save_connection(
        connection_id="rotate-cloudlab",
        name="rotated",
        provider_id="cloudlab",
        configuration={},
        secret_ciphertext="ciphertext-v2",
        masked_summary="configured",
        status="unverified",
        expected_revision=1,
    )
    assert repaired["revision"] == 2
    assert repaired["status"] == "unverified"


async def test_terminal_failed_acquire_history_does_not_block_new_autoscale_create(db, store):
    repo = store.compute_resources
    old_allocation, _, _ = await resource(store, name="old-autoscaled")
    failed_acquire = await operation(repo, old_allocation, "acquire")
    assert await repo.finish_operation(
        **fence(failed_acquire),
        state="failed",
        step="failed",
        message="provider confirmed failure",
    )
    await repo.observe_allocation(
        allocation_id=old_allocation,
        observation={"observed_state": "terminated", "scope_complete": True},
    )
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'old-route','released')""",
            old_allocation,
        )
        await conn.execute(
            """INSERT INTO agent_compute_connections
               (id,name,provider_id,secret_ciphertext,status)
               VALUES ('replacement-cloudlab','CloudLab','cloudlab','ciphertext','verified'),
                      ('replacement-ssh','SSH','existing_ssh','ciphertext','verified')"""
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,max_hosts=1,
                   connection_id='replacement-cloudlab',ssh_connection_id='replacement-ssh',
                   deployment_profiles=ARRAY['replacement-route']"""
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot)
               VALUES ('replacement-route','{"id":"replacement-route"}')"""
        )

    created = await repo.create_autoscale_allocation_operation(
        expected_revision=0,
        expected_generation=0,
        request_id="autoscale-after-terminal-failure",
        fingerprint="autoscale-after-terminal-failure",
        allocation={"name": "replacement"},
        parameters={},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_up"},
        cooldown_seconds=60,
    )

    assert created["operation"]["kind"] == "acquire"
    assert created["operation"]["allocation_id"] != old_allocation


async def test_terminal_blocked_operation_history_does_not_block_autoscale_retire(db, store, hosts):
    repo = store.compute_resources
    old_allocation, _, _ = await resource(store, name="old-blocked")
    blocked_retire = await operation(repo, old_allocation, "retire")
    assert await repo.finish_operation(
        **fence(blocked_retire),
        state="blocked",
        step="release",
        message="administrator review required",
    )
    await repo.observe_allocation(
        allocation_id=old_allocation,
        observation={"observed_state": "terminated", "scope_complete": True},
    )
    candidate, _, _ = await resource(store, hosts, name="healthy-candidate")
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'old-route','released'),($2,'healthy-route','active')""",
            old_allocation,
            candidate,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ('healthy-route','{"id":"healthy-route"}',$1)""",
            candidate,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=1,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY['healthy-route']"""
        )

    queued = await repo.queue_autoscale_retire(
        expected_revision=0,
        expected_generation=0,
        allocation_id=candidate,
        request_id="retire-after-terminal-history",
        fingerprint="retire-after-terminal-history",
        parameters={"allocation_id": candidate},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_down_idle", "allocation_id": candidate},
        cooldown_seconds=60,
    )

    assert queued["operation"]["kind"] == "retire"
    assert queued["operation"]["allocation_id"] == candidate


async def test_manual_and_autoscale_retire_creation_serialize(db, store, hosts):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name="retire-race")
    allocation = await repo.get_allocation(allocation_id)
    assert allocation is not None
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'retire-race-route','active')""",
            allocation_id,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=1,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY['retire-race-route']"""
        )

    manual, automatic = await asyncio.gather(
        repo.create_operation(
            request_id="manual-retire-race",
            fingerprint="manual-retire-race",
            kind="retire",
            allocation_id=allocation_id,
            parameters={"expected_revision": allocation["revision"]},
            requested_by="admin",
        ),
        repo.queue_autoscale_retire(
            expected_revision=0,
            expected_generation=0,
            allocation_id=allocation_id,
            request_id="autoscale-retire-race",
            fingerprint="autoscale-retire-race",
            parameters={"allocation_id": allocation_id},
            requested_by="system:cloudlab-autoscaler",
            decision={"action": "scale_down_idle", "allocation_id": allocation_id},
            cooldown_seconds=60,
        ),
        return_exceptions=True,
    )

    outcomes = (manual, automatic)
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    refused = next(outcome for outcome in outcomes if isinstance(outcome, Exception))
    assert str(refused) in {"operation_in_flight", "autoscaling_operation_in_flight"}
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                """SELECT count(*) FROM agent_compute_operations
                   WHERE allocation_id=$1 AND kind='retire'
                     AND state IN ('pending','running','waiting','blocked','unknown')""",
                allocation_id,
            )
            == 1
        )


async def test_live_blocked_extend_on_other_member_does_not_block_autoscale_retire(
    db, store, hosts
):
    repo = store.compute_resources
    other, _, _ = await resource(store, name="live-blocked-extend")
    candidate, _, _ = await resource(store, hosts, name="retire-candidate")
    blocked_extend = await operation(repo, other, "extend")
    assert await repo.finish_operation(
        **fence(blocked_extend),
        state="blocked",
        step="extension_rejected",
        message="extension is unavailable",
        result={"external_pending": False},
    )
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state)
               VALUES ($1,'extend-route','active'),($2,'retire-route','active')""",
            other,
            candidate,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=2,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY['extend-route','retire-route']"""
        )

    queued = await repo.queue_autoscale_retire(
        expected_revision=0,
        expected_generation=0,
        allocation_id=candidate,
        request_id="retire-with-unrelated-blocked-extend",
        fingerprint="retire-with-unrelated-blocked-extend",
        parameters={"allocation_id": candidate},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_down_idle", "allocation_id": candidate},
        cooldown_seconds=60,
    )

    assert queued["operation"]["kind"] == "retire"
    assert queued["operation"]["allocation_id"] == candidate


@pytest.mark.parametrize(
    ("observed_state", "unbind_host", "expected_state", "cancellable"),
    [
        ("ready", True, "active", True),
        ("pending", False, "provisioning", True),
        ("terminated", False, "blocked", False),
    ],
    ids=["ready-unbound", "provider-not-ready", "provider-terminated"],
)
async def test_cancel_autoscale_retire_restores_owned_member_capacity_state(
    db, store, hosts, observed_state, unbind_host, expected_state, cancellable
):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name=f"cancel-retire-{expected_state}")
    profile = f"cancel-retire-{expected_state}-route"
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state) VALUES ($1,$2,'active')""",
            allocation_id,
            profile,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ($1,jsonb_build_object('id',$1::text),$2)""",
            profile,
            allocation_id,
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=1,scale_down_enabled=TRUE,
                   deployment_profiles=ARRAY[$1::text]""",
            profile,
        )

    queued = await repo.queue_autoscale_retire(
        expected_revision=0,
        expected_generation=0,
        allocation_id=allocation_id,
        request_id=f"cancel-retire-{expected_state}",
        fingerprint=f"cancel-retire-{expected_state}",
        parameters={"allocation_id": allocation_id},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_down_idle", "allocation_id": allocation_id},
        cooldown_seconds=60,
    )
    assert queued["operation"]["kind"] == "retire"

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET observed_state=$2 WHERE id=$1",
            allocation_id,
            observed_state,
        )
        if unbind_host:
            await conn.execute(
                "UPDATE agent_runner_hosts SET compute_node_id=NULL WHERE host=$1",
                f"cancel-retire-{expected_state}",
            )

    if not cancellable:
        with pytest.raises(ValueError, match="operation_not_cancellable"):
            await repo.cancel_operation(queued["operation"]["id"])
        allocation = await repo.get_allocation(allocation_id)
        assert allocation["desired_state"] == "present"
        assert allocation["release_committed_at"] is None
        assert (await hosts.get_host(host=f"cancel-retire-{expected_state}"))["enrolled"]
        snapshot = await repo.get_cloudlab_autoscaling()
        assert snapshot["members"][0]["state"] == "blocked"
        assert snapshot["profile_reservations"][0]["allocation_id"] == allocation_id
        return

    cancelled = await repo.cancel_operation(queued["operation"]["id"])

    assert cancelled["state"] == "cancelled"
    assert (await repo.get_allocation(allocation_id))["desired_state"] == "present"
    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] == expected_state
    reservation = snapshot["profile_reservations"][0]
    assert reservation["allocation_id"] == allocation_id
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM agent_cloudlab_autoscaling_members WHERE allocation_id=$1",
                allocation_id,
            )
            == expected_state
        )


@pytest.mark.parametrize("operation_state", ["blocked", "waiting", "failed"])
async def test_cancel_definite_pre_submit_autoscale_rejection_releases_profile(
    db, store, operation_state
):
    repo = store.compute_resources
    allocation_id, operation_row = await autoscale_acquisition(
        db,
        store,
        request_id=f"cancel-definite-autoscale-{operation_state}",
        state=operation_state,
        result={"external_pending": False},
    )

    cancelled = await repo.cancel_operation(operation_row["id"])

    assert cancelled["state"] == "cancelled"
    allocation = await repo.get_allocation(allocation_id)
    assert allocation["desired_state"] == "absent"
    assert allocation["observed_state"] == "absent"
    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] == "released"
    assert snapshot["profile_reservations"][0]["allocation_id"] is None


@pytest.mark.parametrize(
    ("state", "result", "external_id", "expected_error"),
    [
        (
            "unknown",
            {"external_pending": False},
            None,
            "operation_not_cancellable",
        ),
        (
            "blocked",
            {"external_pending": True},
            None,
            "operation_not_cancellable",
        ),
        (
            "blocked",
            {
                "external_pending": False,
                "resource_ref": {"provider_id": "cloudlab", "external_id": "experiment-a"},
            },
            None,
            "operation_not_cancellable",
        ),
        (
            "blocked",
            {"external_pending": False},
            "experiment-a",
            "operation_not_cancellable",
        ),
    ],
    ids=["unknown", "external-pending", "receipt-identity", "allocation-identity"],
)
async def test_unsafe_autoscale_acquisitions_cannot_be_cancelled(
    db, store, state, result, external_id, expected_error
):
    repo = store.compute_resources
    allocation_id, operation_row = await autoscale_acquisition(
        db,
        store,
        request_id=f"unsafe-cancel-{state}-{external_id or bool(result.get('resource_ref'))}",
        state=state,
        result=result,
    )
    if external_id is not None:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_compute_allocations SET external_id=$2 WHERE id=$1",
                allocation_id,
                external_id,
            )

    with pytest.raises(ValueError, match=expected_error):
        await repo.cancel_operation(operation_row["id"])

    assert (await repo.get_operation(operation_row["id"]))["state"] == state
    allocation = await repo.get_allocation(allocation_id)
    assert allocation["desired_state"] == "present"
    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["members"][0]["state"] != "released"
    assert snapshot["profile_reservations"][0]["allocation_id"] == allocation_id


async def test_running_autoscale_acquisition_cannot_be_cancelled(db, store):
    repo = store.compute_resources
    _allocation_id, operation_row = await autoscale_acquisition(
        db,
        store,
        request_id="cancel-running-autoscale-acquire",
        state=None,
        result={"external_pending": False},
    )

    with pytest.raises(ValueError, match="operation_in_flight"):
        await repo.cancel_operation(operation_row["id"])


async def test_retiring_an_autoscaled_server_mid_acquisition_frees_its_route(db, store):
    """An administrator retires an autoscaled server whose Host never heartbeat.

    Its acquisition waits between checks for the deployed Host, and the
    retirement is claimed in that window. Once the release commits the
    dispatcher never claims the acquisition again, so it stayed waiting for
    good, and the route it reserved stayed reserved with it: the autoscaler
    could not deploy another member there.
    """
    repo = store.compute_resources
    allocation_id, acquire = await autoscale_acquisition(
        db, store, request_id="retired-mid-acquisition", state=None
    )
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={
            "external_id": "experiment-retired-mid-acquisition",
            "observed_state": "ready",
            "expiration_known": True,
            "expires_at": datetime.now(UTC) + timedelta(hours=4),
            "scope_complete": True,
            "nodes": [
                {"provider_node_id": "node-1", "instance_identity": "vm-1", "name": "node-1"}
            ],
        },
        **fence(acquire),
    )
    assert await repo.checkpoint_operation(
        **fence(acquire), step="verifying_host", result={"external_pending": False}
    )
    assert await repo.finish_operation(
        **fence(acquire),
        state="waiting",
        step="waiting_for_heartbeat",
        message="Waiting for the deployed Host to register and heartbeat.",
        retry_after_seconds=15,
    )

    retire = await operation(repo, allocation_id, "retire")
    assert retire is not None and retire["kind"] == "retire"
    await repo.begin_retirement(allocation_id=allocation_id, **fence(retire))
    nodes = (await repo.get_allocation(allocation_id))["nodes"]
    assert await repo.record_inventory(
        allocation_id=allocation_id,
        hosts=[],
        empty=True,
        verified_node_ids=[node["id"] for node in nodes],
        **fence(retire),
    )
    committed = await repo.commit_release(allocation_id=allocation_id, **fence(retire))
    assert committed["release_committed"] is True
    await repo.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": "terminated", "scope_complete": True},
        **fence(retire),
    )
    assert await repo.finish_operation(**fence(retire), state="succeeded", step="released")

    snapshot = await repo.get_cloudlab_autoscaling()
    assert snapshot["profile_reservations"][0]["allocation_id"] is None
    assert snapshot["members"][0]["state"] == "released"
    # Nothing can run the acquisition now; it ends with its server.
    assert (await repo.get_operation(acquire["id"]))["state"] == "cancelled"
    replacement = await repo.create_autoscale_allocation_operation(
        expected_revision=0,
        expected_generation=snapshot["runtime"]["generation"],
        request_id="after-retired-mid-acquisition",
        fingerprint="after-retired-mid-acquisition",
        allocation={"name": "replacement"},
        parameters={},
        requested_by="system:cloudlab-autoscaler",
        decision={"action": "scale_up"},
        cooldown_seconds=60,
    )
    assert replacement["operation"]["allocation_id"] != allocation_id


async def test_a_server_released_while_its_upgrade_waits_holds_back_no_later_release(db, store):
    """A release commit cancels the server's waiting upgrade, which nothing can run after it.

    Every control-plane release queues an upgrade for each managed Host, and a
    retirement does not wait for one that is between its checks. After the
    commit the dispatcher claims only retirements, inspections and discards,
    so that upgrade stayed waiting for good, and its deployment profile read as
    one being installed: no later release moved it.
    """
    repo = store.compute_resources
    await repo.save_deployment_profile(
        profile_id="profile-shared",
        label="profile-shared",
        configuration={"deployment_sha": OLD_SHA, "deploy_root": "/opt/profile-shared"},
        source_bundle=b"shared-old",
        source_sha256=OLD_DIGEST,
        secret_ciphertext="cipher-profile-shared",
    )
    snapshot = _profile_snapshot(
        "profile-shared", OLD_SHA, OLD_DIGEST, "stored-profile:profile-shared:bundle"
    )
    retired, _, _ = await resource(store, name="retired-mid-upgrade")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET deployment_profile=$2::jsonb WHERE id=$1",
            retired,
            json.dumps(snapshot),
        )
    upgrade = await operation(repo, retired, "upgrade")
    assert upgrade is not None and upgrade["kind"] == "upgrade"
    assert await repo.finish_operation(
        **fence(upgrade),
        state="waiting",
        step="waiting_for_generation",
        message="Waiting for the Host to advertise the release.",
        retry_after_seconds=600,
    )

    retire = await operation(repo, retired, "retire")
    assert retire is not None and retire["kind"] == "retire"
    await repo.begin_retirement(allocation_id=retired, **fence(retire))
    nodes = (await repo.get_allocation(retired))["nodes"]
    assert await repo.record_inventory(
        allocation_id=retired,
        hosts=[],
        empty=True,
        verified_node_ids=[node["id"] for node in nodes],
        **fence(retire),
    )
    committed = await repo.commit_release(allocation_id=retired, **fence(retire))
    assert committed["release_committed"] is True
    await repo.observe_allocation(
        allocation_id=retired,
        observation={"observed_state": "terminated", "scope_complete": True},
        **fence(retire),
    )
    assert await repo.finish_operation(**fence(retire), state="succeeded", step="released")

    # The profile serves the next server; a later release has to move it.
    current = await _ready_allocation(repo, "current-on-shared", snapshot)
    rollout = await operation(repo, current, "upgrade")
    assert rollout is not None and rollout["allocation_id"] == current
    await repo.advance_host_release(
        allocation_id=current,
        previous_sha=OLD_SHA,
        previous_digest=OLD_DIGEST,
        deployment_sha=NEW_SHA,
        source_sha256=NEW_DIGEST,
        source_bundle=b"replacement",
        release_bundle_path="/app/compute-host-release/source.tar.gz",
        **fence(rollout),
    )
    assert (await repo.get_deployment_profile("profile-shared"))["source_bundle"] == b"replacement"
    # Nothing can run the upgrade now; it ends with its server.
    assert (await repo.get_operation(upgrade["id"]))["state"] == "cancelled"


OLD_SHA = "d" * 40
NEW_SHA = "e" * 40
OTHER_SHA = "f" * 40
OLD_DIGEST = "a" * 64
NEW_DIGEST = "b" * 64
OTHER_DIGEST = "c" * 64


def _profile_snapshot(profile_id: str, sha: str, digest: str, bundle: str) -> dict:
    return {
        "id": profile_id,
        "label": profile_id,
        "deployment_sha": sha,
        "source_sha256": digest,
        "source_bundle": bundle,
        "deploy_root": "/opt/" + profile_id,
    }


async def _ready_allocation(repo, name: str, profile: dict, *, state: str = "ready") -> str:
    request = str(uuid4())
    created = await repo.create_allocation_operation(
        request_id=request,
        fingerprint=request,
        kind="add",
        allocation={
            "name": name,
            "provider_id": "existing_ssh",
            "mode": "attach",
            "management_mode": "manage",
            "deployment_profile": profile,
        },
        parameters={},
        requested_by="admin",
    )
    claimed = await repo.claim_operation(worker_id="fixture")
    assert claimed is not None and claimed["id"] == created["id"]
    await repo.upsert_node(
        allocation_id=created["allocation_id"],
        node={"provider_node_id": name, "instance_identity": request, "name": name},
    )
    await repo.observe_allocation(
        allocation_id=created["allocation_id"],
        observation={
            "external_id": request,
            "observed_state": state,
            "expiration_known": False,
            "scope_complete": True,
        },
    )
    assert await repo.finish_operation(
        operation_id=created["id"],
        generation=claimed["generation"],
        worker_id="fixture",
        state="succeeded",
        step="ready",
    )
    return created["allocation_id"]


async def test_host_release_moves_shared_profiles_and_live_snapshots(store):
    """One release moves together, including its spare, and leaves other work alone."""
    repo = store.compute_resources
    profiles = {
        "profile-current": b"current-old",
        "profile-spare": b"spare-old",
        "profile-inflight": b"inflight-old",
        "profile-retired": b"retired-old",
        "profile-other": b"other-old",
    }
    for profile_id, bundle in profiles.items():
        sha = OTHER_SHA if profile_id == "profile-other" else OLD_SHA
        digest = OTHER_DIGEST if profile_id == "profile-other" else OLD_DIGEST
        await repo.save_deployment_profile(
            profile_id=profile_id,
            label=profile_id,
            configuration={"deployment_sha": sha, "deploy_root": "/opt/" + profile_id},
            source_bundle=bundle,
            source_sha256=digest,
            secret_ciphertext="cipher-" + profile_id,
        )
    current = await _ready_allocation(
        repo,
        "current",
        _profile_snapshot(
            "profile-current", OLD_SHA, OLD_DIGEST, "stored-profile:profile-current:bundle"
        ),
    )
    file_backed = await _ready_allocation(
        repo,
        "file-backed",
        _profile_snapshot("file-backed", OLD_SHA, OLD_DIGEST, "/private/host.tar.gz"),
    )
    inflight = await _ready_allocation(
        repo,
        "inflight",
        _profile_snapshot(
            "profile-inflight", OLD_SHA, OLD_DIGEST, "stored-profile:profile-inflight:bundle"
        ),
    )
    other = await _ready_allocation(
        repo,
        "other",
        _profile_snapshot("profile-other", OTHER_SHA, OTHER_DIGEST, "/private/other.tar.gz"),
    )
    retired = await _ready_allocation(
        repo,
        "retired",
        _profile_snapshot(
            "profile-retired", OLD_SHA, OLD_DIGEST, "stored-profile:profile-retired:bundle"
        ),
        state="terminated",
    )
    acquire = await operation(repo, inflight, "acquire")
    assert acquire["allocation_id"] == inflight
    assert acquire["kind"] == "acquire"
    upgrade = await operation(repo, current, "upgrade")
    assert upgrade["allocation_id"] == current
    assert upgrade["kind"] == "upgrade"
    before = await repo.get_deployment_profile("profile-current")
    with pytest.raises(ValueError, match="host_release_not_recorded"):
        await repo.advance_host_release(
            allocation_id=current,
            previous_sha=OLD_SHA,
            previous_digest="0" * 64,
            deployment_sha=NEW_SHA,
            source_sha256=NEW_DIGEST,
            source_bundle=b"replacement",
            release_bundle_path="/app/compute-host-release/source.tar.gz",
            **fence(upgrade),
        )
    assert (await repo.get_deployment_profile("profile-current"))["source_bundle"] == b"current-old"

    await repo.advance_host_release(
        allocation_id=current,
        previous_sha=OLD_SHA,
        previous_digest=OLD_DIGEST,
        deployment_sha=NEW_SHA,
        source_sha256=NEW_DIGEST,
        source_bundle=b"replacement",
        release_bundle_path="/app/compute-host-release/source.tar.gz",
        **fence(upgrade),
    )

    async def stored(profile_id: str) -> dict:
        return await repo.get_deployment_profile(profile_id)

    moved = await stored("profile-current")
    spare = await stored("profile-spare")
    assert moved["source_bundle"] == spare["source_bundle"] == b"replacement"
    assert moved["source_sha256"] == spare["source_sha256"] == NEW_DIGEST
    assert moved["configuration"]["deployment_sha"] == NEW_SHA
    assert moved["configuration"]["deploy_root"] == "/opt/profile-current"
    assert moved["secret_ciphertext"] == "cipher-profile-current"
    assert moved["revision"] == before["revision"] + 1
    assert (await stored("profile-inflight"))["source_bundle"] == b"inflight-old"
    assert (await stored("profile-inflight"))["source_sha256"] == OLD_DIGEST
    assert (await stored("profile-retired"))["source_bundle"] == b"replacement"
    assert (await stored("profile-other"))["source_bundle"] == b"other-old"
    assert (await stored("profile-other"))["configuration"]["deployment_sha"] == OTHER_SHA

    current_profile = (await repo.get_allocation(current))["deployment_profile"]
    assert current_profile["deployment_sha"] == NEW_SHA
    assert current_profile["source_sha256"] == NEW_DIGEST
    assert current_profile["source_bundle"] == "stored-profile:profile-current:bundle"
    assert current_profile["deploy_root"] == "/opt/profile-current"
    file_profile = (await repo.get_allocation(file_backed))["deployment_profile"]
    assert file_profile["source_bundle"] == "/app/compute-host-release/source.tar.gz"
    assert file_profile["deployment_sha"] == NEW_SHA
    inflight_profile = (await repo.get_allocation(inflight))["deployment_profile"]
    assert inflight_profile["deployment_sha"] == OLD_SHA
    assert inflight_profile["source_bundle"] == "stored-profile:profile-inflight:bundle"
    assert (await repo.get_allocation(other))["deployment_profile"]["deployment_sha"] == OTHER_SHA
    retired_profile = (await repo.get_allocation(retired))["deployment_profile"]
    assert retired_profile["deployment_sha"] == OLD_SHA
    assert retired_profile["source_bundle"] == "stored-profile:profile-retired:bundle"

    await repo.advance_host_release(
        allocation_id=current,
        previous_sha=NEW_SHA,
        previous_digest=NEW_DIGEST,
        deployment_sha=NEW_SHA,
        source_sha256=NEW_DIGEST,
        source_bundle=b"replacement",
        release_bundle_path="/app/compute-host-release/source.tar.gz",
        **fence(upgrade),
    )
    assert (await stored("profile-current"))["revision"] == moved["revision"]

    assert await repo.finish_operation(
        **fence(upgrade),
        state="blocked",
        step="restored",
        message="rolled back",
        result={"release_attempts": 2, "restore_attempted": True, "kept": True},
    )
    checked = await operation(repo, file_backed, "inspect")
    assert checked["kind"] == "inspect"
    assert await repo.finish_operation(
        **fence(checked),
        state="blocked",
        step="observed",
        message="kept",
        result={"release_attempts": 1, "restore_attempted": True},
    )
    inspected = await repo.retry_operation(checked["id"])
    assert inspected["result"]["release_attempts"] == 1
    assert inspected["result"]["restore_attempted"] is True

    retried = await repo.retry_operation(upgrade["id"])
    assert retried["state"] == "pending"
    assert retried["result"] == {"kept": True}


async def test_host_release_moves_reserved_routes_with_their_stored_profiles(db, store):
    """A new pool member is deployed from its route's snapshot, which names the stored bundle.

    The snapshot must keep pinning the bundle its stored profile holds, or the
    deployment refuses it after the experiment is created.
    """
    repo = store.compute_resources
    for profile_id in ("pool-serving", "pool-installing", "pool-spare"):
        await repo.save_deployment_profile(
            profile_id=profile_id,
            label=profile_id,
            configuration={"deployment_sha": OLD_SHA, "deploy_root": "/opt/" + profile_id},
            source_bundle=b"old-" + profile_id.encode(),
            source_sha256=OLD_DIGEST,
            secret_ciphertext="cipher-" + profile_id,
        )
    serving = await _ready_allocation(
        repo,
        "pool-serving",
        _profile_snapshot(
            "pool-serving", OLD_SHA, OLD_DIGEST, "stored-profile:pool-serving:bundle"
        ),
    )
    upgrade = await operation(repo, serving, "upgrade")
    assert upgrade["kind"] == "upgrade"
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_compute_connections
               (id,name,provider_id,secret_ciphertext,status)
               VALUES ('pool-cloudlab','CloudLab','cloudlab','ciphertext','verified'),
                      ('pool-ssh','SSH','existing_ssh','ciphertext','verified')"""
        )
        await conn.execute(
            """UPDATE agent_cloudlab_autoscaling_policy
               SET enabled=TRUE,min_hosts=0,max_hosts=2,
                   connection_id='pool-cloudlab',ssh_connection_id='pool-ssh',
                   deployment_profiles=ARRAY['pool-installing','pool-spare']"""
        )
        for profile_id in ("pool-installing", "pool-spare"):
            await conn.execute(
                """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
                   (deployment_profile,profile_snapshot) VALUES ($1,$2::jsonb)""",
                profile_id,
                json.dumps(
                    _profile_snapshot(
                        profile_id, OLD_SHA, OLD_DIGEST, f"stored-profile:{profile_id}:bundle"
                    )
                ),
            )

    async def scale_up(request_id: str, generation: int) -> dict:
        created = await repo.create_autoscale_allocation_operation(
            expected_revision=0,
            expected_generation=generation,
            request_id=request_id,
            fingerprint=request_id,
            allocation={"name": request_id},
            parameters={},
            requested_by="system:cloudlab-autoscaler",
            decision={"action": "scale_up_pressure"},
            cooldown_seconds=0,
        )
        return created["operation"]

    # A member is being installed from the first route when the release lands.
    installing = await scale_up("pool-member-installing", 0)
    await repo.advance_host_release(
        allocation_id=serving,
        previous_sha=OLD_SHA,
        previous_digest=OLD_DIGEST,
        deployment_sha=NEW_SHA,
        source_sha256=NEW_DIGEST,
        source_bundle=b"replacement",
        release_bundle_path="/app/compute-host-release/source.tar.gz",
        **fence(upgrade),
    )

    # The route being installed keeps its bundle, and its snapshot with it.
    assert (await repo.get_deployment_profile("pool-installing"))["source_sha256"] == OLD_DIGEST
    async with db.acquire() as conn:
        snapshot = await conn.fetchrow(
            """SELECT profile_snapshot->>'deployment_sha' AS sha,
                      profile_snapshot->>'source_sha256' AS digest
               FROM agent_cloudlab_autoscaling_profile_reservations
               WHERE deployment_profile='pool-installing'"""
        )
    assert (snapshot["sha"], snapshot["digest"]) == (OLD_SHA, OLD_DIGEST)
    claimed = await repo.claim_operation(worker_id="worker")
    assert claimed["id"] == installing["id"]
    assert await repo.finish_operation(**fence(claimed), state="succeeded", step="ready")

    # The spare route moved: the next member must pin what it now holds.
    spare = await repo.get_deployment_profile("pool-spare")
    assert spare["source_bundle"] == b"replacement"
    scaled = await scale_up("pool-member-scaled", 1)
    profile = (await repo.get_allocation(scaled["allocation_id"]))["deployment_profile"]
    assert profile["id"] == "pool-spare"
    assert profile["source_bundle"] == "stored-profile:pool-spare:bundle"
    assert (profile["deployment_sha"], profile["source_sha256"]) == (
        spare["configuration"]["deployment_sha"],
        spare["source_sha256"],
    )
    assert (profile["deployment_sha"], profile["source_sha256"]) == (NEW_SHA, NEW_DIGEST)


_MOVE_OUT_ARCHIVES = {
    "enabled": True,
    "region": "us-east-1",
    "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
    "expected_bucket_owner": "123456789012",
    "max_archive_bytes": 2 * 1024**3,
    "multipart_threshold_bytes": 8 * 1024**2,
    "multipart_part_bytes": 8 * 1024**2,
}
_CAPTURES = ["sandbox-archive-settings-v1", "sandbox-checkpoints-v1"]
_RESTORES = [*_CAPTURES, "sandbox-restore-v1"]


async def _archives_on(hosts) -> None:
    configured = await hosts.set_sandbox_archive_settings(
        expected_revision=0, settings=_MOVE_OUT_ARCHIVES
    )
    assert configured is not None and configured["revision"] == 1


async def _applies(hosts, host: str, capabilities: list[str], *, register: bool = False) -> None:
    """The Host runs this archive policy and declares what it can do with it."""
    if register:
        await hosts.register_host(
            host=host, credential_hash=hash_secret("cred-" + host), slots_total=2
        )
    await hosts.record_host_heartbeat(
        host=host,
        capabilities=capabilities,
        sandbox_archive_settings_applied_revision=1,
        sandbox_archive_settings_revision_reported=True,
    )


async def _move_out_holds(db) -> list[str]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT host FROM agent_host_admission_holds WHERE purpose='move_out' ORDER BY host"
        )
    return [row["host"] for row in rows]


async def test_a_host_moves_out_only_where_another_host_would_restore_its_groups(store, hosts, db):
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name="leaving")
    await _archives_on(hosts)
    await _applies(hosts, "leaving", _CAPTURES)

    assert not await repo.set_move_out_hold(allocation_id=allocation_id, due=True)
    assert await _move_out_holds(db) == []

    await _applies(hosts, "target", _RESTORES, register=True)
    assert await repo.set_move_out_hold(allocation_id=allocation_id, due=True)
    assert await _move_out_holds(db) == ["leaving"]

    # A started move continues even if its target goes away.
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM agent_runner_hosts WHERE host='target'")
    assert await repo.set_move_out_hold(allocation_id=allocation_id, due=True)
    # An extension ends it.
    assert not await repo.set_move_out_hold(allocation_id=allocation_id, due=False)
    assert await _move_out_holds(db) == []


async def test_a_host_without_working_checkpoints_never_stops_serving(store, hosts):
    """Its stopped groups would have no checkpoint, so their chats would only wait."""
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name="leaving")
    await _applies(hosts, "target", _RESTORES, register=True)
    await _applies(hosts, "leaving", _CAPTURES)

    # Archives off.
    assert not await repo.set_move_out_hold(allocation_id=allocation_id, due=True)

    # Archives on, but the leaving Host does not capture checkpoints.
    await _archives_on(hosts)
    await _applies(hosts, "target", _RESTORES)
    await _applies(hosts, "leaving", ["sandbox-archive-settings-v1"])
    assert not await repo.set_move_out_hold(allocation_id=allocation_id, due=True)

    await _applies(hosts, "leaving", _CAPTURES)
    assert await repo.set_move_out_hold(allocation_id=allocation_id, due=True)


async def test_a_target_that_takes_no_new_groups_is_no_target(store, hosts, db):
    """Restoring creates the group's container, which a new-groups hold forbids."""
    repo = store.compute_resources
    allocation_id, _, _ = await resource(store, hosts, name="leaving")
    await _archives_on(hosts)
    await _applies(hosts, "leaving", _CAPTURES)
    await _applies(hosts, "target", _RESTORES, register=True)
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_host_admission_holds (host,allocation_id,purpose,source_id,scope) "
            "VALUES ('target',$1,'lease_deadline','target-deadline','new_groups')",
            allocation_id,
        )

    assert not await repo.set_move_out_hold(allocation_id=allocation_id, due=True)


async def test_two_hosts_entering_their_windows_together_never_both_stop(store, hosts, db):
    """Each is the other's only target; at most one of them may move out."""
    repo = store.compute_resources
    first, _, _ = await resource(store, hosts, name="first")
    second, _, _ = await resource(store, hosts, name="second")
    await _archives_on(hosts)
    for name in ("first", "second"):
        await _applies(hosts, name, _RESTORES)

    outcomes = await asyncio.gather(
        repo.set_move_out_hold(allocation_id=first, due=True),
        repo.set_move_out_hold(allocation_id=second, due=True),
    )

    assert sorted(outcomes) == [False, True]
    assert len(await _move_out_holds(db)) == 1
