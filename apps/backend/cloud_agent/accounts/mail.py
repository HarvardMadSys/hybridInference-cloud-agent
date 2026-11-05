"""Account email: links that prove a mailbox, and notices about the account.

The templates and the SMTP delivery are adapted from HybridInference
``apps/backend/serving/utils/email.py`` at
``321e9a6c457dcc80d35977aaac472d54041204a8`` (MIT License, Copyright (c) 2026
Harvard SEAS). What changed:

- Links point at the web app's ``/verify-email#token=...`` and
  ``/reset-password#token=...``. The token is in the fragment, which a browser
  never sends, so it stays out of every access log between the reader and the
  page. The page posts it to the API explicitly; opening the link changes
  nothing.
- The base address is the configured ``AGENT_WEB_BASE_URL`` only, never a
  request's ``Host``.
- Settings are this service's own (``Admin → Settings → Accounts``, imported
  from ``AGENT_SMTP_*``); nothing reads Gateway configuration. Delivery runs
  on a worker thread, after the response.
- Neither a recipient address nor a link is ever logged.

The notices follow HybridInference's: approval and rejection of an
application, a new application for administrators, and the security notices
for a changed password, a changed email address and a deleted account.
"""

from __future__ import annotations

import asyncio
import html
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import setting_env

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)

ENV_SMTP_HOST = "AGENT_SMTP_HOST"
ENV_SMTP_PORT = "AGENT_SMTP_PORT"
ENV_SMTP_USERNAME = "AGENT_SMTP_USERNAME"
ENV_SMTP_PASSWORD = "AGENT_SMTP_PASSWORD"
ENV_SMTP_FROM = "AGENT_SMTP_FROM"
#: ``starttls`` (default), ``tls`` for implicit TLS, or ``none`` for a local relay.
ENV_SMTP_SECURITY = "AGENT_SMTP_SECURITY"

#: The only accepted ``AGENT_SMTP_SECURITY`` values. Anything else, a typo
#: included, leaves mail unconfigured rather than falling back to plain SMTP.
SMTP_SECURITY_MODES = frozenset({"starttls", "tls", "none"})

SITE_NAME = "Cloud Agent"
_SMTP_TIMEOUT_S = 10.0

MailKind = Literal[
    "verify_email",
    "reset_password",
    "account_exists",
    "bind_email",
    "approved",
    "rejected",
    "new_application",
    "password_changed",
    "email_changed",
    "account_deleted",
    "account_reclaimed",
    "broadcast",
]

#: Kinds whose link is a credential: proof of the mailbox that receives it.
CREDENTIAL_KINDS = frozenset({"verify_email", "reset_password", "account_exists", "bind_email"})


