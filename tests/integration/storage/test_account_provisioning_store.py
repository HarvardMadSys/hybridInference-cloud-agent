"""Inference provisioning rows against real Postgres.

The lifecycle rules that matter are temporal and transactional: only
approvals at or after activation are queued automatically, the backfill
queues earlier ones exactly once, and ending an account's access surfaces
its row for revocation. A fake clock in SQL is the only honest way to
check the first of those.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from tests.support.local_accounts import (
    LOCAL_SECRET,
    act,
    close_pool,
    make_account,
    open_pool,
)

from cloud_agent.accounts import config
from cloud_agent.application.owner_termination import enforce_ended_access
from cloud_agent.storage.assembly import Storage

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(config.ENV_LOCAL_SESSION_SECRET, LOCAL_SECRET)


@pytest_asyncio.fixture
async def storage():
    pool = await open_pool()
    try:
        yield Storage(pool)
    finally:
        await close_pool(pool)


async def test_only_approvals_after_activation_are_queued(storage: Storage):
    repo = storage.account_provisioning
    early = await make_account(storage, "early@example.org", admission="approved")

    activated_at = await repo.ensure_activation()
    assert await repo.enqueue_new_approvals(activated_at=activated_at) == 0
    assert await repo.pending() == []

    late = await make_account(storage, "late@example.org", admission="approved")
    assert await repo.enqueue_new_approvals(activated_at=activated_at) == 1
    pending = await repo.pending()
    assert [row["account_id"] for row in pending] == [late]
    assert pending[0]["email"] == "late@example.org"
    assert early != late


async def test_backfill_queues_an_earlier_approval_exactly_once(storage: Storage):
    repo = storage.account_provisioning
    early = await make_account(storage, "early@example.org", admission="approved")
    await repo.ensure_activation()

    assert await repo.eligible_account_ids() == [early]
    assert await repo.enqueue_accounts([early]) == 1
    await repo.mark_invited(early)
    # A second backfill run leaves an invited row exactly as it stands.
    assert await repo.enqueue_accounts([early]) == 0
    assert [row["account_id"] for row in await repo.invited()] == [early]


async def test_access_ending_surfaces_the_row_and_reapproval_requeues(storage: Storage):
    repo = storage.account_provisioning
    account = await make_account(storage, "member@example.org", admission="approved")
    activated_at = await repo.ensure_activation()
    # Approved before activation in this test's clock; queue it by backfill.
    await repo.enqueue_accounts([account])
    await repo.mark_invited(account)
    assert await repo.access_ended() == []

    await act(storage, account, "revoke")
    ended = await repo.access_ended()
    assert [(row["account_id"], row["state"]) for row in ended] == [(account, "invited")]
    await repo.mark_revoked(account, audited=True)

    # Re-approval waits until the ended access has been stopped and
    # acknowledged, as it does for an administrator.
    await enforce_ended_access(storage)
    await act(storage, account, "approve")
    assert await repo.enqueue_new_approvals(activated_at=activated_at) == 1
    assert [row["account_id"] for row in await repo.pending()] == [account]


async def test_invited_and_revoked_marks_write_audit_rows(storage: Storage):
    repo = storage.account_provisioning
    account = await make_account(storage, "audited@example.org", admission="approved")
    await repo.enqueue_accounts([account])

    await repo.mark_invited(account)
    await repo.mark_revoked(account, audited=True)

    rows = await storage.audit_log.list_admin_actions(subject_user_id=account, limit=20)
    actions = [row["action"] for row in reversed(rows)]
    assert actions == [
        "agent.local_account.approve",
        "agent.local_account.provision_invite",
        "agent.local_account.provision_revoke",
    ]
