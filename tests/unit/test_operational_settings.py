"""Operational consumers adopt admin changes without restarting their workers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent import capacity_history, entitlement, operational_health, settings
from cloud_agent.api import mcp_connections
from cloud_agent.storage.repositories import host_pool
from cloud_agent.workers import snapshots


def test_oauth_callbacks_and_repository_access_use_current_settings(monkeypatch):
    values = {
        "AGENT_PUBLIC_API_URL": "https://agents.example/agents/api",
        "AGENT_WEB_BASE_URL": "https://agents.example/agents",
        "AGENT_REPO_ALLOWLIST": "team/first",
    }
    for module in (mcp_connections, entitlement, settings):
        monkeypatch.setattr(module, "setting_env", lambda name: values.get(name))

    assert mcp_connections._public_base_url() == "https://agents.example/agents/api"
    assert entitlement.repo_is_allowed("team/first")
    values["AGENT_REPO_ALLOWLIST"] = "team/second"
    values["AGENT_WEB_BASE_URL"] = "https://new.example/work"
    assert not entitlement.repo_is_allowed("team/first")
    assert entitlement.repo_is_allowed("team/second")
    assert entitlement.repo_is_allowed("other/repo", {"AGENT_REPO_ALLOWLIST": "other/repo"})
    redirect = mcp_connections._callback_redirect("github", ok=True, message="")
    assert redirect.headers["location"].startswith("https://new.example/work/integrations?")


async def test_capacity_retention_uses_the_current_limit(monkeypatch):
    monkeypatch.setattr(capacity_history, "number_setting", lambda key: 7200.0)
    capacity = SimpleNamespace(prune_capacity_history=AsyncMock(return_value=0))
    await capacity_history._prune(capacity, limit=23, timeout_seconds=1)
    capacity.prune_capacity_history.assert_awaited_once_with(older_than_seconds=7200.0, limit=23)


async def test_background_workers_pick_up_interval_changes_between_passes(monkeypatch):
    interval = 5.0
    delays = []

    async def tick(delay):
        nonlocal interval
        delays.append(delay)
        interval = 12.0
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(snapshots, "number_setting", lambda key: interval)
    monkeypatch.setattr(snapshots, "asyncio", SimpleNamespace(sleep=tick))
    monkeypatch.setattr(snapshots, "_capture_due_shared_snapshot", AsyncMock(return_value=False))
    with pytest.raises(asyncio.CancelledError):
        await snapshots.coordinate_shared_snapshots(SimpleNamespace())
    assert delays == [5.0, 12.0]

    delays.clear()
    interval = 5.0
    monkeypatch.setattr(operational_health, "number_setting", lambda key: interval)
    monkeypatch.setattr(
        operational_health,
        "asyncio",
        SimpleNamespace(sleep=tick, timeout=asyncio.timeout),
    )
    queue = SimpleNamespace(
        queue_health=AsyncMock(return_value={"queued": 0, "unavailable": 0, "running": 0})
    )
    with pytest.raises(asyncio.CancelledError):
        await operational_health.monitor_queue(queue)
    assert delays == [5.0, 12.0]


async def test_queue_age_is_bound_as_a_runtime_value_in_health_query(monkeypatch):
    duration = 600.0
    monkeypatch.setattr(host_pool, "number_setting", lambda key: duration)

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    connection = SimpleNamespace(
        fetchrow=AsyncMock(
            side_effect=[
                None,
                {"queued": 2, "unavailable": 1, "running": 0},
                None,
                {"queued": 2, "unavailable": 1, "running": 0},
            ]
        ),
        transaction=Transaction,
    )

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *args):
            return None

    repository = host_pool.PostgresHostPoolRepository(SimpleNamespace(acquire=Acquire))
    assert await repository.queue_health(expected_deployment_sha="release") == {
        "queued": 2,
        "unavailable": 1,
        "running": 0,
    }
    assert connection.fetchrow.call_args.args[3] == 600.0
    assert "$3 * INTERVAL '1 second'" in connection.fetchrow.call_args.args[0]
    duration = 1200.0
    await repository.queue_health(expected_deployment_sha="release")
    assert connection.fetchrow.call_args.args[3] == 1200.0
