"""Tencent Cloud SMS delivery of verification codes.

Adapted from HybridInference ``integrations/tencent_cloud/sms.py`` and
``api.py`` at ``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License,
Copyright (c) 2026 Harvard SEAS): the same TC3-HMAC-SHA256 signing, the same
``SendSms`` request and the same acceptance check. Its credentials are this
service's own administrator settings (``accounts.tencent_*``), read on every
send so a rotation applies at once; nothing reads Gateway configuration.

Only mainland China mobile numbers are accepted, as the approved templates
are; one request is never retried, since a send is paid.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cloud_agent.accounts.tencent_cloud import (
    TencentCloudError,
    call_tencent_cloud,
    signed_headers,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import integer_setting, text_setting

if TYPE_CHECKING:
    import httpx

logger = get_logger(__name__)

_MAINLAND_MOBILE = re.compile(r"\+861[3-9][0-9]{9}")
_CODE = re.compile(r"[0-9]{6}")


def _setting(key: str) -> str:
    return text_setting(key).strip()


class TencentSms:
    """``SendSms`` with the approved code-and-minutes template."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        """Use the default transport, or a test's mock."""
        self._transport = transport

    @property
    def configured(self) -> bool:
        """Credentials, application, signature, template and region are all set."""
        return all(
            _setting(key)
            for key in (
                "accounts.tencent_secret_id",
                "accounts.tencent_secret_key",
                "accounts.tencent_sms_app_id",
                "accounts.tencent_sms_sign_name",
                "accounts.tencent_sms_template_id",
                "accounts.tencent_sms_region",
            )
        )

    def send_code(self, phone: str, code: str) -> bool:
        """Send one code; whether exactly this number was accepted."""
        if (
            not self.configured
            or not _MAINLAND_MOBILE.fullmatch(phone)
            or not _CODE.fullmatch(code)
        ):
            return False
        minutes = (integer_setting("accounts.sms_code_ttl_s") + 59) // 60
        try:
            result = call_tencent_cloud(
                "sms",
                "SendSms",
                "2021-01-11",
                _setting("accounts.tencent_sms_region"),
                {
                    "PhoneNumberSet": [phone],
                    "SmsSdkAppId": _setting("accounts.tencent_sms_app_id"),
                    "SignName": _setting("accounts.tencent_sms_sign_name"),
                    "TemplateId": _setting("accounts.tencent_sms_template_id"),
                    "TemplateParamSet": [code, str(minutes)],
                },
                transport=self._transport,
            )
        except TencentCloudError as error:
            logger.warning(
                "agent_sms_send_failed provider=tencent code=%s",
                error,
                extra={"event": "agent_sms_send_failed", "provider": "tencent"},
            )
            return False
        statuses = result.get("SendStatusSet")
        accepted = (
            isinstance(statuses, list)
            and len(statuses) == 1
            and isinstance(statuses[0], dict)
            and statuses[0].get("Code") == "Ok"
            and statuses[0].get("PhoneNumber") == phone
        )
        if not accepted:
            logger.warning(
                "agent_sms_not_accepted provider=tencent",
                extra={"event": "agent_sms_not_accepted", "provider": "tencent"},
            )
        return accepted


__all__ = ["TencentCloudError", "TencentSms", "signed_headers"]
