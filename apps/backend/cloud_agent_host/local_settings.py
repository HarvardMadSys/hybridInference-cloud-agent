"""This host's own value for a runtime setting, before an admin value arrives."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from cloud_agent_host.session_lifecycle import DEFAULT_CHAT_IDLE_TIMEOUT_S
from cloud_agent_host.storage_admission import new_sandbox_storage_error
from cloud_agent_shared.git.source_archive import DEFAULT_MAX_SOURCE_ARCHIVE_BYTES
from cloud_agent_shared.host_settings import HOST_SETTINGS_BY_KEY

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_host.claimed_job import ClaimedJob

DEFAULT_LEASE_TTL_S = 120.0
DEFAULT_AGENT_TIMEOUT_S = float(HOST_SETTINGS_BY_KEY["agent_timeout_s"].default)

# This host's cap on one first-turn checkout: working tree plus the fetched
# commit's Git objects, in MiB. Deployment configuration, never a job's choice.
ENV_SOURCE_IMPORT_MAX_MIB = "AGENT_SOURCE_IMPORT_MAX_MIB"
#: A typo guard, not a statement about any machine.
MAX_SOURCE_IMPORT_MIB = 64 * 1024


def local_shared_resident_capacity() -> int:
    """This host's own sessions-per-sandbox value, or the default for one it cannot use.

    Only a bootstrap value: an admin's value replaces it on the first answer.
    An unusable local value therefore falls back to the default rather than
    keeping this host from advertising shared-live sandboxes at all.
    """
    setting = HOST_SETTINGS_BY_KEY["shared_resident_capacity"]
    raw = (os.environ.get("AGENT_SHARED_RESIDENT_CAPACITY") or "").strip()
    if not raw:
        return setting.default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not setting.accepts(value):
        print(
            f"ignoring {setting.env}={raw!r}: not a whole number from "
            f"{setting.minimum} through {setting.maximum}",
            file=sys.stderr,
        )
        return setting.default
    return value


def _env_float(name: str, fallback: float) -> float:
    """Read a float setting, falling back rather than crash-looping on a typo."""
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        print(f"ignoring {name}={raw!r}: not a number", file=sys.stderr)
        return fallback


def _local_agent_timeout_s() -> float:
    """Read ``AGENT_TIMEOUT_S``, the turn limit until a settings answer replaces it."""
    return _env_float("AGENT_TIMEOUT_S", DEFAULT_AGENT_TIMEOUT_S)


def _chat_idle_timeout_s() -> float:
    """Read the one product-level chat inactivity clock.

    This controls the delay before an otherwise-settled chat container is
    stopped. Agent-turn and terminal limits are independent watchdogs and must
    never be used to decide that a healthy chat is inactive.
    """
    return _env_float("AGENT_CHAT_IDLE_TIMEOUT_S", DEFAULT_CHAT_IDLE_TIMEOUT_S)


def _sandbox_resource_setting(key: str, settings: Mapping[str, float]) -> int:
    """Read a bounded whole-number resource policy, preserving a zero disk quota."""
    setting = HOST_SETTINGS_BY_KEY[key]
    value = settings.get(key)
    if value is None:
        raw = os.environ.get(setting.env)
        try:
            value = int(raw) if raw else setting.default
        except ValueError as exc:
            raise ValueError(f"{setting.label} must be a whole number") from exc
    try:
        checked = int(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{setting.label} must be a whole number") from exc
    if isinstance(value, bool) or value != checked or not setting.accepts(checked):
        raise ValueError(
            f"{setting.label} must be a whole number from "
            f"{setting.minimum} through {setting.maximum} {setting.unit}"
        )
    return checked


def source_import_max_bytes(env: dict[str, str] | None = None) -> int:
    """Return this host's limit on one retained checkout import.

    An unset, malformed or out-of-range value keeps the built-in limit rather
    than failing every turn, as the host's other numeric settings do.
    """
    source = env if env is not None else os.environ
    raw = (source.get(ENV_SOURCE_IMPORT_MAX_MIB) or "").strip()
    if not raw:
        return DEFAULT_MAX_SOURCE_ARCHIVE_BYTES
    try:
        mib = int(raw)
    except ValueError:
        mib = 0
    if not 1 <= mib <= MAX_SOURCE_IMPORT_MIB:
        print(
            f"ignoring {ENV_SOURCE_IMPORT_MAX_MIB}={raw!r}: "
            f"not a whole number of MiB from 1 through {MAX_SOURCE_IMPORT_MIB}",
            file=sys.stderr,
        )
        return DEFAULT_MAX_SOURCE_ARCHIVE_BYTES
    return mib * 1024 * 1024


def _new_sandbox_storage_error(job: ClaimedJob, settings: Mapping[str, float] | None) -> str | None:
    if job.thread_sandbox:
        return None
    definition = HOST_SETTINGS_BY_KEY["storage_min_available_mib"]
    minimum = (settings or {}).get("storage_min_available_mib")
    if minimum is None:
        minimum = _env_float(definition.env, float(definition.default))
    return new_sandbox_storage_error(minimum_mib=minimum)


def _runner_local_settings(
    agent_timeout_s: float, lease_ttl: float = DEFAULT_LEASE_TTL_S
) -> dict[str, float]:
    """This host's own value for each setting the runner applies.

    Legacy bootstrap values, used only before the first settings heartbeat.
    ``agent_timeout_s`` and ``lease_ttl`` were already read from
    ``AGENT_TIMEOUT_S`` and ``AGENT_LEASE_TTL`` or their flags. The admin
    settings replace every value, including defaults, once adopted.
    """
    return {
        "agent_timeout_s": agent_timeout_s,
        "lease_ttl_s": lease_ttl,
        "chat_idle_timeout_s": _chat_idle_timeout_s(),
        "source_import_max_mib": source_import_max_bytes() // (1024 * 1024),
        "storage_min_available_mib": _env_float(
            "AGENT_STORAGE_MIN_AVAILABLE_MIB",
            float(HOST_SETTINGS_BY_KEY["storage_min_available_mib"].default),
        ),
        "shared_resident_capacity": local_shared_resident_capacity(),
        **{
            key: _sandbox_resource_setting(key, {})
            for key in ("sandbox_cpu_millicores", "sandbox_memory_mib", "sandbox_disk_mib")
        },
    }
