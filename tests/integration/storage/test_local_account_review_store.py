"""Account parity review findings N1-N8 against real Postgres.

Each case started as one of the independent reviewer's repro scripts
(``repro_v2.py`` [A]-[C], ``repro_v2_env_cleanup.py`` [F]) and pins what the
repair changed:

- N1: an address accepted while email verification is off is not proven, so
  neither the administrator address list nor the signup domain list trusts
  it until a link sent to it is followed; every identity records how it was
  verified.
- N2: an account is not erased while a host still has to clean up one of
  its environment runs (the host learns what to clean only from that row).
- N3: a team the account owns alone goes with it; one other members use
  stops the erasure until an administrator hands it to one of them, and a
  team environment the account made stays with the team.
- N4: a password reset tells the administrators of an application only when
  the reset is what verified the identity.
- N5: a sender that dies mid-batch leaves at most the message in flight
  unrecorded, so a takeover resends nothing already handed over.
- N6: signing a browser out everywhere locks the browser before its sessions,
  the order a deletion takes.
- N7: a deleted account is never counted in an audience.
- N8: a message Tencent SES would refuse for its size is recorded with that
  reason.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio
from tests.support.local_accounts import (
    LOCAL_SECRET,
    PASSWORD,
    act,
    close_pool,
    make_account,
    open_pool,
)

from cloud_agent.accounts import config, tokens
from cloud_agent.accounts.passwords import hash_password
from cloud_agent.application.accounts import LocalAccountFlows
from cloud_agent.domain.accounts import AccountTransitionRefused, permissions_for
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)
    config._derived.cache_clear()


@pytest_asyncio.fixture
async def pool():
    created = await open_pool()
    try:
        yield created
    finally:
        await close_pool(created)


@pytest_asyncio.fixture
async def storage(pool):
    return Storage(pool)


class _Flows:
    """The account flows with mail and administrator notices recorded."""

    def __init__(self, storage: Storage) -> None:
        self.mail: list = []
        self.notices: list[str] = []

        async def notify(record) -> None:
            self.notices.append(record.id)

        self.flows = LocalAccountFlows(
            storage.local_accounts,
            web_base_url=lambda: "https://agent.example.edu",
            send=self.mail.append,
            notify_admins=notify,
        )


async def _reset(storage: Storage, flows: LocalAccountFlows, account_id: str, password: str):
    """Follow a reset link sent to the account's address, as its mailbox's holder."""
    token = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="reset_password",
        token_hash=tokens.email_token_hash(token),
        ttl_s=3600,
    )
    await flows.reset_password(token, password)


async def _stopped(pool, account_id: str) -> None:
    """As if the termination pass had acknowledged the deletion's cleanup."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_access_terminations SET completed_at = NOW() WHERE account_id = $1",
            account_id,
        )


async def _erase(storage: Storage, account_id: str):
    return await storage.local_accounts.erase_account(
        account_id=account_id, actor_id="admin-token", actor_account_id=None, admin_ip="t"
    )


async def _verified_by(pool, account_id: str) -> dict[str, str | None]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, verified_by FROM agent_local_login_identities WHERE account_id = $1",
            account_id,
        )
    return {row["kind"]: row["verified_by"] for row in rows}


# ── N1: proof of the mailbox ─────────────────────────────────────────────


async def test_with_verification_off_a_listed_address_waits_for_proof_of_its_mailbox(
    storage, pool, monkeypatch
):
    """repro [A]: a stranger who signs up first with a listed address gets nothing."""
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_REQUIRE_EMAIL_VERIFICATION", "false")
    monkeypatch.setenv("AGENT_LOCAL_ADMIN_EMAILS", "boss@lab.edu")
    recorder = _Flows(storage)
    flows = recorder.flows
    await flows.sign_up("boss@lab.edu", PASSWORD)
    signed = await flows.sign_in("boss@lab.edu", PASSWORD)
    assert (signed.account.email_verified, signed.account.email_proven) == (True, False)
    assert (signed.account.is_admin, permissions_for(signed.account)) == (False, [])
    assert await _verified_by(pool, signed.account.id) == {"email": "policy"}

    # The address's real holder proves it with a reset link, and only then is promoted.
    await _reset(storage, flows, signed.account.id, "Another1Horse")
    assert await _verified_by(pool, signed.account.id) == {"email": "link"}
    promoted = await flows.sign_in("boss@lab.edu", "Another1Horse")
    assert promoted.account.is_admin
    assert permissions_for(promoted.account) == ["agent.use", "agent.admin"]