@dataclass(frozen=True)
class AccountMail:
    """One message to one recipient. ``link`` may hold a credential: never log it.

    ``details`` carries the few values a notice shows (a rejection reason, the
    new address, an applicant's name), already chosen by the caller.
    """

    kind: MailKind
    to: str = field(repr=False)
    link: str = field(default="", repr=False)
    details: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Freeze the details with the message."""
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


class AccountMailer(Protocol):
    """Delivers account mail; tests substitute a recorder."""

    @property
    def configured(self) -> bool:
        """Whether this deployment can send mail at all."""
        ...

    async def send(self, mail: AccountMail) -> bool:
        """Deliver one message; ``False`` on failure, never raising."""
        ...


def _lines(mail: AccountMail) -> tuple[str, list[str]]:
    """The subject and the paragraphs of one message."""
    link, details = mail.link, mail.details
    if mail.kind == "verify_email":
        return f"Verify your {SITE_NAME} account", [
            f"Welcome to {SITE_NAME}!",
            "Please verify your email address by visiting:",
            link,
            "This link will expire in 24 hours.",
            "If you didn't create an account, you can safely ignore this email.",
        ]
    if mail.kind == "reset_password":
        return f"Reset your {SITE_NAME} password", [
            "Someone asked to reset the password for this email address.",
            "To choose a new password, visit:",
            link,
            "This link will expire in 1 hour and signs out every existing session.",
            "If you didn't ask for this, you can safely ignore this email.",
        ]
    if mail.kind == "account_exists":
        return f"Your {SITE_NAME} account", [
            f"Someone tried to sign up for {SITE_NAME} with this email address, "
            "which already has an account.",
            "If that was you, you can choose a new password here:",
            link,
            "This link will expire in 1 hour.",
            "If it wasn't you, you can safely ignore this email.",
        ]
    if mail.kind == "bind_email":
        return f"Confirm your new {SITE_NAME} email address", [
            f"Someone asked to use this address to sign in to a {SITE_NAME} account.",
            "To confirm it, visit:",
            link,
            "This link will expire in 24 hours. Until it is followed, the account keeps "
            "its current address.",
            "If you didn't ask for this, you can safely ignore this email.",
        ]
    return _notice_lines(mail.kind, link, details)


def _notice_lines(kind: str, link: str, details: Mapping[str, str]) -> tuple[str, list[str]]:
    if kind == "approved":
        return f"Your {SITE_NAME} account has been approved", [
            f"Your {SITE_NAME} application has been approved by an administrator.",
            "You can now sign in and start using Agent:",
            link,
            f"If you did not register for {SITE_NAME}, please ignore this email.",
        ]
    if kind == "rejected":
        lines = [f"Unfortunately, your {SITE_NAME} application was not approved at this time."]
        if details.get("reason"):
            lines.append(f"Reason: {details['reason']}")
        lines.append("If you believe this was a mistake, please contact the administrator.")
        return f"Your {SITE_NAME} application update", lines
    if kind == "new_application":
        lines = [
            f"A new {SITE_NAME} account is waiting for approval.",
            f"Account: {details.get('account', '')}",
        ]
        if details.get("user_name"):
            lines.append(f"Name: {details['user_name']}")
        if details.get("purpose"):
            lines.append(f"Use case: {details['purpose']}")
        lines += ["Review it here:", link]
        return f"New {SITE_NAME} application", lines
    if kind == "password_changed":
        verb = "set" if details.get("first_password") == "true" else "changed"
        return f"Your {SITE_NAME} password was {verb}", [
            f"The password of your {SITE_NAME} account was just {verb}. Other devices have "
            "been signed out.",
            "If this wasn't you, reset your password at once and contact the administrator.",
        ]
    if kind == "email_changed":
        return f"Your {SITE_NAME} email address was changed", [
            f"Your {SITE_NAME} account now signs in with {details.get('new_email', '')} instead "
            "of this address, which no longer receives its password resets.",
            "If this wasn't you, contact the administrator at once.",
        ]
    if kind == "account_reclaimed":
        return f"Your {SITE_NAME} account is yours again", [
            f"Your new password is set. This address joined {SITE_NAME} while email "
            "verification was off, so anyone could have signed it up; what was entered at "
            "sign-up (the name, the Terms acceptance, the language and the application text) "
            "has been cleared, and any session is signed out.",
            "If you were waiting for Agent access, apply again from the waiting list. Agent "
            "work already in the account stays; if it is not yours, contact the administrator.",
        ]
    return f"Your {SITE_NAME} account has been deleted", [
        f"Your {SITE_NAME} account was deleted at your request, and it can no longer sign in.",
        "If this wasn't you, contact the administrator at once.",
    ]


def render(mail: AccountMail) -> tuple[str, str, str]:
    """Return ``(subject, text, html)`` for one message.

    Administrator mail (``broadcast``) arrives rendered: its details carry the
    subject, the text and the HTML the administrator previewed.
    """
    if mail.kind == "broadcast":
        details = mail.details
        return details.get("subject", ""), details.get("text", ""), details.get("html", "")
    subject, lines = _lines(mail)
    link = mail.link
    text = "\n\n".join(lines) + "\n"
    paragraphs = "".join(
        f'<p><a href="{html.escape(line, quote=True)}">{html.escape(line)}</a></p>'
        if link and line == link
        else f"<p>{html.escape(line)}</p>"
        for line in lines
    )
    body = (
        '<html><body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">'
        f'<div style="max-width: 600px; margin: 0 auto; padding: 20px;">{paragraphs}</div>'
        "</body></html>"
    )
    return subject, text, body


class SmtpAccountMailer:
    """SMTP delivery from ``AGENT_SMTP_*``, read when each message is sent."""

    @property
    def configured(self) -> bool:
        """Host, sender and a known security mode; credentials are optional for a relay."""
        return bool(_env(ENV_SMTP_HOST) and _env(ENV_SMTP_FROM)) and _security() is not None

    async def send(self, mail: AccountMail) -> bool:
        """Deliver on a worker thread; log only the kind on failure."""
        if not self.configured:
            logger.warning(
                "agent_account_mail_unconfigured",
                extra={"event": "agent_account_mail_unconfigured", "kind": mail.kind},
            )
            return False
        try:
            await asyncio.to_thread(self._deliver, mail)
        except Exception as exc:
            logger.warning(
                "agent_account_mail_failed kind=%s cause=%s",
                mail.kind,
                type(exc).__name__,
                extra={
                    "event": "agent_account_mail_failed",
                    "kind": mail.kind,
                    "cause": type(exc).__name__,
                },
            )
            return False
        return True

    def _deliver(self, mail: AccountMail) -> None:
        subject, text, body = render(mail)
        message = EmailMessage()
        message["From"] = _env(ENV_SMTP_FROM)
        message["To"] = mail.to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(body, subtype="html")
        host = _env(ENV_SMTP_HOST)
        security = _security()
        if security is None:
            raise ValueError("AGENT_SMTP_SECURITY must be starttls, tls or none")
        port = int(_env(ENV_SMTP_PORT) or "0") or (465 if security == "tls" else 587)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if security == "tls":
            smtp = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_S, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_S)
        with smtp:
            if security == "starttls":
                smtp.starttls(context=context)
            username = _env(ENV_SMTP_USERNAME)
            if username:
                smtp.login(username, setting_env(ENV_SMTP_PASSWORD) or "")
            smtp.send_message(message)


def _security() -> str | None:
    value = (_env(ENV_SMTP_SECURITY) or "starttls").lower()
    return value if value in SMTP_SECURITY_MODES else None


def _env(name: str) -> str:
    return (setting_env(name) or "").strip()


__all__ = [
    "CREDENTIAL_KINDS",
    "AccountMail",
    "AccountMailer",
    "MailKind",
    "SmtpAccountMailer",
    "render",
]
