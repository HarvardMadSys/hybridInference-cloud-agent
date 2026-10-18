"""Permanent inventory removal against real foreign keys and worker claims."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.integration.storage.test_admin_sandbox_queries import member, seed
from tests.integration.storage.test_attempt_ledger import _seed_attempt
from tests.integration.storage.test_compute_resources import resource
from tests.integration.storage.test_host_pool_store import db as db, hosts as hosts, store as store

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def pending_allocation(store):
    """Persist an acquisition which has never reached a provider or Host."""
    request = str(uuid4())
    operation = await store.compute_resources.create_allocation_operation(
        request_id=request,
        fingerprint=request,
        kind="acquire",
        allocation={
            "name": "never-created",
            "provider_id": "cloudlab",
            "mode": "provision",
            "management_mode": "manage",
            "release_supported": True,
        },
        parameters={},
        requested_by="admin",
    )
    return operation["allocation_id"], operation


async def remove_current(store, allocation_id):
    """Use the same revision the administrator's detail read would return."""
    allocation = await store.compute_resources.get_allocation(allocation_id)
    assert allocation is not None
    await store.compute_resources.remove_allocation(
        allocation_id=allocation_id, expected_revision=allocation["revision"]
    )


async def terminal_allocation(store, hosts, *, name="removed", state="terminated"):
    """Confirm that an observed machine is gone without deleting local metadata."""
    allocation_id, node, operation = await resource(store, hosts, name=name)
    await store.compute_resources.observe_allocation(
        allocation_id=allocation_id,
        observation={"observed_state": state, "scope_complete": True},
    )
    return allocation_id, node, operation


