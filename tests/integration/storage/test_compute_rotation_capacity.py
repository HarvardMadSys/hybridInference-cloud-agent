"""A replacement node type the provider has no capacity for gives way to the next type.

The real CloudLab adapter sends each create to a synthetic Portal and
classifies its answer; the real worker, store, scheduler and enrollment run
the move. Every other cloud and SSH effect is the inert fake of the workflow
tests. No Portal answer here is a recorded one: each is the shape the adapter
documents for that outcome.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from tests.integration.storage.test_compute_rotation_workflow import (
    HOST_KEY,
    FakeCloud,
    FakeNodes,
    _profile,
    rotation_of,
    successor_of,
)
from tests.integration.storage.test_host_pool_store import db as db, hosts as hosts, store as store

from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.application.compute_release import NEVER_CREATED
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import ComputeConfiguration
from cloud_agent.compute_registry import ComputeProviderRegistration, ComputeProviderRegistry
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.host_pool import hash_secret
from cloud_agent.ports.external.compute_provider import (
    NodeObservation,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
    SshConnection,
)
from cloud_agent.providers.compute.cloudlab import CloudLabProvider
from cloud_agent.schemas_compute import RenewalPolicy, RotateComputeRequest, UpdateComputePolicy
from cloud_agent.workers.compute_resources import ComputeResourceWorker

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

# What the adapter needs to send a create; no real project, profile or account.
SCOPE = {
    "credential_ref": "synthetic-account",
    "project": "lab",
    "profile_name": "cloud-agent-node",
    "profile_project": "lab",
}
# Synthetic Portal answers, one per outcome the adapter distinguishes.
NO_CAPACITY = (429, {"error": "Not enough free nodes of the requested type"})
RATE_LIMITED = (429, {"error": "Too many requests"})
SERVER_ERROR = (500, {"error": "Internal server error"})
UNAUTHORIZED = (401, {"error": "Unauthorized"})


class PortalCloud(FakeCloud):
    """Experiments the Portal answers through the real CloudLab adapter.

    A node type listed in ``portal`` gets that answer to its create, read by
    the adapter itself; any other type is created at once and is ready.
    ``reads`` answers the adapter's read of one experiment the same way.
    Node names are unique, so two nodes of one type can be told apart.
    """

    def __init__(self, repository) -> None:
        super().__init__(repository)
        self.portal: dict[str, tuple[int, dict]] = {}
        self.reads: dict[str, tuple[int, dict]] = {}
        self.posts: list[str] = []
        self.serial = 0

    def _adapter(self) -> CloudLabProvider:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/profiles/"):
                return httpx.Response(
                    200, json={"profiles": [{"name": "cloud-agent-node", "project": "lab"}]}
                )
            if request.method == "POST":
                node_type = json.loads(request.content)["bindings"]["phystype"]
                self.posts.append(node_type)
                status, body = self.portal[node_type]
            else:
                status, body = self.reads[request.url.path.rsplit("/", 1)[-1]]
            return httpx.Response(status, json=body)

        resolver = AsyncMock()
        resolver.resolve.return_value = "synthetic-token"
        return CloudLabProvider(
            api_url="https://portal.example.org/v1",
            resolver=resolver,
            client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    async def acquire(self, request, intent):
        node_type = (request.config.get("bindings") or {}).get("phystype")
        if node_type is None or node_type in self.portal:
            return await self._adapter().acquire(request, intent)
        self.posts.append(node_type)
        self.created.append(node_type)
        self.serial += 1
        external_id = str(uuid4())
        ref = ResourceRef(
            external_id=external_id, config={**request.config, "external_id": external_id}
        )
        name = f"{node_type}-{self.serial}"
        node = NodeObservation(
            provider_node_id="node0",
            instance_identity=external_id + ":node0",
            display_name=name,
            connection=SshConnection(
                address=f"{name}.example.org", username="agent", credential_ref="ssh"
            ),
        )
        observation = ResourceObservation(
            ref=ref,
            state="ready",
            raw_state="ready",
            observed_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=16),
            lease_known=True,
            nodes=(node,),
            scope_complete=True,
        )
        assert request.registration is not None
        outcome = await self.repository.complete_node_registration(
            token_hash=hash_secret(request.registration.token.get_secret_value()),
            host_keys=[HOST_KEY],
        )
        assert outcome == "registered"
        self.experiments[external_id] = observation
        return ProviderReceipt(outcome="confirmed", resource=ref, observation=observation)

    async def inspect(self, ref):
        if ref.external_id in self.reads:
            return await self._adapter().inspect(ref)
        observation = self.experiments[ref.external_id]
        return observation.model_copy(update={"observed_at": datetime.now(UTC)})


class StatefulNodes(FakeNodes):
    """Each node holds one Host state; a copy replaces the target's with the source's."""

    def __init__(self, hosts) -> None:
        super().__init__(hosts)
        self.data: dict[str, str] = {}

    async def transfer(self, source, target, profile, intent, *, final, first=False):
        copied = await super().transfer(source, target, profile, intent, final=final, first=first)
        self.data[target.display_name] = self.data.get(source.display_name, "")
        return copied

    async def wipe(self, node, profile, intent):
        await super().wipe(node, profile, intent)
        self.data.pop(node.display_name, None)


async def rig(db, store, hosts, monkeypatch, *, node_types=("t1", "t2")):
    """One ready Host on ``first-1`` (profile slot-a) whose node may move to ``node_types``."""
    repository = store.compute_resources
    cloud = PortalCloud(repository)
    registry = ComputeProviderRegistry(
        [ComputeProviderRegistration(id="cloudlab", label="Fixture", provider=cloud, fields=())]
    )
    commands = ComputeCommands(
        repository=repository,
        registry=registry,
        secrets=ComputeSecretStore(repository, key="x" * 32),
        configuration=ComputeConfiguration(
            profiles={name: _profile(name) for name in ("slot-a", "slot-b")},
            registration_url="https://control.example.org/v1/compute/node-registrations",
        ),
    )
    nodes = StatefulNodes(hosts)
    sandbox_manager = SimpleNamespace(
        health=AsyncMock(return_value={}), list_sandboxes=AsyncMock(return_value=[])
    )
    for module in ("compute_reconcile", "compute_rotation"):
        monkeypatch.setattr(
            f"cloud_agent.application.{module}.sandbox_manager_for_host",
            lambda host: sandbox_manager,
        )
    # No Host release is configured: the scheduler only renews and moves.
    monkeypatch.setattr(
        "cloud_agent.application.compute_reconcile.load_control_plane_host_release",
        lambda: None,
    )
    reconciler = ComputeReconciler(commands=commands, host_pool=hosts, bootstrap=nodes)
    request_id = str(uuid4())
    first = await repository.create_allocation_operation(
        request_id=request_id,
        fingerprint=request_id,
        kind="acquire",
        requested_by="admin",
        parameters={},
        allocation={
            "name": "lab",
            "provider_id": "cloudlab",
            "mode": "provision",
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
            "configuration": {
                **SCOPE,
                "host_key_source": "registration",
                "bindings": {"phystype": "first"},
            },
            "deployment_profile": _profile("slot-a").model_dump(mode="json"),
        },
    )
    r = SimpleNamespace(
        repo=repository,
        hosts=hosts,
        worker=ComputeResourceWorker(reconciler),
        commands=commands,
        cloud=cloud,
        nodes=nodes,
        allocation_id=first["allocation_id"],
    )
    assert await settle(r, db)
    allocation = await repository.get_allocation(r.allocation_id)
    assert allocation["observed_state"] == "ready", allocation
    r.host = allocation["hosts"][0]["host"]
    nodes.data[allocation["nodes"][0]["name"]] = "written-on-first-node"
    policy = RenewalPolicy(
        enabled=True,
        reason="Keep the evaluation sandboxes",
        renew_before_seconds=3 * 3600,
        extend_to_horizon_seconds=8 * 3600,
        rotation_enabled=True,
        node_types=list(node_types),
    )
    await commands.update_policy(
        r.allocation_id,
        UpdateComputePolicy(expected_revision=allocation["revision"], policy=policy),
    )
    return r


async def settle(r, db, *, passes: int = 80) -> bool:
    """Run due operations, letting every wait elapse; whether all of them finished.

    A wait never ends by the wall clock here: no pass moves time forward, so a
    move that waits for its candidate timeout never settles.
    """
    for _ in range(passes):
        claimed = await r.repo.claim_operation(worker_id="capacity-test")
        if claimed is None:
            if await wake(db) == 0:
                return True
            continue
        await r.worker.execute_claimed(claimed)
    return False


async def wake(db, operation_id: str | None = None) -> int:
    async with db.acquire() as conn:
        status = await conn.execute(
            "UPDATE agent_compute_operations SET next_attempt_at=NOW() "
            "WHERE state='waiting' AND ($1::text IS NULL OR id=$1)",
            operation_id,
        )
    return int(status.split()[-1])


async def run_next(r, db, kind: str) -> dict:
    """Run the next due operation, letting waits elapse if none is; it must be of ``kind``."""
    claimed = await r.repo.claim_operation(worker_id="capacity-test")
    if claimed is None:
        await wake(db)
        claimed = await r.repo.claim_operation(worker_id="capacity-test")
    assert claimed is not None and claimed["kind"] == kind, claimed
    await r.worker.execute_claimed(claimed)
    return claimed


async def move(r) -> None:
    source = await r.repo.get_allocation(r.allocation_id)
    await r.commands.rotate(
        r.allocation_id,
        RotateComputeRequest(request_id="move-now", expected_revision=source["revision"]),
        actor="admin",
    )


async def operations_of(r, allocation_id: str, kind: str) -> list[dict]:
    return await r.repo.list_operations(allocation_id=allocation_id, kinds=[kind])


async def profile_of(r, allocation: dict) -> str:
    return (await r.repo.get_allocation(allocation["id"]))["deployment_profile"]["id"]


async def assert_never_created_and_released(r, candidate: dict) -> None:
    """A candidate refused for capacity: no identity, released as never created."""
    row = await r.repo.get_allocation(candidate["id"])
    assert row["external_id"] is None and row["nodes"] == []
    assert row["observed_state"] == "absent" and row["release_committed_at"] is not None
    [acquire] = await operations_of(r, candidate["id"], "acquire")
    assert acquire["state"] == "cancelled"
    assert acquire["result"]["blocked_reasons"][0]["code"] == "capacity_unavailable"
    assert not acquire["result"].get("resource_ref")
    [discard] = await operations_of(r, candidate["id"], "discard")
    assert discard["state"] == "succeeded"
    assert discard["result"]["release_evidence"] == NEVER_CREATED


async def test_a_type_without_capacity_gives_way_to_the_next_type_at_once(
    db, store, hosts, monkeypatch
):
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = NO_CAPACITY
    await move(r)

    settled = await settle(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert settled and rotation["state"] == "succeeded", (
        rotation["state"],
        rotation["step"],
        rotation["message"],
        r.cloud.posts,
    )
    [attempt] = rotation["result"]["attempts"]
    assert (attempt["node_type"], attempt["class"], attempt["code"]) == (
        "t1",
        "no_capacity",
        "capacity_unavailable",
    )
    # One create for each type: the refused type was not asked again.
    assert r.cloud.posts == ["first", "t1", "t2"]
    refused, replacement = await successor_of(db, r.allocation_id)
    await assert_never_created_and_released(r, refused)
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [r.host]
    assert target["nodes"][0]["name"] == "t2-2"
    # Nothing of the refused type existed to delete; only the old node went.
    old = await r.repo.get_allocation(r.allocation_id)
    assert r.cloud.released == [old["external_id"]]
    # The refused candidate's profile was free again for the next type.
    assert await profile_of(r, refused) == "slot-b"
    assert await profile_of(r, target) == "slot-b"


async def test_a_rate_limited_create_keeps_its_type_and_retries_it(db, store, hosts, monkeypatch):
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = RATE_LIMITED
    await move(r)

    assert not await settle(r, db, passes=12)

    rotation = await rotation_of(r, r.allocation_id)
    assert (rotation["state"], rotation["step"]) == ("waiting", "preparing_candidate")
    assert rotation["result"]["attempts"] == []
    [candidate] = await successor_of(db, r.allocation_id)
    [acquire] = await operations_of(r, candidate["id"], "acquire")
    assert acquire["state"] == "waiting"
    assert acquire["result"]["blocked_reasons"][0]["code"] == "rate_limited"
    assert await operations_of(r, candidate["id"], "discard") == []
    # The limit is retried on the same type; no other type is asked.
    assert set(r.cloud.posts[1:]) == {"t1"} and len(r.cloud.posts) > 2

    del r.cloud.portal["t1"]
    assert await settle(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded" and rotation["result"]["attempts"] == []
    target = await r.repo.get_allocation(candidate["id"])
    assert [h["host"] for h in target["hosts"]] == [r.host]


async def test_an_unknown_create_is_never_replaced_by_another_type(db, store, hosts, monkeypatch):
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = SERVER_ERROR
    await move(r)

    await settle(r, db, passes=12)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "blocked"
    assert rotation["result"]["blocked_reasons"][0]["code"] == "candidate_acquisition_unknown"
    [candidate] = await successor_of(db, r.allocation_id)
    [acquire] = await operations_of(r, candidate["id"], "acquire")
    assert acquire["state"] == "unknown"
    assert await operations_of(r, candidate["id"], "discard") == []
    assert r.cloud.posts == ["first", "t1"]


async def test_a_rejected_credential_stops_the_move_without_another_type(
    db, store, hosts, monkeypatch
):
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = UNAUTHORIZED
    await move(r)

    await settle(r, db, passes=20)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "blocked"
    assert rotation["result"]["blocked_reasons"][0]["code"] == "credentials"
    assert [a["class"] for a in rotation["result"]["attempts"]] == ["credentials"]
    assert r.cloud.posts == ["first", "t1"]
    [candidate] = await successor_of(db, r.allocation_id)
    [acquire] = await operations_of(r, candidate["id"], "acquire")
    assert acquire["state"] == "cancelled"
    assert acquire["result"]["blocked_reasons"][0]["code"] == "credential_invalid"


async def test_a_capacity_answer_about_a_created_experiment_keeps_that_experiment(
    db, store, hosts, monkeypatch
):
    """The create succeeded; a later read hit the limit with a capacity message.

    The candidate has a provider identity, so the move never gives it up for
    the message alone and never sends a second create.
    """
    r = await rig(db, store, hosts, monkeypatch)
    real_acquire = r.cloud.acquire

    async def acquire_then_limit_reads(request, intent):
        receipt = await real_acquire(request, intent)
        if request.config["bindings"]["phystype"] == "t1":
            r.cloud.reads[receipt.resource.external_id] = NO_CAPACITY
            # Like CloudLab: the create names the experiment; reads show its state.
            return ProviderReceipt(outcome="pending", resource=receipt.resource)
        return receipt

    monkeypatch.setattr(r.cloud, "acquire", acquire_then_limit_reads)
    await move(r)

    assert not await settle(r, db, passes=12)

    rotation = await rotation_of(r, r.allocation_id)
    assert (rotation["state"], rotation["step"]) == ("waiting", "preparing_candidate")
    assert rotation["result"]["attempts"] == []
    [candidate] = await successor_of(db, r.allocation_id)
    [acquire] = await operations_of(r, candidate["id"], "acquire")
    assert acquire["state"] == "waiting"
    assert acquire["result"]["blocked_reasons"][0]["code"] == "capacity_unavailable"
    assert acquire["result"]["resource_ref"]["external_id"] in r.cloud.reads
    assert await operations_of(r, candidate["id"], "discard") == []
    assert r.cloud.posts == ["first", "t1"]

    r.cloud.reads.clear()
    assert await settle(r, db)
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded" and rotation["result"]["attempts"] == []
    target = await r.repo.get_allocation(candidate["id"])
    assert [h["host"] for h in target["hosts"]] == [r.host]
    assert r.cloud.posts == ["first", "t1"]


@pytest.mark.parametrize(
    ("node_types", "refused_types", "stop"),
    [
        (("t1",), ("t1",), "candidates_exhausted"),
        (("t1", "t2"), ("t1", "t2"), "candidates_exhausted"),
        # Three refusals in a row stop before the fourth type is asked.
        (("t1", "t2", "t3", "t4"), ("t1", "t2", "t3"), "systemic_no_capacity"),
    ],
)
async def test_a_move_whose_types_all_lack_capacity_releases_them_and_stops(
    db, store, hosts, monkeypatch, node_types, refused_types, stop
):
    """No type waits out the candidate timeout, the last one included.

    Every refused candidate is released as never created; then the move
    blocks with the reason no further type is tried, and alerts once.
    """
    delivered: list[tuple[str, dict]] = []

    async def deliver(event, details):
        delivered.append((event, details))

    monkeypatch.setattr("cloud_agent.application.compute_rotation.deliver_event", deliver)
    r = await rig(db, store, hosts, monkeypatch, node_types=node_types)
    r.cloud.portal.update(dict.fromkeys(refused_types, NO_CAPACITY))
    await move(r)

    settled = await settle(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert settled and rotation["state"] == "blocked", (
        rotation["state"],
        rotation["step"],
        rotation["message"],
        r.cloud.posts,
    )
    assert rotation["result"]["blocked_reasons"][0]["code"] == stop
    assert [(a["node_type"], a["class"], a["code"]) for a in rotation["result"]["attempts"]] == [
        (node_type, "no_capacity", "capacity_unavailable") for node_type in refused_types
    ]
    # One create per type, and none after the move stopped.
    assert r.cloud.posts == ["first", *refused_types]
    candidates = await successor_of(db, r.allocation_id)
    assert len(candidates) == len(refused_types)
    for candidate in candidates:
        await assert_never_created_and_released(r, candidate)
    assert ("compute_rotation_blocked", {"host": r.host, "reason": stop}) in delivered
    # The Host keeps serving on its own node with claims open.
    source = await r.repo.get_allocation(r.allocation_id)
    assert [h["host"] for h in source["hosts"]] == [r.host]
    assert source["release_committed_at"] is None
    async with db.acquire() as conn:
        holds = await conn.fetchval(
            "SELECT count(*) FROM agent_host_admission_holds WHERE host=$1 AND purpose='rotation'",
            r.host,
        )
    assert holds == 0
    assert await settle(r, db)
    assert r.cloud.posts == ["first", *refused_types]


async def test_an_ordinary_acquisition_still_tries_its_types_in_one_pass_and_waits(
    db, store, hosts, monkeypatch
):
    """Outside a move, the adapter walks the preference order and the operation waits."""
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal.update(t1=NO_CAPACITY, t2=NO_CAPACITY)
    request_id = str(uuid4())
    created = await r.repo.create_allocation_operation(
        request_id=request_id,
        fingerprint=request_id,
        kind="acquire",
        requested_by="admin",
        parameters={},
        allocation={
            "name": "second",
            "provider_id": "cloudlab",
            "mode": "provision",
            "management_mode": "manage",
            "lease_managed": True,
            "release_supported": True,
            "configuration": {
                **SCOPE,
                "host_key_source": "registration",
                "bindings": {},
                "node_types": ["t1", "t2"],
            },
            "deployment_profile": _profile("slot-b").model_dump(mode="json"),
        },
    )
    posted = len(r.cloud.posts)

    for passes in (1, 2):
        await run_next(r, db, "acquire")
        [acquire] = await operations_of(r, created["allocation_id"], "acquire")
        assert (acquire["state"], acquire["step"]) == ("waiting", "pending")
        assert acquire["result"]["blocked_reasons"][0]["code"] == "capacity_unavailable"
        assert r.cloud.posts[posted:] == ["t1", "t2"] * passes

    allocation = await r.repo.get_allocation(created["allocation_id"])
    assert allocation["release_committed_at"] is None
    assert allocation["desired_state"] == "present"
    assert await operations_of(r, created["allocation_id"], "discard") == []


@pytest.mark.parametrize("racing_create", ["refused_again", "created"])
async def test_a_retry_running_when_the_move_gives_up_is_waited_for(
    db, store, hosts, monkeypatch, racing_create
):
    """The candidate's own retry was claimed between the move's read and its abandon.

    Nothing is released or requested again while that retry runs. A create it
    gets through is used; a refusal it gets is given up afterwards, once.
    """
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = NO_CAPACITY
    await move(r)
    await run_next(r, db, "rotate")
    child = await run_next(r, db, "acquire")
    abandon = r.repo.abandon_candidate
    racing: list[dict] = []

    async def abandon_after_a_claim(**kwargs):
        if not racing:
            await wake(db, child["id"])
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_compute_operations SET next_attempt_at=NOW()+interval '1 hour' "
                    "WHERE id<>$1 AND state='waiting'",
                    child["id"],
                )
            claimed = await r.repo.claim_operation(worker_id="racing-worker")
            assert claimed is not None and claimed["id"] == child["id"]
            racing.append(claimed)
        return await abandon(**kwargs)

    monkeypatch.setattr(r.repo, "abandon_candidate", abandon_after_a_claim)
    await run_next(r, db, "rotate")

    assert racing, "the move did not try to give up the refused candidate"
    rotation = await rotation_of(r, r.allocation_id)
    assert (rotation["state"], rotation["step"]) == ("waiting", "preparing_candidate")
    assert rotation["result"]["attempts"] == []
    [candidate] = await successor_of(db, r.allocation_id)
    assert await operations_of(r, candidate["id"], "discard") == []
    held = await r.repo.get_allocation(candidate["id"])
    assert held["observed_state"] != "absent" and held["release_committed_at"] is None

    if racing_create == "created":
        del r.cloud.portal["t1"]
    await r.worker.execute_claimed(racing[0])
    assert await settle(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    assert r.cloud.posts == (
        ["first", "t1", "t1", "t2"] if racing_create == "refused_again" else ["first", "t1", "t1"]
    )
    successors = await successor_of(db, r.allocation_id)
    if racing_create == "refused_again":
        assert [a["node_type"] for a in rotation["result"]["attempts"]] == ["t1"]
        refused, replacement = successors
        await assert_never_created_and_released(r, refused)
    else:
        assert rotation["result"]["attempts"] == []
        [replacement] = successors
        assert await operations_of(r, replacement["id"], "discard") == []
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [r.host]
    assert target["external_id"] not in r.cloud.released


@pytest.mark.parametrize("retry_answer", ["created", "unknown", "rate_limited", "refused_again"])
async def test_a_retry_finished_between_the_read_and_the_abandon_is_judged_by_its_result(
    db, store, hosts, monkeypatch, retry_answer
):
    """The candidate's retry ran to its end after the move read a refusal, before it gave up.

    Giving up is decided on the acquisition as it is when the store stops it:
    a created node, an unknown outcome or a rate limit keeps the candidate;
    only a fresh capacity refusal is given up.
    """
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = NO_CAPACITY
    await move(r)
    await run_next(r, db, "rotate")
    child = await run_next(r, db, "acquire")
    abandon = r.repo.abandon_candidate
    raced: list[dict] = []

    async def abandon_after_a_retry(**kwargs):
        if not raced:
            if retry_answer == "created":
                del r.cloud.portal["t1"]
            else:
                r.cloud.portal["t1"] = {
                    "unknown": SERVER_ERROR,
                    "rate_limited": RATE_LIMITED,
                    "refused_again": NO_CAPACITY,
                }[retry_answer]
            await wake(db, child["id"])
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_compute_operations SET next_attempt_at=NOW()+interval '1 hour' "
                    "WHERE id<>$1 AND state='waiting'",
                    child["id"],
                )
            claimed = await r.repo.claim_operation(worker_id="racing-worker")
            assert claimed is not None and claimed["id"] == child["id"]
            await r.worker.execute_claimed(claimed)
            raced.append(await r.repo.get_operation(child["id"]))
        return await abandon(**kwargs)

    monkeypatch.setattr(r.repo, "abandon_candidate", abandon_after_a_retry)
    await run_next(r, db, "rotate")

    assert raced, "the move did not try to give up the refused candidate"
    assert raced[0]["generation"] > child["generation"]
    assert r.cloud.posts == ["first", "t1", "t1"]
    rotation = await rotation_of(r, r.allocation_id)
    [candidate] = await successor_of(db, r.allocation_id)
    [acquire] = await operations_of(r, candidate["id"], "acquire")
    discards = await operations_of(r, candidate["id"], "discard")
    if retry_answer == "refused_again":
        assert acquire["state"] == "cancelled" and len(discards) == 1
        assert [
            (a["node_type"], a["class"], a["code"]) for a in rotation["result"]["attempts"]
        ] == [("t1", "no_capacity", "capacity_unavailable")]
    else:
        assert acquire["state"] != "cancelled" and discards == [], (acquire["state"], discards)
        assert rotation["result"]["attempts"] == []
        held = await r.repo.get_allocation(candidate["id"])
        assert held["release_committed_at"] is None

    if retry_answer == "rate_limited":
        del r.cloud.portal["t1"]
    await settle(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    if retry_answer == "unknown":
        assert rotation["state"] == "blocked"
        assert rotation["result"]["blocked_reasons"][0]["code"] == "candidate_acquisition_unknown"
        [acquire] = await operations_of(r, candidate["id"], "acquire")
        assert acquire["state"] == "unknown"
        assert await operations_of(r, candidate["id"], "discard") == []
        assert r.cloud.posts == ["first", "t1", "t1"]
        return
    assert rotation["state"] == "succeeded", rotation
    successors = await successor_of(db, r.allocation_id)
    if retry_answer == "refused_again":
        assert r.cloud.posts == ["first", "t1", "t1", "t2"]
        refused, replacement = successors
        await assert_never_created_and_released(r, refused)
    else:
        assert r.cloud.posts == ["first", "t1", "t1"] + (
            ["t1"] if retry_answer == "rate_limited" else []
        )
        [replacement] = successors
        assert await operations_of(r, replacement["id"], "discard") == []
        assert rotation["result"]["attempts"] == []
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [r.host]
    assert target["external_id"] not in r.cloud.released


async def test_a_move_restarted_after_giving_up_a_candidate_releases_it_once(
    db, store, hosts, monkeypatch
):
    """The process died after the store stopped the candidate, before the move's checkpoint.

    The restarted move still records the original capacity refusal, keeps the
    one release already queued and asks no type twice.
    """
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = NO_CAPACITY
    await move(r)
    abandon = r.repo.abandon_candidate
    lost: list[str] = []

    async def abandon_then_lose_the_lease(**kwargs):
        discard = await abandon(**kwargs)
        if discard is not None and not lost:
            lost.append(discard["id"])
            raise ValueError("operation_lease_lost")
        return discard

    monkeypatch.setattr(r.repo, "abandon_candidate", abandon_then_lose_the_lease)
    await run_next(r, db, "rotate")
    await run_next(r, db, "acquire")
    await run_next(r, db, "rotate")
    assert lost, "the move did not give up the refused candidate"
    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "running" and not rotation["result"].get("attempts")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agent_compute_operations SET lease_expires_at=NOW() WHERE id=$1",
            rotation["id"],
        )

    assert await settle(r, db)

    rotation = await rotation_of(r, r.allocation_id)
    assert rotation["state"] == "succeeded", rotation
    refused, replacement = await successor_of(db, r.allocation_id)
    [attempt] = rotation["result"]["attempts"]
    assert (attempt["candidate"], attempt["node_type"], attempt["class"], attempt["code"]) == (
        refused["id"],
        "t1",
        "no_capacity",
        "capacity_unavailable",
    )
    await assert_never_created_and_released(r, refused)
    [discard] = await operations_of(r, refused["id"], "discard")
    assert discard["id"] == lost[0]
    assert r.cloud.posts == ["first", "t1", "t2"]
    target = await r.repo.get_allocation(replacement["id"])
    assert [h["host"] for h in target["hosts"]] == [r.host]
    async with db.acquire() as conn:
        second = await conn.fetchval(
            "SELECT count(*) FROM agent_compute_operations WHERE request_id=$1",
            f"rotate:{rotation['id']}:2",
        )
    assert second == 1


async def schedule_move(r, db, allocation_id: str) -> dict:
    """Age a node into its renewal window and let the scheduler start its move."""
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE agent_compute_allocations
            SET created_at=NOW()-interval '8 days',expires_at=NOW()+interval '150 minutes',
                expiration_known=TRUE WHERE id=$1""",
            allocation_id,
        )
    await r.worker.reconciler.schedule()
    [rotation] = await operations_of(r, allocation_id, "rotate")
    assert rotation["parameters"]["trigger"] == "max_age"
    return rotation


