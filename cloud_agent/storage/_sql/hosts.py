"""Host column projections and the row mapper every host read returns."""

from __future__ import annotations

from typing import Any

import cloud_agent.storage._sql.hosts as _hosts
import cloud_agent.storage._sql.rows as _rows

_HOST_COLUMN_NAMES = (
    "host",
    "last_worker_id",
    "first_seen_at",
    "last_seen_at",
    "status",
    "labels",
    "capabilities",
    "slots_total",
    "slots_used",
    "runner_version",
    "broker_url",
    "last_heartbeat_at",
    "sandbox_runtime",
    "sandbox_image_digest",
    "runtime_versions",
    "runtime_versions_available",
)


_HOST_COLUMNS = ", ".join(_hosts._HOST_COLUMN_NAMES) + ", (credential_hash IS NOT NULL) AS enrolled"


_QUALIFIED_HOST_COLUMNS = (
    ", ".join(f"h.{column}" for column in _hosts._HOST_COLUMN_NAMES)
    + ", (h.credential_hash IS NOT NULL) AS enrolled"
)


def host_row_to_dict(row: Any) -> dict[str, Any]:
    """Decode a host row's JSON columns; ``credential_hash`` never leaves."""
    host = dict(row)
    host["labels"] = _rows.load_json(host.get("labels")) or {}
    host["capabilities"] = _rows.load_json(host.get("capabilities")) or []
    # None (never reported) and {} (reported: no labels) stay distinct.
    host["runtime_versions"] = _rows.load_json(host.get("runtime_versions"))
    host["runtime_versions_available"] = _rows.load_json(host.get("runtime_versions_available"))
    return host
