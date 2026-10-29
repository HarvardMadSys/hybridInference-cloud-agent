"""Which transport delivers account mail: SMTP, Tencent SES, or a registered one.

Adapted from HybridInference ``utils/delivery.py`` and ``utils/email.py``
``send_email`` at ``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License,
Copyright (c) 2026 Harvard SEAS). ``accounts.email_provider`` names the
transport, read on every message so an administrator's change applies at
once; ``smtp`` is built in and the default, ``tencent_ses`` is the Tencent
Cloud template transport, and any other name is one registered with
:func:`register_email_provider`. There is no fallback between transports.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloud_agent.accounts.mail import SmtpAccountMailer
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import text_setting

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.accounts.mail import AccountMail

logger = get_logger(__name__)

_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)


@dataclass(frozen=True)
class _Registered:
    configured: Callable[[], bool]
    send: Callable[[AccountMail], bool]


_providers: dict[str, _Registered] = {}


def register_email_provider(
    name: str, *, configured: Callable[[], bool], send: Callable[[AccountMail], bool]
) -> None:
    """Register a synchronous transport under its own name, without replacing another.

    Raises:
        ValueError: An invalid or reserved name, or one already registered.
    """
    if not _PROVIDER_NAME.fullmatch(name) or name in {"smtp", "tencent_ses"}:
        raise ValueError(f"Invalid or reserved email provider name: {name!r}")
    if name in _providers:
        raise ValueError(f"Email provider already registered: {name!r}")
    _providers[name] = _Registered(configured=configured, send=send)


def _provider() -> str:
    return text_setting("accounts.email_provider").strip() or "smtp"


def delivery_refusal(mail: AccountMail) -> str | None:
    """Why the selected transport would refuse this message outright, or ``None``.

    Only Tencent SES refuses by shape (its approved templates and their size
    bound); SMTP and registered transports take any message.
    """
    if _provider() != "tencent_ses":
        return None
    from cloud_agent.accounts.ses_tencent import template_refusal

    return template_refusal(mail)


class DeliveryMailer:
    """The account mailer the settings select, chosen again for every message."""

    @property
    def configured(self) -> bool:
        """Whether the selected transport has everything it needs."""
        name = _provider()
        if name == "smtp":
            return SmtpAccountMailer().configured
        if name == "tencent_ses":
            from cloud_agent.accounts.ses_tencent import TencentSesMailer

            return TencentSesMailer().configured
        provider = _providers.get(name)
        return bool(provider is not None and provider.configured())

    async def send(self, mail: AccountMail) -> bool:
        """Deliver one message through the selected transport; ``False`` on failure."""
        name = _provider()
        if name == "smtp":
            return await SmtpAccountMailer().send(mail)
        sender: Callable[[AccountMail], bool]
        if name == "tencent_ses":
            from cloud_agent.accounts.ses_tencent import TencentSesMailer

            sender = TencentSesMailer().send
        else:
            provider = _providers.get(name)
            if provider is None:
                return False
            sender = provider.send
        try:
            return bool(await asyncio.to_thread(sender, mail))
        except Exception as exc:
            logger.warning(
                "agent_account_mail_failed kind=%s cause=%s",
                mail.kind,
                type(exc).__name__,
                extra={"event": "agent_account_mail_failed", "kind": mail.kind},
            )
            return False


__all__ = ["DeliveryMailer", "delivery_refusal", "register_email_provider"]
