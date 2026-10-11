"""The Audit log route: what it asks the trail for, and what it answers.

The SQL is pinned against Postgres in
``tests/integration/storage/test_audit_store.py``. This file pins the route's
side: the admin gate, how a filter becomes the actions it covers, that polling
reads never come back, and how one more row than asked for becomes a cursor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_audit
from cloud_agent.api.admin_audit import POLLING_READS, audit_category

ADMIN = {"Authorization": "Bearer test-audit-admin"}


def _row(row_id: int, action: str = "agent.runner_host.drain", **over: Any) -> dict[str, Any]:
    return {
        "id": row_id,
        "action": action,
        "actor_id": "admin:u-1",
        "admin_ip": "203.0.113.9",
        "target_user_id": None,
        "details": {"host": "rtx6000b"},
        "success": True,
        "created_at": datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        **over,
    }


class FakeTrail:
    """Answers with canned rows, newest first, and remembers how it was asked."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    async def list_admin_actions(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.rows[: kwargs["limit"]]


@pytest.fixture
async def audit_api(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-audit-admin")
    trail = FakeTrail()
    app = FastAPI()
    app.state.storage = SimpleNamespace(audit_log=trail)
    app.include_router(admin_audit.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, trail, app


@pytest.mark.asyncio
async def test_only_an_admin_reads_the_trail(audit_api) -> None:
    client, trail, _ = audit_api
    response = await client.get("/admin/agent/audit-log")
    assert response.status_code in {401, 403}
    assert trail.calls == []


@pytest.mark.asyncio
async def test_a_page_is_newest_first_with_a_cursor_to_the_next(audit_api) -> None:
    client, trail, _ = audit_api
    trail.rows = [_row(5), _row(4), _row(3)]

    response = await client.get("/admin/agent/audit-log?limit=2", headers=ADMIN)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [entry["id"] for entry in body["entries"]] == [5, 4]
    assert body["next_before"] == 4
    # One more than the page, to learn whether an older page exists.
    assert trail.calls[0]["limit"] == 3
    assert trail.calls[0]["before_id"] is None


@pytest.mark.asyncio
async def test_the_last_page_has_no_cursor_and_before_is_passed_on(audit_api) -> None:
    client, trail, _ = audit_api
    trail.rows = [_row(2), _row(1)]

    body = (await client.get("/admin/agent/audit-log?before=3", headers=ADMIN)).json()

    assert [entry["id"] for entry in body["entries"]] == [2, 1]
    assert body["next_before"] is None
    assert trail.calls[0]["before_id"] == 3


@pytest.mark.asyncio
async def test_polling_reads_are_left_out_of_every_read(audit_api) -> None:
    client, trail, _ = audit_api

    await client.get("/admin/agent/audit-log", headers=ADMIN)
    await client.get("/admin/agent/audit-log?category=servers", headers=ADMIN)

    unfiltered, servers = trail.calls
    assert unfiltered["actions"] == ()
    assert set(POLLING_READS) <= set(unfiltered["excluded_actions"])
    assert set(POLLING_READS) <= set(servers["excluded_actions"])
    assert "agent.compute.servers.list" in POLLING_READS


@pytest.mark.asyncio
async def test_a_filter_asks_only_for_its_own_actions(audit_api) -> None:
    client, trail, _ = audit_api

    await client.get("/admin/agent/audit-log?category=servers", headers=ADMIN)
    await client.get("/admin/agent/audit-log?category=settings", headers=ADMIN)

    servers, settings = trail.calls
    assert servers["actions"] == ("agent.runner_host.", "agent.compute.", "agent.sandbox.")
    # A server's own settings live under agent.runner_host. but are a settings
    # change; the Servers filter must not show them twice.
    assert "agent.runner_host.set_settings" in servers["excluded_actions"]
    assert "agent.runner_host.set_settings" in settings["actions"]
    assert "agent.sandbox_archive_settings." in settings["actions"]
    assert "agent.runner_host.set_settings" not in settings["excluded_actions"]
    assert "agent.compute.cloudlab_defaults.set" in settings["actions"]


@pytest.mark.asyncio
async def test_each_entry_says_which_filter_it_falls_under(audit_api) -> None:
    client, trail, _ = audit_api
    trail.rows = [
        _row(8, "agent.sandbox_archive_settings.set"),
        _row(7, "agent.host_settings.set"),
        _row(6, "agent.runner_host.set_settings"),
        _row(5, "agent.runtime_version.set"),
        _row(4, "agent.compute.server.add"),
        _row(3, "agent.profiling.start"),
        _row(2, "agent.run.event_content.read", target_user_id="user-7"),
        _row(1, "agent.something.new", details=None, success=False),
    ]

    entries = (await client.get("/admin/agent/audit-log", headers=ADMIN)).json()["entries"]

    assert [entry["category"] for entry in entries] == [
        "settings",
        "settings",
        "settings",
        "settings",
        "servers",
        "measurements",
        "content",
        None,
    ]
    assert entries[6]["target_user_id"] == "user-7"
    assert entries[7]["details"] is None
    assert entries[7]["success"] is False


@pytest.mark.asyncio
async def test_a_bad_filter_or_page_size_is_refused(audit_api) -> None:
    client, trail, _ = audit_api
    for query in ("category=nope", "limit=0", "limit=101", "before=0"):
        response = await client.get(f"/admin/agent/audit-log?{query}", headers=ADMIN)
        assert response.status_code == 422, query
    assert trail.calls == []


@pytest.mark.asyncio
async def test_database_reads_have_a_dedicated_audit_filter(audit_api) -> None:
    client, trail, _ = audit_api
    trail.rows = [_row(2, "agent.database.query"), _row(1, "agent.database.pgadmin.open")]

    response = await client.get("/admin/agent/audit-log?category=database", headers=ADMIN)

    assert response.status_code == 200
    assert trail.calls[0]["actions"] == ("agent.database.",)
    assert [entry["category"] for entry in response.json()["entries"]] == ["database", "database"]


@pytest.mark.asyncio
async def test_without_a_database_the_route_says_so(audit_api) -> None:
    client, _, app = audit_api
    app.state.storage = None
    response = await client.get("/admin/agent/audit-log", headers=ADMIN)
    assert response.status_code == 503


def test_every_action_falls_under_at_most_one_filter() -> None:
    """The first filter that covers an action wins, so none is shown twice."""
    assert audit_category("agent.runner_host.set_settings") == "settings"
    assert audit_category("agent.sandbox_archive_settings.set") == "settings"
    assert audit_category("agent.runner_host.set_slots") == "servers"
    assert audit_category("agent.sandbox.stop") == "servers"
    assert audit_category("agent.runner_host_extra.drain") is None
    assert audit_category("agent.run.event_content.read") == "content"
