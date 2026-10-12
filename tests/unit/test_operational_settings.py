"""Operational consumers adopt admin changes without restarting their workers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent import capacity_history, entitlement, ledger, operational_health
from cloud_agent.api import mcp_connections, service_jobs, session
from cloud_agent.application import residency
from cloud_agent.storage.repositories import host_pool
from cloud_agent.workers import snapshots


def test_oauth_callbacks_and_repository_access_use_current_settings(monkeypatch):
    values = {
        "AGENT_SESSION_REDIRECT_URI": "https://agents.example/api/v1/session/callback",
        "AGENT_WEB_BASE_URL": "https://agents.example/agents",
        "AGENT_REPO_ALLOWLIST": "team/first",
    }
    for module in (mcp_connections, entitlement, session):
        monkeypatch.setattr(module, "setting_env", lambda name: values.get(name))

    assert mcp_connections._public_base_url() == "https://agents.example/api"
    assert entitlement.repo_is_allowed("team/first")
    values["AGENT_REPO_ALLOWLIST"] = "team/second"
    values["AGENT_WEB_BASE_URL"] = "https://new.example/work"
    assert not entitlement.repo_is_allowed("team/first")
    assert entitlement.repo_is_allowed("team/second")
    assert entitlement.repo_is_allowed("other/repo", {"AGENT_REPO_ALLOWLIST": "other/repo"})
    assert session._env(session.ENV_WEB_BASE_URL) == "https://new.example/work"
    redirect = mcp_connections._callback_redirect("github", ok=True, message="")
    assert redirect.headers["location"].startswith("https://new.example/work/integrations?")


async def test_oncall_dispatch_rotation_applies_to_next_request(monkeypatch):
    values = {
        service_jobs.ENV_ONCALL_DISPATCH_TOKEN: "old-token",
        service_jobs.ENV_ONCALL_USER_ID: "service-user",
    }
    monkeypatch.setattr(service_jobs, "setting_env", lambda name: values.get(name))
    admin = AsyncMock(side_effect=RuntimeError("not an admin credential"))
    monkeypatch.setattr(service_jobs, "verify_admin_access", admin)
    assert await service_jobs.verify_oncall_dispatch(None, "Bearer old-token", None) == (
        "oncall-dispatch"
    )
    values[service_jobs.ENV_ONCALL_DISPATCH_TOKEN] = "new-token"
    with pytest.raises(RuntimeError, match="not an admin credential"):
        await service_jobs.verify_oncall_dispatch(None, "Bearer old-token", None)
    assert await service_jobs.verify_oncall_dispatch(None, "Bearer new-token", None) == (
        "oncall-dispatch"
    )
    assert service_jobs._oncall_owner()["user_id"] == "service-user"


async def test_residency_grants_use_updated_lease_duration(monkeypatch):
    duration = 2400.0
    monkeypatch.setattr(residency, "setting_env", lambda _: None)
    monkeypatch.setattr(residency, "number_setting", lambda key, fallback: duration)
    grants = SimpleNamespace(
        mint=AsyncMock(return_value={"grant_id": "grant-1", "token": "credential"}),
        revoke=AsyncMock(),
    )
    store = SimpleNamespace(link_residency_grant=AsyncMock(return_value=True))
    row = {
        "user_id": "owner",
        "thread_id": "thread-1",
        "establishing_attempt_id": 12,
        "model": "model-1",
        "generation": 2,
    }
    await residency._mint_residency_grant(store, grants, row=row)
    assert grants.mint.call_args.kwargs["ttl_seconds"] == 2400
    duration = 1200.0
    await residency._mint_residency_grant(store, grants, row=row)
    assert grants.mint.call_args.kwargs["ttl_seconds"] == 1200


async def test_usage_timeout_and_capacity_retention_use_current_limits(monkeypatch):
    monkeypatch.setattr(ledger, "number_setting", lambda key, fallback: 0.01)

    async def stalled_usage(*args, **kwargs):
        await asyncio.Event().wait()

    grants = SimpleNamespace(binding_id="binding", usage=stalled_usage)
    store = SimpleNamespace(complete_ledger_refresh=AsyncMock())
    await ledger.refresh_ledger_entry(
        store,
        grants,
        {
            "id": 1,
            "refresh_generation": 2,
            "terminal": True,
            "binding_id": "binding",
            "grant_id": "grant",
            "window_since": 1,
            "window_until": 2,
        },
    )
    assert store.complete_ledger_refresh.call_args.kwargs["error_code"] == "usage_unavailable"
    monkeypatch.setattr(capacity_history, "number_setting", lambda key, fallback: 7200.0)
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

    monkeypatch.setattr(snapshots, "number_setting", lambda key, fallback: interval)
    monkeypatch.setattr(snapshots, "asyncio", SimpleNamespace(sleep=tick))
    monkeypatch.setattr(snapshots, "_capture_due_shared_snapshot", AsyncMock(return_value=False))
    with pytest.raises(asyncio.CancelledError):
        await snapshots.coordinate_shared_snapshots(SimpleNamespace())
    assert delays == [5.0, 12.0]

    delays.clear()
    interval = 5.0
    monkeypatch.setattr(operational_health, "number_setting", lambda key, fallback: interval)
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
    monkeypatch.setattr(host_pool, "number_setting", lambda key, fallback: duration)

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