async def rotation_pair(db, store, hosts, *, rotation_state="pending"):
    """Persist the source, staging acquisition, and rotation's candidate journal."""
    source_id, _, _ = await terminal_allocation(store, hosts, name="rotation-source")
    candidate_id, acquisition = await pending_allocation(store)
    request = str(uuid4())
    rotation = await store.compute_resources.create_operation(
        request_id=request,
        fingerprint=request,
        kind="rotate",
        allocation_id=source_id,
        parameters={},
        requested_by="admin",
    )
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_allocations SET predecessor_id=$1,
               slot_host='rotation-source',slot_namespace='rotation-slot' WHERE id=$2""",
            source_id,
            candidate_id,
        )
        await conn.execute(
            "UPDATE agent_compute_operations SET state=$2,result=$3::jsonb WHERE id=$1",
            rotation["id"],
            rotation_state,
            json.dumps({"candidate": {"id": candidate_id, "operation_id": acquisition["id"]}}),
        )
        await conn.execute(
            "UPDATE agent_compute_operations SET parameters=$2::jsonb WHERE id=$1",
            acquisition["id"],
            json.dumps(
                {"rotation_operation_id": rotation["id"], "source_allocation_id": source_id}
            ),
        )
    return source_id, candidate_id, rotation, acquisition


@pytest.mark.parametrize("keep_profile", [False, True])
async def test_removal_cleans_owned_metadata_and_preserves_user_history(
    db, store, hosts, keep_profile
):
    allocation_id, node, operation = await terminal_allocation(store, hosts)
    peer_id, peer_node, peer_operation = await resource(store, hosts, name="successor")
    job = await seed(db, store, "removed-history", state="stopped", host="removed")
    await member(db, "removed-history", job, None)
    _, attempt_id, ledger_id = await _seed_attempt(
        db, store, job=job, finished_at=datetime.now(UTC)
    )
    rotation = await store.compute_resources.create_operation(
        request_id="completed-move",
        fingerprint="completed-move",
        kind="rotate",
        allocation_id=allocation_id,
        parameters={},
        requested_by="admin",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state='succeeded',result=$2::jsonb WHERE id=$1",
            rotation["id"],
            json.dumps({"candidate": {"id": peer_id}, "rebound": True, "verified": True}),
        )
        await conn.execute("UPDATE agent_attempts SET host_id='removed' WHERE id=$1", attempt_id)
        await conn.execute(
            """UPDATE agent_sandboxes SET deletion_state='deleted',deletion_discard=TRUE,
               container_id=NULL,deleted_at=NOW() WHERE id='removed-history'"""
        )
        events_before = [
            dict(row)
            for row in await conn.fetch(
                "SELECT * FROM agent_sandbox_events WHERE sandbox_id='removed-history' ORDER BY id"
            )
        ]
        await conn.execute(
            """INSERT INTO agent_compute_connections
               (id,name,provider_id,secret_ciphertext)
               VALUES ('shared-provider','CloudLab','cloudlab','ciphertext'),
                      ('shared-ssh','SSH','existing_ssh','ciphertext')"""
        )
        await conn.execute(
            """INSERT INTO agent_compute_deployment_profiles
               (id,label,configuration,source_bundle,source_sha256,secret_ciphertext)
               VALUES ('shared-profile','Shared setup','{}',$1,$2,'ciphertext')""",
            b"source bundle",
            "a" * 64,
        )
        await conn.execute(
            """UPDATE agent_compute_allocations SET connection_id='shared-provider',
               ssh_connection_id='shared-ssh',deployment_profile='{"id":"shared-profile"}'
               WHERE id=$1""",
            allocation_id,
        )
        await conn.execute(
            "UPDATE agent_compute_allocations SET predecessor_id=$1 WHERE id=$2",
            allocation_id,
            peer_id,
        )
        await conn.execute(
            """INSERT INTO agent_compute_node_registrations
               (id,allocation_id,operation_id,token_hash,expires_at)
               VALUES ('removed-registration',$1,$2,'registration-hash',NOW()+INTERVAL '1 hour')""",
            allocation_id,
            operation["id"],
        )
        await conn.execute(
            """INSERT INTO agent_host_admission_holds
               (host,allocation_id,purpose,source_id,scope)
               VALUES ('removed',$1,'retirement','removed-hold','all_claims'),
                      ('successor',$1,'rotation','removed-cross-host-hold','all_claims'),
                      ('successor',$2,'rotation',$3,'all_claims'),
                      ('successor',$2,'lease_deadline','peer-hold','new_groups')""",
            allocation_id,
            peer_id,
            rotation["id"],
        )
        await conn.execute(
            """INSERT INTO agent_host_enroll_tokens
               (token_hash,host,consumed_host,expires_at)
               VALUES ('removed-token','removed',NULL,NOW()+INTERVAL '1 hour'),
                      ('removed-consumed-token','old-name','removed',NOW()+INTERVAL '1 hour'),
                      ('peer-token','successor',NULL,NOW()+INTERVAL '1 hour')"""
        )
        await conn.execute(
            """INSERT INTO agent_host_capacity_history
               (host,capacity_epoch,bucket_at,sampled_at,sample_id)
               SELECT host,capacity_epoch,NOW(),NOW(),gen_random_uuid()
               FROM agent_runner_hosts"""
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_members
               (allocation_id,deployment_profile,state) VALUES ($1,'shared-profile','released')""",
            allocation_id,
        )
        await conn.execute(
            """INSERT INTO agent_cloudlab_autoscaling_profile_reservations
               (deployment_profile,profile_snapshot,allocation_id)
               VALUES ('shared-profile','{"id":"shared-profile"}',$1)""",
            allocation_id,
        )
        if keep_profile:
            await conn.execute(
                """UPDATE agent_cloudlab_autoscaling_policy
                   SET deployment_profiles=ARRAY['shared-profile']"""
            )

    await remove_current(store, allocation_id)

    assert await store.compute_resources.get_allocation(allocation_id) is None
    successor = await store.compute_resources.get_allocation(peer_id)
    assert successor is not None and successor["predecessor_id"] is None
    assert [row["id"] for row in successor["nodes"]] == [peer_node["id"]]
    assert [row["host"] for row in successor["hosts"]] == ["successor"]
    async with db.acquire() as conn:
        for table, column, value in (
            ("agent_compute_nodes", "id", node["id"]),
            ("agent_runner_hosts", "host", "removed"),
            ("agent_host_capacity_history", "host", "removed"),
            ("agent_compute_node_registrations", "allocation_id", allocation_id),
            ("agent_host_admission_holds", "allocation_id", allocation_id),
            ("agent_compute_operations", "allocation_id", allocation_id),
            ("agent_cloudlab_autoscaling_members", "allocation_id", allocation_id),
        ):
            assert (
                await conn.fetchval(f"SELECT COUNT(*) FROM {table} WHERE {column}=$1", value) == 0
            )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_host_enroll_tokens WHERE host='removed' OR consumed_host='removed'"
            )
            == 0
        )
        reservation = await conn.fetchrow(
            """SELECT * FROM agent_cloudlab_autoscaling_profile_reservations
               WHERE deployment_profile='shared-profile'"""
        )
        if keep_profile:
            assert reservation is not None and reservation["allocation_id"] is None
        else:
            assert reservation is None
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_compute_connections") == 2
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_compute_deployment_profiles") == 1
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_host_capacity_history WHERE host='successor'"
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_host_enroll_tokens WHERE token_hash='peer-token'"
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_host_admission_holds WHERE source_id='peer-hold'"
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_host_admission_holds WHERE source_id=$1",
                rotation["id"],
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_compute_operations WHERE id=$1", peer_operation["id"]
            )
            == 1
        )
        assert await conn.fetchval("SELECT COUNT(*) FROM agent_jobs WHERE id=$1", job["id"]) == 1
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM agent_thread_sandbox_memberships WHERE thread_id=$1",
                job["thread_id"],
            )
            == 1
        )
        assert (
            await conn.fetchval("SELECT COUNT(*) FROM agent_attempt_ledger WHERE id=$1", ledger_id)
            == 1
        )
        attempt = await conn.fetchrow("SELECT * FROM agent_attempts WHERE id=$1", attempt_id)
        assert attempt["status"] == "finished" and attempt["host_id"] is None
        sandbox = await conn.fetchrow("SELECT * FROM agent_sandboxes WHERE id='removed-history'")
        assert sandbox["host"] == "removed" and sandbox["deletion_state"] == "deleted"
        assert [
            dict(row)
            for row in await conn.fetch(
                "SELECT * FROM agent_sandbox_events WHERE sandbox_id='removed-history' ORDER BY id"
            )
        ] == events_before


