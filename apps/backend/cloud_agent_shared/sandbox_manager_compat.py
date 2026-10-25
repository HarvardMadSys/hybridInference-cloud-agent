"""Compatibility names for upgrading an existing Sandbox Manager deployment.

New code and configuration use Sandbox Manager. Persisted settings retain their
original keys and encryption domains, and old host environments remain readable.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Mapping

_Value = TypeVar("_Value")

logger = logging.getLogger(__name__)

# A stable wire ID: old nodes and historical measurements use this value.
SANDBOX_MANAGER_PRODUCER = "broker"

# COMPAT(remove-after=2026-12-31): no deployment sets a former name. Compose and
# the deploy scripts map theirs before Python runs (each marked there), so check
# each deploy root's .env too: sandbox_manager_legacy_env, logged once per name
# and process, sees only what Python reads itself, such as a compute profile.
LEGACY_ENV_NAMES = {
    "AGENT_SANDBOX_MANAGER_TOKEN": "AGENT_WORKSPACE_BROKER_TOKEN",
    "AGENT_SANDBOX_MANAGER_URL": "AGENT_WORKSPACE_BROKER_URL",
    "AGENT_SANDBOX_MANAGER_ADVERTISE_URL": "AGENT_WORKSPACE_BROKER_ADVERTISE_URL",
    "AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS": "AGENT_BROKER_URL_ALLOWED_HOSTS",
    "AGENT_SANDBOX_MANAGER_REVERSE_BIND": "AGENT_BROKER_REVERSE_BIND",
    "AGENT_SANDBOX_MANAGER_REVERSE_PORT": "AGENT_BROKER_REVERSE_PORT",
    "AGENT_SANDBOX_MANAGER_REVERSE_TARGET_HOST": "AGENT_BROKER_REVERSE_TARGET_HOST",
    "AGENT_SANDBOX_MANAGER_REVERSE_TARGET_PORT": "AGENT_BROKER_REVERSE_TARGET_PORT",
    "AGENT_SANDBOX_MANAGER_STOP_GRACE": "AGENT_BROKER_STOP_GRACE",
}

STORED_SETTING_KEYS = {
    "sandbox_manager_token": "workspace_broker_token",
    "sandbox_manager_url_allowed_hosts": "broker_url_allowed_hosts",
}


_REPORTED_LEGACY_NAMES: set[str] = set()


def _report_legacy_name(legacy: str, current: str) -> None:
    """Log, once per process, that a former name supplied a value."""
    if legacy in _REPORTED_LEGACY_NAMES:
        return
    _REPORTED_LEGACY_NAMES.add(legacy)
    logger.warning("sandbox_manager_legacy_env name=%s replacement=%s", legacy, current)


def manager_env(name: str, default: str | None = None) -> str | None:
    """Read a canonical environment name, accepting its former spelling."""
    if name in os.environ:
        return os.environ[name]
    legacy = LEGACY_ENV_NAMES.get(name)
    if legacy is not None and legacy in os.environ:
        _report_legacy_name(legacy, name)
        return os.environ[legacy]
    return default


def manager_environment(values: Mapping[str, _Value]) -> dict[str, _Value]:
    """Add canonical names to an environment without replacing explicit values."""
    result = dict(values)
    for current, legacy in LEGACY_ENV_NAMES.items():
        if current not in result and legacy in result:
            _report_legacy_name(legacy, current)
            result[current] = result[legacy]
    return result
