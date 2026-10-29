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

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import integer_setting, text_setting

logger = get_logger(__name__)

_MAINLAND_MOBILE = re.compile(r"\+861[3-9][0-9]{9}")
_CODE = re.compile(r"[0-9]{6}")
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.]{1,128}")
_SERVICE = "sms"
_ACTION = "SendSms"
_VERSION = "2021-01-11"


class TencentCloudError(RuntimeError):
    """A delivery failure carrying only a safe provider error code."""


def signed_headers(
    *,
    service: str,
    action: str,
    version: str,
    region: str,
    body: bytes,
    secret_id: str,
    secret_key: str,
    token: str = "",
    timestamp: int | None = None,
) -> dict[str, str]:
    """Sign the exact JSON bytes with Tencent Cloud's TC3-HMAC-SHA256."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    host = f"{service}.tencentcloudapi.com"
    content_type = "application/json; charset=utf-8"
    signed = "content-type;host"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed}\n{hashlib.sha256(body).hexdigest()}"
    )
    scope = f"{date}/{service}/tc3_request"
    to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    date_key = hmac.new(f"TC3{secret_key}".encode(), date.encode(), hashlib.sha256).digest()
    service_key = hmac.new(date_key, service.encode(), hashlib.sha256).digest()
    request_key = hmac.new(service_key, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(request_key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Region": region,
        "X-TC-Timestamp": str(timestamp),
    }
    if token:
        headers["X-TC-Token"] = token
    return headers


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
            result = self._call(
                {
                    "PhoneNumberSet": [phone],
                    "SmsSdkAppId": _setting("accounts.tencent_sms_app_id"),
                    "SignName": _setting("accounts.tencent_sms_sign_name"),
                    "TemplateId": _setting("accounts.tencent_sms_template_id"),
                    "TemplateParamSet": [code, str(minutes)],
                }
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

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit one signed request; failures expose only a bounded error code."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = signed_headers(
            service=_SERVICE,
            action=_ACTION,
            version=_VERSION,
            region=_setting("accounts.tencent_sms_region"),
            body=body,
            secret_id=_setting("accounts.tencent_secret_id"),
            secret_key=_setting("accounts.tencent_secret_key"),
            token=_setting("accounts.tencent_token"),
        )
        try:
            with httpx.Client(transport=self._transport, timeout=10) as client:
                response = client.post(
                    f"https://{_SERVICE}.tencentcloudapi.com/", content=body, headers=headers
                )
            response.raise_for_status()
            result = response.json().get("Response")
            if not isinstance(result, dict):
                raise TencentCloudError("InvalidResponse")
            if error := result.get("Error"):
                code = error.get("Code", "ProviderError") if isinstance(error, dict) else None
                raise TencentCloudError(
                    code
                    if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
                    else "ProviderError"
                )
            return result
        except TencentCloudError:
            raise
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            raise TencentCloudError("TransportError") from error


__all__ = ["TencentCloudError", "TencentSms", "signed_headers"]