@pytest.mark.parametrize("state", ["pending", "waiting", "blocked", "failed", "cancelled"])
async def test_remove_never_created_acquisition(db, store, state):
    allocation_id, operation = await pending_allocation(store)
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_operations
               SET state=$2,result='{"external_pending":false}' WHERE id=$1""",
            operation["id"],
            state,
        )
    await remove_current(store, allocation_id)
    assert await store.compute_resources.get_allocation(allocation_id) is None
    assert await store.compute_resources.get_operation(operation["id"]) is None


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    ("state", "result"),
    [("running", {}), ("unknown", {}), ("waiting", {"external_pending": True})],
)
async def test_unresolved_operation_blocks_even_confirmed_terminal_removal(
    db, store, hosts, terminal, state, result
):
    if terminal:
        allocation_id, _, operation = await terminal_allocation(store, hosts)
    else:
        allocation_id, operation = await pending_allocation(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state=$2,result=$3::jsonb WHERE id=$1",
            operation["id"],
            state,
            json.dumps(result),
        )
    before = await store.compute_resources.get_allocation(allocation_id)
    with pytest.raises(ValueError, match=r"^external_operation_unresolved$"):
        await remove_current(store, allocation_id)
    assert await store.compute_resources.get_allocation(allocation_id) == before
    assert await store.compute_resources.get_operation(operation["id"]) is not None


@pytest.mark.parametrize(
    "result",
    [
        {"external_pending": True},
        {"resource_ref": {"provider_id": "cloudlab", "external_id": "experiment"}},
        {"external_id": "experiment"},
        {"resolved_external_id": "experiment"},
    ],
)
async def test_provider_receipt_prevents_forgetting_no_identity_allocation(db, store, result):
    allocation_id, operation = await pending_allocation(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state='failed',result=$2::jsonb WHERE id=$1",
            operation["id"],
            json.dumps(result),
        )
    with pytest.raises(ValueError, match=r"^external_operation_unresolved$"):
        await remove_current(store, allocation_id)
    assert await store.compute_resources.get_allocation(allocation_id) is not None


@pytest.mark.parametrize("state", ["failed", "blocked", "waiting"])
async def test_unfinished_submission_step_requires_provider_recovery(db, store, state):
    allocation_id, operation = await pending_allocation(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state=$2,step='acquiring' WHERE id=$1",
            operation["id"],
            state,
        )
    with pytest.raises(ValueError, match=r"^external_operation_unresolved$"):
        await remove_current(store, allocation_id)


async def test_registered_node_keys_prove_acquisition_has_provider_side_effects(store):
    repo = store.compute_resources
    allocation_id, operation = await pending_allocation(store)
    claimed = await repo.claim_operation(worker_id="registration-worker")
    assert claimed["id"] == operation["id"]
    await repo.create_node_registration(
        allocation_id=allocation_id,
        token_hash="booted-provider-node",
        ttl_seconds=3600,
        operation_id=claimed["id"],
        generation=claimed["generation"],
        worker_id="registration-worker",
    )
    assert (
        await repo.complete_node_registration(
            token_hash="booted-provider-node", host_keys=["ssh-ed25519 AAAAbooted-node"]
        )
        == "registered"
    )
    assert await repo.finish_operation(
        operation_id=claimed["id"],
        generation=claimed["generation"],
        worker_id="registration-worker",
        state="blocked",
        step="pending",
        result={"external_pending": False},
    )
    with pytest.raises(ValueError, match=r"^external_operation_unresolved$"):
        await remove_current(store, allocation_id)
    registration = await repo.node_registration(allocation_id=allocation_id)
    assert registration is not None and registration["registered_at"] is not None


@pytest.mark.parametrize("state", ["terminated", "absent"])
async def test_confirmed_release_allows_historical_provider_receipts(db, store, hosts, state):
    allocation_id, _, operation = await terminal_allocation(store, hosts, state=state)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET result=$2::jsonb WHERE id=$1",
            operation["id"],
            json.dumps(
                {
                    "resource_ref": {"provider_id": "cloudlab", "external_id": "released"},
                    "external_id": "released",
                    "resolved_external_id": "released",
                }
            ),
        )
    await remove_current(store, allocation_id)
    assert await store.compute_resources.get_allocation(allocation_id) is None


@pytest.mark.parametrize("state", ["ready", "unknown", "failed", "absent"])
async def test_external_resource_requires_confirmed_release(db, store, state):
    allocation_id, _, _ = await resource(store)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET observed_state=$2,observed_at=NULL WHERE id=$1",
            allocation_id,
            state,
        )
    with pytest.raises(ValueError, match=r"^resource_release_required$"):
        await remove_current(store, allocation_id)


async def test_attach_without_provider_release_requires_disabled_hosts(db, store, hosts):
    allocation_id, _, _ = await resource(store, hosts)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET release_supported=FALSE WHERE id=$1",
            allocation_id,
        )
    with pytest.raises(ValueError, match=r"^resource_release_required$"):
        await remove_current(store, allocation_id)
    assert await hosts.set_host_status(host="machine", status="disabled") == "ok"
    await remove_current(store, allocation_id)
    assert await hosts.get_host(host="machine") is None


@pytest.mark.parametrize(
    "blocker",
    [
        "placement_pinned",
        "deployment_drain_active",
        "retained_groups_present",
        "active_attempts_present",
    ],
)
async def test_host_activity_guards_still_apply_after_provider_is_gone(db, store, hosts, blocker):
    allocation_id, _, _ = await terminal_allocation(store, hosts)
    if blocker == "placement_pinned":
        async with db.acquire() as conn:
            await conn.execute("UPDATE agent_runner_policy SET active_host='removed' WHERE id")
    elif blocker == "deployment_drain_active":
        host = await hosts.get_host(host="removed")
        outcome, _, _ = await hosts.acquire_host_drain_fence(
            host="removed",
            expected_fence_token=None,
            expected_fence_epoch=host["drain_fence_epoch"],
            fence_token=uuid4(),
        )
        assert outcome == "ok"
    elif blocker == "retained_groups_present":
        await seed(db, store, "retained-removed", state="stopped", host="removed")
    else:
        _, attempt_id, _ = await _seed_attempt(db, store)
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agent_attempts SET host_id='removed' WHERE id=$1", attempt_id
            )
    before = await store.compute_resources.get_allocation(allocation_id)
    with pytest.raises(ValueError, match=rf"^{blocker}$"):
        await remove_current(store, allocation_id)
    assert await store.compute_resources.get_allocation(allocation_id) == before
    assert await hosts.get_host(host="removed") is not None


async def test_a_cold_group_does_not_keep_a_released_servers_records(db, store, hosts):
    """It waits in its checkpoint for whichever host claims it, not for this machine."""
    allocation_id, _, _ = await terminal_allocation(store, hosts)
    await seed(db, store, "cold-removed", state="stopped", host="removed")
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_sandbox_checkpoints
               (id, sandbox_id, generation, host, container_id, base_image, runtime_id,
                object_key, sha256, size_bytes, new_layer_bytes, layer_count,
                format_version, captured_at, start_seq)
               VALUES ($1, 'cold-removed', 1, 'removed', 'container-cold-removed', $2,
                       'claude-code', 'cloud-agent/sandbox/v2/cold.oci.tar', $3,
                       4096, 2048, 3, 1, NOW(), 0)""",
            "c" * 32,
            "ghcr.io/example/cloud-agent-sandbox@sha256:" + "a" * 64,
            "e" * 64,
        )
        await conn.execute(
            "UPDATE agent_sandboxes SET state = 'cold', restore_checkpoint_id = $1 "
            "WHERE id = 'cold-removed'",
            "c" * 32,
        )

    await remove_current(store, allocation_id)

    assert await store.compute_resources.get_allocation(allocation_id) is None
    assert await hosts.get_host(host="removed") is None
    async with db.acquire() as conn:
        group = await conn.fetchrow(
            "SELECT state, host, restore_checkpoint_id FROM agent_sandboxes "
            "WHERE id = 'cold-removed'"
        )
    assert dict(group) == {"state": "cold", "host": "removed", "restore_checkpoint_id": "c" * 32}


