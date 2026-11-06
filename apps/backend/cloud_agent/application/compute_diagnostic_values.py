"""Provider diagnostic values the compute reconciler may keep.

Split from :mod:`cloud_agent.application.compute_reconcile`, which holds the
field allowlists; these validate the values of the identifier and timestamp
fields it copies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID


def _safe_uuid_fields(source: dict[str, Any]) -> dict[str, str]:
    """The provider identifiers in ``source`` that are UUIDs, normalized."""
    details: dict[str, str] = {}
    for key in ("provider_request_id", "external_id", "submission_request_id"):
        value = source.get(key)
        if isinstance(value, str):
            try:
                details[key] = str(UUID(value))
            except ValueError:
                continue
    return details


def _safe_observed_at(observed_at: Any) -> str | None:
    """A timezone-aware ISO timestamp, bounded, or None."""
    if isinstance(observed_at, str):
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                return observed_at[:64]
    return None