async def test_with_verification_off_a_listed_domain_waits_for_proof_of_its_mailbox(
    storage, monkeypatch
):
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_REQUIRE_EMAIL_VERIFICATION", "false")
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_APPROVAL", "domains")
    await storage.local_accounts.add_signup_domain(
        domain="lab.edu", is_wildcard=False, actor_id="admin-token", admin_ip="t"
    )
    recorder = _Flows(storage)
    outcome = await recorder.flows.sign_up("anyone@lab.edu", PASSWORD, use_case="research")
    assert outcome.account is not None
    assert outcome.account.admission_status == "pending"
    await _reset(storage, recorder.flows, outcome.account.id, "Another1Horse")
    approved = await storage.local_accounts.get_account(outcome.account.id)
    assert approved is not None and approved.admission_status == "approved"


async def test_every_identity_records_what_verified_it(storage, pool):
    from cloud_agent.accounts.bootstrap_admin import create_account

    linked = await make_account(storage, "linked@example.edu")
    operator = await create_account(
        storage.local_accounts,
        email="ops@example.edu",
        login_name=None,
        password=PASSWORD,
        admin=False,
    )
    named = await create_account(
        storage.local_accounts, email=None, login_name="named", password=PASSWORD, admin=False
    )
    phone = tokens.new_account_id()
    assert await storage.local_accounts.create_phone_account(
        account_id=phone,
        phone="+8613800138000",
        user_name=None,
        tos_accepted=True,
        purpose=None,
    )
    unverified = await make_account(storage, "later@example.edu", verified=False)
    assert await _verified_by(pool, linked) == {"email": "link"}
    assert await _verified_by(pool, operator) == {"email": "operator"}
    assert await _verified_by(pool, named) == {"login_name": "operator"}
    assert await _verified_by(pool, phone) == {"phone": "code"}
    assert await _verified_by(pool, unverified) == {"email": None}
    # The operator's address counts as proven; one accepted by policy does not.
    for account_id, proven in ((linked, True), (operator, True), (unverified, False)):
        record = await storage.local_accounts.get_account(account_id)
        assert record is not None and record.email_proven is proven

    # Binding an address proves it by its link.
    token = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=phone,
        purpose="bind_email",
        token_hash=tokens.email_token_hash(token),
        ttl_s=3600,
        pending_email="bound@example.edu",
        pending_email_normalized="bound@example.edu",
    )
    assert await storage.local_accounts.bind_email(token_hash=tokens.email_token_hash(token))
    assert await _verified_by(pool, phone) == {"phone": "code", "email": "link"}


async def test_a_verification_link_proves_an_address_accepted_by_policy(storage, pool):
    account_id = tokens.new_account_id()
    assert await storage.local_accounts.create_account(
        account_id=account_id,
        email="accepted@example.edu",
        email_normalized="accepted@example.edu",
        password_hash=await hash_password(PASSWORD),
        email_verified_by="policy",
    )
    assert (
        await storage.local_accounts.promote_listed_admin(
            account_id=account_id, actor_id="admin-emails"
        )
        is None
    )
    token = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="verify_email",
        token_hash=tokens.email_token_hash(token),
        ttl_s=60,
    )
    assert await storage.local_accounts.verify_email(token_hash=tokens.email_token_hash(token))
    assert await _verified_by(pool, account_id) == {"email": "link"}
    promoted = await storage.local_accounts.promote_listed_admin(
        account_id=account_id, actor_id="admin-emails"
    )
    assert promoted is not None and promoted.is_admin


async def test_the_database_refuses_a_verification_without_its_source(storage, pool):
    account_id = await make_account(storage, "sourced@example.edu")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_local_login_identities SET verified_by = NULL WHERE account_id = $1",
                account_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE agent_local_login_identities SET verified_by = 'guess' "
                "WHERE account_id = $1",
                account_id,
            )


