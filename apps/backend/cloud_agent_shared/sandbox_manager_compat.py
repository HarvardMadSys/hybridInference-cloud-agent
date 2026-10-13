"""Compatibility names for upgrading an existing Sandbox Manager deployment.

New code and configuration use Sandbox Manager. Persisted settings retain their
original keys and encryption domains, and old host environments remain readable.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Mapping

_Value = TypeVar("_Value")

# A stable wire ID: old nodes and historical measurements use this value.
SANDBOX_MANAGER_PRODUCER = "broker"

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


def manager_env(name: str, default: str | None = None) -> str | None:
    """Read a canonical environment name, accepting its former spelling."""
    if name in os.environ:
        return os.environ[name]
    return os.environ.get(LEGACY_ENV_NAMES.get(name, name), default)


def manager_environment(values: Mapping[str, _Value]) -> dict[str, _Value]:
    """Add canonical names to an environment without replacing explicit values."""
    result = dict(values)
    for current, legacy in LEGACY_ENV_NAMES.items():
        if current not in result and legacy in result:
            result[current] = result[legacy]
    return result
