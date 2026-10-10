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
#: read is five seconds old, and never later than a read already scheduled.
#:
#: Unlike an owner's refresh request, this one is recorded while a read is under
#: way: that read may have started before the call it is asked for finished.
#: The claim cleared ``next_refresh_at``, so a value there when the read
#: completes can only be such a request, and completing keeps it.
REQUEST_ATTEMPT_LEDGER_REFRESH_SQL = """
UPDATE agent_attempt_ledger l
SET next_refresh_at = GREATEST(NOW(), l.last_refresh_attempt_at + INTERVAL '5 seconds')
WHERE l.attempt_id = $1
  AND (l.next_refresh_at IS NULL OR l.next_refresh_at > GREATEST(
      NOW(), l.last_refresh_attempt_at + INTERVAL '5 seconds'
  ))
"""


def carries_model_usage(event_type: str, payload: dict[str, object] | None) -> bool:
    """Whether a runtime event reports a model call's usage (as thread stats reads it)."""
    return event_type == "usage" or isinstance((payload or {}).get("usage"), dict)
