"""Tencent Cloud SES delivery of account mail through approved templates.

Adapted from HybridInference ``integrations/tencent_cloud/ses.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS). SES sends only templates its operator had approved, so each
kind of account mail maps to one template and passes scalar variables, HTML
escaped, never markup of its own; anything without a dedicated template goes
through the plain ``notice`` template with its text body. Selected with
``accounts.email_provider = tencent_ses``; credentials and template ids are
this service's own settings.
"""

from __future__ import annotations

import html
import json
import re
from typing import TYPE_CHECKING

from cloud_agent.accounts.mail import SITE_NAME, render
from cloud_agent.accounts.tencent_cloud import (
    TencentCloudError,
    call_tencent_cloud,
    credentials_configured,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import integer_setting, text_setting

if TYPE_CHECKING:
    import httpx

    from cloud_agent.accounts.mail import AccountMail

logger = get_logger(__name__)

#: Tencent SES documents this bound on the serialized template variables.
TEMPLATE_DATA_LIMIT_BYTES = 800
_SENDER = re.compile(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+")

#: The approved template each kind of mail is sent through.
TEMPLATE_FOR_KIND = {
    "verify_email": "verification",
    "bind_email": "verification",
    "reset_password": "password_reset",
    "account_exists": "password_reset",
    "approved": "approval",
    "rejected": "rejection",
    "new_application": "admin_signup",
}


def _template_id(template: str) -> int:
    return integer_setting(f"accounts.tencent_ses_template_{template}")


def _data(mail: AccountMail, template: str) -> dict[str, str]:
    if template == "notice":
        return {"site_name": SITE_NAME, "body": render(mail)[1]}
    data = {"site_name": SITE_NAME, "link": mail.link}
    data.update({key: value for key, value in mail.details.items() if key != "html"})
    return data


def _template_data(mail: AccountMail, template: str) -> str:
    return json.dumps(
        {key: html.escape(value, quote=True) for key, value in _data(mail, template).items()},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def template_refusal(mail: AccountMail) -> str | None:
    """Why SES would refuse this message before sending it, or ``None``.

    SES sends only approved templates: a message of a kind without its own
    goes through the ``notice`` template with its whole text as one
    variable, and Tencent bounds the variables at
    :data:`TEMPLATE_DATA_LIMIT_BYTES`. Administrator mail is told this when
    it is previewed and created, not by every recipient failing later.
    """
    template = TEMPLATE_FOR_KIND.get(mail.kind, "notice")
    if not _template_id(template):
        return (
            f"Tencent SES has no approved {template} template configured "
            f"(accounts.tencent_ses_template_{template}) to send this message through."
        )
    size = len(_template_data(mail, template).encode("utf-8"))
    if size > TEMPLATE_DATA_LIMIT_BYTES:
        return (
            f"Tencent SES sends this message through its {template} template, whose variables "
            f"are limited to {TEMPLATE_DATA_LIMIT_BYTES} bytes; this one needs {size}. Shorten "
            "it, or deliver account mail by SMTP (Admin → Settings → Accounts → Email "
            "transport)."
        )
    return None


class TencentSesMailer:
    """``SendEmail`` with one approved template per kind of account mail."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        """Use the default transport, or a test's mock."""
        self._transport = transport

    @property
    def configured(self) -> bool:
        """Credentials, an approved sender and the verification and reset templates."""
        return bool(
            credentials_configured()
            and _SENDER.fullmatch(text_setting("accounts.tencent_ses_from_email").strip())
            and _template_id("verification")
            and _template_id("password_reset")
        )

    def send(self, mail: AccountMail) -> bool:
        """Deliver one message; whether SES accepted it. Never raises."""
        template = TEMPLATE_FOR_KIND.get(mail.kind, "notice")
        template_id = _template_id(template)
        if not self.configured or not template_id:
            logger.warning(
                "agent_ses_template_unavailable template=%s",
                template,
                extra={"event": "agent_ses_template_unavailable"},
            )
            return False
        data = _template_data(mail, template)
        name = text_setting("accounts.tencent_ses_from_name").strip()
        if len(data.encode("utf-8")) > TEMPLATE_DATA_LIMIT_BYTES or any(
            char in name for char in "\r\n:<>"
        ):
            return False
        sender = text_setting("accounts.tencent_ses_from_email").strip()
        try:
            result = call_tencent_cloud(
                "ses",
                "SendEmail",
                "2020-10-02",
                text_setting("accounts.tencent_ses_region").strip(),
                {
                    "FromEmailAddress": f"{name} <{sender}>" if name else sender,
                    "Destination": [mail.to],
                    "Subject": render(mail)[0],
                    "Template": {"TemplateID": template_id, "TemplateData": data},
                    "TriggerType": 1,
                    "Unsubscribe": "0",
                },
                transport=self._transport,
            )
        except TencentCloudError as error:
            logger.warning(
                "agent_ses_send_failed code=%s", error, extra={"event": "agent_ses_send_failed"}
            )
            return False
        return bool(result.get("MessageId"))


__all__ = ["TEMPLATE_FOR_KIND", "TencentSesMailer", "template_refusal"]