async def test_reclaiming_an_address_drops_what_its_claimant_wrote(storage, pool, monkeypatch):
    """Review R3: the holder's reset takes the account back without the claimant's details."""
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_REQUIRE_EMAIL_VERIFICATION", "false")
    recorder = _Flows(storage)
    outcome = await recorder.flows.sign_up(
        "claimed@lab.edu",
        PASSWORD,
        user_name="Not the holder",
        use_case="Written by whoever signed up first",
        tos_accepted=True,
    )
    assert outcome.account is not None and outcome.account.admission_status == "pending"
    account_id = outcome.account.id
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_accounts SET locale = 'zh-CN', locale_chosen_at = NOW() "
            "WHERE id = $1",
            account_id,
        )
    token = tokens.new_email_token()
    await storage.local_accounts.issue_email_token(
        account_id=account_id,
        purpose="reset_password",
        token_hash=tokens.email_token_hash(token),
        ttl_s=3600,
    )
    reset = await storage.local_accounts.reset_password(
        token_hash=tokens.email_token_hash(token), password_hash=await hash_password("Holder1Pass")
    )
    assert reset is not None and reset.reclaimed
    record = await storage.local_accounts.get_account(account_id)
    assert record is not None
    assert (record.user_name, record.locale, record.purpose, record.admission_status) == (
        None,
        None,
        "",
        "not_applied",
    )
    assert record.email_proven
    async with pool.acquire() as conn:
        tos = await conn.fetchval(
            "SELECT tos_accepted_at FROM agent_local_accounts WHERE id = $1", account_id
        )
        audit = await conn.fetchval(
            "SELECT details->>'admission_before' FROM agent_admin_audit_log "
            "WHERE target_user_id = $1 AND action = 'agent.local_account.reclaim'",
            account_id,
        )
    assert (tos, audit) == (None, "pending")
    # A proven address is the holder's own: a later reset reclaims nothing.
    await _reset(storage, recorder.flows, account_id, "Holder2Pass")
    again = await storage.local_accounts.get_account(account_id)
    assert again is not None and again.admission_status == "not_applied"
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agent_admin_audit_log "
                "WHERE target_user_id = $1 AND action = 'agent.local_account.reclaim'",
                account_id,
            )
            == 1
        )


async def test_the_store_refuses_withdrawing_what_the_admin_list_grants_back(storage, monkeypatch):
    """Review R2: held to every caller of the action, in its own transaction."""
    monkeypatch.setenv("AGENT_LOCAL_ADMIN_EMAILS", "lead@example.edu, accepted@example.edu")
    lead = await make_account(storage, "lead@example.edu", admission="approved")
    promoted = await storage.local_accounts.promote_listed_admin(
        account_id=lead, actor_id="admin-emails"
    )
    assert promoted is not None and promoted.is_admin
    for action in ("revoke_admin", "revoke"):
        with pytest.raises(AccountTransitionRefused) as refused:
            await act(storage, lead, action)
        assert refused.value.code == "listed_admin_address"
    # An unproven listed address is not promoted, so nothing is held back for it.
    accepted = tokens.new_account_id()
    assert await storage.local_accounts.create_account(
        account_id=accepted,
        email="accepted@example.edu",
        email_normalized="accepted@example.edu",
        password_hash=await hash_password(PASSWORD),
        email_verified_by="policy",
        purpose="research",
    )
    rejected = await act(storage, accepted, "reject")
    assert rejected.account.admission_status == "rejected"
    monkeypatch.setenv("AGENT_LOCAL_ADMIN_EMAILS", "")
    withdrawn = await act(storage, lead, "revoke_admin")
    assert not withdrawn.account.is_admin


# ── N2: hosts finish their cleanup first ─────────────────────────────────


async def test_erasing_waits_until_the_host_has_cleaned_up_the_environment_runs(storage, pool):
    """repro [F]: the host's only record of what to clean survives until it is clean."""
    owner = await make_account(storage, "envs@lab.edu")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_environment_bindings (id, user_id) VALUES ('bind-1', $1)", owner
        )
        await conn.execute(
            "INSERT INTO agent_environment_runs (id, user_id, repo, base_sha, binding_id, host, "
            "state, cleanup_complete, cancel_requested) "
            "VALUES ('run-1', $1, 'o/r', 'abc', 'bind-1', 'host-a', 'running', FALSE, FALSE)",
            owner,
        )
    await act(storage, owner, "delete")
    # The termination cancels the run; a run on a host waits for that host to clean up.
    await storage.owner_work.end_owner_work(owner)
    await _stopped(pool, owner)
    with pytest.raises(AccountTransitionRefused) as waiting:
        await _erase(storage, owner)
    assert waiting.value.code == "host_cleanup_pending"
    assert "run-1" in str(waiting.value) and "host-a" in str(waiting.value)
    orphans = await storage.environments.list_environment_orphans("host-a")
    assert [run["id"] for run in orphans] == ["run-1"]

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_environment_runs SET cleanup_complete = TRUE WHERE id = 'run-1'"
        )
    result = await _erase(storage, owner)
    assert result.deleted.get("agent_environment_runs") == 1


