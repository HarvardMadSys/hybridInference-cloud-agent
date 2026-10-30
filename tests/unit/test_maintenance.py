"""Maintenance mode: writes get a reason, reads and the agents' routes keep working."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import maintenance


@pytest.fixture(autouse=True)
def _clear():
    maintenance.apply(None)
    yield
    maintenance.apply(None)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(maintenance.MaintenanceGuard)
    maintenance.install_read_only_handler(app)

    @app.get("/v1/agent/jobs")
    async def read() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/agent/jobs")
    async def write() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/agent/deploy/report")
    async def report() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/session/logout")
    async def logout() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/touch")
    async def touches() -> dict[str, bool]:
        raise asyncpg.exceptions.ReadOnlySQLTransactionError("read-only")

    return app


async def _client_calls() -> dict[str, httpx.Response]:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return {
            "read": await client.get("/v1/agent/jobs"),
            "write": await client.post("/v1/agent/jobs"),
            "agent": await client.post("/v1/agent/deploy/report"),
            "logout": await client.post("/v1/session/logout"),
            "touch": await client.get("/v1/touch"),
        }


def _migrating() -> dict[str, object]:
    return {
        "state": "migrating",
        "kind": "database_migration",
        "workflow_id": "w" * 32,
        "detail": "moving",
        "started_at": datetime(2026, 10, 30, tzinfo=UTC),
    }


async def test_nothing_is_refused_outside_maintenance() -> None:
    calls = await _client_calls()
    assert calls["write"].status_code == 200 and calls["read"].status_code == 200
    assert maintenance.seen_workflow() is None
    # A database a previous process marked finished is not maintenance.
    maintenance.apply({**_migrating(), "state": "completed"})
    assert maintenance.current() == {"active": False}


async def test_during_maintenance_a_write_is_refused_and_says_why() -> None:
    maintenance.apply(_migrating())
    calls = await _client_calls()
    refused = calls["write"]
    assert refused.status_code == 503
    assert refused.headers["retry-after"] == str(maintenance.RETRY_AFTER_S)
    assert refused.headers["cache-control"] == "no-store"
    assert "database moves" in refused.json()["detail"]
    assert refused.json()["maintenance"]["since"].startswith("2026-10-30")
    assert "detail" not in refused.json()["maintenance"]
    assert calls["read"].status_code == 200
    assert calls["agent"].status_code == 200
    assert calls["logout"].status_code == 200
    assert maintenance.seen_workflow() == "w" * 32


async def test_a_read_that_writes_to_a_fenced_database_gets_the_same_answer() -> None:
    calls = await _client_calls()
    assert calls["touch"].status_code == 503
    assert "database moves" in calls["touch"].json()["detail"]


def test_progress_lasts_only_as_long_as_the_maintenance() -> None:
    maintenance.apply(_migrating())
    maintenance.record_progress("agent-1", [{"phase": "copy"}])
    assert maintenance.progress()["agent-1"]["operations"] == [{"phase": "copy"}]
    maintenance.apply(None)
    assert maintenance.progress() == {}


async def test_a_failed_read_keeps_the_last_view() -> None:
    maintenance.apply(_migrating())

    class Failing:
        async def current(self):
            raise OSError("database gone")

    import asyncio

    task = asyncio.create_task(maintenance.watch_forever(Failing(), interval_s=0.001))
    await asyncio.sleep(0.01)
    task.cancel()
    assert maintenance.current()["active"] is True
