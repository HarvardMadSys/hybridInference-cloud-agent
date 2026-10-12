"""When a Host moves, which node it tries next, and how the scheduler starts a move."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.application.compute_rotation import (
    ComputeRotation,
    candidate_configuration,
    classify_candidate_failure,
    next_node_type,
)
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.schemas_compute import RenewalPolicy

NOW = datetime.now(UTC)
EXPIRES = NOW + timedelta(hours=2)
POLICY = RenewalPolicy(
    enabled=True,
    reason="Keep the evaluation sandboxes",
    renew_before_seconds=3 * 3600,
    extend_to_horizon_seconds=8 * 3600,
    rotation_enabled=True,
    node_types=["r6615", "c6620", "d6515"],
)


def row(**overrides):
    base = {
        "id": "allocation-a",
        "name": "lab",
        "mode": "provision",
        "management_mode": "manage",
        "created_at": NOW - timedelta(days=2),
        "external_id": "experiment-a",
        "observed_at": NOW,
        "expires_at": EXPIRES,
        "expiration_known": True,
        "revision": 3,
        "policy": POLICY.model_dump(),
        "hosts": [{"host": "compute-a"}],
        "nodes": [{"id": "node-a"}],
    }
    return {**base, **overrides}


def renewal(state: str) -> dict:
    return {
        "id": "extend-1",
        "kind": "extend",
        "state": state,
        "request_id": f"renew:allocation-a:{int(EXPIRES.timestamp())}:3",
    }


def rotation() -> ComputeRotation:
    reconciler = SimpleNamespace(
        repository=SimpleNamespace(), host_pool=SimpleNamespace(), bootstrap=None, commands=None
    )
    return ComputeRotation(reconciler)  # type: ignore[arg-type]


def test_rotation_policy_requires_renewal_and_candidates():
    with pytest.raises(ValueError, match="candidate node types"):
        RenewalPolicy(enabled=True, reason="x", rotation_enabled=True)
    with pytest.raises(ValueError, match="distinct"):
        RenewalPolicy(enabled=True, reason="x", node_types=["a", "a"])
    assert RenewalPolicy().max_node_age_seconds == 7 * 24 * 3600


@pytest.mark.parametrize(
    ("operations", "created", "expected"),
    [
        ([], NOW - timedelta(days=2), None),
        ([renewal("succeeded")], NOW - timedelta(days=2), None),
        ([renewal("blocked")], NOW - timedelta(days=2), "renewal_refused"),
        ([renewal("failed")], NOW - timedelta(days=2), "renewal_refused"),
        ([], NOW - timedelta(days=8), "max_age"),
        # Another move of this allocation, finished or not, rules a new one out.
        (
            [renewal("blocked"), {"kind": "rotate", "state": "blocked", "request_id": "r"}],
            NOW - timedelta(days=2),
            None,
        ),
    ],
)
def test_a_move_starts_on_refusal_or_age(operations, created, expected):
    assert rotation().due(row(created_at=created), POLICY, operations, NOW, EXPIRES) == expected


def test_an_unconfirmed_extension_moves_the_host_halfway_through_the_lead_time():
    soon = NOW + timedelta(hours=1)
    waiting = [
        {**renewal("waiting"), "request_id": f"renew:allocation-a:{int(soon.timestamp())}:3"}
    ]
    assert rotation().due(row(), POLICY, waiting, NOW, soon) == "renewal_unconfirmed"
    assert rotation().due(row(), POLICY, waiting, NOW, NOW + timedelta(hours=2)) is None


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "attach"},
        {"management_mode": "observe"},
        {"hosts": []},
        {"autoscaling_owned": True},
        {"policy": {**POLICY.model_dump(), "rotation_enabled": False}},
    ],
)
def test_only_a_managed_provisioned_single_host_moves(change):
    policy = RenewalPolicy.model_validate(change.get("policy", POLICY.model_dump()))
    assert rotation().due(row(**change), policy, [renewal("blocked")], NOW, EXPIRES) is None


def test_next_type_skips_tried_types_and_stops_on_hopeless_failures():
    def attempt(node_type, failure_class):
        return {"node_type": node_type, "class": failure_class}

    assert next_node_type(POLICY, []) == ("r6615", None)
    assert next_node_type(POLICY, [attempt("r6615", "no_capacity")]) == ("c6620", None)
    assert next_node_type(POLICY, [attempt("r6615", "credentials")]) == (None, "credentials")
    assert next_node_type(POLICY, [attempt("r6615", "configuration")]) == (None, "configuration")
    three = [attempt(t, "no_capacity") for t in ("r6615", "c6620", "d6515")]
    assert next_node_type(POLICY, three) == (None, "systemic_no_capacity")
    mixed = [
        attempt("r6615", "no_capacity"),
        attempt("c6620", "setup_failed"),
        attempt("d6515", "no_capacity"),
    ]
    assert next_node_type(POLICY, mixed) == (None, "candidates_exhausted")


@pytest.mark.parametrize(
    ("code", "nodes", "expected"),
    [
        ("credential_invalid", [], "credentials"),
        ("permission_denied", [], "credentials"),
        ("request_rejected", [], "configuration"),
        ("registration_unavailable", [], "configuration"),
        ("resource_failed", [], "no_capacity"),
        ("resource_failed", [{"id": "n"}], "setup_failed"),
        ("registration_expired", [{"id": "n"}], "setup_failed"),
        ("prerequisites_install_failed", [{"id": "n"}], "setup_failed"),
    ],
)
def test_failures_are_classified_by_what_can_be_seen(code, nodes, expected):
    operation = {"state": "blocked", "result": {"blocked_reasons": [{"code": code}]}}
    assert classify_candidate_failure(operation, {"nodes": nodes}) == (expected, code)


def test_replacement_request_keeps_scope_but_never_the_old_identity():
    source = {
        "project": "lab",
        "external_id": "old-experiment",
        "name": "ca-old",
        "profile_name": "cloud-agent-node",
        "host_key_source": "registration",
        "ssh_public_key": "ssh-ed25519 AAAA researcher",
        "bindings": {"phystype": "c6420", "dataMount": "/mydata"},
    }
    derived = candidate_configuration(source, POLICY, "r6615")
    assert "external_id" not in derived and "name" not in derived
    assert derived["ssh_public_key"] == "ssh-ed25519 AAAA researcher"
    assert derived["bindings"] == {"phystype": "r6615", "dataMount": "/mydata"}
    assert derived["host_key_source"] == "registration"
    assert source["bindings"]["phystype"] == "c6420"


async def test_scheduler_moves_an_old_node_instead_of_extending_it(monkeypatch):
    created = []
    old = row(created_at=NOW - timedelta(days=8))

    async def list_operations(**kwargs):
        return []

    async def create_operation(**kwargs):
        created.append(kwargs)
        return {"id": "op", **kwargs}

    repository = SimpleNamespace(
        list_allocations=AsyncMock(return_value=[old]),
        list_operations=AsyncMock(side_effect=list_operations),
        create_operation=AsyncMock(side_effect=create_operation),
    )
    commands = SimpleNamespace(
        repository=repository,
        configuration=SimpleNamespace(observation_seconds=120),
        extend=AsyncMock(),
        queue_inspection=AsyncMock(),
    )
    reconciler = ComputeReconciler(
        commands=commands,  # type: ignore[arg-type]
        host_pool=SimpleNamespace(),  # type: ignore[arg-type]
        bootstrap=SimpleNamespace(),  # type: ignore[arg-type]
    )
    await reconciler.schedule()
    assert [entry["kind"] for entry in created] == ["rotate"]
    assert created[0]["parameters"] == {"allocation_id": "allocation-a", "trigger": "max_age"}
    commands.extend.assert_not_awaited()


async def test_scheduler_extends_owned_autoscaling_node_instead_of_rotating_legacy_policy():
    old = row(created_at=NOW - timedelta(days=8), autoscaling_owned=True)
    repository = SimpleNamespace(
        list_allocations=AsyncMock(return_value=[old]),
        list_operations=AsyncMock(return_value=[]),
        create_operation=AsyncMock(),
    )
    commands = SimpleNamespace(
        repository=repository,
        configuration=SimpleNamespace(observation_seconds=120),
        extend=AsyncMock(),
        queue_inspection=AsyncMock(),
    )
    reconciler = ComputeReconciler(
        commands=commands,  # type: ignore[arg-type]
        host_pool=SimpleNamespace(),  # type: ignore[arg-type]
        bootstrap=SimpleNamespace(),  # type: ignore[arg-type]
    )

    await reconciler.schedule()

    commands.extend.assert_awaited_once()
    repository.create_operation.assert_not_awaited()


async def test_scheduler_still_extends_when_the_move_is_stuck(monkeypatch):
    old = row(created_at=NOW - timedelta(days=8))
    stuck = {"id": "rotate-1", "kind": "rotate", "state": "blocked", "request_id": "r", "step": "x"}
    repository = SimpleNamespace(
        list_allocations=AsyncMock(return_value=[old]),
        list_operations=AsyncMock(return_value=[stuck]),
        create_operation=AsyncMock(),
    )
    commands = SimpleNamespace(
        repository=repository,
        configuration=SimpleNamespace(observation_seconds=120),
        extend=AsyncMock(),
        queue_inspection=AsyncMock(),
    )
    reconciler = ComputeReconciler(
        commands=commands,  # type: ignore[arg-type]
        host_pool=SimpleNamespace(),  # type: ignore[arg-type]
        bootstrap=SimpleNamespace(),  # type: ignore[arg-type]
    )
    await reconciler.schedule()
    commands.extend.assert_awaited_once()
    repository.create_operation.assert_not_awaited()


async def test_an_unfinished_move_alerts_once_in_the_final_hour(monkeypatch):
    delivered = []

    async def deliver(event, details):
        delivered.append((event, details))

    monkeypatch.setattr("cloud_agent.application.compute_rotation.deliver_event", deliver)
    active = {"id": "rotate-1", "kind": "rotate", "state": "waiting", "step": "draining"}
    mover = rotation()
    late = NOW + timedelta(minutes=50)
    await mover.remind(row(), [active], NOW, late)
    await mover.remind(row(), [active], NOW, late)
    await mover.remind(row(), [active], NOW, NOW + timedelta(hours=2))
    assert delivered == [
        ("compute_rotation_unfinished", {"host": "compute-a", "reason": "draining"})
    ]


async def test_replacement_profile_keeps_the_source_release_and_bundle():
    mover = rotation()
    mover.repository.list_allocations = AsyncMock(return_value=[])
    compatible = SimpleNamespace(id="same", deployment_sha="a", source_sha256="b")
    profiles = {
        "different-release": SimpleNamespace(
            id="different-release", deployment_sha="c", source_sha256="b"
        ),
        "different-bundle": SimpleNamespace(
            id="different-bundle", deployment_sha="a", source_sha256="c"
        ),
        "same": compatible,
    }
    mover.commands = SimpleNamespace(
        deployment_profiles=AsyncMock(return_value=profiles),
    )
    assert (
        await mover._free_profile(
            exclude={"id": "source", "deployment_sha": "a", "source_sha256": "b"}
        )
        is compatible
    )


async def test_replacement_profile_preserves_the_hosts_explicit_storage_path():
    mover = rotation()
    mover.repository.list_allocations = AsyncMock(return_value=[])
    spare = DeploymentProfile(
        id="spare",
        label="Spare host setup",
        source_bundle="/bundle.tar.gz",
        source_sha256="a" * 64,
        deployment_sha="b" * 40,
        environment_ref="deployment:spare",
        data_mount="/profile-disk",
        sandbox_storage_path="/spare-data",
    )
    mover.commands = SimpleNamespace(deployment_profiles=AsyncMock(return_value={"spare": spare}))
    selected = await mover._free_profile(
        exclude={
            "id": "source",
            "source_sha256": spare.source_sha256,
            "deployment_sha": spare.deployment_sha,
            "sandbox_storage_path": "/host-data",
        }
    )
    assert selected is not None
    assert selected.id == "spare"
    assert selected.sandbox_storage_path == "/host-data"
    assert selected.data_mount == "/profile-disk"
    assert spare.sandbox_storage_path == "/spare-data"
