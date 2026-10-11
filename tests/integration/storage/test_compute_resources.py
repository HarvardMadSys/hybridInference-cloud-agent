"""Compute lifecycle invariants against deployed Alembic schema and real locks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.integration.storage.test_host_pool_store import (
    _claim,
    _create_job,
    db as db,
    hosts as hosts,
    store as store,
)

from cloud_agent.host_pool import hash_secret
from cloud_agent.storage._sql.compute_admission import host_compute_admission, lock_compute_entry

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
    assert await store.sandbox.admit_thread_sandbox_turn(**attempt_fence, job_id=job["id"])
    assert await store.sandbox.settle_thread_sandbox(
        **attempt_fence,
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