async def test_removal_requires_current_revision(store):
    allocation_id, _ = await pending_allocation(store)
    with pytest.raises(ValueError, match=r"^revision_conflict$"):
        await store.compute_resources.remove_allocation(
            allocation_id=allocation_id, expected_revision=0
        )
    assert await store.compute_resources.get_allocation(allocation_id) is not None
    with pytest.raises(ValueError, match=r"^allocation_not_found$"):
        await store.compute_resources.remove_allocation(
            allocation_id="missing", expected_revision=1
        )


@pytest.mark.parametrize("target", ["source", "candidate"])
@pytest.mark.parametrize("state", ["pending", "waiting", "blocked"])
async def test_active_rotation_preserves_both_source_and_candidate(db, store, hosts, target, state):
    source_id, candidate_id, rotation, acquisition = await rotation_pair(
        db, store, hosts, rotation_state=state
    )
    allocation_id = source_id if target == "source" else candidate_id
    with pytest.raises(ValueError, match=r"^rotation_in_progress$"):
        await remove_current(store, allocation_id)
    assert await store.compute_resources.get_allocation(source_id) is not None
    assert await store.compute_resources.get_allocation(candidate_id) is not None
    assert await store.compute_resources.get_operation(rotation["id"]) is not None
    assert await store.compute_resources.get_operation(acquisition["id"]) is not None


