"""The platform administrator's read-only view of teams, against the real schema.

Unlike every other team read, these are not fenced by membership: the route
in front of them requires an Agent administrator. They see deleted teams,
departed members and expired links, and never change anything.
"""

from __future__ import annotations

import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store
from tests.integration.storage.test_team_store import (
    expire_invitations,
    invite,
    join,
    new_team,
    token_hash,
)

from cloud_agent.domain.teams import TeamNotFound

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def name(pool, user_id: str, first: str, last: str = "") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_user_profiles (user_id, first_name, last_name) VALUES ($1, $2, $3)",
            user_id,
            first,
            last,
        )


async def test_the_list_shows_every_team_newest_first_with_its_counts(store, pool):
    await name(pool, "alice", "Alice", "Liddell")
    platform = await new_team(store, name="Platform", key="create-1")
    await join(store, platform.id, "bob")
    await join(store, platform.id, "carol")
    revision = (await store.teams.get_team("alice", platform.id)).revision
    await store.teams.set_member_role(
        "alice", platform.id, "bob", role="admin", expected_revision=revision
    )
    await invite(store, platform.id, label="waiting")
    await store.teams.claim_invitation("dave", token_hash=token_hash("waiting"))
    research = await new_team(store, owner="erin", name="Research", key="create-2")

    rows, more = await store.team_admin.list_teams_for_admin(
        query=None, state="active", limit=50, offset=0
    )

    assert more is False
    assert [row.id for row in rows] == [research.id, platform.id]
    first = rows[1]
    assert (first.owner_user_id, first.owner_first_name, first.owner_last_name) == (
        "alice",
        "Alice",
        "Liddell",
    )
    # Three active members; one admin besides the owner; dave is still waiting.
    assert (first.member_count, first.admin_count, first.pending_request_count) == (3, 1, 1)


async def test_the_list_filters_by_state_and_finds_a_name_an_id_or_a_member(store):
    kept = await new_team(store, name="Data_Platform 100%", key="create-1")
    await join(store, kept.id, "bob")
    gone = await new_team(store, name="Old team", key="create-2")
    await store.teams.delete_team("alice", gone.id, expected_revision=gone.revision)

    async def ids(**kwargs) -> list[str]:
        rows, _ = await store.team_admin.list_teams_for_admin(
            **{"query": None, "state": "active", "limit": 50, "offset": 0, **kwargs}
        )
        return [row.id for row in rows]

    assert await ids() == [kept.id]
    assert await ids(state="deleted") == [gone.id]
    assert set(await ids(state="all")) == {kept.id, gone.id}
    assert await ids(query="platform") == [kept.id]
    # LIKE's wildcards are literal: "_" and "%" match only themselves.
    assert await ids(query="a_P") == [kept.id]
    assert await ids(query="100%") == [kept.id]
    assert await ids(query="x_x") == []
    assert await ids(query=kept.id) == [kept.id]
    assert await ids(query="bob") == [kept.id]
    assert await ids(query="nobody") == []
    deleted = (
        await store.team_admin.list_teams_for_admin(query=None, state="deleted", limit=1, offset=0)
    )[0]
    assert deleted[0].state == "deleted" and deleted[0].deleted_at is not None


async def test_the_list_pages_by_offset(store):
    for number in range(3):
        await new_team(store, name=f"Team {number}", key=f"create-{number}")

    first, more = await store.team_admin.list_teams_for_admin(
        query=None, state="all", limit=2, offset=0
    )
    rest, done = await store.team_admin.list_teams_for_admin(
        query=None, state="all", limit=2, offset=2
    )

    assert more is True and done is False
    assert [row.name for row in [*first, *rest]] == ["Team 2", "Team 1", "Team 0"]


async def test_a_team_shows_departed_members_requests_and_link_counts(store, pool):
    await name(pool, "dave", "Dave")
    team = await new_team(store)
    await join(store, team.id, "bob")
    await join(store, team.id, "carol")
    await store.teams.leave_team("bob", team.id)
    await store.teams.remove_member("alice", team.id, "carol")
    await invite(store, team.id, label="waiting")
    request = await store.teams.claim_invitation("dave", token_hash=token_hash("waiting"))
    revoked = await invite(store, team.id, label="revoked")
    await store.teams.revoke_invitation("alice", team.id, revoked.id)
    await invite(store, team.id, label="stale")

    detail = await store.team_admin.get_team_for_admin(team.id)

    assert detail.team.member_count == 1
    assert [(m.user_id, m.role, m.state) for m in detail.members] == [
        ("alice", "owner", "active"),
        ("bob", "member", "left"),
        ("carol", "member", "removed"),
    ]
    assert detail.members[0].departed_at is None
    assert all(m.departed_at is not None for m in detail.members[1:])
    assert [(r.id, r.user_id, r.first_name) for r in detail.join_requests] == [
        (request.id, "dave", "Dave")
    ]
    # bob's and carol's links were claimed, dave's too; one revoked, one open.
    assert detail.invitations == {"open": 1, "expired": 0, "claimed": 3, "revoked": 1}

    await expire_invitations(pool, team.id)
    expired = await store.team_admin.get_team_for_admin(team.id)
    assert expired.join_requests == []
    assert expired.invitations["expired"] == 1 and expired.invitations["open"] == 0


async def test_a_deleted_team_stays_readable_and_an_unknown_one_is_not_found(store):
    team = await new_team(store)
    await store.teams.delete_team("alice", team.id, expected_revision=team.revision)

    detail = await store.team_admin.get_team_for_admin(team.id)

    assert detail.team.state == "deleted"
    # The owner's membership outlives the team, so the record says who it was.
    assert [(m.user_id, m.role) for m in detail.members] == [("alice", "owner")]
    with pytest.raises(TeamNotFound):
        await store.team_admin.get_team_for_admin("ateam_missing")
    with pytest.raises(TeamNotFound):
        await store.team_admin.list_team_audit_for_admin("ateam_missing", before=None, limit=10)


async def test_audit_events_page_backwards_with_the_names_profiles_give(store, pool):
    await name(pool, "alice", "Alice")
    await name(pool, "bob", "Bob", "Builder")
    team = await new_team(store)
    await join(store, team.id, "bob")
    await store.teams.remove_member("alice", team.id, "bob")

    newest = await store.team_admin.list_team_audit_for_admin(team.id, before=None, limit=2)
    older = await store.team_admin.list_team_audit_for_admin(
        team.id, before=newest[-1].id, limit=10
    )

    assert [e.action for e in [*newest, *older]] == [
        "member_removed",
        "join_approved",
        "join_requested",
        "invitation_created",
        "team_created",
    ]
    removed = newest[0]
    assert (removed.actor_user_id, removed.actor_first_name) == ("alice", "Alice")
    assert (removed.target_user_id, removed.target_first_name, removed.target_last_name) == (
        "bob",
        "Bob",
        "Builder",
    )