async def test_two_scheduled_moves_reuse_profiles_and_copy_only_the_current_node(
    db, store, hosts, monkeypatch
):
    """slot-a → slot-b → slot-a, each move first refused capacity for t1.

    The second move's replacement takes the profile the original node held.
    It must be filled from the first move's target, where the Host wrote new
    state, and nothing may copy the original node's old state over that.
    """
    r = await rig(db, store, hosts, monkeypatch)
    r.cloud.portal["t1"] = NO_CAPACITY
    original = await r.repo.get_allocation(r.allocation_id)

    await schedule_move(r, db, r.allocation_id)
    assert await settle(r, db)
    first_move = await rotation_of(r, r.allocation_id)
    assert first_move["state"] == "succeeded", first_move
    refused_1, target_1 = await successor_of(db, r.allocation_id)
    await assert_never_created_and_released(r, refused_1)
    target_1 = await r.repo.get_allocation(target_1["id"])
    node_1 = target_1["nodes"][0]["name"]
    assert [h["host"] for h in target_1["hosts"]] == [r.host]
    # The Host runs on the first target and writes there.
    r.nodes.data[node_1] = "written-on-first-target"
    calls_before = len(r.nodes.calls)

    await schedule_move(r, db, target_1["id"])
    assert await settle(r, db)
    second_move = await rotation_of(r, target_1["id"])
    assert second_move["state"] == "succeeded", second_move
    assert [a["node_type"] for a in second_move["result"]["attempts"]] == ["t1"]
    refused_2, target_2 = await successor_of(db, target_1["id"])
    await assert_never_created_and_released(r, refused_2)
    target_2 = await r.repo.get_allocation(target_2["id"])
    node_2 = target_2["nodes"][0]["name"]
    assert [h["host"] for h in target_2["hosts"]] == [r.host]

    profiles = [
        await profile_of(r, row) for row in (original, refused_1, target_1, refused_2, target_2)
    ]
    assert profiles == ["slot-a", "slot-b", "slot-b", "slot-a", "slot-a"]
    original_node = original["nodes"][0]["name"]
    copies = [call for call in r.nodes.calls[calls_before:] if call[0].startswith("transfer-")]
    assert copies and {call[1] for call in copies} == {f"{node_1}->{node_2}"}
    assert not any(
        original_node in call[1] or call[1].endswith(f"->{node_1}")
        for call in r.nodes.calls[calls_before:]
    )
    assert r.nodes.data[node_2] == "written-on-first-target"
    assert r.cloud.posts == ["first", "t1", "t2", "t1", "t2"]
    assert r.cloud.released == [original["external_id"], target_1["external_id"]]
