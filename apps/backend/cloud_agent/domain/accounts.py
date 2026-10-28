"""Local accounts and the Agent waiting list: states, decisions and admin transitions.

Identity and admission are separate on purpose. An account is who signs in;
its admission is whether that person may use Agent. A verified account whose
application is pending, rejected or revoked still signs in and reads its
status; only an approved one is granted ``agent.use``, and only an account an
administrator explicitly marked is granted ``agent.admin`` on top of it.

Everything here is pure: the storage layer applies a transition inside the
transaction that read the row it judges, and the routes only translate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from datetime import datetime

AccountStatus = Literal["active", "suspended"]
AdmissionStatus = Literal["not_applied", "pending", "approved", "rejected", "revoked"]
AdminAction = Literal[
    "approve", "reject", "revoke", "suspend", "restore", "grant_admin", "revoke_admin"
]

ADMISSION_STATUSES: tuple[str, ...] = get_args(AdmissionStatus)
ADMIN_ACTIONS: tuple[str, ...] = get_args(AdminAction)

#: The longest application purpose and review reason. The columns enforce it.
MAX_PURPOSE_CHARS = 2000
MAX_REASON_CHARS = 2000

#: Actions that take Agent use away and therefore stop the owner's work.
ACCESS_ENDING_ACTIONS = frozenset({"revoke", "suspend"})
#: Actions that give Agent use or administration, refused while an earlier
#: revocation's cleanup is still open.
ACCESS_GRANTING_ACTIONS = frozenset({"approve", "grant_admin"})


class AccountNotFound(LookupError):
    """No local account has that id."""


class AccountVersionConflict(Exception):
    """The account changed after the administrator read it."""

    def __init__(self, current_version: int) -> None:
        """Remember the version the caller should re-read."""
        super().__init__(f"account is at version {current_version}")
        self.current_version = current_version


class AccountTransitionRefused(Exception):
    """The action does not apply to the account in its current state."""

    def __init__(self, message: str, *, code: str = "invalid_transition") -> None:
        """Keep a fixed refusal code beside the sentence the admin reads."""
        super().__init__(message)
        self.code = code


class EmailTaken(Exception):
    """A sign-up lost the race for its address; its transaction rolls back."""


class WaitlistNotResubmittable(Exception):
    """An approved account has nothing to apply for."""


@dataclass(frozen=True)
class AccountRecord:
    """One local account with its sign-in and admission state, never its hash."""

    id: str
    email: str
    email_verified: bool
    status: AccountStatus
    is_admin: bool
    version: int
    created_at: datetime
    admission_status: AdmissionStatus
    purpose: str
    review_reason: str | None
    submitted_at: datetime | None
    reviewed_at: datetime | None


@dataclass(frozen=True)
class AdmissionEdit:
    """What one admin action writes: the new state and what the audit row records."""

    account_status: AccountStatus
    is_admin: bool
    admission_status: AdmissionStatus
    review_reason: str | None
    reviewed: bool
    ends_access: bool


def permissions_for(record: AccountRecord) -> list[str]:
    """The Agent permissions an account currently holds.

    ``agent.use`` needs a verified, active, approved account. ``agent.admin``
    needs all of that as well as the explicit flag: the flag alone grants
    nothing, so an admin whose admission is revoked or who is suspended loses
    the console with everything else.
    """
    if not (
        record.email_verified
        and record.status == "active"
        and record.admission_status == "approved"
    ):
        return []
    return ["agent.use", "agent.admin"] if record.is_admin else ["agent.use"]


def may_sign_in(record: AccountRecord) -> bool:
    """Whether the account may hold a session: verified and not suspended."""
    return record.email_verified and record.status == "active"


def _refuse(message: str, code: str = "invalid_transition") -> AccountTransitionRefused:
    return AccountTransitionRefused(message, code=code)


def _approve(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    if not record.email_verified:
        raise _refuse("Only an account with a verified email can be approved.")
    if record.status != "active":
        raise _refuse("Restore the account before approving it.")
    if record.admission_status == "approved":
        raise _refuse("The account is already approved.")
    return AdmissionEdit("active", record.is_admin, "approved", None, True, False)


def _reject(record: AccountRecord, reason: str | None) -> AdmissionEdit:
    if record.admission_status != "pending":
        raise _refuse("Only a pending application can be rejected.")
    return AdmissionEdit(record.status, False, "rejected", reason, True, False)


def _revoke(record: AccountRecord, reason: str | None) -> AdmissionEdit:
    if record.admission_status != "approved":
        raise _refuse("Only approved access can be revoked.")
    return AdmissionEdit(record.status, False, "revoked", reason, True, True)


def _suspend(record: AccountRecord, reason: str | None) -> AdmissionEdit:
    # Suspension withdraws Agent use and administration for good: approved
    # access becomes revoked, so restoring the account later lets it sign in
    # and reapply, never use Agent again without a fresh approval.
    if record.status != "active":
        raise _refuse("The account is already suspended.")
    if record.admission_status == "approved":
        return AdmissionEdit("suspended", False, "revoked", reason, True, True)
    return AdmissionEdit("suspended", False, record.admission_status, None, False, True)


def _restore(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    if record.status != "suspended":
        raise _refuse("Only a suspended account can be restored.")
    return AdmissionEdit("active", False, record.admission_status, None, False, False)


def _grant_admin(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    if record.is_admin:
        raise _refuse("The account is already an administrator.")
    if not (
        record.email_verified
        and record.status == "active"
        and record.admission_status == "approved"
    ):
        raise _refuse("Administration can be granted only to a verified, active, approved account.")
    return AdmissionEdit("active", True, "approved", None, False, False)


def _revoke_admin(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    if not record.is_admin:
        raise _refuse("The account is not an administrator.")
    return AdmissionEdit(record.status, False, record.admission_status, None, False, False)


_TRANSITIONS = {
    "approve": _approve,
    "reject": _reject,
    "revoke": _revoke,
    "suspend": _suspend,
    "restore": _restore,
    "grant_admin": _grant_admin,
    "revoke_admin": _revoke_admin,
}

#: What an administrator may not do to their own account: each would leave the
#: console without the person using it, and the deployment's ADMIN_TOKEN can.
_SELF_LOCKOUT = frozenset({"suspend", "revoke", "revoke_admin"})


def require_actor_admin(actor: AccountRecord | None) -> None:
    """Refuse an action whose acting account is no longer an administrator.

    Checked under the actor's row lock in the action's own transaction, so an
    administrator suspended or demoted while their request was in flight
    changes nothing.

    Raises:
        AccountTransitionRefused: ``actor_not_admin``.
    """
    if actor is None or "agent.admin" not in permissions_for(actor):
        raise _refuse("Your administrator access has ended.", code="actor_not_admin")


def plan_admin_action(
    record: AccountRecord, action: str, *, reason: str | None, actor_account_id: str | None
) -> AdmissionEdit:
    """Decide what ``action`` does to ``record``, or refuse it.

    Args:
        record: The account as read under its row lock.
        action: One of :data:`ADMIN_ACTIONS`.
        reason: The administrator's public reason; kept only for reject/revoke.
        actor_account_id: The acting administrator's own account, or ``None``
            for the deployment's infrastructure token.

    Raises:
        AccountTransitionRefused: The action does not apply now.
    """
    transition = _TRANSITIONS.get(action)
    if transition is None:
        raise _refuse(f"Unknown action {action!r}.")
    if actor_account_id == record.id and action in _SELF_LOCKOUT:
        raise _refuse("Administrators cannot take their own access away.", code="self_lockout")
    return transition(record, reason)


def plan_waitlist_submission(record: AccountRecord) -> bool:
    """Whether submitting changes the application; ``False`` for an idempotent repeat.

    Raises:
        WaitlistNotResubmittable: The account is already approved.
    """
    if record.admission_status == "approved":
        raise WaitlistNotResubmittable
    return record.admission_status != "pending"


__all__ = [
    "ACCESS_ENDING_ACTIONS",
    "ACCESS_GRANTING_ACTIONS",
    "ADMIN_ACTIONS",
    "ADMISSION_STATUSES",
    "MAX_PURPOSE_CHARS",
    "MAX_REASON_CHARS",
    "AccountNotFound",
    "AccountRecord",
    "AccountStatus",
    "AccountTransitionRefused",
    "AccountVersionConflict",
    "AdminAction",
    "AdmissionEdit",
    "AdmissionStatus",
    "EmailTaken",
    "WaitlistNotResubmittable",
    "may_sign_in",
    "permissions_for",
    "plan_admin_action",
    "plan_waitlist_submission",
    "require_actor_admin",
]
