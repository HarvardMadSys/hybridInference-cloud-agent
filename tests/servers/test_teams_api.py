"""Team routes: session identity, stable refusals, one-time tokens and Gateway checks."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import identity
from cloud_agent.api import teams
from cloud_agent.api.deps import get_gateway_client, get_storage
from cloud_agent.domain.teams import (
    ApplicantRecord,
    InvitationRecord,
    InvitationUnavailable,
    MemberRecord,
    OwnJoinRequestRecord,
    TeamConflict,
    TeamForbidden,
    TeamNotFound,
    TeamRecord,
)
from cloud_agent.gateway import GatewayError
from cloud_agent.user_avatar import generate_user_avatar

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
TOKEN = "A" * 43
TEAM = TeamRecord(
    id="ateam_1",
    name="Platform",
    owner_user_id="alice",
    revision=3,
    member_count=2,
    created_at=NOW,
    role="owner",
)
INVITATION = InvitationRecord(
    id="atinv_1",
    created_by_user_id="alice",
    status="open",
    expires_at=NOW,
    created_at=NOW,
    claimed_by_user_id=None,
    request_state=None,
)
APPLICANT = ApplicantRecord(
    id="atjr_1",
    user_id="bob",
    first_name="",
    last_name="",
    state="pending",
    created_at=NOW,
    expires_at=NOW,
)
OWN_REQUEST = OwnJoinRequestRecord(
    id="atjr_1",
    team_id="ateam_1",
    team_name="Platform",
    inviter_user_id="alice",
    inviter_first_name="Alice",
    inviter_last_name="Liddell",
    state="pending",
    created_at=NOW,
    expires_at=NOW,
)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv(identity.ENV_ISSUER, "https://gateway.example")
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "teams-test-session-secret-long-enough")
    application = FastAPI()
    application.include_router(teams.router)
    repository = AsyncMock()
    repository.get_team.return_value = TEAM
    gateway = AsyncMock()
    admitted = {"alice", "bob", "carol"}
    gateway.agent_access.side_effect = lambda user_id: {
        "user_id": user_id,
        "allowed": user_id in admitted,
        "permissions": ["agent.use"] if user_id in admitted else [],
    }
    gateway.user_email.side_effect = lambda user_id: f"{user_id}@example.com"
    application.dependency_overrides[get_gateway_client] = lambda: gateway
    application.dependency_overrides[get_storage] = lambda: SimpleNamespace(teams=repository)
    application.state.repository = repository
    application.state.gateway = gateway
    application.state.admitted = admitted
    return application


def headers(user="alice"):
    return {"Authorization": f"Bearer {identity.mint_session(user_id=user)}"}


async def call(app, method, path, user="alice", **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://agent.example"
    ) as client:
        return await client.request(method, path, headers=headers(user) if user else {}, **kwargs)


async def test_every_route_needs_a_session(app):
    response = await call(app, "GET", "/v1/agent/teams", user=None)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.list_teams.assert_not_called()


async def test_the_actor_comes_from_the_session_not_the_request(app):
    app.state.repository.create_team.return_value = TEAM
    response = await call(
        app,
        "POST",
        "/v1/agent/teams?user_id=mallory",
        json={"name": "  Platform  ", "request_key": "create-1"},
    )
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["role"] == "owner"
    app.state.repository.create_team.assert_awaited_once_with(
        "alice", name="Platform", request_key="create-1"
    )


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (TeamNotFound("No such team."), 404, "not_found"),
        (TeamForbidden("Only the team owner can do this."), 403, "forbidden"),
        (TeamConflict("Changed.", code="revision_conflict"), 409, "revision_conflict"),
        (InvitationUnavailable("Gone."), 410, "invitation_unavailable"),
    ],
)
async def test_refusals_have_stable_types_and_are_never_cached(app, error, status, code):
    app.state.repository.get_team.side_effect = error
    response = await call(app, "GET", "/v1/agent/teams/ateam_1")
    assert response.status_code == status
    assert response.json() == {"error": {"type": code, "message": error.message}}
    assert response.headers["Cache-Control"] == "no-store"


async def test_malformed_requests_do_not_echo_their_input(app):
    secret = "x" * 42 + "!"
    response = await call(app, "POST", "/v1/agent/team-invitations/claim", json={"token": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["error"]["type"] == "invalid_request"
    for body in (
        {"name": "", "request_key": "k"},
        {"name": "a‮b", "request_key": "k"},
        {"name": "x" * 81, "request_key": "k"},
        {"name": "ok", "request_key": "k", "owner_user_id": "mallory"},
    ):
        assert (await call(app, "POST", "/v1/agent/teams", json=body)).status_code == 422
    revision = {"name": "ok", "expected_revision": True}
    assert (await call(app, "PATCH", "/v1/agent/teams/t", json=revision)).status_code == 422
    app.state.repository.create_team.assert_not_called()


async def test_an_invitation_token_is_returned_once_and_only_its_hash_is_stored(app):
    app.state.repository.create_invitation.return_value = (INVITATION, True)
    created = await call(
        app, "POST", "/v1/agent/teams/ateam_1/invitations", json={"request_key": "inv-1"}
    )
    assert created.status_code == 201
    token = created.json()["token"]
    assert len(token) == 43
    kwargs = app.state.repository.create_invitation.await_args.kwargs
    assert kwargs["token_hash"] == hashlib.sha256(token.encode()).digest()
    assert kwargs["request_key"] == "inv-1"

    app.state.repository.create_invitation.return_value = (INVITATION, False)
    replay = await call(
        app, "POST", "/v1/agent/teams/ateam_1/invitations", json={"request_key": "inv-1"}
    )
    assert replay.json()["token"] is None
    assert replay.json()["invitation"]["id"] == "atinv_1"


async def test_claiming_sends_the_hash_and_shows_only_team_name_and_inviter(app):
    app.state.repository.claim_invitation.return_value = OWN_REQUEST
    response = await call(
        app, "POST", "/v1/agent/team-invitations/claim", user="bob", json={"token": TOKEN}
    )
    assert response.status_code == 200
    app.state.repository.claim_invitation.assert_awaited_once_with(
        "bob", token_hash=hashlib.sha256(TOKEN.encode()).digest()
    )
    body = response.json()
    assert body["team_name"] == "Platform"
    assert body["inviter_display_name"] == "Alice Liddell"
    assert "owner_user_id" not in body and "email" not in body
    # The inviter had a name, so the Gateway was not asked for an email.
    app.state.gateway.user_email.assert_not_called()


async def test_approval_requires_the_applicants_current_admission(app):
    repository = app.state.repository
    repository.get_join_request.return_value = APPLICANT
    app.state.admitted.discard("bob")
    refused = await call(
        app,
        "POST",
        "/v1/agent/teams/ateam_1/join-requests/atjr_1/decision",
        json={"approve": True},
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "applicant_not_admitted"
    repository.decide_join_request.assert_not_called()

    app.state.gateway.agent_access.side_effect = GatewayError("down")
    unavailable = await call(
        app,
        "POST",
        "/v1/agent/teams/ateam_1/join-requests/atjr_1/decision",
        json={"approve": True},
    )
    assert unavailable.status_code == 503
    repository.decide_join_request.assert_not_called()


async def test_declining_asks_the_gateway_nothing_about_the_applicant(app):
    repository = app.state.repository
    repository.decide_join_request.return_value = ApplicantRecord(
        **{**APPLICANT.__dict__, "state": "declined"}
    )
    response = await call(
        app,
        "POST",
        "/v1/agent/teams/ateam_1/join-requests/atjr_1/decision",
        json={"approve": False},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "declined"
    assert response.json()["display_name"] == "bob@example.com"
    assert [c.args for c in app.state.gateway.agent_access.await_args_list] == [("alice",)]
    repository.get_join_request.assert_not_called()


async def test_transfer_asks_the_gateway_only_about_an_actual_member(app):
    repository = app.state.repository
    repository.list_members.return_value = [
        MemberRecord(user_id="alice", first_name="", last_name="", joined_at=NOW, role="owner"),
        MemberRecord(user_id="bob", first_name="", last_name="", joined_at=NOW, role="member"),
    ]
    outsider = await call(
        app,
        "POST",
        "/v1/agent/teams/ateam_1/transfer-ownership",
        json={"user_id": "carol", "expected_revision": 3},
    )
    assert outsider.status_code == 404
    assert [c.args for c in app.state.gateway.agent_access.await_args_list] == [("alice",)]

    repository.transfer_ownership.return_value = TeamRecord(
        **{**TEAM.__dict__, "owner_user_id": "bob", "role": "member", "revision": 4}
    )
    moved = await call(
        app,
        "POST",
        "/v1/agent/teams/ateam_1/transfer-ownership",
        json={"user_id": "bob", "expected_revision": 3},
    )
    assert moved.status_code == 200 and moved.json()["owner_user_id"] == "bob"
    repository.transfer_ownership.assert_awaited_once_with(
        "alice", "ateam_1", user_id="bob", expected_revision=3
    )


async def test_roster_shows_names_and_survives_an_email_lookup_failure(app):
    app.state.repository.list_members.return_value = [
        MemberRecord(
            user_id="alice", first_name="Alice", last_name="", joined_at=NOW, role="owner"
        ),
        MemberRecord(user_id="bob", first_name="", last_name="", joined_at=NOW, role="member"),
    ]

    async def email(user_id):
        if user_id == "bob":
            raise GatewayError("unavailable")
        return f"{user_id}@example.com"

    app.state.gateway.user_email.side_effect = email
    response = await call(app, "GET", "/v1/agent/teams/ateam_1/members")
    assert response.status_code == 200
    alice, bob = response.json()["members"]
    assert (alice["display_name"], alice["email"], alice["you"]) == (
        "Alice",
        "alice@example.com",
        True,
    )
    assert (bob["display_name"], bob["email"], bob["you"]) == ("", None, False)
    assert bob["avatar_data_url"] == generate_user_avatar("bob").data_url


@pytest.mark.parametrize("action", ["team_renamed", "member_role_changed"])
async def test_audit_pages_with_a_cursor(app, action):
    from cloud_agent.domain.teams import AuditRecord

    events = [
        AuditRecord(
            id=n,
            actor_user_id="alice",
            action=action,
            target_user_id=None,
            target_id=None,
            created_at=NOW,
        )
        for n in (9, 8, 7)
    ]
    app.state.repository.list_audit_events.return_value = events
    response = await call(app, "GET", "/v1/agent/teams/ateam_1/audit-events?limit=2")
    assert response.status_code == 200
    assert [e["id"] for e in response.json()["events"]] == [9, 8]
    assert all(e["action"] == action for e in response.json()["events"])
    assert response.json()["next_before"] == 8
    app.state.repository.list_audit_events.assert_awaited_once_with(
        "alice", "ateam_1", before=None, limit=3
    )


async def test_delete_takes_the_read_revision(app):
    response = await call(app, "DELETE", "/v1/agent/teams/ateam_1?expected_revision=3")
    assert response.status_code == 204
    app.state.repository.delete_team.assert_awaited_once_with(
        "alice", "ateam_1", expected_revision=3
    )
    assert (await call(app, "DELETE", "/v1/agent/teams/ateam_1")).status_code == 422


async def test_role_change_uses_session_actor_and_requires_revision(app):
    app.state.repository.set_member_role.return_value = TEAM
    path = "/v1/agent/teams/ateam_1/members/bob/role"
    response = await call(app, "PATCH", path, json={"role": "admin", "expected_revision": 3})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    app.state.repository.set_member_role.assert_awaited_once_with(
        "alice", "ateam_1", "bob", role="admin", expected_revision=3
    )
    for body in (
        {"role": "owner", "expected_revision": 3},
        {"role": "admin"},
        {"role": "admin", "expected_revision": True},
    ):
        assert (await call(app, "PATCH", path, json=body)).status_code == 422
    app.state.repository.set_member_role.side_effect = TeamForbidden("Owner only")
    denied = await call(app, "PATCH", path, json={"role": "member", "expected_revision": 3})
    assert denied.status_code == 403
    assert denied.json()["error"]["type"] == "forbidden"


async def test_admin_cannot_use_transfer_to_probe_a_members_gateway_access(app):
    app.state.repository.get_team.return_value = TeamRecord(**{**TEAM.__dict__, "role": "admin"})
    response = await call(
        app,
        "POST",
        "/v1/agent/teams/ateam_1/transfer-ownership",
        user="bob",
        json={"user_id": "carol", "expected_revision": 3},
    )
    assert response.status_code == 403
    app.state.repository.list_members.assert_not_called()
    app.state.repository.transfer_ownership.assert_not_called()
    assert all(
        call.kwargs.get("user_id") != "carol"
        for call in app.state.gateway.agent_access.call_args_list
    )
