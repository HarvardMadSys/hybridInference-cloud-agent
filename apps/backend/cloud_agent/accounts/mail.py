"""Account email: verification, password reset and the existing-account notice.

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
- Settings are this service's own ``AGENT_SMTP_*``; nothing reads Gateway
  configuration. Delivery runs on a worker thread, after the response.
- Neither a recipient address nor a link is ever logged.
"""

from __future__ import annotations

import asyncio
import html
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Literal, Protocol

from cloud_agent.logging_utils import get_logger

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

MailKind = Literal["verify_email", "reset_password", "account_exists"]


@dataclass(frozen=True)
class AccountMail:
    """One message to one recipient. ``link`` holds a credential: never log it."""

    kind: MailKind
    to: str = field(repr=False)
    link: str = field(repr=False)


class AccountMailer(Protocol):
    """Delivers account mail; tests substitute a recorder."""

    @property
    def configured(self) -> bool:
        """Whether this deployment can send mail at all."""
        ...

    async def send(self, mail: AccountMail) -> bool:
        """Deliver one message; ``False`` on failure, never raising."""
        ...


def render(mail: AccountMail) -> tuple[str, str, str]:
    """Return ``(subject, text, html)`` for one message."""
    link = mail.link
    if mail.kind == "verify_email":
        subject = f"Verify your {SITE_NAME} account"
        lines = [
            f"Welcome to {SITE_NAME}!",
            "Please verify your email address by visiting:",
            link,
            "This link will expire in 24 hours.",
            "If you didn't create an account, you can safely ignore this email.",
        ]
    elif mail.kind == "reset_password":
        subject = f"Reset your {SITE_NAME} password"
        lines = [
            "Someone asked to reset the password for this email address.",
            "To choose a new password, visit:",
            link,
            "This link will expire in 1 hour and signs out every existing session.",
            "If you didn't ask for this, you can safely ignore this email.",
        ]
    else:
        subject = f"Your {SITE_NAME} account"
        lines = [
            f"Someone tried to sign up for {SITE_NAME} with this email address, "
            "which already has an account.",
            "If that was you, you can choose a new password here:",
            link,
            "This link will expire in 1 hour.",
            "If it wasn't you, you can safely ignore this email.",
        ]
    text = "\n\n".join(lines) + "\n"
    paragraphs = "".join(
        f'<p><a href="{html.escape(line, quote=True)}">{html.escape(line)}</a></p>'
        if line == link
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
        port = int(_env(ENV_SMTP_PORT) or (465 if security == "tls" else 587))
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
                smtp.login(username, os.environ.get(ENV_SMTP_PASSWORD) or "")
            smtp.send_message(message)


def _security() -> str | None:
    value = (_env(ENV_SMTP_SECURITY) or "starttls").lower()
    return value if value in SMTP_SECURITY_MODES else None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


__all__ = [
    "AccountMail",
    "AccountMailer",
    "MailKind",
    "SmtpAccountMailer",
    "render",
]