async def test_rotation_attempt_history_keeps_a_candidate_recoverable(db, store, hosts):
    _, candidate_id, rotation, _ = await rotation_pair(db, store, hosts)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET predecessor_id=NULL WHERE id=$1", candidate_id
        )
        await conn.execute(
            "UPDATE agent_compute_operations SET result=$2::jsonb WHERE id=$1",
            rotation["id"],
            json.dumps({"candidate": None, "attempts": [{"candidate": candidate_id}]}),
        )
    with pytest.raises(ValueError, match=r"^rotation_in_progress$"):
        await remove_current(store, candidate_id)


async def test_acquisition_parameters_keep_its_parent_rotation_recoverable(db, store, hosts):
    _, candidate_id, rotation, _ = await rotation_pair(db, store, hosts)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_allocations SET predecessor_id=NULL WHERE id=$1", candidate_id
        )
        await conn.execute(
            "UPDATE agent_compute_operations SET result='{}' WHERE id=$1", rotation["id"]
        )
    with pytest.raises(ValueError, match=r"^rotation_in_progress$"):
        await remove_current(store, candidate_id)


@pytest.mark.parametrize("target", ["source", "candidate"])
async def test_retryable_failed_rotation_preserves_live_source_and_candidate(
    db, store, hosts, target
):
    source_id, candidate_id, _, _ = await rotation_pair(db, store, hosts, rotation_state="failed")
    await store.compute_resources.observe_allocation(
        allocation_id=source_id, observation={"observed_state": "ready"}
    )
    allocation_id = source_id if target == "source" else candidate_id
    with pytest.raises(ValueError, match=r"^rotation_in_progress$"):
        await remove_current(store, allocation_id)


