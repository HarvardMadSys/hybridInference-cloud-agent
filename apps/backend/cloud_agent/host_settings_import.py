"""Which of a host's legacy environment values a heartbeat imports as its admin values.

A host's first complete report imports its non-default values once; after
that only a setting registered later (``LATE_IMPORT_KEYS``) that this host has
not yet been offered may import, so an administrator's reset is never undone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.storage._sql.rows import load_json
from cloud_agent_shared.host_settings import (
    HOST_SETTINGS_BY_KEY,
    LATE_IMPORT_KEYS,
    RUNNER_SETTING_KEYS,
    admin_values,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping


def plan_settings_import(
    row: Mapping[str, Any],
    observed: Mapping[str, int | float] | None,
    *,
    global_settings: Mapping[str, Any],
    global_exclusions: Collection[str],
    legacy_sandbox_manager_keys: frozenset[str],
) -> tuple[dict[str, Any], bool, set[str]] | None:
    """``(settings_desired, settings_imported, settings_late_imported)``, or ``None``.

    ``None`` means this report imports nothing: no report, or the first import
    is complete and every late key it carries has been offered before.
    """
    if observed is None:
        return None
    offered = set(row.get("settings_late_imported") or ())
    late = LATE_IMPORT_KEYS & observed.keys()
    if row["settings_imported"] and not late - offered:
        return None
    previous = dict(load_json(row.get("settings_desired")) or {})
    excluded = set(global_exclusions) | set(row.get("settings_import_excluded") or ())
    importable = observed.keys() if not row["settings_imported"] else late - offered
    imports = {
        key: value
        for key, value in admin_values(dict(observed)).items()
        if key in importable
        and value != HOST_SETTINGS_BY_KEY[key].default
        and key not in previous
        and key not in global_settings
        and key not in excluded
    }
    # A runner from before a late key never reports it, so the first import
    # cannot wait for one.
    expected = (RUNNER_SETTING_KEYS - LATE_IMPORT_KEYS) | (
        legacy_sandbox_manager_keys if row.get("sandbox_manager_url") else frozenset()
    )
    complete = bool(row["settings_imported"]) or expected <= observed.keys()
    return {**previous, **imports}, complete, offered | late
