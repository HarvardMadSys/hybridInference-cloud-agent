"""Notification retention runs outside the owner read path."""

from __future__ import annotations

import pytest

from cloud_agent.workers import notification_maintenance


class _Maintenance:
    def __init__(self, results: list[int | Exception]) -> None:
        self.results = iter(results)
        self.limits: list[int] = []

    async def prune_notification_changes(self, *, limit: int = 500) -> int:
        self.limits.append(limit)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_failure_is_contained_and_the_next_pass_still_runs(monkeypatch, caplog):
    maintenance = _Maintenance([RuntimeError("database unavailable"), 0])

    async def run_twice(_interval, *passes):
        await passes[0]()
        await passes[0]()

    monkeypatch.setattr(notification_maintenance, "run_every", run_twice)

    await notification_maintenance.run_notification_maintenance(maintenance)

    assert maintenance.limits == [500, 500]
    assert "Notification marker maintenance sweep failed" in caplog.text


@pytest.mark.asyncio
async def test_each_pass_drains_full_batches(monkeypatch):
    maintenance = _Maintenance([500, 500, 123])

    async def run_once(_interval, *passes):
        await passes[0]()

    monkeypatch.setattr(notification_maintenance, "run_every", run_once)

    await notification_maintenance.run_notification_maintenance(maintenance)

    assert maintenance.limits == [500, 500, 500]
