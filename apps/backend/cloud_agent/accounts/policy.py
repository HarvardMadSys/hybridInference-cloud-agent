"""Sign-up policy: who may register, and who is approved without waiting.

Adapted from HybridInference ``auth/signup_policy.py`` and
``utils/email_blocklist.py`` at ``8b49872d1dcbed5c4bc8132b026b6740c3437aa7``
(MIT License, Copyright (c) 2026 Harvard SEAS). The switches are this
service's administrator settings (:mod:`cloud_agent.accounts.settings_registry`),
and the allowlist lives in ``agent_local_signup_domains``.

Approval differs in one way that keeps this service's waiting list: the
``manual`` policy (the default) approves nobody automatically. Under the
``domains`` policy HybridInference's rule applies exactly: an empty list
approves every account, otherwise only an email whose domain is listed
(exactly, or as a subdomain of a ``*.`` entry). Approval happens once the
account's identity is verified, never before, and a phone account has no
domain, so it is approved automatically only while the list is empty.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cloud_agent.settings import boolean_setting, text_setting

if TYPE_CHECKING:
    from cloud_agent.ports.accounts import LocalAccountRepository

#: Disposable and reserved mail domains HybridInference refuses at sign-up.
BLOCKED_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "test.com",
        "test.org",
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "tempmail.com",
        "tempmail.net",
        "10minutemail.com",
        "yopmail.com",
        "trashmail.com",
        "throwawaymail.com",
        "fakeinbox.com",
        "getairmail.com",
        "dispostable.com",
        "maildrop.cc",
    }
)

#: RFC 2606 reserved top-level domains, which never receive real mail.
_RESERVED_TLDS: tuple[str, ...] = (".test", ".example", ".invalid", ".localhost")

_DOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")

#: The audit actor of an approval the policy made.
POLICY_ACTOR = "signup-policy"


def email_domain(email: str) -> str:
    """The domain of a normalized address, lowercased; empty when there is none."""
    return email.rsplit("@", 1)[1].strip().lower() if "@" in email else ""


def is_email_domain_blocked(email: str) -> bool:
    """Whether an address uses a disposable or reserved domain."""
    domain = email_domain(email)
    if not domain:
        return False
    if domain in BLOCKED_EMAIL_DOMAINS:
        return True
    return any(domain == tld[1:] or domain.endswith(tld) for tld in _RESERVED_TLDS)


def parse_signup_domain(raw: str) -> tuple[str, bool]:
    """``(domain, is_wildcard)`` for an administrator's entry, or :class:`ValueError`.

    ``*.example.edu`` is a wildcard matching subdomains only; anything else is
    an exact domain. Stored lowercased, without the ``*.``.
    """
    value = raw.strip().lower().rstrip(".")
    wildcard = value.startswith("*.")
    if wildcard:
        value = value[2:]
    if "*" in value or not _DOMAIN.fullmatch(value) or len(value) > 253:
        raise ValueError("Enter a domain such as example.edu or *.example.edu.")
    return value, wildcard


def require_email_verification() -> bool:
    """Whether a new email account must prove its address before it is verified."""
    return boolean_setting("accounts.require_email_verification")


def approval_policy() -> str:
    """``manual`` or ``domains``."""
    value = text_setting("accounts.approval_policy")
    return value if value in ("manual", "domains") else "manual"


def admin_notify_enabled() -> bool:
    """Whether administrators are mailed about an application waiting for approval."""
    return boolean_setting("accounts.signup_admin_notify")


def notify_recipients() -> list[str]:
    """The configured new-application recipients; empty means every administrator."""
    raw = text_setting("accounts.signup_notify_emails")
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def admin_emails() -> set[str]:
    """The configured administrator addresses, lowercased (``accounts.admin_emails``)."""
    raw = text_setting("accounts.admin_emails")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def terms_url() -> str:
    """Where the Terms of Service are published, or empty."""
    return text_setting("accounts.terms_url").strip()


async def approves_automatically(accounts: LocalAccountRepository, email: str | None) -> bool:
    """Whether the policy approves an account with this email (or none) once verified."""
    if approval_policy() != "domains":
        return False
    empty, allowed = await accounts.signup_domain_policy(email_domain(email or ""))
    return empty or (bool(email) and allowed)


__all__ = [
    "BLOCKED_EMAIL_DOMAINS",
    "POLICY_ACTOR",
    "admin_emails",
    "admin_notify_enabled",
    "approval_policy",
    "approves_automatically",
    "email_domain",
    "is_email_domain_blocked",
    "notify_recipients",
    "parse_signup_domain",
    "require_email_verification",
    "terms_url",
]