async def test_deleting_an_account_forgets_its_phone_codes(storage, pool):
    """Scope review G17: as HybridInference's soft delete forgets the account's challenges."""
    account_id = tokens.new_account_id()
    phone = "+8613800138001"
    assert await storage.local_accounts.create_phone_account(
        account_id=account_id, phone=phone, user_name=None, tos_accepted=True, purpose=None
    )
    for purpose in ("set_password", "delete_account"):
        assert (
            await storage.local_accounts.reserve_phone_code(
                phone_hash=tokens.phone_hash(phone),
                purpose=purpose,
                account_id=account_id,
                challenge_id=tokens.new_session_id(),
                code_hash=tokens.phone_code_hash(phone, purpose, account_id, "123456"),
                expire_s=300,
                cooldown_s=0,
            )
            == 0
        )
    async with pool.acquire() as conn:
        held = await conn.fetchval(
            "SELECT count(*) FROM agent_local_phone_codes WHERE account_key = $1", account_id
        )
    assert held == 2
    await act(storage, account_id, "delete")
    async with pool.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT count(*) FROM agent_local_phone_codes WHERE account_key = $1", account_id
        )


# ── N3: teams ────────────────────────────────────────────────────────────


async def _team(pool, team_id: str, owner: str, *members: str) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO agent_teams (id, name, owner_user_id, created_by_user_id, "
            "create_request_key) VALUES ($1, $2, $3, $3, $1)",
            team_id,
            f"Team {team_id}",
            owner,
        )
        await conn.execute(
            "INSERT INTO agent_team_members (team_id, user_id, role) VALUES ($1, $2, 'admin')",
            team_id,
            owner,
        )
        for member in members:
            await conn.execute(
                "INSERT INTO agent_team_members (team_id, user_id) VALUES ($1, $2)",
                team_id,
                member,
            )
        await conn.execute(
            "INSERT INTO agent_team_invitations (id, team_id, created_by_user_id, request_key, "
            "token_hash, expires_at) VALUES ($1, $2, $3, $1, $4, NOW() + INTERVAL '1 day')",
            f"inv-{team_id}",
            team_id,
            owner,
            tokens.email_token_hash(tokens.new_email_token()),
        )


async def test_erasing_deletes_a_team_the_account_owns_alone(storage, pool):
    """repro [C] alone: refused with a generic error before; now the team goes with it."""
    owner = await make_account(storage, "alone@lab.edu")
    await _team(pool, "team-alone", owner)
    await act(storage, owner, "delete")
    await _stopped(pool, owner)
    result = await _erase(storage, owner)
    assert result.deleted_teams == ["team-alone"]
    async with pool.acquire() as conn:
        team = await conn.fetchrow("SELECT state FROM agent_teams WHERE id = 'team-alone'")
        invitation = await conn.fetchval(
            "SELECT state FROM agent_team_invitations WHERE id = 'inv-team-alone'"
        )
        audit = await conn.fetchval(
            "SELECT details->>'reason' FROM agent_team_audit_events "
            "WHERE team_id = 'team-alone' AND action = 'team_deleted'"
        )
    assert (team["state"], invitation, audit) == ("deleted", "revoked", "account_erased")


