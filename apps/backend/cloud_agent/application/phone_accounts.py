"""Phone sign-up, sign-in and binding with one-time codes.

Adapted from HybridInference ``phone_auth_routes.py`` and ``utils/phone_auth.py``
at ``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c)
2026 Harvard SEAS), as it actually behaves:

- A code is bound to a purpose (``signup``, ``login``, ``bind``, and the owner
  confirmations ``delete_account``, ``bind_email``, ``set_password``), to the
  number and to the account it concerns. It is reserved before the message is
  sent and usable only once the provider accepted it.
- Asking for a code never says whether the number has an account: a code is
  sent only where it can be used (a login code to a registered number, a
  sign-up code to a free one, an owner code to the account's own number),
  and every request answers the same.
- Binding adds a number to an account without one. HybridInference has no
  way to remove or replace a bound number, and neither has this.
- A phone-only account has no password; it signs in with a code, and gives a
  ``set_password`` code to choose a first one.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import TYPE_CHECKING

from cloud_agent.accounts import tokens
from cloud_agent.accounts.config import local_session_ttl_s
from cloud_agent.accounts.sms import sms_enabled
from cloud_agent.application.accounts import (
    InvalidCredentials,
    SignedIn,
    SignUpOutcome,
    refuse_unusable_account,
)
from cloud_agent.domain.accounts import IdentifierTaken
from cloud_agent.settings import integer_setting, text_setting

if TYPE_CHECKING:
    from cloud_agent.accounts.sms import SmsSender
    from cloud_agent.application.accounts import LocalAccountFlows

PHONE_PURPOSES = ("signup", "login", "bind", "delete_account", "bind_email", "set_password")
#: Purposes that confirm a signed-in owner; their codes go only to its own number.
OWNER_PURPOSES = frozenset({"delete_account", "bind_email", "set_password"})
#: Purposes that need a signed-in account.
ACCOUNT_PURPOSES = OWNER_PURPOSES | {"bind"}

_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
_CODE = re.compile(r"[0-9]{6}")
_MAINLAND = re.compile(r"\+861[3-9][0-9]{9}")


class InvalidPhone(ValueError):
    """Not a number this deployment sends codes to; the message says why."""


class PhoneUnavailable(Exception):
    """Phone sign-in is off or has no configured provider."""


class PhoneCooldown(Exception):
    """Too many codes asked for; ``retry_after`` seconds to wait."""

    def __init__(self, retry_after: int) -> None:
        """Remember the wait."""
        super().__init__("Too many code requests. Please try again later.")
        self.retry_after = retry_after


class PhoneSendFailed(Exception):
    """The provider did not accept the message."""


class InvalidPhoneCode(Exception):
    """The code is wrong, used, expired, out of attempts or for something else."""


def normalize_phone(raw: str) -> str:
    """The canonical number: E.164, with the default country code, within the prefixes.

    Raises:
        InvalidPhone: Not E.164, or outside the deployment's allowed prefixes.
    """
    value = re.sub(r"[\s()-]", "", raw.strip())
    default = text_setting("accounts.sms_default_country_code").strip()
    if not value.startswith("+") and default:
        value = "+" + default + value
    if not _E164.fullmatch(value):
        raise InvalidPhone("Enter a phone number with its country code, such as +12025550123.")
    prefixes = [
        item.strip()
        for item in text_setting("accounts.sms_allowed_prefixes").split(",")
        if item.strip()
    ]
    if prefixes and not any(value.startswith(prefix) for prefix in prefixes):
        raise InvalidPhone("Phone numbers in this region are not enabled for this deployment.")
    if text_setting("accounts.sms_provider") == "tencent" and not _MAINLAND.fullmatch(value):
        # The Tencent templates send to mainland China mobile numbers only.
        raise InvalidPhone("Phone numbers in this region are not enabled for this deployment.")
    return value


def valid_code(code: str | None) -> bool:
    """Whether a value has the six-digit shape of a code."""
    return bool(code) and _CODE.fullmatch(code or "") is not None


class PhoneFlows:
    """Codes and phone accounts, over the account flows and one provider."""

    def __init__(self, flows: LocalAccountFlows, sender: SmsSender | None) -> None:
        """Bind the account flows and the selected provider."""
        self._flows = flows
        self._accounts = flows.accounts
        self._sender = sender

    @property
    def account_flows(self) -> LocalAccountFlows:
        """The account flows the phone flows run over."""
        return self._flows

    @property
    def enabled(self) -> bool:
        """Whether phone sign-in is on and can deliver."""
        return sms_enabled(self._sender)

    def require_enabled(self) -> None:
        """Raise :class:`PhoneUnavailable` unless phone sign-in can deliver."""
        if not self.enabled:
            raise PhoneUnavailable

    async def send_code(self, phone: str, purpose: str, account_id: str | None) -> None:
        """Reserve, send and activate a code where it can be used; silently drop it elsewhere.

        Raises:
            PhoneCooldown: The number is cooling down, or the account may not
                ask (deleted, or not active for an account purpose).
            PhoneSendFailed: The provider did not take an eligible message.
        """
        self.require_enabled()
        owner = await self._accounts.identifier_owner("phone", phone)
        code_account = owner if purpose == "login" else account_id
        if purpose == "login":
            eligible = owner is not None
        elif purpose == "signup":
            eligible = owner is None
        elif purpose in OWNER_PURPOSES:
            eligible = owner is not None and owner == account_id
        else:
            eligible = owner is None or owner == account_id
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = secrets.token_hex(16)
        retry = await self._accounts.reserve_phone_code(
            phone_hash=tokens.phone_hash(phone),
            purpose=purpose,
            account_id=code_account,
            challenge_id=challenge,
            code_hash=tokens.phone_code_hash(phone, purpose, code_account, code),
            expire_s=integer_setting("accounts.sms_code_ttl_s"),
            cooldown_s=integer_setting("accounts.sms_send_cooldown_s"),
        )
        if retry is None:
            raise PhoneCooldown(86400)
        if retry:
            raise PhoneCooldown(retry)
        if not eligible:
            await self._accounts.invalidate_phone_code(challenge)
            return
        assert self._sender is not None
        sent = await asyncio.to_thread(self._sender.send_code, phone, code)
        if not sent:
            await self._accounts.invalidate_phone_code(challenge)
            raise PhoneSendFailed
        if not await self._accounts.activate_phone_code(challenge):
            raise PhoneSendFailed

    async def verify(self, phone: str, purpose: str, account_id: str | None, code: str) -> None:
        """Spend one check of the code; :class:`InvalidPhoneCode` unless it matches."""
        if not valid_code(code) or not await self._accounts.consume_phone_code(
            phone_hash=tokens.phone_hash(phone),
            purpose=purpose,
            account_id=account_id,
            code_hash=tokens.phone_code_hash(phone, purpose, account_id, code),
            max_attempts=integer_setting("accounts.sms_max_attempts"),
        ):
            raise InvalidPhoneCode

    async def sign_up(
        self, phone: str, code: str, *, user_name: str | None, use_case: str | None
    ) -> SignUpOutcome:
        """Create a verified phone account from a sign-up code.

        A phone sign-up is its application, as in HybridInference. The
        policy approves it at once only while the domain list is empty.

        Raises:
            InvalidPhoneCode: The code does not verify.
            IdentifierTaken: The number signed up meanwhile.
        """
        self.require_enabled()
        await self.verify(phone, "signup", None, code)
        account_id = tokens.new_account_id()
        if not await self._accounts.create_phone_account(
            account_id=account_id,
            phone=phone,
            user_name=user_name,
            tos_accepted=True,
            purpose=(use_case or "").strip(),
        ):
            raise IdentifierTaken
        return await self._flows.after_verified_identity(account_id, applied=True)

    async def sign_in(self, phone: str, code: str) -> SignedIn:
        """Spend a login code and open a session.

        Raises:
            InvalidPhoneCode: The code does not verify.
            InvalidCredentials: No account has the number.
            AccountDeleted, AccountSuspended, EmailNotVerified: The account
                cannot sign in.
        """
        self.require_enabled()
        login = await self._accounts.find_login("phone", phone)
        await self.verify(phone, "login", login.account.id if login else None, code)
        if login is None:
            raise InvalidCredentials
        refuse_unusable_account(login.account)
        token = tokens.new_session_token()
        session_id = tokens.new_session_id()
        account = await self._accounts.create_session(
            session_id=session_id,
            account_id=login.account.id,
            token_hash=tokens.session_hash(token),
            ttl_s=local_session_ttl_s(),
            phone=phone,
        )
        if account is None:
            raise InvalidCredentials
        return SignedIn(
            token=token,
            account=await self._flows.promote_if_listed(account),
            session_id=session_id,
        )

    async def bind(self, account_id: str, phone: str, code: str) -> None:
        """Bind a number proven by a ``bind`` code.

        Raises:
            InvalidPhoneCode: The code does not verify.
            IdentifierTaken: Another account has the number, or this one
                already has a different one.
        """
        self.require_enabled()
        await self.verify(phone, "bind", account_id, code)
        if not await self._accounts.bind_phone(account_id=account_id, phone=phone):
            raise IdentifierTaken


__all__ = [
    "ACCOUNT_PURPOSES",
    "OWNER_PURPOSES",
    "PHONE_PURPOSES",
    "InvalidPhone",
    "InvalidPhoneCode",
    "PhoneCooldown",
    "PhoneFlows",
    "PhoneSendFailed",
    "PhoneUnavailable",
    "normalize_phone",
    "valid_code",
]
