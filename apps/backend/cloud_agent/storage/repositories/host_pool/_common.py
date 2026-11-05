"""State and private helpers shared by the runner-host pool's mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger
from cloud_agent.storage._sql.rows import load_json

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    import asyncpg


logger = get_logger(__package__)


def _changed_settings(
    stored: Mapping[str, Any], changes: Mapping[str, int | None]
) -> dict[str, Any]:
    """Apply an admin's change to stored settings: set each value, or drop it on None."""
    updated = dict(stored)
    for key, value in changes.items():
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = value
    return updated


class HostPoolBase:
    """The pool every mixin reads, and the host row decoder they share."""

    _pool: asyncpg.Pool

    @staticmethod
    def _host_row_to_dict(row: Any) -> dict[str, Any]:
        """Decode a host row's JSON columns; ``credential_hash`` never leaves."""
        host = dict(row)
        host["labels"] = load_json(host.get("labels")) or {}
        host["capabilities"] = load_json(host.get("capabilities")) or []
        # None (never reported) and {} (reported: no labels) stay distinct.
        host["runtime_versions"] = load_json(host.get("runtime_versions"))
        host["runtime_versions_available"] = load_json(host.get("runtime_versions_available"))
        # Read with ``.get`` like the capacity columns: a row from a projection
        # that predates them must still decode. The report keeps None (never
        # reported) apart from {} (reported, nothing to say).
        host["settings_desired"] = load_json(host.get("settings_desired")) or {}
        host["settings_applied"] = load_json(host.get("settings_applied"))
        host["settings_local"] = load_json(host.get("settings_local"))
        host["config_reports"] = load_json(host.get("config_reports")) or {}
        # Absent from most projections, so read with ``.get``: a row that never
        # selected the capacity columns must decode to "no observation", not
        # raise a KeyError on a path that has nothing to do with capacity.
        if "capacity_snapshot" in host:
            host["capacity_snapshot"] = load_json(host["capacity_snapshot"])
        return host
