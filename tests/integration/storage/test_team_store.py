"""Team membership, invitations and join requests against the real schema.

These pin the store's half of the Team contract: one owner who is always an
active member, single-use invitations that only their claimant can use, one
pending request per account and team, access generations that only move
forward, and every refusal decided inside the transaction that would commit.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import asyncpg
import pytest
from tests.integration.storage.test_agent_job_store import pool as pool, store as store
from tests.integration.storage.test_schema_upgrade import _db_config
from tests.support.migrations import apply_migrations, downgrade_to

from cloud_agent.domain.teams import (
    MAX_OPEN_INVITATIONS,
    InvitationUnavailable,
    TeamConflict,
    TeamForbidden,
    TeamNotFound,
)

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

DAY = timedelta(hours=24)


def token_hash(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


async def new_team(store, owner="alice", name="Platform", key="create-1"):
    return await store.teams.create_team(owner, name=name, request_key=key)


async def invite(store, team_id, owner="alice", label="link-1"):
    invitation, created = await store.teams.create_invitation(
        owner, team_id, request_key=label, token_hash=token_hash(label), ttl=DAY
    )
    assert created
    return invitation


async def join(store, team_id, user, *, owner="alice", label=None):
    label = label or f"link-{user}"
    await invite(store, team_id, owner, label)
    request = await store.teams.claim_invitation(user, token_hash=token_hash(label))
    return await store.teams.decide_join_request(owner, team_id, request.id, approve=True)


async def generation(pool, team_id, user):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT state, access_generation FROM agent_team_members "
            "WHERE team_id = $1 AND user_id = $2",
            team_id,
            user,
        )


async def expire_invitations(pool, team_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_team_invitations SET created_at = NOW() - INTERVAL '2 days', "
            "expires_at = NOW() - INTERVAL '1 day' WHERE team_id = $1",
            team_id,
        )


async def test_creating_a_team_makes_the_creator_its_only_owner_and_is_idempotent(store, pool):
    team = await new_team(store)
    again = await new_team(store, name="Different name, same request")
    assert again == team
    assert team.role == "owner" and team.owner_user_id == "alice" and team.member_count == 1
    assert [t.id for t in await store.teams.list_teams("alice")] == [team.id]
    assert await store.teams.list_teams("bob") == []
    with pytest.raises(TeamNotFound):
        await store.teams.get_team("bob", team.id)
    members = await store.teams.list_members("alice", team.id)
    assert [(m.user_id, m.role) for m in members] == [("alice", "owner")]
    async with pool.acquire() as conn:
        actions = await conn.fetch("SELECT action FROM agent_team_audit_events")
    assert [row["action"] for row in actions] == ["team_created"]


async def test_the_database_refuses_a_team_whose_owner_is_not_an_active_member(store, pool):
    team = await new_team(store)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE agent_team_members SET state = 'left', departed_at = NOW() "
                    "WHERE team_id = $1",
                    team.id,
                )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE agent_teams SET owner_user_id = 'mallory' WHERE id = $1", team.id
                )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE agent_teams SET name = ' padded ' WHERE id = $1", team.id)


async def test_owned_team_limit_counts_only_live_teams(store, monkeypatch):
    from cloud_agent.storage.repositories import teams as repository

    monkeypatch.setattr(repository, "MAX_OWNED_TEAMS", 2)
    first = await new_team(store, key="a")
    await new_team(store, key="b")
    with pytest.raises(TeamConflict) as refused:
        await new_team(store, key="c")
    assert refused.value.code == "team_limit_reached"
    await store.teams.delete_team("alice", first.id, expected_revision=first.revision)
    assert (await new_team(store, key="c")).role == "owner"


async def test_an_invitation_becomes_one_accounts_request_and_approval_makes_a_member(store, pool):
    team = await new_team(store)
    invitation = await invite(store, team.id)
    assert invitation.status == "open" and invitation.claimed_by_user_id is None
    replay, created = await store.teams.create_invitation(
        "alice", team.id, request_key="link-1", token_hash=token_hash("other"), ttl=DAY
    )
    assert not created and replay.id == invitation.id

    request = await store.teams.claim_invitation("bob", token_hash=token_hash("link-1"))
    assert (request.team_name, request.inviter_user_id, request.state) == (
        "Platform",
        "alice",
        "pending",
    )
    assert await store.teams.claim_invitation("bob", token_hash=token_hash("link-1")) == request
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("carol", token_hash=token_hash("link-1"))
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("carol", token_hash=token_hash("never-issued"))
    # A pending applicant is not a member yet.
    with pytest.raises(TeamNotFound):
        await store.teams.get_team("bob", team.id)

    (pending,) = await store.teams.list_join_requests("alice", team.id)
    assert (pending.id, pending.user_id, pending.state) == (request.id, "bob", "pending")
    with pytest.raises(TeamNotFound):
        await store.teams.list_join_requests("bob", team.id)

    approved = await store.teams.decide_join_request("alice", team.id, request.id, approve=True)
    assert approved.state == "approved"
    again = await store.teams.decide_join_request("alice", team.id, request.id, approve=True)
    assert again == approved
    with pytest.raises(TeamConflict) as closed:
        await store.teams.decide_join_request("alice", team.id, request.id, approve=False)
    assert closed.value.code == "request_closed"

    bob_view = await store.teams.get_team("bob", team.id)
    assert bob_view.role == "member" and bob_view.member_count == 2
    assert [m.user_id for m in await store.teams.list_members("bob", team.id)] == ["alice", "bob"]
    (listed,) = await store.teams.list_invitations("alice", team.id)
    assert (listed.status, listed.claimed_by_user_id, listed.request_state) == (
        "claimed",
        "bob",
        "approved",
    )
    (own,) = await store.teams.list_own_join_requests("bob")
    assert own.state == "approved"
    with pytest.raises(TeamConflict) as member:
        await invite(store, team.id, label="link-2")
        await store.teams.claim_invitation("bob", token_hash=token_hash("link-2"))
    assert member.value.code == "already_member"


async def test_only_the_owner_manages_invitations_requests_and_members(store):
    team = await new_team(store)
    await join(store, team.id, "bob")
    request_label = "for-carol"
    await invite(store, team.id, label=request_label)
    request = await store.teams.claim_invitation("carol", token_hash=token_hash(request_label))
    refused = [
        store.teams.create_invitation(
            "bob", team.id, request_key="b", token_hash=token_hash("b"), ttl=DAY
        ),
        store.teams.list_invitations("bob", team.id),
        store.teams.decide_join_request("bob", team.id, request.id, approve=True),
        store.teams.remove_member("bob", team.id, "alice"),
        store.teams.rename_team("bob", team.id, name="Mine", expected_revision=1),
        store.teams.transfer_ownership("bob", team.id, user_id="bob", expected_revision=1),
        store.teams.delete_team("bob", team.id, expected_revision=1),
        store.teams.list_audit_events("bob", team.id, before=None, limit=10),
    ]
    for call in refused:
        with pytest.raises(TeamForbidden):
            await call
    # An outsider cannot tell the team exists.
    with pytest.raises(TeamNotFound):
        await store.teams.list_invitations("carol", team.id)
    with pytest.raises(TeamNotFound):
        await store.teams.decide_join_request("alice", "ateam_missing", request.id, approve=True)


async def test_dead_links_cannot_be_claimed_and_expired_requests_cannot_be_approved(store, pool):
    team = await new_team(store)
    await invite(store, team.id, label="revoked")
    revoked = (await store.teams.list_invitations("alice", team.id))[0]
    await store.teams.revoke_invitation("alice", team.id, revoked.id)
    await store.teams.revoke_invitation("alice", team.id, revoked.id)
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("bob", token_hash=token_hash("revoked"))

    await invite(store, team.id, label="claimed")
    request = await store.teams.claim_invitation("bob", token_hash=token_hash("claimed"))
    await invite(store, team.id, label="unclaimed")
    await expire_invitations(pool, team.id)
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("carol", token_hash=token_hash("unclaimed"))
    with pytest.raises(TeamConflict) as expired:
        await store.teams.decide_join_request("alice", team.id, request.id, approve=True)
    assert expired.value.code == "request_expired"
    assert await store.teams.list_join_requests("alice", team.id) == []
    statuses = {
        i.id: (i.status, i.request_state)
        for i in await store.teams.list_invitations("alice", team.id)
    }
    assert sorted(statuses.values()) == [
        ("claimed", "expired"),
        ("expired", None),
        ("revoked", None),
    ]

    # The expired request no longer holds Bob's one pending slot in this team.
    await invite(store, team.id, label="fresh")
    fresh = await store.teams.claim_invitation("bob", token_hash=token_hash("fresh"))
    assert fresh.state == "pending" and fresh.id != request.id
    assert {r.state for r in await store.teams.list_own_join_requests("bob")} == {
        "pending",
        "expired",
    }


async def test_an_account_has_one_pending_request_per_team(store):
    team = await new_team(store)
    await invite(store, team.id, label="one")
    await invite(store, team.id, label="two")
    await store.teams.claim_invitation("bob", token_hash=token_hash("one"))
    with pytest.raises(TeamConflict) as pending:
        await store.teams.claim_invitation("bob", token_hash=token_hash("two"))
    assert pending.value.code == "request_pending"
    # The second link was not used up by the refusal.
    other = await store.teams.claim_invitation("carol", token_hash=token_hash("two"))
    assert other.state == "pending"


@pytest.mark.parametrize("departure", ["leave", "remove"])
async def test_a_former_members_old_link_says_the_membership_ended(store, departure):
    team = await new_team(store)
    approved = await join(store, team.id, "bob")
    # Claiming again while still a member is a reload: the same approved request.
    again = await store.teams.claim_invitation("bob", token_hash=token_hash("link-bob"))
    assert (again.id, again.state) == (approved.id, "approved")
    # A decided request that made no member is returned as it stands.
    await invite(store, team.id, label="declined")
    request = await store.teams.claim_invitation("carol", token_hash=token_hash("declined"))
    await store.teams.decide_join_request("alice", team.id, request.id, approve=False)
    declined = await store.teams.claim_invitation("carol", token_hash=token_hash("declined"))
    assert (declined.id, declined.state) == (request.id, "declined")

    if departure == "leave":
        await store.teams.leave_team("bob", team.id)
    else:
        await store.teams.remove_member("alice", team.id, "bob")
    with pytest.raises(TeamConflict) as ended:
        await store.teams.claim_invitation("bob", token_hash=token_hash("link-bob"))
    assert ended.value.code == "no_longer_member"
    assert ended.value.message == (
        "You are no longer a member of this team. Ask an owner or admin for a new invitation link."
    )
    with pytest.raises(TeamNotFound):
        await store.teams.get_team("bob", team.id)
    # The spent link is still Bob's alone.
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("carol", token_hash=token_hash("link-bob"))


async def test_concurrent_claims_of_one_link_leave_exactly_one_request(store):
    team = await new_team(store)
    await invite(store, team.id, label="race")
    results = await asyncio.gather(
        *(
            store.teams.claim_invitation(user, token_hash=token_hash("race"))
            for user in ("bob", "carol", "dave", "erin")
        ),
        return_exceptions=True,
    )
    claimed = [r for r in results if not isinstance(r, Exception)]
    assert len(claimed) == 1
    assert all(isinstance(r, InvitationUnavailable) for r in results if isinstance(r, Exception))
    assert len(await store.teams.list_join_requests("alice", team.id)) == 1


async def test_revoking_a_claimed_link_cancels_its_pending_request_but_not_a_decided_one(store):
    team = await new_team(store)
    await invite(store, team.id, label="pending")
    request = await store.teams.claim_invitation("bob", token_hash=token_hash("pending"))
    (invitation,) = await store.teams.list_invitations("alice", team.id)
    await store.teams.revoke_invitation("alice", team.id, invitation.id)
    (own,) = await store.teams.list_own_join_requests("bob")
    assert own.state == "cancelled"
    with pytest.raises(TeamConflict):
        await store.teams.decide_join_request("alice", team.id, request.id, approve=True)

    await join(store, team.id, "carol")
    used = next(
        i
        for i in await store.teams.list_invitations("alice", team.id)
        if i.claimed_by_user_id == "carol"
    )
    with pytest.raises(TeamConflict) as closed:
        await store.teams.revoke_invitation("alice", team.id, used.id)
    assert closed.value.code == "invitation_closed"


async def test_departures_advance_the_access_generation_and_a_return_advances_it_again(store, pool):
    team = await new_team(store)
    await join(store, team.id, "bob")
    assert tuple(await generation(pool, team.id, "bob")) == ("active", 1)

    with pytest.raises(TeamConflict) as owner:
        await store.teams.leave_team("alice", team.id)
    assert owner.value.code == "owner_cannot_leave"
    with pytest.raises(TeamConflict):
        await store.teams.remove_member("alice", team.id, "alice")

    await store.teams.leave_team("bob", team.id)
    assert tuple(await generation(pool, team.id, "bob")) == ("left", 2)
    with pytest.raises(TeamNotFound):
        await store.teams.get_team("bob", team.id)
    with pytest.raises(TeamNotFound):
        await store.teams.leave_team("bob", team.id)

    await join(store, team.id, "bob", label="return")
    assert tuple(await generation(pool, team.id, "bob")) == ("active", 3)
    await store.teams.remove_member("alice", team.id, "bob")
    assert tuple(await generation(pool, team.id, "bob")) == ("removed", 4)
    with pytest.raises(TeamNotFound):
        await store.teams.remove_member("alice", team.id, "bob")


async def test_member_limit_is_enforced_at_approval(store, monkeypatch):
    from cloud_agent.storage.repositories import teams as repository

    monkeypatch.setattr(repository, "MAX_TEAM_MEMBERS", 2)
    team = await new_team(store)
    await join(store, team.id, "bob")
    await invite(store, team.id, label="third")
    request = await store.teams.claim_invitation("carol", token_hash=token_hash("third"))
    with pytest.raises(TeamConflict) as full:
        await store.teams.decide_join_request("alice", team.id, request.id, approve=True)
    assert full.value.code == "team_full"


async def test_open_invitation_limit_counts_only_usable_links(store, pool, monkeypatch):
    from cloud_agent.storage.repositories import teams as repository

    monkeypatch.setattr(repository, "MAX_OPEN_INVITATIONS", 2)
    assert MAX_OPEN_INVITATIONS > 2
    team = await new_team(store)
    await invite(store, team.id, label="a")
    await invite(store, team.id, label="b")
    with pytest.raises(TeamConflict) as limit:
        await invite(store, team.id, label="c")
    assert limit.value.code == "invitation_limit_reached"
    await expire_invitations(pool, team.id)
    await invite(store, team.id, label="c")


async def test_transfer_hands_over_the_team_and_closes_the_old_owners_links(store):
    team = await new_team(store)
    await join(store, team.id, "bob")
    await invite(store, team.id, label="open")
    await invite(store, team.id, label="claimed")
    await store.teams.claim_invitation("carol", token_hash=token_hash("claimed"))

    with pytest.raises(TeamConflict) as stale:
        await store.teams.transfer_ownership("alice", team.id, user_id="bob", expected_revision=99)
    assert stale.value.code == "revision_conflict"
    with pytest.raises(TeamNotFound):
        await store.teams.transfer_ownership(
            "alice", team.id, user_id="carol", expected_revision=team.revision
        )
    with pytest.raises(TeamConflict):
        await store.teams.transfer_ownership(
            "alice", team.id, user_id="alice", expected_revision=team.revision
        )

    moved = await store.teams.transfer_ownership(
        "alice", team.id, user_id="bob", expected_revision=team.revision
    )
    assert (moved.owner_user_id, moved.role, moved.revision) == ("bob", "member", team.revision + 1)
    invitations = await store.teams.list_invitations("bob", team.id)
    assert sorted((i.status, i.request_state or "") for i in invitations) == [
        ("claimed", "approved"),  # Bob's own, already used
        ("revoked", ""),
        ("revoked", "cancelled"),
    ]
    assert (await store.teams.list_own_join_requests("carol"))[0].state == "cancelled"
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("dave", token_hash=token_hash("open"))
    await store.teams.leave_team("alice", team.id)
    assert [m.user_id for m in await store.teams.list_members("bob", team.id)] == ["bob"]


async def test_concurrent_transfer_and_departure_never_leave_a_team_without_an_owner(store, pool):
    for round_ in range(5):
        team = await new_team(store, key=f"race-{round_}")
        await join(store, team.id, "bob", label=f"race-{round_}")
        results = await asyncio.gather(
            store.teams.transfer_ownership(
                "alice", team.id, user_id="bob", expected_revision=team.revision
            ),
            store.teams.leave_team("bob", team.id),
            return_exceptions=True,
        )
        assert sum(isinstance(r, Exception) for r in results) == 1
        async with pool.acquire() as conn:
            owner_active = await conn.fetchval(
                "SELECT m.state FROM agent_teams AS t JOIN agent_team_members AS m "
                "ON m.team_id = t.id AND m.user_id = t.owner_user_id WHERE t.id = $1",
                team.id,
            )
        assert owner_active == "active"


async def test_rename_and_delete_follow_the_read_revision(store):
    team = await new_team(store)
    await join(store, team.id, "bob")
    await invite(store, team.id, label="left-open")
    renamed = await store.teams.rename_team(
        "alice", team.id, name="Infra", expected_revision=team.revision
    )
    assert (renamed.name, renamed.revision) == ("Infra", team.revision + 1)
    unchanged = await store.teams.rename_team(
        "alice", team.id, name="Infra", expected_revision=renamed.revision
    )
    assert unchanged.revision == renamed.revision
    with pytest.raises(TeamConflict):
        await store.teams.delete_team("alice", team.id, expected_revision=team.revision)

    await store.teams.delete_team("alice", team.id, expected_revision=renamed.revision)
    for user in ("alice", "bob"):
        assert await store.teams.list_teams(user) == []
        with pytest.raises(TeamNotFound):
            await store.teams.get_team(user, team.id)
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("carol", token_hash=token_hash("left-open"))


async def test_audit_records_who_did_what_and_pages_backwards(store, pool):
    team = await new_team(store)
    await join(store, team.id, "bob")
    await store.teams.remove_member("alice", team.id, "bob")
    page = await store.teams.list_audit_events("alice", team.id, before=None, limit=3)
    assert [(e.action, e.actor_user_id, e.target_user_id) for e in page] == [
        ("member_removed", "alice", "bob"),
        ("join_approved", "alice", "bob"),
        ("join_requested", "bob", None),
    ]
    rest = await store.teams.list_audit_events("alice", team.id, before=page[-1].id, limit=10)
    assert [e.action for e in rest] == ["invitation_created", "team_created"]
    async with pool.acquire() as conn:
        details = await conn.fetch("SELECT details::text AS details FROM agent_team_audit_events")
    hashed = token_hash("link-bob").hex()
    assert all("link-bob" not in row["details"] and hashed not in row["details"] for row in details)


async def test_team_migration_round_trips(store, pool):
    config = _db_config()
    try:
        for _ in range(2):
            downgrade_to(config, "0078_environment_snapshots")
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT to_regclass('agent_teams')") is None
            apply_migrations(config, "0079_teams")
            team = await new_team(store)
            assert team.member_count == 1
    finally:
        apply_migrations(config)


async def make_admin(store, team_id, user):
    current = await store.teams.get_team("alice", team_id)
    return await store.teams.set_member_role(
        "alice", team_id, user, role="admin", expected_revision=current.revision
    )


async def test_admin_manages_daily_work_but_cannot_manage_roles_or_ownership(store):
    team = await new_team(store)
    for user in ("bob", "carol", "dave"):
        await join(store, team.id, user)
    await make_admin(store, team.id, "bob")
    current = await make_admin(store, team.id, "carol")
    assert (await store.teams.get_team("bob", team.id)).role == "admin"
    assert (await store.teams.list_teams("bob"))[0].role == "admin"
    roster = await store.teams.list_members("bob", team.id)
    assert [member.role for member in roster] == ["owner", "admin", "admin", "member"]
    renamed = await store.teams.rename_team(
        "bob", team.id, name="Infra", expected_revision=current.revision
    )
    invitation = await invite(store, team.id, owner="bob", label="admin-invite")
    assert invitation.created_by_user_id == "bob"
    request = await store.teams.claim_invitation("eva", token_hash=token_hash("admin-invite"))
    assert [r.id for r in await store.teams.list_join_requests("bob", team.id)] == [request.id]
    assert (await store.teams.get_join_request("bob", team.id, request.id)).user_id == "eva"
    await store.teams.decide_join_request("bob", team.id, request.id, approve=True)
    await store.teams.remove_member("bob", team.id, "dave")
    for target in ("alice", "bob", "carol"):
        with pytest.raises(TeamForbidden):
            await store.teams.remove_member("bob", team.id, target)
    for role in ("member", "admin"):
        with pytest.raises(TeamForbidden):
            await store.teams.set_member_role(
                "bob", team.id, "eva", role=role, expected_revision=renamed.revision
            )
    with pytest.raises(TeamForbidden):
        await store.teams.delete_team("bob", team.id, expected_revision=renamed.revision)
    with pytest.raises(TeamForbidden):
        await store.teams.transfer_ownership(
            "bob", team.id, user_id="eva", expected_revision=renamed.revision
        )
    with pytest.raises(TeamForbidden):
        await store.teams.list_audit_events("bob", team.id, before=None, limit=10)
    another = await invite(store, team.id, owner="alice", label="owner-invite")
    await store.teams.revoke_invitation("bob", team.id, another.id)
    assert any(
        i.id == another.id and i.status == "revoked"
        for i in await store.teams.list_invitations("bob", team.id)
    )


async def test_demoting_admin_closes_their_links_and_immediately_removes_management_access(store):
    team = await new_team(store)
    await join(store, team.id, "bob")
    promoted = await make_admin(store, team.id, "bob")
    await invite(store, team.id, owner="bob", label="admin-pending")
    await invite(store, team.id, owner="bob", label="admin-open")
    request = await store.teams.claim_invitation("carol", token_hash=token_hash("admin-pending"))
    demoted = await store.teams.set_member_role(
        "alice", team.id, "bob", role="member", expected_revision=promoted.revision
    )
    assert demoted.revision == promoted.revision + 1
    assert (await store.teams.get_team("bob", team.id)).role == "member"
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("dave", token_hash=token_hash("admin-open"))
    assert (await store.teams.list_own_join_requests("carol"))[0].state == "cancelled"
    with pytest.raises(TeamForbidden):
        await store.teams.decide_join_request("bob", team.id, request.id, approve=True)
    with pytest.raises(TeamForbidden):
        await invite(store, team.id, owner="bob", label="revoked-admin")
    with pytest.raises(TeamForbidden):
        await store.teams.rename_team(
            "bob", team.id, name="Denied", expected_revision=demoted.revision
        )
    audit = await store.teams.list_audit_events("alice", team.id, before=None, limit=20)
    assert sum(e.action == "member_role_changed" and e.target_user_id == "bob" for e in audit) == 2


async def test_role_changes_are_owner_only_revision_checked_and_cannot_replace_ownership(store):
    team = await new_team(store)
    await join(store, team.id, "bob")
    with pytest.raises(TeamConflict):
        await store.teams.set_member_role(
            "alice", team.id, "alice", role="admin", expected_revision=team.revision
        )
    with pytest.raises(TeamNotFound):
        await store.teams.set_member_role(
            "alice", team.id, "outsider", role="admin", expected_revision=team.revision
        )
    with pytest.raises(TeamForbidden):
        await store.teams.set_member_role(
            "bob", team.id, "bob", role="admin", expected_revision=team.revision
        )
    promoted = await make_admin(store, team.id, "bob")
    with pytest.raises(TeamConflict):
        await store.teams.set_member_role(
            "alice", team.id, "bob", role="member", expected_revision=team.revision
        )
    same = await store.teams.set_member_role(
        "alice", team.id, "bob", role="admin", expected_revision=promoted.revision
    )
    assert same.revision == promoted.revision
    moved = await store.teams.transfer_ownership(
        "alice", team.id, user_id="bob", expected_revision=same.revision
    )
    assert moved.role == "member"
    assert (await store.teams.get_team("bob", team.id)).role == "owner"
    moved_back = await store.teams.transfer_ownership(
        "bob", team.id, user_id="alice", expected_revision=moved.revision
    )
    assert moved_back.role == "member"


@pytest.mark.parametrize("departure", ["leave", "remove"])
async def test_departed_admin_rejoins_as_an_ordinary_member(store, pool, departure):
    team = await new_team(store)
    await join(store, team.id, "bob")
    await make_admin(store, team.id, "bob")
    await invite(store, team.id, owner="bob", label="old-admin-link")
    if departure == "leave":
        await store.teams.leave_team("bob", team.id)
    else:
        await store.teams.remove_member("alice", team.id, "bob")
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("carol", token_hash=token_hash("old-admin-link"))
    await join(store, team.id, "bob", label="rejoin")
    assert (await store.teams.get_team("bob", team.id)).role == "member"
    assert (await generation(pool, team.id, "bob"))["access_generation"] == 3


async def test_demotion_racing_approval_cannot_authorize_a_later_admin_write(store):
    team = await new_team(store)
    await join(store, team.id, "bob")
    promoted = await make_admin(store, team.id, "bob")
    await invite(store, team.id, owner="alice", label="race-approval")
    request = await store.teams.claim_invitation("carol", token_hash=token_hash("race-approval"))
    results = await asyncio.gather(
        store.teams.set_member_role(
            "alice", team.id, "bob", role="member", expected_revision=promoted.revision
        ),
        store.teams.decide_join_request("bob", team.id, request.id, approve=True),
        return_exceptions=True,
    )
    assert not isinstance(results[0], Exception)
    assert not isinstance(results[1], Exception) or isinstance(results[1], TeamForbidden)
    with pytest.raises(TeamForbidden):
        await store.teams.decide_join_request("bob", team.id, request.id, approve=True)


async def test_active_links_and_pending_requests_remain_visible_after_fifty_closed_links(store):
    team = await new_team(store)
    active = await invite(store, team.id, label="keep-open")
    pending = await invite(store, team.id, label="keep-pending")
    await store.teams.claim_invitation("bob", token_hash=token_hash("keep-pending"))
    for i in range(50):
        closed = await invite(store, team.id, label=f"closed-{i}")
        await store.teams.revoke_invitation("alice", team.id, closed.id)
    listed = {i.id: i for i in await store.teams.list_invitations("alice", team.id)}
    assert listed[active.id].status == "open"
    assert listed[pending.id].request_state == "pending"
    await store.teams.revoke_invitation("alice", team.id, active.id)
    with pytest.raises(InvitationUnavailable):
        await store.teams.claim_invitation("carol", token_hash=token_hash("keep-open"))
