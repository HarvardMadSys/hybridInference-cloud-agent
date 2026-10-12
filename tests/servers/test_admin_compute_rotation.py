"""Moving a server's Host to a replacement node through the administrator API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.servers.test_admin_compute import ADMIN, allocation, compute_api as compute_api

from cloud_agent.schemas_compute import RenewalPolicy

POLICY = RenewalPolicy(
    enabled=True,
    reason="Keep the evaluation sandboxes",
    renew_before_seconds=3 * 3600,
    extend_to_horizon_seconds=8 * 3600,
    rotation_enabled=True,
    node_types=["r6615", "d6515"],
)


async def movable(client, repo, **overrides):
    _, operation = await allocation(client)
    row = repo.allocations[operation["allocation_id"]]
    row.update(
        {
            "management_mode": "manage",
            "scope_complete": True,
            "external_id": "experiment-a",
            "observed_state": "ready",
            "created_at": datetime.now(UTC) - timedelta(days=2),
            "expires_at": datetime.now(UTC) + timedelta(hours=10),
            "expiration_known": True,
            "configuration": {"project": "research", "host_key_source": "registration"},
            "policy": POLICY.model_dump(),
            "nodes": [{"id": "node-a", "name": "Machine A", "connection": {}}],
            "hosts": [{"host": "compute-a", "compute_node_id": "node-a"}],
            **overrides,
        }
    )
    return row


@pytest.mark.asyncio
async def test_move_now_queues_one_audited_rotation(compute_api):
    client, repo, _ = compute_api
    row = await movable(client, repo)
    body = {"request_id": "move-a", "expected_revision": row["revision"]}
    response = await client.post(
        f"/admin/agent/compute-allocations/{row['id']}/rotate", headers=ADMIN, json=body
    )
    assert response.status_code == 202, response.text
    operation = response.json()["operation"]
    assert operation["kind"] == "rotate" and operation["allocation_id"] == row["id"]
    stored = repo.operations[operation["id"]]
    assert stored["parameters"]["trigger"] == "manual"
    assert stored["requested_by"] == "admin-token"
    assert repo.audit_rows[-1]["action"] == "agent.compute.allocation.rotate"
    replay = await client.post(
        f"/admin/agent/compute-allocations/{row['id']}/rotate", headers=ADMIN, json=body
    )
    assert replay.json()["operation"]["id"] == operation["id"]


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"configuration": {"project": "research"}}, 409, "rotation_needs_registration"),
        ({"mode": "attach"}, 409, "rotation_unsupported"),
        ({"hosts": []}, 409, "rotation_unsupported"),
        ({"policy": RenewalPolicy().model_dump()}, 422, "rotation_node_types_missing"),
        ({"management_mode": "observe"}, 409, "unmanaged_scope"),
    ],
)
@pytest.mark.asyncio
async def test_move_now_refuses_servers_that_cannot_move(compute_api, change, status, code):
    client, repo, _ = compute_api
    row = await movable(client, repo, **change)
    response = await client.post(
        f"/admin/agent/compute-allocations/{row['id']}/rotate",
        headers=ADMIN,
        json={"request_id": "move-a", "expected_revision": row["revision"]},
    )
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code
    assert not [op for op in repo.operations.values() if op["kind"] == "rotate"]


@pytest.mark.asyncio
async def test_autoscaling_member_has_no_manual_policy_or_rotation_escape(compute_api):
    client, repo, _ = compute_api
    row = await movable(client, repo, autoscaling_owned=True)

    servers = (await client.get("/admin/agent/servers", headers=ADMIN)).json()
    [view] = [item for item in servers["allocations"] if item["id"] == row["id"]]
    assert "policy" not in view["allowed_actions"]
    assert "rotate" not in view["allowed_actions"]
    assert view["rotation"]["enabled"] is False
    assert view["rotation"]["replace_after"] is None
    assert "autoscaling_managed" in {reason["code"] for reason in view["blocked_reasons"]}

    for policy in (RenewalPolicy(), POLICY):
        refused = await client.patch(
            f"/admin/agent/compute-allocations/{row['id']}/policy",
            headers=ADMIN,
            json={"expected_revision": row["revision"], "policy": policy.model_dump()},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "autoscaling_managed"

    refused = await client.post(
        f"/admin/agent/compute-allocations/{row['id']}/rotate",
        headers=ADMIN,
        json={"request_id": "owned-move", "expected_revision": row["revision"]},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "autoscaling_managed"
    assert not [op for op in repo.operations.values() if op["kind"] == "rotate"]


@pytest.mark.asyncio
async def test_a_second_move_is_refused_while_one_runs(compute_api, monkeypatch):
    client, repo, _ = compute_api
    row = await movable(client, repo)
    original = repo.create_operation

    async def busy(**kwargs):
        if kwargs["kind"] == "rotate":
            raise ValueError("rotation_in_progress")
        return await original(**kwargs)

    monkeypatch.setattr(repo, "create_operation", busy)
    response = await client.post(
        f"/admin/agent/compute-allocations/{row['id']}/rotate",
        headers=ADMIN,
        json={"request_id": "move-b", "expected_revision": row["revision"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "rotation_in_progress"


@pytest.mark.asyncio
async def test_rotation_policy_needs_nodes_that_register_their_keys(compute_api):
    client, repo, _ = compute_api
    row = await movable(client, repo, configuration={"project": "research"})
    body = {"expected_revision": row["revision"], "policy": POLICY.model_dump()}
    refused = await client.patch(
        f"/admin/agent/compute-allocations/{row['id']}/policy", headers=ADMIN, json=body
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "rotation_needs_registration"
    row["configuration"] = {"project": "research", "host_key_source": "registration"}
    saved = await client.patch(
        f"/admin/agent/compute-allocations/{row['id']}/policy", headers=ADMIN, json=body
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["allocation"]["policy"]["node_types"] == ["r6615", "d6515"]


@pytest.mark.asyncio
async def test_rotation_progress_is_public_and_cancellable_only_before_the_stop(compute_api):
    client, repo, _ = compute_api
    row = await movable(client, repo)
    response = await client.post(
        f"/admin/agent/compute-allocations/{row['id']}/rotate",
        headers=ADMIN,
        json={"request_id": "move-a", "expected_revision": row["revision"]},
    )
    operation_id = response.json()["operation"]["id"]
    stored = repo.operations[operation_id]
    stored.update(
        state="waiting",
        step="draining",
        result={
            "slot": {"host": "compute-a", "node_id": "node-a", "namespace": "ca-nodea"},
            "candidate": {"id": "allocation-b", "node_type": "r6615"},
            "attempts": [{"node_type": "d6515", "class": "no_capacity", "code": "resource_failed"}],
            "hold_set": True,
        },
    )
    view = (
        await client.get(f"/admin/agent/compute-operations/{operation_id}", headers=ADMIN)
    ).json()
    assert view["operation"]["allowed_actions"] == ["cancel"]
    assert view["operation"]["result"] == {"attempts": stored["result"]["attempts"]}
    stored["result"]["source_stopped"] = True
    view = (
        await client.get(f"/admin/agent/compute-operations/{operation_id}", headers=ADMIN)
    ).json()
    assert view["operation"]["allowed_actions"] == []
    refused = await client.post(
        f"/admin/agent/compute-operations/{operation_id}/cancel", headers=ADMIN, json={}
    )
    assert refused.status_code == 409


@pytest.mark.asyncio
async def test_servers_show_when_a_node_is_next_renewed_or_replaced(compute_api):
    client, repo, _ = compute_api
    row = await movable(client, repo)
    servers = (await client.get("/admin/agent/servers", headers=ADMIN)).json()
    [view] = [item for item in servers["allocations"] if item["id"] == row["id"]]
    assert "rotate" in view["allowed_actions"]
    rotation = view["rotation"]
    assert rotation["enabled"] is True and rotation["last"] is None
    decide_at = datetime.fromisoformat(rotation["decide_at"])
    assert abs(decide_at - (row["expires_at"] - timedelta(hours=3))) < timedelta(seconds=1)
    replace_after = datetime.fromisoformat(rotation["replace_after"])
    assert abs(replace_after - (row["created_at"] + timedelta(days=7))) < timedelta(seconds=1)
    row["rotation"] = {"id": "rotate-1", "state": "waiting", "step": "draining", "message": "m"}
    servers = (await client.get("/admin/agent/servers", headers=ADMIN)).json()
    [view] = [item for item in servers["allocations"] if item["id"] == row["id"]]
    assert "rotate" not in view["allowed_actions"]
    assert view["rotation"]["last"]["step"] == "draining"