async def test_a_shared_team_is_handed_to_a_member_before_its_owner_is_erased(storage, pool):
    """repro [C] with a member: the remedy is named, taken, and nobody else loses anything."""
    owner = await make_account(storage, "owner@lab.edu")
    member = await make_account(storage, "member@lab.edu")
    await _team(pool, "team-shared", owner, member)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agent_environments (id, user_id, repo, name, normalized_name, team_id, "
            "copy_request_key) VALUES ('env-team', $1, 'o/r', 'Shared', 'shared', "
            "'team-shared', 'copy-1')",
            owner,
        )
    with pytest.raises(AccountTransitionRefused) as active:
        await storage.local_accounts.transfer_owned_team(
            account_id=owner, team_id="team-shared", user_id=member, actor_id="a", admin_ip="t"
        )
    assert active.value.code == "invalid_transition"

    await act(storage, owner, "delete")
    await _stopped(pool, owner)
    with pytest.raises(AccountTransitionRefused) as shared:
        await _erase(storage, owner)
    assert shared.value.code == "team_transfer_required"
    assert "Team team-shared (team-shared)" in str(shared.value)
    owned = await storage.local_accounts.owned_teams(owner)
    assert [(t["id"], [m["label"] for m in t["members"]]) for t in owned] == [
        ("team-shared", ["member@lab.edu"])
    ]
    with pytest.raises(AccountTransitionRefused) as stranger:
        await storage.local_accounts.transfer_owned_team(
            account_id=owner, team_id="team-shared", user_id="nobody", actor_id="a", admin_ip="t"
        )
    assert stranger.value.code == "not_a_member"

    remaining = await storage.local_accounts.transfer_owned_team(
        account_id=owner,
        team_id="team-shared",
        user_id=member,
        actor_id="admin:ops",
        admin_ip="t",
    )
    assert remaining == []
    result = await _erase(storage, owner)
    assert (result.deleted_teams, result.handed_environments) == ([], ["env-team"])
    async with pool.acquire() as conn:
        team = await conn.fetchrow(
            "SELECT state, owner_user_id FROM agent_teams WHERE id = 'team-shared'"
        )
        roster = await conn.fetch(
            "SELECT user_id, state FROM agent_team_members WHERE team_id = 'team-shared'"
        )
        environment = await conn.fetchrow(
            "SELECT user_id, team_id, copy_request_key FROM agent_environments "
            "WHERE id = 'env-team'"
        )
        transferred = await conn.fetchrow(
            "SELECT actor_user_id, target_user_id FROM agent_team_audit_events "
            "WHERE team_id = 'team-shared' AND action = 'ownership_transferred'"
        )
        invitation = await conn.fetchval(
            "SELECT state FROM agent_team_invitations WHERE id = 'inv-team-shared'"
        )
    assert (team["state"], team["owner_user_id"]) == ("active", member)
    # The member keeps the team and the environment; only the erased owner's row is gone.
    assert [(r["user_id"], r["state"]) for r in roster] == [(member, "active")]
    assert dict(environment) == {
        "user_id": member,
        "team_id": "team-shared",
        "copy_request_key": None,
    }
    assert dict(transferred) == {"actor_user_id": "admin:ops", "target_user_id": member}
    assert invitation == "revoked"


# ── N4: one notice per application ───────────────────────────────────────


@pytest.mark.parametrize("verification", ["true", "false"])
async def test_a_password_reset_tells_the_administrators_only_when_it_verifies(
    storage, monkeypatch, verification
):
    """repro [B]: 1 notice after sign-up became 3 after two resets."""
    monkeypatch.setenv("AGENT_LOCAL_SIGNUP_REQUIRE_EMAIL_VERIFICATION", verification)
    recorder = _Flows(storage)
    outcome = await recorder.flows.sign_up("applicant@lab.edu", PASSWORD, use_case="research")
    assert outcome.account is not None
    account_id = outcome.account.id
    # Verification on: nobody is told until the identity is verified.
    assert len(recorder.notices) == (0 if verification == "true" else 1)
    for attempt in range(2):
        await _reset(storage, recorder.flows, account_id, f"Another1Horse{attempt}")
    assert recorder.notices == [account_id]


# ── N5: a crashed sender ─────────────────────────────────────────────────


