"""The ledger's own window expression, and the refresh a model call asks for."""

from __future__ import annotations

from cloud_agent.storage._sql.attempts import NEXT_ATTEMPT_START_SQL

LEDGER_WINDOW_UNTIL_SQL = f"""
LEAST(
    COALESCE(a.finished_at, NOW()),
    {NEXT_ATTEMPT_START_SQL}
)
"""


#: Ask for this attempt's usage to be read again, at once or as soon as the last
#: read is five seconds old. Never later than a read already scheduled, and not
#: while one is under way: the same throttle an owner's refresh request gets.
REQUEST_ATTEMPT_LEDGER_REFRESH_SQL = """
UPDATE agent_attempt_ledger l
SET next_refresh_at = GREATEST(NOW(), l.last_refresh_attempt_at + INTERVAL '5 seconds')
WHERE l.attempt_id = $1
  AND (l.refresh_lease_expires_at IS NULL OR l.refresh_lease_expires_at <= NOW())
  AND (l.next_refresh_at IS NULL OR l.next_refresh_at > GREATEST(
      NOW(), l.last_refresh_attempt_at + INTERVAL '5 seconds'
  ))
"""


def carries_model_usage(event_type: str, payload: dict[str, object] | None) -> bool:
    """Whether a runtime event reports a model call's usage (as thread stats reads it)."""
    return event_type == "usage" or isinstance((payload or {}).get("usage"), dict)
