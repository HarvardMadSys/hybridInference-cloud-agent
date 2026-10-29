"""Host-release loading gates must preserve upgrade and rotation safety."""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.test_compute_reconcile import NOW, RELEASE, harness

from cloud_agent.application import compute_reconcile
from cloud_agent.ports.compute_resources import ComputeSchedulingSnapshot
from cloud_agent.schemas_compute import RenewalPolicy


@pytest.mark.asyncio
async def test_schedule_skips_host_release_load_without_allocations(monkeypatch):
    h = harness(monkeypatch)
    h.repository.scheduling_inventory = AsyncMock(
        return_value=ComputeSchedulingSnapshot([], {}, {})
    )
    loader = Mock(return_value=RELEASE)
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", loader)

    await h.reconciler.schedule()

    loader.assert_not_called()


@pytest.mark.asyncio
async def test_retiring_allocation_keeps_rotation_guard_for_active_upgrade(monkeypatch):
    h = harness(monkeypatch, identified=True)
    h.allocation.update(
        desired_state="absent",
        observed_state="ready",
        created_at=NOW - timedelta(days=8),
        expires_at=NOW + timedelta(minutes=30),
        policy=RenewalPolicy(
            enabled=True,
            reason="Keep the evaluation sandboxes",
            renew_before_seconds=3600,
            extend_to_horizon_seconds=8 * 3600,
            rotation_enabled=True,
            node_types=["r6615"],
        ).model_dump(),
    )
    upgrade = {
        "kind": "upgrade",
        "state": "running",
        "request_id": f"host-release:{h.allocation['id']}:{RELEASE.sha}",
        "parameters": {"allocation_id": h.allocation["id"], "deployment_sha": RELEASE.sha},
    }
    h.repository.list_operations.return_value = [upgrade]
    h.repository.create_operation = AsyncMock()
    loader = Mock(return_value=RELEASE)
    monkeypatch.setattr(compute_reconcile, "load_control_plane_host_release", loader)
    h.reconciler.rotation.start = AsyncMock(return_value=False)

    await h.reconciler.schedule()

    loader.assert_called_once()
    h.reconciler.rotation.start.assert_not_awaited()
    h.commands.extend.assert_awaited_once()
    h.repository.create_operation.assert_not_awaited()