class _CrashingMailer:
    configured = True

    def __init__(self, crash_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.crash_after = crash_after

    async def send(self, mail) -> bool:
        if self.crash_after is not None and len(self.sent) == self.crash_after:
            raise RuntimeError("the process stops here")
        self.sent.append(mail.to)
        return True


async def _broadcast(storage: Storage, *, body: str = "Hi") -> str:
    broadcast_id = tokens.new_session_id()
    await storage.local_accounts.create_broadcast(
        broadcast_id=broadcast_id,
        subject="Hello",
        body_html=f"<p>{body}</p>",
        body_text=body,
        template_key=None,
        statuses=["active"],
        admissions=["approved"],
        scheduled_at=None,
        created_by="admin-token",
        admin_ip="t",
    )
    return broadcast_id


async def test_a_sender_that_dies_mid_batch_resends_nothing_it_handed_over(storage, pool):
    from cloud_agent.application.account_broadcasts import send_broadcast

    for index in range(4):
        await make_account(storage, f"reader{index}@example.edu", admission="approved")
    accounts = storage.local_accounts
    broadcast_id = await _broadcast(storage)
    claimed = await accounts.claim_due_broadcast()
    assert claimed is not None
    dying = _CrashingMailer(crash_after=2)
    with pytest.raises(RuntimeError):
        await send_broadcast(accounts, dying, claimed)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_broadcasts SET claimed_at = NOW() - INTERVAL '1 hour' "
            "WHERE id = $1",
            broadcast_id,
        )
    resumed = await accounts.claim_due_broadcast()
    assert resumed is not None
    takeover = _CrashingMailer()
    assert await send_broadcast(accounts, takeover, resumed) == "sent"
    # Every reader exactly once: what the first sender handed over was recorded.
    assert len(dying.sent) == 2 and len(takeover.sent) == 2
    assert sorted(dying.sent + takeover.sent) == [f"reader{i}@example.edu" for i in range(4)]


async def test_a_stalled_sender_stops_once_another_took_its_claim(storage, pool):
    """Review R1: the first sender resumes after a takeover and sends nothing more."""
    from cloud_agent.application.account_broadcasts import send_broadcast

    for index in range(3):
        await make_account(storage, f"stalled{index}@example.edu", admission="approved")
    accounts = storage.local_accounts
    broadcast_id = await _broadcast(storage)
    stalled = await accounts.claim_due_broadcast()
    assert stalled is not None
    # The first sender stalls past the stale bound; another takes the broadcast over.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_local_broadcasts SET claimed_at = NOW() - INTERVAL '1 hour' "
            "WHERE id = $1",
            broadcast_id,
        )
    current = await accounts.claim_due_broadcast()
    assert current is not None and current["claim_token"] != stalled["claim_token"]
    late = _CrashingMailer()
    assert await send_broadcast(accounts, late, stalled) == "claim_lost"
    assert late.sent == []
    pending = await accounts.pending_broadcast_recipients(broadcast_id=broadcast_id, limit=5)
    assert not await accounts.record_broadcast_results(
        broadcast_id=broadcast_id,
        claim_token=stalled["claim_token"],
        outcomes=[(pending[0]["account_id"], "sent", None)],
    )
    assert (
        await accounts.finish_broadcast(
            broadcast_id=broadcast_id, claim_token=stalled["claim_token"]
        )
        is None
    )
    holder = _CrashingMailer()
    assert await send_broadcast(accounts, holder, current) == "sent"
    assert sorted(holder.sent) == [f"stalled{i}@example.edu" for i in range(3)]


async def test_a_message_tencent_ses_cannot_take_is_recorded_with_why(storage, monkeypatch):
    from cloud_agent.application.account_broadcasts import send_broadcast

    await make_account(storage, "long@example.edu", admission="approved")
    accounts = storage.local_accounts
    await _broadcast(storage, body="word " * 400)
    # The transport changed to SES after the message was created.
    monkeypatch.setenv("AGENT_LOCAL_EMAIL_PROVIDER", "tencent_ses")
    monkeypatch.setenv("AGENT_TENCENT_SES_TEMPLATE_NOTICE", "7")
    claimed = await accounts.claim_due_broadcast()
    assert claimed is not None
    mailer = _CrashingMailer()
    assert await send_broadcast(accounts, mailer, claimed) == "failed"
    assert mailer.sent == []
    found = await accounts.broadcast_detail(broadcast_id=claimed["id"], limit=5, offset=0)
    assert found is not None
    [recipient] = found[1]
    assert recipient["status"] == "failed" and "800 bytes" in recipient["error"]


# ── N6: lock order when signing out everywhere ───────────────────────────