@pytest.mark.parametrize("kind", ["acquire", "discard"])
async def test_source_remains_until_candidate_work_has_settled(db, store, hosts, kind):
    source_id, _, _, acquisition = await rotation_pair(db, store, hosts, rotation_state="cancelled")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET kind=$2 WHERE id=$1", acquisition["id"], kind
        )
    with pytest.raises(ValueError, match=r"^rotation_in_progress$"):
        await remove_current(store, source_id)


async def test_source_remains_while_unbound_live_candidate_still_needs_release(db, store, hosts):
    source_id, candidate_id, _, acquisition = await rotation_pair(
        db, store, hosts, rotation_state="cancelled"
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET state='succeeded',step='prepared' WHERE id=$1",
            acquisition["id"],
        )
    await store.compute_resources.observe_allocation(
        allocation_id=candidate_id,
        observation={"external_id": "unbound-live-candidate", "observed_state": "ready"},
    )
    with pytest.raises(ValueError, match=r"^rotation_in_progress$"):
        await remove_current(store, source_id)


async def test_claim_and_removal_never_leave_a_worker_for_deleted_allocation(store):
    repo = store.compute_resources
    for _ in range(6):
        allocation_id, operation = await pending_allocation(store)
        removed, claimed = await asyncio.gather(
            remove_current(store, allocation_id),
            repo.claim_operation(worker_id="removal-racer"),
            return_exceptions=True,
        )
        if isinstance(removed, Exception):
            assert isinstance(removed, ValueError)
            assert str(removed) == "external_operation_unresolved"
            assert isinstance(claimed, dict) and claimed["id"] == operation["id"]
            assert await repo.get_allocation(allocation_id) is not None
            assert await repo.finish_operation(
                operation_id=claimed["id"],
                generation=claimed["generation"],
                worker_id="removal-racer",
                state="failed",
                step="pending",
                result={"external_pending": False},
            )
            await remove_current(store, allocation_id)
        else:
            assert removed is None and claimed is None
            assert await repo.get_allocation(allocation_id) is None


async def test_consumed_enrollment_token_cannot_resurrect_removed_host(store, hosts):
    allocation_id, _, _ = await terminal_allocation(store, hosts)
    token_hash = "removed-registration-token"
    await hosts.issue_enroll_token(token_hash=token_hash, host="removed", ttl_seconds=3600)
    assert await hosts.consume_enroll_token(token_hash=token_hash, host="removed")

    await remove_current(store, allocation_id)

    with pytest.raises(ValueError, match=r"^enrollment_token_removed$"):
        await hosts.register_host(
            host="removed", credential_hash="late-credential", enrollment_token_hash=token_hash
        )
    assert await hosts.get_host(host="removed") is None
