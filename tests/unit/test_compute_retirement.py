"""Retirement across real reconciler steps: retention, left-behind containers, the inventory proof.

Split from ``test_compute_reconcile``, whose harness these tests share. They
drive a ``retire`` operation through the reconciler and assert on the provider
requests, the Host's removal requests and the operation's recorded outcome.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.test_compute_reconcile import NOW, REF, harness, observed

from cloud_agent.domain.compute import public_operation
from cloud_agent.ports.external.compute_provider import (
    ProviderError,
    ProviderReceipt,
)
from cloud_agent.ports.external.host_bootstrap import HostInventory
from cloud_agent.sandbox_manager.sandbox_manager_client import SandboxManagerError
from cloud_agent.schemas.compute import RenewalPolicy


@pytest.mark.asyncio
async def test_committed_release_queries_before_replaying_same_immutable_resource(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="retiring")
    h.operation["result"]["release_requested"] = True
    events = []

    async def inspect(ref):
        events.append(("inspect", ref.external_id))
        return observed()

    async def release(ref, _intent):
        events.append(("release", ref.external_id))
        return ProviderReceipt(
            outcome="confirmed", resource=ref, observation=observed(state="terminated")
        )

    h.provider.inspect.side_effect = inspect
    h.provider.release.side_effect = release
    await h.reconciler.execute(h.operation)
    assert events == [("inspect", REF.external_id), ("release", REF.external_id)]
    assert h.operation["state"] == "succeeded"
    h.repository.begin_retirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_of_a_server_the_provider_never_created_finishes_locally(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=False)
    h.allocation.update(
        external_id=None,
        nodes=[],
        hosts=[],
        scope_complete=False,
        observed_at=None,
        observed_state="unknown",
        desired_state="present",
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    assert h.allocation["observed_state"] == "absent"
    assert h.allocation["provider_state"] == "not_created"
    h.provider.release.assert_not_awaited()
    h.provider.inspect.assert_not_awaited()
    h.repository.record_inventory.assert_not_awaited()
    h.repository.commit_release.assert_awaited_once()
    observation = h.repository.observe_allocation.await_args.kwargs["observation"]
    assert observation["observed_state"] == "absent"
    assert observation["scope_complete"] is True


@pytest.mark.asyncio
async def test_incomplete_scope_still_blocks_release_of_an_identified_server(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.repository.retirement_impact.return_value = {"blocked_reasons": ["unmanaged_scope"]}

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.operation["step"] == "waiting_for_retention"
    h.repository.commit_release.assert_not_awaited()
    h.provider.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_precommit_terminal_retire_skips_impossible_retention_wait(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.provider.inspect.return_value = observed(state="terminated")
    h.repository.retirement_impact.return_value = {"blocked_reasons": ["retained_groups_present"]}

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    assert h.operation["result"]["external_pending"] is False
    h.repository.begin_retirement.assert_awaited_once()
    h.repository.retirement_impact.assert_not_awaited()
    h.repository.record_inventory.assert_not_awaited()
    h.repository.commit_release.assert_awaited_once()
    h.provider.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_settles_retirement_already_observed_terminal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(desired_state="absent", observed_state="terminated")

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    h.commands._require_managed.assert_not_called()
    h.repository.begin_retirement.assert_not_awaited()
    h.repository.commit_release.assert_awaited_once()
    h.provider.inspect.assert_not_awaited()
    h.provider.release.assert_not_awaited()


def _left_behind_container():
    return {
        "sandbox_id": "asbx_moved",
        "generation": 1,
        "container_id": "container-old",
        "origin_thread_id": "athr_moved",
        "running": False,
    }


def _left_behind(h, *, removal=None):
    """A Host holding one stopped container its moved group left behind."""
    container = _left_behind_container()
    h.sandbox_manager.list_sandboxes = AsyncMock(side_effect=[[container], []])
    h.sandbox_manager.discard_sandbox_deletion = AsyncMock(side_effect=removal)
    authorized = {key: container[key] for key in container if key != "running"}
    h.repository.authorize_superseded_removals = AsyncMock(
        return_value=[{**authorized, "request_id": "superseded-recorded"}]
    )
    h.repository.record_superseded_removal = AsyncMock()
    # The durable count follows what was confirmed.
    h.repository.superseded_removals = AsyncMock(
        side_effect=lambda **_: h.repository.record_superseded_removal.await_count
    )
    h.bootstrap.inventory.return_value = HostInventory(complete=True)

    def receipt(_thread_id, payload):
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    if removal is None:
        h.sandbox_manager.discard_sandbox_deletion.side_effect = receipt
    return container


@pytest.mark.asyncio
async def test_release_removes_what_moved_groups_left_behind_then_releases(monkeypatch):
    """The Host still holds the stopped container of a group that moved away.

    Nobody has written to that chat since, so the group is still ``cold``:
    its container goes anyway, and the release goes on.
    """
    h = harness(monkeypatch, kind="retire", identified=True)
    container = _left_behind(h)

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["step"] == "released"
    assert h.operation["result"]["superseded_removed"] == 1
    h.repository.authorize_superseded_removals.assert_awaited_once_with(
        host="compute-nodeid", containers=[container], operation_id=h.operation["id"]
    )
    thread_id, payload = h.sandbox_manager.discard_sandbox_deletion.await_args.args
    assert thread_id == "athr_moved"
    # Sent under the request id the authorization recorded.
    assert payload == {
        "request_id": "superseded-recorded",
        "generation": 1,
        "expected_container_id": "container-old",
        "sandbox_id": "asbx_moved",
    }
    h.repository.record_superseded_removal.assert_awaited_once_with(
        request_id="superseded-recorded"
    )
    h.bootstrap.inventory.assert_awaited_once()
    h.repository.commit_release.assert_awaited_once()
    h.provider.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_stops_the_runner_before_reading_the_inventory(monkeypatch):
    """A refused runner restarting under its policy would leave self-tests to count."""
    h = harness(monkeypatch, kind="retire", identified=True)
    h.bootstrap.inventory.return_value = HostInventory(complete=True)
    order = Mock()
    order.attach_mock(h.bootstrap.quiesce, "quiesce")
    order.attach_mock(h.bootstrap.inventory, "inventory")
    h.bootstrap.quiesce.return_value = 1

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert [name for name, *_ in order.mock_calls] == ["quiesce", "inventory"]
    assert h.operation["result"]["inventory"] == [
        {
            "node_id": h.allocation["nodes"][0]["id"],
            "probes_removed": 1,
            "complete": True,
            "retained_containers": 0,
            "unknown_containers": 0,
            "orphan_state": False,
            "blocked_reasons": [],
        }
    ]


@pytest.mark.asyncio
async def test_a_refused_inventory_keeps_what_the_node_showed(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.bootstrap.inventory.return_value = HostInventory(
        complete=True,
        retained_containers=1,
        blocked_reasons=("retained_containers_present",),
    )

    await h.reconciler.execute(h.operation)

    # Something is there: nothing changes it by waiting, so it needs attention.
    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "inventory_not_empty"
    (scan,) = h.operation["result"]["inventory"]
    assert scan["retained_containers"] == 1
    assert scan["blocked_reasons"] == ["retained_containers_present"]
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_inventory_that_could_not_be_read_is_read_again(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.bootstrap.inventory.return_value = HostInventory(
        complete=False, blocked_reasons=("host_inventory_unavailable",)
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["inventory"][0]["blocked_reasons"] == [
        "host_inventory_unavailable"
    ]
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_runner_that_could_not_be_stopped_is_never_inventoried(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.bootstrap.quiesce.side_effect = ProviderError(
        "host_quiesce_failed", "The releasing Host's runner could not be stopped", retryable=True
    )

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    h.bootstrap.inventory.assert_not_awaited()
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_records_that_it_stops_runners_before_the_first_stop(monkeypatch):
    """A stop whose answer is lost still ends cancellation: the record comes first."""
    h = harness(monkeypatch, kind="retire", identified=True)
    order = Mock()
    order.attach_mock(h.repository.checkpoint_operation, "checkpoint")
    order.attach_mock(h.bootstrap.quiesce, "quiesce")
    h.bootstrap.quiesce.side_effect = ProviderError(
        "result_unknown", "The node's answer was lost", retryable=True
    )

    await h.reconciler.execute(h.operation)

    stopping = [
        name
        for name, _args, kwargs in order.mock_calls
        if name == "quiesce" or (kwargs.get("result") or {}).get("runners_stopping")
    ]
    assert stopping[:2] == ["checkpoint", "quiesce"]
    assert h.operation["result"]["runners_stopping"] is True
    assert "cancel" not in public_operation(h.operation)["allowed_actions"]


@pytest.mark.asyncio
async def test_release_keeps_a_container_still_needed_and_stops_for_attention(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h)
    h.repository.authorize_superseded_removals.return_value = []

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "retained_groups_present"
    h.sandbox_manager.discard_sandbox_deletion.assert_not_awaited()
    h.repository.commit_release.assert_not_awaited()
    h.provider.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_never_offers_a_running_container_for_removal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    container = _left_behind(h)
    container["running"] = True
    h.repository.authorize_superseded_removals.return_value = []

    await h.reconciler.execute(h.operation)

    h.repository.authorize_superseded_removals.assert_awaited_once_with(
        host="compute-nodeid", containers=[], operation_id=h.operation["id"]
    )
    h.sandbox_manager.discard_sandbox_deletion.assert_not_awaited()
    assert h.operation["state"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 409, 429, 503])
async def test_a_removal_that_does_not_answer_is_retried_as_the_same_request(monkeypatch, status):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(status, "unavailable"))

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == (
        "superseded_container_not_removed"
    )
    h.repository.commit_release.assert_not_awaited()
    h.repository.record_superseded_removal.assert_not_awaited()
    first = h.sandbox_manager.discard_sandbox_deletion.await_args.args[1]["request_id"]
    assert first == "superseded-recorded"

    h.operation.update(state="running", step="pending")
    h.sandbox_manager.list_sandboxes.side_effect = None
    h.sandbox_manager.list_sandboxes.return_value = [_left_behind_container()]
    await h.reconciler.execute(h.operation)
    assert h.sandbox_manager.discard_sandbox_deletion.await_args.args[1]["request_id"] == first


@pytest.mark.asyncio
async def test_a_removal_whose_answer_was_lost_is_confirmed_before_release(monkeypatch):
    """The Host removed it, but the answer never arrived: nothing lists it any more."""
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(0, "unavailable"))

    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"

    def receipt(_thread_id, payload):
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    h.operation.update(state="running", step="pending")
    h.sandbox_manager.list_sandboxes.side_effect = None
    h.sandbox_manager.list_sandboxes.return_value = []
    h.sandbox_manager.discard_sandbox_deletion.side_effect = receipt
    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["result"]["superseded_removed"] == 1
    assert h.repository.authorize_superseded_removals.await_args.kwargs["containers"] == []
    first, second = h.sandbox_manager.discard_sandbox_deletion.await_args_list
    assert first.args[1]["request_id"] == second.args[1]["request_id"] == "superseded-recorded"
    h.repository.record_superseded_removal.assert_awaited_once_with(
        request_id="superseded-recorded"
    )


@pytest.mark.asyncio
async def test_removals_confirmed_before_another_fails_still_count(monkeypatch):
    """A of two is confirmed, B gets no answer; once B goes too, both count."""
    h = harness(monkeypatch, kind="retire", identified=True)
    first = _left_behind_container()
    second = {**first, "sandbox_id": "asbx_other", "container_id": "container-other"}
    _left_behind(h)
    h.repository.authorize_superseded_removals.return_value = [
        {
            **{k: v for k, v in c.items() if k != "running"},
            "request_id": "superseded-" + c["sandbox_id"],
        }
        for c in (first, second)
    ]
    answers = iter([None, SandboxManagerError(503, "busy")])

    def removal(_thread_id, payload):
        answer = next(answers, None)
        if answer is not None:
            raise answer
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            "deleted": True,
        }

    h.sandbox_manager.list_sandboxes = AsyncMock(return_value=[first, second])
    h.sandbox_manager.discard_sandbox_deletion = AsyncMock(side_effect=removal)

    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["superseded_removed"] == 1

    h.operation.update(state="running", step="pending")
    h.repository.authorize_superseded_removals.return_value = [
        h.repository.authorize_superseded_removals.return_value[1]
    ]
    h.sandbox_manager.list_sandboxes = AsyncMock(side_effect=[[second], []])
    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "succeeded"
    assert h.operation["result"]["superseded_removed"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 410, 422])
async def test_a_host_refusing_the_removal_stops_the_release_for_attention(monkeypatch, status):
    h = harness(monkeypatch, kind="retire", identified=True)
    _left_behind(h, removal=SandboxManagerError(status, "refused"))

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        {"deleted": False},
        {"deleted": True, "container_id": "container-other"},
        {"deleted": True, "request_id": "another-request"},
    ],
)
async def test_an_unconfirmed_removal_does_not_count_as_one(monkeypatch, receipt):
    h = harness(monkeypatch, kind="retire", identified=True)

    def answer(_thread_id, payload):
        return {
            "request_id": payload["request_id"],
            "container_id": payload["expected_container_id"],
            **receipt,
        }

    _left_behind(h, removal=answer)

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    h.repository.record_superseded_removal.assert_not_awaited()
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_container_still_there_after_removal_still_blocks_the_release(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    container = _left_behind(h)
    h.sandbox_manager.list_sandboxes.side_effect = [[container], [container]]

    await h.reconciler.execute(h.operation)

    assert h.operation["state"] == "blocked"
    assert h.operation["result"]["blocked_reasons"][0]["code"] == "retained_groups_present"
    h.repository.commit_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_release_waits_if_provider_already_processing_it(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="retiring")
    h.operation["result"]["release_requested"] = True
    h.provider.inspect.return_value = observed(state="pending")
    await h.reconciler.execute(h.operation)
    h.provider.release.assert_not_awaited()
    assert h.operation["state"] == "waiting"


@pytest.mark.asyncio
async def test_committed_release_polls_terminating_without_manifest_until_terminal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(release_committed_at=NOW, desired_state="absent")
    h.operation["result"]["release_requested"] = True
    original_nodes = deepcopy(h.allocation["nodes"])
    # CloudLab loses a complete manifest while the experiment is terminating.
    h.provider.inspect.side_effect = [
        observed(state="pending").model_copy(update={"scope_complete": False, "nodes": ()}),
        observed(state="terminated").model_copy(update={"scope_complete": False, "nodes": ()}),
    ]
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "waiting"
    assert h.operation["result"]["external_pending"]
    assert h.allocation["nodes"] == original_nodes
    await h.reconciler.execute(h.operation)
    assert h.operation["state"] == "succeeded"
    assert not h.operation["result"]["external_pending"]
    h.provider.release.assert_not_awaited()
    h.repository.begin_retirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_retirement_waiting_does_not_stop_previously_enabled_renewal(monkeypatch):
    h = harness(monkeypatch, kind="retire", identified=True)
    h.allocation.update(
        desired_state="retiring",
        expires_at=NOW + timedelta(minutes=10),
        policy=RenewalPolicy(enabled=True, reason="Preserve retained sessions").model_dump(),
    )
    h.repository.list_operations.return_value = [
        {**h.operation, "state": "waiting", "step": "waiting_for_retention"}
    ]
    await h.reconciler.schedule()
    h.commands.extend.assert_awaited_once()
    args = h.commands.extend.await_args
    assert args.args[0] == h.allocation["id"]
    assert args.args[1].reason == "Preserve retained sessions"
    assert args.kwargs["actor"] == "compute-renewal"
