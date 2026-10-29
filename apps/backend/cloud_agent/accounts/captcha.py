"""Cloudflare Turnstile verification for sign-up and phone codes.

Adapted from HybridInference ``utils/turnstile.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS). With no secret key configured (``accounts.turnstile_secret_key``)
nothing is checked, as there; once one is, a missing or rejected token, a
network error or a non-200 answer refuses, so an outage cannot let unverified
sign-ups through. The control plane makes this call, never a sandbox.
"""

from __future__ import annotations

import httpx

from cloud_agent.logging_utils import get_logger
from cloud_agent.settings import text_setting

logger = get_logger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def site_key() -> str:
    """The public key the forms render the widget with; empty when captcha is off."""
    return text_setting("accounts.turnstile_site_key").strip()


def captcha_enabled() -> bool:
    """Whether tokens are checked."""
    return bool(text_setting("accounts.turnstile_secret_key").strip())


async def verify_captcha(
    token: str | None,
    remote_ip: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Whether a Turnstile token is valid, or captcha is not configured."""
    secret = text_setting("accounts.turnstile_secret_key").strip()
    if not secret:
        return True
    if not token:
        return False
    data = {"secret": secret, "response": token}
    if remote_ip and remote_ip != "unknown":
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            response = await client.post(VERIFY_URL, data=data)
    except httpx.HTTPError as exc:
        logger.warning(
            "agent_captcha_unavailable cause=%s",
            type(exc).__name__,
            extra={"event": "agent_captcha_unavailable"},
        )
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return bool(isinstance(payload, dict) and payload.get("success"))


__all__ = ["VERIFY_URL", "captcha_enabled", "site_key", "verify_captcha"]
