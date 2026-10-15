"""Admin → Teams: every team, its people and its history, for administrators only."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_teams
from cloud_agent.api.deps import get_gateway_client
from cloud_agent.domain.teams import (
    AdminAuditRecord,
    AdminMemberRecord,
    AdminTeamDetail,
    AdminTeamRecord,
    ApplicantRecord,
    TeamNotFound,
)
from cloud_agent.gateway import GatewayError
from cloud_agent.user_avatar import generate_user_avatar

ADMIN = {"Authorization": "Bearer test-teams-admin"}
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def team(**over: Any) -> AdminTeamRecord:
    return AdminTeamRecord(
        **{
            "id": "ateam_1",
            "name": "Platform",
            "state": "active",
            "owner_user_id": "alice",
            "owner_first_name": "Alice",
            "owner_last_name": "",
            "member_count": 3,
            "admin_count": 1,
            "pending_request_count": 1,
            "created_at": NOW,
            "deleted_at": None,
            **over,
        }
    )


def audit(event_id: int, **over: Any) -> AdminAuditRecord:
    return AdminAuditRecord(
        **{
            "id": event_id,
            "actor_user_id": "alice",
            "actor_first_name": "Alice",
            "actor_last_name": "",
            "action": "member_removed",
            "target_user_id": "bob",
            "target_first_name": "",
            "target_last_name": "",
            "target_id": None,
            "created_at": NOW,
            **over,
        }
    )


class FakeTeams:
    """Canned answers that remember how they were asked."""

    def __init__(self) -> None:
        self.rows: list[AdminTeamRecord] = [team()]
        self.more = False
        self.detail: AdminTeamDetail | None = None
        self.events: list[AdminAuditRecord] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_teams_for_admin(self, **kwargs: Any) -> tuple[list[AdminTeamRecord], bool]:
        self.calls.append(("list", kwargs))
        return self.rows, self.more

    async def get_team_for_admin(self, team_id: str) -> AdminTeamDetail:
        self.calls.append(("get", {"team_id": team_id}))
        if self.detail is None:
            raise TeamNotFound("No such team.")
        return self.detail

    async def list_team_audit_for_admin(
        self, team_id: str, **kwargs: Any
    ) -> list[AdminAuditRecord]:
        self.calls.append(("audit", {"team_id": team_id, **kwargs}))
        if self.detail is None:
            raise TeamNotFound("No such team.")
        return self.events[: kwargs["limit"]]


class FakeGateway:
    def __init__(self) -> None:
        self.unreadable: set[str] = set()

    async def user_email(self, user_id: str) -> str:
        if user_id in self.unreadable:
            raise GatewayError("unavailable")
        return f"{user_id}@example.com"


@pytest.fixture
async def api(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-teams-admin")
    teams = FakeTeams()
    gateway = FakeGateway()
    app = FastAPI()
    app.state.storage = SimpleNamespace(team_admin=teams)
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    app.include_router(admin_teams.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, teams, gateway


async def test_only_an_admin_sees_teams(api) -> None:
    client, teams, _ = api
    for path in ("", "/ateam_1", "/ateam_1/audit-events"):
        response = await client.get(f"/admin/agent/teams{path}")
        assert response.status_code in {401, 403}
    assert teams.calls == []


async def test_the_list_names_owners_and_offers_the_next_page(api) -> None:
    client, teams, _ = api
    teams.more = True

    response = await client.get(
        "/admin/agent/teams?q=%20plat%20&state=all&limit=1&offset=4", headers=ADMIN
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert teams.calls == [("list", {"query": "plat", "state": "all", "limit": 1, "offset": 4})]
    body = response.json()
    assert body["next_offset"] == 5
    (summary,) = body["teams"]
    assert summary["owner"] == {
        "user_id": "alice",
        "display_name": "Alice",
        "email": "alice@example.com",
        "avatar_data_url": generate_user_avatar("alice").data_url,
    }
    assert (summary["member_count"], summary["admin_count"], summary["pending_request_count"]) == (
        3,
        1,
        1,
    )


async def test_a_blank_search_lists_everything_and_a_bad_state_is_refused(api) -> None:
    client, teams, _ = api
    assert (await client.get("/admin/agent/teams?q=%20%20", headers=ADMIN)).status_code == 200
    assert teams.calls[-1][1]["query"] is None
    assert teams.calls[-1][1]["state"] == "active"
    assert (await client.get("/admin/agent/teams?state=gone", headers=ADMIN)).status_code == 422
    assert (await client.get("/admin/agent/teams?limit=101", headers=ADMIN)).status_code == 422


async def test_a_team_lists_everyone_and_survives_an_unreadable_email(api) -> None:
    client, teams, gateway = api
    gateway.unreadable = {"carol"}
    teams.detail = AdminTeamDetail(
        team=team(),
        members=[
            AdminMemberRecord("alice", "Alice", "", "owner", "active", NOW, None),
            AdminMemberRecord("bob", "", "", "admin", "active", NOW, None),
            AdminMemberRecord("carol", "", "", "member", "removed", NOW, NOW),
        ],
        join_requests=[ApplicantRecord("atjr_1", "dave", "Dave", "", "pending", NOW, NOW)],
        invitations={"open": 1, "expired": 2, "claimed": 3, "revoked": 0},
    )

    response = await client.get("/admin/agent/teams/ateam_1", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert [
        (m["user_id"], m["display_name"], m["email"], m["role"], m["state"])
        for m in body["members"]
    ] == [
        ("alice", "Alice", "alice@example.com", "owner", "active"),
        ("bob", "bob@example.com", "bob@example.com", "admin", "active"),
        ("carol", "", None, "member", "removed"),
    ]
    assert body["members"][2]["departed_at"] is not None
    assert [(r["id"], r["display_name"]) for r in body["join_requests"]] == [("atjr_1", "Dave")]
    assert body["invitations"] == {"open": 1, "expired": 2, "claimed": 3, "revoked": 0}


async def test_an_unknown_team_is_not_found(api) -> None:
    client, _, _ = api
    for path in ("/ateam_missing", "/ateam_missing/audit-events"):
        response = await client.get(f"/admin/agent/teams{path}", headers=ADMIN)
        assert response.status_code == 404
        assert response.json()["detail"]["error"]["type"] == "not_found"


async def test_audit_events_name_people_and_page_backwards(api) -> None:
    client, teams, _ = api
    teams.detail = AdminTeamDetail(team=team(), members=[], join_requests=[], invitations={})
    teams.events = [audit(9), audit(8, action="team_created", target_user_id=None), audit(7)]

    response = await client.get("/admin/agent/teams/ateam_1/audit-events?limit=2", headers=ADMIN)

    assert response.status_code == 200
    assert teams.calls[-1] == ("audit", {"team_id": "ateam_1", "before": None, "limit": 3})
    body = response.json()
    assert body["next_before"] == 8
    first, second = body["events"]
    assert (first["action"], first["actor"]["display_name"]) == ("member_removed", "Alice")
    assert first["target_user"]["display_name"] == "bob@example.com"
    assert second["target_user"] is None
