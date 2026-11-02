"""Local account identity, admission and administrator transitions, as pure rules."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from cloud_agent.domain.accounts import (
    AccountRecord,
    AccountTransitionRefused,
    WaitlistNotResubmittable,
    may_sign_in,
    permissions_for,
    plan_admin_action,
    plan_waitlist_submission,
    require_actor_admin,
)

_NOW = datetime(2026, 10, 28, tzinfo=UTC)


def _account(**changes) -> AccountRecord:
    # An email account's identity is verified exactly when its email is: the
    # rule ``agent_local_account_access`` computes and the record carries.
    changes.setdefault("identity_verified", changes.get("email_verified", True))
    base = AccountRecord(
        id="01J0000000000000000000000A",
        email="person@example.org",
        email_verified=True,
        status="active",
        is_admin=False,
        version=3,
        created_at=_NOW,
        admission_status="pending",
        purpose="",
        review_reason=None,
        submitted_at=_NOW,
        reviewed_at=None,
    )
    return replace(base, **changes)


def _apply(record: AccountRecord, action: str, *, reason=None, actor=None) -> AccountRecord:
    edit = plan_admin_action(record, action, reason=reason, actor_account_id=actor)
    return replace(
        record,
        status=edit.account_status,
        is_admin=edit.is_admin,
        admission_status=edit.admission_status,
        review_reason=edit.review_reason if edit.reviewed else record.review_reason,
    )


@pytest.mark.parametrize("status", ["not_applied", "pending", "rejected", "revoked"])
def test_a_verified_account_that_is_not_approved_signs_in_but_holds_nothing(status):
    record = _account(admission_status=status)
    assert may_sign_in(record)
    assert permissions_for(record) == []


def test_approval_grants_use_and_only_the_flag_adds_administration():
    approved = _account(admission_status="approved")
    assert permissions_for(approved) == ["agent.use"]
    assert permissions_for(replace(approved, is_admin=True)) == ["agent.use", "agent.admin"]


@pytest.mark.parametrize(
    "changes",
    [{"email_verified": False}, {"status": "suspended"}, {"admission_status": "revoked"}],
)
def test_the_admin_flag_alone_grants_nothing(changes):
    record = _account(**{"admission_status": "approved", "is_admin": True, **changes})
    assert permissions_for(record) == []


def test_an_unverified_account_cannot_sign_in():
    assert not may_sign_in(_account(email_verified=False))


def test_suspension_withdraws_approval_and_administration_for_good():
    admin = _account(admission_status="approved", is_admin=True)
    suspended = _apply(admin, "suspend")
    assert suspended.status == "suspended"
    assert suspended.admission_status == "revoked"
    assert suspended.is_admin is False
    restored = _apply(suspended, "restore")
    assert restored.status == "active"
    # Restoring lets the account sign in and reapply; it grants nothing.
    assert permissions_for(restored) == []
    with pytest.raises(AccountTransitionRefused):
        _apply(restored, "grant_admin")
    reapproved = _apply(restored, "approve")
    assert permissions_for(reapproved) == ["agent.use"]


def test_ending_actions_are_marked_so_their_work_is_stopped():
    approved = _account(admission_status="approved")
    for action in ("revoke", "suspend"):
        assert plan_admin_action(approved, action, reason=None, actor_account_id=None).ends_access
    for action in ("approve", "reject"):
        record = _account()
        assert not plan_admin_action(record, action, reason=None, actor_account_id=None).ends_access


def test_granting_administration_never_implies_approval():
    with pytest.raises(AccountTransitionRefused):
        _apply(_account(admission_status="pending"), "grant_admin")
    with pytest.raises(AccountTransitionRefused):
        _apply(_account(admission_status="approved", email_verified=False), "grant_admin")
    granted = _apply(_account(admission_status="approved"), "grant_admin")
    assert permissions_for(granted) == ["agent.use", "agent.admin"]


def test_revoking_access_also_takes_administration():
    revoked = _apply(_account(admission_status="approved", is_admin=True), "revoke", reason="gone")
    assert revoked.is_admin is False
    assert revoked.review_reason == "gone"
    assert permissions_for(revoked) == []


def test_only_a_verified_active_account_is_approved():
    with pytest.raises(AccountTransitionRefused):
        _apply(_account(email_verified=False), "approve")
    with pytest.raises(AccountTransitionRefused):
        _apply(_account(status="suspended"), "approve")


def test_only_a_pending_application_is_rejected_and_the_reason_is_kept():
    with pytest.raises(AccountTransitionRefused):
        _apply(_account(admission_status="approved"), "reject")
    rejected = _apply(_account(), "reject", reason="not yet")
    assert rejected.admission_status == "rejected"
    assert rejected.review_reason == "not yet"


@pytest.mark.parametrize("action", ["suspend", "revoke", "revoke_admin"])
def test_an_administrator_cannot_lock_themself_out(action):
    admin = _account(admission_status="approved", is_admin=True)
    with pytest.raises(AccountTransitionRefused) as refused:
        plan_admin_action(admin, action, reason=None, actor_account_id=admin.id)
    assert refused.value.code == "self_lockout"


def test_an_actor_whose_administration_ended_is_refused():
    require_actor_admin(_account(admission_status="approved", is_admin=True))
    for actor in (
        None,
        _account(admission_status="approved"),
        _account(admission_status="approved", is_admin=True, status="suspended"),
    ):
        with pytest.raises(AccountTransitionRefused) as refused:
            require_actor_admin(actor)
        assert refused.value.code == "actor_not_admin"


def test_an_unknown_action_is_refused():
    # "delete" became a real action (account deletion); any other name is still refused.
    with pytest.raises(AccountTransitionRefused):
        plan_admin_action(_account(), "purge", reason=None, actor_account_id=None)


def test_applying_is_idempotent_while_pending_and_explicit_after_a_refusal():
    assert plan_waitlist_submission(_account(admission_status="not_applied")) is True
    assert plan_waitlist_submission(_account(admission_status="pending")) is False
    assert plan_waitlist_submission(_account(admission_status="rejected")) is True
    assert plan_waitlist_submission(_account(admission_status="revoked")) is True
    with pytest.raises(WaitlistNotResubmittable):
        plan_waitlist_submission(_account(admission_status="approved"))
