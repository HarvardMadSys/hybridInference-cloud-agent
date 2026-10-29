"""A small TC3-signed Tencent Cloud API client, shared by SMS and SES delivery.

Adapted from HybridInference ``integrations/tencent_cloud/api.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS). One signed request per call and never a retry, since a send is
paid. Provider messages and request bodies can carry personal data or codes,
so a failure exposes only a bounded error code. Credentials are this
service's own ``accounts.tencent_*`` settings, read on every call.
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

from cloud_agent.settings import text_setting

_SAFE_CODE = re.compile(r"[A-Za-z0-9_.]{1,128}")


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


def credentials_configured() -> bool:
    """Whether the CAM credential pair is set."""
    return bool(
        text_setting("accounts.tencent_secret_id").strip()
        and text_setting("accounts.tencent_secret_key").strip()
    )


def call_tencent_cloud(
    service: str,
    action: str,
    version: str,
    region: str,
    payload: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Submit one signed request; failures expose only a bounded error code.

    Raises:
        TencentCloudError: Missing credentials, a transport failure, or the
            provider's error, by its code alone.
    """
    if not credentials_configured():
        raise TencentCloudError("CredentialsMissing")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = signed_headers(
        service=service,
        action=action,
        version=version,
        region=region,
        body=body,
        secret_id=text_setting("accounts.tencent_secret_id").strip(),
        secret_key=text_setting("accounts.tencent_secret_key").strip(),
        token=text_setting("accounts.tencent_token").strip(),
    )
    try:
        with httpx.Client(transport=transport, timeout=10) as client:
            response = client.post(
                f"https://{service}.tencentcloudapi.com/", content=body, headers=headers
            )
        response.raise_for_status()
        result = response.json().get("Response")
        if not isinstance(result, dict):
            raise TencentCloudError("InvalidResponse")
        if error := result.get("Error"):
            code = error.get("Code", "ProviderError") if isinstance(error, dict) else None
            raise TencentCloudError(
                code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "ProviderError"
            )
        return result
    except TencentCloudError:
        raise
    except (httpx.HTTPError, ValueError, AttributeError) as error:
        raise TencentCloudError("TransportError") from error


__all__ = ["TencentCloudError", "call_tencent_cloud", "credentials_configured", "signed_headers"]
