"""Workers read the saved settings on every pass and hand them to the store.

The repositories take these values as parameters, so the tests that pin the
SQL no longer show where they come from. These pin the other half: the
worker reads the named setting each time it calls the store, so a saved
change applies on the next pass.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_agent.context.context_timing import assemble_timing
from cloud_agent.deployment import operational_health
from cloud_agent.workers import recovery_maintenance


async def test_the_queue_monitor_reads_the_alert_age_on_every_pass(monkeypatch):
    saved: dict[str, float] = {"queue_alert_after_s": 300.0}
    passes = 0

    async def tick(_seconds: float) -> None:
        nonlocal passes
        passes += 1
        if passes == 2:
            saved["queue_alert_after_s"] = 45.0
        if passes > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(operational_health, "number_setting", lambda key: saved.get(key, 1.0))
    monkeypatch.setattr(
        operational_health, "asyncio", SimpleNamespace(sleep=tick, timeout=asyncio.timeout)
    )
    store = SimpleNamespace(
        queue_health=AsyncMock(return_value={"queued": 0, "unavailable": 0, "running": 0})
    )
    with pytest.raises(asyncio.CancelledError):
        await operational_health.monitor_queue(store)

    ages = [call.kwargs["queue_alert_after_s"] for call in store.queue_health.await_args_list]
    assert ages == [300.0, 45.0]


async def test_goal_scheduling_passes_the_saved_goal_settings_on_every_sweep(monkeypatch):
    saved: dict[str, float] = {
        "goals.arm_after_seconds": 120.0,
        "goals.stalled_after_seconds": 1800.0,
    }

    async def two_passes(_name: str, sweep: Any, **_kwargs: Any) -> None:
        await sweep()
        saved["goals.arm_after_seconds"] = 30.0
        await sweep()

    monkeypatch.setattr(recovery_maintenance, "number_setting", saved.__getitem__)
    monkeypatch.setattr(recovery_maintenance, "_run_periodic_loop", two_passes)
    goals = SimpleNamespace(
        sweep_goal_scheduling=AsyncMock(return_value={"rearmed": [], "stalled": []})
    )

    await recovery_maintenance.run_goal_scheduling_maintenance(SimpleNamespace(goals=goals))

    assert [call.kwargs for call in goals.sweep_goal_scheduling.await_args_list] == [
        {"arm_after_seconds": 120.0, "stalled_after_seconds": 1800.0},
        {"arm_after_seconds": 30.0, "stalled_after_seconds": 1800.0},
    ]


def test_the_retired_gateway_timing_fields_are_still_served_at_their_fixed_values():
    """COMPAT: a tab on the previous frontend reads these and must not fail."""
    timing = assemble_timing([]).model_dump()

    assert timing["gateway"] == "not_recorded"
    assert timing["gateway_reason"] is None
    assert timing["executions_split"] == 0
    assert timing["model_requests"] == 0
    assert timing["output_tps"] is None
    assert timing["requests"] == []
