"""Node host-key registration against the deployed schema and real transactions."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.integration.storage.test_compute_resources import fence, operation, resource
from tests.integration.storage.test_host_pool_store import (
    db as db,
    hosts as hosts,
    store as store,
)

from cloud_agent.host_pool import hash_secret
from cloud_agent.ports.external.compute_provider import SshConnection

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

KEY_A = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdO7VBdfUlnQkwf4yfnmrk1h3G625rktRv0VgZa5awA"]
KEY_B = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILl/TKgQBzAypTWuysaDqD0UnXP/63l++yTL9nQfbekM"]


async def open_registration(store, allocation_id: str, *, ttl: float = 3600) -> str:
    repo = store.compute_resources
    op = await operation(repo, allocation_id, kind="acquire")
    token = str(uuid4())
    await repo.create_node_registration(
        allocation_id=allocation_id, token_hash=hash_secret(token), ttl_seconds=ttl, **fence(op)
    )
    assert await repo.finish_operation(**fence(op), state="succeeded", step="done")
    return token


async def test_a_token_records_one_set_of_keys_and_only_the_same_set_again(store):
    allocation_id, _, _ = await resource(store, provision=True)
    repo = store.compute_resources
    token = await open_registration(store, allocation_id)

    open_state = await repo.node_registration(allocation_id=allocation_id)
    assert open_state["registered_at"] is None and open_state["host_keys"] == []
    assert "token_hash" not in open_state

    assert (
        await repo.complete_node_registration(token_hash=hash_secret(token), host_keys=KEY_A)
        == "registered"
    )
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(token), host_keys=KEY_A)
        == "replayed"
    )
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(token), host_keys=KEY_B)
        == "conflict"
    )
    assert (await repo.node_registration(allocation_id=allocation_id))["host_keys"] == KEY_A


async def test_unknown_expired_and_superseded_tokens_are_all_simply_invalid(store):
    allocation_id, _, _ = await resource(store, provision=True)
    repo = store.compute_resources
    assert (
        await repo.complete_node_registration(
            token_hash=hash_secret("never-issued"), host_keys=KEY_A
        )
        == "invalid"
    )

    expired = await open_registration(store, allocation_id, ttl=0)
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(expired), host_keys=KEY_A)
        == "invalid"
    )
    assert (await repo.node_registration(allocation_id=allocation_id))["expired"] is True

    older = await open_registration(store, allocation_id)
    newer = await open_registration(store, allocation_id)
    # A resubmitted acquisition got a new token; the earlier one died with it.
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(older), host_keys=KEY_A)
        == "invalid"
    )
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(newer), host_keys=KEY_A)
        == "registered"
    )


async def test_a_missed_registration_reopens_for_the_same_token_only(store):
    """The node holds the token it was created with; only its validity can move."""
    allocation_id, _, _ = await resource(store, provision=True)
    repo = store.compute_resources
    token = await open_registration(store, allocation_id, ttl=0)
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(token), host_keys=KEY_A)
        == "invalid"
    )
    op = await operation(repo, allocation_id, kind="acquire")
    assert await repo.reopen_node_registration(
        allocation_id=allocation_id, ttl_seconds=3600, **fence(op)
    )
    assert (await repo.node_registration(allocation_id=allocation_id))["expired"] is False
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(token), host_keys=KEY_A)
        == "registered"
    )
    # A completed registration is never reopened or rewritten.
    assert not await repo.reopen_node_registration(
        allocation_id=allocation_id, ttl_seconds=3600, **fence(op)
    )
    assert (await repo.node_registration(allocation_id=allocation_id))["host_keys"] == KEY_A


async def test_a_superseded_or_retiring_registration_is_not_reopened(store):
    allocation_id, _, _ = await resource(store, provision=True)
    repo = store.compute_resources
    older = await open_registration(store, allocation_id, ttl=0)
    newer = await open_registration(store, allocation_id, ttl=0)
    op = await operation(repo, allocation_id, kind="acquire")
    assert await repo.reopen_node_registration(
        allocation_id=allocation_id, ttl_seconds=3600, **fence(op)
    )
    # Only the newest token came back to life.
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(older), host_keys=KEY_A)
        == "invalid"
    )
    assert await repo.finish_operation(**fence(op), state="succeeded", step="done")
    retire = await operation(repo, allocation_id, kind="retire")
    await repo.begin_retirement(allocation_id=allocation_id, **fence(retire))
    assert not await repo.reopen_node_registration(
        allocation_id=allocation_id, ttl_seconds=3600, **fence(retire)
    )
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(newer), host_keys=KEY_A)
        == "invalid"
    )


async def test_a_retiring_allocation_accepts_no_registration(store):
    allocation_id, _, _ = await resource(store, provision=True)
    repo = store.compute_resources
    token = await open_registration(store, allocation_id)
    op = await operation(repo, allocation_id, kind="retire")
    await repo.begin_retirement(allocation_id=allocation_id, **fence(op))
    assert (
        await repo.complete_node_registration(token_hash=hash_secret(token), host_keys=KEY_A)
        == "invalid"
    )


async def test_a_pinned_host_key_survives_later_provider_observations(store):
    allocation_id, node, _ = await resource(store, provision=True)
    repo = store.compute_resources
    connection = {
        "address": "node.example.org",
        "port": 22,
        "username": "agent",
        "credential_ref": "ssh-1",
    }
    await repo.upsert_node(
        allocation_id=allocation_id,
        node={
            **{k: node[k] for k in ("provider_node_id", "instance_identity", "name")},
            "connection": connection,
        },
    )
    op = await operation(repo, allocation_id, kind="acquire")
    await repo.record_host_key(node_id=node["id"], host_key=KEY_A[0], **fence(op))
    # The provider's next observation of the same instance carries no key.
    await repo.upsert_node(
        allocation_id=allocation_id,
        node={
            **{k: node[k] for k in ("provider_node_id", "instance_identity", "name")},
            "connection": connection,
        },
    )
    stored = (await repo.get_allocation(allocation_id))["nodes"][0]["connection"]
    assert stored == {**connection, "host_key": KEY_A[0]}


async def test_a_pinned_host_key_survives_an_observation_serialized_with_its_empty_fields(store):
    """The reconciler saves the adapter's whole connection model, unset fields as null."""
    allocation_id, node, _ = await resource(store, provision=True)
    repo = store.compute_resources
    observed = SshConnection(
        address="node.example.org", username="agent", credential_ref="ssh-1"
    ).model_dump(mode="json")
    assert observed["host_key"] is None
    identity = {k: node[k] for k in ("provider_node_id", "instance_identity", "name")}
    await repo.upsert_node(allocation_id=allocation_id, node={**identity, "connection": observed})
    op = await operation(repo, allocation_id, kind="acquire")
    await repo.record_host_key(node_id=node["id"], host_key=KEY_A[0], **fence(op))
    await repo.upsert_node(allocation_id=allocation_id, node={**identity, "connection": observed})
    stored = (await repo.get_allocation(allocation_id))["nodes"][0]["connection"]
    assert stored == {**observed, "host_key": KEY_A[0]}
