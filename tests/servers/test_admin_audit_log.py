"""The Audit log route: what it asks the trail for, and what it answers.

The SQL is pinned against Postgres in
``tests/integration/storage/test_audit_store.py``. This file pins the route's
side: the admin gate, how a filter becomes the actions it covers, that only
people are shown unless everything is asked for, how a row reads, and how one
more row than asked for becomes a cursor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_audit
from cloud_agent.api.admin_audit import audit_category
from cloud_agent.domain.audit import PEOPLE, actions_in

ADMIN = {"Authorization": "Bearer test-audit-admin"}


def _row(row_id: int, action: str = "agent.runner_host.drain", **over: Any) -> dict[str, Any]:
    return {
        "id": row_id,
        "action": action,
        "actor_id": "admin:u-1",
        "actor_type": "admin",
        "actor_label": "juncheng",
        "actor_deleted": False,
        "admin_ip": "203.0.113.9",
        "target_user_id": None,
        "subject_label": None,
        "subject_deleted": False,
        "target_type": "host",
        "target_id": "rtx6000b",
        "target_label": None,
        "details": {"host": "rtx6000b"},
        "success": True,
        "outcome": "applied",
        "reason": None,
        "correlation_id": None,
        "result": None,
        "result_at": None,
        "created_at": datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        **over,
    }


class FakeTrail:
    """Answers with canned rows, newest first, and remembers how it was asked."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.failures: tuple[int, datetime | None] = (0, None)

    async def list_admin_actions(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.rows[: kwargs["limit"]]

    async def write_failures_since(self, since: datetime) -> tuple[int, datetime | None]:
        return self.failures


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
async def test_people_are_shown_unless_everything_is_asked_for(audit_api) -> None:
    """Automation, the control plane's policy and self-service stay out by default."""
    client, trail, _ = audit_api

    await client.get("/admin/agent/audit-log", headers=ADMIN)
    await client.get("/admin/agent/audit-log?actors=all", headers=ADMIN)

    default, everything = trail.calls
    assert default["actor_types"] == PEOPLE == ("admin", "break_glass")
    assert everything["actor_types"] == ()
    assert default["actions"] == everything["actions"] == ()


@pytest.mark.asyncio
async def test_a_filter_asks_only_for_its_own_actions(audit_api) -> None:
    client, trail, _ = audit_api

    await client.get("/admin/agent/audit-log?category=servers", headers=ADMIN)
    await client.get("/admin/agent/audit-log?category=settings", headers=ADMIN)
    await client.get("/admin/agent/audit-log?category=accounts", headers=ADMIN)

    servers, settings, accounts = trail.calls
    assert servers["actions"] == actions_in("servers")
    assert "agent.runner_host.drain" in servers["actions"]
    # A server's own settings are a settings change, shown under one filter only.
    assert "agent.runner_host.set_settings" not in servers["actions"]
    assert "agent.runner_host.set_settings" in settings["actions"]
    assert "agent.compute.cloudlab_defaults.set" in settings["actions"]
    assert "agent.local_account.approve" in accounts["actions"]
    assert "agent.local_account.signup" in accounts["actions"]


@pytest.mark.asyncio
async def test_a_user_or_a_target_narrows_the_read(audit_api) -> None:
    client, trail, _ = audit_api

    await client.get("/admin/agent/audit-log?user=acct-7", headers=ADMIN)
    await client.get("/admin/agent/audit-log?target_type=sandbox&target_id=asbx_1", headers=ADMIN)

    by_user, by_target = trail.calls
    assert by_user["subject_user_id"] == "acct-7"
    assert by_user["target"] is None
    assert by_target["target"] == ("sandbox", "asbx_1")


@pytest.mark.asyncio
async def test_a_target_needs_both_halves(audit_api) -> None:
    client, trail, _ = audit_api
    response = await client.get("/admin/agent/audit-log?target_type=host", headers=ADMIN)
    assert response.status_code == 422
    assert trail.calls == []


@pytest.mark.asyncio
async def test_an_entry_names_who_acted_on_whom(audit_api) -> None:
    """The question the page exists for: which administrator did what to which user."""
    client, trail, _ = audit_api
    trail.rows = [
        _row(
            3,
            "agent.local_account.approve",
            target_user_id="acct-7",
            subject_label="alice@example.com",
            subject_deleted=True,
            target_type="account",
            target_id="acct-7",
            target_label="alice@example.com",
            reason="lab member",
            details={"from": {}, "to": {}},
        ),
    ]

    entry = (await client.get("/admin/agent/audit-log", headers=ADMIN)).json()["entries"][0]

    assert entry["category"] == "accounts"
    assert entry["kind"] == "change"
    assert entry["actor_type"] == "admin"
    assert entry["actor_account_id"] == "u-1"
    assert entry["actor_label"] == "juncheng"
    assert entry["target_user_id"] == "acct-7"
    assert entry["subject_label"] == "alice@example.com"
    assert entry["subject_deleted"] is True
    assert entry["reason"] == "lab member"


@pytest.mark.asyncio
async def test_a_requested_row_carries_its_final_result(audit_api) -> None:
    client, trail, _ = audit_api
    done = datetime(2026, 10, 9, 12, 2, tzinfo=UTC)
    trail.rows = [
        _row(
            2,
            "agent.sandbox.delete.requested",
            target_type="sandbox",
            target_id="asbx_1",
            outcome="requested",
            correlation_id="req-1",
            result="applied",
            result_at=done,
        ),
        _row(1, "agent.sandbox.delete.requested", outcome="requested", correlation_id="req-0"),
    ]

    entries = (await client.get("/admin/agent/audit-log", headers=ADMIN)).json()["entries"]

    assert entries[0]["outcome"] == "requested"
    assert entries[0]["result"] == "applied"
    assert entries[0]["result_at"] == "2026-10-09T12:02:00Z"
    assert entries[1]["result"] is None


@pytest.mark.asyncio
async def test_a_row_the_previous_release_wrote_reads_like_a_new_one(audit_api) -> None:
    """During the rollout a row can arrive without its attribution columns."""
    client, trail, _ = audit_api
    trail.rows = [
        _row(
            2,
            "agent.sandbox.delete.requested",
            target_user_id="acct-7",
            target_type=None,
            target_id=None,
            outcome=None,
            details={"sandbox_id": "asbx_9", "request_id": "req-9"},
        ),
        _row(
            1,
            "agent.something.new",
            details=None,
            success=False,
            target_type=None,
            target_id=None,
            outcome=None,
        ),
    ]

    entries = (await client.get("/admin/agent/audit-log", headers=ADMIN)).json()["entries"]

    assert (entries[0]["target_type"], entries[0]["target_id"]) == ("sandbox", "asbx_9")
    assert entries[0]["outcome"] == "requested"
    assert entries[1]["category"] is None
    assert entries[1]["kind"] is None
    assert entries[1]["target_type"] == "unknown"
    assert entries[1]["outcome"] == "failed"
    assert entries[1]["details"] is None


@pytest.mark.asyncio
async def test_recent_write_failures_are_reported(audit_api) -> None:
    client, trail, _ = audit_api
    latest = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    trail.failures = (3, latest)

    body = (await client.get("/admin/agent/audit-log", headers=ADMIN)).json()

    assert body["write_failures"] == {"count": 3, "latest_at": "2026-11-05T08:00:00Z"}


@pytest.mark.asyncio
async def test_a_bad_filter_or_page_size_is_refused(audit_api) -> None:
    client, trail, _ = audit_api
    for query in ("category=nope", "actors=robots", "limit=0", "limit=101", "before=0"):
        response = await client.get(f"/admin/agent/audit-log?{query}", headers=ADMIN)
        assert response.status_code == 422, query
    assert trail.calls == []


@pytest.mark.asyncio
async def test_sensitive_access_has_its_own_filter(audit_api) -> None:
    client, trail, _ = audit_api
    trail.rows = [_row(2, "agent.database.query"), _row(1, "agent.database.pgadmin.open")]

    response = await client.get("/admin/agent/audit-log?category=sensitive_access", headers=ADMIN)

    assert response.status_code == 200
    assert set(trail.calls[0]["actions"]) == {
        "agent.database.pgadmin.open",
        "agent.database.query",
        "agent.run.event_content.read",
    }
    entries = response.json()["entries"]
    assert [entry["kind"] for entry in entries] == ["access", "access"]


@pytest.mark.asyncio
async def test_without_a_database_the_route_says_so(audit_api) -> None:
    client, _, app = audit_api
    app.state.storage = None
    response = await client.get("/admin/agent/audit-log", headers=ADMIN)
    assert response.status_code == 503


def test_every_action_falls_under_one_filter() -> None:
    assert audit_category("agent.runner_host.set_settings") == "settings"
    assert audit_category("agent.sandbox_archive_settings.set") == "settings"
    assert audit_category("agent.runner_host.set_slots") == "servers"
    assert audit_category("agent.sandbox.stop") == "sandboxes"
    assert audit_category("agent.local_signup_domain.add") == "access_control"
    assert audit_category("agent.runner_host_extra.drain") is None
    assert audit_category("agent.run.event_content.read") == "sensitive_access"
