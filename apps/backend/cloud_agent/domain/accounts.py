"""Local accounts and the Agent waiting list: states, decisions and admin transitions.

Identity and admission are separate on purpose. An account is who signs in;
its admission is whether that person may use Agent. A verified account whose
application is pending, rejected or revoked still signs in and reads its
status; only an approved one is granted ``agent.use``, and only an account an
administrator explicitly marked is granted ``agent.admin`` on top of it.

An account's identity is *verified* when it has no unverified email and at
least one verified sign-in identity: a confirmed email, a login name (created
by the operator, so verified by construction) or a phone number confirmed by
a code. The database computes the same rule in ``agent_local_account_access``;
:attr:`AccountRecord.identity_verified` is read from there, never derived
again from one identity kind.

Everything here is pure: the storage layer applies a transition inside the
transaction that read the row it judges, and the routes only translate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from datetime import datetime

AccountStatus = Literal["active", "suspended", "deleted"]
AdmissionStatus = Literal["not_applied", "pending", "approved", "rejected", "revoked"]
AdminAction = Literal[
    "approve",
    "reject",
    "revoke",
    "suspend",
    "restore",
    "grant_admin",
    "revoke_admin",
    "delete",
    "resume",
]

ADMISSION_STATUSES: tuple[str, ...] = get_args(AdmissionStatus)
ADMIN_ACTIONS: tuple[str, ...] = get_args(AdminAction)

#: The longest application purpose and review reason. The columns enforce it.
MAX_PURPOSE_CHARS = 2000
MAX_REASON_CHARS = 2000

#: Actions that take Agent use away and therefore stop the owner's work.
ACCESS_ENDING_ACTIONS = frozenset({"revoke", "suspend", "delete"})
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


class IdentifierTaken(Exception):
    """An email, login name or phone number already signs in to another account."""


class SelfDeleteRefused(Exception):
    """The account may not delete itself (an administrator), or changed meanwhile."""


@dataclass(frozen=True)
class AccountRecord:
    """One local account with its sign-in and admission state, never its hash."""

    id: str
    email: str | None
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
    identity_verified: bool = False
    login_name: str | None = None
    phone: str | None = None
    has_password: bool = False
    user_name: str | None = None
    admin_note: str | None = None
    suspension_message: str | None = None
    last_login_at: datetime | None = None
    deleted_at: datetime | None = None
    erased_at: datetime | None = None
    locale: str | None = None
    locale_chosen_at: datetime | None = None
    reviewed_by: str | None = None
    #: The email was proven by a link sent to it, or set by the operator, not
    #: merely accepted because email verification is off.
    email_proven: bool = False

    @property
    def label(self) -> str:
        """The name the account signs in with: its email, else login name, else phone."""
        return self.email or self.login_name or self.phone or self.id


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
        record.identity_verified
        and record.status == "active"
        and record.admission_status == "approved"
    ):
        return []
    return ["agent.use", "agent.admin"] if record.is_admin else ["agent.use"]


def may_sign_in(record: AccountRecord) -> bool:
    """Whether the account may hold a session: verified, and neither suspended nor deleted."""
    return record.identity_verified and record.status == "active"


def _refuse(message: str, code: str = "invalid_transition") -> AccountTransitionRefused:
    return AccountTransitionRefused(message, code=code)


def _approve(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    if not record.identity_verified:
        raise _refuse("Only an account with a verified sign-in identity can be approved.")
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
    if record.status == "deleted":
        raise _refuse("A deleted account cannot be suspended.")
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
        record.identity_verified
        and record.status == "active"
        and record.admission_status == "approved"
    ):
        raise _refuse("Administration can be granted only to a verified, active, approved account.")
    return AdmissionEdit("active", True, "approved", None, False, False)


def _revoke_admin(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    if not record.is_admin:
        raise _refuse("The account is not an administrator.")
    return AdmissionEdit(record.status, False, record.admission_status, None, False, False)


def deletion_edit(record: AccountRecord) -> AdmissionEdit:
    """What deleting an active or suspended account writes, by its owner or an admin.

    As HybridInference's soft delete: the account keeps its data and can be
    resumed by an administrator, but it signs in nowhere, holds no session
    and no administration, and approved access becomes revoked, so resuming
    it never brings Agent use back without a fresh approval.
    """
    if record.status == "deleted":
        raise _refuse("The account is already deleted.")
    admission = "revoked" if record.admission_status == "approved" else record.admission_status
    reviewed = record.admission_status == "approved"
    return AdmissionEdit("deleted", False, admission, None, reviewed, True)


def _delete(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    return deletion_edit(record)


def _resume(record: AccountRecord, _reason: str | None) -> AdmissionEdit:
    if record.status != "deleted":
        raise _refuse("Only a deleted account can be resumed.")
    if record.erased_at is not None:
        raise _refuse("An erased account cannot be resumed. Create a new account instead.")
    return AdmissionEdit("active", False, record.admission_status, None, False, False)


_TRANSITIONS = {
    "approve": _approve,
    "reject": _reject,
    "revoke": _revoke,
    "suspend": _suspend,
    "restore": _restore,
    "grant_admin": _grant_admin,
    "revoke_admin": _revoke_admin,
    "delete": _delete,
    "resume": _resume,
}

#: What an administrator may not do to their own account: each would leave the
#: console without the person using it, and the deployment's ADMIN_TOKEN can.
_SELF_LOCKOUT = frozenset({"suspend", "revoke", "revoke_admin", "delete"})


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


def plan_self_deletion(record: AccountRecord) -> AdmissionEdit:
    """What an owner deleting their own account writes, or the refusal.

    An administrator cannot delete itself, as in HybridInference: the
    deployment must not lose its last administrator this way.

    Raises:
        SelfDeleteRefused: The account is an administrator or not active.
    """
    if record.is_admin:
        raise SelfDeleteRefused("An administrator account cannot delete itself.")
    if record.status != "active":
        raise SelfDeleteRefused("Only an active account can be deleted by its owner.")
    return deletion_edit(record)


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
    "IdentifierTaken",
    "SelfDeleteRefused",
    "WaitlistNotResubmittable",
    "deletion_edit",
    "may_sign_in",
    "permissions_for",
    "plan_admin_action",
    "plan_self_deletion",
    "plan_waitlist_submission",
    "require_actor_admin",
]
