"""Importing another service's accounts against real Postgres.

The hash below was made by FreeInference's own ``hash_password`` (passlib,
Argon2id, 64 MiB, three passes, four lanes): an imported account must sign in
here with the password it had there, unchanged.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from tests.support.local_accounts import (
    LOCAL_SECRET,
    close_pool,
    make_account,
    open_pool,
)

from cloud_agent.accounts import config
from cloud_agent.accounts.import_accounts import ACTOR, import_accounts
from cloud_agent.application.accounts import InvalidCredentials, LocalAccountFlows
from cloud_agent.domain.accounts import permissions_for
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

FREEINFERENCE_PASSWORD = "Imported-pass-2026!"
FREEINFERENCE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$Oedc6/0/Z+zdW4sRopQyxg$XXRJBJw4FeNXlciRYhlo4xJp/RbGG/yWlOj37S7XsMA"


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


def _flows(storage: Storage) -> LocalAccountFlows:
    return LocalAccountFlows(
        storage.local_accounts,
        web_base_url=lambda: "https://agent.example.edu",
        send=lambda _: None,
    )


def _lines(*rows: dict) -> list[str]:
    return [json.dumps(row) for row in rows]


ADA = {
    "email": "Ada@Example.edu",
    "email_verified": True,
    "login_name": "ada",
    "phone": "+8613800138000",
    "phone_verified": True,
    "user_name": "Ada",
    "password_hash": FREEINFERENCE_HASH,
}


async def test_an_imported_account_signs_in_with_its_old_password_and_is_approved(storage, pool):
    report = await import_accounts(storage.local_accounts, _lines(ADA), apply=True)
    assert (report.imported, report.skipped) == (1, [])

    flows = _flows(storage)
    signed_in = await flows.sign_in("ada@example.edu", FREEINFERENCE_PASSWORD)
    account = signed_in.account
    assert (await flows.sign_in("ada", FREEINFERENCE_PASSWORD)).account.id == account.id
    with pytest.raises(InvalidCredentials):
        await flows.sign_in("ada@example.edu", "Wrong-pass-2026!")

    assert permissions_for(account) == ["agent.use"]
    assert account.admission_status == "approved" and account.email_proven
    assert (account.phone, account.user_name) == ("+8613800138000", "Ada")
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT tos_accepted_at FROM agent_local_accounts WHERE id = $1", account.id
            )
            is None
        )
        audit = await conn.fetchrow(
            "SELECT actor_id, details FROM agent_admin_audit_log "
            "WHERE action = 'agent.local_account.import' AND target_user_id = $1",
            account.id,
        )
    assert audit["actor_id"] == ACTOR
    assert "argon2" not in audit["details"] and "ada" not in audit["details"].lower()


async def test_an_unverified_email_is_imported_unproven(storage):
    row = {"email": "new@example.edu", "email_verified": False, "password_hash": FREEINFERENCE_HASH}
    assert (await import_accounts(storage.local_accounts, _lines(row), apply=True)).imported == 1
    login = await storage.local_accounts.password_login("new@example.edu")
    assert login is not None and not login.account.email_proven
    assert login.account.admission_status == "approved"


async def test_an_existing_account_is_left_alone_and_a_second_run_imports_nothing(storage):
    existing = await make_account(storage, "ada@example.edu", admission="pending")
    before = await storage.local_accounts.stored_password_hash(existing)
    bob = {"email": "bob@example.edu", "email_verified": True, "password_hash": FREEINFERENCE_HASH}

    first = await import_accounts(storage.local_accounts, _lines(ADA, bob), apply=True)
    assert first.imported == 1 and first.skipped == [(1, "identifier already has an account")]
    record = await storage.local_accounts.get_account(existing)
    assert record is not None and record.admission_status == "pending"
    assert await storage.local_accounts.stored_password_hash(existing) == before
    # Skipped whole: none of the colliding account's other identifiers was taken.
    assert await storage.local_accounts.identifier_owner("login_name", "ada") is None
    assert await storage.local_accounts.identifier_owner("phone", "+8613800138000") is None

    again = await import_accounts(storage.local_accounts, _lines(ADA, bob), apply=True)
    assert again.imported == 0 and len(again.skipped) == 2


async def test_a_dry_run_against_the_database_matches_the_apply(storage):
    await make_account(storage, "taken@example.edu")
    rows = _lines(ADA, {"email": "taken@example.edu", "email_verified": True})
    dry = await import_accounts(storage.local_accounts, rows, apply=False)
    assert await storage.local_accounts.password_login("ada@example.edu") is None
    applied = await import_accounts(storage.local_accounts, rows, apply=True)
    assert (
        (dry.imported, dry.skipped)
        == (applied.imported, applied.skipped)
        == (
            1,
            [(2, "identifier already has an account")],
        )
    )