async def test_signing_out_everywhere_locks_the_browser_before_any_session(storage, pool):
    account_id = await make_account(storage, "everywhere@example.edu")
    token = tokens.new_browser_token()
    await storage.local_accounts.create_browser(
        browser_id="browser-all", token_hash=tokens.browser_hash(token), ttl_s=600
    )
    session_id = tokens.new_session_id()
    stored = await storage.local_accounts.stored_password_hash(account_id)
    assert stored is not None
    await storage.local_accounts.create_session(
        session_id=session_id,
        account_id=account_id,
        token_hash=tokens.session_hash(tokens.new_session_token()),
        ttl_s=600,
        password_hash=stored,
    )
    assert await storage.local_accounts.attach_browser_account(
        browser_id="browser-all",
        account_id=account_id,
        session_id=session_id,
        max_accounts=5,
        new_token_hash=tokens.browser_hash(token),
        ttl_s=600,
    )
    holder = await pool.acquire()
    probe = await pool.acquire()
    revoking = None
    try:
        # A deletion's position: it holds the browser and has yet to revoke sessions.
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute(
            "SELECT 1 FROM agent_local_browser_sessions WHERE id = 'browser-all' FOR UPDATE"
        )
        revoking = asyncio.create_task(
            storage.local_accounts.revoke_browser(browser_id="browser-all")
        )
        async with asyncio.timeout(10):
            while not await probe.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query LIKE '%agent_local_browser_sessions%' AND pid <> $1)",
                holder.get_server_pid(),
            ):
                await asyncio.sleep(0.05)
        # Waiting on the browser, it holds none of the sessions the deletion needs next.
        probe_transaction = probe.transaction()
        await probe_transaction.start()
        assert await probe.fetchval(
            "SELECT 1 FROM agent_local_sessions WHERE id = $1 FOR UPDATE NOWAIT", session_id
        )
        await probe_transaction.rollback()
        await transaction.rollback()
        async with asyncio.timeout(10):
            await revoking
    finally:
        if revoking is not None and not revoking.done():
            revoking.cancel()
        await pool.release(probe)
        await pool.release(holder)
    assert await storage.local_accounts.browser_state(tokens.browser_hash(token)) is None


async def test_signing_out_everywhere_racing_a_deletion_never_deadlocks(storage):
    for index in range(10):
        account_id = await make_account(storage, f"leaver{index}@example.edu")
        token = tokens.new_browser_token()
        browser_id = f"leave-{index}"
        await storage.local_accounts.create_browser(
            browser_id=browser_id, token_hash=tokens.browser_hash(token), ttl_s=600
        )
        session_id = tokens.new_session_id()
        stored = await storage.local_accounts.stored_password_hash(account_id)
        assert stored is not None
        await storage.local_accounts.create_session(
            session_id=session_id,
            account_id=account_id,
            token_hash=tokens.session_hash(tokens.new_session_token()),
            ttl_s=600,
            password_hash=stored,
        )
        await storage.local_accounts.attach_browser_account(
            browser_id=browser_id,
            account_id=account_id,
            session_id=session_id,
            max_accounts=5,
            new_token_hash=tokens.browser_hash(token),
            ttl_s=600,
        )
        record = await storage.local_accounts.get_account(account_id)
        assert record is not None
        outcomes = await asyncio.gather(
            storage.local_accounts.revoke_browser(browser_id=browser_id),
            storage.local_accounts.delete_own_account(
                account_id=account_id, expected_version=record.version, admin_ip="t"
            ),
            return_exceptions=True,
        )
        assert not any(isinstance(o, Exception) for o in outcomes), outcomes


# ── N7: audiences ────────────────────────────────────────────────────────


async def test_a_deleted_account_is_never_counted_in_an_audience(storage):
    await make_account(storage, "stays@example.edu", admission="approved")
    leaving = await make_account(storage, "left@example.edu", admission="approved")
    await act(storage, leaving, "delete")
    audience = await storage.local_accounts.broadcast_audience(
        statuses=["active", "suspended", "deleted"], admissions=["approved", "revoked"]
    )
    broadcast_id = await _broadcast(storage)
    detail = await storage.local_accounts.broadcast_detail(
        broadcast_id=broadcast_id, limit=5, offset=0
    )
    assert detail is not None
    assert audience == detail[0]["recipient_count"] == 1
