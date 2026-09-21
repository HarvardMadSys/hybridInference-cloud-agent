"""The ledger's own window expression."""

from __future__ import annotations

from cloud_agent.storage._sql.attempts import NEXT_ATTEMPT_START_SQL

LEDGER_WINDOW_UNTIL_SQL = f"""
LEAST(
    COALESCE(a.finished_at, NOW()),
    {NEXT_ATTEMPT_START_SQL}
)
"""
