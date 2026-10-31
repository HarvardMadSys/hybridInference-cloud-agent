"""Text-message delivery of phone verification codes.

Adapted from HybridInference ``utils/sms.py`` and ``utils/delivery.py`` at
``8b49872d1dcbed5c4bc8132b026b6740c3437aa7`` (MIT License, Copyright (c) 2026
Harvard SEAS). Phone sign-in needs an explicit opt-in
(``accounts.sms_enabled``) and a configured provider; a provider is chosen by
name (``accounts.sms_provider``), and ``none`` disables delivery. Providers
other than the built-in ``tencent`` register under their own names
(:func:`register_sms_provider`); there is no fallback between them.

A provider's ``send`` is synchronous and runs on a worker thread. It returns
whether the provider accepted the message, never raising: provider errors can
carry credentials or the code, so only a bounded code is ever logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cloud_agent.settings import boolean_setting, text_setting

if TYPE_CHECKING:
    from collections.abc import Callable

_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)


class SmsSender(Protocol):
    """Delivers one verification code; tests substitute a recorder."""

    @property
    def configured(self) -> bool:
        """Whether every setting the provider needs is present."""
        ...

    def send_code(self, phone: str, code: str) -> bool:
        """Deliver one code; whether the provider accepted it. Never raises."""
        ...


@dataclass(frozen=True)
class _Registered:
    configured: Callable[[], bool]
    send: Callable[[str, str], bool]


class _RegisteredSender:
    def __init__(self, provider: _Registered) -> None:
        self._provider = provider

    @property
    def configured(self) -> bool:
        return bool(self._provider.configured())

    def send_code(self, phone: str, code: str) -> bool:
        try:
            return bool(self._provider.send(phone, code))
        except Exception:
            return False


_providers: dict[str, _Registered] = {}


def register_sms_provider(
    name: str, *, configured: Callable[[], bool], send: Callable[[str, str], bool]
) -> None:
    """Register a provider under its own name, without replacing another.

    Raises:
        ValueError: An invalid or reserved name, or one already registered.
    """
    if not _PROVIDER_NAME.fullmatch(name) or name in {"none", "tencent"}:
        raise ValueError(f"Invalid or reserved SMS provider name: {name!r}")
    if name in _providers:
        raise ValueError(f"SMS provider already registered: {name!r}")
    _providers[name] = _Registered(configured=configured, send=send)


def configured_sender() -> SmsSender | None:
    """The provider the settings select, or ``None`` when it is ``none`` or unknown."""
    name = text_setting("accounts.sms_provider")
    if name == "tencent":
        from cloud_agent.accounts.sms_tencent import TencentSms

        return TencentSms()
    provider = _providers.get(name)
    return _RegisteredSender(provider) if provider is not None else None


def sms_enabled(sender: SmsSender | None) -> bool:
    """Whether phone sign-in is switched on and its provider is configured."""
    return boolean_setting("accounts.sms_enabled") and sender is not None and sender.configured


__all__ = ["SmsSender", "configured_sender", "register_sms_provider", "sms_enabled"]
