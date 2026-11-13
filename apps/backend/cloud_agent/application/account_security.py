"""A signed-in owner's own account changes: password, email, deletion.

Adapted from HybridInference ``reauth.py``, ``user_routes.change_password``,
``auth_routes.bind_email`` and ``auth_routes.delete_own_account`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS).

A session proves a session, not a person: one lifted from a shared computer
holds it for its lifetime. So each of these asks again, the way the account
signs in: its password, or for an account without one a code sent to its own
verified number (:func:`confirm_owner`). The caller counts each password
attempt against the sign-in limits, so this is never a second place to guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloud_agent.accounts.mail import AccountMail
from cloud_agent.accounts.passwords import hash_password, verify_password
from cloud_agent.accounts.policy import is_email_domain_blocked
from cloud_agent.application.accounts import (
    BlockedEmailDomain,
    normalize_email,
    require_strong_password,
)
from cloud_agent.domain.accounts import IdentifierTaken, SelfDeleteRefused

if TYPE_CHECKING:
    from cloud_agent.application.accounts import LocalAccountFlows
    from cloud_agent.application.phone_accounts import PhoneFlows
    from cloud_agent.domain.accounts import AccountRecord


class WrongPassword(Exception):
    """The current password given to confirm a change is wrong."""


class OwnerProofRequired(Exception):
    """The change needs a proof the request did not give, or the account has none."""


class OwnerProofUnavailable(Exception):
    """The account's only proof is a phone code, and phone sign-in is off."""


class SamePassword(ValueError):
    """The new password is the current one."""


class AccountChanged(Exception):
    """The account changed while the change was being made; nothing was written."""


class ConfirmationMismatch(ValueError):
    """The typed confirmation is not the account's identifier."""


@dataclass(frozen=True)
class EmailBindRequest:
    """What asking to bind an address did."""

    email: str
    already_bound: bool


async def confirm_owner(
    flows: LocalAccountFlows,
    phone: PhoneFlows | None,
    account: AccountRecord,
    *,
    password: str | None,
    phone_code: str | None,
    purpose: str,
) -> str | None:
    """Prove the owner is present; the password hash checked, or ``None`` for a code.

    Raises:
        WrongPassword: The account has a password and this is not it.
        OwnerProofRequired: No proof given, or the account has none.
        OwnerProofUnavailable: Only a phone code would do, and phone is off.
        InvalidPhoneCode: The code does not verify.
    """
    stored = await flows.accounts.stored_password_hash(account.id)
    if stored is not None:
        if not password or not await verify_password(password, stored):
            raise WrongPassword
        return stored
    if account.phone:
        if phone is None or not phone.enabled:
            raise OwnerProofUnavailable
        if not phone_code:
            raise OwnerProofRequired("Enter the verification code sent to your phone.")
        await phone.verify(account.phone, purpose, account.id, phone_code)
        return None
    raise OwnerProofRequired(
        "This account has no password or verified phone to confirm this with. "
        "Please contact the administrator."
    )


async def change_password(
    flows: LocalAccountFlows,
    phone: PhoneFlows | None,
    account: AccountRecord,
    *,
    session_id: str | None,
    old_password: str | None,
    phone_code: str | None,
    new_password: str,
) -> bool:
    """Change the password, or set a first one; every other session ends.

    Returns whether it was a first password.

    Raises:
        WeakPassword, SamePassword: The new password is refused.
        AccountChanged: The password changed meanwhile.
    """
    require_strong_password(new_password)
    expected = await confirm_owner(
        flows,
        phone,
        account,
        password=old_password,
        phone_code=phone_code,
        purpose="set_password",
    )
    if expected is not None and old_password == new_password:
        raise SamePassword("New password must be different from current password.")
    new_hash = await hash_password(new_password)
    if not await flows.accounts.change_password(
        account_id=account.id,
        expected_hash=expected,
        new_hash=new_hash,
        keep_session_id=session_id,
    ):
        raise AccountChanged
    if account.email and account.email_verified:
        flows.send(
            AccountMail(
                kind="password_changed",
                to=account.email,
                details={"first_password": "true" if expected is None else "false"},
            )
        )
    return expected is None


async def request_email_bind(
    flows: LocalAccountFlows,
    phone: PhoneFlows | None,
    account: AccountRecord,
    *,
    email: str,
    password: str | None,
    phone_code: str | None,
) -> EmailBindRequest:
    """Mail a link that makes ``email`` the account's address once followed.

    The current address stays until then. Whether the new one is taken is
    told only after the owner is confirmed: a stolen session must not be able
    to ask.

    Raises:
        InvalidEmail, BlockedEmailDomain: The address is refused.
        IdentifierTaken: Another account signs in with it.
    """
    display, normalized = normalize_email(email)
    if is_email_domain_blocked(normalized):
        raise BlockedEmailDomain("This email domain is not allowed.")
    if (account.email or "").lower() == normalized and account.email_verified:
        return EmailBindRequest(email=display, already_bound=True)
    await confirm_owner(
        flows,
        phone,
        account,
        password=password,
        phone_code=phone_code,
        purpose="bind_email",
    )
    owner = await flows.accounts.identifier_owner("email", normalized)
    if owner is not None and owner != account.id:
        raise IdentifierTaken
    await flows.mail_token(account.id, display, "bind_email", pending_normalized=normalized)
    return EmailBindRequest(email=display, already_bound=False)


async def delete_own_account(
    flows: LocalAccountFlows,
    phone: PhoneFlows | None,
    account: AccountRecord,
    *,
    confirm: str,
    password: str | None,
    phone_code: str | None,
    admin_ip: str,
) -> AccountRecord:
    """Delete the account at its owner's request, after the owner is confirmed.

    The owner types the account's identifier, then proves it is theirs.
    An administrator cannot delete itself.

    Raises:
        SelfDeleteRefused: An administrator.
        ConfirmationMismatch: The typed identifier is not the account's.
        AccountVersionConflict: The account changed while it was confirmed.
    """
    if account.is_admin:
        raise SelfDeleteRefused(
            "An administrator account cannot delete itself. Ask another administrator."
        )
    if confirm.strip().lower() != account.label.lower():
        raise ConfirmationMismatch("The confirmation does not match this account.")
    await confirm_owner(
        flows,
        phone,
        account,
        password=password,
        phone_code=phone_code,
        purpose="delete_account",
    )
    deleted = await flows.accounts.delete_own_account(
        account_id=account.id, expected_version=account.version, admin_ip=admin_ip
    )
    if account.email and account.email_verified:
        flows.send(AccountMail(kind="account_deleted", to=account.email))
    return deleted


__all__ = [
    "AccountChanged",
    "ConfirmationMismatch",
    "EmailBindRequest",
    "OwnerProofRequired",
    "OwnerProofUnavailable",
    "SamePassword",
    "WrongPassword",
    "change_password",
    "confirm_owner",
    "delete_own_account",
    "request_email_bind",
]
